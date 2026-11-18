# Meta Lead Ads Integration for Odoo — © 2026 CoreSys Builders. All rights reserved.
# Licensed under the Odoo Proprietary License v1.0 (OPL-1).
# Unauthorized copying, redistribution, or resale of this software, in whole or in
# part, via any medium, is strictly prohibited and constitutes a license violation.
# OPL-1: https://www.odoo.com/documentation/20.0/legal/licenses.html#odoo-apps

# Leads analytics dashboard: read-only aggregation for the admin dashboard.
#
# Nothing here writes, calls Graph or triggers a sync. Callers must be Meta
# Admins; the check runs in the method because a menu groups= does not stop a
# direct RPC call. Secret fields (access_token, app_secret, raw_payload) are
# never read into the payload, and the health / recent-sync dicts are built
# from a fixed set of keys so they can't grow to include one.
#
# _read_group() returns a list of tuples, not the old read_group() dicts.
# The <date field>:<g> granularity is picked server-side from
# day/week/month/quarter/year; the client's period_mode never reaches it.
import logging

from dateutil.relativedelta import relativedelta

from odoo import _, api, fields, models
from odoo.exceptions import AccessError

from . import const

_logger = logging.getLogger(__name__)

# Accepted period modes. Anything else falls back to the default, so a crafted
# RPC can't inject text into the <date field>:<g> groupby string.
_PERIOD_MODES = ('month', 'quarter', 'year', 'custom')
_DEFAULT_PERIOD_MODE = 'month'

# Campaigns below this many leads are left out of the conversion ranking (the
# rates are noise). Filtered in Python since _read_group has no having=. The
# volume donut still shows every campaign.
_MIN_RANK_VOLUME = 10

# Rows returned per drill-down list.
_DRILLDOWN_TOP = 10

# Row labels. "Unattributed" (no Meta ID) and "Unnamed" (an ID whose leads
# never carried a name) stay plain strings, like the names Meta sends.
_UNATTRIBUTED = 'Unattributed'
_UNNAMED = 'Unnamed'
# Added to a label when two IDs share a name: space, middle dot, space,
# ellipsis, then the end of the ID. 4 characters first, 6 if those still
# match, then the whole ID.
_SUFFIX_SEP = ' \u00b7 \u2026'
_SUFFIX_WIDTHS = (4, 6)

# Days before expiry at which a token shows as "expiring soon". The expiry
# date itself is never put in the caption.
_TOKEN_EXPIRY_SOON_DAYS = 7

# Sync-log statuses the Synced KPI counts: a lead created, or one already
# there. A duplicate skip and a removed-lead skip are not syncs.
_SYNCED_STATUSES = ('success', 'skipped_idempotent')

# Odoo stamps date_conversion only when a lead is converted, never when one
# is created as an opportunity.
_CONVERTED = [('date_conversion', '!=', False)]
# Leads that could be converted: still a lead, or converted from one.
_STARTED_AS_LEAD = ['|', ('type', '=', 'lead'),
                    ('date_conversion', '!=', False)]
# Opportunities a Page created directly. They can't be converted.
_CREATED_AS_OPP = [('type', '=', 'opportunity'),
                   ('date_conversion', '=', False)]
# Every opportunity can be won, converted or not.
_WON = ['|', ('date_conversion', '!=', False), ('type', '=', 'opportunity'),
        ('stage_id.is_won', '=', True)]

# Webhook leads held because their Page is archived. The webhook pill leaves
# them out of its failures and its caption says how many there are.
_HELD_DOMAIN = [('status', '=', 'failed'), ('trigger', '=', 'webhook'),
                ('meta_leadgen_id', '!=', False),
                ('page_id.active', '=', False)]

