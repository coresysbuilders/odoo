# Meta Lead Ads Integration for Odoo — © 2026 CoreSys Builders. All rights reserved.
# Licensed under the Odoo Proprietary License v1.0 (OPL-1).
# Unauthorized copying, redistribution, or resale of this software, in whole or in
# part, via any medium, is strictly prohibited and constitutes a license violation.
# OPL-1: https://www.odoo.com/documentation/18.0/legal/licenses.html#odoo-apps

"""Scheduler health check.

If Odoo's cron worker isn't running, lead sync stops without any error, so
the module tracks a heartbeat and reports ok / pending / stalled / disabled.
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
        """A recent heartbeat reports ok."""
        self.drain.active = True
        self._set(self.HB, fields.Datetime.now())
        self.assertEqual(self.Account._scheduler_health()['status'], 'ok')

    def test_stalled_when_heartbeat_stale_and_nextcall_overdue(self):
        """An old heartbeat with an overdue nextcall reports stalled."""
        self.drain.active = True
        self._set(self.HB, fields.Datetime.now() - timedelta(hours=2))
        self.drain.nextcall = fields.Datetime.now() - timedelta(hours=2)
        self.assertEqual(self.Account._scheduler_health()['status'], 'stalled')

    def test_pending_right_after_install(self):
        """Just installed with no run yet reports pending."""
        self.drain.active = True
        self._set(self.HB, False)
        self._set(self.INST, fields.Datetime.now())
        self.drain.nextcall = fields.Datetime.now()
        self.assertEqual(self.Account._scheduler_health()['status'], 'pending')

    def test_stalled_when_never_ran_since_old_install(self):
        """No run hours after install reports stalled, even if nextcall isn't overdue."""
        self.drain.active = True
        self._set(self.HB, False)
        self._set(self.INST, fields.Datetime.now() - timedelta(hours=2))
        self.drain.nextcall = fields.Datetime.now()
        self.assertEqual(self.Account._scheduler_health()['status'], 'stalled')

    def test_disabled_when_cron_inactive(self):
        """An archived drain cron reports disabled, not stalled."""
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
        """The account's cron_status field shows the health status."""
        self.drain.active = True
        self._set(self.HB, fields.Datetime.now())
        account = self.Account.create({'name': 'B', 'account_id': 'ACC_SCHED2'})
        account.invalidate_recordset(['cron_status', 'cron_status_message'])
        self.assertEqual(account.cron_status, 'ok')
        self.assertTrue(account.cron_status_message)
