# Meta Lead Ads Integration for Odoo — © 2026 CoreSys Builders. All rights reserved.
# Licensed under the Odoo Proprietary License v1.0 (OPL-1).
# Unauthorized copying, redistribution, or resale of this software, in whole or in
# part, via any medium, is strictly prohibited and constitutes a license violation.
# OPL-1: https://www.odoo.com/documentation/18.0/legal/licenses.html#odoo-apps

"""Scheduler self-check: the module flags when Odoo's cron worker isn't
actually running its jobs (which otherwise makes lead sync fail silently).

Covers the heartbeat ping, the on-demand health classification across its
states (ok / pending / stalled / disabled), and the manual check action.
"""
from datetime import timedelta

from odoo import fields
from odoo.tests.common import TransactionCase, tagged


@tagged('post_install', '-at_install')
class TestSchedulerHealth(TransactionCase):

    def setUp(self):
        super().setUp()
        self.Account = self.env['meta.account']
        self.ICP = self.env['ir.config_parameter'].sudo()
        self.drain = self.env.ref('meta_lead_ads.cron_meta_webhook_drain')
        self.HB = 'meta_lead_ads.cron_last_run'
        self.INST = 'meta_lead_ads.installed_at'

    def _set(self, key, dt):
        self.ICP.set_param(key, fields.Datetime.to_string(dt) if dt else '')

    def test_ok_when_recent_heartbeat(self):
        """A fresh heartbeat is positive proof the scheduler is running."""
        self.drain.active = True
        self._set(self.HB, fields.Datetime.now())
        self.assertEqual(self.Account._scheduler_health()['status'], 'ok')

    def test_stalled_when_heartbeat_stale_and_nextcall_overdue(self):
        """No recent run and a nextcall frozen well in the past -> stalled."""
        self.drain.active = True
        self._set(self.HB, fields.Datetime.now() - timedelta(hours=2))
        self.drain.nextcall = fields.Datetime.now() - timedelta(hours=2)
        self.assertEqual(self.Account._scheduler_health()['status'], 'stalled')

    def test_pending_right_after_install(self):
        """Freshly installed, no run yet, nextcall not overdue -> pending."""
        self.drain.active = True
        self._set(self.HB, False)
        self._set(self.INST, fields.Datetime.now())
        self.drain.nextcall = fields.Datetime.now()
        self.assertEqual(self.Account._scheduler_health()['status'], 'pending')

    def test_stalled_when_never_ran_since_old_install(self):
        """Installed a while ago, still no run, even if nextcall isn't yet
        overdue -> stalled (the scheduler never confirmed itself)."""
        self.drain.active = True
        self._set(self.HB, False)
        self._set(self.INST, fields.Datetime.now() - timedelta(hours=2))
        self.drain.nextcall = fields.Datetime.now()
        self.assertEqual(self.Account._scheduler_health()['status'], 'stalled')

    def test_disabled_when_cron_inactive(self):
        """An inactive drain cron is reported distinctly so the admin re-enables
        it rather than chasing a server misconfig."""
        self.drain.active = False
        self.assertEqual(self.Account._scheduler_health()['status'], 'disabled')

    def test_heartbeat_ping_sets_recent_param(self):
        self._set(self.HB, False)
        self.Account._ping_scheduler_heartbeat()
        val = self.ICP.get_param(self.HB)
        self.assertTrue(val)
        self.assertLess(
            fields.Datetime.now() - fields.Datetime.to_datetime(val),
            timedelta(minutes=1))

    def test_action_check_scheduler_returns_notification(self):
        self.drain.active = True
        self._set(self.HB, fields.Datetime.now())
        account = self.Account.create({'name': 'A', 'account_id': 'ACC_SCHED'})
        action = account.action_check_scheduler()
        self.assertEqual(action['tag'], 'display_notification')
        self.assertIn('message', action['params'])

    def test_computed_status_on_account(self):
        """The form-facing computed field mirrors the health status."""
        self.drain.active = True
        self._set(self.HB, fields.Datetime.now())
        account = self.Account.create({'name': 'B', 'account_id': 'ACC_SCHED2'})
        account.invalidate_recordset(['cron_status', 'cron_status_message'])
        self.assertEqual(account.cron_status, 'ok')
        self.assertTrue(account.cron_status_message)
