# Meta Lead Ads Integration for Odoo — © 2026 CoreSys Builders. All rights reserved.
# Licensed under the Odoo Proprietary License v1.0 (OPL-1).
# Unauthorized copying, redistribution, or resale of this software, in whole or in
# part, via any medium, is strictly prohibited and constitutes a license violation.
# OPL-1: https://www.odoo.com/documentation/20.0/legal/licenses.html#odoo-apps

"""Tests for ``meta.account.get_dashboard_metrics``.

The window is half-open in UTC: a record at ``from`` counts, one at ``to``
does not. Fixture create_dates are pinned to 12:00 UTC so a server timezone
can't push a record into the neighbouring day.

period_mode is what the user picked; bucket_granularity is derived from it
and is always a real date unit, never 'custom'.
"""
import json
from datetime import datetime, timedelta
from unittest import mock

from odoo.tests.common import TransactionCase, tagged
from odoo.fields import Datetime

from odoo.addons.meta_lead_ads.models import const
from odoo.addons.meta_lead_ads.models import meta_dashboard as _dash

from .test_ingest import IngestFixtureMixin
from .test_multi_company import MultiCompanyFixtureMixin

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
                 email=None, extra=None, submitted=None, converted=False):
        """Create a Meta lead and set its create_date to noon UTC on ``when``.

        ``submitted``, when given, is stored as meta_submitted_at, also at
        noon UTC. ``extra`` is merged into the create values (company, ad
        names...). ``converted`` stamps date_conversion at the same noon, as
        if sales had converted the lead.
        """
        self._seed['n'] += 1
        stamp = datetime(when.year, when.month, when.day, NOON, 0, 0)
        vals = {
            'name': 'Dash Lead %d' % self._seed['n'],
            'type': lead_type,
            'meta_leadgen_id': 'DLG_%d' % self._seed['n'],
            'meta_campaign_name': campaign,
            'stage_id': (self.won_stage if won else self.open_stage).id,
        }
        if submitted is not None:
            vals['meta_submitted_at'] = datetime(
                submitted.year, submitted.month, submitted.day, NOON, 0, 0)
        if email is not None:
            vals['email_from'] = email
        if converted:
            vals['date_conversion'] = stamp
        vals.update(extra or {})
        # The dashboard groups by Meta ID, so a named campaign, ad or ad set
        # gets an ID made from its name unless the test passes one. Blank
        # names stay without an ID and show as 'Unattributed'.
        for id_field, name_field, prefix in (
                ('meta_campaign_id', 'meta_campaign_name', 'C_'),
                ('meta_ad_id', 'meta_ad_name', 'AD_'),
                ('meta_adset_id', 'meta_adset_name', 'AS_')):
            name = vals.get(name_field)
            if id_field not in vals and isinstance(name, str) and name.strip():
                vals[id_field] = prefix + name
        lead = self.Lead.create(vals)
        # The ORM sets create_date on insert, so overwrite it in SQL.
        lead.flush_recordset()
        self.env.cr.execute(
            "UPDATE crm_lead SET create_date = %s WHERE id = %s",
            (Datetime.to_string(stamp), lead.id))
        lead.invalidate_recordset(['create_date'])
        return lead

    def _mk_log(self, when, status, *, trigger=None, leadgen_id=None,
                page=None):
        """Create a sync-log row dated noon UTC on ``when``. Without a Page
        the row has no company."""
        self._seed['n'] += 1
        log = self.Log.create({
            'meta_leadgen_id': leadgen_id or ('SLG_%d' % self._seed['n']),
            'status': status,
            'trigger': trigger,
            'page_id': page.id if page else False,
        })
        self._set_log_date(log, when)
        return log

    def _set_log_date(self, log, when):
        stamp = datetime(when.year, when.month, when.day, NOON, 0, 0)
        log.flush_recordset()
        self.env.cr.execute(
            "UPDATE meta_sync_log SET create_date = %s WHERE id = %s",
            (Datetime.to_string(stamp), log.id))
        log.invalidate_recordset(['create_date'])

    def _age_rows(self, records, delta):
        """Set create_date to now - delta, for checks that compare against
        the current time rather than a calendar day. A failed sync row's
        failure time moves with it, as if it had failed then."""
        records.flush_recordset()
        when = Datetime.to_string(Datetime.now() - delta)
        self.env.cr.execute(
            'UPDATE "%s" SET create_date = %%s WHERE id IN %%s'
            % records._table, (when, tuple(records.ids)))
        if records._name == 'meta.sync.log':
            self.env.cr.execute(
                "UPDATE meta_sync_log SET failed_at = %s "
                "WHERE id IN %s AND status = 'failed'",
                (when, tuple(records.ids)))
            records.invalidate_recordset(['failed_at'])
        records.invalidate_recordset(['create_date'])

    def _set_stamp(self, key, when):
        """Write a health stamp; a false ``when`` removes it."""
        self.env['ir.config_parameter'].sudo().set_str(
            key, Datetime.to_string(when) if when else False)

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

        # Beta: 12 leads, so it qualifies for the conversion ranking. Its 6
        # opportunities were converted by sales, 3 of them won.
        for i in range(12):
            self._mk_lead(
                datetime(2026, 6, 10), campaign='Beta',
                lead_type='opportunity' if i < 6 else 'lead',
                won=(i < 3), converted=(i < 6))

        # Gamma: only 4 leads, so it is left out of the ranking. Its 2
        # opportunities were created as such, never converted.
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
        self.assertEqual(m['conversion'], {
            'converted_rate': None, 'won_rate': None,
            'created_as_opportunity': 0})

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

    def test_conversion_rates_over_leads_that_started_as_leads(self):
        """converted_rate is converted / started as lead; won_rate is won /
        all opportunities."""
        self._seed_window()
        m = self._metrics(period_mode='month',
                          date_from=self.win_from, date_to=self.win_to)
        win = [('meta_leadgen_id', '!=', False),
               ('create_date', '>=', self.win_from),
               ('create_date', '<', self.win_to)]
        started = self.Lead.search_count(win + [
            '|', ('type', '=', 'lead'), ('date_conversion', '!=', False)])
        converted = self.Lead.search_count(
            win + [('date_conversion', '!=', False)])
        direct = self.Lead.search_count(win + [
            ('type', '=', 'opportunity'), ('date_conversion', '=', False)])
        won = self.Lead.search_count(win + [
            '|', ('date_conversion', '!=', False),
            ('type', '=', 'opportunity'),
            ('stage_id.is_won', '=', True)])
        self.assertTrue(converted)
        self.assertTrue(direct)
        self.assertTrue(won)
        self.assertAlmostEqual(
            m['conversion']['converted_rate'], converted / started, places=6)
        self.assertAlmostEqual(
            m['conversion']['won_rate'], won / (converted + direct), places=6)
        # Gamma's two opportunities were created as such.
        self.assertEqual(m['conversion']['created_as_opportunity'], direct)
        self.assertEqual(direct, 2)

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


