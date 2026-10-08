# Meta Lead Ads Integration for Odoo — © 2026 CoreSys Builders. All rights reserved.
# Licensed under the Odoo Proprietary License v1.0 (OPL-1).
# Unauthorized copying, redistribution, or resale of this software, in whole or in
# part, via any medium, is strictly prohibited and constitutes a license violation.
# OPL-1: https://www.odoo.com/documentation/18.0/legal/licenses.html#odoo-apps

"""Tests for the manual retry action on meta.sync.log.

Retry is a thin wrapper over ``meta.lead.ingest.ingest_leadgen``. The tests
cover ``action_retry`` (bulk, one savepoint per row), ``_retry_one``,
``_lock_for_retry`` (FOR UPDATE NOWAIT in a nested savepoint),
``_reconcile_after_ingest`` and ``_resolve_page``.

The fixtures never write ``page_id`` or ``retry_origin_log_id`` by hand. The
page is resolved through ``lead.meta_page_id_ref``, and the correlation key is
stamped by the real ``Log._record`` from the ``meta_retry_origin_log_id``
context key.

Patch ``ingest_leadgen`` on the class: ``mock.patch.object`` can't patch a
recordset. After a savepoint rollback inside the code under test, call
``invalidate_recordset()`` before asserting, or the ORM cache hides the
rollback.
"""
import json
from unittest import mock

from odoo.exceptions import UserError
from odoo.tests.common import TransactionCase, tagged

from odoo.addons.meta_lead_ads.models.exceptions import (
    MetaTransientError, MetaAuthError)

# Statuses the reconcile step treats as the fresh outcome row. The idempotent
# path logs 'skipped_idempotent'; 'skipped_duplicate' is not one of them.
_RECONCILE_TERMINAL = ('success', 'skipped_idempotent')


class SyncRetryFixtureMixin:
    """Account, page and form fixtures plus the ingest class for patching."""

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
        self.IngestClass = type(self.Ingest)
        self.Lead = self.env['crm.lead']
        self.Log = self.env['meta.sync.log']

    # -- fixture helpers ----------------------------------------------------- #
    def _failed_row(self, leadgen_id='LG1', raw=None, trigger='webhook',
                    lead=None, link_page=True):
        """Create a failed sync-log row to retry.

        ``raw`` is stored as JSON so a replay can decode it. ``link_page``
        makes the page resolvable through the linked lead.
        """
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
        """Get or create a crm.lead for ``leadgen_id``.

        meta_leadgen_id is unique, and several tests call this twice for the
        same id (once via ``_failed_row``), so an existing lead is reused.
        """
        if leadgen_id:
            existing = self.Lead.search(
                [('meta_leadgen_id', '=', leadgen_id)], limit=1)
            if existing:
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
        """Return a side_effect for ingest_leadgen that logs like the service.

        It calls the real ``Log._record`` so the context stamp of
        retry_origin_log_id is exercised, then returns ``lead``.
        ``emit_row=False`` returns the lead without logging a row.
        """
        def _side(self_ingest, page, leadgen_id, trigger='webhook', raw=None):
            if emit_row:
                self_ingest.env['meta.sync.log']._record(
                    leadgen_id, 'manual', status, lead=lead, raw=raw)
            return lead
        return _side


