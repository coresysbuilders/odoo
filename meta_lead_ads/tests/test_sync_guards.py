# Meta Lead Ads Integration for Odoo — © 2026 CoreSys Builders. All rights reserved.
# Licensed under the Odoo Proprietary License v1.0 (OPL-1).
# Unauthorized copying, redistribution, or resale of this software, in whole or in
# part, via any medium, is strictly prohibited and constitutes a license violation.
# OPL-1: https://www.odoo.com/documentation/18.0/legal/licenses.html#odoo-apps

"""Guards that keep sync from going wrong quietly.

- Archiving a Meta account stops its Pages from syncing.
- A Page Meta lists without a token keeps the token Odoo has.
- A Graph call without a token is an auth error, not a crash.
- A Page listing cut short archives nothing.
- The webhook drain stops before the cron time limit can kill it.
- The every-minute drain doesn't rewrite its run stamp every minute.
"""
from datetime import timedelta
from unittest import mock

from odoo import fields
from odoo.tests.common import TransactionCase, tagged
from odoo.tools import mute_logger

from odoo.addons.meta_lead_ads.models import const, meta_webhook_event
from odoo.addons.meta_lead_ads.models.exceptions import MetaAuthError
from odoo.addons.meta_lead_ads.models.meta_sync_log import (
    _PAGE_ARCHIVED_MESSAGE)

from .test_webhook_drain import WebhookDrainFixtureMixin

_DRAIN_LOGGER = 'odoo.addons.meta_lead_ads.models.meta_webhook_event'