def _names(rows):
    return {r['name'] for r in rows}


@tagged('post_install', '-at_install')
class TestDashboardCompanyScope(MultiCompanyFixtureMixin, DashboardFixtureMixin,
                                TransactionCase):
    """Every card follows the companies picked in the switcher, plus records
    with no company. The webhook, queue drain and backfill pills stay
    global."""

    def _metrics_as(self, user, companies, **kw):
        kw.setdefault('period_mode', 'month')
        kw.setdefault('date_from', self.win_from)
        kw.setdefault('date_to', self.win_to)
        return (self.Account.with_user(user)
                .with_context(allowed_company_ids=companies.ids)
                .get_dashboard_metrics(**kw))

    def _co_lead(self, when, company, tag, **kw):
        # No salesperson or team, so crm's own company checks stay quiet.
        return self._mk_lead(when, campaign='Camp%s' % tag, extra={
            'company_id': company.id if company else False,
            'user_id': False, 'team_id': False,
            'meta_ad_name': 'Ad%s' % tag,
            'meta_adset_name': 'Set%s' % tag,
        }, **kw)

    def _quarantined_log(self, when, leadgen_id):
        """A row that kept its payload but lost both its lead and its Page."""
        lead = self.Lead.create({'name': 'Gone soon', 'type': 'lead'})
        log = self.Log.sudo()._record(
            leadgen_id, 'cron', 'success', lead=lead,
            raw={'field_data': [{'name': 'email',
                                 'values': ['q@example.com']}]})
        lead.unlink()
        self.env.flush_all()
        self.env.invalidate_all()
        self._set_log_date(log, when)
        self.assertFalse(log.company_id)
        self.assertTrue(log.sudo().raw_payload)
        return log

    def _assert_no_account_ref(self, health, accounts):
        refs = set(accounts.mapped('name')) | set(accounts.mapped('account_id'))
        for card, signal in health.items():
            self.assertEqual(set(signal), {'status', 'label', 'caption'}, card)
            for value in signal.values():
                self.assertNotIn(value, accounts.ids, card)
                for ref in refs:
                    self.assertNotIn(ref, str(value), card)

    def test_dashboard_company_scope_every_card(self):
        june = datetime(2026, 6, 10)
        may = datetime(2026, 5, 10)
        for _i in range(2):
            self._co_lead(june, self.co_a, 'A')
        self._co_lead(june, self.co_b, 'B', lead_type='opportunity', won=True)
        for _i in range(2):
            self._co_lead(june, self.co_b, 'B')
        blank = self._co_lead(june, False, 'Blank')
        self.assertFalse(blank.company_id)
        # Prior month, for the deltas.
        self._co_lead(may, self.co_a, 'A')
        for _i in range(2):
            self._co_lead(may, self.co_b, 'B')

        # Let every campaign into the ranking so its scope can be checked.
        with mock.patch.object(_dash, '_MIN_RANK_VOLUME', 1):
            m_a = self._metrics_as(self.admin_a, self.co_a)
            m_ab = self._metrics_as(self.admin_ab, self.co_a | self.co_b)

        self.assertEqual(m_a['kpis']['new'], 3)
        self.assertEqual(sum(b['count'] for b in m_a['series']), 3)
        self.assertEqual(m_a['conversion']['converted_rate'], 0.0)
        self.assertIsNone(m_a['conversion']['won_rate'])
        self.assertEqual(m_a['conversion']['created_as_opportunity'], 0)
        self.assertEqual(_names(m_a['campaigns']), {'CampA', 'CampBlank'})
        self.assertEqual(_names(m_a['conversion_ranking']),
                         {'CampA', 'CampBlank'})
        self.assertEqual(_names(m_a['drilldown']['ads']), {'AdA', 'AdBlank'})
        self.assertEqual(_names(m_a['drilldown']['adsets']),
                         {'SetA', 'SetBlank'})
        # 3 this month against 1 in May.
        self.assertAlmostEqual(m_a['deltas']['new'], 2.0, places=6)

        self.assertEqual(m_ab['kpis']['new'], 6)
        self.assertEqual(sum(b['count'] for b in m_ab['series']), 6)
        # The company B opportunity was created as one: 0 of 5 converted.
        self.assertEqual(m_ab['conversion']['converted_rate'], 0.0)
        self.assertEqual(m_ab['conversion']['created_as_opportunity'], 1)
        self.assertAlmostEqual(m_ab['conversion']['won_rate'], 1.0, places=6)
        self.assertEqual(_names(m_ab['campaigns']),
                         {'CampA', 'CampB', 'CampBlank'})
        self.assertIn('CampB', _names(m_ab['conversion_ranking']))
        self.assertIn('AdB', _names(m_ab['drilldown']['ads']))
        self.assertIn('SetB', _names(m_ab['drilldown']['adsets']))
        # 6 this month against 3 in May.
        self.assertAlmostEqual(m_ab['deltas']['new'], 1.0, places=6)

    def test_dashboard_sync_scope(self):
        self._mk_log(datetime(2026, 6, 5), 'success', leadgen_id='SA1',
                     page=self.page)
        self._mk_log(datetime(2026, 6, 20), 'success', leadgen_id='SB1',
                     page=self.page_b)
        self._mk_log(datetime(2026, 6, 21), 'failed', leadgen_id='SB2',
                     page=self.page_b)
        self._mk_log(datetime(2026, 6, 6), 'failed', leadgen_id='SN1')
        self._mk_log(datetime(2026, 5, 5), 'success', leadgen_id='SA0',
                     page=self.page)
        self._mk_log(datetime(2026, 5, 10), 'success', leadgen_id='SB0',
                     page=self.page_b)
        self._mk_log(datetime(2026, 5, 11), 'failed', leadgen_id='SB00',
                     page=self.page_b)
        self._mk_log(datetime(2026, 5, 6), 'failed', leadgen_id='SN0')
        # Newest row of all, so an unscoped list would show it first.
        self._quarantined_log(datetime(2026, 6, 22), 'SQ1')

        m_a = self._metrics_as(self.admin_a, self.co_a)
        # Synced: SA1 and the quarantined row. Failed: SN1.
        self.assertEqual(m_a['kpis']['synced'], 2)
        self.assertEqual(m_a['kpis']['failed'], 1)
        self.assertAlmostEqual(m_a['deltas']['synced'], 1.0, places=6)
        self.assertAlmostEqual(m_a['deltas']['failed'], 0.0, places=6)
        listed_a = [r['meta_leadgen_id'] for r in m_a['sync_recent']]
        self.assertEqual(set(listed_a), {'SA1', 'SN1', 'SA0', 'SN0'})

        m_b = self._metrics_as(self.admin_b, self.co_b)
        self.assertEqual(m_b['kpis']['synced'], 2)
        self.assertEqual(m_b['kpis']['failed'], 2)
        listed_b = [r['meta_leadgen_id'] for r in m_b['sync_recent']]
        self.assertEqual(listed_b, ['SB2', 'SB1', 'SN1', 'SB00', 'SB0'])

    def test_dashboard_health_destination_accounts(self):
        self.account.write({'token_valid': True,
                            'access_status': 'lead_retrieval_granted'})
        acc_b = self.Account.create({
            'name': 'Acct B', 'account_id': 'ACCB',
            'access_token': 'tok_acct_b', 'company_id': self.co_b.id})
        self.assertFalse(acc_b.token_valid)

        # acc_b feeds nothing into company A, so it doesn't count there.
        health = self._metrics_as(self.admin_a, self.co_a)['health']
        self.assertEqual(health['token']['status'], 'valid')
        self._assert_no_account_ref(health, self.account | acc_b)
        health = self._metrics_as(self.admin_b, self.co_b)['health']
        self.assertEqual(health['token']['status'], 'invalid')

        # With acc_b archived, company B is still fed by self.account
        # through page_b.
        acc_b.active = False
        health = self._metrics_as(self.admin_b, self.co_b)['health']
        self.assertEqual(health['token']['status'], 'valid')
        self._assert_no_account_ref(health, self.account | acc_b)

    def test_dashboard_global_status_cards(self):
        # A webhook failure in company B, newer than the last success.
        self._set_stamp(const.WEBHOOK_OK_PARAM,
                        Datetime.now() - timedelta(hours=1))
        failed = self.Log.create({
            'meta_leadgen_id': 'SW1', 'status': 'failed',
            'trigger': 'webhook', 'page_id': self.page_b.id})
        self._age_rows(failed, timedelta(minutes=10))
        h_a = self._metrics_as(self.admin_a, self.co_a)['health']
        h_b = self._metrics_as(self.admin_b, self.co_b)['health']
        self.assertEqual(h_a['webhook']['status'], 'failing')
        for card in ('webhook', 'drain', 'backfill'):
            self.assertEqual(h_a[card]['status'], h_b[card]['status'], card)
            self.assertEqual(h_a[card]['label'], h_b[card]['label'], card)
        self.assertEqual(h_a['webhook']['caption'], h_b['webhook']['caption'])

    def test_held_caption_counts_only_companies_in_scope(self):
        """The webhook pill is global, but the leads waiting on an archived
        Page are counted only in the companies the viewer picked."""
        self.assertEqual(self.page.lead_company_id, self.co_a)
        self.assertEqual(self.page_b.lead_company_id, self.co_b)
        for page in (self.page, self.page_b):
            self._mk_log(datetime(2026, 6, 10), 'failed', trigger='webhook',
                         page=page)
            page.sudo().active = False
        both = self.co_a | self.co_b
        caption_a = self._metrics_as(
            self.admin_a, self.co_a)['health']['webhook']['caption']
        caption_ab = self._metrics_as(
            self.admin_ab, both)['health']['webhook']['caption']
        self.assertTrue(
            caption_a.endswith('; 1 lead waiting on an archived Page'),
            caption_a)
        self.assertTrue(
            caption_ab.endswith('; 2 leads waiting on archived Pages'),
            caption_ab)


