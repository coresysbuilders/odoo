# Meta Lead Ads Integration for Odoo — © 2026 CoreSys Builders. All rights reserved.
# Licensed under the Odoo Proprietary License v1.0 (OPL-1).
# Unauthorized copying, redistribution, or resale of this software, in whole or in
# part, via any medium, is strictly prohibited and constitutes a license violation.
# OPL-1: https://www.odoo.com/documentation/18.0/legal/licenses.html#odoo-apps

"""Webhook leads of a Page that is archived in Odoo.

The drain holds such a lead on a failed row that keeps its Page and company,
so the right admins see it and can retry it once the Page is active again.
A Page id with no meta.page row at all still reads as an unknown Page.
Reactivating the Page puts its held leads back in the queue by itself.
"""
from datetime import timedelta
from unittest import mock

from odoo import Command, fields
from odoo.exceptions import AccessError
from odoo.tests.common import TransactionCase, new_test_user, tagged
from odoo.tools import mute_logger

from odoo.addons.meta_lead_ads.models import const, meta_sync_log
from odoo.addons.meta_lead_ads.models.exceptions import MetaPermanentError
from odoo.addons.meta_lead_ads.models.meta_sync_log import (
    _PAGE_ARCHIVED_MESSAGE)

from .test_webhook_drain import WebhookDrainFixtureMixin

_DRAIN_LOGGER = 'odoo.addons.meta_lead_ads.models.meta_webhook_event'


