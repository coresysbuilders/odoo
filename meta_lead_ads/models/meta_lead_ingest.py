# Meta Lead Ads Integration for Odoo — © 2026 CoreSys Builders. All rights reserved.
# Licensed under the Odoo Proprietary License v1.0 (OPL-1).
# Unauthorized copying, redistribution, or resale of this software, in whole or in
# part, via any medium, is strictly prohibited and constitutes a license violation.
# OPL-1: https://www.odoo.com/documentation/19.0/legal/licenses.html#odoo-apps

# Lead ingestion service. ``ingest_leadgen`` is the only code path that creates
# a crm.lead from Meta data; the webhook, the cron and the manual wizard all
# call it. Idempotency comes from the UNIQUE constraint on meta_leadgen_id,
# not from a Python search, because those callers can race each other.
import logging
import re
from datetime import date, datetime, timezone

from psycopg2 import IntegrityError

from odoo import _, api, fields, models, Command
from odoo.exceptions import AccessError, ValidationError

from .const import (
    DEFAULT_LEAD_NAME, LEAD_NAME_PARAM, VALID_TOKENS,
    NOTIFY_ENABLED_PARAM, NOTIFY_TARGET_PARAM,
    NOTIFY_USER_PARAM, NOTIFY_GROUP_PARAM,
)
from .exceptions import (MetaAuthError, MetaPermanentError,
                         MetaRateLimitError, MetaTransientError)

# Standard Meta keys, always mapped, so a form with no mapping rows still works.
# full_name is the contact person, not the lead title.
CANONICAL = {
    'full_name': 'contact_name',
    'email': 'email_from',
    'phone_number': 'phone',
    'company_name': 'partner_name',
}

_logger = logging.getLogger(__name__)

# Meta's 'platform' value -> crm.lead.meta_platform selection key.
_PLATFORM = {'fb': 'facebook', 'facebook': 'facebook',
             'ig': 'instagram', 'instagram': 'instagram'}

# Meta object ids are letters, digits, '_' and '-'. Anything else is rejected
# before we touch the DB or build a Graph URL with it.
_LEADGEN_RE = re.compile(r'^[A-Za-z0-9_-]+$')

# _TOKEN_RE finds well-formed {word} tokens; rendering substitutes only these
# and leaves any other brace text as-is.
# _BRACE_RE finds any single-brace body so the validator can reject unknown
# tokens and malformed ones like {foo-bar} or {}.
_TOKEN_RE = re.compile(r'\{(\w+)\}')
_BRACE_RE = re.compile(r'\{([^{}]*)\}')

# Strict parsers for typed mappings. An answer that doesn't parse cleanly is
# kept as a meta.lead.answer row instead of being guessed ("$5k" is not 5).
_TRUE_WORDS = frozenset({'true', 'yes', 'y', '1', 'on', 'checked'})
_FALSE_WORDS = frozenset({'false', 'no', 'n', '0', 'off', 'unchecked'})
# Plain or comma-grouped number, optionally with a currency symbol or ISO code.
_NUMBER_RE = re.compile(
    r'^(?:[A-Za-z]{3}\s*)?[$€£¥₹]?\s*'
    r'([-+]?(?:\d{1,3}(?:,\d{3})+|\d+)(?:\.\d+)?)'
    r'\s*(?:[A-Za-z]{3})?$')
_PG_INT_MAX = 2 ** 31 - 1


def parse_meta_time(raw):
    """Parse a Meta ``created_time`` or ISO 8601 string into a naive UTC datetime.

    Returns ``None`` for empty or unparseable input instead of raising, so a
    bad timestamp never drops a lead."""
    if not raw or not isinstance(raw, str):
        return None
    raw = raw.strip()
    parsed = None
    try:
        parsed = datetime.fromisoformat(raw)
    except ValueError:
        # Meta sends "+0000"; Python < 3.11 can't parse that offset.
        for fmt in ('%Y-%m-%dT%H:%M:%S%z', '%Y-%m-%dT%H:%M:%S.%f%z'):
            try:
                parsed = datetime.strptime(raw, fmt)
                break
            except ValueError:
                continue
    if parsed is None:
        return None
    if parsed.tzinfo is not None:
        parsed = parsed.astimezone(timezone.utc).replace(tzinfo=None)
    return parsed