@tagged('post_install', '-at_install')
class TestDashboardHealthPills(DashboardFixtureMixin, TransactionCase):
    """The four health pills: webhook, queue drain, backfill and token, each
    from its own evidence, read through the same helper as the account
    form."""

    def setUp(self):
        super().setUp()
        self.drain = self.env.ref('meta_lead_ads.cron_meta_webhook_drain')
        self.backfill = self.env.ref('meta_lead_ads.cron_meta_backfill')
        self.drain.active = True
        self.backfill.active = True
        self.backfill.write({'interval_number': 15,
                             'interval_type': 'minutes'})
        now = Datetime.now()
        self.drain.nextcall = now
        self.backfill.nextcall = now
        self._set_stamp(const.DRAIN_RUN_PARAM, now - timedelta(minutes=2))
        self._set_stamp(const.BACKFILL_RUN_PARAM, now - timedelta(minutes=5))
        self._set_stamp(const.WEBHOOK_OK_PARAM, False)
        self._set_stamp('meta_lead_ads.installed_at',
                        now - timedelta(days=30))

    def _health(self):
        return self._metrics()['health']

    def _ago(self, **kw):
        return Datetime.now() - timedelta(**kw)

    def _failed_webhook_row(self, age):
        row = self._mk_log(datetime(2026, 6, 1), 'failed', trigger='webhook')
        row.flush_recordset()
        when = Datetime.to_string(Datetime.now() - age)
        self.env.cr.execute(
            'UPDATE meta_sync_log SET create_date = %s, failed_at = %s '
            'WHERE id = %s', (when, when, row.id))
        row.invalidate_recordset(['create_date', 'failed_at'])
        return row

    def _pending_event(self, leadgen_id, age):
        event = self.env['meta.webhook.event'].sudo().create({
            'leadgen_id': leadgen_id, 'page_id': 'PG1', 'status': 'pending'})
        self._age_rows(event, age)
        return event

    def _assert_pill(self, signal, status, label, caption):
        self.assertEqual(
            (signal['status'], signal['label'], signal['caption']),
            (status, label, caption))

    # ---- shape -----------------------------------------------------------

    def test_four_pills_of_strings(self):
        health = self._health()
        self.assertEqual(set(health), {'webhook', 'drain', 'backfill', 'token'})
        for name, signal in health.items():
            self.assertEqual(set(signal), {'status', 'label', 'caption'}, name)
            for value in signal.values():
                self.assertIsInstance(value, str, name)

    # ---- webhook ---------------------------------------------------------

    def test_webhook_running_after_a_recent_success(self):
        self._set_stamp(const.WEBHOOK_OK_PARAM, self._ago(hours=1))
        self._assert_pill(self._health()['webhook'], 'running', 'Running',
                          'last lead received 60 minutes ago')

    def test_webhook_quiet_after_an_old_success(self):
        self._set_stamp(const.WEBHOOK_OK_PARAM, self._ago(days=3))
        self._assert_pill(self._health()['webhook'], 'quiet', 'Quiet',
                          'last lead received 3 days ago')

    def test_webhook_failing_after_the_last_success(self):
        self._set_stamp(const.WEBHOOK_OK_PARAM, self._ago(hours=2))
        self._failed_webhook_row(timedelta(minutes=30))
        self._assert_pill(self._health()['webhook'], 'failing',
                          'Receiving, but failing',
                          '1 failed since the last success')

    def test_webhook_failure_older_than_the_success_is_not_failing(self):
        self._set_stamp(const.WEBHOOK_OK_PARAM, self._ago(hours=1))
        self._failed_webhook_row(timedelta(hours=3))
        self.assertEqual(self._health()['webhook']['status'], 'running')

    def test_webhook_failing_with_no_success_yet(self):
        self._failed_webhook_row(timedelta(minutes=30))
        self._assert_pill(self._health()['webhook'], 'failing',
                          'Receiving, but failing',
                          '1 failed in the last 7 days, '
                          'no success recorded yet')

    def test_webhook_no_evidence(self):
        self._assert_pill(self._health()['webhook'], 'no_activity',
                          'No webhook leads yet', 'no success recorded yet')

    def test_old_pending_event_is_falling_behind(self):
        self._set_stamp(const.WEBHOOK_OK_PARAM, self._ago(hours=1))
        self._pending_event('LG_PILL_SLOW', timedelta(minutes=20))
        health = self._health()
        self._assert_pill(health['drain'], 'backlog', 'Falling behind',
                          '1 events waiting over 10 minutes')
        self._assert_pill(health['webhook'], 'backlog', 'Falling behind',
                          '1 events waiting over 10 minutes')

    def test_new_pending_event_is_not_a_backlog(self):
        self._set_stamp(const.WEBHOOK_OK_PARAM, self._ago(hours=1))
        self._pending_event('LG_PILL_NEW', timedelta(minutes=2))
        health = self._health()
        self.assertEqual(health['drain']['status'], 'ok')
        self.assertEqual(health['webhook']['status'], 'running')

    def test_rate_limited_event_keeps_pills_green(self):
        """An event held back after a Meta rate limit is waiting on Meta,
        not stuck in the queue."""
        self._set_stamp(const.WEBHOOK_OK_PARAM, self._ago(hours=1))
        event = self._pending_event('LG_PILL_HELD', timedelta(minutes=20))
        event.write({'next_attempt_at': Datetime.now()
                     + timedelta(minutes=20)})
        health = self._health()
        self.assertEqual(health['drain']['status'], 'ok')
        self.assertEqual(health['webhook']['status'], 'running')

    # ---- leads held by an archived Page ------------------------------------

    def _held_rows(self, page, count, when=None):
        """Failed webhook rows of a Page that is then archived. They failed
        10 minutes ago, after the last success. ``when`` keeps their
        create_date on that day, for the window counts."""
        rows = self.Log.browse()
        for _i in range(count):
            rows |= self._mk_log(when or datetime(2026, 6, 10), 'failed',
                                 trigger='webhook', page=page)
        if when is None:
            self._age_rows(rows, timedelta(minutes=10))
        else:
            rows.flush_recordset()
            self.env.cr.execute(
                "UPDATE meta_sync_log SET failed_at = %s WHERE id IN %s",
                (Datetime.to_string(self._ago(minutes=10)), tuple(rows.ids)))
            rows.invalidate_recordset(['failed_at'])
        page.sudo().active = False
        return rows

    def _second_page(self):
        return self.env['meta.page'].create({
            'name': 'Second Page', 'page_id': 'PG_HOLD2',
            'account_id': self.account.id, 'lead_type': 'lead'})

    def test_held_rows_add_a_caption_clause(self):
        """Held leads don't turn the pill red; the caption says they
        wait."""
        self._set_stamp(const.WEBHOOK_OK_PARAM, self._ago(hours=1))
        self._held_rows(self.page, 2)
        self._assert_pill(self._health()['webhook'], 'running', 'Running',
                          'last lead received 60 minutes ago; '
                          '2 leads waiting on an archived Page')

    def test_one_held_row_caption(self):
        self._set_stamp(const.WEBHOOK_OK_PARAM, self._ago(hours=1))
        self._held_rows(self.page, 1)
        caption = self._health()['webhook']['caption']
        self.assertTrue(
            caption.endswith('; 1 lead waiting on an archived Page'), caption)

    def test_held_rows_on_two_pages_caption(self):
        self._set_stamp(const.WEBHOOK_OK_PARAM, self._ago(hours=1))
        self._held_rows(self._second_page(), 1)
        self._held_rows(self.page, 2)
        webhook = self._health()['webhook']
        self.assertEqual(webhook['status'], 'running')
        self.assertTrue(
            webhook['caption'].endswith(
                '; 3 leads waiting on archived Pages'), webhook['caption'])

    def test_held_rows_still_count_as_failed(self):
        """Archiving a Page doesn't hide its leads from the Failed KPI."""
        self._held_rows(self.page, 2, when=datetime(2026, 6, 10))
        m = self._metrics(period_mode='month', date_from=self.win_from,
                          date_to=self.win_to)
        self.assertEqual(m['kpis']['failed'], 2)
        self.assertNotEqual(m['health']['webhook']['status'], 'failing')

    # ---- queue drain -----------------------------------------------------

    def test_drain_running(self):
        self._assert_pill(self._health()['drain'], 'ok', 'Running',
                          'last ran 2 minutes ago')

    def test_drain_disabled(self):
        self.drain.active = False
        self._assert_pill(self._health()['drain'], 'disabled', 'Disabled',
                          'scheduled action is off')

    def test_drain_stalled_after_a_run(self):
        self._set_stamp(const.DRAIN_RUN_PARAM, self._ago(hours=2))
        self.drain.nextcall = self._ago(hours=2)
        self._assert_pill(self._health()['drain'], 'stalled', 'Not running',
                          'last ran 2 hours ago')

    def test_drain_stalled_without_a_run(self):
        self._set_stamp(const.DRAIN_RUN_PARAM, False)
        self.drain.nextcall = self._ago(hours=2)
        self._assert_pill(self._health()['drain'], 'stalled', 'Not running',
                          "hasn't run yet")

    def test_drain_waiting_for_its_first_run(self):
        self._set_stamp(const.DRAIN_RUN_PARAM, False)
        self._set_stamp('meta_lead_ads.installed_at', self._ago(minutes=1))
        self._assert_pill(self._health()['drain'], 'pending',
                          'Waiting for first run', 'give it a few minutes')

    # ---- backfill --------------------------------------------------------

    def test_backfill_running(self):
        self._assert_pill(self._health()['backfill'], 'ok', 'Running',
                          'last ran 5 minutes ago')

    def test_backfill_disabled(self):
        self.backfill.active = False
        self._assert_pill(self._health()['backfill'], 'disabled', 'Disabled',
                          'scheduled action is off')

    def test_backfill_stalled_after_a_run(self):
        self._set_stamp(const.BACKFILL_RUN_PARAM, self._ago(hours=2))
        self.backfill.nextcall = self._ago(hours=2)
        self._assert_pill(self._health()['backfill'], 'stalled',
                          'Not running', 'last ran 2 hours ago')

    def test_backfill_waiting_for_its_first_run(self):
        self._set_stamp(const.BACKFILL_RUN_PARAM, False)
        self._set_stamp('meta_lead_ads.installed_at', self._ago(minutes=1))
        self._assert_pill(self._health()['backfill'], 'pending',
                          'Waiting for first run', 'give it a few minutes')

    def test_backfill_stall_leaves_the_drain_alone(self):
        self._set_stamp(const.BACKFILL_RUN_PARAM, self._ago(hours=2))
        self.backfill.nextcall = self._ago(hours=2)
        health = self._health()
        self.assertEqual(health['drain']['status'], 'ok')
        self.assertEqual(health['backfill']['status'], 'stalled')

    def test_unknown_code_reads_not_running(self):
        """A status the dashboard doesn't know shows as Not running rather
        than as healthy."""
        now = Datetime.now()
        fake = {
            'now': now,
            'drain': {'status': 'odd', 'last_run': False, 'backlog_count': 0,
                      'backlog_minutes': 10},
            'backfill': {'status': 'odd', 'last_run': now},
            'webhook': {'status': 'odd', 'last_ok': False, 'failed_count': 0,
                        'backlog_count': 0, 'backlog_minutes': 10},
            'status': 'odd', 'message': '',
        }
        with mock.patch.object(type(self.Account), '_meta_cron_health',
                               return_value=fake):
            health = self._health()
        for name in ('webhook', 'drain', 'backfill'):
            self.assertEqual(health[name]['label'], 'Not running', name)
        self.assertEqual(health['drain']['caption'], "hasn't run yet")
        self.assertEqual(health['backfill']['caption'],
                         'last ran less than a minute ago')

    # ---- token -----------------------------------------------------------

    def test_token_pill_unchanged(self):
        self.account.write({
            'token_valid': True, 'access_status': 'lead_retrieval_granted',
            'last_checked': self._ago(hours=2)})
        self._assert_pill(self._health()['token'], 'valid', 'Valid',
                          'checked 2 hours ago')
        self.account.expires_at = self._ago(days=-2)
        self._assert_pill(self._health()['token'], 'expiring',
                          'Expiring soon', 'checked 2 hours ago')
        self.account.token_valid = False
        self._assert_pill(self._health()['token'], 'invalid', 'Invalid',
                          'checked 2 hours ago')

    # ---- no identifiers --------------------------------------------------

    def test_captions_hold_no_identifiers(self):
        """Counts and ages only: no account, App ID, token or lead id."""
        self.account.write({'last_checked': self._ago(hours=1)})
        failed = self._failed_webhook_row(timedelta(minutes=30))
        event = self._pending_event('LG_PILL_IDS', timedelta(minutes=20))
        # The fixture's name 'Page' is a word of the caption itself.
        self.page.name = 'Harbor Clinic'
        held = self._held_rows(self.page, 1)
        self._set_stamp(const.BACKFILL_RUN_PARAM, self._ago(hours=2))
        self.backfill.nextcall = self._ago(hours=2)
        health = self._health()
        self.assertEqual(health['webhook']['status'], 'failing')
        self.assertIn('waiting on an archived Page',
                      health['webhook']['caption'])
        self.assertEqual(health['drain']['status'], 'backlog')
        self.assertEqual(health['backfill']['status'], 'stalled')
        refs = [self.account.name, self.account.account_id,
                self.account.app_id, 'secret_test', 'tok_acct', 'tok_test',
                failed.meta_leadgen_id, event.leadgen_id, 'PG1', 'F1',
                held.meta_leadgen_id, self.page.page_id, self.page.name]
        for name, signal in health.items():
            for value in signal.values():
                for ref in refs:
                    self.assertNotIn(ref, value, name)


