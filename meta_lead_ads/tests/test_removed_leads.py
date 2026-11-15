# Meta Lead Ads Integration for Odoo — © 2026 CoreSys Builders. All rights reserved.
# Licensed under the Odoo Proprietary License v1.0 (OPL-1).
# Unauthorized copying, redistribution, or resale of this software, in whole or in
# part, via any medium, is strictly prohibited and constitutes a license violation.
# OPL-1: https://www.odoo.com/documentation/18.0/legal/licenses.html#odoo-apps

"""A lead deleted in Odoo stays deleted on every automatic path.

The webhook drain, both Retry buttons and the sweeps meet the removed lead
through ingest_leadgen, which refuses it before any Graph call. Each path
then leaves exactly one skipped_removed sync row for the lead.
"""
from datetime import timedelta
from unittest import mock

from lxml import etree

from odoo import Command
from odoo.exceptions import AccessError, UserError
from odoo.tests.common import TransactionCase, new_test_user, tagged
from odoo.tools import format_datetime, mute_logger

from odoo.addons.meta_lead_ads.models import const
from odoo.addons.meta_lead_ads.models.exceptions import (
    MetaAuthError, MetaGraphError, MetaLeadRemovedError, MetaPermanentError,
    MetaRateLimitError)

from .test_ingest import IngestFixtureMixin
from .test_multi_company import MultiCompanyFixtureMixin

_REMOVED = 'Removed in Odoo; skipped'
_DRAIN_LOGGER = 'odoo.addons.meta_lead_ads.models.meta_webhook_event'
_EVENT_LOGGER = _DRAIN_LOGGER
_LOG_LOGGER = 'odoo.addons.meta_lead_ads.models.meta_sync_log'


