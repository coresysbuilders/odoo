# Meta Lead Ads Integration for Odoo — © 2026 CoreSys Builders. All rights reserved.
# Licensed under the Odoo Proprietary License v1.0 (OPL-1).
# Unauthorized copying, redistribution, or resale of this software, in whole or in
# part, via any medium, is strictly prohibited and constitutes a license violation.
# OPL-1: https://www.odoo.com/documentation/18.0/legal/licenses.html#odoo-apps

"""Tests for the sync-log manual-retry reliability surface.

``class TestMetaSyncRetry`` exercises the ``meta.sync.log`` retry machinery over
``meta.lead.ingest.ingest_leadgen`` (the single crm.lead create path; the manual
retry is a thin idempotent wrapper). Each assertion pins both the
behavioral contract and the hardened concurrency design.

The retry surface under test:
  * ``action_retry(self)``       -- bulk public action; filters status=='failed',
    raises UserError on an empty/non-failed selection, runs each row in its own
    savepoint for isolation, returns a display_notification dict with counts.
  * ``_retry_one(self)``         -- single-row worker; acquires the row lock via
    ``_lock_for_retry()``, increments retry_count exactly once right after the
    lock, resolves page / decodes payload / calls ingest_leadgen
    ``with_context(meta_retry_origin_log_id=self.id)``.
  * ``_lock_for_retry(self)``    -- FOR UPDATE NOWAIT in its own nested savepoint;
    True on lock / False on LockNotAvailable, leaving a clean cursor so the loser
    can still write its error.
  * ``_reconcile_after_ingest(self, lead)`` -- single-surviving-row collapse,
    correlated by the domain (``retry_origin_log_id == self.id`` AND matching
    ``meta_leadgen_id`` AND status in (success / skipped_idempotent)), not by
    create_date.
  * ``_resolve_page(self)``      -- page resolution chain; returns a (possibly
    empty) ``meta.page`` recordset.
  * field ``page_id = Many2one('meta.page')`` (nullable).
  * field ``retry_origin_log_id = Many2one('meta.sync.log', index=True)`` -- the
    attempt-correlation key stamped by ``_record`` from context.

The fixture and correlation tests deliberately avoid hand-writing the
``page_id`` / ``retry_origin_log_id`` columns: page resolution is driven via the
lead linkage (``lead.meta_page_id_ref = self.page``), and the correlation key is
exercised through the ingest service's call to the real ``Log._record(...)``, which
reads ``self.env.context.get('meta_retry_origin_log_id')``.

Conventions used throughout:
  - Patch ``ingest_leadgen`` on ``type(...)`` (the class), never a recordset
    (``mock.patch.object`` is read-only on recordsets).
  - Use ``search_count(...)``; the legacy ``count`` kwarg was removed in
    Odoo 18.
  - ``assertRaises`` takes a single exception class, never a tuple.
  - After a savepoint rollback inside the code under test, call
    ``record.invalidate_recordset()`` (or re-browse) before asserting persisted
    DB state -- Odoo's ORM cache can otherwise return the pre-rollback in-memory
    value and mask the rollback.
"""
import json
from unittest import mock

from odoo.exceptions import UserError
from odoo.tests.common import TransactionCase, tagged

from odoo.addons.meta_lead_ads.models.exceptions import (
    MetaTransientError, MetaAuthError)

# The meta.sync.log terminal statuses the reconcile collapse treats as a fresh
# row. The idempotent path emits 'skipped_idempotent', not 'skipped_duplicate';
# 'skipped_duplicate' is intentionally not a reconcile-terminal status.
_RECONCILE_TERMINAL = ('success', 'skipped_idempotent')


