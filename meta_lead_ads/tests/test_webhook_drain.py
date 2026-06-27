# Meta Lead Ads Integration for Odoo — © 2026 CoreSys Builders. All rights reserved.
# Licensed under the Odoo Proprietary License v1.0 (OPL-1).
# Unauthorized copying, redistribution, or resale of this software, in whole or in
# part, via any medium, is strictly prohibited and constitutes a license violation.
# OPL-1: https://www.odoo.com/documentation/19.0/legal/licenses.html#odoo-apps

"""Tests for the webhook queue: payload extraction + dedup, and the cron drain
lifecycle including sibling isolation.

Two TransactionCase classes live here:

  * ``TestMetaWebhookExtract`` -- the model-level extraction + dedup contract,
    calling ``meta.webhook.event._ingest_payload(data, raw=...)`` directly (no
    HTTP). Covers the happy path, the edge cases (multi-entry / multi-changes /
    missing value / missing leadgen_id / page_id fallback to entry.id) and the
    guard ``test_unexpected_error_surfaces`` (a non-duplicate error in
    _ingest_payload surfaces; only the IntegrityError unique-violation race is
    swallowed).
  * ``TestMetaWebhookDrain`` -- ``_cron_drain`` -> ``ingest_leadgen``
    (mocked on the class). Covers success, transient retry-then-give-up
    at MAX_ATTEMPTS=5 (+ no 6th attempt), permanent-fail-fast for both
    MetaPermanentError and MetaAuthError, unknown-page, and per-event-savepoint
    sibling isolation: one event's unexpected (non-typed) drain error must not
    roll back a sibling's status write.

Conventions used throughout:
  - Patch ``ingest_leadgen`` / ``create`` on ``type(...)`` (the class), never a
    recordset (``mock.patch.object`` is read-only on recordsets).
  - Use ``search_count(...)``; ``search(count=)`` was removed in Odoo 18.
  - ``assertRaises`` takes a single exception class, never a tuple.
"""
from unittest import mock

from odoo.tests.common import TransactionCase, tagged

from odoo.addons.meta_lead_ads.models.exceptions import (
    MetaTransientError, MetaPermanentError, MetaAuthError)

MAX_ATTEMPTS = 5            # mirrors meta_webhook_event.MAX_ATTEMPTS (retry cap)


class WebhookDrainFixtureMixin:
    """meta.account -> meta.page -> meta.lead.form chain (mirrors
    test_ingest.IngestFixtureMixin) + the ingest class for class-patching."""

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
        self.Event = self.env['meta.webhook.event']
        self.Ingest = self.env['meta.lead.ingest']
        # Patch ingest_leadgen on the class, never a recordset.
        self.IngestClass = type(self.Ingest)
        self.Lead = self.env['crm.lead']
        self.Log = self.env['meta.sync.log']

    def _payload(self, leadgen_id='LG1', page_id='PG1', form_id='F1',
                 ad_id='AD1'):
        """A verified Meta Page-leadgen envelope (object='page'; entry[].id ==
        page_id; changes[].field=='leadgen')."""
        return {
            'object': 'page',
            'entry': [{
                'id': page_id, 'time': 1700000000,
                'changes': [{
                    'field': 'leadgen',
                    'value': {
                        'leadgen_id': leadgen_id, 'page_id': page_id,
                        'form_id': form_id, 'ad_id': ad_id,
                        'created_time': 1700000000,
                    },
                }],
            }],
        }

    def _fake_lead(self):
        """A real crm.lead so the success path can assert lead_id is set."""
        return self.Lead.create({'name': 'WH Lead', 'type': 'lead'})