@tagged('post_install', '-at_install')
class TestRemovedLeads(MultiCompanyFixtureMixin, IngestFixtureMixin,
                       TransactionCase):

    def setUp(self):
        super().setUp()
        self.Log = self.env['meta.sync.log'].sudo()
        self.Event = self.env['meta.webhook.event'].sudo()
        self.EventClass = type(self.env['meta.webhook.event'])
        self.Tomb = self.env['meta.leadgen.tombstone']
        self.TombClass = type(self.Tomb)
        self.LogClass = type(self.env['meta.sync.log'])

    # -- helpers --------------------------------------------------------- #
    def _remove(self, *leadgen_ids):
        self.Tomb._remember(list(leadgen_ids))

    def _rows(self, leadgen_id, status=None):
        domain = [('meta_leadgen_id', '=', leadgen_id)]
        if status:
            domain.append(('status', '=', status))
        return self.Log.search(domain)

    def _leads(self, leadgen_id):
        return self.Lead.with_context(active_test=False).search(
            [('meta_leadgen_id', '=', leadgen_id)])

    def _queue(self, leadgen_id, status='pending', **vals):
        vals.update({'leadgen_id': leadgen_id, 'page_id': 'PG1',
                     'form_id': 'F1', 'status': status})
        return self.Event.create(vals)

    def _drain(self):
        with self._patch_graph():
            self.env['meta.webhook.event']._cron_drain()
        self.env.invalidate_all()

    def _assert_one_skip(self, leadgen_id):
        rows = self._rows(leadgen_id)
        self.assertEqual(len(rows), 1, rows.mapped('status'))
        self.assertEqual(rows.status, 'skipped_removed')
        self.assertEqual(rows.error_message, _REMOVED)
        return rows

    # -- the gate -------------------------------------------------------- #
    def test_removed_error_is_not_a_permanent_failure(self):
        self.assertTrue(issubclass(MetaLeadRemovedError, MetaGraphError))
        self.assertFalse(issubclass(MetaLeadRemovedError, MetaPermanentError))

    def test_ingest_refuses_removed_lead_before_graph(self):
        self._remove('LGRMV_G')
        with self._patch_graph():
            with self.assertRaises(MetaLeadRemovedError):
                self.Ingest.ingest_leadgen(self.page, 'LGRMV_G')
            self._fetch_lead.assert_not_called()
        self.assertFalse(self._leads('LGRMV_G'))
        self.assertFalse(self._rows('LGRMV_G'))

    def test_existing_lead_still_returned(self):
        # A lead that exists is returned before the removed check runs.
        lead = self.Lead.create({'name': 'Kept', 'meta_leadgen_id': 'LGRMV_K'})
        self._remove('LGRMV_K')
        with self._patch_graph():
            got = self.Ingest.ingest_leadgen(self.page, 'LGRMV_K')
            self._fetch_lead.assert_not_called()
        self.assertEqual(got, lead)

    # -- the skip policy ------------------------------------------------- #
    def test_record_skip_repeated_writes_one_row(self):
        self._remove('LGRMV_P')
        for _i in range(3):
            self.Log._record_removed_skip(['LGRMV_P'], 'webhook',
                                          page=self.page)
        row = self._assert_one_skip('LGRMV_P')
        self.assertEqual(row.company_id, self.page.lead_company_id)

    def test_record_skip_converts_failed_row(self):
        failed = self.Log._record_failure('LGRMV_C', 'webhook', 'boom',
                                          page=self.page)
        self._remove('LGRMV_C')
        self.Log._record_removed_skip(['LGRMV_C'], 'cron', page=self.page)
        row = self._assert_one_skip('LGRMV_C')
        self.assertEqual(row, failed)
        self.assertEqual(row.trigger, 'webhook')
        # The one-open-failed-row index is free again.
        self.Log._record_failure('LGRMV_C', 'webhook', 'later',
                                 page=self.page)
        self.assertEqual(len(self._rows('LGRMV_C', 'failed')), 1)

    def test_record_skip_ignores_ids_not_removed(self):
        failed = self.Log._record_failure('LGRMV_N', 'webhook', 'boom',
                                          page=self.page)
        self.Log._record_removed_skip(['LGRMV_N', 'LGRMV_N2'], 'webhook',
                                      page=self.page)
        self.assertEqual(failed.status, 'failed')
        self.assertFalse(self._rows('LGRMV_N2'))

    # -- the drain ------------------------------------------------------- #
    def test_drain_settles_removed_event(self):
        self._remove('LGRMV_D')
        event = self._queue('LGRMV_D')
        self._drain()
        self._fetch_lead.assert_not_called()
        self.assertEqual(event.status, 'done')
        self.assertFalse(event.lead_id)
        self.assertEqual(event.error_message, _REMOVED)
        self.assertEqual(event.attempts, 0)
        self.assertFalse(self._leads('LGRMV_D'))
        row = self._assert_one_skip('LGRMV_D')
        self.assertEqual(row.trigger, 'webhook')
        # Done events are not picked up again.
        self._drain()
        self._assert_one_skip('LGRMV_D')

    @mute_logger(_DRAIN_LOGGER)
    def test_drain_fallback_settles_removed_event(self):
        self._remove('LGRMV_F')
        event = self._queue('LGRMV_F')
        with mock.patch.object(self.EventClass, '_process_one',
                               side_effect=RuntimeError('boom')):
            self._drain()
        self.assertEqual(event.status, 'done')
        self.assertEqual(event.error_message, _REMOVED)
        self.assertFalse(self._rows('LGRMV_F', 'failed'))
        row = self._assert_one_skip('LGRMV_F')
        # The new row carries the Page, so it isn't shared across companies.
        self.assertEqual(row.page_id, self.page)
        self.assertTrue(row.company_id)
        self.assertEqual(row.company_id, self.page.lead_company_id)

    @mute_logger(_DRAIN_LOGGER)
    def test_drain_fallback_still_fails_other_leads(self):
        event = self._queue('LGRMV_F2')
        with mock.patch.object(self.EventClass, '_process_one',
                               side_effect=RuntimeError('boom')):
            self._drain()
        self.assertEqual(event.status, 'failed')
        self.assertEqual(len(self._rows('LGRMV_F2', 'failed')), 1)

    # -- the sweeps ------------------------------------------------------ #
    def _listed(self, leadgen_id):
        return self._fake_lead(id=leadgen_id,
                               created_time='2026-06-14T10:00:00+0000')

    def test_sweep_prefilter_records_one_skip(self):
        self._remove('LGRMV_S')
        for _i in range(2):
            with self._patch_graph():
                outcome = self.form._ingest_listed(
                    [self._listed('LGRMV_S')], {})
                self._fetch_lead.assert_not_called()
            self.assertFalse(outcome['stop'])
            self.assertEqual(outcome['streak'], 0)
            self.assertEqual(outcome['quarantined'], [])
            self.assertTrue(outcome['settled_until'])
        row = self._assert_one_skip('LGRMV_S')
        self.assertEqual(row.trigger, 'cron')
        self.assertEqual(row.page_id, self.page)
        self.assertFalse(self._leads('LGRMV_S'))

    def test_sweep_race_records_one_skip(self):
        # The lead is deleted after the list was filtered: the pre-filter
        # misses it and the gate in ingest stops it.
        self._remove('LGRMV_R')
        real = self.TombClass._removed
        calls = []

        def removed(tomb, ids):
            calls.append(ids)
            return set() if len(calls) == 1 else real(tomb, ids)

        with mock.patch.object(self.TombClass, '_removed', removed), \
                self._patch_graph():
            outcome = self.form._ingest_listed([self._listed('LGRMV_R')], {},
                                               streak=1)
            self._fetch_lead.assert_not_called()
        self.assertGreater(len(calls), 1)
        self.assertFalse(outcome['stop'])
        # Neither counted nor reset.
        self.assertEqual(outcome['streak'], 1)
        self.assertEqual(outcome['quarantined'], [])
        self.assertTrue(outcome['settled_until'])
        self._assert_one_skip('LGRMV_R')
        self.assertFalse(self._leads('LGRMV_R'))

    # -- retention ------------------------------------------------------- #
    def test_vacuum_prunes_removed_skips(self):
        self._remove('LGRMV_V')
        row = self.Log._record_removed_skip(['LGRMV_V'], 'webhook',
                                            page=self.page)
        self.env.flush_all()
        self.env.cr.execute(
            "UPDATE meta_sync_log SET create_date = now() - interval '60 days'"
            " WHERE id = %s", (row.id,))
        self.env.invalidate_all()
        # Kept while the lead is recorded as removed, so a later sweep
        # doesn't write the skip again.
        self.Log._cron_vacuum_logs()
        self.assertTrue(row.exists())
        with self._patch_graph():
            self.form._ingest_listed([self._listed('LGRMV_V')], {})
        self.assertEqual(self._assert_one_skip('LGRMV_V'), row)
        self.Tomb.sudo().search([('leadgen_id', '=', 'LGRMV_V')]).unlink()
        self.Log._cron_vacuum_logs()
        self.assertFalse(row.exists())

    # -- sync-log Retry -------------------------------------------------- #
    def _failed(self, leadgen_id, with_event=True):
        row = self.Log._record_failure(leadgen_id, 'webhook', 'boom',
                                       page=self.page)
        event = self.Event.browse()
        if with_event:
            event = self._queue(leadgen_id, status='failed',
                                error_message='boom')
        return row, event

    def _log_retry(self, rows):
        with self._patch_graph():
            result = self.env['meta.sync.log'].browse(rows.ids).with_user(
                self.admin_a).action_retry()
        self.env.invalidate_all()
        return result

    def test_log_retry_settles_removed_row(self):
        row, event = self._failed('LGRMV_SR')
        self._remove('LGRMV_SR')
        result = self._log_retry(row)
        self._fetch_lead.assert_not_called()
        self.assertEqual(self._assert_one_skip('LGRMV_SR'), row)
        self.assertEqual(event.status, 'done')
        self.assertFalse(event.lead_id)
        self.assertEqual(event.error_message, _REMOVED)
        self.assertFalse(self._leads('LGRMV_SR'))
        params = result['params']
        self.assertEqual(params['type'], 'success')
        self.assertIn('0 still failed', params['message'])
        self.assertIn('deleted in Odoo', params['message'])
        # The index is free for a later failure.
        self.Log._record_failure('LGRMV_SR', 'webhook', 'later',
                                 page=self.page)
        self.assertEqual(len(self._rows('LGRMV_SR', 'failed')), 1)

    def test_log_retry_again_changes_nothing(self):
        row, _event = self._failed('LGRMV_SA')
        self._remove('LGRMV_SA')
        self._log_retry(row)
        with self.assertRaises(UserError):
            self._log_retry(row)
        # Reached directly, the settled row reads as removed, not busy.
        self.assertEqual(row._retry_one(), 'removed')
        self._assert_one_skip('LGRMV_SA')

    @mute_logger(_LOG_LOGGER)
    def test_log_retry_fallback_settles_removed_row(self):
        row, event = self._failed('LGRMV_SF')
        self._remove('LGRMV_SF')
        with mock.patch.object(self.LogClass, '_retry_one',
                               side_effect=RuntimeError('boom')):
            result = self._log_retry(row)
        self.assertEqual(self._assert_one_skip('LGRMV_SF'), row)
        self.assertEqual(event.status, 'done')
        self.assertFalse(self._rows('LGRMV_SF', 'failed'))
        self.assertIn('0 still failed', result['params']['message'])
        self.assertIn('1 of them were deleted in Odoo',
                      result['params']['message'])

    # -- queue Retry ----------------------------------------------------- #
    def _queue_retry(self, events):
        with self._patch_graph():
            result = self.env['meta.webhook.event'].browse(
                events.ids).with_user(self.admin_a).action_retry()
        self.env.invalidate_all()
        return result

    def _miss_first_check(self):
        """Patch the tombstone lookup so its first call finds nothing."""
        real = self.TombClass._removed
        calls = []

        def removed(tomb, ids):
            calls.append(ids)
            return set() if len(calls) == 1 else real(tomb, ids)

        return mock.patch.object(self.TombClass, '_removed', removed)

    def test_queue_retry_settles_removed_event(self):
        _row, event = self._failed('LGRMV_Q')
        self._remove('LGRMV_Q')
        result = self._queue_retry(event)
        self._fetch_lead.assert_not_called()
        self.assertEqual(event.status, 'done')
        self.assertFalse(event.lead_id)
        self._assert_one_skip('LGRMV_Q')
        self.assertIn('0 still failed', result['params']['message'])
        self.assertIn('1 of them were deleted in Odoo',
                      result['params']['message'])
        # Done now, so a second click has nothing to retry.
        with self.assertRaises(UserError):
            self._queue_retry(event)
        self._assert_one_skip('LGRMV_Q')

    def test_queue_retry_engine_settles_removed_event(self):
        # The queue's own check misses, so the retry engine meets the gate.
        _row, event = self._failed('LGRMV_QE')
        self._remove('LGRMV_QE')
        with self._miss_first_check():
            result = self._queue_retry(event)
        self._fetch_lead.assert_not_called()
        self.assertEqual(event.status, 'done')
        self._assert_one_skip('LGRMV_QE')
        self.assertIn('1 of them were deleted in Odoo',
                      result['params']['message'])

    def test_queue_retry_removed_not_counted_as_sent(self):
        _row, removed_event = self._failed('LGRMV_Q1')
        self._remove('LGRMV_Q1')
        _row, other = self._failed('LGRMV_Q2')
        with mock.patch.object(const, 'RETRY_BATCH_LIMIT', 1), \
                self._patch_graph(fetch_lead=self._fake_lead(id='LGRMV_Q2')):
            result = self.env['meta.webhook.event'].browse(
                (removed_event | other).ids).with_user(
                    self.admin_a).action_retry()
        self.env.invalidate_all()
        self.assertEqual(removed_event.status, 'done')
        # The removed lead used none of the batch, so the other was sent.
        self.assertEqual(other.status, 'done')
        self.assertTrue(other.lead_id)
        self.assertNotIn('were not retried', result['params']['message'])

    @mute_logger(_EVENT_LOGGER)
    def test_fail_retry_keeps_removed_outcome(self):
        # The retry reaches the removed outcome, then something raises in
        # the same savepoint. The rollback undoes the skip, and _fail_retry
        # must settle it again instead of failing the event.
        _row, event = self._failed('LGRMV_QF')
        self._remove('LGRMV_QF')
        real_retry = self.LogClass._retry_one
        outcomes = []

        def retry_then_raise(row, held=None):
            outcomes.append(real_retry(row, held=held))
            raise RuntimeError('after the outcome')

        with self._miss_first_check(), \
                mock.patch.object(self.LogClass, '_retry_one',
                                  retry_then_raise):
            result = self._queue_retry(event)
        self.assertEqual(outcomes, ['removed'])
        self.assertEqual(event.status, 'done')
        self.assertEqual(event.error_message, _REMOVED)
        self.assertFalse(self._rows('LGRMV_QF', 'failed'))
        self._assert_one_skip('LGRMV_QF')
        self.assertIn('0 still failed', result['params']['message'])
        self.assertIn('1 of them were deleted in Odoo',
                      result['params']['message'])

    @mute_logger(_EVENT_LOGGER)
    def test_fail_retry_generic_error_on_removed_lead(self):
        _row, event = self._failed('LGRMV_QG')
        self._remove('LGRMV_QG')
        with self._miss_first_check(), \
                mock.patch.object(self.LogClass, '_retry_one',
                                  side_effect=RuntimeError('boom')):
            result = self._queue_retry(event)
        self.assertEqual(event.status, 'done')
        self.assertFalse(self._rows('LGRMV_QG', 'failed'))
        self._assert_one_skip('LGRMV_QG')
        self.assertIn('1 of them were deleted in Odoo',
                      result['params']['message'])

    def test_notification_counts_removed(self):
        result = self.env['meta.sync.log']._retry_notification(
            0, 0, removed=2)
        params = result['params']
        self.assertEqual(params['type'], 'success')
        self.assertIn('2 retried', params['message'])
        self.assertIn('2 of them were deleted in Odoo', params['message'])
        self.assertIn('Configuration > Removed Meta Leads', params['message'])