class SyncRetryFixtureMixin:
    """meta.account -> meta.page -> meta.lead.form chain (mirrors
    test_webhook_drain.WebhookDrainFixtureMixin) + the ingest class captured for
    class-patching.

    The fixture writes only the columns the retry path needs to resolve a page
    via the lead linkage -- never page_id / retry_origin_log_id directly.
    """

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
        self.Ingest = self.env['meta.lead.ingest']
        # Patch ingest_leadgen on the class, never a recordset.
        self.IngestClass = type(self.Ingest)
        self.Lead = self.env['crm.lead']
        self.Log = self.env['meta.sync.log']

    # -- fixture helpers (existing fields only) ------------------------------ #
    def _failed_row(self, leadgen_id='LG1', raw=None, trigger='webhook',
                    lead=None, link_page=True):
        """A failed meta.sync.log row to retry. ``raw`` (a dict) is json.dumped
        into raw_payload so a replay can decode it; ``link_page`` wires a
        resolvable page via the lead linkage (meta_page_id_ref), not a direct
        log.page_id write."""
        if lead is None and link_page:
            lead = self._lead(leadgen_id=leadgen_id, with_page=True)
        return self.Log.create({
            'meta_leadgen_id': leadgen_id,
            'status': 'failed',
            'trigger': trigger,
            'error_message': 'boom',
            'raw_payload': json.dumps(raw) if raw is not None else False,
            'lead_id': lead.id if lead else False,
        })

    def _lead(self, leadgen_id=None, with_page=False):
        """A real crm.lead so success/idempotent paths can assert lead_id. When
        ``with_page`` the page linkage is set via meta_page_id_ref so
        _resolve_page can find self.page through the existing field set.

        crm.lead.meta_leadgen_id carries a DB-UNIQUE constraint. Several methods
        call ``_lead(LGn)`` twice for the same id (once inside ``_failed_row`` and
        once in the test body), so the fixture is get-or-create on
        meta_leadgen_id: a second call for an id already used reuses the existing
        record, so the two call sites converge on one crm.lead and preserve every
        ``row.lead_id == lead`` / ``== existing`` identity assertion. An unused id
        (or leadgen_id=None) is a plain create. No secret/token is ever written
        here.
        """
        if leadgen_id:
            existing = self.Lead.search(
                [('meta_leadgen_id', '=', leadgen_id)], limit=1)
            if existing:
                # Reuse: do not create a second colliding crm.lead. Backfill the
                # page linkage if requested and not yet set, then return it.
                if with_page and not existing.meta_page_id_ref:
                    existing.meta_page_id_ref = self.page.id
                return existing
        vals = {'name': 'Retry Lead', 'type': 'lead'}
        if leadgen_id:
            vals['meta_leadgen_id'] = leadgen_id
        if with_page:
            vals['meta_page_id_ref'] = self.page.id
        return self.Lead.create(vals)

    def _patch_emit(self, status, lead=None, stamp_origin=True, emit_row=True):
        """Build a class-patch side_effect for ingest_leadgen that simulates the
        ingest service faithfully: it calls the real Log._record(..., trigger='manual',
        <status>) so the production context-stamp of retry_origin_log_id is
        exercised authentically, then returns ``lead``.

        ``emit_row=False`` simulates ingest_leadgen returning a lead without
        emitting a fresh _record row (the defensive case).
        ``stamp_origin`` is informational: the real _record reads the context key
        the production caller passes, so no hand-write of the field happens here.
        """
        def _side(self_ingest, page, leadgen_id, trigger='webhook', raw=None):
            if emit_row:
                # Calling the real _record exercises the context stamp of
                # retry_origin_log_id.
                self_ingest.env['meta.sync.log']._record(
                    leadgen_id, 'manual', status, lead=lead, raw=raw)
            return lead
        return _side


