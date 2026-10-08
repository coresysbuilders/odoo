# Meta Lead Ads Integration for Odoo — © 2026 CoreSys Builders. All rights reserved.
# Licensed under the Odoo Proprietary License v1.0 (OPL-1).
# Unauthorized copying, redistribution, or resale of this software, in whole or in
# part, via any medium, is strictly prohibited and constitutes a license violation.
# OPL-1: https://www.odoo.com/documentation/19.0/legal/licenses.html#odoo-apps

"""Tests for the webhook event queue.

``TestMetaWebhookExtract`` calls ``_ingest_payload`` directly, without HTTP.
``TestMetaWebhookDrain`` runs ``_cron_drain`` with ``ingest_leadgen`` patched
on the class, since mock.patch.object can't patch a recordset.
"""
from unittest import mock

from odoo.tests.common import TransactionCase, tagged

from odoo.addons.meta_lead_ads.models.exceptions import (
    MetaTransientError, MetaPermanentError, MetaAuthError)

MAX_ATTEMPTS = 5            # keep in step with meta_webhook_event.MAX_ATTEMPTS


class WebhookDrainFixtureMixin:
    """Account, page and form fixtures, as in test_ingest."""

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
        self.IngestClass = type(self.Ingest)
        self.Lead = self.env['crm.lead']
        self.Log = self.env['meta.sync.log']

    def _payload(self, leadgen_id='LG1', page_id='PG1', form_id='F1',
                 ad_id='AD1'):
        """Return a Page leadgen webhook body shaped like Meta's."""
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
        """Create a real crm.lead for the mocked ingest to return."""
        return self.Lead.create({'name': 'WH Lead', 'type': 'lead'})


@tagged('post_install', '-at_install')
class TestMetaWebhookExtract(WebhookDrainFixtureMixin, TransactionCase):
    """Parsing webhook bodies into queue rows, deduplicated on leadgen_id."""

    def test_extract_and_dedup(self):
        """Leadgen changes are queued once each, other events are ignored, and
        no crm.lead is created at this stage."""
        lead_before = self.Lead.search_count([])
        data = self._payload(leadgen_id='LG_X1')
        raw = b'{"object":"page"}'
        self.Event._ingest_payload(data, raw=raw)
        self.assertEqual(
            self.Event.search_count([('leadgen_id', '=', 'LG_X1')]), 1)
        evt = self.Event.search([('leadgen_id', '=', 'LG_X1')], limit=1)
        self.assertEqual(evt.page_id, 'PG1')
        self.assertEqual(evt.form_id, 'F1')
        # Same payload again: still one row.
        self.Event._ingest_payload(data, raw=raw)
        self.assertEqual(
            self.Event.search_count([('leadgen_id', '=', 'LG_X1')]), 1)
        # Non-page objects are ignored.
        before = self.Event.search_count([])
        self.Event._ingest_payload(
            {'object': 'user', 'entry': []}, raw=b'{}')
        self.assertEqual(self.Event.search_count([]), before)
        # So are page changes other than leadgen.
        self.Event._ingest_payload({
            'object': 'page',
            'entry': [{'id': 'PG1', 'changes': [
                {'field': 'feed', 'value': {'leadgen_id': 'LG_FEED'}}]}]},
            raw=b'{}')
        self.assertEqual(
            self.Event.search_count([('leadgen_id', '=', 'LG_FEED')]), 0)
        self.assertEqual(self.Lead.search_count([]), lead_before)

    def test_extract_edge_cases(self):
        """Odd but valid payload shapes are handled without crashing."""
        # Several entries, one lead each.
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
        # Several changes in one entry.
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
        # A leadgen change with no value is skipped.
        before_c = self.Event.search_count([])
        self.Event._ingest_payload({
            'object': 'page',
            'entry': [{'id': 'PG1', 'changes': [
                {'field': 'leadgen'},
                {'field': 'leadgen', 'value': None}]}]}, raw=b'{}')
        self.assertEqual(self.Event.search_count([]), before_c)
        # A value without leadgen_id is skipped.
        before_d = self.Event.search_count([])
        self.Event._ingest_payload({
            'object': 'page',
            'entry': [{'id': 'PG1', 'changes': [
                {'field': 'leadgen', 'value': {'page_id': 'PG1'}}]}]},
            raw=b'{}')
        self.assertEqual(self.Event.search_count([]), before_d)
        # Without page_id in the value, the entry id is used.
        self.Event._ingest_payload({
            'object': 'page',
            'entry': [{'id': 'PG_FALLBACK', 'changes': [
                {'field': 'leadgen', 'value': {'leadgen_id': 'LG_E'}}]}]},
            raw=b'{}')
        evt = self.Event.search([('leadgen_id', '=', 'LG_E')], limit=1)
        self.assertEqual(evt.page_id, 'PG_FALLBACK')

    def test_unexpected_error_surfaces(self):
        """Errors other than the duplicate-key race propagate.

        Only the IntegrityError from a concurrent duplicate is swallowed;
        anything else must surface so a lead is never lost silently.
        """
        EventClass = type(self.Event)
        data = self._payload(leadgen_id='LG_BOOM')
        with mock.patch.object(EventClass, 'create',
                               side_effect=ValueError('unexpected')):
            with self.assertRaises(ValueError):
                self.Event._ingest_payload(data, raw=b'{}')