@tagged('post_install', '-at_install')
class TestLeadgenIdGuard(MultiCompanyFixtureMixin, IngestFixtureMixin,
                         TransactionCase):
    """Only ingest sets crm.lead.meta_leadgen_id, so no user can bring back
    a removed lead or claim an id before Meta sends it."""

    def _leads(self, leadgen_id):
        return self.Lead.with_context(active_test=False).search(
            [('meta_leadgen_id', '=', leadgen_id)])

    def test_user_cannot_create_with_leadgen_id(self):
        self.env['meta.leadgen.tombstone']._remember(['LGRMV_U1'])
        for user in (self.sales_a, self.admin_a):
            with self.assertRaises(AccessError):
                self.Lead.with_user(user).create(
                    {'name': 'Planted', 'meta_leadgen_id': 'LGRMV_U1'})
        self.assertFalse(self._leads('LGRMV_U1'))
        # Leads without one are created as usual.
        lead = self.Lead.with_user(self.sales_a).create(
            {'name': 'Plain', 'meta_leadgen_id': False})
        self.assertTrue(lead)

    def test_user_cannot_write_leadgen_id(self):
        lead = self.Lead.with_user(self.sales_a).create({'name': 'Plain'})
        stamped = self.Lead.create({'name': 'Meta',
                                    'meta_leadgen_id': 'LGRMV_U3'})
        with self.assertRaises(AccessError):
            lead.with_user(self.sales_a).write(
                {'meta_leadgen_id': 'LGRMV_U2'})
        with self.assertRaises(AccessError):
            stamped.with_user(self.sales_a).write({'meta_leadgen_id': False})
        self.assertFalse(lead.meta_leadgen_id)
        self.assertEqual(stamped.meta_leadgen_id, 'LGRMV_U3')
        # Other edits still work.
        stamped.with_user(self.sales_a).write({'name': 'Renamed'})
        self.assertEqual(stamped.name, 'Renamed')

    def test_import_cannot_set_leadgen_id(self):
        result = self.Lead.with_user(self.sales_a).load(
            ['name', 'meta_leadgen_id'], [['Imported', 'LGRMV_U4']])
        self.assertFalse([i for i in result['ids'] or [] if i])
        self.assertIn('set by the integration only',
                      ' '.join(m['message'] for m in result['messages']))
        self.assertFalse(self._leads('LGRMV_U4'))

    def test_context_defaults_cannot_plant_guarded_fields(self):
        defaults = {
            'default_meta_leadgen_id': 'LGRMV_U6',
            'default_meta_campaign_id': 'CAMP_X',
            'default_meta_form_id': 'FORM_X',
            'default_meta_attribution_pending': True,
            'default_meta_page_id_ref': self.page_b.id,
        }
        lead = self.Lead.with_user(self.sales_a).with_context(
            **defaults).create({'name': 'Default planted'})
        lead = lead.sudo()
        self.assertFalse(lead.meta_leadgen_id)
        self.assertFalse(lead.meta_campaign_id)
        self.assertFalse(lead.meta_form_id)
        self.assertFalse(lead.meta_attribution_pending)
        self.assertFalse(lead.meta_page_id_ref)
        self.assertFalse(self._leads('LGRMV_U6'))
        # A readable Page given as a default is still used.
        sales_admin_a = new_test_user(
            self.env, 'rm_sales_admin_a',
            groups='base.group_user,sales_team.group_sale_salesman_all_leads,'
                   'meta_lead_ads.group_meta_admin',
            company_id=self.co_a.id,
            company_ids=[Command.set([self.co_a.id])])
        own = self.Lead.with_user(sales_admin_a).with_context(
            default_meta_page_id_ref=self.page.id).create({'name': 'Own'})
        self.assertEqual(own.sudo().meta_page_id_ref, self.page)
        # Superuser code keeps the defaults it passes.
        su_lead = self.Lead.sudo().with_context(
            default_meta_leadgen_id='LGRMV_U7').create({'name': 'Su'})
        self.assertEqual(su_lead.meta_leadgen_id, 'LGRMV_U7')

    def test_ingest_still_sets_leadgen_id(self):
        with self._patch_graph(fetch_lead=self._fake_lead(id='LGRMV_U5')):
            lead = self.Ingest.with_user(self.admin_a).ingest_leadgen(
                self.page, 'LGRMV_U5')
        self.assertEqual(lead.sudo().meta_leadgen_id, 'LGRMV_U5')


