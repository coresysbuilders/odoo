# Meta Lead Ads Integration for Odoo — © 2026 CoreSys Builders. All rights reserved.
# Licensed under the Odoo Proprietary License v1.0 (OPL-1).
# Unauthorized copying, redistribution, or resale of this software, in whole or in
# part, via any medium, is strictly prohibited and constitutes a license violation.
# OPL-1: https://www.odoo.com/documentation/18.0/legal/licenses.html#odoo-apps

"""Tests for the cron backfill and reconcile sweeps.

``TestMetaBackfill`` drives ``meta.lead.form._cron_backfill`` /
``_sweep_form`` with ``meta.graph.client._iter_paged`` and ``ingest_leadgen``
patched on their class (``mock.patch.object`` can't patch a recordset). It
covers ordering, cursor handling, scope, lookback, per-form isolation, and the
same-second and malformed-timestamp edge cases.

``TestBackfillIntegrity`` runs the real ingest and fakes only the Graph reads:
the listing through ``_iter_paged``, or through ``_request`` where the real
pager has to run. A shared fake clock drives the time budget. It covers
bad-lead isolation and the failure cut-off, the cursor and catch-up slices,
form rotation under the budget, Meta's pauses and rejected tokens, the daily
``_cron_reconcile`` and its place in the window, and removed leads not coming
back.

No HTTP call is made.
"""
import json
from contextlib import ExitStack
from datetime import datetime, timedelta, timezone
from unittest import mock

from psycopg2 import errorcodes
from psycopg2.errors import (DeadlockDetected, LockNotAvailable,
                             SerializationFailure)

from odoo import fields
from odoo.exceptions import AccessError
from odoo.tests.common import TransactionCase, tagged
from odoo.tools import mute_logger

from odoo.addons.meta_lead_ads.models import (
    const, meta_graph_client, meta_lead_form, meta_lead_ingest)
from odoo.addons.meta_lead_ads.models.exceptions import (
    MetaAuthError, MetaIngestRaceError, MetaPermanentError,
    MetaRateLimitError, MetaTransientError)
from odoo.addons.meta_lead_ads.models.meta_lead_form import _meta_error_summary
from odoo.addons.meta_lead_ads.models.meta_lead_ingest import parse_meta_time
from odoo.addons.meta_lead_ads.models.meta_sync_log import _is_db_contention
from odoo.addons.meta_lead_ads.models.meta_webhook_event import APP_PAUSE_PARAM

from .test_ingest import IngestFixtureMixin
from .test_multi_company import MultiCompanyFixtureMixin


class _PgBusy(Exception):
    """A plain exception carrying a lock SQLSTATE, as some psycopg2 builds
    raise instead of the specific error class."""
    pgcode = errorcodes.LOCK_NOT_AVAILABLE


def _ts(created_time):
    """Convert a Meta ``created_time`` to the naive UTC string the cursor stores."""
    return fields.Datetime.to_string(parse_meta_time(created_time))


def _filters(params):
    filtering = params.get('filtering') or []
    if isinstance(filtering, str):
        filtering = json.loads(filtering)
    return [f for f in filtering if isinstance(f, dict)]


def _filter_floor(params):
    """Return the lower time bound the sweep sent, as epoch seconds."""
    for key in ('since', 'time_created'):
        if key in params:
            return int(params[key])
    # Read the GREATER_THAN entry itself: once an upper bound is sent too,
    # the last entry in the list is not the floor.
    for filt in _filters(params):
        if filt.get('operator') == 'GREATER_THAN':
            return int(filt['value'])
    return None


def _filter_ceiling(params):
    """Return the upper time bound the sweep sent, or None."""
    for filt in _filters(params):
        if filt.get('operator') == 'LESS_THAN':
            return int(filt['value'])
    return None


def _epoch(created_time):
    parsed = parse_meta_time(created_time)
    if parsed is None:
        return None
    return int(parsed.replace(tzinfo=timezone.utc).timestamp())


class BackfillFixtureMixin:
    """Account, page and form fixtures plus the classes to patch."""

    def setUp(self):
        super().setUp()
        self.account = self.env['meta.account'].create({
            'name': 'Acct', 'account_id': 'ACC1',
            'app_id': 'app_test', 'app_secret': 'secret_test',
            'access_token': 'tok_acct',
        })
        self.page = self.env['meta.page'].create({
            'name': 'Page', 'page_id': 'PG1',
            'access_token': 'tok_test', 'account_id': self.account.id,
        })
        self.form = self.env['meta.lead.form'].create({
            'name': 'Contact Us', 'form_id': 'F1', 'page_id': self.page.id,
        })
        self.Form = self.env['meta.lead.form']
        self.Client = self.env['meta.graph.client']
        self.Ingest = self.env['meta.lead.ingest']
        self.Lead = self.env['crm.lead']
        self.Log = self.env['meta.sync.log']
        self.ClientClass = type(self.Client)
        self.IngestClass = type(self.Ingest)

    def _lead_payload(self, leadgen_id, created_time):
        """Return a lead item as _iter_paged yields it."""
        return {'id': leadgen_id, 'created_time': created_time}

    def _fake_lead(self):
        return self.Lead.create({'name': 'BF Lead', 'type': 'lead'})


@tagged('post_install', '-at_install')
class TestMetaBackfill(BackfillFixtureMixin, TransactionCase):
    """Cron backfill sweep."""

    def test_sweep_oldest_first(self):
        """Leads returned newest first are ingested oldest first."""
        newest_first = [
            self._lead_payload('LG_C', '2026-06-03T10:00:00+0000'),
            self._lead_payload('LG_B', '2026-06-02T10:00:00+0000'),
            self._lead_payload('LG_A', '2026-06-01T10:00:00+0000'),
        ]
        with mock.patch.object(self.ClientClass, '_iter_paged',
                               return_value=iter(newest_first)), \
             mock.patch.object(self.IngestClass, 'ingest_leadgen',
                               return_value=self._fake_lead()) as m_ingest:
            self.Form._cron_backfill()
        called_ids = [c.args[1] for c in m_ingest.call_args_list]
        self.assertEqual(called_ids, ['LG_A', 'LG_B', 'LG_C'])

    def test_cursor_advance_per_lead(self):
        """The cursor ends at the newest lead's created_time in naive UTC."""
        payload = [
            self._lead_payload('LG_2', '2026-06-02T10:00:00+0000'),
            self._lead_payload('LG_1', '2026-06-01T10:00:00+0000'),
        ]
        with mock.patch.object(self.ClientClass, '_iter_paged',
                               return_value=iter(payload)), \
             mock.patch.object(self.IngestClass, 'ingest_leadgen',
                               return_value=self._fake_lead()):
            self.Form._cron_backfill()
        self.form.invalidate_recordset(['last_synced_time'])
        self.assertEqual(
            fields.Datetime.to_string(self.form.last_synced_time),
            _ts('2026-06-02T10:00:00+0000'))
        self.assertGreater(self.form.last_synced_time,
                           fields.Datetime.from_string(
                               _ts('2026-06-01T10:00:00+0000')))

    def test_newest_first_input_reversed(self):
        """A reverse-chronological page is processed oldest first.

        Otherwise a crash mid-sweep could leave an older lead behind a cursor
        already set from a newer one.
        """
        reverse_chrono = [
            self._lead_payload('LG_NEW', '2026-06-05T12:00:00+0000'),
            self._lead_payload('LG_MID', '2026-06-04T12:00:00+0000'),
            self._lead_payload('LG_OLD', '2026-06-03T12:00:00+0000'),
        ]
        with mock.patch.object(self.ClientClass, '_iter_paged',
                               return_value=iter(reverse_chrono)), \
             mock.patch.object(self.IngestClass, 'ingest_leadgen',
                               return_value=self._fake_lead()) as m_ingest:
            self.Form._cron_backfill()
        called_ids = [c.args[1] for c in m_ingest.call_args_list]
        self.assertEqual(called_ids, ['LG_OLD', 'LG_MID', 'LG_NEW'])

    def test_scope_active_sync_enabled(self):
        """Only active forms with sync enabled are swept."""
        self.env['meta.lead.form'].create({
            'name': 'Disabled', 'form_id': 'F_OFF', 'page_id': self.page.id,
            'sync_enabled': False})
        self.env['meta.lead.form'].create({
            'name': 'Archived', 'form_id': 'F_ARC', 'page_id': self.page.id,
            'active': False})

        def _per_form(token, path, params=None, app_secret=None, status=None, **kw):
            # The lead id tells the assertion which form's path was queried.
            if 'F_OFF' in (path or '') or 'F_ARC' in (path or ''):
                return iter([self._lead_payload('LG_SKIP', '2026-06-01T00:00:00+0000')])
            return iter([self._lead_payload('LG_F1', '2026-06-01T00:00:00+0000')])

        with mock.patch.object(self.ClientClass, '_iter_paged',
                               side_effect=_per_form), \
             mock.patch.object(self.IngestClass, 'ingest_leadgen',
                               return_value=self._fake_lead()) as m_ingest:
            self.Form._cron_backfill()
        called_ids = [c.args[1] for c in m_ingest.call_args_list]
        self.assertIn('LG_F1', called_ids)
        self.assertNotIn('LG_SKIP', called_ids)

    def test_lookback_default(self):
        """A form never synced before starts about 30 days back."""
        captured = {}

        def _capture(token, path, params=None, app_secret=None, status=None, **kw):
            captured['params'] = dict(params or {})
            return iter([])

        with mock.patch.object(self.ClientClass, '_iter_paged',
                               side_effect=_capture), \
             mock.patch.object(self.IngestClass, 'ingest_leadgen',
                               return_value=self._fake_lead()):
            self.Form._cron_backfill()
        params = captured.get('params', {})
        self.assertIsInstance(params.get('filtering'), str)
        floor = _filter_floor(params)
        self.assertIsNotNone(floor, 'sweep must pass a since/time_created floor')
        now = int(datetime.now(timezone.utc).timestamp())
        thirty_days = 30 * 24 * 3600
        # Allow two days either way in case the default is configured differently.
        self.assertLess(abs((now - floor) - thirty_days), 2 * 24 * 3600)

    def test_per_form_isolation(self):
        """A Graph error on one form doesn't stop the other forms."""
        self.env['meta.lead.form'].create({
            'name': 'Sibling', 'form_id': 'F_SIB', 'page_id': self.page.id})

        def _maybe_raise(token, path, params=None, app_secret=None,
                         status=None, **kw):
            if 'F1' in (path or ''):
                raise RuntimeError('graph boom for F1')
            return iter([self._lead_payload('LG_SIB', '2026-06-01T00:00:00+0000')])

        with mock.patch.object(self.ClientClass, '_iter_paged',
                               side_effect=_maybe_raise), \
             mock.patch.object(self.IngestClass, 'ingest_leadgen',
                               return_value=self._fake_lead()) as m_ingest:
            self.Form._cron_backfill()
        called_ids = [c.args[1] for c in m_ingest.call_args_list]
        self.assertIn('LG_SIB', called_ids)

    def test_resweep_no_duplicate(self):
        """A second sweep over the same leads only calls ingest_leadgen again.

        Duplicate protection is the UNIQUE constraint on meta_leadgen_id in
        the ingest service; the cron has no create path of its own.
        """
        payload = [self._lead_payload('LG_DUP', '2026-06-01T00:00:00+0000')]
        fake = self._fake_lead()

        with mock.patch.object(self.ClientClass, '_iter_paged',
                               side_effect=lambda *a, **k: iter(list(payload))), \
             mock.patch.object(self.IngestClass, 'ingest_leadgen',
                               return_value=fake) as m_ingest:
            self.Form._cron_backfill()
            first_calls = m_ingest.call_count
            self.Form._cron_backfill()
            second_calls = m_ingest.call_count
        self.assertGreaterEqual(first_calls, 1)
        self.assertGreaterEqual(second_calls, first_calls)

    def test_cursor_no_regression(self):
        """A lead older than the cursor may be re-ingested but never moves the cursor back."""
        seed = fields.Datetime.from_string(_ts('2026-06-10T00:00:00+0000'))
        self.form.last_synced_time = seed
        older = [self._lead_payload('LG_OLD', '2026-06-01T00:00:00+0000')]
        with mock.patch.object(self.ClientClass, '_iter_paged',
                               return_value=iter(older)), \
             mock.patch.object(self.IngestClass, 'ingest_leadgen',
                               return_value=self._fake_lead()):
            self.Form._cron_backfill()
        self.form.invalidate_recordset(['last_synced_time'])
        self.assertGreaterEqual(self.form.last_synced_time, seed)

    def test_same_second_both_ingested(self):
        """Two leads with the same created_time second are both ingested in one sweep."""
        same_second = [
            self._lead_payload('LG_S2', '2026-06-01T08:00:00+0000'),
            self._lead_payload('LG_S1', '2026-06-01T08:00:00+0000'),
        ]
        with mock.patch.object(self.ClientClass, '_iter_paged',
                               return_value=iter(same_second)), \
             mock.patch.object(self.IngestClass, 'ingest_leadgen',
                               return_value=self._fake_lead()) as m_ingest:
            self.Form._cron_backfill()
        called_ids = [c.args[1] for c in m_ingest.call_args_list]
        self.assertEqual(sorted(called_ids), ['LG_S1', 'LG_S2'])
        self.assertEqual(len([i for i in called_ids if i in ('LG_S1', 'LG_S2')]),
                         2)

    def test_malformed_created_time_ingested(self):
        """A lead with a bad created_time is still ingested, gets no failed
        row and does not move the cursor."""
        payload = [
            self._lead_payload('LG_BAD', 'not-an-iso-timestamp'),
            self._lead_payload('LG_GOOD', '2026-06-07T09:00:00+0000'),
        ]
        with mock.patch.object(self.ClientClass, '_iter_paged',
                               return_value=iter(payload)), \
             mock.patch.object(self.IngestClass, 'ingest_leadgen',
                               return_value=self._fake_lead()) as m_ingest:
            self.Form._cron_backfill()
        called_ids = [c.args[1] for c in m_ingest.call_args_list]
        self.assertIn('LG_BAD', called_ids)
        self.assertIn('LG_GOOD', called_ids)
        self.form.invalidate_recordset(['last_synced_time'])
        # Cursor comes from the good lead only.
        self.assertEqual(
            fields.Datetime.to_string(self.form.last_synced_time),
            _ts('2026-06-07T09:00:00+0000'))
        self.assertEqual(
            self.Log.search_count([('status', '=', 'failed')]), 0)

    def test_first_run_cursor_from_false_advances(self):
        """On the first run the cursor moves from False to the lead's time without a TypeError.

        Comparing a datetime with False raises in Python 3, so the sweep has
        to check for an empty cursor first.
        """
        self.form.last_synced_time = False
        payload = [self._lead_payload('LG_FIRST', '2026-06-08T07:00:00+0000')]
        with mock.patch.object(self.ClientClass, '_iter_paged',
                               return_value=iter(payload)), \
             mock.patch.object(self.IngestClass, 'ingest_leadgen',
                               return_value=self._fake_lead()):
            self.Form._cron_backfill()
        self.form.invalidate_recordset(['last_synced_time'])
        self.assertEqual(
            fields.Datetime.to_string(self.form.last_synced_time),
            _ts('2026-06-08T07:00:00+0000'))

    def test_same_second_across_pages(self):
        """Same-second leads on different Graph pages are both ingested.

        _iter_paged reads every page before the sweep sorts, so page breaks
        don't matter.
        """
        # The mock returns the already flattened sequence of both pages.
        across_pages = iter([
            self._lead_payload('LG_P1', '2026-06-01T11:00:00+0000'),  # end of page 1
            self._lead_payload('LG_P2', '2026-06-01T11:00:00+0000'),  # start of page 2
        ])
        with mock.patch.object(self.ClientClass, '_iter_paged',
                               return_value=across_pages), \
             mock.patch.object(self.IngestClass, 'ingest_leadgen',
                               return_value=self._fake_lead()) as m_ingest:
            self.Form._cron_backfill()
        called_ids = [c.args[1] for c in m_ingest.call_args_list]
        self.assertEqual(
            len([i for i in called_ids if i in ('LG_P1', 'LG_P2')]), 2)

    def test_same_second_later_sweep_via_overlap(self):
        """The next sweep starts at cursor minus the overlap, so a late same-second lead is caught.

        Graph can show a lead from second T only after the cursor has already
        reached T. The re-read of the first lead is absorbed by idempotency
        and the cursor stays at T or later.
        """
        t_iso = '2026-06-09T06:00:00+0000'
        t_dt = fields.Datetime.from_string(_ts(t_iso))

        # First sweep: one lead at T.
        with mock.patch.object(self.ClientClass, '_iter_paged',
                               return_value=iter(
                                   [self._lead_payload('LG_T', t_iso)])), \
             mock.patch.object(self.IngestClass, 'ingest_leadgen',
                               return_value=self._fake_lead()):
            self.Form._cron_backfill()
        self.form.invalidate_recordset(['last_synced_time'])
        self.assertEqual(self.form.last_synced_time, t_dt)

        overlap = self.Form._backfill_overlap_seconds()
        captured = {}

        def _capture_floor(token, path, params=None, app_secret=None,
                           status=None, **kw):
            captured['params'] = dict(params or {})
            return iter([self._lead_payload('LG_T_SIB', t_iso)])

        with mock.patch.object(self.ClientClass, '_iter_paged',
                               side_effect=_capture_floor), \
             mock.patch.object(self.IngestClass, 'ingest_leadgen',
                               return_value=self._fake_lead()) as m_ingest:
            self.Form._cron_backfill()
        params = captured.get('params', {})
        self.assertIsInstance(params.get('filtering'), str)
        floor = _filter_floor(params)
        self.assertIsNotNone(floor)
        expected_floor = int(t_dt.replace(tzinfo=timezone.utc).timestamp()) - overlap
        self.assertEqual(floor, expected_floor)
        called_ids = [c.args[1] for c in m_ingest.call_args_list]
        self.assertIn('LG_T_SIB', called_ids)
        self.form.invalidate_recordset(['last_synced_time'])
        self.assertGreaterEqual(self.form.last_synced_time, t_dt)

    # ------------------------------------------------------------------ #
    # Errors in the middle of a form
    # ------------------------------------------------------------------ #
    def test_transient_midform_preserves_cursor(self):
        """A transient error on lead 3 stops the form but keeps the cursor at lead 2.

        Leads 1 to 3 are attempted, lead 4 is not (the loop breaks, it doesn't
        continue), and the next sweep picks up from lead 2. A transient stop
        writes no 'failed' log row.
        """
        t1, t2, t3, t4 = ('2026-06-01T08:00:00+0000',
                          '2026-06-01T09:00:00+0000',
                          '2026-06-01T10:00:00+0000',
                          '2026-06-01T11:00:00+0000')
        payload = [
            self._lead_payload('LG_1', t1),
            self._lead_payload('LG_2', t2),
            self._lead_payload('LG_3', t3),   # raises MetaTransientError
            self._lead_payload('LG_4', t4),   # never reached
        ]

        def _ingest(self_ingest, page, leadgen_id, trigger='cron', raw=None,
                    **kwargs):
            if leadgen_id == 'LG_3':
                raise MetaTransientError('temporary on lead 3')
            return self._fake_lead()

        with mock.patch.object(self.ClientClass, '_iter_paged',
                               return_value=iter(payload)), \
             mock.patch.object(self.IngestClass, 'ingest_leadgen',
                               autospec=True, side_effect=_ingest) as m_ingest:
            # Same per-form savepoint that _cron_backfill wraps around each form.
            with self.env.cr.savepoint():
                self.form._sweep_form()
        self.form.invalidate_recordset(['last_synced_time'])
        # With autospec the first arg is self, so leadgen_id is args[2].
        called_ids = [c.args[2] for c in m_ingest.call_args_list]
        self.assertIn('LG_1', called_ids)
        self.assertIn('LG_2', called_ids)
        self.assertIn('LG_3', called_ids)
        self.assertNotIn('LG_4', called_ids)
        self.assertEqual(
            fields.Datetime.to_string(self.form.last_synced_time), _ts(t2))
        self.assertEqual(
            self.Log.search_count([('status', '=', 'failed')]), 0)

    def test_permanent_midform_quarantines_lead(self):
        """A permanent error on lead 3 records that lead and keeps the others.

        The failed lead has its own row to retry from, so the cursor may move
        past it.
        """
        self.form.last_synced_time = False
        t1, t2, t3 = ('2026-06-02T08:00:00+0000',
                      '2026-06-02T09:00:00+0000',
                      '2026-06-02T10:00:00+0000')
        payload = [
            self._lead_payload('LG_P1', t1),
            self._lead_payload('LG_P2', t2),
            self._lead_payload('LG_P3', t3),
        ]

        def _ingest(self_ingest, page, leadgen_id, trigger='cron', raw=None,
                    **kwargs):
            if leadgen_id == 'LG_P3':
                raise MetaPermanentError('permanent on lead 3')
            return self._fake_lead()

        with mock.patch.object(self.ClientClass, '_iter_paged',
                               return_value=iter(payload)), \
             mock.patch.object(self.IngestClass, 'ingest_leadgen',
                               autospec=True, side_effect=_ingest) as m_ingest:
            # Same per-form savepoint that _cron_backfill wraps around each
            # form; nothing escapes it now.
            with self.env.cr.savepoint():
                complete = self.form._sweep_form()['complete']
        self.assertTrue(complete)
        called_ids = [c.args[2] for c in m_ingest.call_args_list]
        self.assertIn('LG_P1', called_ids)
        self.assertIn('LG_P2', called_ids)
        self.form.invalidate_recordset(['last_synced_time'])
        self.assertEqual(
            fields.Datetime.to_string(self.form.last_synced_time), _ts(t3))
        rows = self.Log.search([('status', '=', 'failed')])
        self.assertEqual(len(rows), 1)
        self.assertEqual(rows.meta_leadgen_id, 'LG_P3')

    def _listing(self, *args, **kwargs):
        return iter([
            self._lead_payload('LG_ARC_1', '2026-06-01T10:00:00+0000'),
            self._lead_payload('LG_ARC_2', '2026-06-02T10:00:00+0000'),
        ])

    def test_sweep_skips_forms_of_archived_pages(self):
        """An archived Page's forms stay active but are not listed."""
        self.page.active = False
        self.assertTrue(self.form.active)
        self.assertTrue(self.form.sync_enabled)
        with mock.patch.object(self.ClientClass, '_iter_paged',
                               side_effect=self._listing) as m_list, \
             mock.patch.object(self.IngestClass, 'ingest_leadgen',
                               return_value=self._fake_lead()) as m_ingest:
            self.Form._cron_backfill()
        m_list.assert_not_called()
        m_ingest.assert_not_called()

    def test_sweep_resumes_after_page_reactivation(self):
        """Once the Page is active again, its forms are swept again."""
        self.page.active = False
        with mock.patch.object(self.ClientClass, '_iter_paged',
                               side_effect=self._listing), \
             mock.patch.object(self.IngestClass, 'ingest_leadgen',
                               return_value=self._fake_lead()) as m_ingest:
            self.Form._cron_backfill()
            self.assertEqual(m_ingest.call_count, 0)
            self.page.active = True
            self.Form._cron_backfill()
        called_ids = [c.args[1] for c in m_ingest.call_args_list]
        self.assertEqual(called_ids, ['LG_ARC_1', 'LG_ARC_2'])


