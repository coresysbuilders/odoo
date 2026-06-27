# Meta Lead Ads Integration for Odoo — © 2026 CoreSys Builders. All rights reserved.
# Licensed under the Odoo Proprietary License v1.0 (OPL-1).
# Unauthorized copying, redistribution, or resale of this software, in whole or in
# part, via any medium, is strictly prohibited and constitutes a license violation.
# OPL-1: https://www.odoo.com/documentation/19.0/legal/licenses.html#odoo-apps

# Single idempotent ingestion service — the core of the integration.
# Load-bearing rules: one create surface, idempotency via a DB UNIQUE
# constraint (not a Python search), two-layer dedup, lossless capture (no
# ir.model.fields creation in the hot path), and UTM get-or-create.
#
# This AbstractModel's ``ingest_leadgen`` is the only code path that turns a
# (page, leadgen_id) into a crm.lead from Meta data. The webhook, the cron and
# the manual wizard are all thin wrappers over this stable signature.
import logging
import re
from datetime import date

from psycopg2 import IntegrityError

from odoo import _, api, fields, models, Command
from odoo.exceptions import ValidationError

from .const import (
    DEFAULT_LEAD_NAME, LEAD_NAME_PARAM, VALID_TOKENS,
    NOTIFY_ENABLED_PARAM, NOTIFY_TARGET_PARAM,
    NOTIFY_USER_PARAM, NOTIFY_GROUP_PARAM,
)
from .exceptions import (MetaAuthError, MetaPermanentError,
                         MetaRateLimitError, MetaTransientError)

# Built-in canonical Meta keys, always on. A form with zero meta.field.mapping
# rows still maps out of the box; full_name is the person (contact_name), never
# the subject line.
CANONICAL = {
    'full_name': 'contact_name',
    'email': 'email_from',
    'phone_number': 'phone',
    'company_name': 'partner_name',
}

_logger = logging.getLogger(__name__)

# Meta lead payload 'platform' -> crm.lead.meta_platform Selection.
_PLATFORM = {'fb': 'facebook', 'facebook': 'facebook',
             'ig': 'instagram', 'instagram': 'instagram'}

# Meta object ids are digits/letters/_/- only. Reject anything else (empty,
# whitespace, '/'- or '?'-bearing) before any DB/Graph call — defense-in-depth
# on top of _request's '://'/'?' path guard.
_LEADGEN_RE = re.compile(r'^[A-Za-z0-9_-]+$')

# Lead-name template tokenizers (local private compiled regexes — a compiled
# regex stays in the file that uses it; the constants it checks against are the
# PUBLIC ones imported from .const).
#   _TOKEN_RE  — matches a well-formed {\w+} token; render substitutes ONLY
#                these. A malformed brace body stays literal at render time
#                (render never validates, never raises).
#   _BRACE_RE  — matches ANY single-brace body ([^{}]*) so the validator can
#                reject brace bodies that are not a known \w+ token (unknown
#                tokens AND malformed bodies like {foo-bar}/{}/{form bar}).
#                Doubled braces {{...}} are NOT matched as a single-brace body
#                (the inner content carries braces) → escaped literal, no raise.
_TOKEN_RE = re.compile(r'\{(\w+)\}')
_BRACE_RE = re.compile(r'\{([^{}]*)\}')


