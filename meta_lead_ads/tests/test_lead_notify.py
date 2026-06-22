# Meta Lead Ads Integration for Odoo — © 2026 CoreSys Builders. All rights reserved.
# Licensed under the Odoo Proprietary License v1.0 (OPL-1).
# Unauthorized copying, redistribution, or resale of this software, in whole or in
# part, via any medium, is strictly prohibited and constitutes a license violation.
# OPL-1: https://www.odoo.com/documentation/18.0/legal/licenses.html#odoo-apps

"""Tests for the new-lead notification setting.

A fresh Meta lead notifies either one configured user or every active member
of a configured group. The notification:
  - is off by default,
  - fires only on the create path (never on a dedup/enrich match or an
    idempotent skip),
  - excludes inactive group members,
  - and never breaks ingestion if it fails.

Delivery is to the recipients' Odoo INBOX (a ``user_notification`` message +
``inbox`` mail.notification rows), independent of each recipient's email-vs-
inbox preference and of outgoing email (SMTP) availability — an operational
alert must not vanish when a recipient prefers email and the mail server is
down. The tests therefore assert on the real inbox notifications, not on a
mocked message_notify.
"""
from unittest import mock

from odoo.tests.common import TransactionCase, tagged
from odoo.tools import mute_logger

from odoo.addons.meta_lead_ads.tests.test_ingest import IngestFixtureMixin
from odoo.addons.meta_lead_ads.models.const import (
    NOTIFY_ENABLED_PARAM, NOTIFY_TARGET_PARAM,
    NOTIFY_USER_PARAM, NOTIFY_GROUP_PARAM,
)

_ALERT_SUBJECT = "New Meta lead"


@tagged('post_install', '-at_install')
class TestLeadNotify(IngestFixtureMixin, TransactionCase):

    def setUp(self):
        super().setUp()
        self.ICP = self.env['ir.config_parameter'].sudo()
        self.group = self.env['res.groups'].create({'name': 'Meta Notify Test'})
        self.user_a = self.env['res.users'].create({
            'name': 'Notify A', 'login': 'notify_a',
            'groups_id': [(4, self.group.id)],
        })
        self.user_b = self.env['res.users'].create({
            'name': 'Notify B', 'login': 'notify_b',
            'groups_id': [(4, self.group.id)],
        })
        # Recipients prefer EMAIL — the exact production scenario that, under
        # message_notify, would route to a (failing) SMTP send and never reach
        # the inbox. The inbox-direct delivery must reach them regardless.
        (self.user_a + self.user_b).write({'notification_type': 'email'})

    def _enable(self, target_type='user', user=None, group=None):
        self.ICP.set_param(NOTIFY_ENABLED_PARAM, '1')
        self.ICP.set_param(NOTIFY_TARGET_PARAM, target_type)
        self.ICP.set_param(NOTIFY_USER_PARAM, str(user.id) if user else '')
        self.ICP.set_param(NOTIFY_GROUP_PARAM, str(group.id) if group else '')

    def _inbox_recipients(self, lead):
        """Partner ids that received an INBOX alert notification for ``lead``."""
        notifs = self.env['mail.notification'].search([
            ('mail_message_id.model', '=', 'crm.lead'),
            ('mail_message_id.res_id', '=', lead.id),
            ('mail_message_id.subject', '=', _ALERT_SUBJECT),
            ('notification_type', '=', 'inbox'),
        ])
        return set(notifs.mapped('res_partner_id').ids)

    def _alert_messages(self, lead):
        return self.env['mail.message'].search([
            ('model', '=', 'crm.lead'),
            ('res_id', '=', lead.id),
            ('subject', '=', _ALERT_SUBJECT),
        ])

    def test_disabled_by_default_no_notification(self):
        """With the setting untouched, a new lead notifies nobody."""
        with self._patch_graph():
            lead = self.Ingest.ingest_leadgen(self.page, 'LG1', 'manual')
        self.assertFalse(self._inbox_recipients(lead))

    def test_user_target_notifies_that_user_inbox(self):
        """A 'specific user' target posts an inbox alert to that user's partner,
        as an inbox notification even though the user prefers email."""
        self._enable('user', user=self.user_a)
        with self._patch_graph():
            lead = self.Ingest.ingest_leadgen(self.page, 'LG1', 'manual')
        self.assertEqual(
            self._inbox_recipients(lead), {self.user_a.partner_id.id})
        # No email notification (and so no dependency on SMTP) was produced.
        email_notifs = self.env['mail.notification'].search([
            ('mail_message_id', 'in', self._alert_messages(lead).ids),
            ('notification_type', '=', 'email'),
        ])
        self.assertFalse(email_notifs)

    def test_group_target_notifies_all_active_members_inbox(self):
        """A 'role/group' target posts an inbox alert to every active member."""
        self._enable('group', group=self.group)
        with self._patch_graph():
            lead = self.Ingest.ingest_leadgen(self.page, 'LG1', 'manual')
        notified = self._inbox_recipients(lead)
        self.assertIn(self.user_a.partner_id.id, notified)
        self.assertIn(self.user_b.partner_id.id, notified)

    def test_inactive_group_member_excluded(self):
        """An archived group member is not notified."""
        self.user_b.active = False
        self._enable('group', group=self.group)
        with self._patch_graph():
            lead = self.Ingest.ingest_leadgen(self.page, 'LG1', 'manual')
        notified = self._inbox_recipients(lead)
        self.assertIn(self.user_a.partner_id.id, notified)
        self.assertNotIn(self.user_b.partner_id.id, notified)

    def test_idempotent_skip_does_not_notify(self):
        """Re-ingesting the same leadgen_id (a skip) notifies nobody new."""
        self._enable('user', user=self.user_a)
        with self._patch_graph():
            lead = self.Ingest.ingest_leadgen(self.page, 'LG1', 'manual')
            self.assertEqual(len(self._alert_messages(lead)), 1)
            self.Ingest.ingest_leadgen(self.page, 'LG1', 'manual')  # skip
        # The skip added no second alert message.
        self.assertEqual(len(self._alert_messages(lead)), 1)

    def test_dedup_match_does_not_notify(self):
        """An incoming lead that links to an existing lead by email enriches it
        (match path) and must NOT notify — only brand-new leads notify."""
        self._enable('user', user=self.user_a)
        self.Lead.create({'name': 'Existing', 'email_from': 'jane@example.com'})
        with self._patch_graph():
            lead = self.Ingest.ingest_leadgen(self.page, 'LG1', 'manual')
        self.assertFalse(self._inbox_recipients(lead))

    def test_notification_failure_does_not_break_ingest(self):
        """A failure inside the notification is swallowed; the lead is created."""
        self._enable('user', user=self.user_a)
        with self._patch_graph(), \
                mock.patch.object(type(self.Ingest), '_post_inbox_alert',
                                  side_effect=Exception('boom')), \
                mute_logger(
                    'odoo.addons.meta_lead_ads.models.meta_lead_ingest'):
            lead = self.Ingest.ingest_leadgen(self.page, 'LG1', 'manual')
        self.assertTrue(lead.exists())
        self.assertEqual(lead.meta_leadgen_id, 'LG1')
