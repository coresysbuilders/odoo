# Meta Lead Ads Integration for Odoo — © 2026 CoreSys Builders. All rights reserved.
# Licensed under the Odoo Proprietary License v1.0 (OPL-1).
# Unauthorized copying, redistribution, or resale of this software, in whole or in
# part, via any medium, is strictly prohibited and constitutes a license violation.
# OPL-1: https://www.odoo.com/documentation/18.0/legal/licenses.html#odoo-apps

# Leads analytics dashboard: read-only aggregation for the admin dashboard.
#
# Nothing here writes, calls Graph or triggers a sync. Callers must be Meta
# Admins; the check runs in the method because a menu groups= does not stop a
# direct RPC call. Secret fields (access_token, app_secret, raw_payload) are
# never read into the payload, and the health / recent-sync dicts are built
# from a fixed set of keys so they can't grow to include one.
#
# _read_group() returns a list of tuples, not the old read_group() dicts.
# The create_date:<g> granularity is picked server-side from
# day/week/month/quarter/year; the client's period_mode never reaches it.
import logging

from dateutil.relativedelta import relativedelta

from odoo import _, api, fields, models
from odoo.exceptions import AccessError

_logger = logging.getLogger(__name__)

# Accepted period modes. Anything else falls back to the default, so a crafted
# RPC can't inject text into the create_date:<g> groupby string.
_PERIOD_MODES = ('month', 'quarter', 'year', 'custom')
_DEFAULT_PERIOD_MODE = 'month'

# Campaigns below this many leads are left out of the conversion ranking (the
# rates are noise). Filtered in Python since _read_group has no having=. The
# volume donut still shows every campaign.
_MIN_RANK_VOLUME = 10

# Rows returned per drill-down list.
_DRILLDOWN_TOP = 10

# A webhook-triggered sync-log row newer than this means the webhook is
# delivering. Older rows only => waiting; none at all => unknown.
_WEBHOOK_FRESH_HOURS = 24

# Days before expiry at which a token shows as "expiring soon". The expiry
# date itself is never put in the caption.
_TOKEN_EXPIRY_SOON_DAYS = 7