@tagged('post_install', '-at_install')
class TestSyncGuards(WebhookDrainFixtureMixin, TransactionCase):

    def setUp(self):
        super().setUp()
        # Patch targets go on the class; recordsets can't be patched.
        self.Client = type(self.env['meta.graph.client'])
        self.EventClass = type(self.Event)

    def _queue(self, leadgen_id, page_id='PG1'):
        return self.Event.create({
            'leadgen_id': leadgen_id, 'page_id': page_id,
            'form_id': 'F1', 'status': 'pending'})

    # -- archived account ------------------------------------------------- #
    def test_archiving_the_account_archives_its_pages(self):
        self.account.action_archive()
        self.assertFalse(self.page.active)
        # The sweep only takes forms of active Pages.
        self.assertNotIn(
            self.form, self.env['meta.lead.form'].search(
                [('page_id.active', '=', True)]))

    @mute_logger(_DRAIN_LOGGER)
    def test_archived_account_holds_webhook_leads(self):
        self.account.action_archive()
        event = self._queue('LG_ACC_ARCH')
        with mock.patch.object(self.Client, 'fetch_lead') as fetch:
            self.Event._cron_drain()
        fetch.assert_not_called()
        self.assertEqual(event.status, 'failed')
        self.assertEqual(event.error_message, _PAGE_ARCHIVED_MESSAGE)

    def test_archiving_one_account_leaves_other_pages(self):
        other = self.env['meta.account'].create({
            'name': 'Other', 'account_id': 'ACC2', 'app_id': 'app_test',
            'access_token': 'tok_other'})
        other_page = self.env['meta.page'].create({
            'name': 'Other Page', 'page_id': 'PG2',
            'access_token': 'tok_p2', 'account_id': other.id})
        other.action_archive()
        self.assertFalse(other_page.active)
        self.assertTrue(self.page.active)

    # -- missing Page token ----------------------------------------------- #
    def test_listing_without_token_keeps_the_stored_one(self):
        Wizard = self.env['meta.onboarding']
        Wizard._get_or_create_page(self.account, 'PG1', 'Page renamed', None)
        self.assertEqual(self.page.name, 'Page renamed')
        self.assertEqual(self.page.sudo().access_token, 'tok_test')
        Wizard._get_or_create_page(self.account, 'PG1', 'Page', 'tok_new')
        self.assertEqual(self.page.sudo().access_token, 'tok_new')

    def test_graph_call_without_token_is_an_auth_error(self):
        Graph = self.env['meta.graph.client']
        with mock.patch.object(self.Client, '_get_session') as session:
            for token in (None, False, ''):
                with self.assertRaises(MetaAuthError):
                    Graph._request(token, 'me', app_secret='secret_test')
        session.assert_not_called()

    # -- cut-short Page listing ------------------------------------------- #
    def test_truncated_page_listing_is_reported(self):
        # A next link without a cursor can't be followed.
        listing = {'data': [{'id': 'PG1', 'name': 'Page'}],
                   'paging': {'next': 'https://graph.facebook.com/x'}}
        status = {}
        with mock.patch.object(self.Client, '_request',
                               return_value=listing):
            pages = self.env['meta.graph.client'].discover_pages(
                'tok', status=status)
        self.assertEqual([p['id'] for p in pages], ['PG1'])
        self.assertTrue(status.get('truncated'))

    def test_truncated_page_listing_archives_nothing(self):
        Wizard = self.env['meta.onboarding']
        listed = [{'id': 'PG9', 'name': 'New', 'access_token': 'tok9'}]
        Wizard._reconcile_pages(self.account, listed, complete=False)
        self.assertTrue(self.page.active)
        # A full listing still archives a Page Meta no longer returns.
        Wizard._reconcile_pages(self.account, listed)
        self.assertFalse(self.page.active)

    # -- drain run stamp --------------------------------------------------- #
    def test_drain_stamp_is_refreshed_every_few_minutes_only(self):
        ICP = self.env['ir.config_parameter'].sudo()
        now = fields.Datetime.now()
        recent = fields.Datetime.to_string(now - timedelta(minutes=2))
        ICP.set_param(const.DRAIN_RUN_PARAM, recent)
        self.Event._cron_drain()
        self.assertEqual(ICP.get_param(const.DRAIN_RUN_PARAM), recent)
        old = fields.Datetime.to_string(
            now - timedelta(minutes=const.DRAIN_STAMP_MINUTES + 1))
        ICP.set_param(const.DRAIN_RUN_PARAM, old)
        self.Event._cron_drain()
        self.assertGreater(ICP.get_param(const.DRAIN_RUN_PARAM), old)
        # A stamp in the future (a clock moved back) is rewritten too.
        ahead = fields.Datetime.to_string(now + timedelta(hours=1))
        ICP.set_param(const.DRAIN_RUN_PARAM, ahead)
        self.Event._cron_drain()
        self.assertLess(ICP.get_param(const.DRAIN_RUN_PARAM), ahead)

    # -- drain time budget ------------------------------------------------- #
    def test_drain_stops_when_out_of_time(self):
        events = [self._queue('LG_BUDGET_%d' % i) for i in range(3)]
        clock = [0.0]

        def process(event, lookup_states=None, deadline=None):
            self.assertEqual(deadline, const.DRAIN_TIME_BUDGET)
            clock[0] += 30
            event.write({'status': 'done'})

        with mock.patch.object(meta_webhook_event, '_monotonic',
                               lambda: clock[0]), \
                mock.patch.object(self.EventClass, '_process_one',
                                  autospec=True, side_effect=process):
            self.Event._cron_drain()
        # Two events fit in the budget; the third waits for the next run.
        # They share a create date here, so which one waits is not fixed.
        self.assertEqual(sorted(e.status for e in events),
                         ['done', 'done', 'pending'])

    def test_drain_passes_its_deadline_to_name_lookups(self):
        event = self._queue('LG_BUDGET_NAMES')
        seen = {}

        def ingest(page, leadgen_id, **kw):
            seen.update(kw.get('lookup_state') or {})
            raise MetaAuthError('stop here')

        with mock.patch.object(type(self.env['meta.lead.ingest']),
                               'ingest_leadgen', side_effect=ingest), \
                mute_logger(_DRAIN_LOGGER):
            event._process_one(lookup_states={}, deadline=123.0)
        self.assertEqual(seen.get('deadline'), 123.0)