@tagged('post_install', '-at_install')
class TestDashboardRetentionCounts(MultiCompanyFixtureMixin,
                                  DashboardFixtureMixin, TransactionCase):
    """Synced and Failed for a past period stay the same after the log
    cleanup or a privacy erasure, and Failed counts only open failures."""

    def setUp(self):
        super().setUp()
        today = Datetime.now().replace(hour=0, minute=0, second=0,
                                       microsecond=0)
        # A 40-day window ending tonight, and the 40 days before it as the
        # prior period. Both start and end at UTC midnight, as the
        # dashboard's own windows do.
        self.r_to = today + timedelta(days=1)
        self.r_from = self.r_to - timedelta(days=40)
        # Inside the window but older than the 14-day sync log retention,
        # so the cleanup removes success and skipped rows from this day.
        self.recent = today - timedelta(days=30)
        # Inside the prior window.
        self.older = today - timedelta(days=60)
        self.env['ir.config_parameter'].sudo().set_str(
            const.SYNC_LOG_RETENTION_PARAM, '14')

    def _counts(self, user=None, companies=None):
        """KPIs and deltas of the test window, as ``user`` (the dashboard
        admin by default) with ``companies`` selected."""
        Account = self.Account.with_user(user or self.dash_admin)
        if companies is not None:
            Account = Account.with_context(allowed_company_ids=companies.ids)
        m = Account.get_dashboard_metrics(
            date_from=self.r_from, date_to=self.r_to, period_mode='custom')
        return m['kpis'], m['deltas']

    def _add_totals(self, day, company, status, count, trigger='cron'):
        """Insert a daily totals row the way the cleanup does, in SQL."""
        self.env.cr.execute("""
            INSERT INTO meta_sync_stats
                   (day, company_id, trigger, status, row_count)
            VALUES (%s, %s, %s, %s, %s)
        """, (day.date(), company.id if company else None, trigger, status,
              count))
        self.env['meta.sync.stats'].invalidate_model()

    def test_counts_are_the_same_after_the_cleanup(self):
        removable = self.Log
        for _i in range(3):
            removable |= self._mk_log(self.recent, 'success',
                                      trigger='webhook', page=self.page)
        removable |= self._mk_log(self.recent, 'skipped_idempotent',
                                  trigger='cron', page=self.page)
        removable |= self._mk_log(self.recent, 'skipped_duplicate',
                                  trigger='cron', page=self.page)
        failed = self.Log
        for _i in range(2):
            failed |= self._mk_log(self.recent, 'failed', trigger='webhook',
                                   page=self.page)
        # The prior period, for the deltas.
        for _i in range(2):
            removable |= self._mk_log(self.older, 'success', trigger='cron')
        failed |= self._mk_log(self.older, 'failed', trigger='cron')

        kpis, deltas = self._counts()
        # Success and idempotent skips count as synced; a duplicate skip
        # doesn't.
        self.assertEqual(kpis['synced'], 4)
        self.assertEqual(kpis['failed'], 2)
        self.assertAlmostEqual(deltas['synced'], 1.0, places=6)
        self.assertAlmostEqual(deltas['failed'], 1.0, places=6)

        self.Log._cron_vacuum_logs()

        self.assertFalse(removable.exists())
        self.assertEqual(failed.exists(), failed)
        kpis_after, deltas_after = self._counts()
        self.assertEqual(kpis_after, kpis)
        self.assertEqual(deltas_after, deltas)

    def test_erased_failure_leaves_synced_alone(self):
        """A failure that never became a lead and is then erased can't be
        retried any more, so it drops out of Failed. Synced doesn't move."""
        self._mk_log(self.recent, 'success', trigger='cron', page=self.page)
        self._mk_log(self.recent, 'failed', trigger='cron', page=self.page)
        failed = self.Log.sudo().create({
            'meta_leadgen_id': 'LG_RET_ERASE', 'trigger': 'cron',
            'status': 'failed', 'page_id': self.page.id,
            'raw_payload': json.dumps({'id': 'LG_RET_ERASE', 'field_data': [
                {'name': 'email', 'values': ['ret.erase@example.com']}]}),
        })
        self._set_log_date(failed, self.recent)
        self.assertEqual(failed.company_id, self.co_a)
        kpis, _deltas = self._counts()
        self.assertEqual(kpis['failed'], 2)

        failed.with_user(self.admin_a).action_erase_personal_data()

        self.assertEqual(failed.status, 'skipped_removed')
        kpis_after, _deltas = self._counts()
        self.assertEqual(kpis_after['synced'], kpis['synced'])
        self.assertEqual(kpis_after['failed'], kpis['failed'] - 1)

    def test_healed_failure_no_longer_counts(self):
        """A failure whose lead has since arrived is closed by the healing
        step, and Failed goes down with it."""
        self._mk_log(self.recent, 'failed', trigger='cron', page=self.page)
        self._mk_log(self.recent, 'failed', trigger='webhook',
                     leadgen_id='LG_RET_HEAL', page=self.page)
        kpis, _deltas = self._counts()
        self.assertEqual(kpis['failed'], 2)

        self._mk_lead(self.recent, extra={'meta_leadgen_id': 'LG_RET_HEAL'})
        self.Log._heal_resolved_failures()

        kpis_after, _deltas = self._counts()
        self.assertEqual(kpis_after['failed'], kpis['failed'] - 1)

    def test_totals_follow_the_selected_company(self):
        self._add_totals(self.recent, self.co_b, 'success', 5)
        self._add_totals(self.recent, False, 'skipped_idempotent', 2)
        # Other statuses and days outside the window don't count.
        self._add_totals(self.recent, self.co_a, 'skipped_duplicate', 7)
        self._add_totals(self.r_to, self.co_a, 'success', 9)

        kpis_a, _deltas = self._counts(self.admin_a, self.co_a)
        kpis_b, _deltas = self._counts(self.admin_b, self.co_b)
        kpis_ab, _deltas = self._counts(self.admin_ab, self.co_a | self.co_b)
        self.assertEqual(kpis_a['synced'], 2)
        self.assertEqual(kpis_b['synced'], 7)
        self.assertEqual(kpis_ab['synced'], 7)

    def test_totals_count_in_the_prior_period_delta(self):
        self._mk_log(self.recent, 'success', trigger='cron', page=self.page)
        self._mk_log(self.recent, 'success', trigger='cron', page=self.page)
        self._add_totals(self.older, self.co_a, 'success', 4)
        _kpis, deltas = self._counts(self.admin_a, self.co_a)
        self.assertAlmostEqual(deltas['synced'], -0.5, places=6)