# Higher is worse. The token pill reports the worst token across the active
# accounts in scope. The drain and backfill codes are ranked in meta_account,
# next to the checks that produce them.
_TOKEN_SEVERITY = {'valid': 0, 'expiring': 1, 'invalid': 3}


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

        Lead counts date a lead by its Meta submission time, or by its
        creation in Odoo when that is missing (_lead_window). Synced and
        Failed count sync-log rows by when Odoo processed them.

        Everything except the webhook, queue drain and backfill pills is
        limited to the companies selected in the switcher, plus records with
        no company.
        """
        # Check access before reading anything. Keep the message generic.
        if not self.env.user.has_group('meta_lead_ads.group_meta_admin'):
            raise AccessError(_("Meta Admin access required."))

        # Read the selected companies before sudo. Here Odoo refuses an
        # allowed_company_ids the user doesn't belong to; under sudo it
        # would accept any id.
        company_ids = self.env.companies.ids

        # Past the group check, read as sudo: a Meta Admin isn't necessarily a
        # Sales user and would otherwise hit AccessError on crm.lead. No secret
        # field is read below, so this doesn't expose anything extra. Sudo
        # also skips record rules, so every query below carries one of these
        # company scopes.
        self = self.sudo()

        # Counts may include rows with no company, as the crm lead rule does.
        lead_scope = [('company_id', 'in', company_ids + [False])]
        log_scope = [('company_id', 'in', company_ids + [False])]
        # Listed rows follow the sync-log rule instead: a row with no company
        # is only shown while it holds no payload.
        log_row_scope = ['|', ('company_id', 'in', company_ids),
                         '&', ('company_id', '=', False),
                         ('raw_payload', '=', False)]
        # Accounts always have a company. An account in another company still
        # counts when one of its Pages sends leads into a selected company.
        account_scope = ['|', ('company_id', 'in', company_ids),
                         ('page_ids.lead_company_id', 'in', company_ids)]

        if period_mode not in _PERIOD_MODES:
            period_mode = _DEFAULT_PERIOD_MODE

        d_from, d_to, bucket = self._dashboard_window(
            date_from, date_to, period_mode)

        Lead = self.env['crm.lead']
        meta = [('meta_leadgen_id', '!=', False)] + lead_scope
        win = self._lead_window(meta, d_from, d_to)

        # Leads over time. bucket is one of day/week/month/quarter/year.
        # Each branch of the window is grouped on its own date field and
        # the counts are added up per bucket; both are Datetime fields, so
        # the bucket values line up.
        per_bucket = {}
        submitted = Lead._read_group(
            win + [('meta_submitted_at', '!=', False)],
            [f'meta_submitted_at:{bucket}'], ['__count'])
        created = Lead._read_group(
            win + [('meta_submitted_at', '=', False)],
            [f'create_date:{bucket}'], ['__count'])
        for (b, c) in submitted + created:
            per_bucket[b] = per_bucket.get(b, 0) + c
        series = [{'bucket': self._bucket_label(b, bucket),
                   'count': per_bucket[b]}
                  for b in sorted(per_bucket)]

        total = Lead.search_count(win)
        started = Lead.search_count(win + _STARTED_AS_LEAD)
        converted = Lead.search_count(win + _CONVERTED)
        direct = Lead.search_count(win + _CREATED_AS_OPP)
        # Won = opportunities of the window that now sit in a won stage.
        won = Lead.search_count(win + _WON)
        opportunities = converted + direct

        Log = self.env['meta.sync.log']
        logwin = log_scope + [('create_date', '>=', d_from),
                              ('create_date', '<', d_to)]
        synced = self._synced_count(log_scope, d_from, d_to)
        # A failed row is still unresolved: a retry rewrites it, healing
        # deletes it once the lead exists, and an erasure turns it into a
        # removed-lead skip.
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
            # None means there is nothing to measure; the dashboard shows a
            # dash for it.
            'conversion': {
                'converted_rate': (converted / started) if started else None,
                'won_rate': (won / opportunities) if opportunities else None,
                'created_as_opportunity': direct,
            },
            'campaigns': campaigns,
            'conversion_ranking': ranked,
            'drilldown': self._dashboard_drilldown(meta, d_from, d_to),
            # These two ignore the selected window.
            'sync_recent': self._dashboard_sync_recent(log_row_scope),
            'health': self._dashboard_health(account_scope, log_scope),
            'deltas': self._dashboard_deltas(
                meta, d_from, d_to, period_mode, log_scope),
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
    def _lead_window(self, meta, d_from, d_to):
        """``meta`` narrowed to the leads of [d_from, d_to).

        A lead is dated by Meta's submission time when it is stored, else
        by the time Odoo created it. The two branches exclude each other,
        so each lead falls in exactly one and is counted once.
        """
        return meta + [
            '|',
            '&', ('meta_submitted_at', '>=', d_from),
            ('meta_submitted_at', '<', d_to),
            '&', ('meta_submitted_at', '=', False),
            '&', ('create_date', '>=', d_from), ('create_date', '<', d_to),
        ]

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
    #
    # This helper and the ones below run as sudo. Their company scope (or
    # the `meta` domain that already holds it) is a required argument, so
    # none of them can run unscoped.
    # ------------------------------------------------------------------ #
    @api.model
    def _dashboard_deltas(self, meta, d_from, d_to, period_mode, log_scope):
        """Relative change per KPI vs the previous period. None when the
        previous count is 0, so the UI hides the delta."""
        prev_from, prev_to = self._dashboard_prev_window(
            d_from, d_to, period_mode)
        Lead = self.env['crm.lead']
        Log = self.env['meta.sync.log']

        cur_new = Lead.search_count(self._lead_window(meta, d_from, d_to))
        prior_new = Lead.search_count(
            self._lead_window(meta, prev_from, prev_to))

        cur_synced = self._synced_count(log_scope, d_from, d_to)
        prior_synced = self._synced_count(log_scope, prev_from, prev_to)

        cur_failed = Log.search_count(log_scope + [
            ('create_date', '>=', d_from), ('create_date', '<', d_to),
            ('status', '=', 'failed')])
        prior_failed = Log.search_count(log_scope + [
            ('create_date', '>=', prev_from), ('create_date', '<', prev_to),
            ('status', '=', 'failed')])

        return {
            'new': self._relative_delta(cur_new, prior_new),
            'synced': self._relative_delta(cur_synced, prior_synced),
            'failed': self._relative_delta(cur_failed, prior_failed),
        }

    @api.model
    def _synced_count(self, log_scope, d_from, d_to):
        """Synced rows processed in [d_from, d_to): the live sync-log rows
        plus the daily totals of rows the cleanup has already deleted.

        The cleanup adds a row to the totals in the same transaction that
        deletes it, so a row is counted on one side only. The totals are
        kept per day; the bounds are exact because the dashboard windows
        start and end at UTC midnight.
        """
        live = self.env['meta.sync.log'].search_count(log_scope + [
            ('create_date', '>=', d_from), ('create_date', '<', d_to),
            ('status', 'in', _SYNCED_STATUSES)])
        [(folded,)] = self.env['meta.sync.stats']._read_group(
            log_scope + [('day', '>=', d_from.date()),
                         ('day', '<', d_to.date()),
                         ('status', 'in', _SYNCED_STATUSES)],
            [], ['row_count:sum'])
        return live + (folded or 0)

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

        donut lists every campaign by volume, one row per Meta campaign ID,
        with leads that carry no ID grouped as 'Unattributed'. ranked is the
        same list, labels included, minus campaigns under _MIN_RANK_VOLUME
        leads.
        """
        donut = self._dashboard_group_conversion(
            meta, d_from, d_to, 'meta_campaign_id', 'meta_campaign_name')
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
                meta, d_from, d_to, 'meta_ad_id', 'meta_ad_name',
                top=_DRILLDOWN_TOP),
            'adsets': self._dashboard_group_conversion(
                meta, d_from, d_to, 'meta_adset_id', 'meta_adset_name',
                top=_DRILLDOWN_TOP),
        }

    @api.model
    def _dashboard_group_conversion(self, meta, d_from, d_to, id_field,
                                    name_field, top=None):
        """Group window leads by the Meta ID in ``id_field`` with count,
        converted_rate and won_rate, sorted by count and cut to ``top``
        rows if given.

        Uses five grouped reads (all, started as lead, converted, created as
        opportunity, won). A rate is None when its row has nothing to
        measure. Leads with an empty or blank ID are merged under
        'Unattributed'. Each other row is labelled with the newest name
        ``name_field`` holds for its ID, see _dashboard_suffix_labels.
        """
        Lead = self.env['crm.lead']
        win = self._lead_window(meta, d_from, d_to)

        # None stands for "no ID". False and '' come back as separate
        # groups; both end up there.
        def _key(gid):
            return gid.strip() if (gid and gid.strip()) else None

        totals = {}
        order = []
        stored_ids = {}
        for (gid, count) in Lead._read_group(win, [id_field], ['__count']):
            key = _key(gid)
            if key not in totals:
                totals[key] = {'count': 0, 'started': 0, 'converted': 0,
                               'direct': 0, 'won': 0}
                order.append(key)
                stored_ids[key] = []
            totals[key]['count'] += count
            if key is not None:
                stored_ids[key].append(gid)

        # count above keeps every Meta lead of the group, so the ranking
        # minimum doesn't depend on how the leads were created.
        for slot, domain in (('started', _STARTED_AS_LEAD),
                             ('converted', _CONVERTED),
                             ('direct', _CREATED_AS_OPP),
                             ('won', _WON)):
            for (gid, count) in Lead._read_group(win + domain, [id_field],
                                                 ['__count']):
                key = _key(gid)
                if key in totals:
                    totals[key][slot] += count

        rows = []
        for key in order:
            agg = totals[key]
            c, started, conv = agg['count'], agg['started'], agg['converted']
            opps = conv + agg['direct']
            rows.append({
                '_gid': key,
                'count': c,
                'converted_rate': (conv / started) if started else None,
                'won_rate': (agg['won'] / opps) if opps else None,
            })
        rows.sort(key=lambda r: r['count'], reverse=True)
        if top is not None:
            rows = rows[:top]

        # Labels are only worked out for the rows that are returned, so a
        # suffix only appears when two of those rows share a name.
        lookup = [gid for r in rows if r['_gid'] is not None
                  for gid in stored_ids[r['_gid']]]
        names = self._dashboard_id_labels(meta, id_field, name_field, lookup)
        for row in rows:
            row['name'] = names.get(row['_gid']) or False
        return self._dashboard_suffix_labels(rows)

    @api.model
    def _dashboard_id_labels(self, meta, id_field, name_field, ids):
        """Return {id: newest non-blank name, or False} for the given IDs.

        "Newest" is the name on the most recently created lead that has one.
        The lookup keeps the company scope in ``meta`` but not the period,
        so a renamed campaign shows its current name in every period.
        """
        labels = {gid.strip(): False for gid in ids}
        if not ids:
            return labels
        newest = {}
        groups = self.env['crm.lead']._read_group(
            meta + [(id_field, 'in', list(ids)),
                    (name_field, '!=', False), (name_field, '!=', '')],
            [id_field, name_field], ['id:max'])
        for (gid, name, max_id) in groups:
            name = (name or '').strip()
            if not name:
                continue
            key = gid.strip()
            if max_id > newest.get(key, 0):
                newest[key] = max_id
                labels[key] = name
        return labels

    @api.model
    def _dashboard_suffix_labels(self, rows):
        """Give each row its final ``name`` and drop the internal ID.

        Rows come in with '_gid' (None for no ID) and 'name' (the newest
        name, or False). A row with no ID is 'Unattributed'. When two or
        more rows share a name, and on every row whose ID has no name
        ('Unnamed'), the end of the ID is added: the last 4 characters,
        then 6, then the whole ID, until the rows in that group differ.
        """
        groups = {}
        for row in rows:
            if row['_gid'] is None:
                row['name'] = _UNATTRIBUTED
                continue
            nameless = not row['name']
            if nameless:
                row['name'] = _UNNAMED
            group = groups.setdefault(row['name'], {'rows': [],
                                                    'nameless': False})
            group['rows'].append(row)
            group['nameless'] = group['nameless'] or nameless

        for name, group in groups.items():
            members = group['rows']
            if len(members) < 2 and not group['nameless']:
                continue
            gids = [r['_gid'] for r in members]
            for width in _SUFFIX_WIDTHS + (None,):
                tails = [gid[-width:] if width else gid for gid in gids]
                if len(set(tails)) == len(tails):
                    break
            for row, tail in zip(members, tails):
                row['name'] = name + _SUFFIX_SEP + tail

        for row in rows:
            del row['_gid']
        return rows

    # ------------------------------------------------------------------ #
    # Recent sync activity (not limited to the selected window).
    # ------------------------------------------------------------------ #
    @api.model
    def _dashboard_sync_recent(self, log_row_scope):
        """The 5 newest sync-log rows in scope, with status, lead id and date
        only. raw_payload is never read."""
        rows = self.env['meta.sync.log'].search_read(
            log_row_scope, ['status', 'meta_leadgen_id', 'create_date'],
            limit=5)
        # Rebuild each row so only these three keys go out (search_read also
        # adds 'id').
        return [{
            'status': r['status'],
            'meta_leadgen_id': r['meta_leadgen_id'],
            'create_date': fields.Datetime.to_string(r['create_date'])
            if r['create_date'] else False,
        } for r in rows]

    # ------------------------------------------------------------------ #
    # System health, worst status across active accounts in scope.
    # ------------------------------------------------------------------ #
    @api.model
    def _dashboard_health(self, account_scope, log_scope):
        """Return the webhook, queue drain, backfill and token signals, each
        a {status, label, caption} dict of strings. No token or expiry value
        is included, and no account name or id, since an account in scope
        may belong to another company.

        The first three come from the same check as the account form's
        scheduler badge, so the two can't disagree. log_scope limits the
        count of leads held by an archived Page.
        """
        accounts = self.env['meta.account'].search(
            [('active', '=', True)] + account_scope)
        # There is one webhook endpoint and one drain and backfill job for
        # the whole database, so their status is not split by company. Only
        # the token pill and the count of waiting leads follow the scope.
        health = self.env['meta.account']._meta_cron_health()
        groups = self.env['meta.sync.log']._read_group(
            log_scope + _HELD_DOMAIN, ['page_id'], ['__count'])
        held = sum(count for _page, count in groups)
        return {
            'webhook': self._health_webhook(health, held=held,
                                            held_pages=len(groups)),
            'drain': self._health_drain(health),
            'backfill': self._health_backfill(health),
            'token': self._health_token(accounts,
                                        self._health_caption(accounts)),
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
    def _health_webhook(self, health, held=0, held_pages=0):
        """Webhook signal. Running needs a recent successful webhook lead;
        an old success with no failure since is only quiet, and failures
        newer than the last success read as failing. When ``held`` leads
        wait on archived Pages, the caption says so; the status doesn't
        change."""
        webhook = health['webhook']
        status = webhook['status']
        last_ok = webhook['last_ok']
        if last_ok:
            received = _("last lead received %s") % self._humanize_ago(
                health['now'] - last_ok)
        else:
            # An upgraded database may have had successes before the stamp
            # existed, so say only what is recorded.
            received = _("no success recorded yet")
        if status == 'failing':
            label = _("Receiving, but failing")
            if last_ok:
                caption = _("%(count)s failed since the last success",
                            count=webhook['failed_count'])
            else:
                caption = _(
                    "%(count)s failed in the last %(days)s days, "
                    "no success recorded yet",
                    count=webhook['failed_count'],
                    days=const.WEBHOOK_FAIL_LOOKBACK_DAYS)
        elif status == 'backlog':
            label = _("Falling behind")
            caption = self._health_backlog_caption(webhook)
        elif status == 'running':
            label, caption = _("Running"), received
        elif status == 'quiet':
            label, caption = _("Quiet"), received
        elif status == 'no_activity':
            label, caption = _("No webhook leads yet"), received
        else:
            label, caption = _("Not running"), received
        if held:
            if held == 1:
                clause = _("1 lead waiting on an archived Page")
            elif held_pages == 1:
                clause = _("%(count)s leads waiting on an archived Page",
                           count=held)
            else:
                clause = _("%(count)s leads waiting on archived Pages",
                           count=held)
            caption = _("%(caption)s; %(held)s", caption=caption,
                        held=clause)
        return {'status': status, 'label': label, 'caption': caption}

    @api.model
    def _health_drain(self, health):
        """Queue drain signal: its own last run, its cron and the queue."""
        return self._health_cron_job(health, health['drain'])

    @api.model
    def _health_backfill(self, health):
        """Backfill signal: its own last run and its cron."""
        return self._health_cron_job(health, health['backfill'])

    @api.model
    def _health_cron_job(self, health, job):
        """Map one scheduled job's result to {status, label, caption}."""
        status = job['status']
        if status == 'ok':
            label = _("Running")
        elif status == 'pending':
            label = _("Waiting for first run")
        elif status == 'backlog':
            label = _("Falling behind")
        elif status == 'disabled':
            label = _("Disabled")
        else:
            label = _("Not running")
        if status == 'pending':
            caption = _("give it a few minutes")
        elif status == 'backlog':
            caption = self._health_backlog_caption(job)
        elif status == 'disabled':
            caption = _("scheduled action is off")
        elif job['last_run']:
            caption = _("last ran %s") % self._humanize_ago(
                health['now'] - job['last_run'])
        else:
            caption = _("hasn't run yet")
        return {'status': status, 'label': label, 'caption': caption}

    @api.model
    def _health_backlog_caption(self, signal):
        return _("%(count)s events waiting over %(minutes)s minutes",
                 count=signal.get('backlog_count', 0),
                 minutes=signal.get('backlog_minutes', 0))

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
            if _TOKEN_SEVERITY[code] > _TOKEN_SEVERITY[worst]:
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
