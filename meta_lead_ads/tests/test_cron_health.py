# Meta Lead Ads Integration for Odoo — © 2026 CoreSys Builders. All rights reserved.
# Licensed under the Odoo Proprietary License v1.0 (OPL-1).
# Unauthorized copying, redistribution, or resale of this software, in whole or in
# part, via any medium, is strictly prohibited and constitutes a license violation.
# OPL-1: https://www.odoo.com/documentation/19.0/legal/licenses.html#odoo-apps

"""Scheduler and webhook health.

If Odoo's cron worker isn't running, lead sync stops without any error. The
queue drain and the backfill each record their own last run, the drain also
records the last run that finished a webhook event, and the account form
reports the job at fault.
"""
import inspect
from datetime import timedelta
from unittest import mock

from dateutil.relativedelta import relativedelta

from odoo import fields
from odoo.tests.common import TransactionCase, tagged
from odoo.tools import mute_logger

from odoo.addons.meta_lead_ads.models import const, meta_account
from odoo.addons.meta_lead_ads.models.exceptions import (
    MetaPermanentError, MetaTransientError)
from odoo.addons.meta_lead_ads.models.meta_webhook_event import APP_PAUSE_PARAM

from .test_webhook_drain import WebhookDrainFixtureMixin

_DRAIN_LOGGER = 'odoo.addons.meta_lead_ads.models.meta_webhook_event'


class _HealthHelpers:

    def _set(self, key, dt):
        self.env['ir.config_parameter'].sudo().set_param(
            key, fields.Datetime.to_string(dt) if dt else False)

    def _get(self, key):
        return fields.Datetime.to_datetime(
            self.env['ir.config_parameter'].sudo().get_param(key))

    def _assert_recent(self, key):
        value = self._get(key)
        self.assertTrue(value, "%s was not stamped" % key)
        self.assertLess(fields.Datetime.now() - value, timedelta(minutes=1))

    def _backdate(self, records, delta):
        """Set create_date to now - delta. The ORM sets it, so use SQL."""
        records.flush_recordset()
        self.env.cr.execute(
            'UPDATE "%s" SET create_date = %%s WHERE id IN %%s'
            % records._table,
            (fields.Datetime.now() - delta, tuple(records.ids)))
        records.invalidate_recordset(['create_date'])

    def _backdate_failure(self, rows, delta):
        """Move a failed sync row's create_date and failure time back."""
        rows.flush_recordset()
        when = fields.Datetime.now() - delta
        self.env.cr.execute(
            'UPDATE meta_sync_log SET create_date = %s, failed_at = %s '
            'WHERE id IN %s', (when, when, tuple(rows.ids)))
        rows.invalidate_recordset(['create_date', 'failed_at'])

    def _pending_event(self, leadgen_id, age):
        event = self.env['meta.webhook.event'].sudo().create({
            'leadgen_id': leadgen_id, 'page_id': 'PG_HEALTH',
            'status': 'pending'})
        self._backdate(event, age)
        return event


@tagged('post_install', '-at_install')
class TestSchedulerHealth(_HealthHelpers, TransactionCase):

    def setUp(self):
        super().setUp()
        self.Account = self.env['meta.account']
        self.ICP = self.env['ir.config_parameter'].sudo()
        self.drain = self.env.ref('meta_lead_ads.cron_meta_webhook_drain')
        self.backfill = self.env.ref('meta_lead_ads.cron_meta_backfill')
        self.DRAIN = const.DRAIN_RUN_PARAM
        self.BACKFILL = const.BACKFILL_RUN_PARAM
        self.INST = 'meta_lead_ads.installed_at'
        self.drain.active = True
        self.backfill.active = True

    def _both_fresh(self):
        now = fields.Datetime.now()
        self._set(self.DRAIN, now)
        self._set(self.BACKFILL, now)

    def test_ok_when_recent_heartbeat(self):
        """Recent runs of both jobs report ok."""
        self._both_fresh()
        self.assertEqual(self.Account._scheduler_health()['status'], 'ok')

    def test_stalled_when_heartbeat_stale_and_nextcall_overdue(self):
        """An old drain run with an overdue nextcall reports stalled."""
        self._both_fresh()
        self._set(self.DRAIN, fields.Datetime.now() - timedelta(hours=2))
        self.drain.nextcall = fields.Datetime.now() - timedelta(hours=2)
        self.assertEqual(self.Account._scheduler_health()['status'], 'stalled')

    def test_pending_right_after_install(self):
        """Just installed with no drain run yet reports pending."""
        self._both_fresh()
        self._set(self.DRAIN, False)
        self._set(self.INST, fields.Datetime.now())
        self.drain.nextcall = fields.Datetime.now()
        self.assertEqual(self.Account._scheduler_health()['status'], 'pending')

    def test_stalled_when_never_ran_since_old_install(self):
        """No drain run hours after install reports stalled, even if nextcall
        isn't overdue."""
        self._both_fresh()
        self._set(self.DRAIN, False)
        self._set(self.INST, fields.Datetime.now() - timedelta(hours=2))
        self.drain.nextcall = fields.Datetime.now()
        self.assertEqual(self.Account._scheduler_health()['status'], 'stalled')

    def test_disabled_when_cron_inactive(self):
        """An archived drain cron reports disabled, not stalled."""
        self._both_fresh()
        self.drain.active = False
        self.assertEqual(self.Account._scheduler_health()['status'], 'disabled')

    def test_stamp_cron_run_sets_recent_param(self):
        self._set(self.DRAIN, False)
        self.Account._stamp_cron_run(self.DRAIN)
        self._assert_recent(self.DRAIN)

    def test_backfill_stamps_its_own_key(self):
        """The backfill records its own run and leaves the drain's alone."""
        self._set(self.DRAIN, False)
        self._set(self.BACKFILL, False)
        Form = self.env['meta.lead.form']
        with mock.patch.object(type(Form), '_run_sweep', return_value=None):
            Form._cron_backfill()
        self._assert_recent(self.BACKFILL)
        self.assertFalse(self._get(self.DRAIN))

    def test_reconcile_stamps_nothing(self):
        self._set(self.DRAIN, False)
        self._set(self.BACKFILL, False)
        Form = self.env['meta.lead.form']
        with mock.patch.object(type(Form), '_run_sweep', return_value=None):
            Form._cron_reconcile()
        self.assertFalse(self._get(self.DRAIN))
        self.assertFalse(self._get(self.BACKFILL))

    def test_retry_attribution_stamps_nothing(self):
        keys = (self.DRAIN, self.BACKFILL, const.WEBHOOK_OK_PARAM)
        for key in keys:
            self._set(key, False)
        params_before = self.ICP.search_count([])
        self.env['meta.lead.ingest']._cron_retry_attribution()
        for key in keys:
            self.assertFalse(self._get(key), key)
        # No other parameter appears either.
        self.assertEqual(self.ICP.search_count([]), params_before)

    def test_action_check_scheduler_returns_notification(self):
        self._both_fresh()
        account = self.Account.create({'name': 'A', 'account_id': 'ACC_SCHED'})
        action = account.action_check_scheduler()
        self.assertEqual(action['tag'], 'display_notification')
        self.assertIn('message', action['params'])
        self.assertEqual(action['params']['type'], 'success')

    def test_computed_status_on_account(self):
        """The account's cron_status field shows the health status."""
        self._both_fresh()
        account = self.Account.create({'name': 'B', 'account_id': 'ACC_SCHED2'})
        account.invalidate_recordset(['cron_status', 'cron_status_message'])
        self.assertEqual(account.cron_status, 'ok')
        self.assertTrue(account.cron_status_message)

    def test_meta_admin_without_settings_rights_reads_status(self):
        """A Meta Admin who is not a Settings admin can still open the account form."""
        user = self.env['res.users'].create({
            'name': 'Meta Admin NoSys', 'login': 'meta_admin_nosys',
            'company_id': self.env.company.id,
            'company_ids': [(6, 0, [self.env.company.id])],
            'group_ids': [(6, 0, [self.env.ref('base.group_user').id,
                                  self.env.ref('meta_lead_ads.group_meta_admin').id])]})
        self.assertFalse(user.has_group('base.group_system'))
        self._both_fresh()
        account = self.Account.create({'name': 'C', 'account_id': 'ACC_SCHED3'})
        fnames = {'name': {}, 'cron_status': {}, 'cron_status_message': {}}

        # Drop the cached cron values too, otherwise the user's read is
        # served from cache and never hits the ir.cron access check.
        self.env.invalidate_all()
        [row] = account.with_user(user).web_read(fnames)
        self.assertEqual(row['cron_status'], 'ok')
        self.assertTrue(row['cron_status_message'])

        self.env.invalidate_all()
        action = account.with_user(user).action_check_scheduler()
        self.assertEqual(action['tag'], 'display_notification')

        # The status still follows the real cron, not a blank record.
        self.drain.active = False
        self.env.invalidate_all()
        [row] = account.with_user(user).web_read(fnames)
        self.assertEqual(row['cron_status'], 'disabled')