@tagged('post_install', '-at_install')
class TestMetaWebhookDrain(WebhookDrainFixtureMixin, TransactionCase):
    """The queue drain cron: success, retries, failures and isolation."""

    def _queue(self, leadgen_id='LG1', page_id='PG1'):
        return self.Event.create({
            'leadgen_id': leadgen_id, 'page_id': page_id,
            'form_id': 'F1', 'status': 'pending'})

    def test_drain_success(self):
        """A pending event is ingested once with trigger 'webhook' and done."""
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
        """Transient errors retry up to MAX_ATTEMPTS, then the row fails.

        Once failed, later sweeps leave it alone.
        """
        evt = self._queue(leadgen_id='LG_TRANS')
        with mock.patch.object(self.IngestClass, 'ingest_leadgen',
                               side_effect=MetaTransientError('temp')) as m:
            for sweep in range(1, MAX_ATTEMPTS):
                self.Event._cron_drain()
                self.assertEqual(evt.status, 'pending')
                self.assertEqual(evt.attempts, sweep)
            # Last allowed attempt: give up.
            self.Event._cron_drain()
            self.assertEqual(evt.status, 'failed')
            self.assertEqual(evt.attempts, MAX_ATTEMPTS)
            self.assertTrue(self.Log.search_count(
                [('meta_leadgen_id', '=', 'LG_TRANS'),
                 ('status', '=', 'failed')]))
            # Another sweep must not pick up the failed row.
            self.Event._cron_drain()
        self.assertEqual(evt.attempts, MAX_ATTEMPTS)
        self.assertEqual(m.call_count, MAX_ATTEMPTS)

    def test_permanent_fails_fast(self):
        """Permanent and auth errors fail the row at once, without retrying."""
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
        """An event for an unknown page fails without calling ingest."""
        evt = self._queue(leadgen_id='LG_NOPAGE', page_id='PG_UNKNOWN')
        with mock.patch.object(self.IngestClass, 'ingest_leadgen') as m:
            self.Event._cron_drain()
        self.assertEqual(evt.status, 'failed')
        m.assert_not_called()
        self.assertTrue(self.Log.search_count(
            [('meta_leadgen_id', '=', 'LG_NOPAGE'),
             ('status', '=', 'failed')]))

    def test_drain_sibling_isolation(self):
        """An unexpected error on one event doesn't undo another's result.

        Each event runs in its own savepoint, so rolling back B keeps A done.
        """
        evt_a = self._queue(leadgen_id='LG_SIB_A')
        evt_b = self._queue(leadgen_id='LG_SIB_B')
        # The drain is FIFO, so A is processed first.
        self.assertLess(evt_a.id, evt_b.id)
        fake = self._fake_lead()

        def _side_effect(page, leadgen_id, **kwargs):
            if leadgen_id == 'LG_SIB_B':
                raise ValueError('unexpected')
            return fake

        with mock.patch.object(self.IngestClass, 'ingest_leadgen',
                               side_effect=_side_effect):
            self.Event._cron_drain()
        self.assertEqual(evt_a.status, 'done')
        self.assertEqual(evt_a.lead_id, fake)
        self.assertEqual(evt_b.status, 'failed')
        self.assertTrue(self.Log.search_count(
            [('meta_leadgen_id', '=', 'LG_SIB_B'), ('status', '=', 'failed')]))
        self.assertFalse(self.Log.search_count(
            [('meta_leadgen_id', '=', 'LG_SIB_A'), ('status', '=', 'failed')]))
