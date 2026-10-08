# Meta Lead Ads Integration for Odoo — © 2026 CoreSys Builders. All rights reserved.
# Licensed under the Odoo Proprietary License v1.0 (OPL-1).
# Unauthorized copying, redistribution, or resale of this software, in whole or in
# part, via any medium, is strictly prohibited and constitutes a license violation.
# OPL-1: https://www.odoo.com/documentation/19.0/legal/licenses.html#odoo-apps

"""Tests for ``meta.account.get_dashboard_metrics``.

The window is half-open in UTC: a record at ``from`` counts, one at ``to``
does not. Fixture create_dates are pinned to 12:00 UTC so a server timezone
can't push a record into the neighbouring day.

period_mode is what the user picked; bucket_granularity is derived from it
and is always a real date unit, never 'custom'.
"""
from datetime import datetime, timedelta

from odoo.tests.common import TransactionCase, tagged
from odoo.fields import Datetime

from .test_ingest import IngestFixtureMixin

# Seeded create_dates use noon UTC so timezone conversion can't move them
# across a day boundary.
NOON = 12


class DashboardFixtureMixin(IngestFixtureMixin):
    """Fixed dashboard data: Meta leads across dates, campaigns and stages,
    plus sync-log rows inside and outside the test window."""

    def setUp(self):
        super().setUp()
        self.Account = self.env['meta.account']
        self.Log = self.env['meta.sync.log']

        base_internal = self.env.ref('base.group_user')
        self.admin_group = self.env.ref('meta_lead_ads.group_meta_admin')
        self.dash_admin = self.env['res.users'].create({
            'name': 'Dash Admin', 'login': 'dash_admin_metrics',
            'group_ids': [(6, 0, [base_internal.id, self.admin_group.id])]})

        # Create our own won stage; a fresh database may not have one.
        self.won_stage = self.env['crm.stage'].create(
            {'name': 'Dash Won', 'is_won': True})
        self.open_stage = self.env['crm.stage'].search(
            [('is_won', '=', False)], limit=1)
        if not self.open_stage:
            self.open_stage = self.env['crm.stage'].create(
                {'name': 'Dash New', 'is_won': False})

        # Window under test is June 2026; May is the prior period for deltas.
        self.win_from = datetime(2026, 6, 1, 0, 0, 0)
        self.win_to = datetime(2026, 7, 1, 0, 0, 0)
        self.prior_from = datetime(2026, 5, 1, 0, 0, 0)

        self._seed = {'n': 0}

    # ---- seed helpers ----------------------------------------------------

    def _mk_lead(self, when, *, campaign=None, lead_type='lead', won=False,
                 email=None):
        """Create a Meta lead and set its create_date to noon UTC on ``when``."""
        self._seed['n'] += 1
        stamp = datetime(when.year, when.month, when.day, NOON, 0, 0)
        vals = {
            'name': 'Dash Lead %d' % self._seed['n'],
            'type': lead_type,
            'meta_leadgen_id': 'DLG_%d' % self._seed['n'],
            'meta_campaign_name': campaign,
            'stage_id': (self.won_stage if won else self.open_stage).id,
        }
        if email is not None:
            vals['email_from'] = email
        lead = self.Lead.create(vals)
        # The ORM sets create_date on insert, so overwrite it in SQL.
        lead.flush_recordset()
        self.env.cr.execute(
            "UPDATE crm_lead SET create_date = %s WHERE id = %s",
            (Datetime.to_string(stamp), lead.id))
        lead.invalidate_recordset(['create_date'])
        return lead

    def _mk_log(self, when, status, *, trigger=None, leadgen_id=None):
        """Create a sync-log row dated noon UTC on ``when``."""
        self._seed['n'] += 1
        stamp = datetime(when.year, when.month, when.day, NOON, 0, 0)
        log = self.Log.create({
            'meta_leadgen_id': leadgen_id or ('SLG_%d' % self._seed['n']),
            'status': status,
            'trigger': trigger,
        })
        log.flush_recordset()
        self.env.cr.execute(
            "UPDATE meta_sync_log SET create_date = %s WHERE id = %s",
            (Datetime.to_string(stamp), log.id))
        log.invalidate_recordset(['create_date'])
        return log

    def _seed_window(self):
        """Seed the June 2026 data set shared by the metric tests.

        Includes leads on June 1, June 30 and July 1 (the last one falls
        outside the window), May leads for deltas, campaigns above and below
        the 10-lead ranking threshold, and sync-log rows in and out of range.
        """
        self.at_from = self._mk_lead(self.win_from, campaign='Alpha')
        self.before_to = self._mk_lead(
            self.win_to - timedelta(days=1), campaign='Alpha')
        self.at_to = self._mk_lead(self.win_to, campaign='Alpha')  # excluded

        # Beta: 12 leads, so it qualifies for the conversion ranking.
        for i in range(12):
            self._mk_lead(
                datetime(2026, 6, 10), campaign='Beta',
                lead_type='opportunity' if i < 6 else 'lead',
                won=(i < 3))   # 6 opportunities, 3 of them won

        # Gamma: only 4 leads, so it is left out of the ranking.
        for i in range(4):
            self._mk_lead(datetime(2026, 6, 12), campaign='Gamma',
                          lead_type='opportunity' if i < 2 else 'lead')

        # False and '' campaign names should both show as 'Unattributed'.
        self._mk_lead(datetime(2026, 6, 15), campaign=False)
        self._mk_lead(datetime(2026, 6, 15), campaign='')

        # May leads give deltas['new'] something to compare against.
        self.prior_leads = [
            self._mk_lead(datetime(2026, 5, 10), campaign='Alpha')
            for _ in range(2)]

        # June sync-log rows with mixed status and trigger.
        self._mk_log(datetime(2026, 6, 5), 'success', trigger='webhook')
        self._mk_log(datetime(2026, 6, 6), 'skipped_idempotent', trigger='cron')
        self._mk_log(datetime(2026, 6, 7), 'failed', trigger='webhook')
        self._mk_log(datetime(2026, 6, 8), 'pending', trigger='manual')
        # May rows, which must not count toward June KPIs.
        self._mk_log(datetime(2026, 5, 20), 'success', trigger='cron')
        self._mk_log(datetime(2026, 5, 21), 'failed', trigger='webhook')

    def _metrics(self, **kw):
        """Call get_dashboard_metrics as the dashboard admin user."""
        return self.Account.with_user(self.dash_admin).get_dashboard_metrics(
            **kw)