@tagged('post_install', '-at_install')
class TestMetaWebhookExtract(WebhookDrainFixtureMixin, TransactionCase):
    """The model-level extraction + dedup contract. Calls ``_ingest_payload``
    directly; asserts extraction, idempotent dedup on leadgen_id (global scope),
    zero crm.lead, plus the edge cases and the non-duplicate-error-surfaces
    guard."""

    def test_extract_and_dedup(self):
        """Extract leadgen_id/page_id/form_id from entry[].changes[].value;
        re-ingesting the same payload stays at one row (dedup on leadgen_id);
        a non-'page' object and a non-'leadgen' field both create zero rows;
        _ingest_payload creates zero crm.lead."""
        lead_before = self.Lead.search_count([])
        data = self._payload(leadgen_id='LG_X1')
        raw = b'{"object":"page"}'
        self.Event._ingest_payload(data, raw=raw)
        self.assertEqual(
            self.Event.search_count([('leadgen_id', '=', 'LG_X1')]), 1)
        evt = self.Event.search([('leadgen_id', '=', 'LG_X1')], limit=1)
        self.assertEqual(evt.page_id, 'PG1')
        self.assertEqual(evt.form_id, 'F1')
        # dedup: a second identical payload does not create a 2nd row.
        self.Event._ingest_payload(data, raw=raw)
        self.assertEqual(
            self.Event.search_count([('leadgen_id', '=', 'LG_X1')]), 1)
        # a non-'page' object -> ZERO rows.
        before = self.Event.search_count([])
        self.Event._ingest_payload(
            {'object': 'user', 'entry': []}, raw=b'{}')
        self.assertEqual(self.Event.search_count([]), before)
        # a non-'leadgen' field -> ZERO rows.
        self.Event._ingest_payload({
            'object': 'page',
            'entry': [{'id': 'PG1', 'changes': [
                {'field': 'feed', 'value': {'leadgen_id': 'LG_FEED'}}]}]},
            raw=b'{}')
        self.assertEqual(
            self.Event.search_count([('leadgen_id', '=', 'LG_FEED')]), 0)
        # _ingest_payload never creates a crm.lead.
        self.assertEqual(self.Lead.search_count([]), lead_before)

    def test_extract_edge_cases(self):
        """_ingest_payload handles, without crashing and without spurious rows:
        (a) multiple entry[] -> one row per distinct leadgen_id;
        (b) multiple changes[] in one entry -> one row per leadgen change;
        (c) a leadgen change with no 'value' -> zero rows; (d) a value missing
        leadgen_id -> zero rows; (e) value missing page_id -> page_id falls back
        to entry.id."""
        # (a) multiple entry[] elements, distinct leadgen ids.
        multi_entry = {
            'object': 'page',
            'entry': [
                {'id': 'PG1', 'changes': [
                    {'field': 'leadgen',
                     'value': {'leadgen_id': 'LG_A', 'page_id': 'PG1'}}]},
                {'id': 'PG1', 'changes': [
                    {'field': 'leadgen',
                     'value': {'leadgen_id': 'LG_B', 'page_id': 'PG1'}}]},
            ]}
        self.Event._ingest_payload(multi_entry, raw=b'{}')
        self.assertEqual(
            self.Event.search_count([('leadgen_id', '=', 'LG_A')]), 1)
        self.assertEqual(
            self.Event.search_count([('leadgen_id', '=', 'LG_B')]), 1)
        # (b) multiple changes[] in one entry -> one row per leadgen change.
        multi_change = {
            'object': 'page',
            'entry': [{'id': 'PG1', 'changes': [
                {'field': 'leadgen',
                 'value': {'leadgen_id': 'LG_C', 'page_id': 'PG1'}},
                {'field': 'leadgen',
                 'value': {'leadgen_id': 'LG_D', 'page_id': 'PG1'}}]}]}
        self.Event._ingest_payload(multi_change, raw=b'{}')
        self.assertEqual(
            self.Event.search_count([('leadgen_id', '=', 'LG_C')]), 1)
        self.assertEqual(
            self.Event.search_count([('leadgen_id', '=', 'LG_D')]), 1)
        # (c) a leadgen change with no 'value' -> zero rows, no exception.
        before_c = self.Event.search_count([])
        self.Event._ingest_payload({
            'object': 'page',
            'entry': [{'id': 'PG1', 'changes': [
                {'field': 'leadgen'},
                {'field': 'leadgen', 'value': None}]}]}, raw=b'{}')
        self.assertEqual(self.Event.search_count([]), before_c)
        # (d) a value missing leadgen_id -> zero rows.
        before_d = self.Event.search_count([])
        self.Event._ingest_payload({
            'object': 'page',
            'entry': [{'id': 'PG1', 'changes': [
                {'field': 'leadgen', 'value': {'page_id': 'PG1'}}]}]},
            raw=b'{}')
        self.assertEqual(self.Event.search_count([]), before_d)
        # (e) value missing page_id -> page_id falls back to entry.id.
        self.Event._ingest_payload({
            'object': 'page',
            'entry': [{'id': 'PG_FALLBACK', 'changes': [
                {'field': 'leadgen', 'value': {'leadgen_id': 'LG_E'}}]}]},
            raw=b'{}')
        evt = self.Event.search([('leadgen_id', '=', 'LG_E')], limit=1)
        self.assertEqual(evt.page_id, 'PG_FALLBACK')

    def test_unexpected_error_surfaces(self):
        """A non-duplicate unexpected error inside _ingest_payload surfaces --
        only the IntegrityError unique-violation race is absorbed by the dedup
        savepoint; everything else propagates so a lead is never silently
        dropped. Patch create on the class to raise a plain ValueError for a
        fresh leadgen_id; assert it propagates."""
        EventClass = type(self.Event)
        data = self._payload(leadgen_id='LG_BOOM')
        with mock.patch.object(EventClass, 'create',
                               side_effect=ValueError('unexpected')):
            with self.assertRaises(ValueError):
                self.Event._ingest_payload(data, raw=b'{}')


