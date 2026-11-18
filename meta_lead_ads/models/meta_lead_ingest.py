# Meta Lead Ads Integration for Odoo — © 2026 CoreSys Builders. All rights reserved.
# Licensed under the Odoo Proprietary License v1.0 (OPL-1).
# Unauthorized copying, redistribution, or resale of this software, in whole or in
# part, via any medium, is strictly prohibited and constitutes a license violation.
# OPL-1: https://www.odoo.com/documentation/20.0/legal/licenses.html#odoo-apps

# Lead ingestion service. ``ingest_leadgen`` is the only code path that creates
# a crm.lead from Meta data; the webhook, the cron and the manual wizard all
# call it. Idempotency comes from the UNIQUE constraint on meta_leadgen_id,
# not from a Python search, because those callers can race each other.
import collections
import logging
import re
import threading
import time
from contextlib import contextmanager
from datetime import date, datetime, timedelta, timezone
from decimal import Decimal, InvalidOperation

from markupsafe import Markup, escape
from psycopg2 import IntegrityError
from psycopg2.errors import SerializationFailure, UniqueViolation

from odoo import _, api, fields, models, Command
from odoo.exceptions import AccessError, ValidationError
from odoo.fields import Domain
from odoo.tools import email_normalize, is_html_empty
from odoo.tools.mail import generate_tracking_message_id

from .const import (
    DEFAULT_LEAD_NAME, LEAD_NAME_PARAM, VALID_TOKENS,
    NOTIFY_ENABLED_PARAM, NOTIFY_TARGET_PARAM,
    NOTIFY_USER_PARAM, NOTIFY_GROUP_PARAM,
    ATTRIBUTION_AUTH_COOLDOWN_HOURS, ATTRIBUTION_RETRY_BATCH,
    ATTRIBUTION_RETRY_CAP, ATTRIBUTION_RETRY_TIME_BUDGET,
    ATTRIBUTION_TRANSIENT_BACKOFF_HOURS, ATTRIBUTION_TRANSIENT_STREAK,
    is_blocked_target,
)
from .exceptions import (MetaAuthError, MetaIngestRaceError,
                         MetaLeadRemovedError, MetaPermanentError,
                         MetaRateLimitError, MetaTransientError)
from .phone_key import phone_dedup_key

# Standard Meta keys, always mapped, so a form with no mapping rows still works.
# full_name is the contact person, not the lead title.
CANONICAL = {
    'full_name': 'contact_name',
    'email': 'email_from',
    'phone_number': 'phone',
    'company_name': 'partner_name',
}

# The Q&A block in the lead description is a div with this class. The Html
# sanitizer keeps classes but strips data- attributes, so a class is the only
# marker that survives saving.
QA_CLASS = 'o_meta_lead_qa'
# Same heading text as older plain-text blocks, so those can still be found.
QA_HEADING = '── Meta Lead Ad submission ──'
# Goes into the message_id of our inbox alerts so they can be told apart
# from other notifications on the lead.
ALERT_MESSAGE_TAG = 'meta-lead-alert'

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
# Our own cap, not a Postgres one: Odoo rounds float and monetary values with
# float_round, which loses precision and can overflow long before the column
# would, so a number this large is kept as text.
_MAX_NUMBER = Decimal(10) ** 15
# Only full ISO dates are read without a declared format. Compact forms like
# 20261225 or week dates are left alone, since a person rarely means them.
_ISO_DATE_RE = re.compile(
    r'^\d{4}-\d{2}-\d{2}'
    r'(?:[T ]\d{2}:\d{2}(?::\d{2}(?:\.\d{1,6})?)?(?:Z|[+-]\d{2}:?\d{2})?)?$')
# Formats an admin can declare on a date mapping.
_DECLARED_DATE_FORMATS = {
    'dmy': '%d/%m/%Y',
    'mdy': '%m/%d/%Y',
    'ymd': '%Y-%m-%d',
}

# Returned by _enrich_match when a no-company match can't be moved into the
# lead's company; ingest then creates a new lead instead.
_STAMP_REJECTED = object()

# What _find_dedup_match returns; see its docstring.
_DedupMatch = collections.namedtuple(
    '_DedupMatch', 'lead key others total truncated')
# Rows read per dedup step. Enough to list and count the usual duplicates,
# and bounded when many leads share one inbox address.
_DEDUP_CANDIDATE_LIMIT = 50
# Other matching leads named on the sync log and in the lead note.
_MATCH_LIST_CAP = 10

# Postgres name of utm.campaign's UNIQUE(name), the same on Odoo 18 and 19.
_CAMPAIGN_UNIQ = 'utm_campaign_unique_name'
# Postgres name of crm.lead's UNIQUE(meta_leadgen_id), also the same on both.
_LEADGEN_UNIQ = 'crm_lead_meta_leadgen_id_uniq'

# (object type, id field, name field) for each name looked up on Meta.
_ATTRIBUTION_PAIRS = (
    ('campaign', 'meta_campaign_id', 'meta_campaign_name'),
    ('adset', 'meta_adset_id', 'meta_adset_name'),
    ('ad', 'meta_ad_id', 'meta_ad_name'),
    ('form', 'meta_form_id', 'meta_form_name'),
)
# Meta errors a name lookup may raise. Anything else is a bug and still raises.
_NAME_LOOKUP_ERRORS = (MetaAuthError, MetaPermanentError,
                       MetaRateLimitError, MetaTransientError)

# Clock for the retry job's time budget. Kept as a module name so tests can
# move the clock.
_monotonic = time.monotonic


def _attribution_gap(values):
    """Tell whether any Meta id in ``values`` (a dict or a lead) has no name."""
    get = values.get if isinstance(values, dict) else values.__getitem__
    return any(get(id_field) and not get(name_field)
               for _type, id_field, name_field in _ATTRIBUTION_PAIRS)


def _unique_conflict_on(exc, constraint):
    """Tell whether ``exc`` is a UNIQUE violation of that one constraint."""
    return (isinstance(exc, UniqueViolation)
            and getattr(getattr(exc, 'diag', None),
                        'constraint_name', None) == constraint)


def _is_leadgen_conflict(exc):
    """Tell whether another lead already holds this leadgen id."""
    return _unique_conflict_on(exc, _LEADGEN_UNIQ)


def _escape_like(value):
    """Escape ``value`` so =ilike compares it exactly, case aside.

    =ilike treats % and _ as wildcards, and _ is common in email addresses."""
    return (value.replace('\\', '\\\\')
            .replace('%', '\\%').replace('_', '\\_'))


# A lost ingest race fails the lead's INSERT or UPDATE, and odoo.sql_db logs
# that statement at ERROR with every value in it, lead answers included. The
# race is handled below, so while _quiet_lost_races is active in a thread the
# filter drops that one record for these two errors. Other SQL errors are
# still logged.
_LOST_RACE_ERRORS = (UniqueViolation, SerializationFailure)
_race_state = threading.local()


class _LostRaceFilter(logging.Filter):
    def filter(self, record):
        if not getattr(_race_state, 'quiet', False):
            return True
        args = record.args
        return not (isinstance(args, tuple) and args
                    and isinstance(args[-1], _LOST_RACE_ERRORS))


_sql_logger = logging.getLogger('odoo.sql_db')
if not any(type(f).__name__ == '_LostRaceFilter'
           for f in _sql_logger.filters):
    _sql_logger.addFilter(_LostRaceFilter())


@contextmanager
def _quiet_lost_races():
    previous = getattr(_race_state, 'quiet', False)
    _race_state.quiet = True
    try:
        yield
    finally:
        _race_state.quiet = previous