@tagged('post_install', '-at_install')
class TestMetaSyncRetry(SyncRetryFixtureMixin, TransactionCase):
    """Manual retry of failed sync-log rows."""

    # -- replay / re-fetch --------------------------------------------------- #
    def test_manual_replay_success(self):
        """A stored payload is replayed and the clicked row becomes the single success row."""
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
        self.assertEqual(page, self.page)
        self.assertEqual(m.call_args.kwargs.get('trigger'), 'manual')
        self.assertEqual(m.call_args.kwargs.get('raw'), raw)
        self.assertEqual(row.status, 'success')
        self.assertEqual(row.retry_count, 1)
        self.assertEqual(
            self.Log.search_count([('meta_leadgen_id', '=', 'LG1')]), 1)
        survivor = self.Log.search([('meta_leadgen_id', '=', 'LG1')], limit=1)
        self.assertEqual(survivor.trigger, 'manual')

    def test_manual_refetch_success(self):
        """With no stored payload, retry re-fetches from Graph (raw=None) and succeeds."""
        row = self._failed_row(leadgen_id='LG2', raw=None)
        lead = self._lead(leadgen_id='LG2')
        with mock.patch.object(
                type(self.Ingest), 'ingest_leadgen', autospec=True,
                side_effect=self._patch_emit('success', lead=lead)) as m:
            row.action_retry()
        row.invalidate_recordset()
        self.assertEqual(m.call_count, 1)
        self.assertIsNone(m.call_args.kwargs.get('raw'))
        self.assertEqual(row.status, 'success')
        self.assertEqual(row.retry_count, 1)
        survivor = self.Log.search([('meta_leadgen_id', '=', 'LG2')], limit=1)
        self.assertEqual(survivor.trigger, 'manual')

    def test_manual_trigger_overwritten_on_retry(self):
        """A retried webhook row ends up with trigger 'manual'."""
        row = self._failed_row(leadgen_id='LG3', raw=None, trigger='webhook')
        lead = self._lead(leadgen_id='LG3')
        with mock.patch.object(
                type(self.Ingest), 'ingest_leadgen', autospec=True,
                side_effect=self._patch_emit('success', lead=lead)):
            row.action_retry()
        row.invalidate_recordset()
        survivor = self.Log.search([('meta_leadgen_id', '=', 'LG3')], limit=1)
        self.assertEqual(survivor.trigger, 'manual')

    # -- idempotent short-circuit -------------------------------------------- #
    def test_idempotent_short_circuit_sets_lead_id(self):
        """An idempotent hit marks the row 'skipped_idempotent' and links the existing lead."""
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
        """If ingest returns a lead without logging a row, the clicked row still records success."""
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

    # -- attempt correlation and reconcile domain ---------------------------- #
    def test_attempt_correlation_two_candidate_rows(self):
        """Reconcile only collapses the row stamped with this attempt's origin id.

        An older manual success row for the same leadgen_id has no origin id
        and must survive; matching on create_date would wrongly remove it.
        """
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
        self.assertTrue(stale.exists())
        self.assertEqual(stale.status, 'success')
        self.assertEqual(row.status, 'success')
        self.assertEqual(row.lead_id, lead)

    def test_reconcile_domain_rejects_nonterminal_same_leadgen(self):
        """A 'pending' row for the same leadgen_id is not collapsed by reconcile."""
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
        self.assertTrue(nonterminal.exists())
        self.assertEqual(nonterminal.status, 'pending')
        self.assertEqual(row.status, 'success')

    # -- transient / auth errors keep the row failed ------------------------- #
    def test_manual_refetch_auth_stays_failed(self):
        """A MetaAuthError keeps the row failed with the new error message."""
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
        """A MetaTransientError keeps the row failed with the new error message."""
        row = self._failed_row(leadgen_id='LG9', raw=None)
        with mock.patch.object(
                type(self.Ingest), 'ingest_leadgen', autospec=True,
                side_effect=MetaTransientError('temporary glitch')):
            row.action_retry()
        row.invalidate_recordset()
        self.assertEqual(row.status, 'failed')
        self.assertEqual(row.retry_count, 1)
        self.assertIn('temporary glitch', row.error_message or '')

    # -- corrupt payload ----------------------------------------------------- #
    def test_corrupt_raw_payload_token_free_fail(self):
        """Invalid stored JSON fails before ingest, without echoing the payload or raising."""
        # Make the page resolvable so only the decode can fail.
        lead = self._lead(leadgen_id='LG10', with_page=True)
        row = self.Log.create({
            'meta_leadgen_id': 'LG10', 'status': 'failed', 'trigger': 'webhook',
            'error_message': 'boom', 'raw_payload': '{not json',
            'lead_id': lead.id})
        with mock.patch.object(
                type(self.Ingest), 'ingest_leadgen', autospec=True) as m:
            row.action_retry()
        row.invalidate_recordset()
        m.assert_not_called()
        self.assertEqual(row.status, 'failed')
        self.assertEqual(row.error_message, 'Stored payload is not valid JSON')
        self.assertEqual(row.retry_count, 1)
        self.assertNotIn('{not json', row.error_message or '')

    # -- page resolution ----------------------------------------------------- #
    def test_page_resolution(self):
        """Page resolves via meta_page_id_ref, then the raw meta_page_id, else fails cleanly."""
        # Via meta_page_id_ref.
        lead_ref = self._lead(leadgen_id='LG11a', with_page=True)
        row_a = self.Log.create({
            'meta_leadgen_id': 'LG11a', 'status': 'failed',
            'trigger': 'webhook', 'lead_id': lead_ref.id})
        self.assertEqual(row_a._resolve_page(), self.page)
        # Via the Char meta_page_id and a meta.page search.
        lead_char = self.Lead.create({
            'name': 'CharPage', 'type': 'lead', 'meta_page_id': 'PG1'})
        row_b = self.Log.create({
            'meta_leadgen_id': 'LG11b', 'status': 'failed',
            'trigger': 'webhook', 'lead_id': lead_char.id})
        self.assertEqual(row_b._resolve_page(), self.page)
        # Unresolvable: row stays failed and the error leaks no secrets.
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

    # -- bulk isolation ------------------------------------------------------ #
    def test_bulk_retry_isolation(self):
        """In a bulk retry, one row's unexpected error doesn't undo another row's success.

        The failing row is counted once and gets a generic error message.
        """
        lead = self._lead(leadgen_id='LG12A')
        row_a = self._failed_row(leadgen_id='LG12A', raw=None)
        row_b = self._failed_row(leadgen_id='LG12B', raw=None)

        def _side(self_ingest, page, leadgen_id, trigger='webhook', raw=None):
            if leadgen_id == 'LG12B':
                raise Exception('boom-unexpected')
            self_ingest.env['meta.sync.log']._record(
                leadgen_id, 'manual', 'success', lead=lead, raw=raw)
            return lead

        with mock.patch.object(
                type(self.Ingest), 'ingest_leadgen', autospec=True,
                side_effect=_side):
            result = (row_a | row_b).action_retry()
        (row_a | row_b).invalidate_recordset()
        self.assertEqual(row_a.status, 'success')
        self.assertEqual(row_b.status, 'failed')
        self.assertEqual(row_b.retry_count, 1)
        self.assertEqual(row_b.error_message, 'Unexpected retry error')
        params = (result or {}).get('params', {})
        message = params.get('message', '')
        self.assertIn('1', message)

    # -- retry counter ------------------------------------------------------- #
    def test_single_increment_on_failure_before_ingest(self):
        """A failure before ingest_leadgen is called still counts the attempt exactly once."""
        row = self._failed_row(leadgen_id='LG13', raw=None, link_page=False)
        with mock.patch.object(type(self.Ingest), 'ingest_leadgen',
                               autospec=True) as m:
            row.action_retry()
        row.invalidate_recordset()
        m.assert_not_called()
        self.assertEqual(row.retry_count, 1)
        self.assertEqual(row.status, 'failed')

    # -- lock contention ----------------------------------------------------- #
    def test_lock_loser_fast_fails(self):
        """A row that can't get its NOWAIT lock is not counted and gets an 'already being retried' error.

        The held lock is simulated by making cursor.execute raise
        LockNotAvailable on the FOR UPDATE NOWAIT query. The error write only
        works if the lock failure was contained in its own savepoint.
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
        self.assertEqual(row.retry_count, before)
        self.assertIn('already', (row.error_message or '').lower())

    # -- guard --------------------------------------------------------------- #
    def test_retry_guard_non_failed(self):
        """action_retry raises UserError when no selected row is failed."""
        ok = self.Log.create({
            'meta_leadgen_id': 'LG15', 'status': 'success', 'trigger': 'manual'})
        with self.assertRaises(UserError):
            ok.action_retry()
