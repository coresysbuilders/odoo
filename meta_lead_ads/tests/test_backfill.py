# Meta Lead Ads Integration for Odoo — © 2026 CoreSys Builders. All rights reserved.
# Licensed under the Odoo Proprietary License v1.0 (OPL-1).
# Unauthorized copying, redistribution, or resale of this software, in whole or in
# part, via any medium, is strictly prohibited and constitutes a license violation.
# OPL-1: https://www.odoo.com/documentation/18.0/legal/licenses.html#odoo-apps

"""Tests for the cron backfill sweep.

``meta.lead.form._cron_backfill`` / ``_backfill_one_form`` page through each
form's leads and hand them to ``meta.lead.ingest.ingest_leadgen``. Both
``meta.graph.client._iter_paged`` and ``ingest_leadgen`` are patched on their
class (``mock.patch.object`` can't patch a recordset), so no HTTP call is made.

Covers ordering, cursor handling, scope, lookback, per-form isolation, and the
same-second and malformed-timestamp edge cases.
"""
import json
from datetime import datetime, timedelta, timezone
from unittest import mock

from odoo import fields
from odoo.tests.common import TransactionCase, tagged

from odoo.addons.meta_lead_ads.models.exceptions import (
    MetaTransientError, MetaPermanentError)


def _ts(created_time):
    """Convert a Meta ``created_time`` to the naive UTC string the cursor stores."""
    dt = datetime.fromisoformat(created_time)
    return fields.Datetime.to_string(
        dt.astimezone(timezone.utc).replace(tzinfo=None))


def _filter_floor(params):
    floor = None
    for key in ('since', 'time_created'):
        if key in params:
            return int(params[key])
    filtering = params.get('filtering') or []
    if isinstance(filtering, str):
        filtering = json.loads(filtering)
    for filt in filtering:
        if isinstance(filt, dict) and filt.get('value'):
            floor = int(filt['value'])
    return floor


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

        def _per_form(token, path, params=None, app_secret=None):
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

        def _capture(token, path, params=None, app_secret=None):
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

        def _maybe_raise(token, path, params=None, app_secret=None):
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

    def test_malformed_created_time_skipped(self):
        """A lead with a bad created_time is skipped and logged without secrets; the rest still sync."""
        before_logs = self.Log.search_count(
            [('status', '=', 'failed'), ('trigger', '=', 'cron')])
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
        self.assertIn('LG_GOOD', called_ids)
        self.form.invalidate_recordset(['last_synced_time'])
        # Cursor comes from the good lead only.
        self.assertEqual(
            fields.Datetime.to_string(self.form.last_synced_time),
            _ts('2026-06-07T09:00:00+0000'))
        after_logs = self.Log.search_count(
            [('status', '=', 'failed'), ('trigger', '=', 'cron')])
        self.assertEqual(after_logs, before_logs + 1)
        bad_log = self.Log.search(
            [('status', '=', 'failed'), ('trigger', '=', 'cron')],
            order='id desc', limit=1)
        err = (bad_log.error_message or '')
        for secret in ('tok_acct', 'tok_test', 'secret_test',
                       'not-an-iso-timestamp'):
            self.assertNotIn(secret, err)

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

        def _capture_floor(token, path, params=None, app_secret=None):
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

        def _ingest(self_ingest, page, leadgen_id, trigger='cron', raw=None):
            if leadgen_id == 'LG_3':
                raise MetaTransientError('temporary on lead 3')
            return self._fake_lead()

        with mock.patch.object(self.ClientClass, '_iter_paged',
                               return_value=iter(payload)), \
             mock.patch.object(self.IngestClass, 'ingest_leadgen',
                               autospec=True, side_effect=_ingest) as m_ingest:
            # Same per-form savepoint that _cron_backfill wraps around each form.
            with self.env.cr.savepoint():
                self.form._backfill_one_form()
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

    def test_permanent_midform_fails_form(self):
        """A permanent error on lead 3 rolls back the whole form's cursor for this sweep."""
        seed = False
        self.form.last_synced_time = seed
        t1, t2, t3 = ('2026-06-02T08:00:00+0000',
                      '2026-06-02T09:00:00+0000',
                      '2026-06-02T10:00:00+0000')
        payload = [
            self._lead_payload('LG_P1', t1),
            self._lead_payload('LG_P2', t2),
            self._lead_payload('LG_P3', t3),
        ]

        def _ingest(self_ingest, page, leadgen_id, trigger='cron', raw=None):
            if leadgen_id == 'LG_P3':
                raise MetaPermanentError('permanent on lead 3')
            return self._fake_lead()

        with mock.patch.object(self.ClientClass, '_iter_paged',
                               return_value=iter(payload)), \
             mock.patch.object(self.IngestClass, 'ingest_leadgen',
                               autospec=True, side_effect=_ingest):
            # Same as _cron_backfill: the per-form savepoint rolls back on a
            # permanent error.
            try:
                with self.env.cr.savepoint():
                    self.form._backfill_one_form()
            except MetaPermanentError:
                pass
        self.form.invalidate_recordset(['last_synced_time'])
        self.assertFalse(self.form.last_synced_time)