@tagged('post_install', '-at_install')
class TestArchivedPageHold(WebhookDrainFixtureMixin, TransactionCase):
    """The drain holds the leads of an archived Page instead of losing it."""

    def setUp(self):
        super().setUp()
        # The fixture's name, 'Page', is a word of the hold message, so it
        # could not show that the message leaves the name out.
        self.page.name = 'Harbor Clinic'
        self.EventClass = type(self.Event)

    def _queue(self, leadgen_id, page_id='PG1'):
        return self.Event.create({
            'leadgen_id': leadgen_id, 'page_id': page_id,
            'form_id': 'F1', 'status': 'pending'})

    def _archive(self):
        self.page.active = False

    def _row(self, leadgen_id):
        return self.Log.search([('meta_leadgen_id', '=', leadgen_id)])

    def _vacuum_event(self, event):
        # Past the default 90 days a failed queue row is kept.
        self.env.flush_all()
        self.env.cr.execute(
            "UPDATE meta_webhook_event SET write_date = %s WHERE id = %s",
            (fields.Datetime.now() - timedelta(days=91), event.id))
        self.env.invalidate_all()
        self.Log.sudo()._cron_vacuum_logs()
        self.env.invalidate_all()

    def _assert_held(self, event, leadgen_id):
        self.assertEqual(event.status, 'failed')
        self.assertEqual(event.attempts, 0)
        self.assertEqual(event.error_message, _PAGE_ARCHIVED_MESSAGE)
        row = self._row(leadgen_id)
        self.assertEqual(len(row), 1)
        self.assertEqual(row.status, 'failed')
        self.assertEqual(row.trigger, 'webhook')
        self.assertEqual(row.page_id, self.page)
        self.assertTrue(self.page.lead_company_id)
        self.assertEqual(row.company_id, self.page.lead_company_id)
        self.assertEqual(row.error_message, _PAGE_ARCHIVED_MESSAGE)
        for message in (event.error_message, row.error_message):
            self.assertNotIn(self.page.page_id, message)
            self.assertNotIn(self.page.name, message)
        return row

    def test_archived_page_event_is_held(self):
        """No ingest; the event and its row fail with the Page kept."""
        self._archive()
        event = self._queue('LGA1')
        with mock.patch.object(self.IngestClass, 'ingest_leadgen') as m:
            self.Event._cron_drain()
        m.assert_not_called()
        self._assert_held(event, 'LGA1')

    def test_event_queued_before_archive_is_held_at_drain(self):
        """The Page is checked when the event is drained, not when queued."""
        event = self._queue('LGA2')
        self._archive()
        with mock.patch.object(self.IngestClass, 'ingest_leadgen') as m:
            self.Event._cron_drain()
        m.assert_not_called()
        self._assert_held(event, 'LGA2')

    def test_intake_sets_company_for_archived_page(self):
        """Intake still gives the event its company, and creates no lead."""
        self._archive()
        self.Event.sudo()._ingest_payload(self._payload('LGA3'))
        event = self.Event.search([('leadgen_id', '=', 'LGA3')])
        self.assertEqual(len(event), 1)
        self.assertEqual(event.status, 'pending')
        self.assertTrue(self.page.lead_company_id)
        self.assertEqual(event.page_company_id, self.page.lead_company_id)
        self.assertFalse(self.Lead.with_context(active_test=False).search_count(
            [('meta_leadgen_id', '=', 'LGA3')]))

    @mute_logger(_DRAIN_LOGGER)
    def test_unexpected_error_on_archived_page_keeps_the_page(self):
        """The drain's error path finds the archived Page and says so."""
        self._archive()
        event = self._queue('LGA4')
        with mock.patch.object(self.EventClass, '_process_one',
                               side_effect=RuntimeError('boom')):
            self.Event._cron_drain()
        self.assertEqual(event.status, 'failed')
        self.assertEqual(event.error_message, _PAGE_ARCHIVED_MESSAGE)
        row = self._row('LGA4')
        self.assertEqual(len(row), 1)
        self.assertEqual(row.status, 'failed')
        self.assertEqual(row.page_id, self.page)
        self.assertEqual(row.company_id, self.page.lead_company_id)
        self.assertEqual(row.error_message, _PAGE_ARCHIVED_MESSAGE)

    @mute_logger(_DRAIN_LOGGER)
    def test_unexpected_error_on_active_page_is_unchanged(self):
        """With the Page active the error path reads as before."""
        event = self._queue('LGA6')
        with mock.patch.object(self.EventClass, '_process_one',
                               side_effect=RuntimeError('boom')):
            self.Event._cron_drain()
        self.assertEqual(event.status, 'failed')
        self.assertEqual(event.error_message, 'Unexpected drain error')
        row = self._row('LGA6')
        self.assertEqual(row.status, 'failed')
        self.assertEqual(row.error_message, 'Unexpected drain error')
        self.assertEqual(row.page_id, self.page)

    def test_removed_lead_of_archived_page_settles_as_removed(self):
        """A lead deleted in Odoo is skipped as removed, not held."""
        self.env['meta.leadgen.tombstone'].sudo()._remember(['LGA5'])
        self._archive()
        event = self._queue('LGA5')
        with mock.patch.object(self.IngestClass, 'ingest_leadgen') as m:
            self.Event._cron_drain()
        m.assert_not_called()
        self.assertEqual(event.status, 'done')
        self.assertFalse(self.Log.search_count(
            [('meta_leadgen_id', '=', 'LGA5'), ('status', '=', 'failed')]))
        self.assertEqual(self.Log.search_count(
            [('meta_leadgen_id', '=', 'LGA5'),
             ('status', '=', 'skipped_removed')]), 1)

    def test_unknown_page_is_unchanged(self):
        """A Page id Odoo has never seen keeps the unknown Page failure."""
        event = self._queue('LGA8', page_id='PG_NONE')
        with mock.patch.object(self.IngestClass, 'ingest_leadgen') as m:
            self.Event._cron_drain()
        m.assert_not_called()
        self.assertEqual(event.status, 'failed')
        row = self._row('LGA8')
        self.assertEqual(len(row), 1)
        self.assertEqual(row.status, 'failed')
        self.assertEqual(row.error_message,
                         'Unknown page; cannot process event')
        self.assertFalse(row.page_id)
        self.assertFalse(row.company_id)

    def test_held_row_keeps_its_page_after_the_event_is_vacuumed(self):
        """Once the queue row is cleaned up, the held row still has its Page
        to retry from."""
        self._archive()
        event = self._queue('LGA7')
        with mock.patch.object(self.IngestClass, 'ingest_leadgen'):
            self.Event._cron_drain()
        row = self._assert_held(event, 'LGA7')
        self._vacuum_event(event)
        self.assertFalse(self.Event.search_count(
            [('leadgen_id', '=', 'LGA7')]))
        self.assertTrue(row.exists())
        self.assertEqual(row.status, 'failed')
        self.assertEqual(row.page_id, self.page)
        self.assertEqual(row._resolve_page(), self.page)