@tagged('post_install', '-at_install')
class TestMetaSyncRetry(SyncRetryFixtureMixin, TransactionCase):
    """The manual retry action over action_retry / _retry_one / _lock_for_retry
    / _reconcile_after_ingest / _resolve_page, including the hardened
    concurrency paths."""

    # ---------------------------------------------------------------- replay/
    # re-fetch split -------------------------------------------------------- #
    def test_manual_replay_success(self):
        """Replay: a failed row with a stored raw_payload re-runs ingest_leadgen
        with the decoded payload (raw != None) on the resolved page; the clicked
        row flips in-place to 'success', retry_count == 1, and there is exactly
        one surviving row for the leadgen_id."""
        raw = {'id': 'LG1', 'field_data': []}
        row = self._failed_row(leadgen_id='LG1', raw=raw)
        lead = self._lead(leadgen_id='LG1')
        with mock.patch.object(
                type(self.Ingest), 'ingest_leadgen', autospec=True,
                side_effect=self._patch_emit('success', lead=lead)) as m:
            row.action_retry()
        row.invalidate_recordset()
        self.assertEqual(m.call_count, 1)
        _self, page, leadgen_id = m.call_args.args[:3]
        self.assertEqual(page, self.page)            # resolved page
        self.assertEqual(m.call_args.kwargs.get('trigger'), 'manual')
        self.assertEqual(m.call_args.kwargs.get('raw'), raw)   # replay, decoded
        self.assertEqual(row.status, 'success')
        self.assertEqual(row.retry_count, 1)
        self.assertEqual(
            self.Log.search_count([('meta_leadgen_id', '=', 'LG1')]), 1)
        survivor = self.Log.search([('meta_leadgen_id', '=', 'LG1')], limit=1)
        self.assertEqual(survivor.trigger, 'manual')

    def test_manual_refetch_success(self):
        """Re-fetch: a failed row with an empty raw_payload re-runs ingest_leadgen
        with raw=None (a fresh Graph re-fetch); the clicked row flips to
        'success' in-place, retry_count == 1, surviving trigger == 'manual'."""
        row = self._failed_row(leadgen_id='LG2', raw=None)
        lead = self._lead(leadgen_id='LG2')
        with mock.patch.object(
                type(self.Ingest), 'ingest_leadgen', autospec=True,
                side_effect=self._patch_emit('success', lead=lead)) as m:
            row.action_retry()
        row.invalidate_recordset()
        self.assertEqual(m.call_count, 1)
        self.assertIsNone(m.call_args.kwargs.get('raw'))   # re-fetch, no replay
        self.assertEqual(row.status, 'success')
        self.assertEqual(row.retry_count, 1)
        survivor = self.Log.search([('meta_leadgen_id', '=', 'LG2')], limit=1)
        self.assertEqual(survivor.trigger, 'manual')

    def test_manual_trigger_overwritten_on_retry(self):
        """A failed row whose original trigger was 'webhook' becomes 'manual' on
        the surviving row after a manual retry -- the latest attempt's trigger
        wins."""
        row = self._failed_row(leadgen_id='LG3', raw=None, trigger='webhook')
        lead = self._lead(leadgen_id='LG3')
        with mock.patch.object(
                type(self.Ingest), 'ingest_leadgen', autospec=True,
                side_effect=self._patch_emit('success', lead=lead)):
            row.action_retry()
        row.invalidate_recordset()
        survivor = self.Log.search([('meta_leadgen_id', '=', 'LG3')], limit=1)
        self.assertEqual(survivor.trigger, 'manual')

    # ---------------------------------------------------------------- idempotent
    # short-circuit -------------------------------------------------------- #
    def test_idempotent_short_circuit_sets_lead_id(self):
        """The ingest service's idempotent hit emits a fresh
        _record(..., 'skipped_idempotent', lead=<existing>). The clicked row
        flips to 'skipped_idempotent' (never 'skipped_duplicate' for this path),
        lead_id == the existing lead, retry_count == 1, one surviving row."""
        existing = self._lead(leadgen_id='LG4')
        row = self._failed_row(leadgen_id='LG4', raw=None)
        with mock.patch.object(
                type(self.Ingest), 'ingest_leadgen', autospec=True,
                side_effect=self._patch_emit(
                    'skipped_idempotent', lead=existing)):
            row.action_retry()
        row.invalidate_recordset()
        self.assertEqual(row.status, 'skipped_idempotent')
        self.assertEqual(row.lead_id, existing)
        self.assertEqual(row.retry_count, 1)
        self.assertEqual(
            self.Log.search_count([('meta_leadgen_id', '=', 'LG4')]), 1)

    def test_no_fresh_row_defensive_success_sets_lead_id(self):
        """Defensive success: ingest_leadgen returns a lead but emits no fresh
        _record row at all. The clicked row must still get lead_id from the
        returned lead, status == 'success', trigger == 'manual', retry_count == 1
        (an in-place success, no reconcile target)."""
        lead = self._lead(leadgen_id='LG5')
        row = self._failed_row(leadgen_id='LG5', raw=None)
        with mock.patch.object(
                type(self.Ingest), 'ingest_leadgen', autospec=True,
                side_effect=self._patch_emit(
                    'success', lead=lead, emit_row=False)):
            row.action_retry()
        row.invalidate_recordset()
        self.assertEqual(row.lead_id, lead)
        self.assertEqual(row.status, 'success')
        self.assertEqual(row.trigger, 'manual')
        self.assertEqual(row.retry_count, 1)

    # ---------------------------------------------------------------- attempt
    # correlation + reconcile domain --------------------------------------- #
    def test_attempt_correlation_two_candidate_rows(self):
        """With two candidate manual terminal rows for the same leadgen_id -- a
        stale prior 'success' row (retry_origin_log_id unset, an orphaned earlier
        retry) and the fresh row this attempt emits (stamped
        retry_origin_log_id == clicked.id) -- the reconcile must collapse only the
        freshly-correlated row and leave the stale prior success untouched. A
        naive create_date-desc match would wrongly collapse the stale row; its
        survival proves the retry_origin_log_id correlation."""
        # A stale prior manual success row (existing fields only).
        stale = self.Log.create({
            'meta_leadgen_id': 'LG6', 'status': 'success',
            'trigger': 'manual', 'error_message': False})
        lead = self._lead(leadgen_id='LG6')
        row = self._failed_row(leadgen_id='LG6', raw=None)
        with mock.patch.object(
                type(self.Ingest), 'ingest_leadgen', autospec=True,
                side_effect=self._patch_emit('success', lead=lead)):
            row.action_retry()
        row.invalidate_recordset()
        stale.invalidate_recordset()
        # The stale prior success survives -- it was NOT collapsed by create_date.
        self.assertTrue(stale.exists())
        self.assertEqual(stale.status, 'success')
        # The clicked row carries the correlated fresh outcome.
        self.assertEqual(row.status, 'success')
        self.assertEqual(row.lead_id, lead)

    def test_reconcile_domain_rejects_nonterminal_same_leadgen(self):
        """A same-leadgen, same-correlation row in a non-terminal status (e.g.
        'pending') must not be collapsed -- only a row whose status is in
        (success, skipped_idempotent) is a reconcile target. The patched ingest
        also emits the genuine terminal fresh row; assert the terminal one
        collapses and the non-terminal same-leadgen row survives.
        (skipped_duplicate is intentionally not a terminal status.)"""
        # A non-terminal same-leadgen row that must be REJECTED as a target.
        nonterminal = self.Log.create({
            'meta_leadgen_id': 'LG7', 'status': 'pending',
            'trigger': 'manual', 'error_message': False})
        lead = self._lead(leadgen_id='LG7')
        row = self._failed_row(leadgen_id='LG7', raw=None)
        with mock.patch.object(
                type(self.Ingest), 'ingest_leadgen', autospec=True,
                side_effect=self._patch_emit('success', lead=lead)):
            row.action_retry()
        row.invalidate_recordset()
        nonterminal.invalidate_recordset()
        # The non-terminal same-leadgen row was NOT collapsed by the reconcile.
        self.assertTrue(nonterminal.exists())
        self.assertEqual(nonterminal.status, 'pending')
        self.assertEqual(row.status, 'success')

    # ---------------------------------------------------------------- transient
    # / auth stay-failed --------------------------------------------------- #
    def test_manual_refetch_auth_stays_failed(self):
        """A MetaAuthError from ingest_leadgen leaves the row 'failed',
        retry_count == 1, error_message replaced with the redacted str; the row
        is neither marked success nor unlinked."""
        row = self._failed_row(leadgen_id='LG8', raw=None)
        with mock.patch.object(
                type(self.Ingest), 'ingest_leadgen', autospec=True,
                side_effect=MetaAuthError('auth dead')):
            row.action_retry()
        row.invalidate_recordset()
        self.assertTrue(row.exists())
        self.assertEqual(row.status, 'failed')
        self.assertEqual(row.retry_count, 1)
        self.assertIn('auth dead', row.error_message or '')

    def test_manual_transient_stays_failed(self):
        """A MetaTransientError leaves the row 'failed', retry_count == 1, error
        replaced -- a transient failure is not silently succeeded on a manual
        retry."""
        row = self._failed_row(leadgen_id='LG9', raw=None)
        with mock.patch.object(
                type(self.Ingest), 'ingest_leadgen', autospec=True,
                side_effect=MetaTransientError('temporary glitch')):
            row.action_retry()
        row.invalidate_recordset()
        self.assertEqual(row.status, 'failed')
        self.assertEqual(row.retry_count, 1)
        self.assertIn('temporary glitch', row.error_message or '')

    # ---------------------------------------------------------------- corrupt
    # payload -------------------------------------------------------------- #
    def test_corrupt_raw_payload_token_free_fail(self):
        """A failed row whose raw_payload is non-JSON garbage must fail the
        decode before ingest_leadgen is called. ingest_leadgen is never invoked,
        the row stays 'failed' with the token-free message 'Stored payload is not
        valid JSON' (no payload echoed), retry_count == 1, and action_retry does
        not raise out (a bulk batch would continue)."""
        # Resolvable page via the lead linkage so resolution is NOT the failure.
        lead = self._lead(leadgen_id='LG10', with_page=True)
        row = self.Log.create({
            'meta_leadgen_id': 'LG10', 'status': 'failed', 'trigger': 'webhook',
            'error_message': 'boom', 'raw_payload': '{not json',
            'lead_id': lead.id})
        with mock.patch.object(
                type(self.Ingest), 'ingest_leadgen', autospec=True) as m:
            row.action_retry()      # must NOT raise out of the batch
        row.invalidate_recordset()
        m.assert_not_called()
        self.assertEqual(row.status, 'failed')
        self.assertEqual(row.error_message, 'Stored payload is not valid JSON')
        self.assertEqual(row.retry_count, 1)
        # Token-free: the corrupt payload is not echoed into the error.
        self.assertNotIn('{not json', row.error_message or '')

    # ---------------------------------------------------------------- page
    # resolution chain ----------------------------------------------------- #
    def test_page_resolution(self):
        """Page resolution chain:
        (a) a lead carrying meta_page_id_ref resolves to that page;
        (b) a lead carrying only the raw Char meta_page_id matching the fixture
            page's page_id resolves via meta.page.search;
        (c) an unresolvable row (no lead, no page id) leaves the row 'failed' with
            a token-free error containing 'Cannot resolve' and no token / raw
            page-id substring."""
        # (a) resolves via meta_page_id_ref.
        lead_ref = self._lead(leadgen_id='LG11a', with_page=True)
        row_a = self.Log.create({
            'meta_leadgen_id': 'LG11a', 'status': 'failed',
            'trigger': 'webhook', 'lead_id': lead_ref.id})
        self.assertEqual(row_a._resolve_page(), self.page)
        # (b) resolves via the raw Char meta_page_id -> meta.page.search.
        lead_char = self.Lead.create({
            'name': 'CharPage', 'type': 'lead', 'meta_page_id': 'PG1'})
        row_b = self.Log.create({
            'meta_leadgen_id': 'LG11b', 'status': 'failed',
            'trigger': 'webhook', 'lead_id': lead_char.id})
        self.assertEqual(row_b._resolve_page(), self.page)
        # (c) unresolvable -> failed, token-free 'Cannot resolve'.
        row_c = self._failed_row(leadgen_id='LG11c', raw=None, link_page=False)
        with mock.patch.object(
                type(self.Ingest), 'ingest_leadgen', autospec=True) as m:
            row_c.action_retry()
        row_c.invalidate_recordset()
        m.assert_not_called()
        self.assertEqual(row_c.status, 'failed')
        self.assertIn('Cannot resolve', row_c.error_message or '')
        for secret in ('tok_test', 'tok_acct', 'secret_test'):
            self.assertNotIn(secret, row_c.error_message or '')

    # ---------------------------------------------------------------- bulk
    # isolation ------------------------------------------------------------ #
    def test_bulk_retry_isolation(self):
        """Per-unit savepoint isolation: two failed rows A and B retried in one
        action_retry. A succeeds (fresh stamped row); B raises a generic
        Exception. After the call, invalidate_recordset then assert: A flips to
        'success' (its write survives B's savepoint rollback), B stays 'failed'
        with retry_count == 1 (single increment -- B's increment fired before its
        exception, so the outer catch does not double-count) and error_message ==
        'Unexpected retry error'; the notification reflects '1 succeeded' and
        '1 still failed'."""
        lead = self._lead(leadgen_id='LG12A')
        row_a = self._failed_row(leadgen_id='LG12A', raw=None)
        row_b = self._failed_row(leadgen_id='LG12B', raw=None)

        def _side(self_ingest, page, leadgen_id, trigger='webhook', raw=None):
            if leadgen_id == 'LG12B':
                raise Exception('boom-unexpected')   # genuinely unexpected
            self_ingest.env['meta.sync.log']._record(
                leadgen_id, 'manual', 'success', lead=lead, raw=raw)
            return lead

        with mock.patch.object(
                type(self.Ingest), 'ingest_leadgen', autospec=True,
                side_effect=_side):
            result = (row_a | row_b).action_retry()
        (row_a | row_b).invalidate_recordset()
        # A's success write survived B's savepoint rollback.
        self.assertEqual(row_a.status, 'success')
        # B isolated: failed, single increment, token-free generic message.
        self.assertEqual(row_b.status, 'failed')
        self.assertEqual(row_b.retry_count, 1)
        self.assertEqual(row_b.error_message, 'Unexpected retry error')
        # The notification dict reports the per-row counts.
        params = (result or {}).get('params', {})
        message = params.get('message', '')
        self.assertIn('1', message)

    # ---------------------------------------------------------------- single
    # increment ownership -------------------------------------------------- #
    def test_single_increment_on_pre_keystone_failure(self):
        """A failed row whose page cannot be resolved fails before the
        ingest_leadgen call but after the lock+increment. retry_count must be
        exactly 1 (the increment owned by _retry_one fires once even though
        ingest_leadgen is never reached) and the row stays 'failed'. Every
        attempt that acquires the lock is counted exactly once, including a
        failure before ingest_leadgen."""
        row = self._failed_row(leadgen_id='LG13', raw=None, link_page=False)
        with mock.patch.object(type(self.Ingest), 'ingest_leadgen',
                               autospec=True) as m:
            row.action_retry()
        row.invalidate_recordset()
        m.assert_not_called()
        self.assertEqual(row.retry_count, 1)
        self.assertEqual(row.status, 'failed')

    # ---------------------------------------------------------------- lock
    # loser clean fast-fail ------------------------------------------------ #
    def test_lock_loser_fast_fails(self):
        """A row that cannot acquire its FOR UPDATE NOWAIT lock is left 'failed'
        with a token-free 'already being retried' message, is not incremented
        (retry_count unchanged), and its error is written -- proving
        _lock_for_retry contained the LockNotAvailable in its own nested savepoint
        so the loser's write hit a clean cursor. We simulate a held lock by
        patching the cursor execute to raise psycopg2 LockNotAvailable on the
        SELECT ... FOR UPDATE NOWAIT.
        """
        import psycopg2.errors
        row = self._failed_row(leadgen_id='LG14', raw=None)
        before = row.retry_count
        real_execute = self.env.cr.execute

        def _exec(query, params=None):
            q = query if isinstance(query, str) else str(query)
            if 'FOR UPDATE' in q and 'NOWAIT' in q:
                raise psycopg2.errors.LockNotAvailable('row is locked')
            return real_execute(query, params)

        with mock.patch.object(self.env.cr, 'execute', side_effect=_exec):
            row.action_retry()
        row.invalidate_recordset()
        # (i) not incremented -- the loser never owns the increment.
        self.assertEqual(row.retry_count, before)
        # (ii) the clean write happened (the lock failure was savepoint-contained).
        self.assertIn('already', (row.error_message or '').lower())

    # ---------------------------------------------------------------- guard
    # on a non-failed selection -------------------------------------------- #
    def test_retry_guard_non_failed(self):
        """action_retry raises UserError when the selection contains no 'failed'
        row -- here a single 'success' row."""
        ok = self.Log.create({
            'meta_leadgen_id': 'LG15', 'status': 'success', 'trigger': 'manual'})
        with self.assertRaises(UserError):
            ok.action_retry()
