# Meta Lead Ads Integration for Odoo — © 2026 CoreSys Builders. All rights reserved.
# Licensed under the Odoo Proprietary License v1.0 (OPL-1).
# Unauthorized copying, redistribution, or resale of this software, in whole or in
# part, via any medium, is strictly prohibited and constitutes a license violation.
# OPL-1: https://www.odoo.com/documentation/18.0/legal/licenses.html#odoo-apps

"""RED unit tests for the Leads Analytics Dashboard backend aggregation.

Pins the executable contract of the not-yet-implemented
``meta.account.get_dashboard_metrics(date_from=None, date_to=None,
period_mode='month')`` (implemented in Plan 02). These tests MUST fail RED now
(the method does not exist -> ``AttributeError``) and turn GREEN once Plan 02
adds the read-only aggregation method.

What this scaffold locks:
  * period_mode (month/quarter/year/custom) is SEPARATE from the DERIVED
    bucket_granularity (day/week/month/quarter/year). There is NEVER a
    ``create_date:custom``.
  * half-open UTC window: a record exactly at ``from`` is counted, a record
    exactly at ``to`` is NOT. All fixture create_dates are
    written at a fixed 12:00:00 UTC so VPS-local timezone conversion never
    shifts a boundary bucket across a day boundary (C-5 timezone drift).
  * won_rate denominator = opportunities; "won" = a lead on a
    crm.stage with is_won=True, which the fixture seeds EXPLICITLY and moves
    leads onto.
  * min-volume guard N=10 applies to the CONVERSION ranking ONLY; the donut
    volume list keeps ALL campaigns incl. a sub-10 one.
  * NULL vs blank campaign: both meta_campaign_name=False and '' fall back to
    'Unattributed'.
  * sync_recent = GLOBAL latest-5 meta.sync.log rows, INDEPENDENT of the window
    argument (Divergent: sync_recent decision; DASH-04 "last 5").
  * deltas = relative (current - prior)/prior per KPI; None when prior == 0
    (Divergent: deltas formula; DASH-06).

Odoo 18 conventions (memory vps-odoo18-verification-env):
  * @tagged('post_install', '-at_install') on every class.
  * search_count(...), never the removed count= kwarg.
  * assertRaises takes a SINGLE exception class, never a tuple.
  * Seeds go through the existing single create path (the IngestFixtureMixin
    account->page->form chain reused here); NO second lead-creation path.
"""
from datetime import datetime, timedelta

from odoo.tests.common import TransactionCase, tagged
from odoo.fields import Datetime

from .test_ingest import IngestFixtureMixin

# A fixed mid-day UTC anchor so timezone conversion on the VPS can never shift a
# boundary record across a day boundary (C-5). Every seeded create_date is
# written at hour=12.
NOON = 12