@tagged('post_install', '-at_install')
class TestBackfillIntegrity(MultiCompanyFixtureMixin, IngestFixtureMixin,
                            TransactionCase):
    """Backfill integrity: bad-lead isolation, cursor rules, reconciliation
    and pauses.

    Runs the real ingest with only the Graph reads faked. The fixtures cover
    two Meta accounts, three Pages and four forms, and every listing the
    sweep makes is recorded in ``self.listed`` as (path, params, kwargs).
    """

    def setUp(self):
        super().setUp()
        self.Form = self.env['meta.lead.form']
        self.Log = self.env['meta.sync.log']
        self.Lead = self.env['crm.lead'].with_context(active_test=False)
        # self.Client stays the Graph client class from IngestFixtureMixin,
        # because _patch_graph patches methods on the class.
        self.form_a2 = self.Form.create({
            'name': 'BI Second', 'form_id': 'FBIA2', 'page_id': self.page.id})
        self.form_b = self.Form.create({
            'name': 'BI Company B', 'form_id': 'FBIB',
            'page_id': self.page_b.id})
        self.account2 = self.env['meta.account'].create({
            'name': 'BI Acct 2', 'account_id': 'ACCBI2',
            'app_id': 'app_test', 'app_secret': 'secret_test',
            'access_token': 'tok_acct2', 'company_id': self.co_a.id,
        })
        self.page2 = self.env['meta.page'].create({
            'name': 'BI Page 2', 'page_id': 'PGBI2', 'access_token': 'tok_pg2',
            'account_id': self.account2.id, 'company_id': self.co_a.id,
        })
        self.form2 = self.Form.create({
            'name': 'BI Other Account', 'form_id': 'FBI2',
            'page_id': self.page2.id})
        self.listed = []

    # ---- helpers ---------------------------------------------------------

    def _item(self, leadgen_id, created_time):
        return {'id': leadgen_id, 'created_time': created_time}

    def _payload(self, leadgen_id, created_time='2026-06-14T10:00:00+0000'):
        # Distinct contact per lead, so business dedup never links them.
        return self._fake_lead(
            id=leadgen_id, created_time=created_time, field_data=[
                {'name': 'email',
                 'values': ['%s@bi.test' % leadgen_id.lower()]},
                {'name': 'full_name', 'values': [leadgen_id]},
            ])

    def _path(self, form):
        return '%s/leads' % form.form_id

    def _listing(self, by_path=None, truncated=(), raises=None,
                 deadline_hit=()):
        """Fake the leads listing per path, recording every call."""
        by_path = by_path or {}

        def _paged(token, path, params=None, app_secret=None, status=None,
                   **kw):
            self.listed.append((path, dict(params or {}), dict(kw)))
            if raises and path in raises:
                raise raises[path]
            if status is not None:
                if path in truncated:
                    status['truncated'] = True
                if path in deadline_hit:
                    status['deadline_hit'] = True
            return iter([dict(i) for i in by_path.get(path, [])])
        return mock.patch.object(self.Client, '_iter_paged',
                                 side_effect=_paged)

    def _listing_by_window(self, by_path=None):
        """Like _listing, but only return items inside the sent time bounds.

        Both bounds are strict, as Meta's GREATER_THAN / LESS_THAN are. An
        item whose time does not parse is always returned.
        """
        by_path = by_path or {}

        def _paged(token, path, params=None, app_secret=None, status=None,
                   **kw):
            params = dict(params or {})
            self.listed.append((path, params, dict(kw)))
            lo = _filter_floor(params)
            hi = _filter_ceiling(params)
            out = []
            for item in by_path.get(path, []):
                at = _epoch(item.get('created_time'))
                if at is None or ((lo is None or at > lo)
                                  and (hi is None or at < hi)):
                    out.append(dict(item))
            return iter(out)
        return mock.patch.object(self.Client, '_iter_paged',
                                 side_effect=_paged)

    def _fetch(self, failing=None, exc=None, payloads=None):
        failing = failing or ()
        payloads = payloads or {}

        def _side(page, leadgen_id):
            if leadgen_id in failing:
                raise exc
            if leadgen_id in payloads:
                return payloads[leadgen_id]
            return self._payload(leadgen_id)
        return mock.Mock(side_effect=_side)

    def _sweep(self, by_path=None, fetch=None, window=False, **listing):
        if window:
            fake = self._listing_by_window(by_path)
        else:
            fake = self._listing(by_path, **listing)
        with fake, self._patch_graph(fetch_lead=fetch or self._fetch()):
            self.Form._cron_backfill()
        self.env.invalidate_all()

    def _failed(self, leadgen_id=None, form=None):
        domain = [('status', '=', 'failed'), ('trigger', '=', 'cron')]
        if leadgen_id is not None:
            domain.append(('meta_leadgen_id', '=', leadgen_id))
        if form is not None:
            domain.append(('form_id', '=', form.id))
        return self.Log.sudo().search(domain)

    def _lead(self, leadgen_id):
        return self.Lead.search([('meta_leadgen_id', '=', leadgen_id)])

    def _assert_clean(self, text):
        text = text or ''
        for secret in ('tok_acct', 'tok_acct2', 'tok_test', 'tok_pg2',
                       'tok_b', 'secret_test', 'PG1', 'PGBI2'):
            self.assertNotIn(secret, text)

    def _patch_clock(self, clock):
        """Drive every time-budget check from ``clock[0]``.

        Production reads time.monotonic in all three modules, so one shared
        clock models it.
        """
        stack = ExitStack()
        for module in (meta_lead_form, meta_graph_client, meta_lead_ingest):
            stack.enter_context(mock.patch.object(
                module, '_monotonic', lambda: clock[0]))
        return stack

    def _only_fixture_forms(self):
        """Leave only the four fixture forms in the sweep."""
        mine = self.form | self.form_a2 | self.form_b | self.form2
        (self.Form.search([('active', '=', True)]) - mine).sudo().write(
            {'sync_enabled': False})

    def _stamp(self, stamps):
        for form, stamp in stamps.items():
            form.sudo().write({'last_swept_time': stamp})

    def _listed_paths(self):
        return [path for path, _params, _kw in self.listed]

    # ---- fixtures --------------------------------------------------------

    def test_fixture_forms_are_swept(self):
        """Every fixture form is listed under its own path."""
        self._sweep({})
        paths = {path for path, _params, _kw in self.listed}
        for form in (self.form, self.form_a2, self.form_b, self.form2):
            self.assertIn(self._path(form), paths)

    # ---- cursor state ----------------------------------------------------

    def test_cursor_state_fields(self):
        """A new form is not catching up and has no incomplete streak; both
        fields are readonly and never copied."""
        self.assertFalse(self.form.backfill_slicing)
        self.assertEqual(self.form.backfill_incomplete_count, 0)
        for name in ('backfill_slicing', 'backfill_incomplete_count',
                     'backfill_slice_hours', 'last_synced_time'):
            field = self.Form._fields[name]
            self.assertTrue(field.readonly, name)
            self.assertIs(field.copy, False, name)

    def test_overlap_floor_is_one_second(self):
        """The overlap never drops below one second, so a lead in the
        cursor's own second is read again."""
        icp = self.env['ir.config_parameter'].sudo()
        key = 'meta_lead_ads.backfill_overlap_seconds'
        for raw, expected in (('0', 1), ('-5', 1), ('junk', 60),
                              ('120', 120), ('999999999999', 86400)):
            icp.set_param(key, raw)
            self.assertEqual(self.Form._backfill_overlap_seconds(), expected,
                             raw)

    def test_lookback_days_capped(self):
        """A huge lookback is capped so the date arithmetic can't overflow
        and fail every form."""
        icp = self.env['ir.config_parameter'].sudo()
        key = 'meta_lead_ads.backfill_lookback_days'
        for raw, expected in (('0', 1), ('junk', 30), ('45', 45),
                              ('999999999999', 3650)):
            icp.set_param(key, raw)
            self.assertEqual(self.Form._backfill_lookback_days(), expected,
                             raw)

    def test_backfill_constants(self):
        self.assertEqual(const.BACKFILL_FAILURE_STREAK, 5)
        self.assertEqual(const.BACKFILL_STUCK_SWEEPS, 3)
        self.assertEqual(const.BACKFILL_SLICE_HOURS, 24)
        self.assertEqual(const.BACKFILL_MIN_SLICE_HOURS, 1)
        self.assertEqual(const.BACKFILL_TIME_BUDGET, 45)

    def test_sweep_state_fields_guarded(self):
        """Only the superuser can set a form's backfill position; a Meta
        Admin over RPC gets an AccessError on create and write."""
        self.assertEqual(
            set(self.Form._sweep_state_fields),
            {'last_synced_time', 'backfill_slicing', 'backfill_slice_hours',
             'backfill_incomplete_count', 'last_swept_time',
             'last_reconciled_time', 'reconcile_resume_time',
             'reconcile_slice_hours', 'reconcile_pass_end'})
        values = {
            'last_synced_time': datetime(2026, 6, 1),
            'backfill_slicing': True,
            'backfill_slice_hours': 6,
            'backfill_incomplete_count': 3,
            'last_swept_time': datetime(2026, 6, 2),
            'last_reconciled_time': datetime(2026, 6, 3),
            'reconcile_resume_time': datetime(2026, 6, 1),
            'reconcile_slice_hours': 3,
            'reconcile_pass_end': datetime(2026, 6, 4),
        }
        form_as_admin = self.form.with_user(self.admin_a)
        Form_as_admin = self.Form.with_user(self.admin_a)
        for name in self.Form._sweep_state_fields:
            value = values[name]
            with self.subTest(field=name):
                with self.assertRaises(AccessError):
                    form_as_admin.write({name: value})
                with self.assertRaises(AccessError):
                    Form_as_admin.create({
                        'name': 'Guard', 'form_id': 'FBIGUARD1',
                        'page_id': self.page.id, name: value})
        # Ordinary edits still work for the admin.
        form_as_admin.write({'name': 'renamed'})
        self.assertEqual(self.form.name, 'renamed')
        created = Form_as_admin.create({
            'name': 'Guard', 'form_id': 'FBIGUARD1', 'page_id': self.page.id})
        self.assertTrue(created)
        # The superuser (the sweep) can set them.
        self.form.write(values)
        self.form.invalidate_recordset()
        self.assertEqual(self.form.last_synced_time, datetime(2026, 6, 1))
        self.assertTrue(self.form.backfill_slicing)
        self.assertEqual(self.form.backfill_slice_hours, 6)
        self.assertEqual(self.form.backfill_incomplete_count, 3)
        self.assertEqual(self.form.last_swept_time, datetime(2026, 6, 2))
        self.assertEqual(self.form.last_reconciled_time, datetime(2026, 6, 3))
        self.assertEqual(self.form.reconcile_resume_time, datetime(2026, 6, 1))
        self.assertEqual(self.form.reconcile_slice_hours, 3)
        self.assertEqual(self.form.reconcile_pass_end, datetime(2026, 6, 4))

    # ---- helpers of the sweep --------------------------------------------

    def test_is_db_contention_helper(self):
        self.assertTrue(_is_db_contention(SerializationFailure()))
        self.assertTrue(_is_db_contention(LockNotAvailable()))
        self.assertTrue(_is_db_contention(DeadlockDetected()))
        self.assertTrue(_is_db_contention(_PgBusy()))
        busy = _PgBusy()
        busy.pgcode = errorcodes.SERIALIZATION_FAILURE
        self.assertTrue(_is_db_contention(busy))
        busy = _PgBusy()
        busy.pgcode = errorcodes.DEADLOCK_DETECTED
        self.assertTrue(_is_db_contention(busy))
        self.assertFalse(_is_db_contention(RuntimeError('x')))
        unique = _PgBusy()
        unique.pgcode = errorcodes.UNIQUE_VIOLATION
        self.assertFalse(_is_db_contention(unique))
        self.assertFalse(_is_db_contention(MetaPermanentError('x')))

    def test_meta_error_summary_fixed_text(self):
        gone = MetaPermanentError('gone tok_x')
        self.assertEqual(_meta_error_summary(gone, 'lead'),
                         'Meta refused to return this lead')
        self.assertEqual(_meta_error_summary(gone, 'listing'),
                         'Meta refused the lead listing for this form')
        self.assertEqual(
            _meta_error_summary(
                MetaPermanentError('gone tok_x', code=100, subcode=33),
                'lead'),
            'Meta refused to return this lead '
            '(Meta error code 100, subcode 33)')
        self.assertEqual(
            _meta_error_summary(MetaPermanentError('gone tok_x', code=100),
                                'lead'),
            'Meta refused to return this lead (Meta error code 100)')
        self.assertEqual(
            _meta_error_summary(MetaPermanentError('gone tok_x', code='100'),
                                'lead'),
            'Meta refused to return this lead')
        expired = MetaAuthError('expired')
        self.assertEqual(
            _meta_error_summary(expired, 'listing'),
            'Meta rejected the access token while listing this form')
        self.assertEqual(
            _meta_error_summary(expired, 'lead'),
            'Meta rejected the access token while reading this lead')
        for exc in (gone, expired):
            for scope in ('lead', 'listing'):
                text = _meta_error_summary(exc, scope)
                self.assertNotIn('gone tok_x', text)
                self.assertNotIn('expired', text)

    # ---- streamed listings -----------------------------------------------

    def test_drop_known_filters_in_chunks(self):
        """Leads already stored are dropped batch by batch, with one crm.lead
        query per batch of ids; the rest keep their listing order."""
        for leadgen_id in ('LGBI_K1', 'LGBI_K3', 'LGBI_K4'):
            self.env['crm.lead'].sudo().create(
                {'name': leadgen_id, 'meta_leadgen_id': leadgen_id})
        items = [self._item('LGBI_K%d' % i, '2026-06-14T10:0%d:00+0000' % i)
                 for i in range(1, 5)]
        items.append({'created_time': '2026-06-14T10:05:00+0000'})
        lead_class = type(self.env['crm.lead'])
        original = lead_class.search
        with mock.patch.object(meta_lead_form, '_KNOWN_ID_CHUNK', 2), \
                mock.patch.object(lead_class, 'search', autospec=True,
                                  side_effect=original) as search:
            out = list(self.form._drop_known(iter(items)))
        self.assertEqual(out, [items[1], items[4]])
        queries = [c for c in search.call_args_list
                   if 'meta_leadgen_id' in repr(c.args[1])]
        self.assertEqual(len(queries), 2)

    def test_ingest_listed_stream_error_keeps_ingested(self):
        """A Meta error from a later listing page ends the read as a stop and
        keeps the leads ingested before it."""
        cases = (
            ('LGBI_ST1', MetaRateLimitError('slow', code=80005,
                                            retry_after_min=10), 'rate_limit'),
            ('LGBI_ST2', MetaPermanentError('bad'), 'listing_permanent'),
            ('LGBI_ST3', MetaAuthError('x'), 'auth'),
            ('LGBI_ST4', MetaTransientError('x'), 'transient'),
        )
        for leadgen_id, exc, stop in cases:
            with self.subTest(stop):
                def _pages(leadgen_id=leadgen_id, exc=exc):
                    yield self._item(leadgen_id, '2026-06-14T10:00:00+0000')
                    raise exc

                with self.env.cr.savepoint(), self._patch_graph(
                        fetch_lead=self._payload(leadgen_id)):
                    outcome = self.form._ingest_listed(
                        _pages(), {}, ordered=False)
                self.assertEqual(len(self._lead(leadgen_id)), 1)
                self.assertEqual(outcome['stop'], stop)
                self.assertIs(outcome['error'], exc)
                self.assertTrue(outcome['listing_error'])
                self.assertIs(outcome['leadgen_id'], False)
                self.assertIsNone(outcome['settled_until'])
        outcome = self.form._ingest_listed(iter([]), {}, ordered=False,
                                           streak=3)
        self.assertEqual(outcome['streak'], 3)
        self.assertIsNone(outcome['stop'])
        self.assertFalse(outcome['listing_error'])

    # ---- time budget and form rotation -----------------------------------

    def test_name_lookups_stop_at_deadline(self):
        """Once the run's budget is used a lead makes no name lookup; it is
        stored flagged and the retry job fills the name in later."""
        def _nameless(leadgen_id, campaign_id):
            payload = self._payload(leadgen_id)
            payload.pop('campaign_name', None)
            payload['campaign_id'] = campaign_id
            return payload

        clock = [0]
        payload = _nameless('LGBI_DL1', 'CBI_DL1')

        def _late_fetch(page, leadgen_id):
            # The fetch is the call in flight when the budget runs out.
            clock[0] = 10000
            return payload

        resolve = mock.Mock(side_effect=self._resolve_passthrough)
        with self._patch_clock(clock), \
                self._listing({'F1/leads': [self._item(
                    'LGBI_DL1', '2026-06-14T10:00:00+0000')]}), \
                self._patch_graph(fetch_lead=mock.Mock(
                    side_effect=_late_fetch), resolve_name=resolve):
            self.Form._cron_backfill()
        self.env.invalidate_all()
        resolve.assert_not_called()
        lead = self._lead('LGBI_DL1')
        self.assertEqual(len(lead), 1)
        self.assertTrue(lead.meta_attribution_pending)
        self.assertEqual(self.form.last_synced_time,
                         datetime(2026, 6, 14, 10, 0))

        with self.subTest('clock not moved'):
            clock[0] = 0
            payload2 = _nameless('LGBI_DL2', 'CBI_DL2')
            resolve = mock.Mock(side_effect=self._resolve_passthrough)
            with self._patch_clock(clock), \
                    self._listing({'F1/leads': [self._item(
                        'LGBI_DL2', '2026-06-14T10:01:00+0000')]}), \
                    self._patch_graph(fetch_lead=payload2,
                                      resolve_name=resolve):
                self.Form._cron_backfill()
            self.env.invalidate_all()
            self.assertEqual(resolve.call_count, 1)
            self.assertIn('CBI_DL2', resolve.call_args.args)
            self.assertTrue(self._lead('LGBI_DL2'))

    def test_incremental_rotates_forms(self):
        """Forms are swept longest-waiting first; a form cut by the budget
        keeps its place and starts the next run."""
        self._only_fixture_forms()
        now = fields.Datetime.now()
        old = {self.form: now - timedelta(days=4),
               self.form_a2: now - timedelta(days=3),
               self.form_b: now - timedelta(days=2),
               self.form2: now - timedelta(days=1)}
        self._stamp(old)
        cursor_a2 = self.form_a2.last_synced_time
        clock = [0]
        run = [1]
        paths = []

        def _paged(token, path, params=None, app_secret=None, status=None,
                   **kw):
            paths.append(path)
            if path == 'FBIA2/leads' and run[0] == 1:
                status['deadline_hit'] = True
                clock[0] = 10000
                return iter([self._item('LGBI_RA1',
                                        '2026-06-14T10:00:00+0000')])
            return iter([])

        def _run():
            del paths[:]
            with self._patch_clock(clock), \
                    mock.patch.object(self.Client, '_iter_paged',
                                      side_effect=_paged), \
                    self._patch_graph():
                self.Form._cron_backfill()
            self.env.invalidate_all()

        _run()
        self.assertEqual(paths, ['F1/leads', 'FBIA2/leads'])
        self.assertGreater(self.form.last_swept_time, old[self.form])
        for form in (self.form_a2, self.form_b, self.form2):
            self.assertEqual(form.last_swept_time, old[form])
        self.assertEqual(self.form_a2.last_synced_time, cursor_a2)
        self.assertFalse(self._lead('LGBI_RA1'))

        clock[0] = 0
        run[0] = 2
        start = fields.Datetime.now()
        _run()
        self.assertEqual(paths, ['FBIA2/leads', 'FBIB/leads', 'FBI2/leads',
                                 'F1/leads'])
        for form in (self.form, self.form_a2, self.form_b, self.form2):
            self.assertGreaterEqual(form.last_swept_time, start)

        with self.subTest('never swept goes first'):
            self.form2.sudo().write({'last_swept_time': False})
            _run()
            self.assertEqual(paths[0], 'FBI2/leads')

    def test_stopped_turn_keeps_priority(self):
        """A form stopped by a transient error is not stamped and goes first
        next run; the forms after it are stamped."""
        item = self._item('LGBI_TP1', '2026-06-14T10:00:00+0000')
        cases = (
            ('lead', {'fetch': self._fetch(('LGBI_TP1',),
                                           MetaTransientError('slow'))}),
            ('listing', {'raises': {'F1/leads': MetaTransientError('x')}}),
        )
        self._only_fixture_forms()
        for label, kwargs in cases:
            with self.subTest(label):
                now = fields.Datetime.now()
                old = {self.form: now - timedelta(days=4),
                       self.form_a2: now - timedelta(days=3),
                       self.form_b: now - timedelta(days=2),
                       self.form2: now - timedelta(days=1)}
                self._stamp(old)
                self.listed = []
                self._sweep({'F1/leads': [item]}, **kwargs)
                self.assertEqual(self._listed_paths()[0], 'F1/leads')
                self.assertEqual(self.form.last_swept_time, old[self.form])
                for form in (self.form_a2, self.form_b, self.form2):
                    self.assertGreaterEqual(form.last_swept_time, now)
                self.assertFalse(self._failed())
                self.listed = []
                self._sweep({})
                self.assertEqual(self._listed_paths()[0], 'F1/leads')

    def test_first_form_stamped_when_budget_runs_out(self):
        """The run's first form goes to the back even when the budget cut
        it, so one heavy form can't hold the head of the queue."""
        self._only_fixture_forms()
        now = fields.Datetime.now()
        old = {self.form: now - timedelta(days=4),
               self.form_a2: now - timedelta(days=3),
               self.form_b: now - timedelta(days=2),
               self.form2: now - timedelta(days=1)}
        self._stamp(old)
        clock = [0]

        def _side(page, leadgen_id):
            if leadgen_id == 'LGBI_FB1':
                clock[0] = 10000
            return self._payload(leadgen_id)

        fetch = mock.Mock(side_effect=_side)
        with self._patch_clock(clock):
            self._sweep({'F1/leads': [
                self._item('LGBI_FB1', '2026-06-14T10:00:00+0000'),
                self._item('LGBI_FB2', '2026-06-14T10:01:00+0000')]},
                fetch=fetch)
        self.assertTrue(self._lead('LGBI_FB1'))
        self.assertFalse(self._lead('LGBI_FB2'))
        self.assertEqual([c.args[1] for c in fetch.call_args_list],
                         ['LGBI_FB1'])
        self.assertEqual(self.form.last_synced_time,
                         datetime(2026, 6, 14, 10, 0))
        self.assertGreater(self.form.last_swept_time, old[self.form])
        self.assertEqual(self._listed_paths(), ['F1/leads'])

        clock[0] = 0
        self.listed = []
        with self._patch_clock(clock):
            self._sweep({})
        self.assertEqual(self._listed_paths()[0], 'FBIA2/leads')

    # ---- one bad lead no longer stops its form ---------------------------

    def _seed_cursor(self):
        self.form.sudo().write({'last_synced_time': datetime(2026, 6, 1)})

    def _lead_rows(self):
        return self._failed(form=self.form).filtered('meta_leadgen_id')

    def _form_rows(self):
        return self._failed(form=self.form).filtered(
            lambda r: not r.meta_leadgen_id)

    def _sweep_outcome(self, **over):
        outcome = {'settled_until': datetime(2026, 6, 14, 10, 0),
                   'stop': None, 'error': None, 'leadgen_id': False,
                   'quarantined': [], 'id_less': False, 'truncated': False,
                   'deadline_hit': False, 'complete': True}
        outcome.update(over)
        return outcome

    def test_permanent_lead_quarantined_others_ingested(self):
        self._seed_cursor()
        self._sweep({'F1/leads': [
            self._item('LGBI_Q1', '2026-06-14T10:00:00+0000'),
            self._item('LGBI_Q2', '2026-06-14T10:01:00+0000'),
            self._item('LGBI_Q3', '2026-06-14T10:02:00+0000')]},
            fetch=self._fetch(('LGBI_Q2',), MetaPermanentError('gone')))
        self.assertTrue(self._lead('LGBI_Q1'))
        self.assertTrue(self._lead('LGBI_Q3'))
        self.assertFalse(self._lead('LGBI_Q2'))
        self.assertEqual(self.form.last_synced_time,
                         datetime(2026, 6, 14, 10, 2))
        row = self._failed('LGBI_Q2')
        self.assertEqual(len(row), 1)
        self.assertEqual(len(self._failed()), 1)
        self.assertEqual(row.page_id, self.page)
        self.assertEqual(row.form_id, self.form)
        self.assertEqual(row.company_id, self.co_a)
        self.assertEqual(row.error_message, 'Meta refused to return this lead')
        self.assertNotIn('gone', row.error_message)
        self._assert_clean(row.error_message)

        with self.subTest('code and subcode kept'):
            self._sweep({'F1/leads': [
                self._item('LGBI_Q4', '2026-06-14T10:03:00+0000')]},
                fetch=self._fetch(('LGBI_Q4',), MetaPermanentError(
                    'gone', code=100, subcode=33)))
            self.assertEqual(
                self._failed('LGBI_Q4').error_message,
                'Meta refused to return this lead '
                '(Meta error code 100, subcode 33)')

        with self._patch_graph(fetch_lead=self._payload('LGBI_Q2')):
            row.with_user(self.admin_a).action_retry()
        self.env.invalidate_all()
        self.assertEqual(len(self._lead('LGBI_Q2')), 1)
        self.assertNotEqual(row.status, 'failed')

    def test_unexpected_lead_error_quarantined(self):
        Ingest = type(self.Ingest)
        original = Ingest._map_fields

        def _map(ingest, page, data, company=None):
            if data.get('id') == 'LGBI_U2':
                raise RuntimeError('boom tok_test PG1')
            return original(ingest, page, data, company=company)

        self._seed_cursor()
        with mock.patch.object(Ingest, '_map_fields', autospec=True,
                               side_effect=_map), \
                mute_logger('odoo.addons.meta_lead_ads.models.meta_lead_form'):
            self._sweep({'F1/leads': [
                self._item('LGBI_U1', '2026-06-14T10:00:00+0000'),
                self._item('LGBI_U2', '2026-06-14T10:01:00+0000'),
                self._item('LGBI_U3', '2026-06-14T10:02:00+0000')]})
        self.assertTrue(self._lead('LGBI_U1'))
        self.assertTrue(self._lead('LGBI_U3'))
        self.assertFalse(self._lead('LGBI_U2'))
        row = self._failed('LGBI_U2')
        self.assertEqual(len(row), 1)
        self.assertEqual(row.error_message, 'Unexpected backfill error')
        self._assert_clean(row.error_message)

    def test_failure_streak_cuts_off_form(self):
        self._seed_cursor()
        failing = tuple('LGBI_S%d' % i for i in range(1, 6))
        items = [self._item('LGBI_S%d' % i, '2026-06-14T10:0%d:00+0000' % i)
                 for i in range(7)]
        fetch = self._fetch(failing, MetaPermanentError('gone'))
        self._sweep({'F1/leads': items}, fetch=fetch)
        fetched = [c.args[1] for c in fetch.call_args_list]
        self.assertNotIn('LGBI_S6', fetched)
        self.assertFalse(self._lead('LGBI_S6'))
        self.assertTrue(self._lead('LGBI_S0'))
        # The cursor stays before the first failure of the run.
        self.assertEqual(self.form.last_synced_time,
                         datetime(2026, 6, 14, 10, 0))
        self.assertEqual(sorted(self._lead_rows().mapped('meta_leadgen_id')),
                         list(failing))
        form_rows = self._form_rows()
        self.assertEqual(len(form_rows), 1)
        self.assertEqual(form_rows.error_message,
                         '5 leads in a row failed; backfill stopped for this '
                         'form')

    def test_same_failing_leads_do_not_hold_cursor_for_good(self):
        """Leads that fail again on the next sweep already have their rows,
        so they no longer count towards the cut-off and the cursor moves
        past them. New failures still count."""
        self._seed_cursor()
        failing = tuple('LGBI_SA%d' % i for i in range(1, 6))
        items = [self._item('LGBI_SA%d' % i, '2026-06-14T10:0%d:00+0000' % i)
                 for i in range(1, 7)]
        fetch = self._fetch(failing, MetaPermanentError('gone'))
        self._sweep({'F1/leads': items}, fetch=fetch)
        self.assertEqual(len(self._form_rows()), 1)
        self.assertFalse(self._lead('LGBI_SA6'))
        self.assertEqual(self.form.last_synced_time, datetime(2026, 6, 1))

        self._sweep({'F1/leads': items}, fetch=fetch)
        self.assertEqual(len(self._lead('LGBI_SA6')), 1)
        self.assertEqual(self.form.last_synced_time,
                         datetime(2026, 6, 14, 10, 6))
        self.assertEqual(sorted(self._lead_rows().mapped('meta_leadgen_id')),
                         list(failing))
        # A complete sweep clears the cut-off row; the lead rows stay.
        self.assertFalse(self._form_rows())

        with self.subTest('new failures still count'):
            more = tuple('LGBI_SB%d' % i for i in range(1, 6))
            items = [self._item(lg, '2026-06-14T11:0%d:00+0000' % i)
                     for i, lg in enumerate(more, 1)]
            self._sweep({'F1/leads': items}, fetch=self._fetch(
                more, MetaPermanentError('gone')))
            self.assertEqual(len(self._form_rows()), 1)
            self.assertEqual(self.form.last_synced_time,
                             datetime(2026, 6, 14, 10, 6))

    def test_success_resets_failure_streak(self):
        self._seed_cursor()
        items = [self._item('LGBI_R%d' % i, '2026-06-14T10:0%d:00+0000' % i)
                 for i in range(1, 5)]
        with mock.patch.object(const, 'BACKFILL_FAILURE_STREAK', 2):
            self._sweep({'F1/leads': items}, fetch=self._fetch(
                ('LGBI_R1', 'LGBI_R3'), MetaPermanentError('gone')))
        self.assertTrue(self._lead('LGBI_R2'))
        self.assertTrue(self._lead('LGBI_R4'))
        self.assertEqual(len(self._lead_rows()), 2)
        self.assertEqual(len(self._form_rows()), 0)
        self.assertEqual(self.form.last_synced_time,
                         datetime(2026, 6, 14, 10, 4))

    def test_serialization_failure_is_not_quarantined(self):
        self._seed_cursor()
        cases = (('LGBI_SF', '10', SerializationFailure()),
                 ('LGBI_RC', '11', MetaIngestRaceError('x')))
        for prefix, hour, exc in cases:
            with self.subTest(type(exc).__name__):
                ids = ['%s%d' % (prefix, i) for i in range(1, 4)]
                items = [self._item(lg, '2026-06-14T%s:0%d:00+0000'
                                    % (hour, i))
                         for i, lg in enumerate(ids)]
                self._sweep({'F1/leads': items},
                            fetch=self._fetch((ids[1],), exc))
                self.assertTrue(self._lead(ids[0]))
                self.assertFalse(self._lead(ids[1]))
                self.assertFalse(self._lead(ids[2]))
                self.assertEqual(
                    self.form.last_synced_time,
                    datetime(2026, 6, 14, int(hour), 0))
                self.assertEqual(len(self._failed()), 0)

    def test_db_contention_with_pgcode_is_not_quarantined(self):
        self._seed_cursor()
        lock = _PgBusy()
        serial = _PgBusy()
        serial.pgcode = errorcodes.SERIALIZATION_FAILURE
        deadlock = _PgBusy()
        deadlock.pgcode = errorcodes.DEADLOCK_DETECTED
        cases = (('LGBI_PL', '10', lock), ('LGBI_PS', '11', serial),
                 ('LGBI_PD', '12', deadlock))
        with mock.patch.object(const, 'BACKFILL_FAILURE_STREAK', 2):
            for prefix, hour, exc in cases:
                with self.subTest(exc.pgcode):
                    ids = ['%s%d' % (prefix, i) for i in range(1, 8)]
                    items = [self._item(lg, '2026-06-14T%s:0%d:00+0000'
                                        % (hour, i))
                             for i, lg in enumerate(ids)]
                    self._sweep({'F1/leads': items},
                                fetch=self._fetch(tuple(ids[1:]), exc))
                    self.assertTrue(self._lead(ids[0]))
                    self.assertEqual(len(self._failed()), 0)
                    self.assertEqual(
                        self.form.last_synced_time,
                        datetime(2026, 6, 14, int(hour), 0))

            with self.subTest('plain error is quarantined'):
                items = [self._item('LGBI_PX1', '2026-06-14T13:00:00+0000'),
                         self._item('LGBI_PX2', '2026-06-14T13:01:00+0000')]
                with mute_logger(
                        'odoo.addons.meta_lead_ads.models.meta_lead_form'):
                    self._sweep({'F1/leads': items}, fetch=self._fetch(
                        ('LGBI_PX2',), RuntimeError('x')))
                row = self._failed('LGBI_PX2')
                self.assertEqual(len(row), 1)
                self.assertEqual(row.error_message,
                                 'Unexpected backfill error')

    def test_cron_path_writes_sweep_state_as_admin(self):
        """The sweep keeps its own state through sudo; a Meta Admin writing
        it directly gets an AccessError."""
        since = datetime(2026, 6, 1, tzinfo=timezone.utc)
        form_as_admin = self.form.with_user(self.admin_a)
        form_as_admin._advance_cursor(self._sweep_outcome(), since, None)
        self.form.invalidate_recordset()
        self.assertEqual(self.form.last_synced_time,
                         datetime(2026, 6, 14, 10, 0))
        with self.assertRaises(AccessError):
            form_as_admin.write({'last_synced_time': datetime(2026, 6, 20)})
        with self.assertRaises(AccessError):
            form_as_admin.write({'last_swept_time': fields.Datetime.now()})

    def test_time_budget_stops_sweep(self):
        self._only_fixture_forms()
        now = fields.Datetime.now()
        self._stamp({self.form: now - timedelta(days=4),
                     self.form_a2: now - timedelta(days=3),
                     self.form_b: now - timedelta(days=2),
                     self.form2: now - timedelta(days=1)})
        self._seed_cursor()
        clock = [0]

        def _side(page, leadgen_id):
            payload = self._payload(leadgen_id)
            if leadgen_id == 'LGBI_TB1':
                clock[0] = 10000
            return payload

        fetch = mock.Mock(side_effect=_side)
        with self._patch_clock(clock):
            self._sweep({'F1/leads': [
                self._item('LGBI_TB1', '2026-06-14T10:00:00+0000'),
                self._item('LGBI_TB2', '2026-06-14T10:01:00+0000')]},
                fetch=fetch)
        self.assertTrue(self._lead('LGBI_TB1'))
        self.assertEqual([c.args[1] for c in fetch.call_args_list],
                         ['LGBI_TB1'])
        self.assertEqual(self.form.last_synced_time,
                         datetime(2026, 6, 14, 10, 0))
        self.assertEqual(len(self._failed()), 0)
        self.assertNotIn('FBI2/leads', self._listed_paths())

    def test_listing_deadline_stops_paging(self):
        self._seed_cursor()
        clock = [0]
        calls = []

        def _request(token, path, params=None, **kw):
            calls.append(path)
            if path == 'F1/leads':
                clock[0] = 10000
                return {'data': [self._item('LGBI_LB1',
                                            '2026-06-14T10:00:00+0000')],
                        'paging': {'cursors': {'after': 'P2'}, 'next': 'x'}}
            return {'data': []}

        with self._patch_clock(clock), \
                mock.patch.object(self.Client, '_request',
                                  side_effect=_request), \
                self._patch_graph():
            outcome = self.form._sweep_form(deadline=100)
        self.env.invalidate_all()
        self.assertEqual(calls, ['F1/leads'])
        self.assertTrue(outcome['deadline_hit'])
        self.assertFalse(outcome['truncated'])
        self.assertEqual(outcome['stop'], 'budget')
        self.assertFalse(outcome['complete'])
        self.assertFalse(self._lead('LGBI_LB1'))
        self.assertEqual(self.form.last_synced_time, datetime(2026, 6, 1))
        # Called on its own the form isn't the run's first, so a budget cut
        # only holds the cursor.
        self.assertFalse(self.form.backfill_slicing)
        self.assertEqual(self.form.backfill_incomplete_count, 0)

        self._only_fixture_forms()
        now = fields.Datetime.now()
        self._stamp({self.form: now - timedelta(days=4),
                     self.form_a2: now - timedelta(days=3),
                     self.form_b: now - timedelta(days=2),
                     self.form2: now - timedelta(days=1)})
        clock[0] = 0
        del calls[:]
        with self._patch_clock(clock), \
                mock.patch.object(self.Client, '_request',
                                  side_effect=_request), \
                self._patch_graph():
            self.Form._cron_backfill()
        self.env.invalidate_all()
        self.assertEqual(calls, ['F1/leads'])
        for path in ('FBIA2/leads', 'FBIB/leads', 'FBI2/leads'):
            self.assertNotIn(path, calls)
        self.assertEqual(len(self._failed()), 0)
        self.assertEqual(self.form.last_synced_time, datetime(2026, 6, 1))
        # F1 was the run's first form and still didn't fit: read in slices.
        self.assertTrue(self.form.backfill_slicing)
        self.assertEqual(self.form.backfill_incomplete_count, 1)

    def test_malformed_time_lead_has_blank_submitted_at(self):
        """A lead without a usable created_time is ingested with a blank
        submission time and doesn't move the cursor. Its title's date falls
        back to today, which is only the title."""
        self._seed_cursor()
        bad = self._payload('LGBI_M1', created_time='not-a-time')
        self._sweep({'F1/leads': [
            self._item('LGBI_M1', 'not-a-time'),
            self._item('LGBI_M2', '2026-06-14T10:05:00+0000')]},
            fetch=self._fetch(payloads={
                'LGBI_M1': bad,
                'LGBI_M2': self._payload('LGBI_M2',
                                         '2026-06-14T10:05:00+0000')}))
        self.assertTrue(self._lead('LGBI_M2'))
        lead = self._lead('LGBI_M1')
        self.assertEqual(len(lead), 1)
        self.assertFalse(lead.meta_submitted_at)
        self.assertEqual(len(self._failed()), 0)
        self.assertEqual(self.form.last_synced_time,
                         datetime(2026, 6, 14, 10, 5))

        with self.subTest('created_time missing'):
            missing = self._payload('LGBI_M3')
            missing.pop('created_time')
            self._sweep({'F1/leads': [
                {'id': 'LGBI_M3'},
                self._item('LGBI_M4', '2026-06-14T10:06:00+0000')]},
                fetch=self._fetch(payloads={
                    'LGBI_M3': missing,
                    'LGBI_M4': self._payload('LGBI_M4',
                                             '2026-06-14T10:06:00+0000')}))
            lead = self._lead('LGBI_M3')
            self.assertEqual(len(lead), 1)
            self.assertFalse(lead.meta_submitted_at)
            self.assertEqual(len(self._failed()), 0)
            self.assertEqual(self.form.last_synced_time,
                             datetime(2026, 6, 14, 10, 6))

    # ---- incomplete listings and catch-up slices --------------------------

    _C = datetime(2026, 6, 1)

    def _meta_time(self, dt):
        return dt.strftime('%Y-%m-%dT%H:%M:%S+0000')

    def _dt_epoch(self, dt):
        return int(dt.replace(tzinfo=timezone.utc).timestamp())

    def _set_state(self, form=None, **vals):
        (form or self.form).sudo().write(vals)

    def _f1_params(self):
        """Params of the last F1 listing the sweep sent."""
        return [params for path, params, _kw in self.listed
                if path == 'F1/leads'][-1]

    def _lead_less_rows(self, form=None):
        return self._failed(form=form or self.form).filtered(
            lambda r: not r.meta_leadgen_id)

    def test_truncated_listing_holds_cursor(self):
        self._seed_cursor()
        self._sweep({'F1/leads': [
            self._item('LGBI_TR1', '2026-06-14T10:00:00+0000'),
            self._item('LGBI_TR2', '2026-06-14T10:05:00+0000')]},
            truncated=('F1/leads',))
        self.assertTrue(self._lead('LGBI_TR1'))
        self.assertTrue(self._lead('LGBI_TR2'))
        self.assertEqual(self.form.last_synced_time, datetime(2026, 6, 1))
        self.assertTrue(self.form.backfill_slicing)
        self.assertEqual(self.form.backfill_incomplete_count, 1)
        self.assertEqual(len(self._failed(form=self.form)), 0)

    def _sweep_requests(self, request):
        with mock.patch.object(self.Client, '_request', side_effect=request), \
                self._patch_graph(fetch_lead=self._fetch()):
            self.Form._cron_backfill()
        self.env.invalidate_all()

    def test_repeated_cursor_holds_cursor(self):
        """End to end through the real pager: Meta repeating its cursor
        leaves the listing incomplete and the cursor where it was."""
        self._seed_cursor()
        item = self._item('LGBI_RP1', '2026-06-14T10:00:00+0000')

        def _request(token, path, params=None, **kw):
            if path == 'F1/leads':
                return {'data': [dict(item)],
                        'paging': {'cursors': {'after': 'SAME'}, 'next': 'x'}}
            return {'data': []}

        self._sweep_requests(_request)
        self.assertEqual(len(self._lead('LGBI_RP1')), 1)
        self.assertEqual(self.form.last_synced_time, datetime(2026, 6, 1))
        self.assertTrue(self.form.backfill_slicing)
        self.assertEqual(self.form.backfill_incomplete_count, 1)

    def test_page_cap_holds_cursor(self):
        self._seed_cursor()
        pages = [0]

        def _request(token, path, params=None, **kw):
            if path == 'F1/leads':
                pages[0] += 1
                return {'data': [self._item(
                            'LGBI_PC%d' % pages[0],
                            '2026-06-14T10:0%d:00+0000' % pages[0])],
                        'paging': {'cursors': {'after': 'P%d' % pages[0]},
                                   'next': 'x'}}
            return {'data': []}

        with mock.patch(
                'odoo.addons.meta_lead_ads.models.meta_graph_client._MAX_PAGES', 2):
            self._sweep_requests(_request)
        self.assertEqual(pages[0], 2)
        self.assertTrue(self._lead('LGBI_PC1'))
        self.assertTrue(self._lead('LGBI_PC2'))
        self.assertEqual(self.form.last_synced_time, datetime(2026, 6, 1))
        self.assertTrue(self.form.backfill_slicing)

    def _old_stamps(self):
        now = fields.Datetime.now()
        old = {self.form: now - timedelta(days=4),
               self.form_a2: now - timedelta(days=3),
               self.form_b: now - timedelta(days=2),
               self.form2: now - timedelta(days=1)}
        self._stamp(old)
        return old

    def test_deadline_cut_on_later_form_holds_cursor_only(self):
        """A form cut by the budget after an earlier form used part of it is
        not counted as incomplete: cursor held, no row, no warning, and it
        goes first next run."""
        self._only_fixture_forms()
        old = self._old_stamps()
        stuck = const.BACKFILL_STUCK_SWEEPS - 1
        self._set_state(self.form_a2, last_synced_time=self._C,
                        backfill_incomplete_count=stuck)
        clock = [0]
        run = [1]
        calls = []

        def _request(token, path, params=None, **kw):
            calls.append(path)
            if path == 'FBIA2/leads' and run[0] == 1:
                clock[0] = 10000
                return {'data': [self._item('LGBI_DC1',
                                            '2026-06-14T10:00:00+0000')],
                        'paging': {'cursors': {'after': 'P2'}, 'next': 'x'}}
            return {'data': []}

        # Neither 'was incomplete; cursor held' nor 'did not fit' may be
        # logged; both are warnings.
        with self._patch_clock(clock), self.assertNoLogs(
                'odoo.addons.meta_lead_ads.models.meta_lead_form',
                level='WARNING'):
            self._sweep_requests(_request)
        self.assertEqual(calls, ['F1/leads', 'FBIA2/leads'])
        self.assertNotIn('FBIB/leads', calls)
        self.assertEqual(self.form_a2.last_synced_time, self._C)
        self.assertFalse(self.form_a2.backfill_slicing)
        self.assertEqual(self.form_a2.backfill_incomplete_count, stuck)
        self.assertFalse(self._lead_less_rows(self.form_a2))
        self.assertFalse(self._lead('LGBI_DC1'))
        self.assertEqual(self.form_a2.last_swept_time, old[self.form_a2])

        clock[0] = 0
        run[0] = 2
        del calls[:]
        with self._patch_clock(clock):
            self._sweep_requests(_request)
        self.assertEqual(calls[0], 'FBIA2/leads')

    def test_first_form_too_long_for_budget_slices(self):
        """The run's first form had the whole budget; a listing that still
        doesn't fit is read one day at a time from the next run."""
        self._only_fixture_forms()
        old = self._old_stamps()
        self._set_state(last_synced_time=self._C)
        clock = [0]
        run = [1]
        f1_params = []

        def _request(token, path, params=None, **kw):
            if path == 'F1/leads':
                f1_params.append(dict(params or {}))
                if run[0] == 1:
                    clock[0] = 10000
                    return {'data': [self._item('LGBI_FF1',
                                                '2026-06-14T10:00:00+0000')],
                            'paging': {'cursors': {'after': 'P2'},
                                       'next': 'x'}}
            return {'data': []}

        with self._patch_clock(clock), self.assertLogs(
                'odoo.addons.meta_lead_ads.models.meta_lead_form',
                level='WARNING') as logs:
            self._sweep_requests(_request)
        self.assertEqual(self.form.last_synced_time, self._C)
        self.assertTrue(self.form.backfill_slicing)
        self.assertEqual(self.form.backfill_incomplete_count, 1)
        self.assertGreater(self.form.last_swept_time, old[self.form])
        self.assertFalse(self._lead_less_rows())
        hits = [line for line in logs.output
                if "did not fit in one run's time budget; reading it in "
                   "slices" in line]
        self.assertEqual(len(hits), 1)
        self._assert_clean(hits[0])
        # Our budget ran out, not Meta's listing, so the log must not send
        # an admin looking for a problem on Meta's side.
        self.assertFalse([line for line in logs.output
                          if 'was incomplete; cursor held' in line])

        clock[0] = 0
        run[0] = 2
        del f1_params[:]
        with self._patch_clock(clock):
            self._sweep_requests(_request)
        self.assertEqual(_filter_ceiling(f1_params[0]),
                         self._dt_epoch(self._C + timedelta(hours=24)))

    def test_failing_form_does_not_block_slicing_behind_it(self):
        """A form whose listing fails every run is never stamped and stays
        first in line. It must not use up the run's first-form turn: the
        over-long form behind it still gets sliced and moves forward."""
        self._only_fixture_forms()
        self._old_stamps()
        self._set_state(self.form_a2, last_synced_time=self._C)
        clock = [0]
        b_params = []

        def _request(token, path, params=None, **kw):
            if path == 'F1/leads':
                raise MetaPermanentError('form gone', code=100)
            if path == 'FBIA2/leads':
                b_params.append(dict(params or {}))
                if _filter_ceiling(params) is not None:
                    # A one-day slice fits easily.
                    return {'data': []}
                # The unbounded read is too long for one run.
                if 'after' not in (params or {}):
                    return {'data': [], 'paging': {
                        'cursors': {'after': 'P2'}, 'next': 'x'}}
                clock[0] = 10000
                return {'data': [], 'paging': {
                    'cursors': {'after': 'P3'}, 'next': 'x'}}
            return {'data': []}

        for _run in range(3):
            clock[0] = 0
            with self._patch_clock(clock):
                self._sweep_requests(_request)
        self.assertTrue(self.form_a2.backfill_slicing)
        self.assertGreater(self.form_a2.last_synced_time, self._C)
        self.assertTrue(any(_filter_ceiling(p) is not None for p in b_params))
        # The failing form keeps one row and its place in line.
        self.assertEqual(len(self._lead_less_rows(self.form)), 1)

    def test_form_stopped_on_a_lead_does_not_block_slicing_behind_it(self):
        """Same as above, but the first form's listing works and Meta stops
        it on a lead every run. It keeps its place without using up the
        turn, so the long form behind it is sliced and the forms after that
        are listed again."""
        self._only_fixture_forms()
        self._old_stamps()
        self._set_state(self.form_a2, last_synced_time=self._C)
        clock = [0]
        calls = []

        def _request(token, path, params=None, **kw):
            calls.append(path)
            if path == 'F1/leads':
                return {'data': [self._item('LGBI_HS1',
                                            '2026-06-14T10:00:00+0000')]}
            if path == 'FBIA2/leads':
                if _filter_ceiling(params) is not None:
                    return {'data': []}
                if 'after' not in (params or {}):
                    return {'data': [], 'paging': {
                        'cursors': {'after': 'P2'}, 'next': 'x'}}
                clock[0] = 10000
                return {'data': [], 'paging': {
                    'cursors': {'after': 'P3'}, 'next': 'x'}}
            return {'data': []}

        fetch = self._fetch(('LGBI_HS1',), MetaTransientError('slow'))
        for _run in range(3):
            clock[0] = 0
            with self._patch_clock(clock), \
                    mock.patch.object(self.Client, '_request',
                                      side_effect=_request), \
                    self._patch_graph(fetch_lead=fetch):
                self.Form._cron_backfill()
            self.env.invalidate_all()
        self.assertTrue(self.form_a2.backfill_slicing)
        self.assertGreater(self.form_a2.last_synced_time, self._C)
        self.assertIn('FBIB/leads', calls)
        self.assertIn('FBI2/leads', calls)
        self.assertFalse(self._lead('LGBI_HS1'))
        self.assertFalse(self.form.last_synced_time)

    def test_form_hanging_every_run_does_not_starve_the_line(self):
        """A form whose listing hangs for most of the budget and then fails
        goes to the back once it has left the rest of the run without half
        the budget. The form behind it, cut with what was left, keeps its
        place and gets the whole budget, and its slicing, next run."""
        self._only_fixture_forms()
        old = self._old_stamps()
        self._set_state(self.form_a2, last_synced_time=self._C)
        clock = [0]
        calls = []

        def _request(token, path, params=None, **kw):
            calls.append(path)
            if path == 'F1/leads':
                # Hangs to the read timeout, then the network gives up.
                clock[0] += 35
                raise MetaTransientError('timeout')
            if path == 'FBIA2/leads':
                if _filter_ceiling(params) is not None:
                    return {'data': []}
                if 'after' not in (params or {}):
                    return {'data': [], 'paging': {
                        'cursors': {'after': 'P2'}, 'next': 'x'}}
                clock[0] = 10000
                return {'data': [], 'paging': {
                    'cursors': {'after': 'P3'}, 'next': 'x'}}
            return {'data': []}

        def _run():
            clock[0] = 0
            del calls[:]
            with self._patch_clock(clock):
                self._sweep_requests(_request)

        _run()
        self.assertEqual(calls[:2], ['F1/leads', 'FBIA2/leads'])
        self.assertGreater(self.form.last_swept_time, old[self.form])
        # Cut with only a few seconds left: no slicing, and no stamp.
        self.assertFalse(self.form_a2.backfill_slicing)
        self.assertEqual(self.form_a2.last_swept_time, old[self.form_a2])

        _run()
        self.assertEqual(calls[0], 'FBIA2/leads')
        self.assertTrue(self.form_a2.backfill_slicing)
        self.assertGreater(self.form_a2.last_swept_time, old[self.form_a2])

        _run()
        self.assertEqual(calls[0], 'FBIB/leads')
        self.assertIn('FBI2/leads', calls)

    def test_stop_resets_incomplete_count(self):
        """The count tracks listing completeness only: a whole listing resets
        it even when a lead stopped the form."""
        self._set_state(last_synced_time=self._C, backfill_incomplete_count=2)
        self._sweep({'F1/leads': [
            self._item('LGBI_SR1', '2026-06-14T10:00:00+0000'),
            self._item('LGBI_SR2', '2026-06-14T10:01:00+0000')]},
            fetch=self._fetch(('LGBI_SR2',), MetaTransientError('slow')))
        self.assertEqual(self.form.backfill_incomplete_count, 0)
        self.assertEqual(self.form.last_synced_time,
                         datetime(2026, 6, 14, 10, 0))
        self.assertFalse(self.form.backfill_slicing)

        with self.subTest('slicing'):
            c = self._C
            self._set_state(last_synced_time=c, backfill_slicing=True,
                            backfill_incomplete_count=2)
            self._sweep({'F1/leads': [
                self._item('LGBI_SR3', self._meta_time(c + timedelta(hours=1))),
                self._item('LGBI_SR4', self._meta_time(c + timedelta(hours=2)))]},
                fetch=self._fetch(('LGBI_SR4',), MetaTransientError('slow')),
                window=True)
            self.assertTrue(self._lead('LGBI_SR3'))
            self.assertFalse(self._lead('LGBI_SR4'))
            self.assertEqual(self.form.backfill_incomplete_count, 0)
            self.assertEqual(self.form.last_synced_time,
                             c + timedelta(hours=1))
            self.assertTrue(self.form.backfill_slicing)

    def test_slices_after_incomplete_read(self):
        c = self._C
        self._set_state(last_synced_time=c, backfill_slicing=True)
        early = self._item('LGBI_SL1', self._meta_time(c + timedelta(hours=2)))
        late = self._item('LGBI_SL2', self._meta_time(c + timedelta(hours=30)))
        self._sweep({'F1/leads': [early, late]}, window=True)
        params = self._f1_params()
        self.assertEqual(_filter_ceiling(params),
                         self._dt_epoch(c + timedelta(hours=24)))
        self.assertEqual(_filter_floor(params),
                         self._dt_epoch(c)
                         - self.Form._backfill_overlap_seconds())
        self.assertTrue(self._lead('LGBI_SL1'))
        self.assertFalse(self._lead('LGBI_SL2'))
        self.assertEqual(self.form.last_synced_time, c + timedelta(hours=24))
        self.assertTrue(self.form.backfill_slicing)

        self._sweep({'F1/leads': [early, late]}, window=True)
        self.assertTrue(self._lead('LGBI_SL2'))
        self.assertEqual(self.form.last_synced_time, c + timedelta(hours=48))

    def test_slicing_ends_when_caught_up(self):
        now = fields.Datetime.now()
        lead_at = now - timedelta(hours=1)
        self._set_state(last_synced_time=now - timedelta(hours=2),
                        backfill_slicing=True, backfill_incomplete_count=2)
        self._sweep({'F1/leads': [
            self._item('LGBI_CU1', self._meta_time(lead_at))]}, window=True)
        self.assertIsNone(_filter_ceiling(self._f1_params()))
        self.assertTrue(self._lead('LGBI_CU1'))
        self.assertEqual(self.form.last_synced_time,
                         lead_at.replace(microsecond=0))
        self.assertFalse(self.form.backfill_slicing)
        self.assertEqual(self.form.backfill_incomplete_count, 0)

    def test_stuck_form_row_then_clears(self):
        self._seed_cursor()
        for sweep in range(1, 4):
            self._sweep({}, truncated=('F1/leads',))
            rows = self._lead_less_rows()
            if sweep < 3:
                self.assertFalse(rows, sweep)
        self.assertEqual(len(rows), 1)
        self.assertEqual(rows.error_message,
                         'Lead listing incomplete; backfill cursor held')
        self._assert_clean(rows.error_message)
        self.assertEqual(self.form.backfill_incomplete_count, 3)
        self.assertEqual(self.form.last_synced_time, datetime(2026, 6, 1))

        self._sweep({})
        self.assertFalse(self._lead_less_rows())
        self.assertEqual(self.form.backfill_incomplete_count, 0)

    def test_incomplete_slice_narrows_the_next_one(self):
        """A catch-up slice that is itself incomplete halves the next one,
        clean slices double it back to a day, and catching up resets it."""
        c = self._C
        self._set_state(last_synced_time=c, backfill_slicing=True)
        expected = ((24, 12), (12, 6))
        for read, after in expected:
            self._sweep({}, truncated=('F1/leads',))
            self.assertEqual(_filter_ceiling(self._f1_params()),
                             self._dt_epoch(c + timedelta(hours=read)))
            self.assertEqual(self.form.backfill_slice_hours, after)
            self.assertEqual(self.form.last_synced_time, c)

        self._sweep({}, window=True)
        self.assertEqual(_filter_ceiling(self._f1_params()),
                         self._dt_epoch(c + timedelta(hours=6)))
        self.assertEqual(self.form.last_synced_time, c + timedelta(hours=6))
        self.assertEqual(self.form.backfill_slice_hours, 12)
        self._sweep({}, window=True)
        self.assertEqual(self.form.last_synced_time, c + timedelta(hours=18))
        # Back to a whole day, stored as empty.
        self.assertFalse(self.form.backfill_slice_hours)

        with self.subTest('floor'):
            self._set_state(backfill_slice_hours=1)
            with self.assertLogs(
                    'odoo.addons.meta_lead_ads.models.meta_lead_form',
                    level='WARNING') as logs:
                self._sweep({}, truncated=('F1/leads',))
            self.assertEqual(self.form.backfill_slice_hours, 1)
            hits = [line for line in logs.output
                    if 'could not list 1 hour(s)' in line]
            self.assertEqual(len(hits), 1)

        with self.subTest('caught up'):
            now = fields.Datetime.now()
            self._set_state(last_synced_time=now - timedelta(minutes=30),
                            backfill_slice_hours=3)
            self._sweep({}, window=True)
            self.assertFalse(self.form.backfill_slicing)
            self.assertFalse(self.form.backfill_slice_hours)

    def test_empty_slice_advances(self):
        c = self._C
        self._set_state(last_synced_time=c, backfill_slicing=True)
        self._sweep({}, window=True)
        self.assertEqual(self.form.last_synced_time, c + timedelta(hours=24))
        self.assertTrue(self.form.backfill_slicing)

    def test_slice_clips_when_meta_ignores_upper_bound(self):
        """If Meta drops the LESS_THAN bound the later leads are still
        ingested, but the cursor stops at the slice end."""
        c = self._C
        self._set_state(last_synced_time=c, backfill_slicing=True)
        items = [self._item('LGBI_IG1', self._meta_time(c + timedelta(hours=2))),
                 self._item('LGBI_IG2',
                            self._meta_time(c + timedelta(hours=30)))]
        with self.assertLogs('odoo.addons.meta_lead_ads.models.meta_lead_form',
                             level='WARNING') as logs:
            self._sweep({'F1/leads': items})
        self.assertEqual(_filter_ceiling(self._f1_params()),
                         self._dt_epoch(c + timedelta(hours=24)))
        self.assertTrue(self._lead('LGBI_IG1'))
        self.assertTrue(self._lead('LGBI_IG2'))
        self.assertEqual(self.form.last_synced_time, c + timedelta(hours=24))
        self.assertTrue(self.form.backfill_slicing)
        hits = [line for line in logs.output
                if 'returned leads past the slice end' in line]
        self.assertEqual(len(hits), 1)
        self._assert_clean(hits[0])

        with self.subTest('truncated slice'):
            self._set_state(last_synced_time=c, backfill_slicing=True)
            before = self.form.backfill_incomplete_count
            items = [
                self._item('LGBI_IG3', self._meta_time(c + timedelta(hours=3))),
                self._item('LGBI_IG4',
                           self._meta_time(c + timedelta(hours=31)))]
            self._sweep({'F1/leads': items}, truncated=('F1/leads',))
            self.assertTrue(self._lead('LGBI_IG3'))
            self.assertTrue(self._lead('LGBI_IG4'))
            self.assertEqual(self.form.last_synced_time, c)
            self.assertTrue(self.form.backfill_slicing)
            self.assertEqual(self.form.backfill_incomplete_count, before + 1)

    def test_cutoff_row_wins_over_stuck_row(self):
        """Both rows want the form's one lead-less slot; the cut-off row
        keeps it and the stuck state goes to the log."""
        self._set_state(last_synced_time=self._C,
                        backfill_incomplete_count=const.BACKFILL_STUCK_SWEEPS - 1)
        failing = tuple('LGBI_CW%d' % i for i in range(1, 6))
        items = [self._item(lg, '2026-06-14T10:0%d:00+0000' % i)
                 for i, lg in enumerate(failing, 1)]
        with self.assertLogs('odoo.addons.meta_lead_ads.models.meta_lead_form',
                             level='WARNING') as logs:
            self._sweep({'F1/leads': items}, truncated=('F1/leads',),
                        fetch=self._fetch(failing, MetaPermanentError('gone')))
        rows = self._lead_less_rows()
        self.assertEqual(len(rows), 1)
        self.assertEqual(rows.error_message,
                         '5 leads in a row failed; backfill stopped for this '
                         'form')
        self.assertEqual(len(self._lead_rows()), 5)
        self.assertEqual(self.form.backfill_incomplete_count,
                         const.BACKFILL_STUCK_SWEEPS)
        self.assertEqual(self.form.last_synced_time, self._C)
        hits = [line for line in logs.output
                if 'was incomplete; cursor held' in line]
        self.assertEqual(len(hits), 1)
        self._assert_clean(hits[0])

    # ---- dead tokens and Meta's pauses -----------------------------------

    def _assert_paused_about(self, until, before, after, minutes=10):
        self.assertTrue(until)
        self.assertGreaterEqual(until, before + timedelta(minutes=minutes - 1))
        self.assertLessEqual(until, after + timedelta(minutes=minutes + 1))

    def _app_pause(self):
        return self.env['meta.webhook.event']._app_lead_fetch_paused_until()

    def test_auth_stops_only_that_account(self):
        """A rejected token skips the rest of its account's forms; other
        accounts still sync, and the run leaves one row on the form where
        the token failed."""
        self._only_fixture_forms()
        for _run in range(2):
            self.listed = []
            self._sweep(
                {'FBI2/leads': [self._item('LGBI_A1',
                                           '2026-06-14T10:00:00+0000')]},
                raises={'F1/leads': MetaAuthError('token expired')})
            paths = self._listed_paths()
            self.assertIn('F1/leads', paths)
            self.assertNotIn('FBIA2/leads', paths)
            self.assertNotIn('FBIB/leads', paths)
            self.assertIn('FBI2/leads', paths)
            self.assertTrue(self._lead('LGBI_A1'))
            rows = self._failed()
            self.assertEqual(len(rows), 1)
            self.assertFalse(rows.meta_leadgen_id)
            self.assertEqual(rows.form_id, self.form)
            self.assertEqual(rows.page_id, self.page)
            self.assertEqual(
                rows.error_message,
                'Meta rejected the access token while listing this form')
            self.assertNotIn('token expired', rows.error_message)
            self._assert_clean(rows.error_message)

    def test_auth_on_lead_keeps_progress(self):
        """A token rejected on a lead's fetch keeps the leads before it, holds
        the cursor there and stops the rest of the account."""
        self._only_fixture_forms()
        self._sweep({'F1/leads': [
            self._item('LGBI_L1', '2026-06-14T10:00:00+0000'),
            self._item('LGBI_L2', '2026-06-14T10:01:00+0000'),
            self._item('LGBI_L3', '2026-06-14T10:02:00+0000')]},
            fetch=self._fetch(('LGBI_L2',), MetaAuthError('expired')))
        self.assertTrue(self._lead('LGBI_L1'))
        self.assertFalse(self._lead('LGBI_L2'))
        self.assertFalse(self._lead('LGBI_L3'))
        self.assertEqual(self.form.last_synced_time,
                         datetime(2026, 6, 14, 10, 0))
        rows = self._failed()
        self.assertEqual(len(rows), 1)
        self.assertEqual(rows.meta_leadgen_id, 'LGBI_L2')
        self.assertEqual(
            rows.error_message,
            'Meta rejected the access token while reading this lead')
        self._assert_clean(rows.error_message)
        paths = self._listed_paths()
        self.assertNotIn('FBIA2/leads', paths)
        self.assertNotIn('FBIB/leads', paths)
        self.assertIn('FBI2/leads', paths)

    def test_paused_page_forms_not_swept(self):
        """A paused Page's forms wait for the pause to end; other Pages are
        swept."""
        self._only_fixture_forms()
        now = fields.Datetime.now()
        self.page.sudo().lead_fetch_paused_until = now + timedelta(hours=1)
        self._sweep({})
        paths = self._listed_paths()
        self.assertNotIn('F1/leads', paths)
        self.assertNotIn('FBIA2/leads', paths)
        self.assertIn('FBIB/leads', paths)
        self.assertIn('FBI2/leads', paths)

        self.page.sudo().lead_fetch_paused_until = (
            fields.Datetime.now() - timedelta(minutes=1))
        self.listed = []
        self._sweep({})
        paths = self._listed_paths()
        self.assertIn('F1/leads', paths)
        self.assertIn('FBIA2/leads', paths)

    def test_app_pause_skips_sweep(self):
        """Nothing is listed while lead reads are paused for the whole
        app."""
        self.env['ir.config_parameter'].sudo().set_param(
            APP_PAUSE_PARAM, fields.Datetime.to_string(
                fields.Datetime.now() + timedelta(hours=1)))
        self._sweep({})
        self.assertEqual(self.listed, [])
        self._reconcile({})
        self.assertEqual(self.listed, [])

    def test_listing_throttle_pauses_page(self):
        """A Page-level throttle on a listing pauses that Page for Meta's
        estimate and skips its other forms; other Pages go on."""
        self._only_fixture_forms()
        before = fields.Datetime.now()
        self._sweep({}, raises={'F1/leads': MetaRateLimitError(
            'slow', code=80005, retry_after_min=10)})
        after = fields.Datetime.now()
        self._assert_paused_about(self.page.lead_fetch_paused_until,
                                  before, after)
        paths = self._listed_paths()
        self.assertNotIn('FBIA2/leads', paths)
        self.assertIn('FBIB/leads', paths)
        self.assertIn('FBI2/leads', paths)
        self.assertIsNone(self._app_pause())
        self.assertEqual(len(self._failed()), 0)

    def test_lead_throttle_pauses_page(self):
        """A Page-level throttle on a lead's fetch keeps the leads before it
        and pauses the Page."""
        self._only_fixture_forms()
        before = fields.Datetime.now()
        self._sweep({'F1/leads': [
            self._item('LGBI_T1', '2026-06-14T10:00:00+0000'),
            self._item('LGBI_T2', '2026-06-14T10:01:00+0000')]},
            fetch=self._fetch(('LGBI_T2',), MetaRateLimitError(
                'slow', code=80005, retry_after_min=10)))
        after = fields.Datetime.now()
        self.assertTrue(self._lead('LGBI_T1'))
        self.assertFalse(self._lead('LGBI_T2'))
        self.assertEqual(self.form.last_synced_time,
                         datetime(2026, 6, 14, 10, 0))
        self._assert_paused_about(self.page.lead_fetch_paused_until,
                                  before, after)
        paths = self._listed_paths()
        self.assertNotIn('FBIA2/leads', paths)
        self.assertIn('FBIB/leads', paths)
        self.assertIn('FBI2/leads', paths)
        self.assertEqual(len(self._failed()), 0)

    def test_app_throttle_ends_run(self):
        """Any other throttle code pauses the whole app and ends the run."""
        self._only_fixture_forms()
        before = fields.Datetime.now()
        self._sweep({}, raises={'F1/leads': MetaRateLimitError(
            'slow', code=4, retry_after_min=10)})
        after = fields.Datetime.now()
        self._assert_paused_about(self._app_pause(), before, after)
        self.assertEqual(self._listed_paths(), ['F1/leads'])
        self.assertFalse(self.page.lead_fetch_paused_until)
        self.assertEqual(len(self._failed()), 0)

    # ---- reconcile sweep --------------------------------------------------

    def _reconcile(self, by_path=None, fetch=None, window=True, **listing):
        if window:
            fake = self._listing_by_window(by_path)
        else:
            fake = self._listing(by_path, **listing)
        with fake, self._patch_graph(fetch_lead=fetch or self._fetch()):
            self.Form._cron_reconcile()
        self.env.invalidate_all()

    def _reconcile_stamps(self):
        now = fields.Datetime.now()
        old = {self.form: now - timedelta(days=4),
               self.form_a2: now - timedelta(days=3),
               self.form_b: now - timedelta(days=2),
               self.form2: now - timedelta(days=1)}
        for form, stamp in old.items():
            form.sudo().write({'last_reconciled_time': stamp})
        return old

    def _cursor_state(self, form):
        return (form.last_synced_time, form.backfill_slicing,
                form.backfill_incomplete_count, form.last_swept_time)

    def _reconcile_state(self, form):
        return (form.last_reconciled_time, form.reconcile_resume_time,
                form.reconcile_slice_hours)

    def test_reconcile_catches_late_lead(self):
        """A lead Meta shows after the cursor passed its time is missed by
        the backfill and caught by the reconcile sweep."""
        now = fields.Datetime.now().replace(second=0, microsecond=0)
        cursor = now - timedelta(hours=2)
        self._set_state(last_synced_time=cursor)
        by_path = {'F1/leads': [self._item(
            'LGBI_LATE', self._meta_time(cursor - timedelta(hours=1)))]}
        self._sweep(by_path, window=True)
        self.assertFalse(self._lead('LGBI_LATE'))
        self.assertEqual(self.form.last_synced_time, cursor)

        fetch = self._fetch()
        self._reconcile(by_path, fetch=fetch)
        self.assertEqual(len(self._lead('LGBI_LATE')), 1)
        self.assertIn('LGBI_LATE', [c.args[1] for c in fetch.call_args_list])
        self.assertEqual(self.form.last_synced_time, cursor)

    def test_reconcile_leaves_cursor_and_form_rows(self):
        """The reconcile sweep never moves the backfill state, never clears
        a form row and writes no stuck row; the backfill never touches the
        reconcile state either."""
        self._only_fixture_forms()
        now = fields.Datetime.now().replace(microsecond=0)
        self._set_state(last_synced_time=now - timedelta(hours=2),
                        backfill_slicing=True, backfill_incomplete_count=2)
        self.Log._record_failure(False, 'cron', 'x', form=self.form)
        forms = (self.form, self.form_a2, self.form_b, self.form2)
        before = {form: self._cursor_state(form) for form in forms}
        recent = self._meta_time(now - timedelta(hours=3))
        self._reconcile({'F1/leads': [self._item('LGBI_LC1', recent)],
                         'FBIA2/leads': [self._item('LGBI_LC2', recent)]},
                        window=False, truncated=('FBIA2/leads',))
        self.assertTrue(self._lead('LGBI_LC1'))
        self.assertTrue(self._lead('LGBI_LC2'))
        for form in forms:
            self.assertEqual(self._cursor_state(form), before[form],
                             form.form_id)
        rows = self._lead_less_rows()
        self.assertEqual(len(rows), 1)
        self.assertEqual(rows.error_message, 'x')
        self.assertFalse(self._lead_less_rows(self.form_a2))
        first = self._f1_reads()[0]
        self.assertIsNone(_filter_ceiling(first))
        expected = (self._dt_epoch(now - timedelta(days=7))
                    - self.Form._backfill_overlap_seconds())
        self.assertLessEqual(abs(_filter_floor(first) - expected), 60)

        after = {form: self._reconcile_state(form) for form in forms}
        self._sweep({})
        for form in forms:
            self.assertEqual(self._reconcile_state(form), after[form],
                             form.form_id)

    def test_reconcile_skips_known_leads(self):
        """Leads already stored are dropped before any fetch and leave no
        skipped row."""
        items = [self._item('LGBI_K%d' % i, '2026-06-14T10:0%d:00+0000' % i)
                 for i in range(1, 5)]
        self._sweep({'F1/leads': [items[0], items[2], items[3]]})

        def _skipped():
            return self.Log.sudo().search_count(
                [('status', '=', 'skipped_idempotent')])

        before = _skipped()
        fetch = self._fetch()
        with mock.patch.object(meta_lead_form, '_KNOWN_ID_CHUNK', 2):
            self._reconcile({'F1/leads': items}, fetch=fetch, window=False)
        self.assertEqual([c.args[1] for c in fetch.call_args_list],
                         ['LGBI_K2'])
        self.assertEqual(len(self._lead('LGBI_K2')), 1)
        self.assertEqual(_skipped(), before)

    def test_reconcile_closes_quarantine_row(self):
        item = self._item('LGBI_RQ', '2026-06-14T10:00:00+0000')
        self._sweep({'F1/leads': [item]}, fetch=self._fetch(
            ('LGBI_RQ',), MetaPermanentError('gone')))
        self.assertEqual(len(self._failed('LGBI_RQ')), 1)
        self._reconcile({'F1/leads': [item]}, window=False)
        self.assertEqual(len(self._lead('LGBI_RQ')), 1)
        self.assertFalse(self._failed('LGBI_RQ'))

    def test_deleted_or_merged_lead_is_not_brought_back(self):
        """A lead deleted in Odoo, or merged into another, stays gone when a
        later read lists it again; the backfill cursor still moves past
        it."""
        items = [self._item('LGBI_DL%d' % i, '2026-06-14T10:0%d:00+0000' % i)
                 for i in range(1, 4)]
        self._sweep({'F1/leads': items})
        for item in items:
            self.assertEqual(len(self._lead(item['id'])), 1)
        self._lead('LGBI_DL1').unlink()
        # Different contacts, so the merge keeps one and unlinks the other.
        merged = self._lead('LGBI_DL2') | self._lead('LGBI_DL3')
        survivor = merged.merge_opportunity()
        gone = 'LGBI_DL2' if survivor.meta_leadgen_id == 'LGBI_DL3' \
            else 'LGBI_DL3'
        self.assertFalse(self._lead(gone))
        self.assertEqual(
            self.env['meta.leadgen.tombstone']._removed(
                ['LGBI_DL1', 'LGBI_DL2', 'LGBI_DL3']),
            {'LGBI_DL1', gone})

        fetch = self._fetch()
        self._reconcile({'F1/leads': items}, fetch=fetch, window=False)
        self.assertNotIn('LGBI_DL1', [c.args[1] for c in fetch.call_args_list])
        self.assertNotIn(gone, [c.args[1] for c in fetch.call_args_list])
        self.assertFalse(self._lead('LGBI_DL1'))
        self.assertFalse(self._lead(gone))

        with self.subTest('backfill'):
            self._seed_cursor()
            fetch = self._fetch()
            self._sweep({'F1/leads': items}, fetch=fetch)
            self.assertNotIn('LGBI_DL1',
                             [c.args[1] for c in fetch.call_args_list])
            self.assertFalse(self._lead('LGBI_DL1'))
            self.assertFalse(self._lead(gone))
            self.assertEqual(self.form.last_synced_time,
                             datetime(2026, 6, 14, 10, 3))

    def test_removed_lead_records_expire(self):
        Tombstone = self.env['meta.leadgen.tombstone']
        Tombstone._remember(['LGBI_TS_OLD', 'LGBI_TS_NEW', 'LGBI_TS_OLD'])
        # A second delete of the same id is a no-op.
        Tombstone._remember(['LGBI_TS_NEW'])
        old = Tombstone.search([('leadgen_id', '=', 'LGBI_TS_OLD')])
        self.assertEqual(len(old), 1)
        self.env.cr.execute(
            "UPDATE meta_leadgen_tombstone SET create_date = %s "
            "WHERE id = %s",
            (fields.Datetime.now() - timedelta(days=201), old.id))
        self.env.invalidate_all()
        self.Log._cron_vacuum_logs()
        self.assertEqual(
            Tombstone._removed(['LGBI_TS_OLD', 'LGBI_TS_NEW']),
            {'LGBI_TS_NEW'})

    def test_reconcile_cutoff_row_is_its_own(self):
        """A reconcile cut-off leaves a row that says so. A clean backfill
        leaves it alone; the next finished reconcile pass clears it."""
        now = fields.Datetime.now().replace(microsecond=0)
        failing = tuple('LGBI_RC%d' % i for i in range(1, 6))
        items = [self._item(lg, self._meta_time(now - timedelta(hours=i)))
                 for i, lg in enumerate(failing, 1)]
        self._reconcile({'F1/leads': items}, window=False,
                        fetch=self._fetch(failing, MetaPermanentError('gone')))
        rows = self._lead_less_rows()
        self.assertEqual(len(rows), 1)
        self.assertEqual(
            rows.error_message,
            '5 leads in a row failed; reconcile stopped for this form')

        self._sweep({})
        self.assertEqual(self._lead_less_rows(), rows)

        self._reconcile({'F1/leads': items}, window=False)
        self.assertFalse(self._lead_less_rows())
        for lg in failing:
            self.assertEqual(len(self._lead(lg)), 1)

    def test_reconcile_quarantines_and_stops_dead_account(self):
        self._only_fixture_forms()
        items = [self._item('LGBI_RD%d' % i, '2026-06-14T10:0%d:00+0000' % i)
                 for i in range(1, 4)]
        self._reconcile(
            {'F1/leads': items},
            fetch=self._fetch(('LGBI_RD2',), MetaPermanentError('gone')),
            window=False, raises={'FBI2/leads': MetaAuthError('expired')})
        self.assertTrue(self._lead('LGBI_RD1'))
        self.assertTrue(self._lead('LGBI_RD3'))
        lead_rows = self._lead_rows()
        self.assertEqual(len(lead_rows), 1)
        self.assertTrue(lead_rows.error_message.startswith(
            'Meta refused to return this lead'))
        auth_rows = self._lead_less_rows(self.form2)
        self.assertEqual(len(auth_rows), 1)
        self.assertEqual(
            auth_rows.error_message,
            'Meta rejected the access token while listing this form')
        for row in self._failed():
            self.assertNotIn('expired', row.error_message)
            self._assert_clean(row.error_message)

    def test_reconcile_listing_error_midstream_keeps_ingested(self):
        """A Meta error from a later listing page keeps the leads read
        before it; a throttle still pauses the Page, a refusal leaves one
        listing row."""
        self._only_fixture_forms()
        now = fields.Datetime.now().replace(microsecond=0)
        cases = (
            ('throttle', 'LGBI_MS1',
             MetaRateLimitError('slow', code=80005, retry_after_min=10)),
            ('refused', 'LGBI_MS2', MetaPermanentError('bad')),
        )
        for label, leadgen_id, exc in cases:
            with self.subTest(label):
                self.page.sudo().lead_fetch_paused_until = False
                old = self._reconcile_stamps()
                self.listed = []

                def _paged(token, path, params=None, app_secret=None,
                           status=None, leadgen_id=leadgen_id, exc=exc,
                           **kw):
                    self.listed.append((path, dict(params or {}), dict(kw)))
                    if path != 'F1/leads':
                        return iter([])

                    def _pages():
                        yield self._item(leadgen_id, self._meta_time(
                            now - timedelta(hours=1)))
                        raise exc
                    return _pages()

                before = fields.Datetime.now()
                with mock.patch.object(self.Client, '_iter_paged',
                                       side_effect=_paged), \
                        self._patch_graph(fetch_lead=self._fetch()):
                    self.Form._cron_reconcile()
                after = fields.Datetime.now()
                self.env.invalidate_all()
                self.assertEqual(len(self._lead(leadgen_id)), 1)
                self.assertEqual(self.form.last_reconciled_time,
                                 old[self.form])
                if label == 'throttle':
                    self._assert_paused_about(
                        self.page.lead_fetch_paused_until, before, after)
                    self.assertNotIn('FBIA2/leads', self._listed_paths())
                    self.assertEqual(len(self._failed()), 0)
                else:
                    rows = self._lead_less_rows()
                    self.assertEqual(len(rows), 1)
                    self.assertEqual(
                        rows.error_message,
                        'Meta refused the lead listing for this form')
                    for row in self._failed():
                        self.assertNotIn('bad', row.error_message)

    def test_reconcile_rotates_forms(self):
        """Forms take turns by their reconcile stamp; a form cut by the
        budget with the whole budget left narrows its read and goes first
        next run."""
        self._only_fixture_forms()
        old = self._reconcile_stamps()
        clock = [0]
        bump = [True]
        listed = []

        def _paged(token, path, params=None, app_secret=None, status=None,
                   **kw):
            listed.append(path)
            if path == 'FBIA2/leads' and bump[0]:
                status['deadline_hit'] = True
                clock[0] = 10000
            return iter([])

        def _run():
            del listed[:]
            with self._patch_clock(clock), \
                    mock.patch.object(self.Client, '_iter_paged',
                                      side_effect=_paged), \
                    self._patch_graph():
                self.Form._cron_reconcile()
            self.env.invalidate_all()
            # A form may send more than one read in its turn.
            return [p for i, p in enumerate(listed)
                    if i == 0 or listed[i - 1] != p]

        start = fields.Datetime.now()
        self.assertEqual(_run(), ['F1/leads', 'FBIA2/leads'])
        self.assertGreaterEqual(self.form.last_reconciled_time, start)
        for form in (self.form_a2, self.form_b, self.form2):
            self.assertEqual(form.last_reconciled_time, old[form])
        self.assertEqual(self.form_a2.reconcile_slice_hours, 84)
        self.assertTrue(self.form_a2.reconcile_resume_time)

        clock[0] = 0
        bump[0] = False
        start = fields.Datetime.now()
        order = _run()
        self.assertEqual(order, ['FBIA2/leads', 'FBIB/leads', 'FBI2/leads',
                                 'F1/leads'])
        self.assertEqual(len(order), len(set(order)))
        self.assertEqual(listed.count('FBIA2/leads'), 2)
        for form in (self.form, self.form_a2, self.form_b, self.form2):
            self.assertGreaterEqual(form.last_reconciled_time, start)
        self.assertFalse(self.form_a2.reconcile_resume_time)
        self.assertFalse(self.form_a2.reconcile_slice_hours)

        with self.subTest('never reconciled goes first'):
            self.form2.sudo().write({'last_reconciled_time': False})
            self.assertEqual(_run()[0], 'FBI2/leads')

    def test_reconcile_stop_keeps_priority(self):
        """A form stopped by a transient error is not stamped and goes first
        next run; the forms after it are stamped."""
        self._only_fixture_forms()
        now = fields.Datetime.now().replace(microsecond=0)
        item = self._item('LGBI_RS1',
                          self._meta_time(now - timedelta(hours=1)))
        cases = (
            ('lead', self.form, {'F1/leads': [item]},
             {'fetch': self._fetch(('LGBI_RS1',), MetaTransientError('x'))}),
            ('listing', self.form_b, {},
             {'raises': {'FBIB/leads': MetaTransientError('x')}}),
        )
        for label, stopped, by_path, kwargs in cases:
            with self.subTest(label):
                old = self._reconcile_stamps()
                self.listed = []
                start = fields.Datetime.now()
                self._reconcile(by_path, window=False, **kwargs)
                self.assertEqual(stopped.last_reconciled_time, old[stopped])
                for form in old:
                    if form != stopped:
                        self.assertGreaterEqual(form.last_reconciled_time,
                                                start, form.form_id)
                self.listed = []
                self._reconcile({}, window=False)
                self.assertEqual(self._listed_paths()[0],
                                 self._path(stopped))

    def test_reconcile_oversized_form_keeps_progress_and_moves_back(self):
        """The run's first form cut by the budget keeps the leads it read,
        narrows its read, goes to the back and resumes from the same place
        on its next turn."""
        self._only_fixture_forms()
        self._reconcile_stamps()
        now = fields.Datetime.now().replace(microsecond=0)
        items = [self._item('LGBI_OV%d' % i,
                            self._meta_time(now - timedelta(hours=i)))
                 for i in (1, 2)]
        clock = [0]
        bump = [True]

        def _paged(token, path, params=None, app_secret=None, status=None,
                   **kw):
            self.listed.append((path, dict(params or {}), dict(kw)))
            if path != 'F1/leads' or not bump[0]:
                return iter([])

            def _pages():
                yield from (dict(i) for i in items)
                status['deadline_hit'] = True
                clock[0] = 10000
            return _pages()

        start = fields.Datetime.now()
        # One fake page per batch of known ids, as one Graph page is.
        with self._patch_clock(clock), \
                mock.patch.object(meta_lead_form, '_KNOWN_ID_CHUNK', 2), \
                mock.patch.object(self.Client, '_iter_paged',
                                  side_effect=_paged), \
                self._patch_graph(fetch_lead=self._fetch()), \
                self.assertLogs(
                    'odoo.addons.meta_lead_ads.models.meta_lead_form',
                    level='WARNING') as logs:
            self.Form._cron_reconcile()
        self.env.invalidate_all()
        self.assertEqual(self._listed_paths(), ['F1/leads'])
        self.assertTrue(self._lead('LGBI_OV1'))
        self.assertTrue(self._lead('LGBI_OV2'))
        self.assertGreaterEqual(self.form.last_reconciled_time, start)
        self.assertTrue(self.form.reconcile_resume_time)
        self.assertEqual(self.form.reconcile_slice_hours, 84)
        hits = [line for line in logs.output
                if "did not fit in one run's time budget" in line]
        self.assertEqual(len(hits), 1)
        self._assert_clean(hits[0])
        floor = _filter_floor(self._f1_reads()[0])

        clock[0] = 0
        bump[0] = False
        self.listed = []
        with self._patch_clock(clock), \
                mock.patch.object(self.Client, '_iter_paged',
                                  side_effect=_paged), \
                self._patch_graph():
            self.Form._cron_reconcile()
        self.env.invalidate_all()
        paths = self._listed_paths()
        self.assertEqual(paths[0], 'FBIA2/leads')
        self.assertEqual(paths[-1], 'F1/leads')
        self.assertEqual(_filter_floor(self._f1_reads()[0]), floor)

    def test_reconcile_window_param(self):
        icp = self.env['ir.config_parameter'].sudo()
        key = 'meta_lead_ads.reconcile_window_days'
        icp.search([('key', '=', key)]).unlink()
        self.assertEqual(self.Form._reconcile_window_days(), 7)
        for raw, expected in (('14', 14), ('0', 1), ('-3', 1),
                              ('100000', 90), ('junk', 7)):
            icp.set_param(key, raw)
            self.assertEqual(self.Form._reconcile_window_days(), expected,
                             raw)

    def test_reconcile_cron_record(self):
        cron = self.env.ref('meta_lead_ads.cron_meta_reconcile')
        self.assertEqual(cron.interval_number, 1)
        self.assertEqual(cron.interval_type, 'days')
        self.assertIn('model._cron_reconcile()', cron.code)
        self.assertEqual(cron.model_id.model, 'meta.lead.form')
        data = self.env['ir.model.data'].search([
            ('module', '=', 'meta_lead_ads'),
            ('name', '=', 'cron_meta_reconcile')])
        self.assertTrue(data.noupdate)
        self.assertEqual(
            self.env.ref('meta_lead_ads.meta_reconcile_window_days').value,
            '7')

    def test_reconcile_skips_paused_page(self):
        """A paused Page's forms wait for the pause in the reconcile sweep
        too; other Pages are read."""
        self._only_fixture_forms()
        self.page.sudo().lead_fetch_paused_until = (
            fields.Datetime.now() + timedelta(hours=1))
        self._reconcile({})
        paths = self._listed_paths()
        self.assertNotIn('F1/leads', paths)
        self.assertNotIn('FBIA2/leads', paths)
        self.assertIn('FBI2/leads', paths)

    # ---- reconcile: place and read length ---------------------------------

    _DEEP_AGES = (6, 24, 48, 72, 96, 108, 120, 132, 144, 156)

    def _deep_items(self, t):
        """F1 leads at the given hours before ``t``; the oldest is
        LGBI_DEEP."""
        items = []
        for age in self._DEEP_AGES:
            leadgen_id = 'LGBI_DEEP' if age == 156 else 'LGBI_H%d' % age
            items.append(self._item(
                leadgen_id, self._meta_time(t - timedelta(hours=age))))
        return items

    def _reconcile_listing(self, by_path, cut=None, on_read=None):
        """Window-aware listing fake for the reconcile sweep.

        Returns the path's items strictly inside the sent bounds, newest
        first. With ``cut``, a read spanning more than 24 h that holds more
        than two items yields only the two newest and then calls
        ``cut(status)``, the way a listing stopped after its first page
        looks. ``on_read(path, status)`` runs before anything is yielded.
        """
        def _then_cut(items, status):
            yield from items
            cut(status)

        def _paged(token, path, params=None, app_secret=None, status=None,
                   **kw):
            params = dict(params or {})
            self.listed.append((path, params, dict(kw)))
            if on_read:
                on_read(path, status)
            lo = _filter_floor(params)
            hi = _filter_ceiling(params)
            items = [dict(i) for i in by_path.get(path, [])
                     if lo < _epoch(i['created_time'])
                     and (hi is None or _epoch(i['created_time']) < hi)]
            items.sort(key=lambda i: _epoch(i['created_time']), reverse=True)
            top = hi if hi is not None else int(
                datetime.now(timezone.utc).timestamp())
            if cut and top - lo > 24 * 3600 and len(items) > 2:
                return _then_cut(items[:2], status)
            return iter(items)
        return mock.patch.object(self.Client, '_iter_paged',
                                 side_effect=_paged)

    def _f1_reads(self):
        return [params for path, params, _kw in self.listed
                if path == 'F1/leads']

    def test_reconcile_progresses_through_truncated_window(self):
        """A listing Meta keeps cutting short is read in shorter windows
        until each fits, so a lead past the first partial read still comes
        in within the same run."""
        self._only_fixture_forms()
        t = fields.Datetime.now().replace(microsecond=0)
        items = self._deep_items(t)

        def _truncate(status):
            status['truncated'] = True

        with self._reconcile_listing({'F1/leads': items}, cut=_truncate), \
                self._patch_graph(fetch_lead=self._fetch()):
            self.Form._cron_reconcile()
        self.env.invalidate_all()
        for item in items:
            self.assertEqual(len(self._lead(item['id'])), 1, item['id'])
        self.assertTrue(any(_filter_ceiling(p) is not None
                            for p in self._f1_reads()))
        self.assertFalse(self.form.reconcile_resume_time)
        # The pass ended on whole reads, which grew the length back to the
        # whole window, stored as empty.
        self.assertFalse(self.form.reconcile_slice_hours)

        fetch = self._fetch()
        with self._reconcile_listing({'F1/leads': items}, cut=_truncate), \
                self._patch_graph(fetch_lead=fetch):
            self.Form._cron_reconcile()
        fetch.assert_not_called()

    def test_reconcile_progresses_across_deadline_cuts(self):
        """A form whose read never fits the budget narrows every run it
        starts with real budget, with the same floor and a shrinking
        ceiling, and reaches its oldest lead in four runs."""
        self._only_fixture_forms()
        t = fields.Datetime.now().replace(microsecond=0)
        forms = (self.form, self.form_a2, self.form_b, self.form2)
        for i, form in enumerate(forms):
            form.sudo().write(
                {'last_reconciled_time': t - timedelta(days=4 - i)})
        items = self._deep_items(t)
        clock = [0]

        def _cut(status):
            status['deadline_hit'] = True
            clock[0] = 10000

        form_class = type(self.Form)
        original_write = form_class.write
        stamped = []

        def _write(rec, vals):
            if 'last_reconciled_time' in vals:
                stamped.extend(rec.ids)
            return original_write(rec, vals)

        runs = []
        for run in range(1, 7):
            clock[0] = 0
            self.listed = []
            del stamped[:]
            # One fake page per batch of known ids, as one Graph page is.
            with self._patch_clock(clock), \
                    mock.patch.object(meta_lead_form, '_KNOWN_ID_CHUNK', 2), \
                    self._reconcile_listing({'F1/leads': items}, cut=_cut), \
                    self._patch_graph(fetch_lead=self._fetch()), \
                    mock.patch.object(form_class, 'write', autospec=True,
                                      side_effect=_write):
                self.Form._cron_reconcile()
            self.env.invalidate_all()
            first_read = self._f1_reads()[0]
            runs.append({
                'floor': _filter_floor(first_read),
                'ceiling': _filter_ceiling(first_read),
                'hours': self.form.reconcile_slice_hours,
                'resume': self.form.reconcile_resume_time,
                'stamped': list(stamped),
                'deep': bool(self._lead('LGBI_DEEP')),
            })
            # Test-only: spread the stamps so the next run's order never
            # depends on two stamps falling in the same second.
            for k, form_id in enumerate(stamped):
                self.Form.browse(form_id).sudo().write({
                    'last_reconciled_time':
                        t + timedelta(hours=run, seconds=k)})
            if runs[-1]['deep']:
                break

        self.assertEqual(len(runs), 4)
        p = runs[0]['resume']
        self.assertTrue(p)
        floor = self._dt_epoch(p) - self.Form._backfill_overlap_seconds()
        self.assertEqual([r['floor'] for r in runs], [floor] * 4)
        self.assertEqual(
            [r['ceiling'] for r in runs],
            [None, self._dt_epoch(p + timedelta(hours=84)),
             self._dt_epoch(p + timedelta(hours=42)),
             self._dt_epoch(p + timedelta(hours=21))])
        ceilings = [float('inf') if r['ceiling'] is None else r['ceiling']
                    for r in runs]
        self.assertEqual(ceilings, sorted(ceilings, reverse=True))
        self.assertEqual(len(set(ceilings)), 4)
        self.assertEqual([r['hours'] for r in runs], [84, 42, 21, 21])
        self.assertEqual([r['resume'] for r in runs],
                         [p, p, p, p + timedelta(hours=21)])
        others = [self.form_a2.id, self.form_b.id, self.form2.id]
        self.assertEqual([r['stamped'] for r in runs],
                         [[self.form.id], others, [self.form.id], others])
        self.assertEqual([r['deep'] for r in runs],
                         [False, False, False, True])
        self.assertEqual(len(self._lead('LGBI_H120')), 1)

    def test_reconcile_later_form_cut_keeps_length(self):
        """A later form cut with less than half the budget left keeps its
        read length and its place; with at least half left it narrows."""
        self._only_fixture_forms()
        t = fields.Datetime.now().replace(microsecond=0)
        items = [self._item('LGBI_LF1',
                            self._meta_time(t - timedelta(hours=2)))]
        forms = (self.form_a2, self.form, self.form_b, self.form2)
        for label, spent in (('little budget left', 27),
                             ('half the budget left', 10)):
            with self.subTest(label):
                for i, form in enumerate(forms):
                    form.sudo().write({
                        'last_reconciled_time': t - timedelta(days=4 - i),
                        'reconcile_resume_time': False,
                        'reconcile_slice_hours': False})
                clock = [0]

                def _on_read(path, status, spent=spent):
                    if path == 'FBIA2/leads':
                        clock[0] = spent
                    elif path == 'F1/leads':
                        status['deadline_hit'] = True
                        clock[0] = 10000

                self.listed = []
                with self._patch_clock(clock), \
                        self._reconcile_listing({'F1/leads': items},
                                                on_read=_on_read), \
                        self._patch_graph(fetch_lead=self._fetch()):
                    self.Form._cron_reconcile()
                self.env.invalidate_all()
                self.assertEqual(self._listed_paths(),
                                 ['FBIA2/leads', 'F1/leads'])
                self.assertEqual(self.form.last_reconciled_time,
                                 t - timedelta(days=3))
                resume = self.form.reconcile_resume_time
                self.assertTrue(resume)
                self.assertLessEqual(
                    abs((resume - (t - timedelta(days=7))).total_seconds()),
                    60)
                if spent == 27:
                    self.assertFalse(self.form.reconcile_slice_hours)
                    clock[0] = 0
                    self.listed = []
                    with self._patch_clock(clock), \
                            self._reconcile_listing({'F1/leads': items},
                                                    on_read=_on_read), \
                            self._patch_graph(fetch_lead=self._fetch()):
                        self.Form._cron_reconcile()
                    self.env.invalidate_all()
                    self.assertEqual(self._listed_paths()[0], 'F1/leads')
                    self.assertEqual(self.form.reconcile_slice_hours, 84)
                else:
                    self.assertEqual(self.form.reconcile_slice_hours, 84)

    def test_reconcile_restarts_stale_pass(self):
        """A stored place older than twice the window continues from twice
        the window back, and the pass then runs to the present."""
        self._only_fixture_forms()
        now = fields.Datetime.now().replace(microsecond=0)
        self.form.sudo().write(
            {'reconcile_resume_time': now - timedelta(days=30)})
        with self._reconcile_listing({}), self._patch_graph(), \
                self.assertLogs(
                    'odoo.addons.meta_lead_ads.models.meta_lead_form',
                    level='WARNING') as logs:
            self.Form._cron_reconcile()
        self.env.invalidate_all()
        expected = (self._dt_epoch(now - timedelta(days=14))
                    - self.Form._backfill_overlap_seconds())
        self.assertLessEqual(abs(_filter_floor(self._f1_reads()[0])
                                 - expected), 60)
        self.assertFalse(self.form.reconcile_resume_time)
        self.assertGreaterEqual(self.form.reconcile_pass_end, now)
        self.assertTrue(any('stale reconcile position' in line
                            for line in logs.output))

    def test_reconcile_late_turn_reads_the_gap(self):
        """A form whose last pass ended before the window starts the next
        pass there, so a late lead in between is still read; never more
        than twice the window back."""
        self._only_fixture_forms()
        now = fields.Datetime.now().replace(microsecond=0)
        overlap = self.Form._backfill_overlap_seconds()
        gap_lead = self._item('LGBI_GAP',
                              self._meta_time(now - timedelta(days=9)))
        self.form.sudo().write(
            {'reconcile_pass_end': now - timedelta(days=10)})
        with self._reconcile_listing({'F1/leads': [gap_lead]}), \
                self._patch_graph(fetch_lead=self._fetch()):
            self.Form._cron_reconcile()
        self.env.invalidate_all()
        self.assertLessEqual(
            abs(_filter_floor(self._f1_reads()[0])
                - (self._dt_epoch(now - timedelta(days=10)) - overlap)), 60)
        self.assertEqual(len(self._lead('LGBI_GAP')), 1)
        self.assertFalse(self.form.reconcile_resume_time)
        self.assertGreaterEqual(self.form.reconcile_pass_end, now)

        with self.subTest('recent pass end keeps the window'):
            self.listed = []
            with self._reconcile_listing({}), self._patch_graph():
                self.Form._cron_reconcile()
            self.env.invalidate_all()
            self.assertLessEqual(
                abs(_filter_floor(self._f1_reads()[0])
                    - (self._dt_epoch(now - timedelta(days=7)) - overlap)),
                60)

        with self.subTest('capped at twice the window'):
            self.form.sudo().write(
                {'reconcile_pass_end': now - timedelta(days=40)})
            self.listed = []
            with self._reconcile_listing({}), self._patch_graph(), \
                    self.assertLogs(
                        'odoo.addons.meta_lead_ads.models.meta_lead_form',
                        level='WARNING') as logs:
                self.Form._cron_reconcile()
            self.env.invalidate_all()
            self.assertLessEqual(
                abs(_filter_floor(self._f1_reads()[0])
                    - (self._dt_epoch(now - timedelta(days=14)) - overlap)),
                60)
            self.assertTrue(any('more than twice its window' in line
                                for line in logs.output))