@tagged('post_install', '-at_install')
class TestDashboardSubmissionTime(DashboardFixtureMixin, TransactionCase):
    """Lead counts follow when Meta says the lead was submitted, and fall
    back to when Odoo created it when that time is missing."""

    def setUp(self):
        super().setUp()
        self.may = datetime(2026, 5, 1)
        self.june = datetime(2026, 6, 1)
        self.july = datetime(2026, 7, 1)

    def _month(self, start, **kw):
        return self._metrics(period_mode='month', date_from=start, **kw)

    def test_backfilled_lead_counts_in_its_submission_month(self):
        """Submitted 20 May, pulled in by a backfill on 3 June."""
        self._mk_lead(datetime(2026, 6, 3), submitted=datetime(2026, 5, 20))
        may, june = self._month(self.may), self._month(self.june)
        self.assertEqual(may['kpis']['new'], 1)
        self.assertEqual(sum(b['count'] for b in may['series']), 1)
        self.assertEqual(june['kpis']['new'], 0)
        self.assertEqual(june['series'], [])

    def test_lead_without_submission_time_counts_by_creation(self):
        self._mk_lead(datetime(2026, 6, 3))
        self.assertEqual(self._month(self.may)['kpis']['new'], 0)
        self.assertEqual(self._month(self.june)['kpis']['new'], 1)

    def test_series_merges_both_bases_and_counts_each_lead_once(self):
        # In the window by submission time, created after it.
        self._mk_lead(datetime(2026, 7, 3), submitted=datetime(2026, 5, 20))
        # No submission time, created in May.
        self._mk_lead(datetime(2026, 5, 10))
        # Both dates in May: still one lead.
        self._mk_lead(datetime(2026, 5, 6), submitted=datetime(2026, 5, 5))
        # No submission time, created in June.
        self._mk_lead(datetime(2026, 6, 10))
        # Created in the window but submitted before it: left out.
        self._mk_lead(datetime(2026, 4, 2), submitted=datetime(2026, 3, 30))
        # No submission time, created after the window: left out.
        self._mk_lead(datetime(2026, 7, 2))

        m = self._metrics(period_mode='custom', date_from=datetime(2026, 4, 1),
                          date_to=self.july)
        self.assertEqual(m['window']['bucket_granularity'], 'month')
        self.assertEqual(m['kpis']['new'], 4)
        self.assertEqual(m['series'], [
            {'bucket': 'May 2026', 'count': 3},
            {'bucket': 'Jun 2026', 'count': 1},
        ])
        self.assertEqual(sum(b['count'] for b in m['series']),
                         m['kpis']['new'])

    def test_new_delta_uses_submission_time(self):
        # Submitted in May, created in June: a May lead.
        self._mk_lead(datetime(2026, 6, 5), submitted=datetime(2026, 5, 15))
        self._mk_lead(datetime(2026, 5, 12))
        self._mk_lead(datetime(2026, 6, 10))
        m = self._month(self.june)
        self.assertEqual(m['kpis']['new'], 1)
        # 1 in June against 2 in May.
        self.assertAlmostEqual(m['deltas']['new'], -0.5, places=6)

    def test_rates_campaigns_and_drilldown_use_the_same_window(self):
        self._mk_lead(datetime(2026, 6, 3), submitted=datetime(2026, 5, 20),
                      campaign='Late', lead_type='opportunity', won=True,
                      extra={'meta_ad_name': 'LateAd',
                             'meta_adset_name': 'LateSet'})
        self._mk_lead(datetime(2026, 6, 5), campaign='Now',
                      extra={'meta_ad_name': 'NowAd',
                             'meta_adset_name': 'NowSet'})
        with mock.patch.object(_dash, '_MIN_RANK_VOLUME', 1):
            may, june = self._month(self.may), self._month(self.june)

        self.assertEqual(may['conversion'], {
            'converted_rate': None, 'won_rate': 1.0,
            'created_as_opportunity': 1})
        self.assertEqual(_names(may['campaigns']), {'Late'})
        self.assertEqual(_names(may['conversion_ranking']), {'Late'})
        self.assertEqual(_names(may['drilldown']['ads']), {'LateAd'})
        self.assertEqual(_names(may['drilldown']['adsets']), {'LateSet'})

        self.assertEqual(june['conversion'], {
            'converted_rate': 0.0, 'won_rate': None,
            'created_as_opportunity': 0})
        self.assertEqual(_names(june['campaigns']), {'Now'})
        self.assertEqual(_names(june['drilldown']['ads']), {'NowAd'})
        self.assertEqual(_names(june['drilldown']['adsets']), {'NowSet'})


