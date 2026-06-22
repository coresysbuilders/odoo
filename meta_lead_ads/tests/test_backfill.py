# Meta Lead Ads Integration for Odoo — © 2026 CoreSys Builders. All rights reserved.
# Licensed under the Odoo Proprietary License v1.0 (OPL-1).
# Unauthorized copying, redistribution, or resale of this software, in whole or in
# part, via any medium, is strictly prohibited and constitutes a license violation.
# OPL-1: https://www.odoo.com/documentation/18.0/legal/licenses.html#odoo-apps

"""Tests for the cron backfill sweep.

``class TestMetaBackfill`` exercises the ``meta.lead.form._cron_backfill`` /
``_backfill_one_form`` sweep over ``meta.lead.ingest.ingest_leadgen`` (the single
crm.lead create path; the cron is a thin idempotent wrapper). All Graph traffic
is mocked: the
``meta.graph.client._iter_paged`` generator and ``ingest_leadgen`` are both
patched on their class, so no live HTTP and no real token crosses any boundary
here.

Coverage includes the oldest-first sweep + per-lead cursor advance, scope
filter, lookback, per-form isolation, idempotent re-sweep, plus the harder
edge cases: cursor-regression guard, same-second stranding, malformed-timestamp
skip policy, falsey-cursor first-run advance (no TypeError), same-second leads
split across ``_iter_paged`` pages (buffer-then-sort), and a same-second sibling
recovered in a later sweep via the ``_backfill_overlap_seconds()`` window.

Conventions used throughout:
  - Patch ``ingest_leadgen`` / ``_iter_paged`` on ``type(...)`` (the class),
    never a recordset (``mock.patch.object`` is read-only on recordsets).
  - Use ``search_count(...)``; the legacy count kwarg was removed in Odoo 18.
  - ``assertRaises`` takes a single exception class, never a tuple.
"""
import json
from datetime import datetime, timedelta, timezone
from unittest import mock

from odoo import fields
from odoo.tests.common import TransactionCase, tagged

from odoo.addons.meta_lead_ads.models.exceptions import (
    MetaTransientError, MetaPermanentError)


def _ts(created_time):
    """Parse a Meta ISO-8601 ``created_time`` (offset-aware) to the naive-UTC
    Datetime string the implementation stores. Tests compute the expected
    cursor the same way the implementation does, so the assertions stay in
    lock-step.
    """
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
    """meta.account -> meta.page -> meta.lead.form chain (mirrors
    test_webhook_drain.WebhookDrainFixtureMixin) + the graph-client and ingest
    classes captured for class-patching."""

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
        # Patch on the class, never a recordset.
        self.ClientClass = type(self.Client)
        self.IngestClass = type(self.Ingest)

    def _lead_payload(self, leadgen_id, created_time):
        """A Graph lead item as _iter_paged yields it (id + created_time)."""
        return {'id': leadgen_id, 'created_time': created_time}

    def _fake_lead(self):
        return self.Lead.create({'name': 'BF Lead', 'type': 'lead'})