# Higher is worse. Each health signal reports the worst value across active
# accounts.
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
    # Extends meta.account so it reuses that model's ACL; access is checked in
    # the method anyway.
    _inherit = 'meta.account'

    # ------------------------------------------------------------------ #
    # Public entry point.
    # ------------------------------------------------------------------ #
    @api.model
    def get_dashboard_metrics(self, date_from=None, date_to=None,
                              period_mode='month'):
        """Return the full dashboard payload as one JSON-serializable dict.

        Read-only. Series, KPIs, conversion, campaigns, drill-down and deltas
        share the same half-open UTC window. sync_recent (latest 5 rows) and
        health (worst status across accounts) ignore the window.
        """
        # Check access before reading anything. Keep the message generic.
        if not self.env.user.has_group('meta_lead_ads.group_meta_admin'):
            raise AccessError(_("Meta Admin access required."))

        # Past the group check, read as sudo: a Meta Admin isn't necessarily a
        # Sales user and would otherwise hit AccessError on crm.lead. No secret
        # field is read below, so this doesn't expose anything extra.
        self = self.sudo()

        if period_mode not in _PERIOD_MODES:
            period_mode = _DEFAULT_PERIOD_MODE

        d_from, d_to, bucket = self._dashboard_window(
            date_from, date_to, period_mode)

        Lead = self.env['crm.lead']
        meta = [('meta_leadgen_id', '!=', False)]
        win = meta + [('create_date', '>=', d_from),
                      ('create_date', '<', d_to)]

        # Leads over time. bucket is one of day/week/month/quarter/year.
        rows = Lead._read_group(win, [f'create_date:{bucket}'], ['__count'])
        series = [{'bucket': self._bucket_label(b, bucket), 'count': c}
                  for (b, c) in rows]

        total = Lead.search_count(win)
        opp = Lead.search_count(win + [('type', '=', 'opportunity')])
        # Won = leads created in the window that now sit in a won stage.
        won = Lead.search_count(win + [('stage_id.is_won', '=', True)])

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
            # These two ignore the selected window.
            'sync_recent': self._dashboard_sync_recent(),
            'health': self._dashboard_health(),
            'deltas': self._dashboard_deltas(meta, d_from, d_to, period_mode),
        }

    @api.model
    def _bucket_label(self, value, bucket):
        """Format a bucket start date as a chart label, e.g. 'Jun 2026'.

        Returns '' for a falsy value and str(value) for anything that isn't a
        date.
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
        return value.strftime('%d %b %Y')

    # ------------------------------------------------------------------ #
    # Window and bucket helpers.
    # ------------------------------------------------------------------ #
    @api.model
    def _dashboard_window(self, date_from, date_to, period_mode):
        """Return (d_from, d_to, bucket) as half-open naive-UTC datetimes.

        month/quarter/year cover the current unit around the anchor (today
        unless date_from is given). 'custom' uses date_from/date_to and picks
        the bucket from the span: up to 31 days -> day, up to 365 -> month,
        otherwise year.
        """
        if period_mode == 'custom' and date_from and date_to:
            d_from = self._as_utc_dt(date_from)
            d_to = self._as_utc_dt(date_to)
            # An empty or inverted range falls back to the default period. The
            # client already pushes To forward one day, so picking a single day
            # still gives a 1-day window.
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

        # date_from, when given, pins the anchor (the tests rely on this).
        anchor = self._as_utc_dt(date_from) if date_from \
            else fields.Datetime.now()
        # Bucket one step finer than the period so the line chart has points to
        # draw: month by day, quarter by week, year by month.
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
        else:   # month
            d_from = anchor.replace(
                day=1, hour=0, minute=0, second=0, microsecond=0)
            d_to = d_from + relativedelta(months=1)
            bucket = 'day'
        return d_from, d_to, bucket

    @api.model
    def _as_utc_dt(self, value):
        """Turn a date, datetime or date string into a naive UTC datetime,
        which is how Odoo stores create_date."""
        dt = fields.Datetime.to_datetime(value)
        if dt is None:
            dt = fields.Datetime.now()
        if dt.tzinfo is not None:
            dt = dt.replace(tzinfo=None)
        return dt

    @api.model
    def _dashboard_prev_window(self, d_from, d_to, period_mode):
        """Return (prev_from, prev_to) for the period just before this one."""
        if period_mode == 'quarter':
            return d_from - relativedelta(months=3), d_from
        if period_mode == 'year':
            return d_from - relativedelta(years=1), d_from
        if period_mode == 'month':
            return d_from - relativedelta(months=1), d_from
        # custom: same length, ending where this window starts.
        return d_from - (d_to - d_from), d_from

    # ------------------------------------------------------------------ #
    # Change vs the previous period.
    # ------------------------------------------------------------------ #
    @api.model
    def _dashboard_deltas(self, meta, d_from, d_to, period_mode):
        """Relative change per KPI vs the previous period. None when the
        previous count is 0, so the UI hides the delta."""
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
        """(current - prior) / prior, or None when prior is 0."""
        if not prior:
            return None
        return (current - prior) / prior

    # ------------------------------------------------------------------ #
    # Campaign donut and conversion ranking.
    # ------------------------------------------------------------------ #
    @api.model
    def _dashboard_campaigns(self, meta, d_from, d_to):
        """Return (donut, ranked).

        donut lists every campaign by volume, with missing names grouped as
        'Unattributed'. ranked is the same list minus campaigns under
        _MIN_RANK_VOLUME leads.
        """
        donut = self._dashboard_group_conversion(
            meta, d_from, d_to, 'meta_campaign_name', top=None)
        ranked = [c for c in donut if c['count'] >= _MIN_RANK_VOLUME]
        return donut, ranked

    # ------------------------------------------------------------------ #
    # Drill-down: top ads and ad sets.
    # ------------------------------------------------------------------ #
    @api.model
    def _dashboard_drilldown(self, meta, d_from, d_to):
        """Top ads and ad sets by volume, with conversion rates."""
        return {
            'ads': self._dashboard_group_conversion(
                meta, d_from, d_to, 'meta_ad_name', top=_DRILLDOWN_TOP),
            'adsets': self._dashboard_group_conversion(
                meta, d_from, d_to, 'meta_adset_name', top=_DRILLDOWN_TOP),
        }

    @api.model
    def _dashboard_group_conversion(self, meta, d_from, d_to, group_field,
                                    top=None):
        """Group window leads by ``group_field`` with count, opportunity_rate
        and won_rate, sorted by count and cut to ``top`` rows if given.

        Uses three grouped reads (all, opportunities, won). Empty or blank
        names are merged under 'Unattributed'.
        """
        Lead = self.env['crm.lead']
        win = meta + [('create_date', '>=', d_from), ('create_date', '<', d_to)]

        def _label(key):
            return key.strip() if (key and key.strip()) else 'Unattributed'

        totals = {}
        order = []
        for (key, count) in Lead._read_group(win, [group_field], ['__count']):
            label = _label(key)
            # False and '' come back as separate groups; merge them.
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
    # Recent sync activity (not limited to the selected window).
    # ------------------------------------------------------------------ #
    @api.model
    def _dashboard_sync_recent(self):
        """The 5 newest sync-log rows, with status, lead id and date only.
        raw_payload is never read."""
        rows = self.env['meta.sync.log'].search_read(
            [], ['status', 'meta_leadgen_id', 'create_date'], limit=5)
        # Rebuild each row so only these three keys go out (search_read also
        # adds 'id').
        return [{
            'status': r['status'],
            'meta_leadgen_id': r['meta_leadgen_id'],
            'create_date': fields.Datetime.to_string(r['create_date'])
            if r['create_date'] else False,
        } for r in rows]

    # ------------------------------------------------------------------ #
    # System health, worst status across active accounts.
    # ------------------------------------------------------------------ #
    @api.model
    def _dashboard_health(self):
        """Return webhook, scheduler and token signals, each a
        {status, label, caption} dict. No token or expiry value is included."""
        accounts = self.env['meta.account'].search([('active', '=', True)])
        caption = self._health_caption(accounts)
        return {
            'webhook': self._health_webhook(caption),
            'scheduler': self._health_scheduler(caption),
            'token': self._health_token(accounts, caption),
        }

    @api.model
    def _health_caption(self, accounts):
        """Caption like 'checked 2 hours ago', based on the account checked
        longest ago."""
        checks = [c for c in accounts.mapped('last_checked') if c]
        if not checks:
            return _("not yet checked")
        oldest = min(checks)
        return _("checked %s") % self._humanize_ago(
            fields.Datetime.now() - oldest)

    @api.model
    def _health_scheduler(self, caption):
        """Scheduler signal, mapped from meta.account._scheduler_health()."""
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
        """Webhook signal, judged from webhook-triggered sync-log rows only.

        Recent rows -> running, only older rows -> waiting, none -> unknown.
        It doesn't borrow the scheduler status, which could hide a dead
        webhook.
        """
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
            # A warning, not a failure: "Not running" would clash with the
            # amber pill, so keep that label for a real stall.
            status, label = 'unknown', _("No activity yet")
        return {'status': status, 'label': label, 'caption': caption}

    @api.model
    def _health_token(self, accounts, caption):
        """Token signal (valid / expiring / invalid) from token_valid,
        access_status and expires_at. The expiry date is not returned."""
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
        # No active account means no working token.
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