def _counts(rows):
    return {r['name']: r['count'] for r in rows}


ROW_KEYS = {'name', 'count', 'converted_rate', 'won_rate'}


@tagged('post_install', '-at_install')
class TestDashboardMetaIdGrouping(DashboardFixtureMixin, TransactionCase):
    """Campaigns, ad sets and ads are one row per Meta ID, labelled with the
    newest name seen for that ID. Different IDs that share a name get the
    end of their ID added so they can be told apart."""

    def setUp(self):
        super().setUp()
        self.june_day = datetime(2026, 6, 10)

    def _camp(self, name, gid, n=1, when=None, **kw):
        for _i in range(n):
            self._mk_lead(when or self.june_day, campaign=name,
                          extra={'meta_campaign_id': gid}, **kw)

    def _june(self):
        return self._metrics(period_mode='month', date_from=self.win_from)

    def test_two_ids_sharing_a_name_get_suffixes(self):
        self._camp('Spring Sale', '120210000004821', n=3)
        self._camp('Spring Sale', '120210000009037', n=2)
        self._camp('Unique', '120210000001111')
        self.assertEqual(_counts(self._june()['campaigns']), {
            'Spring Sale \u00b7 \u20264821': 3,
            'Spring Sale \u00b7 \u20269037': 2,
            'Unique': 1,
        })

    def test_renamed_campaign_is_one_row_with_the_latest_name(self):
        self._camp('Old Name', 'CMP_RENAMED', n=2, when=datetime(2026, 5, 10))
        self._camp('New Name', 'CMP_RENAMED')
        # A newer lead without a name doesn't blank the label.
        self._camp('', 'CMP_RENAMED')
        self.assertEqual(_counts(self._june()['campaigns']), {'New Name': 2})
        may = self._metrics(period_mode='month', date_from=self.prior_from)
        self.assertEqual(_counts(may['campaigns']), {'New Name': 2})

    def test_id_without_a_name_is_unnamed_with_suffix(self):
        self._camp(False, '120210000005555')
        self._camp('', '120210000005555')
        self.assertEqual(_counts(self._june()['campaigns']),
                         {'Unnamed \u00b7 \u20265555': 2})

    def test_leads_without_an_id_stay_one_unattributed_row(self):
        self._camp('Has a name', False)
        self._camp('Also named', '')
        self._camp(False, False)
        self.assertEqual(_counts(self._june()['campaigns']),
                         {'Unattributed': 3})

    def test_suffix_widens_while_ids_still_collide(self):
        # Same last 4, different last 6.
        self._camp('Twin', 'P_771234')
        self._camp('Twin', 'Q_881234', n=2)
        # Same last 6: the full ID is shown.
        self._camp('Deep', 'X_9123456', n=3)
        self._camp('Deep', 'Y_9123456', n=4)
        self.assertEqual(_counts(self._june()['campaigns']), {
            'Twin \u00b7 \u2026771234': 1,
            'Twin \u00b7 \u2026881234': 2,
            'Deep \u00b7 \u2026X_9123456': 3,
            'Deep \u00b7 \u2026Y_9123456': 4,
        })

    def test_short_ids_that_collide_show_in_full(self):
        self._camp('Pair', 'A1234')
        self._camp('Pair', 'B1234', n=2)
        self.assertEqual(_counts(self._june()['campaigns']), {
            'Pair \u00b7 \u2026A1234': 1,
            'Pair \u00b7 \u2026B1234': 2,
        })

    def test_minimum_volume_applies_per_id(self):
        # 12 leads under one name, but 6 per campaign: neither ranks.
        self._camp('Popular', 'CMP_POP_0001', n=6)
        self._camp('Popular', 'CMP_POP_0002', n=6)
        self._camp('Big', 'CMP_BIG_0003', n=10)
        m = self._june()
        self.assertEqual(_counts(m['campaigns']), {
            'Popular \u00b7 \u20260001': 6,
            'Popular \u00b7 \u20260002': 6,
            'Big': 10,
        })
        self.assertEqual(_counts(m['conversion_ranking']), {'Big': 10})

    def test_ranking_carries_the_donut_labels(self):
        self._camp('Same', 'CMP_SAME_01', n=2, lead_type='opportunity')
        self._camp('Same', 'CMP_SAME_02')
        with mock.patch.object(_dash, '_MIN_RANK_VOLUME', 1):
            m = self._june()
        self.assertEqual(m['conversion_ranking'], m['campaigns'])
        self.assertEqual(_names(m['conversion_ranking']), {
            'Same \u00b7 \u2026E_01', 'Same \u00b7 \u2026E_02'})

    def test_drilldown_groups_ads_and_adsets_by_id(self):
        def ad(ad_name, ad_id, set_name, set_id):
            self._mk_lead(self.june_day, extra={
                'meta_ad_name': ad_name, 'meta_ad_id': ad_id,
                'meta_adset_name': set_name, 'meta_adset_id': set_id})

        ad('Carousel', 'AD_1110001', 'Set X', 'AS_77')
        ad('Carousel', 'AD_2220002', 'Set X', 'AS_77')
        ad('Carousel', 'AD_2220002', 'Set Y', 'AS_77')
        ad(False, 'AD_3330003', False, False)
        drill = self._june()['drilldown']
        self.assertEqual(_counts(drill['ads']), {
            'Carousel \u00b7 \u20260001': 1,
            'Carousel \u00b7 \u20260002': 2,
            'Unnamed \u00b7 \u20260003': 1,
        })
        self.assertEqual(_counts(drill['adsets']),
                         {'Set Y': 3, 'Unattributed': 1})

    def test_rows_keep_the_four_keys(self):
        self._camp('Spring Sale', '120210000004821', n=2,
                   lead_type='opportunity', won=True)
        self._camp('Spring Sale', '120210000009037')
        self._camp(False, '120210000005555')
        self._camp(False, False)
        with mock.patch.object(_dash, '_MIN_RANK_VOLUME', 1):
            m = self._june()
        rows = (m['campaigns'] + m['conversion_ranking']
                + m['drilldown']['ads'] + m['drilldown']['adsets'])
        self.assertTrue(rows)
        for row in rows:
            self.assertEqual(set(row), ROW_KEYS, row)
        rates = {r['name']: (r['converted_rate'], r['won_rate'])
                 for r in m['campaigns']}
        # Two won opportunities created as such, then one plain lead.
        self.assertEqual(rates['Spring Sale \u00b7 \u20264821'], (None, 1.0))
        self.assertEqual(rates['Spring Sale \u00b7 \u20269037'], (0.0, None))