@tagged('post_install', '-at_install')
class TestCronHealthSplit(_HealthHelpers, TransactionCase):
    """The drain and the backfill are judged apart, and the account form
    names the one at fault."""

    def setUp(self):
        super().setUp()
        self.Account = self.env['meta.account']
        self.drain = self.env.ref('meta_lead_ads.cron_meta_webhook_drain')
        self.backfill = self.env.ref('meta_lead_ads.cron_meta_backfill')
        self.drain.active = True
        self.backfill.active = True
        self.backfill.write({'interval_number': 15, 'interval_type': 'minutes'})
        now = fields.Datetime.now()
        self.drain.nextcall = now
        self.backfill.nextcall = now
        self._set(const.DRAIN_RUN_PARAM, now)
        self._set(const.BACKFILL_RUN_PARAM, now)
        self._set('meta_lead_ads.installed_at', now - timedelta(days=30))
        self._set(APP_PAUSE_PARAM, False)

    def _ago(self, **kw):
        return fields.Datetime.now() - timedelta(**kw)

    def _ahead(self, **kw):
        return fields.Datetime.now() + timedelta(**kw)

    def _health(self):
        return self.Account._meta_cron_health()

    def test_both_running(self):
        self._set(const.DRAIN_RUN_PARAM, self._ago(minutes=2))
        self._set(const.BACKFILL_RUN_PARAM, self._ago(minutes=5))
        health = self._health()
        self.assertEqual(health['drain']['status'], 'ok')
        self.assertEqual(health['backfill']['status'], 'ok')
        self.assertEqual(health['status'], 'ok')
        self.assertEqual(
            health['message'],
            "Scheduler healthy. The queue drain last ran 2 minutes ago and "
            "the backfill last ran 5 minutes ago.")
        self.assertEqual(self.Account._scheduler_health(),
                         {'status': 'ok', 'message': health['message']})

    def test_shape(self):
        health = self._health()
        self.assertEqual(set(health), {'now', 'drain', 'backfill', 'webhook',
                                       'status', 'message'})
        self.assertEqual(set(health['drain']), {
            'status', 'last_run', 'backlog_count', 'backlog_minutes'})
        self.assertEqual(set(health['backfill']), {'status', 'last_run'})
        self.assertEqual(set(health['webhook']), {
            'status', 'last_ok', 'failed_count', 'backlog_count',
            'backlog_minutes'})
        self.assertNotIn('webhook', self.Account._meta_cron_health(
            webhook=False))

    def test_backfill_stalled_drain_running(self):
        self._set(const.BACKFILL_RUN_PARAM, self._ago(hours=2))
        self.backfill.nextcall = self._ago(hours=2)
        health = self._health()
        self.assertEqual(health['drain']['status'], 'ok')
        self.assertEqual(health['backfill']['status'], 'stalled')
        self.assertEqual(health['status'], 'stalled')
        self.assertTrue(health['message'].startswith(
            "The backfill last ran 2 hours ago"), health['message'])
        self.assertIn("the queue drain is running", health['message'])

    def test_backfill_never_ran_drain_running(self):
        self._set(const.BACKFILL_RUN_PARAM, False)
        self._set('meta_lead_ads.installed_at', self._ago(hours=2))
        self.backfill.nextcall = self._ago(hours=2)
        health = self._health()
        self.assertEqual(health['backfill']['status'], 'stalled')
        self.assertFalse(health['backfill']['last_run'])
        self.assertTrue(health['message'].startswith(
            "The backfill hasn't run yet; the queue drain is running"),
            health['message'])

    def test_drain_stalled_backfill_running(self):
        self._set(const.DRAIN_RUN_PARAM, self._ago(hours=1))
        self.drain.nextcall = self._ago(hours=1)
        health = self._health()
        self.assertEqual(health['drain']['status'], 'stalled')
        self.assertEqual(health['backfill']['status'], 'ok')
        self.assertTrue(health['message'].startswith(
            "The queue drain last ran 60 minutes ago"), health['message'])
        self.assertIn("the backfill is running", health['message'])

    def test_backfill_window_follows_its_interval(self):
        self.backfill.write({'interval_number': 60})
        self._set(const.BACKFILL_RUN_PARAM, self._ago(hours=2))
        self.backfill.nextcall = self._ago(hours=1)
        self.assertEqual(self._health()['backfill']['status'], 'ok')
        self._set(const.BACKFILL_RUN_PARAM, self._ago(hours=4))
        self.backfill.nextcall = self._ago(hours=4)
        self.assertEqual(self._health()['backfill']['status'], 'stalled')

    def test_backfill_window_for_every_cron_unit(self):
        """Every interval unit a scheduled action offers gives a window of
        three of the backfill's own intervals."""
        self.assertEqual(const.BACKFILL_STALE_FACTOR, 3)
        expected = {
            'minutes': relativedelta(minutes=6),
            'hours': relativedelta(hours=6),
            'days': relativedelta(days=6),
            'weeks': relativedelta(days=42),
            'months': relativedelta(months=6),
        }
        units = self.env['ir.cron']._fields['interval_type'].get_values(
            self.env)
        self.assertEqual(set(units), set(expected))
        start = fields.Datetime.to_datetime('2026-01-31 12:00:00')
        for unit, window in expected.items():
            with self.subTest(unit=unit):
                self.backfill.write({'interval_number': 2,
                                     'interval_type': unit})
                got = self.Account._backfill_stale_window(self.backfill)
                self.assertEqual(start + got, start + window)

    def test_backfill_window_uses_no_private_core_table(self):
        """The window comes from the module's own table of units, not the
        private one in ir_cron, which can change without notice."""
        source = inspect.getsource(meta_account)
        self.assertNotIn('_intervalTypes', source)
        self.assertNotIn('base.models.ir_cron', source)

    def test_backfill_disabled(self):
        self.backfill.active = False
        health = self._health()
        self.assertEqual(health['backfill']['status'], 'disabled')
        self.assertEqual(health['drain']['status'], 'ok')
        self.assertEqual(health['status'], 'disabled')
        self.assertIn("The Meta backfill scheduled action is inactive",
                      health['message'])

    def test_drain_disabled(self):
        self.drain.active = False
        health = self._health()
        self.assertEqual(health['status'], 'disabled')
        self.assertIn("webhook-drain scheduled action is inactive",
                      health['message'])

    def test_backlog(self):
        self._pending_event('LG_BACKLOG', timedelta(minutes=20))
        health = self._health()
        self.assertEqual(health['drain']['status'], 'backlog')
        self.assertEqual(health['drain']['backlog_count'], 1)
        self.assertEqual(health['drain']['backlog_minutes'], 10)
        self.assertEqual(health['status'], 'backlog')
        self.assertIn("1 webhook events have waited more than 10 minutes",
                      health['message'])
        account = self.Account.create({'name': 'BL', 'account_id': 'ACC_BL'})
        account.invalidate_recordset(['cron_status', 'cron_status_message'])
        self.assertEqual(account.cron_status, 'backlog')

    def test_recent_pending_event_is_not_backlog(self):
        self._pending_event('LG_RECENT', timedelta(minutes=5))
        health = self._health()
        self.assertEqual(health['drain']['status'], 'ok')
        self.assertEqual(health['drain']['backlog_count'], 0)

    def test_both_stalled_shows_fix_hint(self):
        for cron, key in ((self.drain, const.DRAIN_RUN_PARAM),
                          (self.backfill, const.BACKFILL_RUN_PARAM)):
            self._set(key, self._ago(hours=2))
            cron.nextcall = self._ago(hours=2)
        health = self._health()
        self.assertEqual(health['status'], 'stalled')
        self.assertEqual(health['message'],
                         self.Account._scheduler_fix_hint())

    def test_fresh_install_waits(self):
        self._set(const.DRAIN_RUN_PARAM, False)
        self._set(const.BACKFILL_RUN_PARAM, False)
        self._set('meta_lead_ads.installed_at', self._ago(minutes=5))
        health = self._health()
        self.assertEqual(health['drain']['status'], 'pending')
        self.assertEqual(health['backfill']['status'], 'pending')
        self.assertEqual(health['status'], 'pending')
        self.assertIn("Waiting for the first scheduled run", health['message'])

    def test_check_scheduler_names_the_stale_job(self):
        self._set(const.BACKFILL_RUN_PARAM, self._ago(hours=2))
        self.backfill.nextcall = self._ago(hours=2)
        account = self.Account.create({'name': 'CS', 'account_id': 'ACC_CS'})
        action = account.action_check_scheduler()
        self.assertEqual(action['params']['type'], 'warning')
        self.assertTrue(action['params']['message'].startswith(
            "The backfill last ran"))
        account.invalidate_recordset(['cron_status', 'cron_status_message'])
        self.assertEqual(account.cron_status, 'stalled')
        self.assertEqual(account.cron_status_message,
                         action['params']['message'])

    def test_messages_hold_no_identifiers(self):
        """Banner text carries counts and ages only."""
        account = self.Account.create({
            'name': 'Secret Name', 'account_id': 'ACC_SECRET_ID',
            'app_id': 'APP_SECRET_ID'})
        self._pending_event('LG_SECRET_ID', timedelta(minutes=20))
        message = self._health()['message']
        for text in ('Secret Name', 'ACC_SECRET_ID', 'APP_SECRET_ID',
                     'LG_SECRET_ID', 'PG_HEALTH'):
            self.assertNotIn(text, message)
        self.assertTrue(account)

    # ---- what the message says about the other job ------------------------

    def test_fresh_install_without_scheduler_shows_fix_hint(self):
        """Twenty minutes after install with neither job ever run, the
        scheduler is down, so the fix hint shows without waiting for the
        backfill to stall too."""
        self._set(const.DRAIN_RUN_PARAM, False)
        self._set(const.BACKFILL_RUN_PARAM, False)
        self._set('meta_lead_ads.installed_at', self._ago(minutes=20))
        self.drain.nextcall = self._ago(minutes=20)
        self.backfill.nextcall = self._ago(minutes=5)
        health = self._health()
        self.assertEqual(health['drain']['status'], 'stalled')
        self.assertEqual(health['backfill']['status'], 'pending')
        self.assertEqual(health['message'],
                         self.Account._scheduler_fix_hint())
        self.assertNotIn("is running", health['message'])
        account = self.Account.create({'name': 'FI', 'account_id': 'ACC_FI'})
        action = account.action_check_scheduler()
        self.assertEqual(action['params']['message'], health['message'])

    def test_drain_stalled_backfill_pending_names_both(self):
        self._set(const.DRAIN_RUN_PARAM, self._ago(minutes=30))
        self.drain.nextcall = self._ago(minutes=30)
        self._set(const.BACKFILL_RUN_PARAM, False)
        self._set('meta_lead_ads.installed_at', self._ago(minutes=40))
        self.backfill.nextcall = self._ago(minutes=5)
        health = self._health()
        self.assertEqual(health['drain']['status'], 'stalled')
        self.assertEqual(health['backfill']['status'], 'pending')
        self.assertTrue(health['message'].startswith(
            "The queue drain last ran 30 minutes ago; the backfill hasn't "
            "run yet."), health['message'])
        self.assertNotIn("is running", health['message'])

    def test_backfill_stalled_drain_pending_names_both(self):
        self._set(const.DRAIN_RUN_PARAM, False)
        self._set('meta_lead_ads.installed_at', self._ago(minutes=10))
        self._set(const.BACKFILL_RUN_PARAM, self._ago(hours=2))
        self.backfill.nextcall = self._ago(hours=2)
        health = self._health()
        self.assertEqual(health['drain']['status'], 'pending')
        self.assertEqual(health['backfill']['status'], 'stalled')
        self.assertTrue(health['message'].startswith(
            "The backfill last ran 2 hours ago; the queue drain hasn't run "
            "yet."), health['message'])

    def test_stalled_drain_named_when_backfill_disabled(self):
        """A disabled backfill doesn't hide a stalled drain."""
        self._set(const.DRAIN_RUN_PARAM, self._ago(hours=1))
        self.drain.nextcall = self._ago(hours=1)
        self.backfill.active = False
        health = self._health()
        self.assertEqual(health['drain']['status'], 'stalled')
        self.assertEqual(health['status'], 'disabled')
        self.assertTrue(health['message'].startswith(
            "The queue drain last ran 60 minutes ago; the backfill is "
            "disabled."), health['message'])

    def test_disabled_drain_mentions_stalled_backfill(self):
        self.drain.active = False
        self._set(const.BACKFILL_RUN_PARAM, self._ago(hours=2))
        self.backfill.nextcall = self._ago(hours=2)
        health = self._health()
        self.assertEqual(health['backfill']['status'], 'stalled')
        self.assertIn("The Meta webhook-drain scheduled action is inactive",
                      health['message'])
        self.assertTrue(health['message'].endswith(
            "The backfill is not running either."), health['message'])

    def test_backlog_clause_after_stalled_backfill(self):
        self._set(const.BACKFILL_RUN_PARAM, self._ago(hours=2))
        self.backfill.nextcall = self._ago(hours=2)
        self._pending_event('LG_BL_CLAUSE', timedelta(minutes=20))
        health = self._health()
        self.assertEqual(health['drain']['status'], 'backlog')
        self.assertTrue(health['message'].startswith(
            "The backfill last ran 2 hours ago; the queue drain is running, "
            "but 1 webhook events have waited more than 10 minutes."),
            health['message'])

    # ---- events the drain holds back on purpose ---------------------------

    def test_rate_limited_event_is_not_backlog(self):
        event = self._pending_event('LG_HELD_RETRY', timedelta(minutes=30))
        event.write({'next_attempt_at': self._ahead(minutes=20)})
        health = self._health()
        self.assertEqual(health['drain']['status'], 'ok')
        self.assertEqual(health['drain']['backlog_count'], 0)

    def test_paused_page_events_are_not_backlog(self):
        Page = self.env['meta.page'].sudo().with_context(active_test=False)
        page = Page.search([('page_id', '=', 'PG_HEALTH')], limit=1)
        if not page:
            account = self.Account.create({'name': 'HP',
                                           'account_id': 'ACC_HP'})
            page = Page.create({'name': 'Health Page',
                                'page_id': 'PG_HEALTH',
                                'account_id': account.id})
        page.lead_fetch_paused_until = self._ahead(minutes=20)
        self._pending_event('LG_HELD_PAGE', timedelta(minutes=30))
        health = self._health()
        self.assertEqual(health['drain']['status'], 'ok')
        self.assertEqual(health['drain']['backlog_count'], 0)

    def test_app_pause_hides_backlog(self):
        self._set(APP_PAUSE_PARAM, self._ahead(minutes=20))
        self._pending_event('LG_HELD_APP', timedelta(minutes=30))
        health = self._health()
        self.assertEqual(health['drain']['status'], 'ok')
        self.assertEqual(health['drain']['backlog_count'], 0)

    def test_due_event_is_still_backlog(self):
        """A retry whose wait is over counts again."""
        event = self._pending_event('LG_HELD_DUE', timedelta(minutes=30))
        event.write({'next_attempt_at': self._ago(minutes=5)})
        health = self._health()
        self.assertEqual(health['drain']['status'], 'backlog')
        self.assertEqual(health['drain']['backlog_count'], 1)