@tagged('post_install', '-at_install')
class TestArchivedPageReactivation(WebhookDrainFixtureMixin,
                                   TransactionCase):
    """Reactivating a Page puts the leads it held back in the queue."""

    def setUp(self):
        super().setUp()
        self.EventClass = type(self.Event)
        self.ICP = self.env['ir.config_parameter'].sudo()
        self.drain_cron = self.env.ref(
            'meta_lead_ads.cron_meta_webhook_drain')

    def _queue(self, leadgen_id, page_id='PG1'):
        return self.Event.create({
            'leadgen_id': leadgen_id, 'page_id': page_id,
            'form_id': 'F1', 'status': 'pending'})

    def _row(self, leadgen_id):
        return self.Log.search([('meta_leadgen_id', '=', leadgen_id)])

    def _hold(self, leadgen_id, page=None):
        """Queue a lead of an archived Page and let the drain hold it."""
        page = page or self.page
        page.active = False
        event = self._queue(leadgen_id, page_id=page.page_id)
        with mock.patch.object(self.IngestClass, 'ingest_leadgen') as m:
            self.Event._cron_drain()
        m.assert_not_called()
        self.assertEqual(event.status, 'failed')
        row = self._row(leadgen_id)
        self.assertEqual(len(row), 1)
        self.assertEqual(row.status, 'failed')
        self.assertEqual(row.page_id, page)
        return event, row

    def _sql(self, query, params):
        self.env.flush_all()
        self.env.cr.execute(query, params)
        self.env.invalidate_all()

    def _vacuum_event(self, event):
        self._sql("UPDATE meta_webhook_event SET write_date = %s "
                  "WHERE id = %s",
                  (fields.Datetime.now() - timedelta(days=91), event.id))
        self.Log.sudo()._cron_vacuum_logs()
        self.env.invalidate_all()

    def _triggers(self):
        # The cron ships active; a trigger is only kept for an active cron.
        self.assertTrue(self.drain_cron.active)
        return self.env['ir.cron.trigger'].sudo().search_count(
            [('cron_id', '=', self.drain_cron.id)])

    def _events(self, leadgen_id):
        return self.Event.search([('leadgen_id', '=', leadgen_id)])

    def test_unarchive_requeues_the_held_event(self):
        """The held event goes back to pending; the click fetches
        nothing."""
        event, _row = self._hold('LGB2')
        before = self._triggers()
        with mock.patch.object(self.IngestClass, 'ingest_leadgen') as m:
            self.page.action_unarchive()
        m.assert_not_called()
        event.invalidate_recordset()
        self.assertEqual(event.status, 'pending')
        self.assertEqual(event.attempts, 0)
        self.assertFalse(event.error_message)
        self.assertTrue(event.requeued)
        self.assertEqual(event.page_company_id, self.page.lead_company_id)
        self.assertGreater(self._triggers(), before)

    def test_unarchive_recreates_a_vacuumed_event(self):
        """A held lead whose queue row was cleaned up gets a new one."""
        event, _row = self._hold('LGB3')
        self._vacuum_event(event)
        self.assertFalse(self._events('LGB3'))
        self.page.action_unarchive()
        event = self._events('LGB3')
        self.assertEqual(len(event), 1)
        self.assertEqual(event.status, 'pending')
        self.assertEqual(event.page_id, 'PG1')
        self.assertEqual(event.page_company_id, self.page.lead_company_id)
        self.assertTrue(event.requeued)

    def test_held_lead_comes_back_after_vacuum_and_reactivation(self):
        """The whole story: held with its Page and company, not failing the
        webhook, kept past the queue cleanup, refused on Retry while
        archived, and back as a lead once the Page is active."""
        self.page.active = False
        event = self._queue('LGB1')
        with mock.patch.object(self.IngestClass, 'ingest_leadgen') as m:
            self.Event._cron_drain()
        m.assert_not_called()
        row = self._row('LGB1')
        self.assertEqual(row.status, 'failed')
        self.assertEqual(row.page_id, self.page)
        self.assertTrue(self.page.lead_company_id)
        self.assertEqual(row.company_id, self.page.lead_company_id)
        health = self.env['meta.account']._meta_cron_health()
        self.assertNotEqual(health['webhook']['status'], 'failing')
        self._vacuum_event(event)
        self.assertFalse(self._events('LGB1'))
        with mock.patch.object(self.IngestClass, 'ingest_leadgen') as m:
            row.action_retry()
        self.assertEqual(m.call_count, 0)
        row.invalidate_recordset()
        self.assertEqual(row.status, 'failed')
        self.page.action_unarchive()
        with mock.patch.object(self.IngestClass, 'ingest_leadgen',
                               return_value=self._fake_lead()) as m:
            self.Event._cron_drain()
        self.assertEqual(m.call_count, 1)
        event = self._events('LGB1')
        self.assertEqual(event.status, 'done')
        self.assertTrue(event.lead_id)
        self.assertFalse(self.Log.search_count(
            [('meta_leadgen_id', '=', 'LGB1'), ('status', '=', 'failed')]))

    def test_requeued_event_that_fails_again_keeps_its_failure_time(self):
        """A lead put back in line is not a new failure."""
        _event, row = self._hold('LGB4')
        when = (fields.Datetime.now() - timedelta(days=2)).replace(
            microsecond=0)
        self._sql("UPDATE meta_sync_log SET failed_at = %s WHERE id = %s",
                  (when, row.id))
        self.page.action_unarchive()
        with mock.patch.object(self.IngestClass, 'ingest_leadgen',
                               side_effect=MetaPermanentError('gone')) as m:
            self.Event._cron_drain()
        self.assertEqual(m.call_count, 1)
        row.invalidate_recordset()
        self.assertEqual(row.status, 'failed')
        self.assertEqual(row.failed_at, when)
        self.assertEqual(row.trigger, 'webhook')
        self.assertEqual(row.error_message, 'gone')

    def test_requeued_success_does_not_stamp_webhook(self):
        """Meta did not send the lead again, so it proves nothing about the
        webhook."""
        self._hold('LGB5')
        self.ICP.set_param(const.WEBHOOK_OK_PARAM, False)
        self.ICP.set_param(const.DRAIN_RUN_PARAM, False)
        self.page.action_unarchive()
        with mock.patch.object(self.IngestClass, 'ingest_leadgen',
                               return_value=self._fake_lead()) as m:
            self.Event._cron_drain()
        self.assertEqual(m.call_count, 1)
        self.assertEqual(self._events('LGB5').status, 'done')
        self.assertFalse(self.ICP.get_param(const.WEBHOOK_OK_PARAM))
        self.assertTrue(self.ICP.get_param(const.DRAIN_RUN_PARAM))

    def test_erased_held_row_is_not_requeued(self):
        """Erased data is never fetched from Meta again."""
        event, row = self._hold('LGB6')
        self._sql("UPDATE meta_sync_log SET payload_cleared = 'erased' "
                  "WHERE id = %s", (row.id,))
        before = self.Event.search_count([])
        self.page.action_unarchive()
        event.invalidate_recordset()
        self.assertEqual(event.status, 'failed')
        self.assertEqual(self.Event.search_count([]), before)

    def test_busy_lead_is_skipped(self):
        """A lead another transaction holds is left as it is."""
        event, _row = self._hold('LGB7')
        with mock.patch.object(self.EventClass, '_lock_leadgen_event',
                               return_value=None):
            self.page.action_unarchive()
        self.assertTrue(self.page.active)
        event.invalidate_recordset()
        self.assertEqual(event.status, 'failed')
        self.assertEqual(event.error_message, _PAGE_ARCHIVED_MESSAGE)

    def test_only_the_reactivated_page_moves(self):
        """Held leads of another archived Page stay held."""
        other = self.env['meta.page'].create({
            'name': 'Second Clinic', 'page_id': 'PG2',
            'access_token': 'tok_test2', 'account_id': self.account.id,
        })
        mine, _row = self._hold('LGB8')
        theirs, _other_row = self._hold('LGB9', page=other)
        self.page.action_unarchive()
        mine.invalidate_recordset()
        theirs.invalidate_recordset()
        self.assertEqual(mine.status, 'pending')
        self.assertEqual(theirs.status, 'failed')
        self.assertEqual(theirs.error_message, _PAGE_ARCHIVED_MESSAGE)
        self.assertFalse(other.active)

    def test_page_without_holds_queues_nothing(self):
        self.page.active = False
        events = self.Event.search_count([])
        before = self._triggers()
        self.page.action_unarchive()
        self.assertTrue(self.page.active)
        self.assertEqual(self.Event.search_count([]), events)
        self.assertEqual(self._triggers(), before)

    def test_requeued_marker_is_system_set(self):
        """Only the re-queue itself sets the marker."""
        company = self.page.lead_company_id
        admin = new_test_user(
            self.env, 'rq_meta_admin',
            groups='base.group_user,meta_lead_ads.group_meta_admin',
            company_id=company.id, company_ids=[Command.set([company.id])])
        Event = self.Event.with_user(admin)
        with self.assertRaises(AccessError):
            Event.create({'leadgen_id': 'LGB10', 'page_id': 'PG1',
                          'requeued': True})
        event = self._queue('LGB11')
        with self.assertRaises(AccessError):
            event.with_user(admin).write({'requeued': True})

    # ---- a re-queued lead waits behind fresh ones -------------------------

    def _age(self, events, minutes, column='create_date'):
        self._sql('UPDATE meta_webhook_event SET "%s" = %%s WHERE id IN %%s'
                  % column,
                  (fields.Datetime.now() - timedelta(minutes=minutes),
                   tuple(events.ids)))

    def _drain_health(self):
        return self.env['meta.account']._meta_cron_health()

    def test_requeue_stamps_its_time(self):
        """Both a reset event and a re-made one record when they went back
        in line."""
        reset, _row = self._hold('LGB12')
        vacuumed, _row = self._hold('LGB13')
        self._vacuum_event(vacuumed)
        self.page.action_unarchive()
        reset.invalidate_recordset()
        remade = self._events('LGB13')
        for event in (reset, remade):
            self.assertEqual(event.status, 'pending')
            self.assertTrue(event.requeued_at)
            self.assertLess(fields.Datetime.now() - event.requeued_at,
                            timedelta(minutes=1))

    def test_fresh_delivery_is_drained_before_a_requeued_lead(self):
        """A lead put back in line waits behind the leads Meta has just
        sent, however long ago it first arrived."""
        held, _row = self._hold('LGB14')
        self.page.action_unarchive()
        self._age(held, 60 * 24)
        fresh = self._queue('LGB15')
        with mock.patch.object(self.IngestClass, 'ingest_leadgen',
                               return_value=self._fake_lead()) as m:
            self.Event._cron_drain(limit=1)
        self.assertEqual(m.call_count, 1)
        self.env.invalidate_all()
        self.assertEqual(fresh.status, 'done')
        self.assertEqual(held.status, 'pending')

    def test_reactivation_does_not_make_a_backlog(self):
        """The held leads are old, but they have only just been put back."""
        held, _row = self._hold('LGB16')
        self._age(held, const.QUEUE_BACKLOG_MINUTES + 30)
        self.page.action_unarchive()
        held.invalidate_recordset()
        self.assertEqual(held.status, 'pending')
        health = self._drain_health()
        self.assertEqual(health['drain']['backlog_count'], 0)
        self.assertNotEqual(health['drain']['status'], 'backlog')
        self.assertNotEqual(health['webhook']['status'], 'backlog')

    def test_requeued_lead_ages_from_its_requeue(self):
        """A lead left waiting since it was put back is a backlog again."""
        held, _row = self._hold('LGB17')
        self.page.action_unarchive()
        self._age(held, const.QUEUE_BACKLOG_MINUTES + 5, column='requeued_at')
        held.invalidate_recordset()
        self.assertLess(fields.Datetime.now() - held.create_date,
                        timedelta(minutes=const.QUEUE_BACKLOG_MINUTES))
        health = self._drain_health()
        self.assertEqual(health['drain']['backlog_count'], 1)
        self.assertEqual(health['drain']['status'], 'backlog')

    def test_requeue_time_is_system_set(self):
        """Only the re-queue itself sets the time, or a user could push a
        stuck lead out of the backlog count."""
        company = self.page.lead_company_id
        admin = new_test_user(
            self.env, 'rq_time_admin',
            groups='base.group_user,meta_lead_ads.group_meta_admin',
            company_id=company.id, company_ids=[Command.set([company.id])])
        Event = self.Event.with_user(admin)
        with self.assertRaises(AccessError):
            Event.create({'leadgen_id': 'LGB18', 'page_id': 'PG1',
                          'requeued_at': fields.Datetime.now()})
        event = self._queue('LGB19')
        with self.assertRaises(AccessError):
            event.with_user(admin).write(
                {'requeued_at': fields.Datetime.now()})

    # ---- the sync row after reactivation ----------------------------------

    def _failed_rows(self, leadgen_id):
        return self.Log.search([('meta_leadgen_id', '=', leadgen_id),
                                ('status', '=', 'failed')])

    def _drain(self, **kwargs):
        with mock.patch.object(self.IngestClass, 'ingest_leadgen',
                               **kwargs) as m:
            self.Event._cron_drain()
        self.env.invalidate_all()
        return m

    def test_reactivation_rewrites_the_held_message(self):
        """The row no longer tells anyone to reactivate an active Page. It
        stays failed, with its failure time and trigger, until the drain
        fetches the lead."""
        self.page.name = 'Harbor Clinic'
        _event, row = self._hold('LGB20')
        before = (row.failed_at, row.trigger)
        self.page.action_unarchive()
        row.invalidate_recordset()
        requeued = meta_sync_log._PAGE_REQUEUED_MESSAGE
        self.assertEqual(row.status, 'failed')
        self.assertEqual(row.error_message, requeued)
        self.assertEqual((row.failed_at, row.trigger), before)
        self.assertNotIn(self.page.page_id, requeued)
        self.assertNotIn(self.page.name, requeued)

    def test_pending_wait_survives_reactivation(self):
        """A lead whose event still waits on Meta is already in line, so
        reactivation keeps Meta's wait."""
        self.page.active = False
        later = fields.Datetime.now() + timedelta(hours=1)
        event = self._queue('LGB21')
        event.write({'next_attempt_at': later})
        row = self.Log._record_failure('LGB21', 'webhook',
                                       _PAGE_ARCHIVED_MESSAGE, page=self.page)
        self.page.action_unarchive()
        self.env.invalidate_all()
        self.assertEqual(event.status, 'pending')
        self.assertEqual(event.next_attempt_at, later)
        self.assertFalse(event.requeued)
        self.assertFalse(event.requeued_at)
        self.assertEqual(row.status, 'failed')
        self.assertEqual(row.error_message,
                         meta_sync_log._PAGE_REQUEUED_MESSAGE)

    def test_lead_busy_at_reactivation_comes_back_on_the_next_drain(self):
        """A lead another transaction held during reactivation keeps its
        message, and the next drain run puts it back and fetches it."""
        busy, busy_row = self._hold('LGB22')
        free, _free_row = self._hold('LGB23')
        real_lock = self.EventClass._lock_leadgen_event

        def _lock(rec, leadgen_id):
            if leadgen_id == 'LGB22':
                return None
            return real_lock(rec, leadgen_id)
        with mock.patch.object(self.EventClass, '_lock_leadgen_event',
                               autospec=True, side_effect=_lock):
            self.page.action_unarchive()
        self.env.invalidate_all()
        self.assertEqual(busy.status, 'failed')
        self.assertEqual(busy_row.error_message, _PAGE_ARCHIVED_MESSAGE)
        self.assertEqual(free.status, 'pending')
        triggers = self._triggers()
        m = self._drain(side_effect=lambda *a, **kw: self._fake_lead())
        self.assertEqual(m.call_count, 2)
        self.assertEqual(busy.status, 'done')
        self.assertTrue(busy.requeued)
        self.assertTrue(busy.lead_id)
        self.assertFalse(self._failed_rows('LGB22'))
        self.assertEqual(free.status, 'done')
        # The drain itself takes the lead, so it asks for no extra run.
        self.assertEqual(self._triggers(), triggers)

    def test_drain_leaves_holds_of_an_archived_page(self):
        event, row = self._hold('LGB24')
        for _run in range(2):
            m = self._drain()
            m.assert_not_called()
        self.assertEqual(event.status, 'failed')
        self.assertFalse(event.requeued)
        self.assertEqual(row.status, 'failed')
        self.assertEqual(row.error_message, _PAGE_ARCHIVED_MESSAGE)

    def test_drain_does_not_requeue_a_real_failure_again(self):
        """A re-queued lead that fails for a real reason stays failed; later
        runs don't put it back."""
        event, row = self._hold('LGB25')
        self.page.action_unarchive()
        m = self._drain(side_effect=MetaPermanentError('gone'))
        self.assertEqual(m.call_count, 1)
        self.assertEqual(event.status, 'failed')
        self.assertEqual(row.error_message, 'gone')
        for _run in range(2):
            m = self._drain()
            m.assert_not_called()
        self.assertEqual(event.status, 'failed')
        self.assertEqual(row.status, 'failed')
        self.assertEqual(row.error_message, 'gone')

    def test_drain_leaves_an_erased_hold_alone(self):
        """Erased data is never fetched from Meta again, even when the row
        still says the Page is archived."""
        event, row = self._hold('LGB26')
        self._sql("UPDATE meta_sync_log SET payload_cleared = 'erased' "
                  "WHERE id = %s", (row.id,))
        self.page.action_unarchive()
        self.assertEqual(row.error_message, _PAGE_ARCHIVED_MESSAGE)
        m = self._drain()
        m.assert_not_called()
        self.assertEqual(event.status, 'failed')
        self.assertFalse(event.requeued)
        self.assertEqual(row.status, 'failed')
        self.assertEqual(row.error_message, _PAGE_ARCHIVED_MESSAGE)
