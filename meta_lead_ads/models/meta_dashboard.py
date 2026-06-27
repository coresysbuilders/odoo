# Meta Lead Ads Integration for Odoo — © 2026 CoreSys Builders. All rights reserved.
# Licensed under the Odoo Proprietary License v1.0 (OPL-1).
# Unauthorized copying, redistribution, or resale of this software, in whole or in
# part, via any medium, is strictly prohibited and constitutes a license violation.
# OPL-1: https://www.odoo.com/documentation/19.0/legal/licenses.html#odoo-apps

# Read-only Leads Analytics Dashboard aggregation surface.
#
# Load-bearing rules:
#   - This is the ONLY new server code in Phase 11 and is STRICTLY READ-ONLY:
#     zero create/write/unlink, no Graph call, no sync trigger. The keystone
#     single-create ingest path (ingest_leadgen) is untouched.
#   - In-method admin gate FIRST (D-14 / T-11-EoP): a non-group_meta_admin
#     caller — with OR without group_meta_user — hits AccessError before any
#     read. The menu groups= hides the UI but cannot stop a direct ORM/RPC call.
#   - NEVER read secret-grouped fields (access_token / app_secret / raw_payload)
#     and never sudo()-read them into the payload (D-15 / T-11-ID). The health
#     and sync_recent shapes are ALLOWLISTED so they can never widen to leak a
#     token / expiry / raw payload.
#
# Odoo 18 ORM contract:
#   _read_group(domain, groupby, aggregates) returns a LIST OF TUPLES (not legacy
#   dicts). Code unpacks the tuples; it never indexes a dict key and never calls
#   the legacy read_group(fields=...). The create_date:<g> bucket granularity is
#   DERIVED server-side from a fixed enum {day,week,month,quarter,year} — never
#   the client-supplied period_mode, and NEVER create_date:custom.
import logging

from dateutil.relativedelta import relativedelta

from odoo import _, api, fields, models
from odoo.exceptions import AccessError

_logger = logging.getLogger(__name__)

# Allowlist of client-supplied period modes (V5 input validation, T-11-Tamper).
# Anything else is coerced to the default so a crafted RPC can never inject an
# arbitrary token into the derived create_date:<g> groupby string.
_PERIOD_MODES = ('month', 'quarter', 'year', 'custom')
_DEFAULT_PERIOD_MODE = 'month'

# Min-volume guard for the CONVERSION ranking ONLY.
# Applied as a PYTHON filter after grouping — NOT a SQL having= (unsupported in
# Odoo 18 _read_group). The donut VOLUME list keeps ALL campaigns incl. sub-N.
_MIN_RANK_VOLUME = 10

# Drill-down / donut row caps — bound the payload (grouped reads, no per-row
# loops).
_DRILLDOWN_TOP = 10

# Webhook-evidence freshness window. A trigger='webhook' sync-log row newer than
# this proves the webhook is delivering; older-but-present history => waiting;
# no webhook rows ever => unknown. Evidence-based, never inheriting scheduler ok
# (do not mask a dead webhook).
_WEBHOOK_FRESH_HOURS = 24

# Token "expiring soon" threshold. expires_at is readonly (not secret-grouped),
# but the RAW expiry value is never emitted into the caption (T-11-ID).
_TOKEN_EXPIRY_SOON_DAYS = 7

# Worst-status severity ordering (higher = worse). The roll-up takes the max
# severity per signal across active accounts.
_SEVERITY = {
    'ok': 0,
    'pending': 1,
    'stalled': 2,
    'disabled': 3,
    # token signal codes
    'valid': 0,
    'expiring': 1,
    'invalid': 3,
    # webhook signal codes
    'running': 0,
    'waiting': 1,
    'not_running': 2,
    'unknown': 1,
}