class MetaLeadIngest(models.AbstractModel):
    _name = 'meta.lead.ingest'
    _description = 'Meta Lead idempotent ingestion service'

    # ------------------------------------------------------------------ #
    # Lead-name composition (shared by the create path, the Settings preview
    # and the retro-rename — one place that renders titles for all of them).
    # ------------------------------------------------------------------ #
    @api.model
    def _validate_lead_name_template(self, template):
        """Raise ``ValidationError`` if ``template`` references an unknown or
        malformed token; return ``None`` when every braced body is a legal
        ``VALID_TOKENS`` member.

        Validation runs at the Settings WRITE boundary only
        (``set_values``) — it is the strict gate. The create path NEVER calls
        this (it renders defensively so a junk param can never drop a paid
        lead).

        Policy:
          - a single-brace body that is not a ``VALID_TOKENS`` member is bad —
            this rejects both unknown ``\\w+`` tokens (``{foo}``) AND malformed
            bodies (``{foo-bar}``, ``{}``, ``{form bar}``);
          - doubled braces ``{{literal}}`` are an escaped literal: ``_BRACE_RE``
            (``[^{}]*``) never matches them as a single-brace body, so they are
            passed through without raising.
        """
        # Escaped braces ``{{`` / ``}}`` are literals (str.format semantics).
        # Strip them BEFORE scanning: otherwise ``_BRACE_RE`` slides to the inner
        # ``{literal}`` brace pair inside ``{{literal}}`` and wrongly rejects it
        # (the escaped literal must pass). Single ``{token}`` bodies are
        # untouched by this strip.
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
        """Render ``template`` against ``token_map`` → the lead title.

        Contract:
          - only well-formed ``{\\w+}`` tokens are substituted; a malformed
            brace stays literal (render NEVER validates, NEVER raises);
          - a token resolving empty vanishes AND consumes one adjacent
            separator run (the dangling separator), so ``A • {x} • B`` with an
            empty ``x`` collapses to ``A • B``, while a real non-separator
            literal (``"Lead from "``) is preserved;
          - the final tidy only trims dangling separators at the empty-token
            seams and edge whitespace — it does NOT globally collapse
            separators, so a VALUE that itself contains ``" • "`` survives
            verbatim;
          - the result is NEVER empty: an all-empty render falls back to the
            byte-exact default subject ``"Meta Lead"``.

        ``str.format`` is deliberately NOT used: it raises ``KeyError`` on an
        unknown key and leaves empty-token gaps (Don't-Hand-Roll).
        """
        template = template or DEFAULT_LEAD_NAME
        token_map = token_map or {}

        # pieces: ordered list of ('lit', text) / ('val', text). Build by
        # walking the well-formed tokens; the gaps between matches are literals.
        pieces = []
        trim_next_lit_lead = False
        pos = 0

        def _emit_lit(text):
            nonlocal trim_next_lit_lead
            if trim_next_lit_lead:
                # An empty token just before this literal had no preceding
                # separator to eat → drop this literal's LEADING separator run.
                text = re.sub(r'^[^\w]+', '', text)
                trim_next_lit_lead = False
            pieces.append(('lit', text))

        def _emit_empty_token():
            nonlocal trim_next_lit_lead
            # Prefer eating the preceding literal's TRAILING separator run;
            # if there is none, mark the next literal's leading run for removal.
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
    # Public entry point — the single create surface.
    # ------------------------------------------------------------------ #
    @api.model
    def ingest_leadgen(self, page, leadgen_id, trigger='webhook', raw=None):
        """Turn a (page, leadgen_id) into a crm.lead idempotently.

        ``page`` is a ``meta.page`` recordset (carries ``access_token`` and
        ``account_id.app_secret``). Returns the created / linked / pre-existing
        ``crm.lead``. This is the only place a crm.lead is created from Meta
        data; the webhook, the cron and the manual wizard all wrap this method.

        Fixed pipeline:
          0. validate leadgen_id
          1. idempotency pre-check (no re-fetch on a known id)
          2. Graph fetch (typed-exception -> 'failed' log + re-raise)
          3. attribution resolve (payload-first, resolve_name only on miss)
          4. field mapping (canonical wins over per-form override)
          5. two-layer business dedup + guarded leadgen stamp
          5a. MATCH path  — enrich-blank-only + append answers + stamp id
          5b. CREATE path — the sole create (savepoint + flush + IntegrityError)
          6. UTM get-or-create
        """
        leadgen_id = self._validate_leadgen_id(leadgen_id)

        Lead = self.env['crm.lead']
        Log = self.env['meta.sync.log']

        # STEP 1 — idempotency pre-check. Named seam so concurrency tests can
        # drive the create path while the row already exists.
        existing = self._find_by_leadgen_id(leadgen_id)
        if existing:
            Log._record(leadgen_id, trigger, 'skipped_idempotent',
                        lead=existing, raw=raw)
            return existing

        # STEP 2 — Graph fetch. Typed Graph errors -> 'failed' log + re-raise.
        # Known limitation (accepted): the 'failed' row is written then the
        # exception is re-raised; if the caller's transaction rolls back
        # (webhook/cron) this log row rolls back with it. Durable failure
        # logging / retry is owned by those callers — do not add a manual cursor
        # commit here.
        try:
            data = self.env['meta.graph.client'].fetch_lead(page, leadgen_id)
        except (MetaAuthError, MetaPermanentError,
                MetaRateLimitError, MetaTransientError) as e:
            # Pre-fetch failure case — this row has no lead and no raw_payload,
            # so a later manual retry has nothing to navigate from. Stamp the
            # owning `page` (the method's first arg, in scope here) so
            # _resolve_page can re-fetch from Graph by leadgen_id. The
            # lead-bearing rows resolve via lead_id and do not pass page=.
            Log._record(leadgen_id, trigger, 'failed',
                        match_key='none', error=str(e), page=page)
            raise

        # STEP 3 — ATTRIBUTION (payload-first; resolve_name only for missing).
        attribution = self._resolve_attribution(page, data)

        # STEP 4 — field map (canonical-wins merge; lossless unmapped capture).
        vals, answers, description_block = self._map_fields(page, data)

        # STEP 5 — business dedup (email->phone) + guarded leadgen stamp.
        match, match_key = self._find_dedup_match(Lead, vals)
        # meta_leadgen_id is a single-valued DB-UNIQUE key — a crm.lead carries
        # at most one leadgen_id.
        #   * match already carries the SAME leadgen_id -> cannot happen past
        #     the Step-1 pre-check; on a race, treat as skipped_idempotent.
        #   * match already carries a DIFFERENT leadgen_id -> do not overwrite,
        #     do not merge. Discard the match so we fall through to the create
        #     path (5b) and spawn a new lead stamped with this leadgen_id, like
        #     a closed-lead non-match. The old lead is left untouched, so both
        #     leadgen_ids stay independently idempotent. Overwriting would
        #     silently destroy the first submission's idempotency identity
        #     (re-process + re-append answers on the next re-send / cron sweep).
        if match and match.meta_leadgen_id and match.meta_leadgen_id == leadgen_id:
            Log._record(leadgen_id, trigger, 'skipped_idempotent',
                        lead=match, raw=raw)
            return match
        if match and match.meta_leadgen_id and match.meta_leadgen_id != leadgen_id:
            match = Lead.browse()   # already Meta-claimed by a different id
            match_key = None

        # STEP 5a — match path (only a usable, un-stamped match reaches here).
        if match:
            return self._enrich_match(
                Lead, Log, match, match_key, leadgen_id, trigger,
                vals, attribution, answers, description_block, data, raw)

        # STEP 5b — create path (sole create; also the different-id discard,
        # with match_key='none'). Contact-less leads reach here too and are
        # created with status='success' — never dropped.
        return self._create_lead(
            Lead, Log, leadgen_id, trigger, vals, attribution,
            answers, description_block, data, raw)

    # ------------------------------------------------------------------ #
    # STEP 0 — input guard.
    # ------------------------------------------------------------------ #
    @api.model
    def _validate_leadgen_id(self, leadgen_id):
        """Reject empty / whitespace-only / path-or-query-bearing ids before any
        DB search or Graph call. Returns the cleaned (stripped) id."""
        lg = (leadgen_id or '').strip()
        if not lg or not _LEADGEN_RE.match(lg):
            raise MetaPermanentError("Invalid leadgen_id: %r" % (leadgen_id,))
        return lg

    # ------------------------------------------------------------------ #
    # STEP 1 — idempotency pre-check seam (patched by the concurrency test).
    # ------------------------------------------------------------------ #
    @api.model
    def _find_by_leadgen_id(self, leadgen_id):
        """The Step-1 idempotency seam — relies on the DB UNIQUE constraint, not
        a Python search of business fields. Returns the crm.lead already carrying
        ``leadgen_id`` or an empty recordset. Kept as an explicit named method so
        the service-path concurrency test can patch it to empty once and drive
        the create/flush path even though the row already exists."""
        return self.env['crm.lead'].search(
            [('meta_leadgen_id', '=', leadgen_id)], limit=1)

    # ------------------------------------------------------------------ #
    # STEP 3 — attribution (payload-first, resolve_name only for misses).
    # ------------------------------------------------------------------ #
    @api.model
    def _resolve_attribution(self, page, data):
        """Build the meta_* attribution vals from the payload. Prefer the
        payload ``*_name``; resolve only the missing names via
        meta.graph.client.resolve_name (payload-first / cache-on-miss: zero
        per-lead Graph calls when the payload carries the name; exactly one
        resolve per missing name)."""
        Graph = self.env['meta.graph.client']
        token = page.access_token
        # Send the appsecret_proof on cache-miss name lookups too (mirrors
        # fetch_lead), so resolution still works when the app requires it.
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
        # Resolve the local meta.lead.form once (reused for the m2o ref below).
        # Form-name precedence: payload form_name -> already-synced local form
        # name (zero Graph traffic) -> cache-on-miss Graph resolve as a last
        # resort. The form is synced at onboarding, so a known form costs no
        # per-lead Graph call.
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
        }
        if platform:
            attribution['meta_platform'] = platform
        # Optional m2o navigation refs (best-effort; nullable by design).
        # `form` was already resolved above for the form-name fallback.
        if form:
            attribution['meta_form_id_ref'] = form.id
        attribution['meta_page_id_ref'] = page.id
        if page.account_id:
            attribution['meta_account_id'] = page.account_id.id
        return attribution

    @api.model
    def _resolve_one(self, Graph, token, object_type, graph_id, payload_name,
                     app_secret=None):
        """resolve_name wrapper: returns the payload name verbatim (zero Graph
        traffic) when present; otherwise resolves via the cache-on-miss
        resolve_name ONLY when there is an id to resolve. ``app_secret`` is
        threaded through so the cache-miss lookup sends the appsecret_proof."""
        if payload_name:
            return payload_name
        if not graph_id:
            return False
        return Graph.resolve_name(token, object_type, graph_id,
                                  payload_name=payload_name,
                                  app_secret=app_secret)

    @api.model
    def _resolve_form(self, page, form_id):
        """Best-effort meta.lead.form lookup for the form name + override map."""
        if not form_id:
            return self.env['meta.lead.form']
        return self.env['meta.lead.form'].search(
            [('form_id', '=', form_id), ('page_id', '=', page.id)], limit=1)

    # ------------------------------------------------------------------ #
    # STEP 4 — field mapping (canonical wins over per-form override).
    # ------------------------------------------------------------------ #
    @api.model
    def _map_fields(self, page, data):
        """Map field_data to crm.lead vals + capture unmapped questions.

        Always-on semantics: the per-form override map may add new Meta keys but
        must not remap a canonical key. We seed from overrides then let CANONICAL
        clobber any override that targets a canonical Meta key.

        Lossless capture: Meta field_data[].values is an array. The crm field and
        the meta.lead.answer.value / description use the deterministic
        ", ".join(...) display string. The full untruncated field_data array is
        preserved losslessly in meta.sync.log.raw_payload (set via
        _record(..., raw=data)), which is the authoritative copy — the join is a
        display convenience, never the only copy.
        """
        form = self._resolve_form(page, data.get('form_id'))
        overrides = {m.meta_key: m.crm_field_id.name
                     for m in form.mapping_ids} if form else {}
        merged = dict(overrides)
        merged.update(CANONICAL)   # canonical keys clobber same-key overrides

        vals = {}
        answers = []
        qa_lines = []
        for entry in data.get('field_data', []):
            name = entry.get('name')
            if not name:
                # Malformed entry with no question key: nothing to map and
                # nothing to key a required meta.lead.answer row on. Skip it
                # rather than crash the whole ingest (never drop a paid lead).
                # The full entry is still preserved losslessly in
                # meta.sync.log.raw_payload, the authoritative copy.
                continue
            values = entry.get('values') or []
            display = ", ".join(str(v) for v in values)
            target = merged.get(name)
            if target:
                vals[target] = display
            else:
                answers.append({'question_key': name, 'label': name,
                                'value': display})
                qa_lines.append("%s: %s" % (name, display))
        description_block = (
            "── Meta Lead Ad submission ──\n" + "\n".join(qa_lines)
        ) if qa_lines else ""
        return vals, answers, description_block

    # ------------------------------------------------------------------ #
    # STEP 5 — business dedup candidate search (email -> normalized phone).
    # ------------------------------------------------------------------ #
    @api.model
    def _find_dedup_match(self, Lead, vals):
        """Find an open (active + not won) crm.lead by email (=ilike) then
        normalized phone. Returns (match_recordset, match_key)."""
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
                # Indexed SQL '=' on the stored normalized column:
                # crm.lead.meta_phone_normalized is computed by the same
                # _normalize_phone, so the stored value matches this lookup key.
                # No more loading every open phone-bearing lead into memory.
                match = Lead.search([('meta_phone_normalized', '=', norm),
                                     ('active', '=', True),
                                     ('stage_id.is_won', '=', False)], limit=1)
                match_key = 'phone' if match else None
        return match, match_key

    @api.model
    def _normalize_phone(self, raw):
        """Self-contained phone normalizer — strip to digits (no phonenumbers
        dependency). Drop a leading NANP country code so '+1 (555) 123-4567' and
        '5551234567' normalize equal."""
        if not raw:
            return False
        digits = re.sub(r'\D', '', raw)
        if len(digits) == 11 and digits.startswith('1'):
            digits = digits[1:]
        return digits or False

    # ------------------------------------------------------------------ #
    # STEP 5a — MATCH PATH (enrich-blank-only + append answers + stamp id).
    # ------------------------------------------------------------------ #
    @api.model
    def _enrich_match(self, Lead, Log, match, match_key, leadgen_id, trigger,
                      vals, attribution, answers, description_block, data, raw):
        """Reached only for a usable (un-stamped) match — the Step-5 gate already
        returned/discarded any already-claimed match. Enrich blank fields only
        (never clobber a salesperson edit), stamp meta_leadgen_id (so a
        re-send/re-sweep hits the Step-1 pre-check), append answers (never
        replace). The stamp goes through the same savepoint + flush_recordset +
        IntegrityError idiom as the create path (DB UNIQUE, not Python
        search)."""
        # Stamp only when currently empty (guaranteed above).
        enrich = {k: v for k, v in vals.items() if v and not match[k]}
        enrich['meta_leadgen_id'] = leadgen_id
        try:
            with self.env.cr.savepoint():
                match.write(enrich)
                match.flush_recordset()   # force the UNIQUE check in the savepoint
        except IntegrityError:
            # Another txn claimed this leadgen_id concurrently — re-read & skip.
            winner = self.env['crm.lead'].search(
                [('meta_leadgen_id', '=', leadgen_id)], limit=1)
            Log._record(leadgen_id, trigger, 'skipped_idempotent',
                        lead=winner, raw=raw)
            return winner
        self._apply_answers(match, answers, description_block)
        self._apply_utm(match, data, attribution.get('meta_campaign_name'))
        Log._record(leadgen_id, trigger, 'success',
                    lead=match, match_key=match_key, raw=data)
        return match

    # ------------------------------------------------------------------ #
    # STEP 5b — create path (the sole create; builds subject + type='lead').
    # ------------------------------------------------------------------ #
    @api.model
    def _create_lead(self, Lead, Log, leadgen_id, trigger, vals, attribution,
                     answers, description_block, data, raw):
        """Create the single new crm.lead for this leadgen_id (true non-match or
        a different-id discard, both match_key='none'). Contact-less leads reach
        here and are created with status='success'."""
        created = data.get('created_time')
        day = (created[:10] if created
               else fields.Date.to_string(date.today()))
        # DEFENSIVE read: the title is composed
        # from the configured template, but the create path NEVER calls the
        # hard validator — a junk value written outside Settings must not
        # drop a paid lead. render_lead_name never raises and never returns
        # empty, so a bad template falls back to the default subject. With the
        # param unset, get_param returns DEFAULT_LEAD_NAME and the title is
        # today's "Meta Lead • <form> • <day>" byte-for-byte.
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
                            # Stamp title provenance: this title is
                            # the Meta-rendered subject, so the retro rename may
                            # re-apply a new template to it until a human edits it.
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
        # Only a genuinely new lead notifies — the dedup/enrich path (5a) and the
        # idempotent skips never reach here.
        self._notify_new_lead(lead)
        return lead

    # ------------------------------------------------------------------ #
    # New-lead notification (best-effort; never blocks ingestion).
    # ------------------------------------------------------------------ #
    @api.model
    def _notify_new_lead(self, lead):
        """Post an inbox notification about a freshly-created Meta lead to the
        configured user, or to every active member of the configured group.

        Driven by the Settings keys (disabled by default). Wrapped so any
        failure here is logged and swallowed — notifying is a side effect and
        must never roll back or abort the lead creation it follows.
        """
        try:
            ICP = self.env['ir.config_parameter'].sudo()
            if ICP.get_param(NOTIFY_ENABLED_PARAM) != '1':
                return
            target_type = ICP.get_param(NOTIFY_TARGET_PARAM, 'user')
            if target_type == 'group':
                gid = ICP.get_param(NOTIFY_GROUP_PARAM)
                # Guard: the stored id must be a positive integer; a blank or
                # malformed value resolves to an empty recordset (never raises).
                group = self.env['res.groups'].sudo().browse(
                    int(gid)) if (gid or '').strip().isdigit() \
                    else self.env['res.groups']
                # Odoo 19: res.groups.users -> user_ids (members explicitly in the
                # group), matching the direct membership the notify target used.
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
        except Exception:   # noqa: BLE001 — notification must never break ingest
            _logger.warning(
                "meta_lead_ads: new-lead notification failed for lead id=%s "
                "(ingestion unaffected)", lead.id, exc_info=True)

    @api.model
    def _post_inbox_alert(self, lead, partners, subject, body):
        """Deliver an operational alert to each recipient's Odoo INBOX (the bell
        / Discuss Inbox), independent of their personal email-vs-inbox
        preference AND of outgoing email (SMTP) availability.

        We create the ``mail.message`` and ``inbox`` ``mail.notification`` rows
        DIRECTLY, deliberately bypassing ``message_notify`` / ``_notify_thread``.
        That serves two ends an operational alert needs:
          1. it lands in the in-app inbox regardless of the recipient's
             notification preference or a failing mail server (the reason this
             alert was being lost — recipients preferred email and prod SMTP was
             rejecting auth); and
          2. it does NOT trigger third-party ``_notify_thread`` overrides — e.g.
             a Firebase web-push hook that raises ``UserError`` when its key is
             missing and would otherwise abort the whole notification.

        ``reply_to`` and ``record_name`` are passed explicitly so
        ``mail.message.create`` skips the catchall-alias lookup, which can
        ``KeyError`` on a freshly-created record. The message is a
        ``user_notification`` so it does NOT appear on the lead's chatter.
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
    # Answer capture (append, never replace).
    # ------------------------------------------------------------------ #
    @api.model
    def _apply_answers(self, lead, answers, description_block):
        """Append new meta.lead.answer rows + append the Q&A block to the lead
        description. Never replaces existing answers/description."""
        if answers:
            lead.write({'answer_ids': [(0, 0, a) for a in answers]})
        if description_block:
            existing_desc = lead.description or ""
            new_desc = (existing_desc + "\n\n" + description_block
                        if existing_desc else description_block)
            lead.write({'description': new_desc})

    # ------------------------------------------------------------------ #
    # STEP 6 — UTM get-or-create.
    # ------------------------------------------------------------------ #
    @api.model
    def _apply_utm(self, lead, data, campaign_name=None):
        """Set the inherited utm.mixin source_id/medium_id/campaign_id by
        get-or-create — FB reuses the stock source, IG the seeded source, medium
        is the seeded Paid Social, campaign is get-or-create by name (no dup
        utm.campaign).

        ``campaign_name`` is the resolved attribution name: prefer it over the
        raw payload ``campaign_name`` so a lead whose payload omits the name but
        whose campaign id resolved (e.g. cron backfill) still gets the
        utm.campaign link, instead of silently dropping it."""
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
        if uvals:
            lead.write(uvals)

    @api.model
    def _get_or_create_campaign(self, name):
        """get-or-create utm.campaign by name (=ilike search then create — never
        a blind .create())."""
        Campaign = self.env['utm.campaign']
        if not name or not name.strip():
            return Campaign
        clean = name.strip()
        lock_key = 'meta_lead_ads.utm.campaign:%s' % clean.lower()
        # Serialize workers by normalized campaign name before search/create.
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