@tagged('post_install', '-at_install')
class TestWebhookHealth(_HealthHelpers, WebhookDrainFixtureMixin,
                        TransactionCase):
    """The drain's stamps: its own run, and the last run that finished a
    webhook event."""

    def setUp(self):
        super().setUp()
        self.EventClass = type(self.Event)
        self.WH = const.WEBHOOK_OK_PARAM
        self._set(self.WH, False)
        self._set(const.DRAIN_RUN_PARAM, False)
        self._set(APP_PAUSE_PARAM, False)

    def _queue(self, leadgen_id):
        return self.Event.create({
            'leadgen_id': leadgen_id, 'page_id': 'PG1',
            'form_id': 'F1', 'status': 'pending'})

    def _spy_set_param(self):
        """Patch set_param to record the keys written, still writing them."""
        cls = type(self.env['ir.config_parameter'])
        original = cls.set_param
        keys = []

        def spy(rec, key, value):
            keys.append(key)
            return original(rec, key, value)
        return mock.patch.object(cls, 'set_param', spy), keys

    def test_drain_stamps_even_when_paused(self):
        """An app-wide pause still records the drain run, and nothing was
        processed, so the webhook stamp stays empty."""
        self._set(APP_PAUSE_PARAM, fields.Datetime.now() + timedelta(hours=1))
        self._queue('LG_WH_PAUSE')
        with mock.patch.object(self.IngestClass, 'ingest_leadgen') as m:
            self.Event._cron_drain()
        self.assertEqual(m.call_count, 0)
        self._assert_recent(const.DRAIN_RUN_PARAM)
        self.assertFalse(self._get(self.WH))

    def test_done_events_stamp_webhook_once(self):
        """Two events finished in one run write the webhook stamp once."""
        first = self._queue('LG_WH_1')
        second = self._queue('LG_WH_2')
        leads = [self._fake_lead(), self._fake_lead()]
        patch, keys = self._spy_set_param()
        with patch, mock.patch.object(self.IngestClass, 'ingest_leadgen',
                                      side_effect=leads):
            self.Event._cron_drain()
        self.assertEqual((first.status, second.status), ('done', 'done'))
        self.assertEqual(keys.count(self.WH), 1)
        self._assert_recent(self.WH)

    def test_failures_only_leave_webhook_stamp(self):
        """A run where every event fails leaves the last success alone."""
        before = fields.Datetime.now() - timedelta(days=3)
        self._set(self.WH, before)
        self._queue('LG_WH_F1')
        self._queue('LG_WH_F2')
        with mock.patch.object(self.IngestClass, 'ingest_leadgen',
                               side_effect=MetaPermanentError('bad')):
            self.Event._cron_drain()
        self.assertEqual(self._get(self.WH), before.replace(microsecond=0))
        self._assert_recent(const.DRAIN_RUN_PARAM)

    @mute_logger(_DRAIN_LOGGER)
    def test_removed_fallback_stamps_webhook(self):
        """An unexpected error on a lead removed in Odoo settles the event,
        which counts as finished."""
        self.env['meta.leadgen.tombstone'].sudo()._remember(['LG_WH_RM'])
        event = self._queue('LG_WH_RM')
        with mock.patch.object(self.EventClass, '_process_one',
                               side_effect=RuntimeError('boom')):
            self.Event._cron_drain()
        self.assertEqual(event.status, 'done')
        self._assert_recent(self.WH)

    @mute_logger(_DRAIN_LOGGER)
    def test_unexpected_error_does_not_stamp_webhook(self):
        self._queue('LG_WH_X')
        with mock.patch.object(self.EventClass, '_process_one',
                               side_effect=RuntimeError('boom')):
            self.Event._cron_drain()
        self.assertFalse(self._get(self.WH))

    # ---- webhook status ---------------------------------------------------

    def _webhook(self):
        return self.env['meta.account']._meta_cron_health()['webhook']

    def _row(self, leadgen_id, status, trigger='webhook'):
        return self.Log._record(leadgen_id, trigger, status,
                                error='x' if status == 'failed' else None)

    def test_running_after_recent_success(self):
        self._set(self.WH, fields.Datetime.now() - timedelta(hours=1))
        # A failure from another trigger is not webhook evidence.
        self._row('LG_WS_CRON', 'failed', trigger='cron')
        webhook = self._webhook()
        self.assertEqual(webhook['status'], 'running')
        self.assertEqual(webhook['failed_count'], 0)
        self.assertTrue(webhook['last_ok'])

    def test_quiet_after_old_success(self):
        self._set(self.WH, fields.Datetime.now() - timedelta(days=3))
        self.assertEqual(self._webhook()['status'], 'quiet')

    def test_failing_when_failure_after_success(self):
        self._set(self.WH, fields.Datetime.now() - timedelta(hours=1))
        self._row('LG_WS_F', 'failed')
        webhook = self._webhook()
        self.assertEqual(webhook['status'], 'failing')
        self.assertEqual(webhook['failed_count'], 1)

    def test_failure_before_success_is_not_failing(self):
        self._set(self.WH, fields.Datetime.now() - timedelta(hours=1))
        row = self._row('LG_WS_OLD', 'failed')
        self._backdate_failure(row, timedelta(hours=2))
        self.assertEqual(self._webhook()['status'], 'running')

    def test_failing_with_no_success_ever(self):
        self._row('LG_WS_N1', 'failed')
        self._row('LG_WS_N2', 'failed')
        webhook = self._webhook()
        self.assertEqual(webhook['status'], 'failing')
        self.assertEqual(webhook['failed_count'], 2)
        self.assertFalse(webhook['last_ok'])

    def test_no_activity(self):
        webhook = self._webhook()
        self.assertEqual(webhook['status'], 'no_activity')
        self.assertFalse(webhook['last_ok'])

    def test_old_failure_without_success_is_not_failing(self):
        """With no success recorded, a month-old failure doesn't keep the
        pill red."""
        row = self._row('LG_WR5_OLD', 'failed')
        self._backdate_failure(row, timedelta(days=30))
        webhook = self._webhook()
        self.assertEqual(webhook['status'], 'no_activity')
        self.assertEqual(webhook['failed_count'], 0)

    def test_recent_failure_without_success_is_failing(self):
        """With no success recorded, only failures of the last week
        count."""
        recent = self._row('LG_WR5_RECENT', 'failed')
        self._backdate_failure(recent, timedelta(days=2))
        old = self._row('LG_WR5_STALE', 'failed')
        self._backdate_failure(old, timedelta(days=30))
        webhook = self._webhook()
        self.assertEqual(webhook['status'], 'failing')
        self.assertEqual(webhook['failed_count'], 1)

    def test_lookback_does_not_apply_after_a_success(self):
        """A failure newer than the last success counts however old it
        is."""
        self._set(self.WH, fields.Datetime.now() - timedelta(days=20))
        row = self._row('LG_WR5_AFTER', 'failed')
        self._backdate_failure(row, timedelta(days=10))
        webhook = self._webhook()
        self.assertEqual(webhook['status'], 'failing')
        self.assertEqual(webhook['failed_count'], 1)

    def test_backlog(self):
        self._set(self.WH, fields.Datetime.now() - timedelta(minutes=5))
        self._pending_event('LG_WS_BL', timedelta(minutes=20))
        webhook = self._webhook()
        self.assertEqual(webhook['status'], 'backlog')
        self.assertEqual(webhook['backlog_count'], 1)
        self.assertEqual(webhook['backlog_minutes'], 10)

    def test_stamp_survives_log_cleanup(self):
        """The success stamp is not a log row, so the cleanup can't erase
        it."""
        self._set(self.WH, fields.Datetime.now() - timedelta(hours=1))
        old = self._row('LG_WS_V1', 'success') | self._row(
            'LG_WS_V2', 'skipped_idempotent')
        self._backdate(old, timedelta(days=60))
        before = self._webhook()
        self.Log._cron_vacuum_logs()
        self.assertFalse(old.exists())
        after = self._webhook()
        self.assertEqual(before['status'], 'running')
        self.assertEqual(after['status'], before['status'])
        self.assertEqual(after['last_ok'], before['last_ok'])

    # ---- failures in a run that also finishes an event ---------------------

    def _queue_in_order(self, *leadgen_ids):
        """Queue events with distinct create dates, so the drain takes them
        in the given order."""
        events = self.Event.browse()
        for n, leadgen_id in enumerate(leadgen_ids):
            event = self._queue(leadgen_id)
            self._backdate(event, timedelta(minutes=len(leadgen_ids) - n))
            events |= event
        return events

    def _failed_rows(self, leadgen_id):
        return self.Log.search([('meta_leadgen_id', '=', leadgen_id),
                                ('status', '=', 'failed')])

    def test_mixed_run_success_then_failure_reads_failing(self):
        """A failure after a success in the same run still counts."""
        first, second = self._queue_in_order('LG_WH_MIX_A1', 'LG_WH_MIX_A2')
        with mock.patch.object(
                self.IngestClass, 'ingest_leadgen',
                side_effect=[self._fake_lead(), MetaPermanentError('bad')]):
            self.Event._cron_drain()
        self.assertEqual((first.status, second.status), ('done', 'failed'))
        webhook = self._webhook()
        self.assertEqual(webhook['status'], 'failing')
        self.assertEqual(webhook['failed_count'], 1)

    def test_mixed_run_failure_then_success_reads_failing(self):
        """A failure before a success in the same run still counts."""
        first, second = self._queue_in_order('LG_WH_MIX_B1', 'LG_WH_MIX_B2')
        with mock.patch.object(
                self.IngestClass, 'ingest_leadgen',
                side_effect=[MetaPermanentError('bad'), self._fake_lead()]):
            self.Event._cron_drain()
        self.assertEqual((first.status, second.status), ('failed', 'done'))
        webhook = self._webhook()
        self.assertEqual(webhook['status'], 'failing')
        self.assertEqual(webhook['failed_count'], 1)

    def test_refailure_of_backfill_row_reads_failing(self):
        """A webhook failure of a lead the backfill already failed counts as
        a new webhook failure."""
        row = self.Log._record_failure('LG_WH_RE_CRON', 'cron', 'old error',
                                       page=self.page)
        self._backdate_failure(row, timedelta(days=2))
        self._set(self.WH, fields.Datetime.now() - timedelta(hours=1))
        self.assertEqual(self._webhook()['status'], 'running')
        self._queue('LG_WH_RE_CRON')
        with mock.patch.object(self.IngestClass, 'ingest_leadgen',
                               side_effect=MetaPermanentError('bad')):
            self.Event._cron_drain()
        rows = self._failed_rows('LG_WH_RE_CRON')
        self.assertEqual(rows, row)
        self.assertEqual(row.trigger, 'webhook')
        webhook = self._webhook()
        self.assertEqual(webhook['status'], 'failing')
        self.assertEqual(webhook['failed_count'], 1)

    def test_refailure_of_webhook_row_after_success_reads_failing(self):
        """A webhook lead that fails again after a later success counts
        again."""
        row = self.Log._record_failure('LG_WH_RE_WH', 'webhook', 'old error',
                                       page=self.page)
        self._backdate_failure(row, timedelta(days=2))
        self._set(self.WH, fields.Datetime.now() - timedelta(hours=1))
        self.assertEqual(self._webhook()['status'], 'running')
        self._queue('LG_WH_RE_WH')
        with mock.patch.object(self.IngestClass, 'ingest_leadgen',
                               side_effect=MetaPermanentError('bad')):
            self.Event._cron_drain()
        self.assertEqual(self._failed_rows('LG_WH_RE_WH'), row)
        webhook = self._webhook()
        self.assertEqual(webhook['status'], 'failing')
        self.assertEqual(webhook['failed_count'], 1)

    def test_backfill_refailure_keeps_webhook_failure(self):
        """A backfill failure of a webhook lead keeps it in the webhook
        count."""
        self._set(self.WH, fields.Datetime.now() - timedelta(hours=1))
        row = self.Log._record_failure('LG_WH_STICKY', 'webhook',
                                       'webhook error', page=self.page)
        webhook = self._webhook()
        self.assertEqual(webhook['status'], 'failing')
        self.assertEqual(webhook['failed_count'], 1)
        # The same call the backfill makes for a lead it can't ingest.
        self.Log._record_failure('LG_WH_STICKY', 'cron', 'backfill error',
                                 page=self.page)
        self.assertEqual(self._failed_rows('LG_WH_STICKY'), row)
        self.assertEqual(row.trigger, 'webhook')
        webhook = self._webhook()
        self.assertEqual(webhook['status'], 'failing')
        self.assertEqual(webhook['failed_count'], 1)

    def test_backfill_refailure_after_webhook_success_is_not_fresh_evidence(
            self):
        """A backfill failure of a webhook lead is not new webhook
        evidence."""
        row = self.Log._record_failure('LG_WH_STICKY2', 'webhook',
                                       'webhook error', page=self.page)
        self._backdate_failure(row, timedelta(hours=2))
        self._set(self.WH, fields.Datetime.now() - timedelta(hours=1))
        self.assertEqual(self._webhook()['status'], 'running')
        self.Log._record_failure('LG_WH_STICKY2', 'cron', 'backfill error',
                                 page=self.page)
        self.assertEqual(row.trigger, 'webhook')
        webhook = self._webhook()
        self.assertEqual(webhook['status'], 'running')
        self.assertEqual(webhook['failed_count'], 0)

    def test_webhook_stamp_is_taken_at_run_start(self):
        """The success stamp holds the time the run started, not the time
        its loop ended."""
        cls = type(self.env['meta.account'])
        original = cls._stamp_cron_run
        calls = []

        def spy(rec, *args, **kwargs):
            calls.append(args)
            return original(rec, *args, **kwargs)

        lead = self._fake_lead()
        during = []

        def ingest(*args, **kwargs):
            during.append(fields.Datetime.now())
            return lead

        self._queue('LG_WH_START')
        with mock.patch.object(cls, '_stamp_cron_run', spy), \
                mock.patch.object(self.IngestClass, 'ingest_leadgen',
                                  side_effect=ingest):
            before = fields.Datetime.now()
            self.Event._cron_drain()
        [args] = [a for a in calls if a and a[0] == self.WH]
        self.assertEqual(len(args), 2)
        started = args[1]
        self.assertLessEqual(before, started)
        self.assertLessEqual(started, during[0])
        stored = self._get(self.WH)
        self.assertEqual(stored, started.replace(microsecond=0))
        self.assertLessEqual(before.replace(microsecond=0), stored)

    def test_expired_payload_is_not_a_new_failure(self):
        """The cleanup expiring a failed row's payload is not a failure."""
        row = self.Log._record('LG_WH_EXP', 'webhook', 'failed', error='x',
                               raw={'id': 'LG_WH_EXP'}, page=self.page)
        self._backdate_failure(row, timedelta(days=100))
        self._set(self.WH, fields.Datetime.now() - timedelta(hours=1))
        self.Log._cron_vacuum_logs()
        self.assertEqual(row.payload_cleared, 'expired')
        self.assertEqual(row.status, 'failed')
        webhook = self._webhook()
        self.assertEqual(webhook['status'], 'running')
        self.assertEqual(webhook['failed_count'], 0)

    def test_resolved_failure_is_not_counted(self):
        """A row that is no longer failed doesn't count."""
        self._set(self.WH, fields.Datetime.now() - timedelta(hours=1))
        row = self.Log._record_failure('LG_WH_RES', 'webhook', 'x',
                                       page=self.page)
        row.write({'status': 'success'})
        webhook = self._webhook()
        self.assertEqual(webhook['status'], 'running')
        self.assertEqual(webhook['failed_count'], 0)

    # ---- leads held by an archived Page ------------------------------------

    def _held_row(self):
        """A failed webhook row whose Page is then archived."""
        row = self.Log._record_failure('LG_WH_HOLD', 'webhook', 'held',
                                       page=self.page)
        self.page.sudo().active = False
        return row

    def test_archived_page_hold_is_not_webhook_failure(self):
        """The webhook worked; the lead waits because of a setting."""
        self._held_row()
        webhook = self._webhook()
        self.assertEqual(webhook['failed_count'], 0)
        self.assertNotEqual(webhook['status'], 'failing')

    def test_unknown_page_failure_still_counts(self):
        """A failed row with no Page at all is still a webhook failure."""
        row = self._row('LG_WH_NOPAGE', 'failed')
        self.assertFalse(row.page_id)
        webhook = self._webhook()
        self.assertEqual(webhook['status'], 'failing')
        self.assertEqual(webhook['failed_count'], 1)

    def test_hold_counts_again_once_its_page_is_active(self):
        self._held_row()
        self.page.sudo().active = True
        webhook = self._webhook()
        self.assertEqual(webhook['status'], 'failing')
        self.assertEqual(webhook['failed_count'], 1)

    def test_cron_refailure_of_cron_row_moves_its_time(self):
        """A backfill failure of a backfill-failed lead records its own
        time."""
        row = self.Log._record_failure('LG_WH_CRON_RE', 'cron', 'first',
                                       page=self.page)
        self._backdate_failure(row, timedelta(days=2))
        self.Log._record_failure('LG_WH_CRON_RE', 'cron', 'second',
                                 page=self.page)
        self.assertEqual(self._failed_rows('LG_WH_CRON_RE'), row)
        self.assertEqual(row.trigger, 'cron')
        self.assertLess(fields.Datetime.now() - row.failed_at,
                        timedelta(minutes=1))

    # ---- a Retry is not a new failure ------------------------------------

    def _failed_webhook_lead(self, leadgen_id, trigger='webhook',
                             age=timedelta(hours=2)):
        """A failed row from before the last success, and its failed
        event."""
        error = 'backfill error' if trigger == 'cron' else 'webhook error'
        row = self.Log._record_failure(leadgen_id, trigger, error,
                                       page=self.page)
        self._backdate_failure(row, age)
        event = self.Event.create({
            'leadgen_id': leadgen_id, 'page_id': 'PG1', 'form_id': 'F1',
            'status': 'failed', 'error_message': 'bad'})
        self._set(self.WH, fields.Datetime.now() - timedelta(hours=1))
        return row, event

    def _assert_running(self):
        webhook = self._webhook()
        self.assertEqual(webhook['status'], 'running')
        self.assertEqual(webhook['failed_count'], 0)

    def test_queue_retry_that_fails_again_keeps_failure_time(self):
        """A queue Retry that fails again is not a new webhook failure."""
        row, event = self._failed_webhook_lead('LG_WH_QR_PERM')
        self._assert_running()
        before = row.failed_at
        with mock.patch.object(self.IngestClass, 'ingest_leadgen',
                               side_effect=MetaPermanentError('still bad')):
            event.action_retry()
        self.env.invalidate_all()
        self.assertEqual(self._failed_rows('LG_WH_QR_PERM'), row)
        self.assertEqual(row.failed_at, before)
        self.assertEqual(row.trigger, 'webhook')
        self.assertEqual(row.retry_count, 1)
        self.assertIn('still bad', row.error_message)
        self._assert_running()

    def test_queue_and_log_retry_agree_on_the_pill(self):
        """Both Retry buttons leave the pill the same."""
        row_a, event_a = self._failed_webhook_lead('LG_WH_QR_A')
        row_b, _event_b = self._failed_webhook_lead('LG_WH_QR_B')
        self._assert_running()
        before_a, before_b = row_a.failed_at, row_b.failed_at
        with mock.patch.object(self.IngestClass, 'ingest_leadgen',
                               side_effect=MetaPermanentError('still bad')):
            event_a.action_retry()
            row_b.action_retry()
        self.env.invalidate_all()
        self.assertEqual((row_a.failed_at, row_b.failed_at),
                         (before_a, before_b))
        self.assertEqual((row_a.trigger, row_b.trigger),
                         ('webhook', 'webhook'))
        self.assertEqual((row_a.retry_count, row_b.retry_count), (1, 1))
        self._assert_running()

    def test_queue_retry_keeps_backfill_trigger(self):
        """A queue Retry of a backfill failure leaves it a backfill one."""
        row, event = self._failed_webhook_lead(
            'LG_WH_QR_CRON', trigger='cron', age=timedelta(days=2))
        before = row.failed_at
        with mock.patch.object(self.IngestClass, 'ingest_leadgen',
                               side_effect=MetaPermanentError('still bad')):
            event.action_retry()
        self.env.invalidate_all()
        self.assertEqual(self._failed_rows('LG_WH_QR_CRON'), row)
        self.assertEqual(row.trigger, 'cron')
        self.assertEqual(row.failed_at, before)

    def test_queue_retry_transient_keeps_failure_time(self):
        """A queue Retry that Meta answers with a timeout keeps the failure
        time."""
        row, event = self._failed_webhook_lead('LG_WH_QR_TRANS')
        before = row.failed_at
        with mock.patch.object(self.IngestClass, 'ingest_leadgen',
                               side_effect=MetaTransientError('timeout')):
            event.action_retry()
        self.env.invalidate_all()
        self.assertEqual(event.status, 'pending')
        self.assertEqual(row.status, 'failed')
        self.assertEqual(row.failed_at, before)
        self._assert_running()

    @mute_logger(_DRAIN_LOGGER)
    def test_queue_retry_unexpected_error_keeps_failure_time(self):
        """A queue Retry that raises unexpectedly keeps the failure time."""
        row, event = self._failed_webhook_lead('LG_WH_QR_BOOM')
        before = row.failed_at
        with mock.patch.object(type(self.env['meta.sync.log']), '_retry_one',
                               side_effect=RuntimeError('boom')):
            event.action_retry()
        self.env.invalidate_all()
        self.assertEqual(event.status, 'failed')
        self.assertEqual(event.error_message, 'Unexpected retry error')
        self.assertEqual(self._failed_rows('LG_WH_QR_BOOM'), row)
        self.assertEqual(row.failed_at, before)
        self.assertEqual(row.trigger, 'webhook')
        self.assertEqual(self._webhook()['status'], 'running')

    def _failed_event_without_row(self, leadgen_id, received):
        """A failed event received `received` ago, with no failed row, and
        a success stamp from an hour ago."""
        event = self.Event.create({
            'leadgen_id': leadgen_id, 'page_id': 'PG1', 'form_id': 'F1',
            'status': 'failed', 'error_message': 'bad'})
        self._backdate(event, received)
        self._set(self.WH, fields.Datetime.now() - timedelta(hours=1))
        self.assertFalse(self._failed_rows(leadgen_id))
        return event

    def test_queue_retry_without_row_dates_failure_now(self):
        """A failed row made by a queue Retry is dated now, not from when
        Meta sent the lead."""
        event = self._failed_event_without_row('LG_WH_QR_NEW',
                                               timedelta(days=3))
        with mock.patch.object(self.IngestClass, 'ingest_leadgen',
                               side_effect=MetaPermanentError('still bad')):
            before = fields.Datetime.now().replace(microsecond=0)
            event.action_retry()
        self.env.invalidate_all()
        row = self._failed_rows('LG_WH_QR_NEW')
        self.assertEqual(len(row), 1)
        self.assertEqual(row.trigger, 'webhook')
        self.assertGreater(row.failed_at, event.create_date)
        self.assertLessEqual(before, row.failed_at)

    def test_queue_retry_counts_event_that_failed_after_the_success(self):
        """An event received before the last success can fail after it. If
        its failed row is gone, a Retry that fails again still turns the
        pill red."""
        event = self._failed_event_without_row('LG_WH_QR_LATE',
                                               timedelta(hours=3))
        with mock.patch.object(self.IngestClass, 'ingest_leadgen',
                               side_effect=MetaPermanentError('still bad')):
            event.action_retry()
        self.env.invalidate_all()
        self.assertEqual(len(self._failed_rows('LG_WH_QR_LATE')), 1)
        webhook = self._webhook()
        self.assertEqual(webhook['status'], 'failing')
        self.assertEqual(webhook['failed_count'], 1)

    @mute_logger(_DRAIN_LOGGER)
    def test_queue_retry_unexpected_error_without_row_dates_failure_now(self):
        """A Retry that raises unexpectedly and finds no failed row dates
        the one it makes now."""
        event = self._failed_event_without_row('LG_WH_QR_BOOM_NEW',
                                               timedelta(hours=3))
        with mock.patch.object(type(self.env['meta.sync.log']), '_retry_one',
                               side_effect=RuntimeError('boom')):
            event.action_retry()
        self.env.invalidate_all()
        self.assertEqual(event.status, 'failed')
        row = self._failed_rows('LG_WH_QR_BOOM_NEW')
        self.assertEqual(len(row), 1)
        self.assertEqual(row.trigger, 'webhook')
        self.assertGreater(row.failed_at, event.create_date)
        self.assertEqual(self._webhook()['status'], 'failing')