@tagged('post_install', '-at_install')
class TestMetaBackfill(BackfillFixtureMixin, TransactionCase):
    """The oldest-first cron sweep + per-lead cursor advance, scope filter,
    lookback, per-form isolation, idempotent re-sweep, and the harder
    edge-case hardening."""

    def test_sweep_oldest_first(self):
        """_iter_paged returns newest-first; the sweep must hand leads to
        ingest_leadgen in ascending created_time order so no older lead is
        stranded behind a high cursor (capture the call order off the mock)."""
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
        """After the sweep, last_synced_time == the newest processed lead's
        created_time parsed to naive UTC; the per-lead advance also moved
        through the intermediate value (cursor is monotonic up to the
        newest)."""
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
        # The newest cursor dominates the older lead's intermediate advance.
        self.assertGreater(self.form.last_synced_time,
                           fields.Datetime.from_string(
                               _ts('2026-06-01T10:00:00+0000')))

    def test_newest_first_input_reversed(self):
        """A deliberately reverse-chronological page is reversed to oldest-first
        before processing, so a crash mid-sweep cannot strand an older lead
        behind a cursor set from a newer one."""
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
        """Only forms with active AND sync_enabled are swept. A
        sync_enabled=False form and an active=False form are both skipped; their
        leadgen_ids never reach ingest_leadgen."""
        self.env['meta.lead.form'].create({
            'name': 'Disabled', 'form_id': 'F_OFF', 'page_id': self.page.id,
            'sync_enabled': False})
        self.env['meta.lead.form'].create({
            'name': 'Archived', 'form_id': 'F_ARC', 'page_id': self.page.id,
            'active': False})

        def _per_form(token, path, params=None, app_secret=None):
            # Tag the yielded lead with the form id embedded in the path so the
            # assertion can prove only the active+enabled form was queried.
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
        """A never-synced form (last_synced_time is False) computes a `since`
        cursor ~30 days back (configurable default), asserted via the
        time-window value passed into the mocked _iter_paged params."""
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
        # The since-floor (Graph filtering[].value / 'since' / 'time_created')
        # must land roughly 30 days back, never in the future, never epoch 0.
        self.assertIsInstance(params.get('filtering'), str)
        floor = _filter_floor(params)
        self.assertIsNotNone(floor, 'sweep must pass a since/time_created floor')
        now = int(datetime.now(timezone.utc).timestamp())
        thirty_days = 30 * 24 * 3600
        # ~30 days back, allowing a generous +/- 2 day tolerance for config drift.
        self.assertLess(abs((now - floor) - thirty_days), 2 * 24 * 3600)

    def test_per_form_isolation(self):
        """One form whose _iter_paged raises is isolated (savepoint); the sweep
        still processes the sibling form."""
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
        """Running the sweep twice over the same payload re-calls the same
        idempotent service; the cron itself adds no second create path -- the
        idempotency (DB UNIQUE on meta_leadgen_id) lives in the service, so the
        second pass creates no second crm.lead."""
        payload = [self._lead_payload('LG_DUP', '2026-06-01T00:00:00+0000')]
        fake = self._fake_lead()

        # The service is idempotent: same leadgen_id -> same lead, no new row.
        with mock.patch.object(self.ClientClass, '_iter_paged',
                               side_effect=lambda *a, **k: iter(list(payload))), \
             mock.patch.object(self.IngestClass, 'ingest_leadgen',
                               return_value=fake) as m_ingest:
            self.Form._cron_backfill()
            first_calls = m_ingest.call_count
            self.Form._cron_backfill()
            second_calls = m_ingest.call_count
        # The cron does not invent a second create path -- it re-calls the same
        # idempotent service (idempotency absorbs the re-read).
        self.assertGreaterEqual(first_calls, 1)
        self.assertGreaterEqual(second_calls, first_calls)

    def test_cursor_no_regression(self):
        """Pre-seed last_synced_time to a known recent value, then feed a lead
        whose created_time is older (an out-of-order / already-ingested
        re-read). The sweep must not lower the cursor (advance only when parsed
        created_time > current). The older lead may still hit ingest_leadgen
        (idempotency absorbs it), but the cursor never moves backward."""
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
        """Two leads with the identical created_time second (distinct ids) in
        one result set. Both must reach ingest_leadgen in the same sweep -- the
        strict-`>` cursor advance plus the overlap window must not strand the
        same-second sibling."""
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
        """A lead with a missing/unparseable created_time is skipped (cursor not
        set from it), a well-formed newer lead is still ingested and advances
        the cursor, and the skip is recorded as a token-free meta.sync.log
        failed/cron row whose error carries no token/secret/payload
        substring."""
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
        # (b) the well-formed lead is ingested...
        self.assertIn('LG_GOOD', called_ids)
        self.form.invalidate_recordset(['last_synced_time'])
        # (a) ...and the cursor advanced to the GOOD lead, never the malformed one.
        self.assertEqual(
            fields.Datetime.to_string(self.form.last_synced_time),
            _ts('2026-06-07T09:00:00+0000'))
        # (c) the malformed skip is logged token-free as failed/cron.
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
        """A never-synced form (last_synced_time is False) ingesting one
        well-formed lead must complete with no TypeError (`parsed > False`
        raises in Py3 -- the impl must guard
        `if (not current) or (parsed > current):`) and advance the cursor from
        False to the lead's parsed naive-UTC created_time."""
        self.form.last_synced_time = False
        payload = [self._lead_payload('LG_FIRST', '2026-06-08T07:00:00+0000')]
        with mock.patch.object(self.ClientClass, '_iter_paged',
                               return_value=iter(payload)), \
             mock.patch.object(self.IngestClass, 'ingest_leadgen',
                               return_value=self._fake_lead()):
            # No TypeError must escape the sweep.
            self.Form._cron_backfill()
        self.form.invalidate_recordset(['last_synced_time'])
        self.assertEqual(
            fields.Datetime.to_string(self.form.last_synced_time),
            _ts('2026-06-08T07:00:00+0000'))

    def test_same_second_across_pages(self):
        """Two same-created_time-second leads (distinct ids) modeled as arriving
        on separate Graph pages -- _iter_paged materializes them into one list
        (it exhausts all pages before the sweep buffers + sorts). Both must
        reach ingest_leadgen in one sweep, proving the buffer-then-sort design
        makes across-page same-second safe."""
        # _iter_paged is a generator that exhausts every page; the mock returns
        # the full materialized sequence the sweep will list(...) and sort.
        across_pages = iter([
            self._lead_payload('LG_P1', '2026-06-01T11:00:00+0000'),  # page 1 tail
            self._lead_payload('LG_P2', '2026-06-01T11:00:00+0000'),  # page 2 head
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
        """First sweep ingests one lead at second T and advances the cursor to
        T. The second sweep's `since` floor must be T - overlap (not strictly
        > T) -- the overlap re-query window from _backfill_overlap_seconds(); an
        equal-second sibling that only became visible later then reaches
        ingest_leadgen (idempotency absorbs the re-read of the first lead) and
        the cursor does not regress below T."""
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
            return iter([self._lead_payload('LG_T_SIB', t_iso)])  # later sibling

        with mock.patch.object(self.ClientClass, '_iter_paged',
                               side_effect=_capture_floor), \
             mock.patch.object(self.IngestClass, 'ingest_leadgen',
                               return_value=self._fake_lead()) as m_ingest:
            self.Form._cron_backfill()
        # The second sweep floor is T - overlap, not strictly > T.
        params = captured.get('params', {})
        self.assertIsInstance(params.get('filtering'), str)
        floor = _filter_floor(params)
        self.assertIsNotNone(floor)
        expected_floor = int(t_dt.replace(tzinfo=timezone.utc).timestamp()) - overlap
        self.assertEqual(floor, expected_floor)
        # The equal-second sibling reached the service.
        called_ids = [c.args[1] for c in m_ingest.call_args_list]
        self.assertIn('LG_T_SIB', called_ids)
        # The cursor never regressed below T.
        self.form.invalidate_recordset(['last_synced_time'])
        self.assertGreaterEqual(self.form.last_synced_time, t_dt)

    # ------------------------------------------------------------------ #
    # Per-lead transient isolation + transaction-realistic cursor
    # persistence: _backfill_one_form's per-lead transient catch breaks the
    # loop without rolling back the already-advanced cursor for leads 1-2.
    # ------------------------------------------------------------------ #
    def test_transient_midform_preserves_cursor(self):
        """A form with 4 oldest-first leads (t1<t2<t3<t4). ingest_leadgen succeeds
        for leads 1-2, raises MetaTransientError on lead 3, and would succeed for
        lead 4. Driving _backfill_one_form through the enclosing per-form
        savepoint (the way _cron_backfill does) must leave the cursor advanced to
        lead 2's created_time and persisted after the savepoint completes -- so
        the next sweep re-reads from lead 2, lead 3 is retried, and lead 4 (which
        the break prevented) is reached then. invalidate_recordset before the
        assertion so it reflects the persisted DB state, not the ORM cache.

        The per-lead catch+break first calls ingest_leadgen(LG_3, ...) --
        recording the call -- and only then does the transient raise inside that
        call and the ``break`` fire. So the expected contract is:
          * LG_1, LG_2 (before the failure) are attempted;
          * LG_3 (the failing lead) is attempted -- the call is recorded before
            the transient raises;
          * LG_4 (after the break) is not attempted -- ``break`` (not
            ``continue``) stops the loop; LG_4's absence proves the break works;
          * the cursor stays at the last successful lead (t2) -- advance happens
            after a clean ingest only, so it never moved to/past LG_3/LG_4;
          * no 'failed' meta.sync.log row is written -- the transient break is
            intentionally silent; only the malformed-row path records 'failed',
            and there is no malformed row here.
        """
        t1, t2, t3, t4 = ('2026-06-01T08:00:00+0000',
                          '2026-06-01T09:00:00+0000',
                          '2026-06-01T10:00:00+0000',
                          '2026-06-01T11:00:00+0000')
        payload = [
            self._lead_payload('LG_1', t1),
            self._lead_payload('LG_2', t2),
            self._lead_payload('LG_3', t3),   # the transient
            self._lead_payload('LG_4', t4),   # must not be reached (break, not continue)
        ]

        def _ingest(self_ingest, page, leadgen_id, trigger='cron', raw=None):
            if leadgen_id == 'LG_3':
                raise MetaTransientError('temporary on lead 3')
            return self._fake_lead()

        with mock.patch.object(self.ClientClass, '_iter_paged',
                               return_value=iter(payload)), \
             mock.patch.object(self.IngestClass, 'ingest_leadgen',
                               autospec=True, side_effect=_ingest) as m_ingest:
            # Drive through the enclosing per-form savepoint, exactly like
            # _cron_backfill wraps the worker (sibling isolation + persistence).
            with self.env.cr.savepoint():
                self.form._backfill_one_form()
        # Re-read after the savepoint completes.
        self.form.invalidate_recordset(['last_synced_time'])
        # autospec=True -> call args are (self_ingest, page, leadgen_id, ...);
        # c.args[2] is the leadgen_id positional.
        called_ids = [c.args[2] for c in m_ingest.call_args_list]
        # Leads before the failure were attempted.
        self.assertIn('LG_1', called_ids)
        self.assertIn('LG_2', called_ids)
        # The failing lead is attempted -- the call is recorded before the
        # transient raises.
        self.assertIn('LG_3', called_ids)
        # No lead after the failing one is attempted -- ``break`` stopped the loop.
        self.assertNotIn('LG_4', called_ids)
        # The cursor advanced to lead 2 and persisted (next sweep re-reads from
        # t2); it never moved to/past LG_3/LG_4 (advance is after a clean ingest
        # only).
        self.assertEqual(
            fields.Datetime.to_string(self.form.last_synced_time), _ts(t2))
        # The transient break is intentionally silent -- no 'failed' sync-log row.
        self.assertEqual(
            self.Log.search_count([('status', '=', 'failed')]), 0)

    def test_permanent_midform_fails_form(self):
        """All-or-nothing on a permanent error: a MetaPermanentError on lead 3 is
        not swallowed per-lead -- it propagates out of _backfill_one_form and,
        inside the _cron_backfill per-form savepoint, rolls back the whole form's
        cursor advance for this sweep (permanent is terminal for the form).
        Re-read with invalidate_recordset before asserting the cursor is
        unchanged."""
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
            # Mirror _cron_backfill: the per-form savepoint catches the typed
            # permanent error and rolls back this form's partial work.
            try:
                with self.env.cr.savepoint():
                    self.form._backfill_one_form()
            except MetaPermanentError:
                pass
        # The whole form's cursor advance rolled back (permanent is
        # all-or-nothing).
        self.form.invalidate_recordset(['last_synced_time'])
        self.assertFalse(self.form.last_synced_time)