@tagged('post_install', '-at_install')
class TestRemovedLeadRecords(MultiCompanyFixtureMixin, IngestFixtureMixin,
                             TransactionCase):
    """What a removed-lead record keeps, and who can read it."""

    _MAGIC = {'id', 'create_uid', 'create_date', 'write_uid', 'write_date'}

    def setUp(self):
        super().setUp()
        self.Tomb = self.env['meta.leadgen.tombstone']
        self.LeadClass = type(self.env['crm.lead'])
        self.sys_admin_a = new_test_user(
            self.env, 'rm_sys_admin_a',
            groups='base.group_user,base.group_system,'
                   'meta_lead_ads.group_meta_admin',
            company_id=self.co_a.id,
            company_ids=[Command.set([self.co_a.id])])

    def _meta_lead(self, leadgen_id, **vals):
        vals = dict({'name': 'Lead %s' % leadgen_id, 'type': 'lead',
                     'meta_leadgen_id': leadgen_id,
                     'meta_page_id_ref': self.page.id,
                     'meta_form_id_ref': self.form.id,
                     'company_id': self.co_a.id,
                     'email_from': '%s@example.com' % leadgen_id.lower()},
                    **vals)
        return self.Lead.create(vals)

    def _tomb(self, leadgen_id):
        return self.Tomb.sudo().search([('leadgen_id', '=', leadgen_id)])

    def test_delete_keeps_links_and_reason(self):
        self._meta_lead('LGRT1').unlink()
        tomb = self._tomb('LGRT1')
        self.assertEqual(len(tomb), 1)
        self.assertEqual(tomb.page_id, self.page)
        self.assertEqual(tomb.form_id, self.form)
        self.assertEqual(tomb.company_id, self.co_a)
        self.assertEqual(tomb.removal_reason, 'deleted')
        self.assertFalse(tomb.merged_into_id)
        self.Tomb._remember(['LGRT1'], details={'LGRT1': {
            'reason': 'merged'}})
        self.assertEqual(len(self._tomb('LGRT1')), 1)
        self.assertEqual(self._tomb('LGRT1').removal_reason, 'deleted')

    def test_blank_company_lead_takes_page_company(self):
        lead = self._meta_lead('LGRT1B', company_id=False, user_id=False,
                               team_id=False)
        self.assertFalse(lead.company_id)
        self.assertEqual(self.page.lead_company_id, self.co_a)
        lead.unlink()
        self.assertEqual(self._tomb('LGRT1B').company_id, self.co_a)

    def test_unlinked_lead_takes_sync_log_page(self):
        self.env['meta.sync.log'].sudo().create({
            'meta_leadgen_id': 'LGRT1C', 'status': 'success',
            'trigger': 'webhook', 'page_id': self.page_b.id})
        lead = self._meta_lead('LGRT1C', company_id=False, user_id=False,
                               team_id=False, meta_page_id_ref=False,
                               meta_form_id_ref=False)
        self.assertFalse(lead.company_id)
        lead.unlink()
        tomb = self._tomb('LGRT1C')
        self.assertEqual(tomb.page_id, self.page_b)
        self.assertEqual(tomb.company_id, self.co_b)
        self.assertTrue(tomb.with_user(self.admin_b).has_access('read'))

    def test_unlinked_lead_takes_event_page(self):
        self.env['meta.webhook.event'].sudo().create({
            'leadgen_id': 'LGRT1D', 'page_id': self.page.page_id,
            'form_id': 'F1', 'status': 'done'})
        lead = self._meta_lead('LGRT1D', company_id=False, user_id=False,
                               team_id=False, meta_page_id_ref=False,
                               meta_form_id_ref=False)
        lead.unlink()
        tomb = self._tomb('LGRT1D')
        self.assertEqual(tomb.page_id, self.page)
        self.assertEqual(tomb.company_id, self.co_a)

    def test_untraced_lead_keeps_no_company(self):
        lead = self._meta_lead('LGRT1E', company_id=False, user_id=False,
                               team_id=False, meta_page_id_ref=False,
                               meta_form_id_ref=False)
        lead.unlink()
        tomb = self._tomb('LGRT1E')
        self.assertFalse(tomb.page_id or tomb.company_id)

    def test_merge_records_surviving_lead(self):
        leads = self._meta_lead('LGRTM1') + self._meta_lead('LGRTM2')
        ordered = leads._sort_by_confidence_level(reverse=True)
        tail_id = ordered[1].meta_leadgen_id
        head = leads._merge_opportunity()
        self.assertEqual(head, ordered[0])
        self.assertFalse(ordered[1].exists())
        tomb = self._tomb(tail_id)
        self.assertEqual(tomb.removal_reason, 'merged')
        self.assertEqual(tomb.merged_into_id, head)
        self.assertFalse(self._tomb(head.meta_leadgen_id))

    def test_merge_through_public_method(self):
        leads = self._meta_lead('LGRTM3') + self._meta_lead('LGRTM4')
        head = leads.merge_opportunity()
        gone = ({'LGRTM3', 'LGRTM4'} - {head.meta_leadgen_id}).pop()
        tomb = self._tomb(gone)
        self.assertEqual(tomb.removal_reason, 'merged')
        self.assertEqual(tomb.merged_into_id, head)

    def test_failed_merge_leaves_plain_delete(self):
        leads = self._meta_lead('LGRTM5') + self._meta_lead('LGRTM6')
        tail = leads._sort_by_confidence_level(reverse=True)[1]
        tail_id = tail.meta_leadgen_id
        write = self.LeadClass.write

        def failing_write(rec, vals):
            # Fails only once the merge has recorded the tail.
            if tail.id in rec.env.cr.cache.get(
                    'meta_lead_ads.merge_heads', {}):
                raise UserError("merge failed")
            return write(rec, vals)

        # A savepoint that doesn't clear the cursor cache on rollback, so
        # only the merge override can drop the recorded tail.
        with mock.patch.object(self.LeadClass, 'write', failing_write):
            with self.assertRaises(UserError),                     self.env.cr.savepoint(flush=False):
                leads._merge_opportunity()
        self.env.invalidate_all()
        self.assertTrue(tail.exists())
        tail.unlink()
        self.assertEqual(self._tomb(tail_id).removal_reason, 'deleted')

    def test_head_deleted_with_tail(self):
        leads = self._meta_lead('LGRTM7') + self._meta_lead('LGRTM8')
        head = leads._merge_opportunity(auto_unlink=False)
        head_id = head.meta_leadgen_id
        gone = ({'LGRTM7', 'LGRTM8'} - {head_id}).pop()
        leads.unlink()
        tomb = self._tomb(gone)
        self.assertEqual(tomb.removal_reason, 'merged')
        self.assertFalse(tomb.merged_into_id)
        self.assertEqual(self._tomb(head_id).removal_reason, 'deleted')

    def test_merge_of_head_moves_link(self):
        leads = self._meta_lead('LGRTM9') + self._meta_lead('LGRTM10')
        ordered = leads._sort_by_confidence_level(reverse=True)
        tail_id = ordered[1].meta_leadgen_id
        head = leads._merge_opportunity()
        head_id = head.meta_leadgen_id
        # An opportunity ranks above a lead, so the first head is merged
        # away into it.
        opp = self._meta_lead('LGRTM11', type='opportunity')
        new_head = (head + opp)._merge_opportunity()
        self.assertEqual(new_head, opp)
        self.assertFalse(head.exists())
        self.env.invalidate_all()
        self.assertEqual(self._tomb(tail_id).merged_into_id, opp)
        self.assertEqual(self._tomb(head_id).merged_into_id, opp)

    def test_old_call_shape(self):
        self.Tomb._remember(['LGRT2'])
        tomb = self._tomb('LGRT2')
        self.assertEqual(len(tomb), 1)
        self.assertFalse(tomb.page_id or tomb.form_id or tomb.company_id
                         or tomb.removal_reason or tomb.merged_into_id)

    def test_no_personal_data_fields(self):
        self.assertEqual(set(self.Tomb._fields) - self._MAGIC, {
            'leadgen_id', 'page_id', 'form_id', 'company_id',
            'removal_reason', 'merged_into_id', 'merged_into_ref',
            'expires_on', 'display_name'})

    def test_expires_on(self):
        self._meta_lead('LGRT3').unlink()
        tomb = self._tomb('LGRT3')
        self.assertEqual(tomb.expires_on,
                         tomb.create_date + timedelta(days=200))

    def test_company_rules(self):
        self._meta_lead('LGRTA').unlink()
        self._meta_lead('LGRTB', meta_page_id_ref=self.page_b.id,
                        meta_form_id_ref=False,
                        company_id=self.co_b.id).unlink()
        self.Tomb._remember(['LGRTN'])
        ids = ['LGRTA', 'LGRTB', 'LGRTN']

        def seen(user):
            return set(self.Tomb.with_user(user).search(
                [('leadgen_id', 'in', ids)]).mapped('leadgen_id'))

        self.assertEqual(seen(self.admin_a), {'LGRTA'})
        self.assertEqual(seen(self.admin_b), {'LGRTB'})
        self.assertEqual(seen(self.sys_admin_a), {'LGRTA', 'LGRTN'})
        with self.assertRaises(AccessError):
            self.Tomb.with_user(self.sales_a).search([])
        with self.assertRaises(AccessError):
            self._tomb('LGRTB').with_user(self.admin_a).check_access('read')