@tagged('post_install', '-at_install')
class TestMetaWebhookDrain(WebhookDrainFixtureMixin, TransactionCase):
    """The cron drain lifecycle over ``ingest_leadgen`` (mocked on the
    class)."""

    def _queue(self, leadgen_id='LG1', page_id='PG1'):
        return self.Event.create({
            'leadgen_id': leadgen_id, 'page_id': page_id,
            'form_id': 'F1', 'status': 'pending'})

    def test_drain_success(self):
        """A pending event drains: ingest_leadgen is called once with
        (page, leadgen_id, trigger='webhook'); status -> 'done'; lead_id set."""
        evt = self._queue(leadgen_id='LG_OK')
        fake = self._fake_lead()
        with mock.patch.object(self.IngestClass, 'ingest_leadgen',
                               return_value=fake) as m_ingest:
            self.Event._cron_drain()
        self.assertEqual(evt.status, 'done')
        self.assertEqual(evt.lead_id, fake)
        self.assertEqual(m_ingest.call_count, 1)
        args, kwargs = m_ingest.call_args
        self.assertEqual(args[0], self.page)
        self.assertEqual(args[1], 'LG_OK')
        self.assertEqual(kwargs.get('trigger'), 'webhook')

    def test_transient_retry_then_give_up(self):
        """MetaTransientError keeps the row 'pending' + bumps attempts each
        sweep; at attempts==MAX_ATTEMPTS (5) it flips to 'failed' + a
        meta.sync.log 'failed' row; there is no 6th ingest attempt afterwards
        (attempts caps at 5, ingest call count caps at 5)."""
        evt = self._queue(leadgen_id='LG_TRANS')
        with mock.patch.object(self.IngestClass, 'ingest_leadgen',
                               side_effect=MetaTransientError('temp')) as m:
            for sweep in range(1, MAX_ATTEMPTS):
                self.Event._cron_drain()
                self.assertEqual(evt.status, 'pending')
                self.assertEqual(evt.attempts, sweep)
            # the MAX_ATTEMPTS-th sweep gives up.
            self.Event._cron_drain()
            self.assertEqual(evt.status, 'failed')
            self.assertEqual(evt.attempts, MAX_ATTEMPTS)
            self.assertTrue(self.Log.search_count(
                [('meta_leadgen_id', '=', 'LG_TRANS'),
                 ('status', '=', 'failed')]))
            # no 6th attempt: a further sweep does not re-ingest a failed row.
            self.Event._cron_drain()
        self.assertEqual(evt.attempts, MAX_ATTEMPTS)
        self.assertEqual(m.call_count, MAX_ATTEMPTS)

    def test_permanent_fails_fast(self):
        """MetaPermanentError and MetaAuthError each fail immediately
        (status='failed', attempts<=1, no retry) + a meta.sync.log 'failed' row.
        Two distinct queue rows (one per error class) avoid shared-state
        pollution."""
        evt_perm = self._queue(leadgen_id='LG_PERM')
        with mock.patch.object(self.IngestClass, 'ingest_leadgen',
                               side_effect=MetaPermanentError('perm')):
            self.Event._cron_drain()
        self.assertEqual(evt_perm.status, 'failed')
        self.assertLessEqual(evt_perm.attempts, 1)
        self.assertTrue(self.Log.search_count(
            [('meta_leadgen_id', '=', 'LG_PERM'), ('status', '=', 'failed')]))

        evt_auth = self._queue(leadgen_id='LG_AUTH')
        with mock.patch.object(self.IngestClass, 'ingest_leadgen',
                               side_effect=MetaAuthError('auth')):
            self.Event._cron_drain()
        self.assertEqual(evt_auth.status, 'failed')
        self.assertLessEqual(evt_auth.attempts, 1)
        self.assertTrue(self.Log.search_count(
            [('meta_leadgen_id', '=', 'LG_AUTH'), ('status', '=', 'failed')]))

    def test_unknown_page(self):
        """A pending event whose page_id matches no meta.page -> 'failed' +
        meta.sync.log row, no exception raised, ingest_leadgen never called."""
        evt = self._queue(leadgen_id='LG_NOPAGE', page_id='PG_UNKNOWN')
        with mock.patch.object(self.IngestClass, 'ingest_leadgen') as m:
            self.Event._cron_drain()
        self.assertEqual(evt.status, 'failed')
        m.assert_not_called()
        self.assertTrue(self.Log.search_count(
            [('meta_leadgen_id', '=', 'LG_NOPAGE'),
             ('status', '=', 'failed')]))

    def test_drain_sibling_isolation(self):
        """Two pending events drained in one sweep. Event A (FIFO-first)
        succeeds -> 'done' (its status write survives); event B raises an
        unexpected non-typed ValueError -> only B is marked 'failed' token-free
        by the outer guard. B's per-event-savepoint rollback must not revert A's
        status write. A meta.sync.log 'failed' row exists for B and none for
        A."""
        evt_a = self._queue(leadgen_id='LG_SIB_A')
        evt_b = self._queue(leadgen_id='LG_SIB_B')
        # Ensure FIFO order A-before-B (the model _order is create_date asc).
        self.assertLess(evt_a.id, evt_b.id)
        fake = self._fake_lead()

        def _side_effect(page, leadgen_id, **kwargs):
            if leadgen_id == 'LG_SIB_B':
                raise ValueError('unexpected')      # genuinely unexpected branch
            return fake

        with mock.patch.object(self.IngestClass, 'ingest_leadgen',
                               side_effect=_side_effect):
            self.Event._cron_drain()
        # A's write survived B's rollback (per-event savepoint isolation).
        self.assertEqual(evt_a.status, 'done')
        self.assertEqual(evt_a.lead_id, fake)
        # B was isolated and failed token-free.
        self.assertEqual(evt_b.status, 'failed')
        self.assertTrue(self.Log.search_count(
            [('meta_leadgen_id', '=', 'LG_SIB_B'), ('status', '=', 'failed')]))
        self.assertFalse(self.Log.search_count(
            [('meta_leadgen_id', '=', 'LG_SIB_A'), ('status', '=', 'failed')]))