class MetaDashboard(models.Model):
    # Ride the existing meta.account ACL via _inherit — no new ir.model.access
    # row is required (the surface is admin-gated in-method anyway).
    _inherit = 'meta.account'

    # ------------------------------------------------------------------ #
    # Public read-only entry point.
    # ------------------------------------------------------------------ #
    @api.model
    def get_dashboard_metrics(self, date_from=None, date_to=None,
                              period_mode='month'):
        """Return ONE JSON-serializable dict with the full dashboard payload.

        STRICTLY READ-ONLY: no create/write/unlink, no Graph call, no sync
        trigger. Admin-gated as the FIRST statement.

        Window coherence (D-06/D-12): series, kpis, conversion, campaigns,
        drilldown and deltas all use the SAME half-open UTC window. sync_recent
        and health are EXEMPT — sync_recent is the GLOBAL latest-5 operational
        heartbeat (D-16) and health is a worst-status roll-up across accounts
        (D-15).
        """
        # Admin gate FIRST — before any read (T-11-EoP). Generic copy: never
        # echo a backend/Graph error string (T-11-ID-err).
        if not self.env.user.has_group('meta_lead_ads.group_meta_admin'):
            raise AccessError(_("Meta Admin access required."))

        # The has_group gate above is the authorization boundary (T-11-EoP).
        # The aggregation itself runs sudo from here on: a Meta Admin is not
        # necessarily a CRM/Sales user, so the cross-model reads (crm.lead,
        # meta.sync.log, meta.account status) would otherwise AccessError for a
        # legitimate non-system Meta Admin. No secret-grouped field
        # (access_token / app_secret / raw_payload) is ever read into the
        # payload, so elevating the read does NOT widen disclosure (T-11-ID).
        self = self.sudo()

        # Validate / coerce period_mode against the allowlist (T-11-Tamper).
        if period_mode not in _PERIOD_MODES:
            period_mode = _DEFAULT_PERIOD_MODE

        d_from, d_to, bucket = self._dashboard_window(
            date_from, date_to, period_mode)

        Lead = self.env['crm.lead']
        meta = [('meta_leadgen_id', '!=', False)]
        win = meta + [('create_date', '>=', d_from),
                      ('create_date', '<', d_to)]

        # -- leads-over-time series (single _read_group, TUPLE-unpacked) --
        # bucket ∈ {day,week,month,quarter,year} — DERIVED, never 'custom'.
        rows = Lead._read_group(win, [f'create_date:{bucket}'], ['__count'])
        series = [{'bucket': self._bucket_label(b, bucket), 'count': c}
                  for (b, c) in rows]

        # -- totals / conversion (search_count, never a per-bucket loop) --
        total = Lead.search_count(win)
        opp = Lead.search_count(win + [('type', '=', 'opportunity')])
        # 'won' counted via stage_id.is_won kept in the DOMAIN within the cohort
        # (RESEARCH VBB#6 — not the groupby).
        won = Lead.search_count(win + [('stage_id.is_won', '=', True)])

        # -- sync-log KPIs over the same window --
        Log = self.env['meta.sync.log']
        logwin = [('create_date', '>=', d_from), ('create_date', '<', d_to)]
        synced = Log.search_count(
            logwin + [('status', 'in', ('success', 'skipped_idempotent'))])
        failed = Log.search_count(logwin + [('status', '=', 'failed')])

        campaigns, ranked = self._dashboard_campaigns(meta, d_from, d_to)

        return {
            'window': {
                'from': fields.Datetime.to_string(d_from),
                'to': fields.Datetime.to_string(d_to),
                'period_mode': period_mode,
                'bucket_granularity': bucket,
            },
            'kpis': {'new': total, 'synced': synced, 'failed': failed},
            'series': series,
            'conversion': {
                'opportunity_rate': (opp / total) if total else 0.0,
                'won_rate': (won / opp) if opp else 0.0,
            },
            'campaigns': campaigns,
            'conversion_ranking': ranked,
            'drilldown': self._dashboard_drilldown(meta, d_from, d_to),
            # EXEMPT from DASH-06 window coherence — global operational heartbeat.
            'sync_recent': self._dashboard_sync_recent(),
            # EXEMPT from window coherence — worst-status roll-up across accounts.
            'health': self._dashboard_health(),
            'deltas': self._dashboard_deltas(meta, d_from, d_to, period_mode),
        }

    @api.model
    def _bucket_label(self, value, bucket):
        """Human, JSON-serializable x-axis label for a leads-over-time bucket.

        ``value`` is the _read_group period-start (a date/datetime) for
        ``create_date:<bucket>``; format it per granularity so the chart axis
        reads e.g. 'Jun 2026' instead of a raw '2026-06-01 00:00:00' (WR-04).
        Falsy -> ''; a non-date value falls back to str() defensively.
        """
        if not value:
            return ''
        if not hasattr(value, 'strftime'):
            return str(value)
        if bucket == 'year':
            return value.strftime('%Y')
        if bucket == 'quarter':
            return 'Q%d %s' % ((value.month - 1) // 3 + 1, value.strftime('%Y'))
        if bucket == 'week':
            iso = value.isocalendar()
            return 'W%02d %s' % (iso[1], iso[0])
        if bucket == 'month':
            return value.strftime('%b %Y')
        # day (and any unexpected fallback)
        return value.strftime('%d %b %Y')

    # ------------------------------------------------------------------ #
    # period_mode -> DERIVED bucket_granularity window helpers.
    # ------------------------------------------------------------------ #
    @api.model
    def _dashboard_window(self, date_from, date_to, period_mode):
        """Return (d_from, d_to, bucket_granularity) as half-open UTC datetimes.

        Named periods (month/quarter/year) are derived from an anchor (today by
        default, D-09) via relativedelta and carry the matching named bucket.
        'custom' uses the explicit date_from/date_to (treated as the user's
        local day-range, normalized to UTC half-open datetimes) and DERIVES the
        bucket by span (≤31d→day, ≤365d→month, else year). NEVER 'custom'.
        """
        if period_mode == 'custom' and date_from and date_to:
            d_from = self._as_utc_dt(date_from)
            d_to = self._as_utc_dt(date_to)
            # Guard an inverted or zero-width custom range (To <= From): fall back
            # to the default named window instead of emitting a nonsense empty
            # dashboard with a forward-shifted prior period (WR-02). The frontend
            # sends an inclusive To (advanced one day) so a normal single-day
            # selection is a valid 1-day window, not zero-width.
            if d_to <= d_from:
                return self._dashboard_window(None, None, _DEFAULT_PERIOD_MODE)
            delta_days = (d_to - d_from).days
            if delta_days <= 31:
                bucket = 'day'
            elif delta_days <= 365:
                bucket = 'month'
            else:
                bucket = 'year'
            return d_from, d_to, bucket

        # Named period anchored on the start of the current named unit (default
        # anchor = today). An explicit date_from anchors the named period for
        # determinism (used by the metric tests); otherwise today.
        anchor = self._as_utc_dt(date_from) if date_from \
            else fields.Datetime.now()
        # The series bucket is intentionally FINER than the window length so the
        # leads-over-time line chart draws a real multi-point line instead of a
        # single dot (UAT test 3): month->day (~30 pts), quarter->week (~13 pts),
        # year->month (12 pts). period_mode still passes through unchanged.
        if period_mode == 'quarter':
            q_month = ((anchor.month - 1) // 3) * 3 + 1
            d_from = anchor.replace(
                month=q_month, day=1, hour=0, minute=0, second=0,
                microsecond=0)
            d_to = d_from + relativedelta(months=3)
            bucket = 'week'
        elif period_mode == 'year':
            d_from = anchor.replace(
                month=1, day=1, hour=0, minute=0, second=0, microsecond=0)
            d_to = d_from + relativedelta(years=1)
            bucket = 'month'
        else:   # 'month' (and the coerced default)
            d_from = anchor.replace(
                day=1, hour=0, minute=0, second=0, microsecond=0)
            d_to = d_from + relativedelta(months=1)
            bucket = 'day'
        return d_from, d_to, bucket

    @api.model
    def _as_utc_dt(self, value):
        """Coerce a client-supplied date/datetime/string into a naive-UTC
        datetime (Odoo stores create_date as naive UTC). Tolerant of a date,
        a datetime, or an ISO/Odoo string."""
        dt = fields.Datetime.to_datetime(value)
        if dt is None:
            dt = fields.Datetime.now()
        # to_datetime returns a naive datetime for naive input and strips tz for
        # aware input; ensure tz-naive so domain comparisons stay UTC-consistent.
        if dt.tzinfo is not None:
            dt = dt.replace(tzinfo=None)
        return dt

    @api.model
    def _dashboard_prev_window(self, d_from, d_to, period_mode):
        """Return the previous equal-length (prev_from, prev_to) half-open
        window. Named periods shift one unit; custom shifts by the span."""
        if period_mode == 'quarter':
            return d_from - relativedelta(months=3), d_from
        if period_mode == 'year':
            return d_from - relativedelta(years=1), d_from
        if period_mode == 'month':
            return d_from - relativedelta(months=1), d_from
        # custom: equally-long range immediately before d_from.
        return d_from - (d_to - d_from), d_from

    # ------------------------------------------------------------------ #
    # Relative deltas vs the previous equal-length period (D-11).
    # ------------------------------------------------------------------ #
    @api.model
    def _dashboard_deltas(self, meta, d_from, d_to, period_mode):
        """Per-KPI RELATIVE delta (current - prior) / prior; value None when the
        prior-period count is 0 (hide-delta-when-no-prior, D-11)."""
        prev_from, prev_to = self._dashboard_prev_window(
            d_from, d_to, period_mode)
        Lead = self.env['crm.lead']
        Log = self.env['meta.sync.log']

        cur_new = Lead.search_count(
            meta + [('create_date', '>=', d_from), ('create_date', '<', d_to)])
        prior_new = Lead.search_count(
            meta + [('create_date', '>=', prev_from),
                    ('create_date', '<', prev_to)])

        cur_synced = Log.search_count([
            ('create_date', '>=', d_from), ('create_date', '<', d_to),
            ('status', 'in', ('success', 'skipped_idempotent'))])
        prior_synced = Log.search_count([
            ('create_date', '>=', prev_from), ('create_date', '<', prev_to),
            ('status', 'in', ('success', 'skipped_idempotent'))])

        cur_failed = Log.search_count([
            ('create_date', '>=', d_from), ('create_date', '<', d_to),
            ('status', '=', 'failed')])
        prior_failed = Log.search_count([
            ('create_date', '>=', prev_from), ('create_date', '<', prev_to),
            ('status', '=', 'failed')])

        return {
            'new': self._relative_delta(cur_new, prior_new),
            'synced': self._relative_delta(cur_synced, prior_synced),
            'failed': self._relative_delta(cur_failed, prior_failed),
        }

    @api.model
    def _relative_delta(self, current, prior):
        """(current - prior) / prior, or None when prior == 0 (no baseline)."""
        if not prior:
            return None
        return (current - prior) / prior

    # ------------------------------------------------------------------ #
    # Campaign donut (ALL campaigns) + min-volume-guarded conversion ranking
    # (DASH-03).
    # ------------------------------------------------------------------ #
    @api.model
    def _dashboard_campaigns(self, meta, d_from, d_to):
        """Return (donut, ranked).

        donut = the FULL volume list grouped by meta_campaign_name with both a
        NULL (False) and a blank ('') key collapsed to 'Unattributed' — ALL
        campaigns incl. sub-threshold ones. Each entry carries per-campaign
        opportunity_rate + won_rate.

        ranked = the CONVERSION ranking with the N=10 min-volume guard applied
        as a PYTHON filter (no SQL having=).
        """
        donut = self._dashboard_group_conversion(
            meta, d_from, d_to, 'meta_campaign_name', top=None)
        # Min-volume guard for the conversion ranking ONLY (donut keeps all).
        ranked = [c for c in donut if c['count'] >= _MIN_RANK_VOLUME]
        return donut, ranked

    # ------------------------------------------------------------------ #
    # Drill-down to top ads / ad sets by volume (DASH-08).
    # ------------------------------------------------------------------ #
    @api.model
    def _dashboard_drilldown(self, meta, d_from, d_to):
        """Top ads / ad sets by volume with per-row conversion, same window.
        Computed from a small number of grouped reads (no per-row loops)."""
        return {
            'ads': self._dashboard_group_conversion(
                meta, d_from, d_to, 'meta_ad_name', top=_DRILLDOWN_TOP),
            'adsets': self._dashboard_group_conversion(
                meta, d_from, d_to, 'meta_adset_name', top=_DRILLDOWN_TOP),
        }

    @api.model
    def _dashboard_group_conversion(self, meta, d_from, d_to, group_field,
                                    top=None):
        """Group Meta leads in the window by ``group_field`` and derive volume +
        per-bucket opportunity_rate/won_rate using exactly THREE grouped reads
        (total / opportunity / won), tuple-unpacked. False OR '' keys collapse
        to 'Unattributed'. Optionally cap to the ``top`` buckets by volume.
        """
        Lead = self.env['crm.lead']
        win = meta + [('create_date', '>=', d_from), ('create_date', '<', d_to)]

        def _label(key):
            # Collapse BOTH NULL (False) and blank ('') to 'Unattributed'.
            # Trailing whitespace-only names also collapse.
            return key.strip() if (key and key.strip()) else 'Unattributed'

        totals = {}
        order = []
        for (key, count) in Lead._read_group(win, [group_field], ['__count']):
            label = _label(key)
            # Distinct False/'' rows both map to 'Unattributed' — merge them.
            if label not in totals:
                totals[label] = {'count': 0, 'opp': 0, 'won': 0}
                order.append(label)
            totals[label]['count'] += count

        opp_win = win + [('type', '=', 'opportunity')]
        for (key, count) in Lead._read_group(opp_win, [group_field], ['__count']):
            label = _label(key)
            if label in totals:
                totals[label]['opp'] += count

        won_win = win + [('stage_id.is_won', '=', True)]
        for (key, count) in Lead._read_group(won_win, [group_field], ['__count']):
            label = _label(key)
            if label in totals:
                totals[label]['won'] += count

        rows = []
        for label in order:
            agg = totals[label]
            c, o, w = agg['count'], agg['opp'], agg['won']
            rows.append({
                'name': label,
                'count': c,
                'opportunity_rate': (o / c) if c else 0.0,
                'won_rate': (w / o) if o else 0.0,
            })
        rows.sort(key=lambda r: r['count'], reverse=True)
        if top is not None:
            rows = rows[:top]
        return rows

    # ------------------------------------------------------------------ #
    # GLOBAL latest-5 recent sync log (DASH-04 / D-16).
    # EXEMPT from DASH-06 period coherence — this is the operational heartbeat
    # ('Recent' / 'last 5'), INDEPENDENT of the window argument (decision
    # 2026-06-19). NEVER reads raw_payload (secret-grouped).
    # ------------------------------------------------------------------ #
    @api.model
    def _dashboard_sync_recent(self):
        """The 5 newest meta.sync.log rows OVERALL (empty domain), newest-first
        via the model _order='create_date desc'. Allowlisted to EXACTLY
        {status, meta_leadgen_id, create_date} — never raw_payload."""
        rows = self.env['meta.sync.log'].search_read(
            [], ['status', 'meta_leadgen_id', 'create_date'], limit=5)
        # search_read injects 'id'; strip to the allowlisted shape so the
        # payload can never widen past {status, meta_leadgen_id, create_date}.
        return [{
            'status': r['status'],
            'meta_leadgen_id': r['meta_leadgen_id'],
            'create_date': fields.Datetime.to_string(r['create_date'])
            if r['create_date'] else False,
        } for r in rows]

    # ------------------------------------------------------------------ #
    # System Health worst-status roll-up (DASH-05 / D-15).
    # EXEMPT from window coherence — across all active accounts. Each signal is
    # a FIXED-SHAPE {status, label, caption} object exposing NO secret values.
    # ------------------------------------------------------------------ #
    @api.model
    def _dashboard_health(self):
        """Return {'webhook': {...}, 'scheduler': {...}, 'token': {...}}, each a
        fixed {status, label, caption} object. No access_token / app_secret /
        raw_payload / raw expiry value is ever emitted."""
        accounts = self.env['meta.account'].search([('active', '=', True)])
        caption = self._health_caption(accounts)
        return {
            'webhook': self._health_webhook(caption),
            'scheduler': self._health_scheduler(caption),
            'token': self._health_token(accounts, caption),
        }

    @api.model
    def _health_caption(self, accounts):
        """A token-free 'checked {relative time}' caption from the OLDEST
        last_checked across accounts (worst freshness). Never emits the raw
        timestamp or any token/expiry value."""
        checks = [c for c in accounts.mapped('last_checked') if c]
        if not checks:
            return _("not yet checked")
        oldest = min(checks)
        return _("checked %s") % self._humanize_ago(
            fields.Datetime.now() - oldest)

    @api.model
    def _health_scheduler(self, caption):
        """Scheduler signal from the existing global _scheduler_health() helper,
        called ONCE. Maps to UI-SPEC Running/Waiting/Not running/Disabled."""
        health = self.env['meta.account']._scheduler_health()
        labels = {
            'ok': _("Running"), 'pending': _("Waiting"),
            'stalled': _("Not running"), 'disabled': _("Disabled"),
        }
        status = health['status']
        return {
            'status': status,
            'label': labels.get(status, _("Not running")),
            'caption': caption,
        }

    @api.model
    def _health_webhook(self, caption):
        """Evidence-based webhook signal: recent trigger='webhook'
        sync-log activity → running; webhook history but none recent → waiting;
        no webhook rows ever → unknown. NEVER inherits scheduler 'ok' to mask a
        dead webhook."""
        Log = self.env['meta.sync.log']
        fresh_cutoff = fields.Datetime.to_string(
            fields.Datetime.now() - relativedelta(hours=_WEBHOOK_FRESH_HOURS))
        recent = Log.search_count([
            ('trigger', '=', 'webhook'),
            ('create_date', '>=', fresh_cutoff)])
        if recent:
            status, label = 'running', _("Running")
        elif Log.search_count([('trigger', '=', 'webhook')]):
            status, label = 'waiting', _("Waiting")
        else:
            # No webhook rows ever => 'unknown' (severity-1/warn). Label matches
            # the warn semantics — 'Not running' reads as a hard failure and
            # contradicts the amber pill (WR-03); reserve it for a real stall.
            status, label = 'unknown', _("No activity yet")
        return {'status': status, 'label': label, 'caption': caption}

    @api.model
    def _health_token(self, accounts, caption):
        """Token signal from token_valid / access_status / expires_at across
        accounts. Maps to UI-SPEC Valid/Expiring soon/Invalid. The RAW expiry
        value is NEVER emitted into the caption (T-11-ID)."""
        now = fields.Datetime.now()
        soon = now + relativedelta(days=_TOKEN_EXPIRY_SOON_DAYS)
        worst = 'valid'
        for acc in accounts:
            if not acc.token_valid or acc.access_status == 'auth_failed':
                code = 'invalid'
            elif acc.expires_at and acc.expires_at <= soon:
                code = 'expiring'
            else:
                code = 'valid'
            if _SEVERITY.get(code, 0) > _SEVERITY.get(worst, 0):
                worst = code
        # No active accounts at all reads as 'invalid' (nothing healthy proven).
        if not accounts:
            worst = 'invalid'
        labels = {
            'valid': _("Valid"), 'expiring': _("Expiring soon"),
            'invalid': _("Invalid"),
        }
        return {
            'status': worst,
            'label': labels.get(worst, _("Invalid")),
            'caption': caption,
        }