@tagged('post_install', '-at_install')
class TestRemovedLeadRestore(MultiCompanyFixtureMixin, IngestFixtureMixin,
                             TransactionCase):
    """Restore is the one way back for a removed lead."""

    def setUp(self):
        super().setUp()
        self.Tomb = self.env['meta.leadgen.tombstone']
        self.Log = self.env['meta.sync.log'].sudo()
        self.IngestClass = type(self.env['meta.lead.ingest'])
        self.crm_admin_a = new_test_user(
            self.env, 'rm_crm_admin_a',
            groups='base.group_user,sales_team.group_sale_salesman_all_leads,'
                   'meta_lead_ads.group_meta_admin',
            company_id=self.co_a.id,
            company_ids=[Command.set([self.co_a.id])])
        self.page2 = self.env['meta.page'].create({
            'name': 'Page 2', 'page_id': 'PG2', 'access_token': 'tok_2',
            'account_id': self.account.id,
        })

    def _seed(self, leadgen_id, page=None, company=None, reason='deleted'):
        details = {'page_id': page.id if page else None,
                   'company_id': (company or self.co_a).id,
                   'reason': reason}
        self.Tomb._remember([leadgen_id], details={leadgen_id: details})
        return self._tomb(leadgen_id)

    def _tomb(self, leadgen_id):
        return self.Tomb.sudo().search([('leadgen_id', '=', leadgen_id)])

    def _leads(self, leadgen_id):
        return self.Lead.with_context(active_test=False).search(
            [('meta_leadgen_id', '=', leadgen_id)])

    def _restore(self, tomb, user, fetch_lead=None):
        with self._patch_graph(fetch_lead=fetch_lead):
            return tomb.with_user(user).action_restore()

    def _meta_lead(self, leadgen_id):
        return self.Lead.create({
            'name': 'Lead %s' % leadgen_id, 'type': 'lead',
            'meta_leadgen_id': leadgen_id, 'meta_page_id_ref': self.page.id,
            'company_id': self.co_a.id,
            'email_from': '%s@example.com' % leadgen_id.lower()})

    # -- success ----------------------------------------------------------- #
    def test_restore_opens_new_lead(self):
        tomb = self._seed('LGRS1', page=self.page)
        action = self._restore(tomb, self.crm_admin_a,
                               fetch_lead=self._fake_lead(id='LGRS1'))
        self.assertFalse(tomb.exists())
        lead = self._leads('LGRS1')
        self.assertEqual(len(lead), 1)
        self.assertTrue(self.Log.search_count([
            ('meta_leadgen_id', '=', 'LGRS1'), ('status', '=', 'success'),
            ('trigger', '=', 'manual')]))
        self.assertEqual(action['type'], 'ir.actions.act_window')
        self.assertEqual(action['res_model'], 'crm.lead')
        self.assertEqual(action['res_id'], lead.id)

    def test_restore_notifies_when_lead_unreadable(self):
        tomb = self._seed('LGRS2', page=self.page)
        action = self._restore(tomb, self.admin_a,
                               fetch_lead=self._fake_lead(id='LGRS2'))
        self.assertEqual(action['tag'], 'display_notification')
        self.assertTrue(self._leads('LGRS2'))

    def test_restore_record_without_reason(self):
        self.Tomb._remember(['LGRS3'], details={'LGRS3': {
            'page_id': self.page.id, 'company_id': self.co_a.id}})
        tomb = self._tomb('LGRS3')
        self.assertFalse(tomb.removal_reason)
        self._restore(tomb, self.crm_admin_a,
                      fetch_lead=self._fake_lead(id='LGRS3'))
        self.assertFalse(tomb.exists())
        self.assertTrue(self._leads('LGRS3'))

    # -- merged ------------------------------------------------------------ #
    def test_merged_lead_refused(self):
        leads = self._meta_lead('LGRSM1') + self._meta_lead('LGRSM2')
        ordered = leads._sort_by_confidence_level(reverse=True)
        head, tail_id = ordered[0], ordered[1].meta_leadgen_id
        leads._merge_opportunity()
        tomb = self._tomb(tail_id)
        fetch = mock.Mock(return_value=self._fake_lead(id=tail_id))
        with self.assertRaises(UserError) as cm:
            self._restore(tomb, self.crm_admin_a, fetch_lead=fetch)
        self.assertIn('#%d' % head.id, str(cm.exception))
        self.assertNotIn(head.name, str(cm.exception))
        self.assertTrue(tomb.exists())
        fetch.assert_not_called()

    def test_merged_lead_restored_once_head_deleted(self):
        leads = self._meta_lead('LGRSM3') + self._meta_lead('LGRSM4')
        ordered = leads._sort_by_confidence_level(reverse=True)
        head, tail_id = ordered[0], ordered[1].meta_leadgen_id
        leads._merge_opportunity()
        tomb = self._tomb(tail_id)
        # Nothing is left to duplicate once the surviving lead is deleted.
        head.unlink()
        self.env.invalidate_all()
        self.assertFalse(tomb.merged_into_id)
        self.assertEqual(tomb.removal_reason, 'merged')
        self._restore(tomb, self.crm_admin_a,
                      fetch_lead=self._fake_lead(id=tail_id))
        self.assertFalse(tomb.exists())
        self.assertEqual(len(self._leads(tail_id)), 1)

    # -- failures keep the record ------------------------------------------ #
    def test_fetch_failure_keeps_record(self):
        tomb = self._seed('LGRS4', page=self.page)
        for error in (MetaPermanentError('gone'),
                      MetaAuthError('token expired')):
            with self.assertRaises(UserError) as cm:
                self._restore(tomb, self.crm_admin_a,
                              fetch_lead=mock.Mock(side_effect=error))
            self.assertIn(str(error), str(cm.exception))
            self.assertTrue(tomb.exists())
            self.assertFalse(self._leads('LGRS4'))

    def test_rate_limit_pauses_page(self):
        tomb = self._seed('LGRS4R', page=self.page)
        throttle = mock.Mock(side_effect=MetaRateLimitError(
            'slow down', code=80005, retry_after_min=10))
        action = self._restore(tomb, self.crm_admin_a, fetch_lead=throttle)
        self.assertEqual(action['tag'], 'display_notification')
        self.assertEqual(action['params']['type'], 'warning')
        self.assertTrue(tomb.exists())
        self.assertFalse(self._leads('LGRS4R'))
        self.page.invalidate_recordset()
        self.assertTrue(self.page.lead_fetch_paused_until)
        # A second click doesn't call Meta while the pause lasts.
        fetch = mock.Mock(return_value=self._fake_lead(id='LGRS4R'))
        with self.assertRaises(UserError) as cm:
            self._restore(tomb, self.crm_admin_a, fetch_lead=fetch)
        self.assertIn('pause lead reads', str(cm.exception))
        fetch.assert_not_called()
        self.assertTrue(tomb.exists())

    def test_app_rate_limit_pauses_all_pages(self):
        tomb = self._seed('LGRS4A', page=self.page)
        throttle = mock.Mock(side_effect=MetaRateLimitError('slow down'))
        action = self._restore(tomb, self.crm_admin_a, fetch_lead=throttle)
        self.assertEqual(action['params']['type'], 'warning')
        self.assertTrue(tomb.exists())
        self.assertFalse(self.page.sudo().lead_fetch_paused_until)
        self.assertTrue(self.env['meta.webhook.event']
                        ._app_lead_fetch_paused_until())

    def test_removed_during_restore_shows_refusal(self):
        tomb = self._seed('LGRS5', page=self.page)
        with mock.patch.object(
                self.IngestClass, 'ingest_leadgen',
                side_effect=MetaLeadRemovedError('removed')):
            with self.assertRaises(UserError) as cm:
                tomb.with_user(self.crm_admin_a).action_restore()
        self.assertEqual(
            str(cm.exception),
            tomb.with_user(self.crm_admin_a)._refusal_message())
        self.assertNotIn('Meta did not return', str(cm.exception))
        self.assertTrue(tomb.exists())

    # -- no Page ----------------------------------------------------------- #
    def test_no_page_keeps_record(self):
        tomb = self._seed('LGRS6')
        fetch = mock.Mock(return_value=self._fake_lead(id='LGRS6'))
        with self.assertRaises(UserError) as cm:
            self._restore(tomb, self.crm_admin_a, fetch_lead=fetch)
        self.assertIn('Ingest Meta Lead by ID', str(cm.exception))
        self.assertNotIn('Meta Page id', str(cm.exception))
        self.assertTrue(tomb.exists())
        self.assertFalse(self._leads('LGRS6'))
        fetch.assert_not_called()

    def test_no_page_names_unknown_meta_page(self):
        tomb = self._seed('LGRS7')
        self.env['meta.webhook.event'].sudo().create({
            'leadgen_id': 'LGRS7', 'page_id': 'PGUNKNOWN', 'form_id': 'F1',
            'status': 'done'})
        with self.assertRaises(UserError) as cm:
            self._restore(tomb, self.crm_admin_a)
        self.assertIn('Ingest Meta Lead by ID', str(cm.exception))
        self.assertIn('PGUNKNOWN', str(cm.exception))
        self.assertTrue(tomb.exists())

    def test_page_out_of_reach_not_named(self):
        # The event points at company B's Page, which admin A can't read.
        tomb = self._seed('LGRS8')
        self.env['meta.webhook.event'].sudo().create({
            'leadgen_id': 'LGRS8', 'page_id': 'PGB', 'form_id': 'F1',
            'status': 'done'})
        fetch = mock.Mock(return_value=self._fake_lead(id='LGRS8'))
        with self.assertRaises(UserError) as cm:
            self._restore(tomb, self.crm_admin_a, fetch_lead=fetch)
        self.assertIn("can't access", str(cm.exception))
        self.assertNotIn('PGB', str(cm.exception))
        self.assertTrue(tomb.exists())
        fetch.assert_not_called()

    # -- Page fallback ------------------------------------------------------ #
    def test_fallback_prefers_success_row(self):
        self.Log.create({'meta_leadgen_id': 'LGRS9', 'status': 'success',
                         'trigger': 'webhook', 'page_id': self.page.id})
        self.Log.create({'meta_leadgen_id': 'LGRS9', 'status': 'failed',
                         'trigger': 'webhook', 'page_id': self.page2.id})
        tomb = self._seed('LGRS9')
        fetch = mock.Mock(return_value=self._fake_lead(id='LGRS9'))
        self._restore(tomb, self.crm_admin_a, fetch_lead=fetch)
        self.assertEqual(fetch.call_args[0][0].id, self.page.id)
        self.assertTrue(self._leads('LGRS9'))

    def test_fallback_uses_any_row(self):
        self.Log.create({'meta_leadgen_id': 'LGRS10', 'status': 'failed',
                         'trigger': 'webhook', 'page_id': self.page2.id})
        tomb = self._seed('LGRS10')
        fetch = mock.Mock(return_value=self._fake_lead(id='LGRS10'))
        self._restore(tomb, self.crm_admin_a, fetch_lead=fetch)
        self.assertEqual(fetch.call_args[0][0].id, self.page2.id)

    # -- access ------------------------------------------------------------ #
    def test_access(self):
        tomb_a = self._seed('LGRS11', page=self.page)
        tomb_b = self._seed('LGRS12', page=self.page_b, company=self.co_b)
        self.Tomb._remember(['LGRS13'], details={'LGRS13': {
            'page_id': self.page.id}})
        tomb_n = self._tomb('LGRS13')
        fetch = mock.Mock(return_value=self._fake_lead(id='LGRS11'))
        with self.assertRaises(AccessError):
            self._restore(tomb_a, self.sales_a, fetch_lead=fetch)
        for tomb in (tomb_b, tomb_n):
            with self.assertRaises(AccessError):
                self._restore(tomb, self.crm_admin_a, fetch_lead=fetch)
        fetch.assert_not_called()
        everything = tomb_a + tomb_b + tomb_n
        self.assertEqual(everything.exists(), everything)

    def test_lead_result_action(self):
        lead_a = self._meta_lead('LGRS14')
        lead_b = self.Lead.create({'name': 'B', 'company_id': self.co_b.id})
        Ingest = self.Ingest.with_user(self.sales_a)
        action = Ingest._lead_result_action(lead_a.with_user(self.sales_a))
        self.assertEqual(action['res_id'], lead_a.id)
        action = Ingest._lead_result_action(lead_b.with_user(self.sales_a))
        self.assertEqual(action['tag'], 'display_notification')