class DashboardFixtureMixin(IngestFixtureMixin):
    """Extend the ingest account->page->form chain with a deterministic dashboard
    data set: Meta crm.lead rows across dates / campaigns / stages (incl. a
    seeded is_won stage) plus meta.sync.log rows inside and outside the window.

    Seeds Meta leads via the EXISTING crm.lead create path used by the ingest
    fixture (no second lead-creation path). create_date is written explicitly at
    12:00:00 UTC after creation because the ORM stamps it on insert.
    """

    def setUp(self):
        super().setUp()
        self.Account = self.env['meta.account']
        self.Log = self.env['meta.sync.log']

        # An admin actor for the happy-path get_dashboard_metrics() calls.
        base_internal = self.env.ref('base.group_user')
        self.admin_group = self.env.ref('meta_lead_ads.group_meta_admin')
        self.dash_admin = self.env['res.users'].create({
            'name': 'Dash Admin', 'login': 'dash_admin_metrics',
            'groups_id': [(6, 0, [base_internal.id, self.admin_group.id])]})

        # Explicitly seed a WON stage (is_won=True) and an open stage. Do NOT
        # assume a pre-existing won stage exists -- this proves the is_won
        # field/semantics on Odoo 18 CE before Plan 02 relies on it.
        self.won_stage = self.env['crm.stage'].create(
            {'name': 'Dash Won', 'is_won': True})
        self.open_stage = self.env['crm.stage'].search(
            [('is_won', '=', False)], limit=1)
        if not self.open_stage:
            self.open_stage = self.env['crm.stage'].create(
                {'name': 'Dash New', 'is_won': False})

        # The in-window month under test: June 2026 (UTC, half-open).
        self.win_from = datetime(2026, 6, 1, 0, 0, 0)
        self.win_to = datetime(2026, 7, 1, 0, 0, 0)
        # The equal-length PRIOR month: May 2026.
        self.prior_from = datetime(2026, 5, 1, 0, 0, 0)

        self._seed = {'n': 0}

    # ---- single-create-path seed helpers --------------------------------

    def _mk_lead(self, when, *, campaign=None, lead_type='lead', won=False,
                 email=None):
        """Create one Meta crm.lead via the standard create path and FORCE its
        create_date to ``when`` at 12:00:00 UTC.

        ``when`` is a date or datetime; the hour is overridden to noon UTC so the
        stored UTC value never drifts across a day boundary under VPS-local tz
        conversion. Returns the lead recordset.
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
        if email is not None:
            vals['email_from'] = email
        lead = self.Lead.create(vals)
        # create_date is stamped by the ORM on insert; force the fixed UTC value.
        lead.flush_recordset()
        self.env.cr.execute(
            "UPDATE crm_lead SET create_date = %s WHERE id = %s",
            (Datetime.to_string(stamp), lead.id))
        lead.invalidate_recordset(['create_date'])
        return lead

    def _mk_log(self, when, status, *, trigger=None, leadgen_id=None):
        """Create one meta.sync.log row with create_date forced to ``when`` at
        noon UTC. Returns the log recordset."""
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
        """Seed the canonical June-2026 scenario used across the metric tests.

        Boundary discipline (half-open [from, to)):
          * one lead exactly at win_from (00:00 of June 1 -> stored at noon) IS
            in-window;
          * one lead just before win_to (June 30) IS in-window;
          * one lead exactly at win_to (July 1) is OUTSIDE (excluded by ``< to``).
        Plus out-of-window May rows for the prior-period delta math, two real
        campaigns, a False-named and a ''-named campaign (both -> Unattributed),
        a sub-10 campaign and a >=10 campaign, opportunities, won leads, and
        sync-log rows of mixed status/trigger inside and outside the window.
        """
        # -- exact boundary records (half-open) --
        self.at_from = self._mk_lead(self.win_from, campaign='Alpha')
        self.before_to = self._mk_lead(
            self.win_to - timedelta(days=1), campaign='Alpha')
        self.at_to = self._mk_lead(self.win_to, campaign='Alpha')  # EXCLUDED

        # -- a >=10 campaign 'Beta' (12 leads), some opp, some won --
        for i in range(12):
            self._mk_lead(
                datetime(2026, 6, 10), campaign='Beta',
                lead_type='opportunity' if i < 6 else 'lead',
                won=(i < 3))   # 3 won, all 6 opps -> won_rate within Beta = 3/6

        # -- a sub-10 campaign 'Gamma' (4 leads) to exercise the min-vol guard --
        for i in range(4):
            self._mk_lead(datetime(2026, 6, 12), campaign='Gamma',
                          lead_type='opportunity' if i < 2 else 'lead')

        # -- NULL vs blank campaign -> both 'Unattributed' --
        self._mk_lead(datetime(2026, 6, 15), campaign=False)
        self._mk_lead(datetime(2026, 6, 15), campaign='')

        # -- prior-period (May) Meta leads so deltas['new'] has a prior --
        self.prior_leads = [
            self._mk_lead(datetime(2026, 5, 10), campaign='Alpha')
            for _ in range(2)]

        # -- sync-log rows: in-window (June) mixed status + trigger --
        self._mk_log(datetime(2026, 6, 5), 'success', trigger='webhook')
        self._mk_log(datetime(2026, 6, 6), 'skipped_idempotent', trigger='cron')
        self._mk_log(datetime(2026, 6, 7), 'failed', trigger='webhook')
        self._mk_log(datetime(2026, 6, 8), 'pending', trigger='manual')
        # -- out-of-window (May) log rows (must not count toward June KPIs) --
        self._mk_log(datetime(2026, 5, 20), 'success', trigger='cron')
        self._mk_log(datetime(2026, 5, 21), 'failed', trigger='webhook')

    def _metrics(self, **kw):
        """Call the (not-yet-existing) method as the dash admin."""
        return self.Account.with_user(self.dash_admin).get_dashboard_metrics(
            **kw)


@tagged('post_install', '-at_install')
class TestDashboardMetrics(DashboardFixtureMixin, TransactionCase):
    """RED contract for get_dashboard_metrics: window/bucket derivation, series,
    conversion (incl. seeded is_won), ranking min-volume guard, KPIs, deltas,
    and the GLOBAL latest-5 sync_recent. Fails RED until Plan 02 implements the
    method."""

    # ---- DASH-01: leads-over-time series + derived bucket ----------------

    def test_series_buckets_meta_leads_by_derived_granularity(self):
        """series counts Meta leads (meta_leadgen_id != False) over the
        Meta+window domain, bucketed by the DERIVED bucket_granularity -- which
        is a real date granularity, NEVER 'custom'."""
        self._seed_window()
        m = self._metrics(period_mode='month',
                          date_from=self.win_from, date_to=self.win_to)
        self.assertIn('series', m)
        self.assertIn('window', m)
        self.assertNotEqual(m['window']['bucket_granularity'], 'custom')
        self.assertIn(
            m['window']['bucket_granularity'],
            ('day', 'week', 'month', 'quarter', 'year'))
        # Every series bucket count is an int and the total equals the in-window
        # Meta-lead count (search_count, never count=).
        in_window = self.Lead.search_count([
            ('meta_leadgen_id', '!=', False),
            ('create_date', '>=', self.win_from),
            ('create_date', '<', self.win_to)])
        self.assertEqual(sum(b['count'] for b in m['series']), in_window)

    # ---- DASH-06: period_mode vs derived bucket_granularity --------------

    def test_named_period_modes_derive_bucket(self):
        """period_mode passes through unchanged; bucket_granularity is derived
        and is a valid date granularity for month/quarter/year."""
        self._seed_window()
        for mode in ('month', 'quarter', 'year'):
            m = self._metrics(period_mode=mode)
            self.assertEqual(m['window']['period_mode'], mode)
            self.assertIn(
                m['window']['bucket_granularity'],
                ('day', 'week', 'month', 'quarter', 'year'))
            self.assertNotEqual(m['window']['bucket_granularity'], 'custom')

    def test_custom_short_range_buckets_by_day(self):
        """A custom <=31-day From/To range derives bucket_granularity='day' and
        period_mode='custom' -- and NEVER yields create_date:custom."""
        self._seed_window()
        c_from = datetime(2026, 6, 1, 0, 0, 0)
        c_to = datetime(2026, 6, 15, 0, 0, 0)   # 14 days span
        m = self._metrics(period_mode='custom', date_from=c_from, date_to=c_to)
        self.assertEqual(m['window']['period_mode'], 'custom')
        self.assertEqual(m['window']['bucket_granularity'], 'day')

    def test_window_is_half_open(self):
        """The half-open window counts the at-`from` record and EXCLUDES the
        at-`to` record."""
        self._seed_window()
        m = self._metrics(period_mode='custom',
                          date_from=self.win_from, date_to=self.win_to)
        in_window = self.Lead.search_count([
            ('meta_leadgen_id', '!=', False),
            ('create_date', '>=', self.win_from),
            ('create_date', '<', self.win_to)])
        # at_from is counted; at_to is not -> the method's New KPI matches the
        # half-open search_count.
        self.assertEqual(m['kpis']['new'], in_window)
        # The at-`to` lead exists but must be outside the half-open window.
        self.assertEqual(self.Lead.search_count([
            ('id', '=', self.at_to.id),
            ('create_date', '>=', self.win_from),
            ('create_date', '<', self.win_to)]), 0)

    # ---- DASH-06: relative deltas incl. None-when-no-prior ----------------

    def test_deltas_relative_when_prior_has_data(self):
        """deltas['new'] = (current - prior)/prior for a window whose equal-length
        prior period has Meta leads."""
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
        self.assertTrue(prior)   # the fixture seeded May leads
        self.assertAlmostEqual(
            m['deltas']['new'], (current - prior) / prior, places=6)

    def test_delta_none_when_no_prior(self):
        """deltas['new'] is None for a window whose equal-length prior period has
        zero Meta leads (hide-delta-when-no-prior)."""
        self._seed_window()
        # March 2026: its prior (Feb) has zero seeded Meta leads.
        f = datetime(2026, 3, 1, 0, 0, 0)
        t = datetime(2026, 4, 1, 0, 0, 0)
        m = self._metrics(period_mode='month', date_from=f, date_to=t)
        self.assertIsNone(m['deltas']['new'])

    def test_zero_total_window_does_not_raise(self):
        """A window with zero Meta leads returns new==0, an empty/zero series,
        conversion rates 0.0, and does not raise."""
        self._seed_window()
        f = datetime(2026, 1, 1, 0, 0, 0)
        t = datetime(2026, 2, 1, 0, 0, 0)
        m = self._metrics(period_mode='month', date_from=f, date_to=t)
        self.assertEqual(m['kpis']['new'], 0)
        self.assertEqual(sum(b['count'] for b in m['series']), 0)
        self.assertEqual(m['conversion']['opportunity_rate'], 0.0)
        self.assertEqual(m['conversion']['won_rate'], 0.0)

    # ---- DASH-08: KPIs ---------------------------------------------------

    def test_kpis_count_window_sources(self):
        """kpis.new = Meta leads in window; kpis.synced = sync-log
        success+skipped_idempotent in window; kpis.failed = sync-log failed in
        window."""
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

    # ---- DASH-02: conversion incl. seeded is_won --------------------------

    def test_conversion_rates_with_opportunity_denominator(self):
        """opportunity_rate = opp/total; won_rate = won/opp (0.0 when opp==0),
        with 'won' counted via the seeded is_won stage."""
        self._seed_window()
        m = self._metrics(period_mode='month',
                          date_from=self.win_from, date_to=self.win_to)
        win = [('meta_leadgen_id', '!=', False),
               ('create_date', '>=', self.win_from),
               ('create_date', '<', self.win_to)]
        total = self.Lead.search_count(win)
        opp = self.Lead.search_count(win + [('type', '=', 'opportunity')])
        won = self.Lead.search_count(win + [('stage_id.is_won', '=', True)])
        self.assertTrue(opp)   # fixture seeded opportunities
        self.assertTrue(won)   # fixture seeded won-stage leads
        self.assertAlmostEqual(
            m['conversion']['opportunity_rate'], opp / total, places=6)
        self.assertAlmostEqual(
            m['conversion']['won_rate'], won / opp, places=6)

    # ---- DASH-03: campaigns donut + min-volume-guarded ranking -----------

    def test_campaigns_donut_includes_all_ranking_guards_low_volume(self):
        """The donut volume list groups by meta_campaign_name (both False and ''
        -> 'Unattributed') and INCLUDES the sub-10 'Gamma' campaign; the
        min-volume-guarded conversion ranking EXCLUDES Gamma."""
        self._seed_window()
        m = self._metrics(period_mode='month',
                          date_from=self.win_from, date_to=self.win_to)
        donut_names = {c['name'] for c in m['campaigns']}
        # Unattributed present (collapsing False and '').
        self.assertIn('Unattributed', donut_names)
        # Sub-10 campaign is in the full volume list.
        self.assertIn('Gamma', donut_names)
        # Beta (>=10) is rankable; Gamma (<10) is excluded from the ranking.
        ranked = m['campaigns_ranked'] if 'campaigns_ranked' in m \
            else m['conversion_ranking']
        ranked_names = {c['name'] for c in ranked}
        self.assertIn('Beta', ranked_names)
        self.assertNotIn('Gamma', ranked_names)

    def test_ranking_table_consumes_guarded_list_not_full_campaigns(self):
        """Frontend wiring tripwire (DASH-03 regression guard).

        The backend ships BOTH `campaigns` (full volume list, sub-10 included)
        and `conversion_ranking` (the N=10-guarded list). A prior defect had the
        OWL ranking table render `campaigns` while a footnote promised the guard,
        so sub-10 campaigns surfaced a misleading per-row conversion rate.

        There is no JS test runner wired in this module (no static/tests bundle),
        so this asserts STRUCTURALLY against the dashboard source: the ranking
        getter must consume `conversion_ranking`, while the donut keeps the full
        `campaigns` list. If someone rewires the table back onto `campaigns`,
        this fails loudly without needing a browser.
        """
        import os
        js_path = os.path.join(
            os.path.dirname(os.path.dirname(__file__)),
            'static', 'src', 'dashboard', 'dashboard.js')
        with open(js_path, encoding='utf-8') as fh:
            src = fh.read()
        # The ranking table must reference the guarded list...
        self.assertIn(
            'conversion_ranking', src,
            "dashboard.js must consume the min-volume-guarded "
            "`conversion_ranking` for the ranking table (DASH-03).")
        # ...and the donut must still reference the full campaign volume list.
        self.assertIn(
            'data.campaigns', src,
            "dashboard.js donut must keep the full `campaigns` volume list.")

        def _getter_body(name):
            """Source slice of a method's body (anchored on its DEFINITION, not a
            call site) up to the next method."""
            sig = name + '() {'
            start = src.index(sig)
            # Bound the slice to this method only (next `_` method or `get `).
            rest = src[start + len(sig):]
            nxt = min(
                (i for i in (rest.find('\n    _'), rest.find('\n    get '))
                 if i != -1),
                default=len(rest))
            return rest[:nxt]

        # The ranking getter binds to the GUARDED list, never the full campaigns.
        ranking_body = _getter_body('_rankingRows')
        self.assertIn(
            'conversion_ranking', ranking_body,
            "the ranking getter must return `conversion_ranking` (DASH-03).")
        self.assertNotIn(
            'data.campaigns', ranking_body,
            "the ranking getter must NOT fall back to the full `campaigns` "
            "list — that reintroduces the DASH-03 min-volume defect.")
        # The donut getter keeps the FULL campaigns volume list.
        self.assertIn(
            'data.campaigns', _getter_body('_donutRows'),
            "the donut getter must keep the full `campaigns` volume list.")

    # ---- DASH-04: GLOBAL latest-5 sync_recent -----------------------------

    def test_sync_recent_is_global_latest_five(self):
        """sync_recent is the 5 newest meta.sync.log rows OVERALL, independent of
        the window argument: even a narrow window that excludes most rows returns
        the global latest-5, newest-first, exposing ONLY status / meta_leadgen_id
        / create_date (never raw_payload)."""
        self._seed_window()
        # A NARROW window that excludes nearly every sync-log row.
        narrow_from = datetime(2026, 6, 8, 0, 0, 0)
        narrow_to = datetime(2026, 6, 9, 0, 0, 0)
        m = self._metrics(period_mode='custom',
                          date_from=narrow_from, date_to=narrow_to)
        recent = m['sync_recent']
        total_logs = self.Log.search_count([])
        self.assertEqual(len(recent), min(5, total_logs))
        # newest-first ordering (create_date desc).
        dates = [r['create_date'] for r in recent]
        self.assertEqual(dates, sorted(dates, reverse=True))
        # window-independence: the global newest row is present even though it is
        # outside the narrow window.
        newest = self.Log.search([], order='create_date desc', limit=1)
        self.assertIn(newest.meta_leadgen_id,
                      [r['meta_leadgen_id'] for r in recent])
        # no raw_payload key leaks on any row.
        for r in recent:
            self.assertNotIn('raw_payload', r)