class MetaLeadIngest(models.AbstractModel):
    _name = 'meta.lead.ingest'
    _description = 'Meta Lead idempotent ingestion service'

    # ------------------------------------------------------------------ #
    # Lead titles (used by the create path, the Settings preview and the
    # retroactive rename).
    # ------------------------------------------------------------------ #
    @api.model
    def _validate_lead_name_template(self, template):
        """Raise ``ValidationError`` if the template has an unknown or malformed token.

        Only called when Settings are saved. The create path never validates,
        so a bad stored value can't block a lead. Doubled braces ``{{text}}``
        are an escaped literal and pass.
        """
        # Drop escaped braces first, otherwise _BRACE_RE would match the inner
        # {text} of {{text}} and reject it.
        scan = (template or '').replace('{{', '').replace('}}', '')
        bad = [body for body in _BRACE_RE.findall(scan)
               if body not in VALID_TOKENS]
        if bad:
            raise ValidationError(_(
                "Invalid lead-name template token(s): %(bad)s. "
                "Valid tokens are: %(valid)s.",
                bad=', '.join('{%s}' % b for b in bad),
                valid=', '.join('{%s}' % t for t in VALID_TOKENS),
            ))
        return None

    @api.model
    def render_lead_name(self, template, token_map):
        """Render the lead title from ``template`` and ``token_map``.

        Never raises and never returns an empty string (falls back to
        "Meta Lead"). Only well-formed ``{\\w+}`` tokens are replaced. An empty
        token disappears together with one neighbouring separator, so
        "A / {x} / B" with x empty becomes "A / B", while real text such as
        "Lead from " is kept. Separators inside a value are left alone.

        ``str.format`` isn't used because it raises on unknown keys and leaves
        gaps for empty values.
        """
        template = template or DEFAULT_LEAD_NAME
        token_map = token_map or {}

        # Ordered ('lit', text) / ('val', text) pieces.
        pieces = []
        trim_next_lit_lead = False
        pos = 0

        def _emit_lit(text):
            nonlocal trim_next_lit_lead
            if trim_next_lit_lead:
                # The empty token before us had no separator to eat on its
                # left, so eat the one at the start of this literal.
                text = re.sub(r'^[^\w]+', '', text)
                trim_next_lit_lead = False
            pieces.append(('lit', text))

        def _emit_empty_token():
            nonlocal trim_next_lit_lead
            # Eat the trailing separator of the previous literal if there is
            # one; otherwise eat the leading separator of the next literal.
            if pieces and pieces[-1][0] == 'lit':
                stripped = re.sub(r'[^\w]+$', '', pieces[-1][1])
                if stripped != pieces[-1][1]:
                    pieces[-1] = ('lit', stripped)
                    return
            trim_next_lit_lead = True

        for m in _TOKEN_RE.finditer(template):
            _emit_lit(template[pos:m.start()])
            value = (token_map.get(m.group(1)) or '').strip()
            if value:
                pieces.append(('val', value))
            else:
                _emit_empty_token()
            pos = m.end()
        _emit_lit(template[pos:])

        result = ''.join(text for _kind, text in pieces).strip()
        return result or _("Meta Lead")

    # ------------------------------------------------------------------ #
    # Entry point
    # ------------------------------------------------------------------ #
    @api.model
    def ingest_leadgen(self, page, leadgen_id, trigger='webhook', raw=None):
        """Create or link the crm.lead for a Meta leadgen id, idempotently.

        ``page`` is a ``meta.page`` record (token and app secret come from it).
        Returns the new, matched or already-existing lead.

        Steps: validate the id, return early if it is already known, fetch the
        lead from Graph, resolve attribution, map fields, then either enrich an
        open lead found by email/phone or create a new one, and set UTM.
        """
        leadgen_id = self._validate_leadgen_id(leadgen_id)

        Lead = self.env['crm.lead']
        Log = self.env['meta.sync.log']

        # Already ingested: don't fetch again.
        existing = self._find_by_leadgen_id(leadgen_id)
        if existing:
            Log._record(leadgen_id, trigger, 'skipped_idempotent',
                        lead=existing, raw=raw)
            return existing

        # The 'failed' log row is written in the caller's transaction, so it is
        # rolled back with it if the caller rolls back. Retries and durable
        # failure logging belong to the webhook/cron; don't commit here.
        try:
            data = self.env['meta.graph.client'].fetch_lead(page, leadgen_id)
        except (MetaAuthError, MetaPermanentError,
                MetaRateLimitError, MetaTransientError) as e:
            # No lead and no payload yet, so store the page: a manual retry
            # needs it to re-fetch by leadgen_id.
            Log._record(leadgen_id, trigger, 'failed',
                        match_key='none', error=str(e), page=page)
            raise

        attribution = self._resolve_attribution(page, data)

        vals, answers, description_block = self._map_fields(page, data)

        match, match_key = self._find_dedup_match(Lead, vals)
        # A lead holds one leadgen_id at most. If the match already has this id
        # we lost a race: treat it as a duplicate. If it has a different id,
        # leave it alone and create a new lead; overwriting would break the
        # first submission's idempotency and re-append its answers on the next
        # re-send.
        if match and match.meta_leadgen_id and match.meta_leadgen_id == leadgen_id:
            Log._record(leadgen_id, trigger, 'skipped_idempotent',
                        lead=match, raw=raw)
            return match
        if match and match.meta_leadgen_id and match.meta_leadgen_id != leadgen_id:
            match = Lead.browse()   # already Meta-claimed by a different id
            match_key = None

        if match:
            return self._enrich_match(
                Lead, Log, match, match_key, leadgen_id, trigger,
                vals, attribution, answers, description_block, data, raw)

        # Leads with no email or phone are created too; they are never dropped.
        return self._create_lead(
            Lead, Log, leadgen_id, trigger, vals, attribution,
            answers, description_block, data, raw)

    # ------------------------------------------------------------------ #
    # Input check
    # ------------------------------------------------------------------ #
    @api.model
    def _validate_leadgen_id(self, leadgen_id):
        """Return the stripped leadgen id, or raise if it is empty or not a plain id."""
        lg = (leadgen_id or '').strip()
        if not lg or not _LEADGEN_RE.match(lg):
            raise MetaPermanentError("Invalid leadgen_id: %r" % (leadgen_id,))
        return lg

    # ------------------------------------------------------------------ #
    # Idempotency lookup
    # ------------------------------------------------------------------ #
    @api.model
    def _find_by_leadgen_id(self, leadgen_id):
        """Return the lead already carrying ``leadgen_id``, or an empty recordset.

        This is only a shortcut; the UNIQUE constraint is what actually
        prevents duplicates. Kept as its own method so the concurrency test
        can patch it and force the create path."""
        return self.env['crm.lead'].search(
            [('meta_leadgen_id', '=', leadgen_id)], limit=1)

    # ------------------------------------------------------------------ #
    # Attribution
    # ------------------------------------------------------------------ #
    @api.model
    def _resolve_attribution(self, page, data):
        """Build the meta_* attribution values for the lead.

        Names come from the payload when present; only missing ones are looked
        up through the name cache, one Graph call per missing name at most."""
        Graph = self.env['meta.graph.client']
        token = page.access_token
        # Name lookups need the appsecret_proof too when the app enforces it.
        app_secret = page.account_id.app_secret

        campaign_name = self._resolve_one(
            Graph, token, 'campaign',
            data.get('campaign_id'), data.get('campaign_name'),
            app_secret=app_secret)
        adset_name = self._resolve_one(
            Graph, token, 'adset',
            data.get('adset_id'), data.get('adset_name'),
            app_secret=app_secret)
        ad_name = self._resolve_one(
            Graph, token, 'ad',
            data.get('ad_id'), data.get('ad_name'),
            app_secret=app_secret)
        # Form name: payload first, then the locally synced form (no Graph
        # call), then a Graph lookup as a last resort.
        form = self._resolve_form(page, data.get('form_id'))
        form_name = self._resolve_one(
            Graph, token, 'form',
            data.get('form_id'), data.get('form_name') or form.name,
            app_secret=app_secret)

        platform = _PLATFORM.get((data.get('platform') or '').lower())

        attribution = {
            'meta_campaign_id': data.get('campaign_id') or False,
            'meta_campaign_name': campaign_name or False,
            'meta_adset_id': data.get('adset_id') or False,
            'meta_adset_name': adset_name or False,
            'meta_ad_id': data.get('ad_id') or False,
            'meta_ad_name': ad_name or False,
            'meta_form_id': data.get('form_id') or False,
            'meta_form_name': form_name or False,
            'meta_page_id': page.page_id or False,
            'meta_page_name': page.name or False,
            'meta_submitted_at': parse_meta_time(data.get('created_time'))
            or False,
        }
        if platform:
            attribution['meta_platform'] = platform
        # Optional links; the form may not be synced yet.
        if form:
            attribution['meta_form_id_ref'] = form.id
        attribution['meta_page_id_ref'] = page.id
        if page.account_id:
            attribution['meta_account_id'] = page.account_id.id
        return attribution

    @api.model
    def _resolve_one(self, Graph, token, object_type, graph_id, payload_name,
                     app_secret=None):
        """Return ``payload_name`` if set, else look the name up by id (False if no id)."""
        if payload_name:
            return payload_name
        if not graph_id:
            return False
        return Graph.resolve_name(token, object_type, graph_id,
                                  payload_name=payload_name,
                                  app_secret=app_secret)

    @api.model
    def _resolve_form(self, page, form_id):
        """Return the synced meta.lead.form for this page and form id, if any."""
        if not form_id:
            return self.env['meta.lead.form']
        return self.env['meta.lead.form'].search(
            [('form_id', '=', form_id), ('page_id', '=', page.id)], limit=1)

    # ------------------------------------------------------------------ #
    # Field mapping
    # ------------------------------------------------------------------ #
    @api.model
    def _map_fields(self, page, data):
        """Map Meta field_data to crm.lead values and collect unmapped answers.

        Per-form mappings can add keys but can't remap the CANONICAL ones.
        Each answer is converted to its target field type; if conversion fails
        the answer is kept as an unmapped answer rather than written badly.

        Multi-value answers are joined with ", " for display. The full
        field_data array is stored in meta.sync.log.raw_payload, which is the
        authoritative copy.

        Returns ``(vals, answers, description_block)``.
        """
        form = self._resolve_form(page, data.get('form_id'))
        overrides = {m.meta_key: m.crm_field_id.name
                     for m in form.mapping_ids} if form else {}
        merged = dict(overrides)
        merged.update(CANONICAL)   # canonical keys clobber same-key overrides
        lead_fields = self.env['crm.lead']._fields

        vals = {}
        answers = []
        qa_lines = []
        for entry in data.get('field_data', []):
            name = entry.get('name')
            if not name:
                # No question key to map or store it under. Skip the entry
                # rather than fail the lead; raw_payload still has it.
                continue
            values = entry.get('values') or []
            display = ", ".join(str(v) for v in values)
            target = merged.get(name)
            field = lead_fields.get(target) if target else None
            if field:
                ok, value = self._coerce_answer(field, values, display)
                if ok:
                    vals[target] = value
                    continue
                # Log the key and target only; the answer itself is PII.
                _logger.info(
                    "meta_lead_ads: answer for question %r could not be "
                    "converted to crm.lead.%s (%s); kept as a Meta answer",
                    name, target, field.type)
            answers.append({'question_key': name, 'label': name,
                            'value': display})
            qa_lines.append("%s: %s" % (name, display))
        description_block = (
            "── Meta Lead Ad submission ──\n" + "\n".join(qa_lines)
        ) if qa_lines else ""
        return vals, answers, description_block

    @api.model
    def _coerce_answer(self, field, values, display):
        """Convert one Meta answer to a value for crm.lead ``field``.

        Returns ``(True, value)`` on success or ``(False, None)``, in which
        case the caller keeps the answer as a meta.lead.answer row.

        char/text take the joined string. selection matches an option key or
        label, case-insensitive. boolean takes yes/no style words. Numbers may
        be comma-grouped with a currency symbol or code; an integer field
        refuses fractions and out-of-range values instead of truncating.
        Dates take ISO 8601 or MM/DD/YYYY. many2one needs exactly one matching
        record, many2many one match per value.
        """
        ftype = field.type
        if ftype in ('char', 'text'):
            return True, display
        clean = [str(v).strip() for v in values
                 if v is not None and str(v).strip()]
        if not clean:
            return False, None
        if ftype == 'many2many':
            ids = []
            for value in clean:
                rec = self._match_record(field.comodel_name, value)
                if not rec:
                    return False, None
                ids.append(rec.id)
            return True, [Command.set(ids)]
        if len(clean) != 1:
            return False, None
        raw = clean[0]
        if ftype == 'selection':
            wanted = raw.lower()
            for key, label in field._description_selection(self.env):
                if wanted in (str(key).lower(), str(label).strip().lower()):
                    return True, key
            return False, None
        if ftype == 'boolean':
            word = raw.lower()
            if word in _TRUE_WORDS:
                return True, True
            if word in _FALSE_WORDS:
                return True, False
            return False, None
        if ftype in ('integer', 'float', 'monetary'):
            match = _NUMBER_RE.match(raw)
            if not match:
                return False, None
            number = float(match.group(1).replace(',', ''))
            if ftype == 'integer':
                if number != int(number) or abs(number) > _PG_INT_MAX:
                    return False, None
                return True, int(number)
            return True, number
        if ftype in ('date', 'datetime'):
            moment = parse_meta_time(raw)
            if moment is None:
                try:
                    moment = datetime.strptime(raw, '%m/%d/%Y')
                except ValueError:
                    return False, None
            return True, (moment.date() if ftype == 'date' else moment)
        if ftype == 'many2one':
            rec = self._match_record(field.comodel_name, raw)
            return (True, rec.id) if rec else (False, None)
        return False, None

    @api.model
    def _match_record(self, model_name, value):
        """Return the one record whose name, or failing that ``code``, equals ``value``.

        Case-insensitive. Returns an empty recordset when nothing matches,
        more than one record matches, or the user can't read the model. Never
        creates records."""
        Model = self.env[model_name]
        # =ilike treats % and _ as wildcards; escape them for an exact match.
        pattern = (value.replace('\\', '\\\\')
                   .replace('%', '\\%').replace('_', '\\_'))
        rec_name = Model._rec_name or 'name'
        for fname in (rec_name, 'code'):
            field = Model._fields.get(fname)
            if field is None:
                continue
            # Odoo truncates the search value to a sized Char's length, so
            # "Atlantis" on res.country.code (size 2) would search "At" and hit
            # Austria. A longer value can't match anyway, so skip.
            if getattr(field, 'size', None) and len(value) > field.size:
                continue
            try:
                found = Model.search([(fname, '=ilike', pattern)], limit=2)
            except (AccessError, ValueError):
                return Model.browse()
            if len(found) == 1:
                return found
            if found:
                return Model.browse()   # ambiguous, don't guess
        return Model.browse()

    # ------------------------------------------------------------------ #
    # Business dedup (email, then phone)
    # ------------------------------------------------------------------ #
    @api.model
    def _find_dedup_match(self, Lead, vals):
        """Find an open lead (active, not won) by email, then by normalized phone.

        Returns ``(lead, match_key)``."""
        match = Lead.browse()
        match_key = None
        if vals.get('email_from'):
            match = Lead.search([('email_from', '=ilike', vals['email_from']),
                                 ('active', '=', True),
                                 ('stage_id.is_won', '=', False)], limit=1)
            match_key = 'email' if match else None
        if not match and vals.get('phone'):
            norm = self._normalize_phone(vals['phone'])
            if norm:
                # meta_phone_normalized is computed with the same
                # _normalize_phone, so an indexed '=' lookup is enough.
                match = Lead.search([('meta_phone_normalized', '=', norm),
                                     ('active', '=', True),
                                     ('stage_id.is_won', '=', False)], limit=1)
                match_key = 'phone' if match else None
        return match, match_key

    @api.model
    def _normalize_phone(self, raw):
        """Reduce a phone number to digits, dropping a leading NANP "1".

        So '+1 (555) 123-4567' and '5551234567' compare equal. Avoids a
        dependency on the phonenumbers library."""
        if not raw:
            return False
        digits = re.sub(r'\D', '', raw)
        if len(digits) == 11 and digits.startswith('1'):
            digits = digits[1:]
        return digits or False

    # ------------------------------------------------------------------ #
    # Match path
    # ------------------------------------------------------------------ #
    @api.model
    def _enrich_match(self, Lead, Log, match, match_key, leadgen_id, trigger,
                      vals, attribution, answers, description_block, data, raw):
        """Update an existing open lead with this submission.

        Fills only blank fields (mapped answers and Meta attribution), so a
        salesperson's edits are never overwritten, appends the answers, and
        stamps meta_leadgen_id so a re-send is caught by the idempotency check.
        The stamp is written in a savepoint and flushed so a concurrent claim
        of the same id surfaces as an IntegrityError here."""
        # The caller only passes matches with no leadgen id yet.
        enrich = {k: v for k, v in vals.items() if v and not match[k]}
        enrich.update({k: v for k, v in attribution.items()
                       if v and not match[k]})
        enrich['meta_leadgen_id'] = leadgen_id
        try:
            with self.env.cr.savepoint():
                match.write(enrich)
                match.flush_recordset()   # force the UNIQUE check in the savepoint
        except IntegrityError:
            # Another transaction stamped this leadgen_id first.
            winner = self.env['crm.lead'].search(
                [('meta_leadgen_id', '=', leadgen_id)], limit=1)
            Log._record(leadgen_id, trigger, 'skipped_idempotent',
                        lead=winner, raw=raw)
            return winner
        self._apply_answers(match, answers, description_block)
        # Keep any first-touch source/medium/campaign the lead already has
        # (e.g. Website).
        self._apply_utm(match, data, attribution.get('meta_campaign_name'),
                        only_blank=True)
        Log._record(leadgen_id, trigger, 'success',
                    lead=match, match_key=match_key, raw=data)
        return match

    # ------------------------------------------------------------------ #
    # Create path
    # ------------------------------------------------------------------ #
    @api.model
    def _create_lead(self, Lead, Log, leadgen_id, trigger, vals, attribution,
                     answers, description_block, data, raw):
        """Create the new crm.lead for this leadgen id and return it.

        On a UNIQUE violation (another worker got there first) returns the
        existing lead and logs the skip."""
        # Use the stored UTC submission date so the title matches
        # meta_submitted_at.
        submitted = attribution.get('meta_submitted_at')
        day = fields.Date.to_string(submitted.date() if submitted
                                    else date.today())
        # The template isn't validated here: a bad value stored outside
        # Settings must not block a lead, and render_lead_name falls back to
        # the default title anyway.
        template = self.env['ir.config_parameter'].sudo().get_param(
            LEAD_NAME_PARAM, DEFAULT_LEAD_NAME)
        token_map = {
            'form_name': attribution.get('meta_form_name') or '',
            'campaign_name': attribution.get('meta_campaign_name') or '',
            'adset_name': attribution.get('meta_adset_name') or '',
            'ad_name': attribution.get('meta_ad_name') or '',
            'contact_name': vals.get('contact_name') or '',
            'page_name': attribution.get('meta_page_name') or '',
            'date': day,
        }
        subject = self.render_lead_name(template, token_map)

        create_vals = dict(vals)
        create_vals.update(attribution)   # meta_* fields + meta_platform + refs
        create_vals.update({'name': subject, 'type': 'lead',
                            'meta_leadgen_id': leadgen_id,
                            # Lets the retroactive rename update this title
                            # until someone edits it by hand.
                            'meta_name_is_generated': True})
        try:
            with self.env.cr.savepoint():
                lead = Lead.create(create_vals)
                lead.flush_recordset()   # force the UNIQUE check in the savepoint
        except IntegrityError:
            lead = self.env['crm.lead'].search(
                [('meta_leadgen_id', '=', leadgen_id)], limit=1)
            Log._record(leadgen_id, trigger, 'skipped_idempotent',
                        lead=lead, raw=raw)
            return lead
        self._apply_answers(lead, answers, description_block)
        self._apply_utm(lead, data, attribution.get('meta_campaign_name'))
        Log._record(leadgen_id, trigger, 'success',
                    lead=lead, match_key='none', raw=data)
        # Only new leads notify; matches and duplicates never get here.
        self._notify_new_lead(lead)
        return lead

    # ------------------------------------------------------------------ #
    # New-lead notification
    # ------------------------------------------------------------------ #
    @api.model
    def _notify_new_lead(self, lead):
        """Notify the configured user, or the active members of the configured group.

        Off by default. Any error is logged and swallowed so a notification
        problem can never undo the lead that was just created.
        """
        try:
            ICP = self.env['ir.config_parameter'].sudo()
            if ICP.get_param(NOTIFY_ENABLED_PARAM) != '1':
                return
            target_type = ICP.get_param(NOTIFY_TARGET_PARAM, 'user')
            if target_type == 'group':
                gid = ICP.get_param(NOTIFY_GROUP_PARAM)
                # Blank or non-numeric ids give an empty recordset.
                group = self.env['res.groups'].sudo().browse(
                    int(gid)) if (gid or '').strip().isdigit() \
                    else self.env['res.groups']
                # Odoo 19 renamed res.groups.users to user_ids (direct members).
                users = group.user_ids.filtered('active') if group.exists() \
                    else self.env['res.users']
            else:
                uid = ICP.get_param(NOTIFY_USER_PARAM)
                user = self.env['res.users'].sudo().browse(
                    int(uid)) if (uid or '').strip().isdigit() \
                    else self.env['res.users']
                users = user.filtered('active') if user.exists() \
                    else self.env['res.users']
            partners = users.partner_id
            if not partners:
                return
            body = _("A new lead arrived from Meta Lead Ads: %s") % (
                lead.name or _("Meta Lead"))
            self._post_inbox_alert(lead, partners, _("New Meta lead"), body)
        except Exception:   # noqa: BLE001 - a failed notification must not break ingest
            _logger.warning(
                "meta_lead_ads: new-lead notification failed for lead id=%s "
                "(ingestion unaffected)", lead.id, exc_info=True)

    @api.model
    def _post_inbox_alert(self, lead, partners, subject, body):
        """Put an alert in each recipient's Odoo Inbox.

        Creates the mail.message and inbox mail.notification rows directly
        instead of using message_notify, because:
        - recipients who prefer email notifications would otherwise depend on
          SMTP, and a broken mail server meant these alerts were lost;
        - third-party _notify_thread overrides (e.g. a web-push module that
          raises UserError when unconfigured) can't abort it.

        ``reply_to`` and ``record_name`` are set explicitly to skip the
        catchall alias lookup, which can raise KeyError on a just-created
        record. As a user_notification the message stays off the lead's
        chatter.
        """
        message = self.env['mail.message'].sudo().create({
            'model': 'crm.lead',
            'res_id': lead.id,
            'message_type': 'user_notification',
            'subtype_id': self.env.ref('mail.mt_note').id,
            'author_id': self.env.ref('base.partner_root').id,
            'subject': subject,
            'body': body,
            'reply_to': self.env.company.email_formatted or '',
            'record_name': lead.name or _("Meta Lead"),
            'partner_ids': [Command.set(partners.ids)],
        })
        self.env['mail.notification'].sudo().create([{
            'mail_message_id': message.id,
            'res_partner_id': pid,
            'notification_type': 'inbox',
            'notification_status': 'sent',
            'is_read': False,
        } for pid in partners.ids])
        return message

    # ------------------------------------------------------------------ #
    # Answers
    # ------------------------------------------------------------------ #
    @api.model
    def _apply_answers(self, lead, answers, description_block):
        """Add answer rows and append the Q&A block to the description; never replace."""
        if answers:
            lead.write({'answer_ids': [(0, 0, a) for a in answers]})
        if description_block:
            existing_desc = lead.description or ""
            new_desc = (existing_desc + "\n\n" + description_block
                        if existing_desc else description_block)
            lead.write({'description': new_desc})

    # ------------------------------------------------------------------ #
    # UTM
    # ------------------------------------------------------------------ #
    @api.model
    def _apply_utm(self, lead, data, campaign_name=None, only_blank=False):
        """Set UTM source, medium and campaign on the lead.

        Facebook uses the stock utm source, Instagram the one this module
        seeds; medium is the seeded Paid Social. ``campaign_name`` is the
        resolved name, preferred over the payload so cron-fetched leads that
        lack the name in the payload still get a campaign. With
        ``only_blank`` (match path) existing UTM values are kept."""
        platform = _PLATFORM.get((data.get('platform') or '').lower())
        if platform == 'facebook':
            source = self.env.ref('utm.utm_source_facebook',
                                  raise_if_not_found=False)
        elif platform == 'instagram':
            source = self.env.ref('meta_lead_ads.utm_source_meta_instagram',
                                  raise_if_not_found=False)
        else:
            source = self.env['utm.source']
        medium = self.env.ref('meta_lead_ads.utm_medium_paid_social',
                              raise_if_not_found=False)
        uvals = {}
        if source:
            uvals['source_id'] = source.id
        if medium:
            uvals['medium_id'] = medium.id
        camp = self._get_or_create_campaign(
            campaign_name or data.get('campaign_name'))
        if camp:
            uvals['campaign_id'] = camp.id
        if only_blank:
            uvals = {k: v for k, v in uvals.items() if not lead[k]}
        if uvals:
            lead.write(uvals)

    @api.model
    def _get_or_create_campaign(self, name):
        """Return the utm.campaign with this name (case-insensitive), creating it if needed."""
        Campaign = self.env['utm.campaign']
        if not name or not name.strip():
            return Campaign
        clean = name.strip()
        lock_key = 'meta_lead_ads.utm.campaign:%s' % clean.lower()
        # Lock so two workers don't both create the same campaign.
        self.env.cr.execute(
            "SELECT pg_advisory_xact_lock(hashtext(%s)::bigint)", (lock_key,))
        rec = Campaign.search([('name', '=ilike', clean)], limit=1)
        if rec:
            return rec
        try:
            with self.env.cr.savepoint():
                rec = Campaign.create({'name': clean})
                rec.flush_recordset()
                return rec
        except IntegrityError:
            rec = Campaign.search([('name', '=ilike', clean)], limit=1)
            if rec:
                return rec
            raise