@tagged('post_install', '-at_install')
class TestRemovedLeadWizard(MultiCompanyFixtureMixin, IngestFixtureMixin,
                            TransactionCase):
    """The import form never brings a removed lead back."""

    def setUp(self):
        super().setUp()
        self.Tomb = self.env['meta.leadgen.tombstone']
        self.IngestClass = type(self.env['meta.lead.ingest'])

    def _deleted_by_manager(self, leadgen_id):
        lead = self.Lead.create({
            'name': 'Lead %s' % leadgen_id, 'type': 'lead',
            'meta_leadgen_id': leadgen_id, 'meta_page_id_ref': self.page.id,
            'company_id': self.co_a.id})
        lead.with_user(self.sales_mgr_a).unlink()
        return self.Tomb.sudo().search([('leadgen_id', '=', leadgen_id)])

    def _wizard(self, user, page, leadgen_id, **ctx):
        return self.env['meta.ingest.leadgen'].with_user(user).with_context(
            **ctx).create({'page_id': page.id, 'leadgen_id': leadgen_id})

    def _leads(self, leadgen_id):
        return self.Lead.with_context(active_test=False).search(
            [('meta_leadgen_id', '=', leadgen_id)])

    def test_refusal_names_date_and_user(self):
        tomb = self._deleted_by_manager('LGRW1')
        self.assertEqual(tomb.create_uid, self.sales_mgr_a)
        fetch = mock.Mock(return_value=self._fake_lead(id='LGRW1'))
        wizard = self._wizard(self.admin_a, self.page, ' LGRW1 ')
        with self._patch_graph(fetch_lead=fetch), \
                self.assertRaises(UserError) as cm:
            wizard.action_ingest()
        message = str(cm.exception)
        when = format_datetime(wizard.env, tomb.create_date)
        self.assertIn(when, message)
        self.assertIn(self.sales_mgr_a.name, message)
        self.assertIn('Removed Meta Leads', message)
        fetch.assert_not_called()
        self.assertFalse(self._leads('LGRW1'))

    def test_refusal_hides_details_from_other_company(self):
        tomb = self._deleted_by_manager('LGRW2')
        wizard = self._wizard(self.admin_b, self.page_b, 'LGRW2')
        with self._patch_graph(), self.assertRaises(UserError) as cm:
            wizard.action_ingest()
        message = str(cm.exception)
        self.assertIn('deleted in Odoo', message)
        self.assertNotIn(self.sales_mgr_a.name, message)
        self.assertNotIn(format_datetime(wizard.env, tomb.create_date),
                         message)
        self.assertNotIn('Removed Meta Leads', message)

    def test_no_restore_mode_in_context(self):
        tomb = self._deleted_by_manager('LGRW3')
        fetch = mock.Mock(return_value=self._fake_lead(id='LGRW3'))
        wizard = self._wizard(self.admin_a, self.page, 'LGRW3',
                              default_leadgen_id='LGRW3',
                              meta_restore_tombstone_id=tomb.id)
        with self._patch_graph(fetch_lead=fetch), \
                self.assertRaises(UserError):
            wizard.action_ingest()
        self.assertTrue(tomb.exists())
        self.assertFalse(self._leads('LGRW3'))
        fetch.assert_not_called()

    def test_removed_after_check_gives_refusal(self):
        # The lead is deleted between the form's check and the import.
        def removed_meanwhile(*args, **kwargs):
            self.Tomb.with_user(self.sales_mgr_a).sudo()._remember(
                ['LGRW4'], details={'LGRW4': {
                    'page_id': self.page.id, 'company_id': self.co_a.id,
                    'reason': 'deleted'}})
            raise MetaLeadRemovedError('removed')

        wizard = self._wizard(self.admin_a, self.page, 'LGRW4')
        with mock.patch.object(self.IngestClass, 'ingest_leadgen',
                               side_effect=removed_meanwhile), \
                self.assertRaises(UserError) as cm:
            wizard.action_ingest()
        self.assertIn(self.sales_mgr_a.name, str(cm.exception))
        self.assertIn('Removed Meta Leads', str(cm.exception))

    def test_other_ids_still_imported(self):
        wizard = self._wizard(self.admin_a, self.page, 'LGRW5')
        with self._patch_graph(fetch_lead=self._fake_lead(id='LGRW5')):
            action = wizard.action_ingest()
        self.assertTrue(self._leads('LGRW5'))
        self.assertEqual(action['tag'], 'display_notification')