# Python 3.10's fromisoformat rejects "Z", offsets without a colon such as
# Meta's "+0000", and fractions that aren't 3 or 6 digits. strptime's %z
# takes all three offset forms, and the minute-only times _ISO_DATE_RE
# admits are listed too.
_ISO_FALLBACK_FORMATS = tuple(
    '%Y-%m-%d' + sep + clock + zone
    for sep in ('T', ' ')
    for clock in ('%H:%M:%S.%f', '%H:%M:%S', '%H:%M')
    for zone in ('%z', ''))


def _parse_iso_fallback(raw):
    for fmt in _ISO_FALLBACK_FORMATS:
        try:
            return datetime.strptime(raw, fmt)
        except ValueError:
            continue
    return None


def parse_meta_time(raw):
    """Parse a Meta ``created_time`` or ISO 8601 string into a naive UTC datetime.

    Returns ``None`` for empty or unparseable input instead of raising, so a
    bad timestamp never drops a lead."""
    if not raw or not isinstance(raw, str):
        return None
    raw = raw.strip()
    try:
        parsed = datetime.fromisoformat(raw)
    except ValueError:
        parsed = _parse_iso_fallback(raw)
    if parsed is None:
        return None
    if parsed.tzinfo is not None:
        # A year-1 value with a positive offset has no UTC equivalent.
        try:
            parsed = parsed.astimezone(timezone.utc).replace(tzinfo=None)
        except OverflowError:
            return None
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

    @api.model
    def _lead_result_action(self, lead, message=None):
        """Open ``lead`` in its form, or notify when the caller can't read it.

        A caller with no CRM rights, or who hasn't selected the lead's
        company, gets a plain notification without the lead's name.
        """
        if not lead.has_access('read'):
            return {
                'type': 'ir.actions.client',
                'tag': 'display_notification',
                'params': {
                    'message': message or _(
                        "The Meta lead was imported. You don't have access "
                        "to open it."),
                    'type': 'success',
                    'sticky': False,
                },
            }
        return {
            'type': 'ir.actions.act_window',
            'res_model': 'crm.lead',
            'res_id': lead.id,
            'view_mode': 'form',
            'target': 'current',
        }

    # ------------------------------------------------------------------ #
    # Entry point
    # ------------------------------------------------------------------ #
    @api.model
    def ingest_leadgen(self, page, leadgen_id, trigger='webhook', raw=None,
                       lookup_state=None):
        """Create or link the crm.lead for a Meta leadgen id, idempotently.

        ``page`` is a ``meta.page`` record (token and app secret come from it).
        A caller ingesting many leads of one Page may pass the same
        ``lookup_state`` dict to each call, so once Meta refuses or throttles
        name lookups the rest of the run skips them and flags the leads.
        Returns the new, matched or already-existing lead, always exactly one
        record, or raises; it never returns an empty recordset.
        MetaIngestRaceError means another transaction holds the lead and a
        retry will find it. MetaLeadRemovedError means the lead was deleted
        in Odoo; only Restore brings it back.

        Steps: validate the id, return early if it is already known, fetch the
        lead from Graph, resolve attribution, map fields, then either enrich an
        open lead found by email/phone or create a new one, and set UTM.
        An attempt that raises while matching or creating leaves no lead,
        answers or success row behind.
        """
        leadgen_id = self._validate_leadgen_id(leadgen_id)
        if not (isinstance(page, models.BaseModel)
                and page._name == 'meta.page' and len(page) == 1):
            raise MetaPermanentError("Invalid page for ingest")

        # The Page token and App Secret are read through sudo below, so a
        # caller has to be someone who could open this Page anyway. Cron and
        # the webhook drain run as superuser and skip this.
        if not self.env.su:
            if not self.env.user.has_group('meta_lead_ads.group_meta_admin'):
                raise AccessError(_("Meta Admin access required."))
            page.with_env(self.env).check_access('read')

        # A Page can be routed to a company whose admins can't see its
        # account, so the secrets are read through sudo after the check
        # above. page_su never leaves this method.
        page_su = page.sudo()
        company = (page_su.lead_company_id or page_su.account_id.company_id
                   or self.env.company)

        # with_company would keep the caller's other selected companies, and
        # default_ keys from the caller's action would set the team,
        # salesperson or company of the new lead. Odoo's own defaults must
        # come from the destination company only. sudo keeps the uid, so
        # create_uid is still the acting user.
        ctx = {k: v for k, v in self.env.context.items()
               if not k.startswith('default_')}
        Lead = self.env['crm.lead'].sudo().with_context(
            ctx, allowed_company_ids=[company.id])
        Log = self.env['meta.sync.log'].sudo().with_context(ctx)

        # Already ingested: don't fetch again.
        existing = self._find_by_leadgen_id(leadgen_id)
        if existing:
            Log._record(leadgen_id, trigger, 'skipped_idempotent',
                        lead=existing, raw=raw, page=page_su)
            return existing.with_env(self.env)

        # Deleted in Odoo: stay deleted. The caller records the skip, since
        # its savepoint would roll back a row written here.
        if self.env['meta.leadgen.tombstone']._removed([leadgen_id]):
            raise MetaLeadRemovedError(
                "Lead %s was deleted in Odoo; not imported again" % leadgen_id)

        # Meta errors propagate untouched. The webhook drain, the backfill and
        # the manual retry each decide whether a failure is final and write
        # the single failure row themselves, so ingest writes none.
        data = self.env['meta.graph.client'].fetch_lead(page_su, leadgen_id)

        attribution = self._resolve_attribution(page_su, data,
                                                state=lookup_state,
                                                company=company)

        vals, answers, description_block = self._map_fields(
            page_su, data, company=company)

        # One savepoint for the whole attempt. If anything raises after the
        # lead is written (a lost UTM campaign race, for one), the lead, its
        # answers and its log row go too, so the caller's retry starts clean
        # instead of finding a half-done lead.
        with self.env.cr.savepoint():
            # A rejected match is skipped and the search goes on, so a lead
            # that matches further down the order is still found.
            rejected = Lead.browse()
            while True:
                found = self._find_dedup_match(
                    Lead, vals, company, leadgen_id=leadgen_id,
                    exclude=rejected)
                match, match_key = found.lead, found.key
                if not match:
                    break
                # The search skips leads holding another leadgen id, so a
                # match with an id holds this one: we lost a race.
                if match.meta_leadgen_id:
                    Log._record(leadgen_id, trigger, 'skipped_idempotent',
                                lead=match, raw=raw, page=page_su)
                    return match.with_env(self.env)
                lead = self._enrich_match(
                    Lead, Log, match, match_key, leadgen_id, trigger,
                    vals, attribution, answers, description_block, data, raw,
                    page=page_su, company=company, found=found)
                if lead is not _STAMP_REJECTED:
                    return self._one_lead(lead, leadgen_id).with_env(self.env)
                # A blank lead that can't join this company is left alone;
                # it is never enriched without its company.
                rejected |= match

            # Leads with no email or phone are created too; they are never
            # dropped.
            lead = self._create_lead(
                Lead, Log, leadgen_id, trigger, vals, attribution,
                answers, description_block, data, raw, company=company,
                page=page_su)
            return self._one_lead(lead, leadgen_id).with_env(self.env)

    @api.model
    def _one_lead(self, lead, leadgen_id):
        """Return ``lead`` if it is exactly one record, else raise for a retry.

        Callers acknowledge the webhook or advance the backfill on what comes
        back, so an empty result must never get out. This runs inside the
        attempt savepoint, so the raise also undoes the attempt."""
        if len(lead) != 1:
            raise MetaIngestRaceError(
                "No lead for %s after ingest; retry" % leadgen_id)
        return lead

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
        can patch it and force the create path. The lookup ignores companies
        and record rules, like the constraint does. Archived leads count too,
        since the constraint doesn't care about active either."""
        return self.env['crm.lead'].sudo().with_context(
            active_test=False).search(
            [('meta_leadgen_id', '=', leadgen_id)], limit=1)

    @api.model
    def _find_winner(self, leadgen_id):
        """Return the lead that beat us to ``leadgen_id`` after a UNIQUE conflict.

        If the other transaction committed after ours started, our snapshot
        can't see its row, and no search in this transaction ever will. Then
        MetaIngestRaceError is raised so the caller retries in a new one."""
        lead = self.env['crm.lead'].sudo().with_context(
            active_test=False).search(
            [('meta_leadgen_id', '=', leadgen_id)], limit=1)
        if not lead:
            raise MetaIngestRaceError(
                "Lead %s was stored by another transaction; retry"
                % leadgen_id)
        return lead

    # ------------------------------------------------------------------ #
    # Attribution
    # ------------------------------------------------------------------ #
    @api.model
    def _resolve_attribution(self, page, data, state=None, company=None):
        """Build the meta_* attribution values for the lead.

        Names come from the payload when present; only missing ones are looked
        up through ``company``'s name cache, one Graph call per missing name
        at most. A lookup that fails leaves its name empty and marks the lead
        meta_attribution_pending; it never stops the lead from being stored.
        ``state`` is shared between lookups so a throttled Page stops early."""
        if state is None:
            state = {}
        Graph = self.env['meta.graph.client']
        token = page.access_token
        # Name lookups need the appsecret_proof too when the app enforces it.
        app_secret = page.account_id.app_secret

        campaign_name = self._resolve_one(
            Graph, token, 'campaign',
            data.get('campaign_id'), data.get('campaign_name'),
            app_secret=app_secret, state=state,
            company=company)
        adset_name = self._resolve_one(
            Graph, token, 'adset',
            data.get('adset_id'), data.get('adset_name'),
            app_secret=app_secret, state=state,
            company=company)
        ad_name = self._resolve_one(
            Graph, token, 'ad',
            data.get('ad_id'), data.get('ad_name'),
            app_secret=app_secret, state=state,
            company=company)
        # Form name: payload first, then the locally synced form (no Graph
        # call), then a Graph lookup as a last resort.
        form = self._resolve_form(page, data.get('form_id'))
        form_name = self._resolve_one(
            Graph, token, 'form',
            data.get('form_id'), data.get('form_name') or form.name,
            app_secret=app_secret, state=state,
            company=company)

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
        attribution['meta_attribution_pending'] = _attribution_gap(attribution)
        if attribution['meta_attribution_pending']:
            # The column has no default, so start the retry count here.
            attribution['meta_attribution_attempts'] = 0
        return attribution

    @api.model
    def _resolve_one(self, Graph, token, object_type, graph_id, payload_name,
                     app_secret=None, state=None, company=None):
        """Return ``payload_name`` if set, else look the name up by id.

        Returns False when there is no id, when the lookup fails, when
        ``state`` says an earlier lookup already stopped them, or when the
        deadline ``state`` carries has passed."""
        if payload_name:
            return payload_name
        if not graph_id:
            return False
        if state and state.get('stop'):
            return False
        # The backfill passes its run deadline, so a lead started near the
        # end of a run makes no more name lookups. The name stays empty, the
        # lead is flagged attribution-pending and the retry job fills it in.
        deadline = state.get('deadline') if state else None
        if deadline is not None and _monotonic() >= deadline:
            return False
        try:
            name = Graph.resolve_name(token, object_type, graph_id,
                                      payload_name=payload_name,
                                      app_secret=app_secret,
                                      company=company)
        except _NAME_LOOKUP_ERRORS as e:
            # Class name only: Meta's error text can echo the request.
            _logger.info(
                "meta_lead_ads: %s name lookup for id %s failed (%s); "
                "attribution left pending",
                object_type, graph_id, type(e).__name__)
            if state is not None and not isinstance(e, MetaPermanentError):
                # A refused, throttled or hanging name endpoint stays that
                # way for a while. The retry job fills the names in later.
                state['stop'] = True
            return False
        return name or False

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
    def _map_fields(self, page, data, company=None):
        """Map Meta field_data to crm.lead values and collect unmapped answers.

        Meta's standard answers (the CANONICAL keys) fill their fields first,
        then the form's active mapping rows in id order. A row can't remap a
        CANONICAL key. An answer whose field another answer already filled,
        or whose row points at a field a form answer may never fill, is kept
        as an unmapped answer. Only the first entry of a question key is
        used; later duplicates are kept as answers. A blank text answer
        doesn't take its field, so a later row can still fill it. The result
        does not depend on the order of field_data, and no answer is dropped.

        Each answer is converted to its target field type; if conversion fails
        the answer is kept as an unmapped answer rather than written badly.
        With ``company`` set, a converted link to a record of another company
        (salesperson, team, contact) is kept as an unmapped answer too, the
        same way crm.lead's own company check would judge it.

        Multi-value answers are joined with ", " for display. The full
        field_data array is stored in meta.sync.log.raw_payload, which is the
        authoritative copy.

        Returns ``(vals, answers, description_block)``. The block is escaped
        HTML (a Markup div with the QA_CLASS class, one line per answer), or
        an empty Markup when every answer was used.
        """
        form = self._resolve_form(page, data.get('form_id'))
        # Some callers run with active_test=False, which would bring archived
        # rows back, so filter on active here.
        rows = (form.mapping_ids.filtered('active').sorted('id') if form
                else self.env['meta.field.mapping'])
        # Only access-rights admins can read ir.model.fields, and a Meta Admin
        # running Restore, Ingest by ID or Retry may not be one. Just the name.
        targets = [(m.meta_key, m.crm_field_id.sudo().name) for m in rows]
        # A canonical key ignores its mapping row, date format included.
        date_formats = {m.meta_key: m.date_format for m in rows
                        if m.date_format and m.meta_key not in CANONICAL}
        lead_fields = self.env['crm.lead']._fields

        # Entries are used by position. Only the first entry of each key can
        # fill a field, so a repeated key never overwrites the first one.
        entries = []
        first_index = {}
        for entry in data.get('field_data', []):
            name = entry.get('name')
            if not name:
                # No question key to map or store it under. Skip the entry
                # rather than fail the lead; raw_payload still has it.
                continue
            values = entry.get('values') or []
            first_index.setdefault(name, len(entries))
            entries.append((name, values, ", ".join(str(v) for v in values)))

        vals = {}
        claimed = set()
        used = set()

        # Standard answers first, so a custom key can only fill one of these
        # fields when the standard answer is missing or blank.
        for name, target in CANONICAL.items():
            index = first_index.get(name)
            if index is None:
                continue
            _name, values, display = entries[index]
            if not display.strip():
                # Nothing to write and nothing worth keeping.
                used.add(index)
                continue
            ok, value = self._coerce_answer(lead_fields[target], values,
                                            display)
            if ok:
                vals[target] = value
                claimed.add(target)
                used.add(index)

        for name, target in targets:
            index = first_index.get(name)
            if name in CANONICAL or index is None:
                continue
            _name, values, display = entries[index]
            field = lead_fields.get(target)
            # Only relational fields carry a comodel.
            if is_blocked_target(target, getattr(field, 'comodel_name', None)):
                # The save checks keep rows off these fields, but a row
                # written some other way must not let an answer change them.
                _logger.warning(
                    "meta_lead_ads: mapping for question %r targets "
                    "crm.lead.%s, which can't be filled from a form answer; "
                    "kept as a Meta answer", name, target)
                continue
            if target in claimed:
                _logger.info(
                    "meta_lead_ads: answer for question %r was not used for "
                    "crm.lead.%s, which another answer already filled; kept "
                    "as a Meta answer", name, target)
                continue
            if not field:
                continue
            if field.type in ('char', 'text') and not display.strip():
                # Leave the field free for a later row.
                used.add(index)
                continue
            ok, value = self._coerce_answer(
                field, values, display, date_format=date_formats.get(name))
            foreign = False
            if (ok and company
                    and field.type in ('many2one', 'many2many')
                    and field.check_company):
                # _coerce_answer gives an id for a many2one and one
                # Command.set for a many2many. A many2many with any
                # foreign id is kept whole, never written in part.
                ids = ([value] if field.type == 'many2one'
                       else list(value[0][2]))
                Target = self.env[field.comodel_name].sudo().with_context(
                    active_test=False)
                domain = (Target._check_company_domain(company)
                          + [('id', 'in', ids)])
                foreign = Target.search_count(domain) < len(set(ids))
            if foreign:
                # The answer names a salesperson, team or contact from
                # another company. Odoo's company check would reject the
                # lead, so the answer is kept as text instead.
                _logger.info(
                    "meta_lead_ads: answer for question %r names a "
                    "record outside the lead's company for crm.lead.%s; "
                    "kept as a Meta answer", name, target)
            elif ok:
                vals[target] = value
                claimed.add(target)
                used.add(index)
            else:
                # Log the key and target only; the answer itself is PII.
                _logger.info(
                    "meta_lead_ads: answer for question %r could not be "
                    "converted to crm.lead.%s (%s); kept as a Meta answer",
                    name, target, field.type)

        answers = []
        qa_lines = []
        for index, (name, _values, display) in enumerate(entries):
            if index in used:
                continue
            answers.append({'question_key': name, 'label': name,
                            'value': display})
            # Anyone can fill in the ad form, so every line is escaped and
            # shows as text, never as a link or markup.
            qa_lines.append(escape("%s: %s" % (name, display)))
        if qa_lines:
            lines = [escape(QA_HEADING)] + qa_lines
            description_block = (Markup('<div class="%s">') % QA_CLASS
                                 + Markup('<br>').join(lines)
                                 + Markup('</div>'))
        else:
            description_block = Markup()
        return vals, answers, description_block

    @api.model
    def _coerce_answer(self, field, values, display, date_format=None):
        """Convert one Meta answer to a value for crm.lead ``field``.

        Returns ``(True, value)`` on success or ``(False, None)``, in which
        case the caller keeps the answer as a meta.lead.answer row.

        char/text take the joined string. selection matches an option key or
        label, case-insensitive. boolean takes yes/no style words. Numbers may
        be comma-grouped with a currency symbol or code; an integer field
        refuses fractions and out-of-range values instead of truncating, and
        numbers of 1e15 or more are refused for every number type. Dates take
        ISO 8601, or the mapping's declared ``date_format`` when one is set.
        many2one needs exactly one matching record, many2many one match per
        value.
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
            # Decimal first: float() turns a long digit string into inf or a
            # value the write then chokes on, which would lose the lead.
            try:
                number = Decimal(match.group(1).replace(',', ''))
            except InvalidOperation:
                return False, None
            if not number.is_finite() or abs(number) >= _MAX_NUMBER:
                return False, None
            if ftype == 'integer':
                if (number != number.to_integral_value()
                        or not -_PG_INT_MAX - 1 <= number <= _PG_INT_MAX):
                    return False, None
                return True, int(number)
            return True, float(number)
        if ftype in ('date', 'datetime'):
            moment = parse_meta_time(raw) if _ISO_DATE_RE.match(raw) else None
            if moment is None and date_format in _DECLARED_DATE_FORMATS:
                try:
                    moment = datetime.strptime(
                        raw, _DECLARED_DATE_FORMATS[date_format])
                except ValueError:
                    moment = None
            if moment is None:
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
        pattern = _escape_like(value)
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
    def _find_dedup_match(self, Lead, vals, company, leadgen_id=None,
                          exclude=None):
        """Find an open lead (active, not won) for this submission.

        Order: email in ``company``, email with no company, normalized phone
        in ``company``, phone with no company. A lead in another company is
        never fetched, so it can't be merged into or mentioned in a log.

        A lead already holding another leadgen id is skipped: a lead holds
        one at most, and overwriting it would break the first submission's
        idempotency. Leads in ``exclude`` are skipped too.

        Within one step the most recently updated lead comes first, so the
        same data always picks the same lead. Every step is searched, so the
        other open leads that match are known too.

        Returns a _DedupMatch:
        - ``lead``: the chosen lead, or an empty recordset
        - ``key``: 'email' or 'phone' for the step that chose it, else None
        - ``others``: ``(lead, labels)`` for every other matching lead, in
          the order found; labels are 'email', 'phone' or 'phone, old format'
        - ``total``: how many distinct leads matched, the chosen one included
        - ``truncated``: a step hit _DEDUP_CANDIDATE_LIMIT, so ``others`` is
          partial and ``total`` is counted in SQL"""
        open_only = [('active', '=', True), ('stage_id.is_won', '=', False)]
        if leadgen_id:
            open_only += ['|', ('meta_leadgen_id', '=', False),
                          ('meta_leadgen_id', '=', leadgen_id)]
        if exclude:
            open_only.append(('id', 'not in', exclude.ids))
        pools = []
        if company:
            pools.append([('company_id', '=', company.id)])
        pools.append([('company_id', '=', False)])
        keys = []
        wanted = (vals.get('email_from') or '').strip()
        key_text = wanted.lower()
        if wanted:
            # =ilike only narrows the search: it may ignore accents and it
            # never trims the stored value. The exact compare below decides.
            # email_normalized also finds a stored address with padding.
            email_domain = [('email_from', '=ilike', _escape_like(wanted))]
            normalized = email_normalize(wanted)
            if normalized:
                email_domain = ['|'] + email_domain + [
                    ('email_normalized', '=', normalized)]
            keys.append(('email', email_domain))
        country = self.env['res.country']
        mapped_country = vals.get('country_id')
        if mapped_country and isinstance(mapped_country, int):
            country = country.browse(mapped_country)
        elif company:
            country = company.partner_id.country_id
        canonical = self._phone_key(vals.get('phone'), country)
        phone_keys = {canonical, self._normalize_phone(vals.get('phone'))}
        phone_keys.discard(False)
        if phone_keys:
            # The stored column uses the same country-aware key. The old
            # digits-only value is searched too, so leads stored before the
            # upgrade recompute still match.
            keys.append(('phone', [('meta_phone_normalized', 'in',
                                    sorted(phone_keys))]))

        def label(match_key, lead):
            if match_key == 'email':
                return 'email'
            if canonical and lead.meta_phone_normalized == canonical:
                return 'phone'
            return 'phone, old format'

        winner, winner_key = Lead.browse(), None
        # Keyed by lead id, holding [lead, labels] in the order found.
        entries = {}
        truncated = False
        for match_key, key_domain in keys:
            for pool in pools:
                found = Lead.search(key_domain + pool + open_only,
                                    order='write_date desc, id desc',
                                    limit=_DEDUP_CANDIDATE_LIMIT)
                if len(found) >= _DEDUP_CANDIDATE_LIMIT:
                    truncated = True
                if match_key == 'email':
                    found = found.filtered(
                        lambda l: (l.email_from or '').strip().lower()
                        == key_text)
                for lead in found:
                    tag = label(match_key, lead)
                    entry = entries.get(lead.id)
                    if entry is None:
                        entries[lead.id] = [lead, [tag]]
                    elif tag not in entry[1]:
                        entry[1].append(tag)
                if found and not winner:
                    winner, winner_key = found[0], match_key
        others = [(lead, tuple(tags)) for lead, tags in entries.values()
                  if lead != winner]
        if truncated:
            # Leads past the cap were never read, so count them in SQL. This
            # can include accent variants the exact compare would drop, which
            # is why a capped count is only ever shown as a lower bound.
            total = Lead.search_count(Domain.AND([
                open_only, Domain.OR(pools),
                Domain.OR([domain for _key, domain in keys])]))
        else:
            total = len(entries)
        return _DedupMatch(winner, winner_key, others, total, truncated)

    @api.model
    def _fits_company(self, record, company):
        """Return True if ``record`` could be moved into ``company``.

        Every relational field with check_company must only point at records
        allowed in ``company``. This is the test Odoo's own _check_company
        makes, so a company write that passes here won't be refused for a
        linked salesperson, team or contact."""
        for name, field in record._fields.items():
            if not (field.relational and field.check_company) \
                    or field.company_dependent:
                continue
            linked = record.sudo()[name]
            if not linked:
                continue
            Target = self.env[field.comodel_name].sudo().with_context(
                active_test=False)
            domain = (Target._check_company_domain(company)
                      + [('id', 'in', linked.ids)])
            if Target.search_count(domain) < len(linked):
                return False
        return True

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

    @api.model
    def _phone_key(self, raw, country):
        """Phone dedup key for ``raw`` read in ``country`` (a res.country,
        possibly empty). See phone_key.phone_dedup_key."""
        return phone_dedup_key(raw, country.code if country else None,
                               country.phone_code if country else None)

    # ------------------------------------------------------------------ #
    # Match path
    # ------------------------------------------------------------------ #
    @api.model
    def _enrich_match(self, Lead, Log, match, match_key, leadgen_id, trigger,
                      vals, attribution, answers, description_block, data, raw,
                      page=None, company=None, found=None):
        """Update an existing open lead with this submission.

        Fills only blank fields (mapped answers and Meta attribution), so a
        salesperson's edits are never overwritten, appends the answers, and
        stamps meta_leadgen_id so a re-send is caught by the idempotency check.
        The stamp is written in a savepoint and flushed so a concurrent claim
        of the same id surfaces as an IntegrityError here; the lead that won
        is returned, or MetaIngestRaceError raised when it isn't visible yet.

        A match with no company is moved into ``company``. If its linked
        records don't fit that company, or another transaction claims it
        first, nothing is written and _STAMP_REJECTED is returned so the
        caller creates a new lead instead. A set company is never changed."""
        # The pending flag is worked out from the match itself after the
        # write, since the match may already have names this lead lacks.
        attribution = {k: v for k, v in attribution.items()
                       if k not in ('meta_attribution_pending',
                                    'meta_attribution_attempts')}
        # The caller only passes matches with no leadgen id yet.
        enrich = {k: v for k, v in vals.items() if v and not match[k]}
        # Only the fields this submission actually fills are recorded; the
        # lead's own values aren't Meta's.
        filled = list(enrich)
        enrich.update({k: v for k, v in attribution.items()
                       if v and not match[k]})
        # The Page's routing only fills what the match lacks, so an owned
        # lead is never reassigned. Its type is never asked for either.
        wanted = tuple(f for f in ('user_id', 'team_id') if not match[f])
        if wanted and page:
            routing = self._page_routing_vals(
                page, company or match.company_id, fnames=wanted)
            if routing.get('user_id'):
                enrich['user_id'] = routing['user_id']
                # Written together with the user, or crm would replace the
                # team with one of the user's own.
                team_id = match.team_id.id or routing.get('team_id')
                if team_id:
                    enrich['team_id'] = team_id
            if not match.team_id and routing.get('team_id') \
                    and 'team_id' not in enrich:
                enrich['team_id'] = routing['team_id']
        enrich['meta_leadgen_id'] = leadgen_id
        if company and not match.company_id:
            if not self._fits_company(match, company):
                return _STAMP_REJECTED
            enrich['company_id'] = company.id
        inventory = match._meta_union_filled(filled)
        if inventory is not None:
            enrich['meta_filled_fields'] = inventory
        try:
            with _quiet_lost_races(), self.env.cr.savepoint():
                match.write(enrich)
                match.flush_recordset()   # force the UNIQUE check in the savepoint
        except IntegrityError as e:
            if not _is_leadgen_conflict(e):
                raise
            # Another transaction stamped this leadgen_id first.
            winner = self._find_winner(leadgen_id)
            Log._record(leadgen_id, trigger, 'skipped_idempotent',
                        lead=winner, raw=raw, page=page)
            return winner
        except SerializationFailure:
            if 'company_id' not in enrich:
                # Only a new transaction can see the other write.
                raise MetaIngestRaceError(
                    "Lead for %s was changed by another transaction; retry"
                    % leadgen_id) from None
            # Another transaction claimed this blank lead first, so this
            # submission gets its own lead.
            return _STAMP_REJECTED
        gap = _attribution_gap(match)
        if gap != match.meta_attribution_pending:
            pending_vals = {'meta_attribution_pending': gap}
            if gap:
                pending_vals['meta_attribution_attempts'] = 0
            match.write(pending_vals)
        self._apply_answers(match, answers, description_block)
        # Keep any first-touch source/medium/campaign the lead already has
        # (e.g. Website).
        self._apply_utm(match, data, attribution.get('meta_campaign_name'),
                        only_blank=True)
        others = found.others if found else []
        Log._record(leadgen_id, trigger, 'success',
                    lead=match, match_key=match_key, raw=data, page=page,
                    candidates=found.total if found else 1,
                    truncated=found.truncated if found else False,
                    other_ids=self._match_list(
                        found, lambda lead, labels: '%d (%s)' % (
                            lead.id, ', '.join(labels))) or None)
        if others:
            self._note_other_matches(match, found)
        return match

    @api.model
    def _match_list(self, found, render):
        """Join the other matching leads of ``found`` with ``render``, at most
        _MATCH_LIST_CAP of them, saying how many were left out."""
        if not found or not found.others:
            return ''
        shown = found.others[:_MATCH_LIST_CAP]
        text = ', '.join(render(lead, labels) for lead, labels in shown)
        if found.truncated:
            text += ', and more (%d+ matches)' % _DEDUP_CANDIDATE_LIMIT
        elif len(found.others) > len(shown):
            text += ', and %d more' % (len(found.others) - len(shown))
        return text

    @api.model
    def _note_other_matches(self, lead, found):
        """Leave an internal note on ``lead`` naming the other open leads
        this submission also matched.

        Only ids go in the note, never names or contact details. It is
        written in the ingest transaction like the sync-log row, so if that
        rolls back both go. A failing note is logged and never undoes the
        lead."""
        # Flush the lead's own writes outside the catch-all, as for the
        # new-lead alert.
        self.env.cr.flush()
        try:
            with self.env.cr.savepoint():
                ids_text = self._match_list(
                    found, lambda other, _labels: '#%d' % other.id)
                # _message_log escapes a str too; done here so the note
                # stays text whatever is passed in.
                lead._message_log(body=escape(_(
                    "Also matches leads %s. Consider merging.", ids_text)))
        except Exception:   # noqa: BLE001 - a failed note must not break ingest
            _logger.warning(
                "meta_lead_ads: duplicate note failed for lead id=%s "
                "(ingestion unaffected)", lead.id, exc_info=True)

    # ------------------------------------------------------------------ #
    # Page routing
    # ------------------------------------------------------------------ #
    @api.model
    def _page_routing_vals(self, page, company,
                           fnames=('user_id', 'team_id', 'type')):
        """Return the Page's routing for a lead in ``company`` as crm.lead
        values, for the keys in ``fnames`` only.

        Nothing else in ingest reads the Page's routing settings. 'type' is
        always returned when asked for ('lead' without a Page). A default
        salesperson or team that no longer fits ``company`` is left out
        with one warning holding ids only. Keys not asked for are not
        checked at all, so a matched lead that already has an owner logs
        nothing about the Page's salesperson. Routing never fails a lead.
        """
        routing = {}
        page = page.sudo().with_context(active_test=False) if page else None
        if 'type' in fnames:
            routing['type'] = (page.lead_type if page else None) or 'lead'
        if not page:
            return routing
        if 'user_id' in fnames:
            user = page.lead_user_id
            # Same test as the Page's save check, so nobody gets leads they
            # can't open.
            if user and company and page._salesperson_fits(user, company):
                routing['user_id'] = user.id
            elif user:
                _logger.warning(
                    "meta_lead_ads: page id=%s default salesperson id=%s "
                    "doesn't fit company id=%s or has no Sales rights; lead "
                    "left without one", page.id, user.id, company.id)
        if 'team_id' in fnames:
            team = page.lead_team_id
            if team and team.active \
                    and (not team.company_id or team.company_id == company):
                routing['team_id'] = team.id
            elif team:
                _logger.warning(
                    "meta_lead_ads: page id=%s default sales team id=%s "
                    "doesn't fit company id=%s; lead left without one",
                    page.id, team.id, company.id)
        return routing

    # ------------------------------------------------------------------ #
    # Create path
    # ------------------------------------------------------------------ #
    @api.model
    def _create_lead(self, Lead, Log, leadgen_id, trigger, vals, attribution,
                     answers, description_block, data, raw, company=None,
                     page=None):
        """Create the new crm.lead for this leadgen id and return it.

        The lead is created in ``company`` (the Page's lead company). If
        another worker already stored this leadgen id, its lead is returned
        and the skip logged, or MetaIngestRaceError is raised when that lead
        isn't visible yet. Any other integrity error is raised as is."""
        company = company or Lead.env.company
        # Use the stored UTC submission date so the title matches
        # meta_submitted_at.
        submitted = attribution.get('meta_submitted_at')
        day = fields.Date.to_string(submitted.date() if submitted
                                    else date.today())
        # The template isn't validated here: a bad value stored outside
        # Settings must not block a lead, and render_lead_name falls back to
        # the default title anyway.
        template = self.env['ir.config_parameter'].sudo().get_str(
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
        # Written in the same create, so no extra write is needed.
        create_vals['meta_filled_fields'] = Lead._meta_format_filled(vals)
        create_vals.update(attribution)   # meta_* fields + meta_platform + refs
        create_vals.update({'name': subject,
                            'meta_leadgen_id': leadgen_id,
                            # Lets the retroactive rename update this title
                            # until someone edits it by hand.
                            'meta_name_is_generated': True})
        create_vals['company_id'] = company.id
        # The salesperson always comes from the Page or stays empty. Left
        # out, crm would make the acting user (the cron, or the admin who
        # clicked Retry) the owner.
        routing = self._page_routing_vals(page, company)
        create_vals['user_id'] = routing.get('user_id', False)
        if 'team_id' in routing:
            # Given in the same create, so crm keeps this team even when the
            # salesperson isn't one of its members.
            create_vals['team_id'] = routing['team_id']
        create_vals['type'] = routing['type']
        try:
            with _quiet_lost_races(), self.env.cr.savepoint():
                lead = Lead.create(create_vals)
                lead.flush_recordset()   # force the UNIQUE check in the savepoint
        except IntegrityError as e:
            if not _is_leadgen_conflict(e):
                raise
            # The winner may be archived or in another company; it is
            # returned as it is and never changed here.
            lead = self._find_winner(leadgen_id)
            Log._record(leadgen_id, trigger, 'skipped_idempotent',
                        lead=lead, raw=raw, page=page)
            return lead
        self._apply_answers(lead, answers, description_block)
        self._apply_utm(lead, data, attribution.get('meta_campaign_name'))
        Log._record(leadgen_id, trigger, 'success',
                    lead=lead, match_key='none', raw=data, page=page)
        # Only new leads notify; matches and duplicates never get here.
        self._notify_new_lead(lead)
        return lead

    # ------------------------------------------------------------------ #
    # New-lead notification
    # ------------------------------------------------------------------ #
    @api.model
    def _notify_new_lead(self, lead):
        """Notify the configured user, or the active members of the configured group.

        Only recipients who can read the lead are alerted; the rest are
        skipped quietly. Off by default. The work runs in its own savepoint
        and any error is logged and swallowed, so even a database error
        there is rolled back on its own and can't undo the lead that was
        just created.
        """
        # Flush the lead's own pending writes here, outside the catch-all,
        # so an error in them is never hidden by the alert's error handling.
        self.env.cr.flush()
        try:
            with self.env.cr.savepoint():
                ICP = self.env['ir.config_parameter'].sudo()
                if ICP.get_str(NOTIFY_ENABLED_PARAM) != '1':
                    return
                target_type = ICP.get_str(NOTIFY_TARGET_PARAM, 'user')
                if target_type == 'group':
                    gid = ICP.get_str(NOTIFY_GROUP_PARAM)
                    # Blank or non-numeric ids give an empty recordset.
                    group = self.env['res.groups'].sudo().browse(
                        int(gid)) if (gid or '').strip().isdigit() \
                        else self.env['res.groups']
                    # Odoo 19 renamed res.groups.users to user_ids (direct
                    # members).
                    users = group.user_ids.filtered('active') \
                        if group.exists() else self.env['res.users']
                else:
                    uid = ICP.get_str(NOTIFY_USER_PARAM)
                    user = self.env['res.users'].sudo().browse(
                        int(uid)) if (uid or '').strip().isdigit() \
                        else self.env['res.users']
                    users = user.filtered('active') if user.exists() \
                        else self.env['res.users']
                readers = users.filtered(
                    lambda u: self._user_can_read(lead, u))
                for skipped in users - readers:
                    _logger.info(
                        "meta_lead_ads: skipped new-lead alert for user "
                        "id=%s on lead id=%s (no read access)",
                        skipped.id, lead.id)
                partners = readers.partner_id
                if not partners:
                    return
                # The lead name can come from form answers, so the whole
                # line is escaped and shows as text in the inbox.
                body = escape(_("A new lead arrived from Meta Lead Ads: %s") % (
                    lead.name or _("Meta Lead")))
                self._post_inbox_alert(lead, partners, _("New Meta lead"),
                                       body)
        except Exception:   # noqa: BLE001 - a failed notification must not break ingest
            _logger.warning(
                "meta_lead_ads: new-lead notification failed for lead id=%s "
                "(ingestion unaffected)", lead.id, exc_info=True)

    @api.model
    def _user_can_read(self, record, user):
        """Tell whether ``user`` may read ``record``.

        with_user drops sudo, so the ACLs and every record rule apply. The
        allowed companies are reset to the user's own: the caller's context
        may pin another company, and a non-sudo env raises on companies the
        user doesn't belong to. A check that fails counts as no access, so
        one broken rule can't silence everyone else.
        """
        if not user:
            # with_user() of an empty user keeps the current env, which may
            # be sudo and would answer yes.
            return False
        try:
            return record.with_user(user).with_context(
                allowed_company_ids=user.company_ids.ids).has_access('read')
        except Exception:   # noqa: BLE001 - fail closed for this user only
            # No exception text: it could carry record data.
            _logger.warning(
                "meta_lead_ads: access check failed for user id=%s on %s "
                "id=%s; treated as no access",
                user.id, record._name, record.id)
            return False

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

        ``body`` must already be safe HTML (Markup); escape any text from
        the lead before passing it. The message_id carries
        ALERT_MESSAGE_TAG so these alerts can be found again later.
        """
        message = self.env['mail.message'].sudo().create({
            'model': 'crm.lead',
            'res_id': lead.id,
            'message_type': 'user_notification',
            'subtype_id': self.env.ref('mail.mt_note').id,
            'author_id': self.env.ref('base.partner_root').id,
            'subject': subject,
            'body': body,
            # The lead's company, not the one the cron happens to run in.
            'reply_to': (lead.company_id.email_formatted
                         or self.env.company.email_formatted or ''),
            'record_name': lead.name or _("Meta Lead"),
            'partner_ids': [Command.set(partners.ids)],
            # The personal data erase finds the module's own alerts by this
            # id, without touching other notifications on the lead.
            'message_id': generate_tracking_message_id(ALERT_MESSAGE_TAG),
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
        """Add answer rows and append the Q&A block to the description.

        The description is never replaced. What is already there is HTML
        (a salesperson's notes, an earlier block) and is kept as HTML: it is
        wrapped in Markup, never escaped, and the escaped block goes after it.
        """
        if answers:
            lead.write({'answer_ids': [(0, 0, a) for a in answers]})
        if description_block:
            existing = lead.description
            # Adding a plain str to Markup would escape the str side and
            # show the old notes as raw tags.
            new_desc = (Markup(existing) + description_block
                        if not is_html_empty(existing) else description_block)
            lead.write({'description': new_desc})

    # ------------------------------------------------------------------ #
    # UTM
    # ------------------------------------------------------------------ #
    @api.model
    def _apply_utm(self, lead, data, campaign_name=None, only_blank=False):
        """Set UTM source, medium and campaign on the lead.

        Facebook and Instagram use the stock utm sources, except that a
        database upgraded from before Odoo 20 keeps the Instagram source
        this module seeded then. Medium is the seeded Paid Social.
        ``campaign_name`` is the resolved name, preferred over the payload so
        cron-fetched leads that lack the name in the payload still get a
        campaign. With ``only_blank`` (match path) existing UTM values are
        kept."""
        platform = _PLATFORM.get((data.get('platform') or '').lower())
        if platform == 'facebook':
            source = self.env.ref('utm.utm_source_facebook',
                                  raise_if_not_found=False)
        elif platform == 'instagram':
            source = (self.env.ref('meta_lead_ads.utm_source_meta_instagram',
                                   raise_if_not_found=False)
                      or self.env.ref('utm.utm_source_instagram',
                                      raise_if_not_found=False))
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
    def _find_campaign(self, name):
        """Return the existing utm.campaign for ``name``, archived ones included.

        The exact name wins; otherwise a campaign that differs only in case
        is reused. Returns an empty recordset when there is neither."""
        # The escaped =ilike only narrows the candidates. The real comparison
        # is done here because =ilike may also ignore accents, and a name is
        # reused only when both agree.
        candidates = self.env['utm.campaign'].with_context(
            active_test=False).search(
                [('name', '=ilike', _escape_like(name))], order='id')
        for camp in candidates:
            if camp.name == name:
                return camp
        folded = name.casefold()
        for camp in candidates:
            if (camp.name or '').casefold() == folded:
                return camp
        return self.env['utm.campaign']

    @api.model
    def _get_or_create_campaign(self, name):
        """Return the utm.campaign for this name, creating it if needed.

        The exact name is preferred, then a case-only variant; archived
        campaigns are reused as they are. Never a wildcard match. If the
        name is taken by a campaign this transaction can't see yet,
        MetaIngestRaceError asks the caller to retry."""
        Campaign = self.env['utm.campaign']
        if not name or not name.strip():
            return Campaign
        clean = name.strip()
        lock_key = 'meta_lead_ads.utm.campaign:%s' % clean.lower()
        # Lock so two workers don't both create the same campaign.
        self.env.cr.execute(
            "SELECT pg_advisory_xact_lock(hashtext(%s)::bigint)", (lock_key,))
        rec = self._find_campaign(clean)
        if rec:
            return rec
        try:
            with _quiet_lost_races(), self.env.cr.savepoint():
                rec = Campaign.create({'name': clean})
                rec.flush_recordset()
                return rec
        except IntegrityError as e:
            if not _unique_conflict_on(e, _CAMPAIGN_UNIQ):
                raise
            rec = self._find_campaign(clean)
            if rec:
                return rec
            raise MetaIngestRaceError(
                "UTM campaign was stored by another transaction; retry")

    # ------------------------------------------------------------------ #
    # Attribution retry
    # ------------------------------------------------------------------ #
    @api.model
    def _attribution_retry_page(self, lead):
        """Return the meta.page the lead came from, archived Pages included.

        The Page only comes from the link ingest wrote, and it must still
        route leads to the lead's own company (a lead without a company is
        refused when the Page has one). Otherwise the lead counts as having
        no Page."""
        lead = lead.sudo()
        page = lead.meta_page_id_ref
        # The Char Page id is plain lead data with no company behind it, so
        # it never picks a token.
        if not page:
            return self.env['meta.page'].browse()
        if page.lead_company_id and page.lead_company_id != lead.company_id:
            return self.env['meta.page'].browse()
        return page

    @api.model
    def _retry_attribution_one(self, lead, page, deadline, info=None):
        """Look up the Meta names the lead is still missing and fill them in.

        Returns why it stopped: 'done' when no name is missing any more,
        'pending' when some still are, 'rate_limited' or 'auth_failed' when
        Meta throttled or refused the token, 'out_of_time' when ``deadline``
        (a _monotonic() value) passed before a Graph call, and 'no_page'
        when the lead's Page is gone. 'transient' means Meta only timed out
        or failed on its side; that round doesn't count as a try and the
        lead waits ATTRIBUTION_TRANSIENT_BACKOFF_HOURS. Names found before a
        stop are written anyway. ``info``, when given, receives the throttle
        exception under 'rate_limit' so the caller can read Meta's wait
        estimate.

        Only the lead's own Meta names, blank UTM fields and a generated
        title are written. Company, salesperson and set UTM values are left
        as they are."""
        if not page:
            return 'no_page'
        # The cron user may not be allowed to read the token or the App
        # Secret, so they are read through sudo. page_su stays in here.
        page_su = page.sudo()
        token = page_su.access_token
        app_secret = page_su.account_id.app_secret
        Graph = self.env['meta.graph.client']
        # Names are cached per company; use the lead's, as ingest did.
        company = lead.company_id or page_su.lead_company_id

        names = {}
        status = None
        permanent = transient = False
        # A form synced since the lead arrived gives its name without a
        # Graph call, the same fallback ingest uses.
        if lead.meta_form_id and not lead.meta_form_name:
            form_name = self._resolve_form(page_su, lead.meta_form_id).name
            if form_name:
                names['meta_form_name'] = form_name
        for object_type, id_field, name_field in _ATTRIBUTION_PAIRS:
            graph_id = lead[id_field]
            if not graph_id or lead[name_field] or name_field in names:
                continue
            if _monotonic() >= deadline:
                status = 'out_of_time'
                break
            try:
                name = Graph.resolve_name(token, object_type, graph_id,
                                          payload_name=False,
                                          app_secret=app_secret,
                                          company=company)
            except MetaRateLimitError as e:
                status = 'rate_limited'
                if isinstance(info, dict):
                    info['rate_limit'] = e
                break
            except MetaAuthError:
                status = 'auth_failed'
                break
            except MetaPermanentError as e:
                permanent = True
                # Class name only: Meta's error text can echo the request.
                _logger.info(
                    "meta_lead_ads: %s name retry for lead id=%s failed (%s)",
                    object_type, lead.id, type(e).__name__)
                continue
            except MetaTransientError as e:
                transient = True
                _logger.info(
                    "meta_lead_ads: %s name retry for lead id=%s failed (%s)",
                    object_type, lead.id, type(e).__name__)
                continue
            if name:
                names[name_field] = name

        if names:
            lead.write(names)
            if names.get('meta_campaign_name') and not lead.campaign_id:
                self._apply_utm(lead, {}, names['meta_campaign_name'],
                                only_blank=True)
            if lead.meta_name_is_generated:
                template = self.env['ir.config_parameter'].sudo().get_str(
                    LEAD_NAME_PARAM, DEFAULT_LEAD_NAME)
                title = self.render_lead_name(template,
                                              lead._meta_token_map())
                if title != lead.name:
                    # Setting the flag again keeps the title generated.
                    lead.write({'name': title,
                                'meta_name_is_generated': True})
        gap = _attribution_gap(lead)
        if gap != lead.meta_attribution_pending:
            lead.write({'meta_attribution_pending': gap})
        if status:
            return status
        if gap and transient and not permanent:
            # Meta was down, the ids may be fine. Not a real try.
            return 'transient'
        return 'pending' if gap else 'done'

    @api.model
    def _defer_attribution_retry(self, lead, until):
        """Hold back one lead's name retry until ``until`` without counting a try."""
        try:
            with _quiet_lost_races(), self.env.cr.savepoint():
                lead.write({'meta_attribution_retry_after': until})
                lead.flush_recordset()
        except Exception as e:   # noqa: BLE001 - bookkeeping only
            _logger.warning(
                "meta_lead_ads: could not hold back the attribution retry "
                "for lead id=%s (%s)", lead.id, type(e).__name__)

    @api.model
    def _count_attribution_attempt(self, lead):
        """Add one failed try to the lead's retry counter."""
        # A failed counter write must not undo the other leads of the run,
        # so it gets its own savepoint and its error is only logged.
        try:
            with _quiet_lost_races(), self.env.cr.savepoint():
                lead.write({'meta_attribution_attempts':
                            (lead.meta_attribution_attempts or 0) + 1})
                lead.flush_recordset()
        except Exception as e:   # noqa: BLE001 - bookkeeping only
            _logger.warning(
                "meta_lead_ads: could not record an attribution retry for "
                "lead id=%s (%s)", lead.id, type(e).__name__)

    @api.model
    def _pause_attribution_page(self, page, until, reason='token refused'):
        """Hold back name retries for every pending lead of ``page`` until ``until``.

        ``reason`` only goes into the log line.
        """
        try:
            with _quiet_lost_races(), self.env.cr.savepoint():
                # A shorter pause must not cut a longer wait already in place.
                leads = self.env['crm.lead'].sudo().with_context(
                    active_test=False).search([
                        ('meta_attribution_pending', '=', True),
                        ('meta_page_id_ref', '=', page.id),
                        '|', ('meta_attribution_retry_after', '=', False),
                        ('meta_attribution_retry_after', '<', until)])
                leads.write({'meta_attribution_retry_after': until})
                leads.flush_recordset()
        except Exception as e:   # noqa: BLE001 - bookkeeping only
            _logger.warning(
                "meta_lead_ads: could not pause attribution retries for "
                "page id=%s (%s)", page.id, type(e).__name__)
            return
        _logger.info(
            "meta_lead_ads: attribution retries for page id=%s paused until "
            "%s (%s)", page.id, until, reason)

    @api.model
    def _cron_retry_attribution(self, limit=None):
        """Cron: fill in the Meta names that could not be read at ingest.

        Least-tried leads go first, so a lead that keeps failing doesn't hold
        back fresh ones. A refused token pauses only its own Page. A throttle
        holds back that Page's leads for as long as Meta asks and ends the
        run, since Meta's limit covers the whole app. Timeouts and
        Meta-side errors are not counted as tries; the lead waits an hour
        instead, and several leads in a row failing that way end the run.
        The run stops when its time budget is used up, and the rest waits
        for the next run. Leads are never created here."""
        now = fields.Datetime.now()
        deadline = _monotonic() + ATTRIBUTION_RETRY_TIME_BUDGET
        Lead = self.env['crm.lead'].sudo().with_context(active_test=False)
        # The attempt column has no default, so an empty value counts as
        # never tried.
        leads = Lead.search([
            ('meta_attribution_pending', '=', True),
            '|', ('meta_attribution_attempts', '=', False),
            ('meta_attribution_attempts', '<', ATTRIBUTION_RETRY_CAP),
            '|', ('meta_attribution_retry_after', '=', False),
            ('meta_attribution_retry_after', '<=', now),
        ], order='meta_attribution_attempts asc nulls first, id',
            limit=limit or ATTRIBUTION_RETRY_BATCH)
        Ingest = self.sudo()
        paused_pages = set()
        transient_streak = 0
        for lead in leads:
            if _monotonic() >= deadline:
                _logger.info(
                    "meta_lead_ads: attribution retry time budget used; "
                    "the rest waits for the next run")
                break
            page = Ingest._attribution_retry_page(lead)
            if page and page.id in paused_pages:
                continue
            info = {}
            try:
                with _quiet_lost_races(), self.env.cr.savepoint():
                    status = Ingest._retry_attribution_one(
                        lead, page, deadline, info=info)
            except (SerializationFailure, MetaIngestRaceError):
                # Another transaction changed the same rows. Try again next
                # run without counting it against the lead.
                continue
            except Exception as e:   # noqa: BLE001 - one lead must not stop the run
                # Class name only: the text can carry Meta's reply or data.
                _logger.warning(
                    "meta_lead_ads: attribution retry failed for lead id=%s "
                    "(%s)", lead.id, type(e).__name__)
                Ingest._count_attribution_attempt(lead)
                continue
            if status == 'rate_limited':
                # Meta says how long to wait, so the Page's leads are held
                # back until then, counted from the throttle rather than the
                # run start. The run still ends: name read limits cover the
                # whole app or ad account. They are separate from lead read
                # limits, so the Page's lead read pause is left alone.
                until = self.env['meta.webhook.event']._rate_limit_until(
                    info.get('rate_limit'), fields.Datetime.now())
                if page:
                    paused_pages.add(page.id)
                    Ingest._pause_attribution_page(page, until,
                                                   reason='rate limit')
                break
            if status == 'out_of_time':
                break
            if status == 'transient':
                # Not counted, but moved out of the queue for a while, so a
                # lead that keeps timing out can't stay first in line.
                Ingest._defer_attribution_retry(
                    lead,
                    now + timedelta(hours=ATTRIBUTION_TRANSIENT_BACKOFF_HOURS))
                transient_streak += 1
                if transient_streak >= ATTRIBUTION_TRANSIENT_STREAK:
                    _logger.info(
                        "meta_lead_ads: Meta keeps timing out on name "
                        "lookups; the rest waits for the next run")
                    break
                continue
            transient_streak = 0
            if status in ('pending', 'no_page', 'auth_failed'):
                Ingest._count_attribution_attempt(lead)
            if status == 'auth_failed':
                paused_pages.add(page.id)
                Ingest._pause_attribution_page(
                    page,
                    now + timedelta(hours=ATTRIBUTION_AUTH_COOLDOWN_HOURS),
                    reason='token refused')