@tagged('post_install', '-at_install')
class TestDashboardMetrics(DashboardFixtureMixin, TransactionCase):
    """Series, KPIs, deltas, conversion, campaign ranking and recent syncs."""

    # ---- leads over time -------------------------------------------------

    def test_series_buckets_meta_leads_by_derived_granularity(self):
        """The series totals the in-window Meta leads, bucketed by date unit."""
        self._seed_window()
        m = self._metrics(period_mode='month',
                          date_from=self.win_from, date_to=self.win_to)
        self.assertIn('series', m)
        self.assertIn('window', m)
        self.assertNotEqual(m['window']['bucket_granularity'], 'custom')
        self.assertIn(
            m['window']['bucket_granularity'],
            ('day', 'week', 'month', 'quarter', 'year'))
        in_window = self.Lead.search_count([
            ('meta_leadgen_id', '!=', False),
            ('create_date', '>=', self.win_from),
            ('create_date', '<', self.win_to)])
        self.assertEqual(sum(b['count'] for b in m['series']), in_window)

    # ---- period mode and bucket size -------------------------------------

    def test_named_period_modes_derive_bucket(self):
        """Month, quarter and year modes each derive a valid bucket unit."""
        self._seed_window()
        for mode in ('month', 'quarter', 'year'):
            m = self._metrics(period_mode=mode)
            self.assertEqual(m['window']['period_mode'], mode)
            self.assertIn(
                m['window']['bucket_granularity'],
                ('day', 'week', 'month', 'quarter', 'year'))
            self.assertNotEqual(m['window']['bucket_granularity'], 'custom')

    def test_custom_short_range_buckets_by_day(self):
        """A custom range of 31 days or less is bucketed by day."""
        self._seed_window()
        c_from = datetime(2026, 6, 1, 0, 0, 0)
        c_to = datetime(2026, 6, 15, 0, 0, 0)
        m = self._metrics(period_mode='custom', date_from=c_from, date_to=c_to)
        self.assertEqual(m['window']['period_mode'], 'custom')
        self.assertEqual(m['window']['bucket_granularity'], 'day')

    def test_window_is_half_open(self):
        """A lead at ``from`` is counted; a lead at ``to`` is not."""
        self._seed_window()
        m = self._metrics(period_mode='custom',
                          date_from=self.win_from, date_to=self.win_to)
        in_window = self.Lead.search_count([
            ('meta_leadgen_id', '!=', False),
            ('create_date', '>=', self.win_from),
            ('create_date', '<', self.win_to)])
        self.assertEqual(m['kpis']['new'], in_window)
        self.assertEqual(self.Lead.search_count([
            ('id', '=', self.at_to.id),
            ('create_date', '>=', self.win_from),
            ('create_date', '<', self.win_to)]), 0)

    # ---- deltas ----------------------------------------------------------

    def test_deltas_relative_when_prior_has_data(self):
        """deltas['new'] is (current - prior) / prior when May has leads."""
        self._seed_window()
        m = self._metrics(period_mode='month',
                          date_from=self.win_from, date_to=self.win_to)
        current = self.Lead.search_count([
            ('meta_leadgen_id', '!=', False),
            ('create_date', '>=', self.win_from),
            ('create_date', '<', self.win_to)])
        prior = self.Lead.search_count([
            ('meta_leadgen_id', '!=', False),
            ('create_date', '>=', self.prior_from),
            ('create_date', '<', self.win_from)])
        self.assertTrue(prior)
        self.assertAlmostEqual(
            m['deltas']['new'], (current - prior) / prior, places=6)

    def test_delta_none_when_no_prior(self):
        """deltas['new'] is None when the prior period has no leads."""
        self._seed_window()
        # Nothing is seeded in February, the period before March.
        f = datetime(2026, 3, 1, 0, 0, 0)
        t = datetime(2026, 4, 1, 0, 0, 0)
        m = self._metrics(period_mode='month', date_from=f, date_to=t)
        self.assertIsNone(m['deltas']['new'])

    def test_zero_total_window_does_not_raise(self):
        """An empty window returns zeros instead of dividing by zero."""
        self._seed_window()
        f = datetime(2026, 1, 1, 0, 0, 0)
        t = datetime(2026, 2, 1, 0, 0, 0)
        m = self._metrics(period_mode='month', date_from=f, date_to=t)
        self.assertEqual(m['kpis']['new'], 0)
        self.assertEqual(sum(b['count'] for b in m['series']), 0)
        self.assertEqual(m['conversion']['opportunity_rate'], 0.0)
        self.assertEqual(m['conversion']['won_rate'], 0.0)

    # ---- KPIs ------------------------------------------------------------

    def test_kpis_count_window_sources(self):
        """New, synced and failed KPIs count only rows inside the window."""
        self._seed_window()
        m = self._metrics(period_mode='month',
                          date_from=self.win_from, date_to=self.win_to)
        logwin = [('create_date', '>=', self.win_from),
                  ('create_date', '<', self.win_to)]
        self.assertEqual(m['kpis']['new'], self.Lead.search_count([
            ('meta_leadgen_id', '!=', False)] + logwin))
        self.assertEqual(m['kpis']['synced'], self.Log.search_count(
            logwin + [('status', 'in', ('success', 'skipped_idempotent'))]))
        self.assertEqual(m['kpis']['failed'], self.Log.search_count(
            logwin + [('status', '=', 'failed')]))

    # ---- conversion ------------------------------------------------------

    def test_conversion_rates_with_opportunity_denominator(self):
        """opportunity_rate is opp / total; won_rate is won / opp."""
        self._seed_window()
        m = self._metrics(period_mode='month',
                          date_from=self.win_from, date_to=self.win_to)
        win = [('meta_leadgen_id', '!=', False),
               ('create_date', '>=', self.win_from),
               ('create_date', '<', self.win_to)]
        total = self.Lead.search_count(win)
        opp = self.Lead.search_count(win + [('type', '=', 'opportunity')])
        won = self.Lead.search_count(win + [('stage_id.is_won', '=', True)])
        self.assertTrue(opp)
        self.assertTrue(won)
        self.assertAlmostEqual(
            m['conversion']['opportunity_rate'], opp / total, places=6)
        self.assertAlmostEqual(
            m['conversion']['won_rate'], won / opp, places=6)

    # ---- campaigns -------------------------------------------------------

    def test_campaigns_donut_includes_all_ranking_guards_low_volume(self):
        """The donut lists every campaign; the ranking drops those under 10."""
        self._seed_window()
        m = self._metrics(period_mode='month',
                          date_from=self.win_from, date_to=self.win_to)
        donut_names = {c['name'] for c in m['campaigns']}
        self.assertIn('Unattributed', donut_names)
        self.assertIn('Gamma', donut_names)
        ranked = m['campaigns_ranked'] if 'campaigns_ranked' in m \
            else m['conversion_ranking']
        ranked_names = {c['name'] for c in ranked}
        self.assertIn('Beta', ranked_names)
        self.assertNotIn('Gamma', ranked_names)

    def test_ranking_table_consumes_guarded_list_not_full_campaigns(self):
        """The JS ranking table reads conversion_ranking, not campaigns.

        The table once rendered the full campaigns list, showing misleading
        rates for small campaigns. There is no JS test runner here, so this
        checks the dashboard.js source text instead.
        """
        import os
        js_path = os.path.join(
            os.path.dirname(os.path.dirname(__file__)),
            'static', 'src', 'dashboard', 'dashboard.js')
        with open(js_path, encoding='utf-8') as fh:
            src = fh.read()
        self.assertIn(
            'conversion_ranking', src,
            "dashboard.js must consume the min-volume-guarded "
            "`conversion_ranking` for the ranking table.")
        self.assertIn(
            'data.campaigns', src,
            "dashboard.js donut must keep the full `campaigns` volume list.")

        def _getter_body(name):
            """Return the source of a JS method body, found by its definition."""
            sig = name + '() {'
            start = src.index(sig)
            # Stop at the next method or getter.
            rest = src[start + len(sig):]
            nxt = min(
                (i for i in (rest.find('\n    _'), rest.find('\n    get '))
                 if i != -1),
                default=len(rest))
            return rest[:nxt]

        ranking_body = _getter_body('_rankingRows')
        self.assertIn(
            'conversion_ranking', ranking_body,
            "the ranking getter must return `conversion_ranking`.")
        self.assertNotIn(
            'data.campaigns', ranking_body,
            "the ranking getter must not fall back to the full `campaigns` "
            "list, which would drop the minimum-volume rule.")
        self.assertIn(
            'data.campaigns', _getter_body('_donutRows'),
            "the donut getter must keep the full `campaigns` volume list.")

    # ---- recent syncs ----------------------------------------------------

    def test_sync_recent_is_global_latest_five(self):
        """sync_recent is the five newest log rows overall, whatever the window.

        Rows must not include raw_payload.
        """
        self._seed_window()
        # A one-day window that leaves out nearly every log row.
        narrow_from = datetime(2026, 6, 8, 0, 0, 0)
        narrow_to = datetime(2026, 6, 9, 0, 0, 0)
        m = self._metrics(period_mode='custom',
                          date_from=narrow_from, date_to=narrow_to)
        recent = m['sync_recent']
        total_logs = self.Log.search_count([])
        self.assertEqual(len(recent), min(5, total_logs))
        dates = [r['create_date'] for r in recent]
        self.assertEqual(dates, sorted(dates, reverse=True))
        # The newest row is outside the window but still listed.
        newest = self.Log.search([], order='create_date desc', limit=1)
        self.assertIn(newest.meta_leadgen_id,
                      [r['meta_leadgen_id'] for r in recent])
        for r in recent:
            self.assertNotIn('raw_payload', r)