@tagged('post_install', '-at_install')
class TestRemovedLeadViews(MultiCompanyFixtureMixin, IngestFixtureMixin,
                           TransactionCase):
    """The Removed Meta Leads list, form and menu."""

    def setUp(self):
        super().setUp()
        self.Tomb = self.env['meta.leadgen.tombstone']
        self.head = self.Lead.create({
            'name': 'Jane Survivor', 'type': 'lead',
            'company_id': self.co_a.id})
        self.Tomb._remember(['LGRV1', 'LGRV2'], details={
            'LGRV1': {'page_id': self.page.id, 'company_id': self.co_a.id,
                      'reason': 'merged', 'merged_into': self.head.id},
            'LGRV2': {'page_id': self.page.id, 'company_id': self.co_a.id,
                      'reason': 'deleted'}})
        self.merged = self.Tomb.sudo().search([('leadgen_id', '=', 'LGRV1')])

    def _arch(self, view_type, user=None):
        Tomb = self.Tomb.with_user(user or self.admin_a)
        view = self.env.ref('meta_lead_ads.meta_leadgen_tombstone_view_%s'
                            % view_type)
        arch = Tomb.get_view(view.id, view_type)['arch']
        return etree.fromstring(arch)

    def test_menu_admin_only(self):
        menu = self.env.ref('meta_lead_ads.menu_meta_leadgen_tombstone')
        self.assertEqual(menu.parent_id, self.env.ref('crm.crm_menu_config'))
        self.assertEqual(menu.groups_id,
                         self.env.ref('meta_lead_ads.group_meta_admin'))
        self.assertEqual(menu.action, self.env.ref(
            'meta_lead_ads.meta_leadgen_tombstone_action'))
        Menu = self.env['ir.ui.menu']
        self.assertIn(menu.id, Menu.with_user(self.admin_a)._visible_menu_ids())
        self.assertNotIn(menu.id,
                         Menu.with_user(self.sales_mgr_a)._visible_menu_ids())

    def test_list_arch(self):
        root = self._arch('list')
        self.assertEqual(root.tag, 'list')
        for attr in ('create', 'edit', 'delete'):
            self.assertEqual(root.get(attr), '0', attr)
        names = [f.get('name') for f in root.xpath('//field')]
        for name in ('leadgen_id', 'create_date', 'create_uid',
                     'removal_reason', 'merged_into_ref', 'page_id',
                     'form_id', 'expires_on'):
            self.assertIn(name, names)
        buttons = root.xpath("//button[@name='action_restore']")
        self.assertEqual(len(buttons), 1)
        self.assertEqual(buttons[0].get('invisible'),
                         "(removal_reason == 'merged' and merged_into_ref) "
                         "or removal_reason == 'erased'")
        self.assertIn('fetched from Meta again',
                      buttons[0].get('confirm') or '')
        self.assertIn('matching open lead', buttons[0].get('confirm') or '')

    def test_form_arch(self):
        root = self._arch('form')
        buttons = root.xpath("//header/button[@name='action_restore']")
        self.assertEqual(len(buttons), 1)
        self.assertEqual(buttons[0].get('invisible'),
                         "(removal_reason == 'merged' and merged_into_ref) "
                         "or removal_reason == 'erased'")
        confirm = buttons[0].get('confirm') or ''
        self.assertIn('fetched from Meta again', confirm)
        self.assertIn('matching open lead', confirm)
        self.assertIn('merge', confirm)

    def test_merge_target_shown_as_id_only(self):
        # A Meta Admin gets the id of the surviving lead, never its name.
        for view_type in ('list', 'form'):
            root = self._arch(view_type)
            self.assertFalse(root.xpath("//field[@name='merged_into_id']"),
                             view_type)
        tomb = self.merged.with_user(self.admin_a)
        data = tomb.web_read({'leadgen_id': {}, 'merged_into_ref': {},
                              'page_id': {'fields': {'display_name': {}}},
                              'create_uid': {'fields': {'display_name': {}}}})
        self.assertEqual(data[0]['merged_into_ref'], '#%d' % self.head.id)
        self.assertNotIn('Jane Survivor', repr(data))
        with self.assertRaises(AccessError):
            tomb.web_read({'merged_into_id': {
                'fields': {'display_name': {}}}})
        with self.assertRaises(AccessError):
            tomb.merged_into_id
        # Restore still refuses the merge and names only the id.
        with self.assertRaises(UserError) as cm:
            tomb.action_restore()
        self.assertIn('#%d' % self.head.id, str(cm.exception))
        self.assertNotIn('Jane Survivor', str(cm.exception))

    def test_list_reads_for_admin(self):
        result = self.Tomb.with_user(self.admin_a).web_search_read(
            [('leadgen_id', 'in', ['LGRV1', 'LGRV2'])],
            {'leadgen_id': {}, 'removal_reason': {}, 'merged_into_ref': {},
             'expires_on': {}, 'company_id': {'fields': {'display_name': {}}}})
        rows = {r['leadgen_id']: r for r in result['records']}
        self.assertEqual(set(rows), {'LGRV1', 'LGRV2'})
        self.assertFalse(rows['LGRV2']['merged_into_ref'])
        self.assertTrue(rows['LGRV2']['expires_on'])
        with self.assertRaises(AccessError):
            self.Tomb.with_user(self.sales_mgr_a).web_search_read(
                [], {'leadgen_id': {}})