@tagged('post_install', '-at_install')
class TestDashboardConversionHonesty(DashboardFixtureMixin, TransactionCase):
    """A lead counts as converted only when sales converted it. A Page that
    creates opportunities directly must not make the rate look like every
    lead was converted."""

    def setUp(self):
        super().setUp()
        self.june_day = datetime(2026, 6, 10)

    def _june(self):
        return self._metrics(period_mode='month', date_from=self.win_from,
                             date_to=self.win_to)

    def _convert(self, lead):
        """Convert an open lead the way the CRM button does."""
        lead.convert_opportunity(False)
        self.assertTrue(lead.date_conversion)
        self.assertEqual(lead.type, 'opportunity')
        return lead

    def _row(self, rows, name):
        [row] = [r for r in rows if r['name'] == name]
        return row

    def test_direct_opportunities_do_not_count_as_converted(self):
        # Converted for real, then won.
        real = self._convert(self._mk_lead(self.june_day, campaign='Mixed'))
        real.write({'stage_id': self.won_stage.id})
        # Converted, still open.
        self._mk_lead(self.june_day, campaign='Mixed',
                      lead_type='opportunity', converted=True)
        # Two leads nobody converted.
        for _i in range(2):
            self._mk_lead(self.june_day, campaign='Mixed')
        # Three created as opportunities, one of them won.
        for i in range(3):
            self._mk_lead(self.june_day, campaign='Mixed',
                          lead_type='opportunity', won=(i == 0))

        with mock.patch.object(_dash, '_MIN_RANK_VOLUME', 1):
            m = self._june()

        conv = m['conversion']
        # 2 converted of the 4 that started as leads.
        self.assertAlmostEqual(conv['converted_rate'], 0.5, places=6)
        self.assertEqual(conv['created_as_opportunity'], 3)
        # 2 won of 5 opportunities.
        self.assertAlmostEqual(conv['won_rate'], 0.4, places=6)

        row = self._row(m['conversion_ranking'], 'Mixed')
        self.assertEqual(row['count'], 7)
        self.assertAlmostEqual(row['converted_rate'], 0.5, places=6)
        self.assertAlmostEqual(row['won_rate'], 0.4, places=6)

    def test_real_conversion_stamps_and_counts(self):
        lead = self._mk_lead(self.june_day, campaign='Solo')
        self.assertFalse(lead.date_conversion)
        self._convert(lead)
        conv = self._june()['conversion']
        self.assertEqual(conv['converted_rate'], 1.0)
        self.assertEqual(conv['created_as_opportunity'], 0)
        self.assertEqual(conv['won_rate'], 0.0)

    def test_opportunity_page_lead_is_created_as_opportunity(self):
        self.page.lead_type = 'opportunity'
        with self._patch_graph(fetch_lead=self._fake_lead(id='LGOPP1')):
            lead = self.Ingest.ingest_leadgen(
                self.page, 'LGOPP1', trigger='webhook')
        self.assertEqual(lead.type, 'opportunity')
        self.assertFalse(lead.date_conversion)
        conv = self._june()['conversion']
        self.assertIsNone(conv['converted_rate'])
        self.assertEqual(conv['created_as_opportunity'], 1)

    def test_all_direct_window_shows_no_converted_rate(self):
        for i in range(12):
            self._mk_lead(self.june_day, campaign='Direct',
                          lead_type='opportunity', won=(i < 2))
        # The real minimum: the campaign has 12 Meta leads, so it ranks
        # even though none of them started as a lead.
        m = self._june()
        self.assertIsNone(m['conversion']['converted_rate'])
        self.assertEqual(m['conversion']['created_as_opportunity'], 12)
        row = self._row(m['conversion_ranking'], 'Direct')
        self.assertEqual(row['count'], 12)
        self.assertIsNone(row['converted_rate'])
        self.assertAlmostEqual(row['won_rate'], 2 / 12, places=6)

    def test_no_opportunities_gives_no_won_rate(self):
        for _i in range(3):
            self._mk_lead(self.june_day, campaign='Plain')
        conv = self._june()['conversion']
        self.assertEqual(conv['converted_rate'], 0.0)
        self.assertIsNone(conv['won_rate'])
        self.assertEqual(conv['created_as_opportunity'], 0)

    def test_conversion_payload_keys(self):
        self._mk_lead(self.june_day, campaign='Keys')
        self.assertEqual(set(self._june()['conversion']),
                         {'converted_rate', 'won_rate',
                          'created_as_opportunity'})
