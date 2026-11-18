# Meta Lead Ads Integration for Odoo — © 2026 CoreSys Builders. All rights reserved.
# Licensed under the Odoo Proprietary License v1.0 (OPL-1).
# Unauthorized copying, redistribution, or resale of this software, in whole or in
# part, via any medium, is strictly prohibited and constitutes a license violation.
# OPL-1: https://www.odoo.com/documentation/20.0/legal/licenses.html#odoo-apps

"""Tests for the new-lead notification setting.

A new Meta lead notifies one user or every active member of a group. It is
off by default, only fires when a lead is created (not on a dedup match or
an idempotent skip), and a failure in it never breaks ingestion.

Alerts go straight to the Odoo inbox whatever the user's email preference,
so they still arrive when SMTP is down. The tests check the real inbox
notifications rather than mocking message_notify.
"""
from unittest import mock

from odoo import Command
from odoo.tests.common import TransactionCase, new_test_user, tagged
from odoo.tools import html2plaintext, mute_logger

from odoo.addons.meta_lead_ads.models import meta_lead_ingest
from odoo.addons.meta_lead_ads.tests.test_ingest import IngestFixtureMixin
from odoo.addons.meta_lead_ads.models.const import (
    LEAD_NAME_PARAM, NOTIFY_ENABLED_PARAM, NOTIFY_TARGET_PARAM,
    NOTIFY_USER_PARAM, NOTIFY_GROUP_PARAM,
)

_ALERT_SUBJECT = "New Meta lead"


@tagged('post_install', '-at_install')
class TestLeadNotify(IngestFixtureMixin, TransactionCase):

    def setUp(self):
        super().setUp()
        self.ICP = self.env['ir.config_parameter'].sudo()
        self.group = self.env['res.groups'].create({'name': 'Meta Notify Test'})
        # Recipients must be able to read the lead, and a no-demo database
        # gives new users no Sales rights, so grant them explicitly.
        all_leads = self.env.ref('sales_team.group_sale_salesman_all_leads')
        self.user_a = self.env['res.users'].create({
            'name': 'Notify A', 'login': 'notify_a',
            'group_ids': [(4, self.group.id), (4, all_leads.id)],
        })
        self.user_b = self.env['res.users'].create({
            'name': 'Notify B', 'login': 'notify_b',
            'group_ids': [(4, self.group.id), (4, all_leads.id)],
        })
        self.co_a = self.env.company
        self.co_b = self.env['res.company'].create({'name': 'Notify Co B'})
        # With an email preference, message_notify would send mail instead
        # of using the inbox; the alert must reach the inbox anyway.
        (self.user_a + self.user_b).write({'notification_type': 'email'})

    def _enable(self, target_type='user', user=None, group=None):
        self.ICP.set_str(NOTIFY_ENABLED_PARAM, '1')
        self.ICP.set_str(NOTIFY_TARGET_PARAM, target_type)
        self.ICP.set_str(NOTIFY_USER_PARAM, str(user.id) if user else '')
        self.ICP.set_str(NOTIFY_GROUP_PARAM, str(group.id) if group else '')

    def _inbox_recipients(self, lead):
        """Partner ids that got an inbox alert for ``lead``."""
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
        """A user target gets an inbox alert even though they prefer email."""
        self._enable('user', user=self.user_a)
        with self._patch_graph():
            lead = self.Ingest.ingest_leadgen(self.page, 'LG1', 'manual')
        self.assertEqual(
            self._inbox_recipients(lead), {self.user_a.partner_id.id})
        # And no email notification, so nothing depends on SMTP.
        email_notifs = self.env['mail.notification'].search([
            ('mail_message_id', 'in', self._alert_messages(lead).ids),
            ('notification_type', '=', 'email'),
        ])
        self.assertFalse(email_notifs)

    def test_group_target_notifies_all_active_members_inbox(self):
        """A group target alerts every active member."""
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
        self.assertEqual(len(self._alert_messages(lead)), 1)

    def test_dedup_match_does_not_notify(self):
        """A lead that matches an existing one by email does not notify."""
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

    # ---- only users who can read the lead are notified -------------------

    def _user(self, login, company, group=None, groups=(
            'base.group_user,sales_team.group_sale_salesman_all_leads')):
        user = new_test_user(
            self.env, login, groups=groups, email='%s@notify.test' % login,
            company_id=company.id, company_ids=[Command.set([company.id])])
        if group:
            user.write({'group_ids': [Command.link(group.id)]})
        return user

    def _logged(self, mocked, level):
        """Formatted lines sent to the patched module logger at ``level``."""
        return [c.args[0] % c.args[1:]
                for c in getattr(mocked, level).call_args_list]

    def test_other_company_user_dropped(self):
        """A target user who only belongs to another company gets nothing."""
        user = self._user('notify_far', self.co_b)
        self._enable('user', user=user)
        with self._patch_graph():
            lead = self.Ingest.ingest_leadgen(self.page, 'LG1', 'manual')
        self.assertEqual(lead.company_id, self.co_a)
        self.assertFalse(self._alert_messages(lead))

    def test_alert_reply_to_uses_lead_company(self):
        """Replies go to the lead's company, not to the cron's."""
        self.co_a.email = 'office@co-a.example'
        self.co_b.email = 'office@co-b.example'
        self.page.company_id = self.co_b
        reader = self._user('notify_far', self.co_b)
        self._enable('user', user=reader)
        with self._patch_graph():
            lead = self.Ingest.ingest_leadgen(self.page, 'LG1', 'manual')
        self.assertEqual(lead.company_id, self.co_b)
        message = self._alert_messages(lead)
        self.assertEqual(len(message), 1)
        self.assertIn('office@co-b.example', message.reply_to)
        self.assertNotIn('co-a', message.reply_to)
        # A company without an email falls back to the current company.
        # email_formatted only depends on the partner, so drop the cache.
        self.co_b.write({'email': False, 'alias_domain_id': False})
        self.env.invalidate_all()
        message = self.Ingest._post_inbox_alert(
            lead, reader.partner_id, _ALERT_SUBJECT, 'again')
        self.assertIn('office@co-a.example', message.reply_to)

    def test_group_target_keeps_only_readers(self):
        """Group members outside the lead company are left out."""
        group = self.env['res.groups'].create({'name': 'Meta Notify Mixed'})
        near = self._user('notify_near', self.co_a, group=group)
        far = self._user('notify_far', self.co_b, group=group)
        self._enable('group', group=group)
        with self._patch_graph():
            lead = self.Ingest.ingest_leadgen(self.page, 'LG1', 'manual')
        notified = self._inbox_recipients(lead)
        self.assertEqual(notified, {near.partner_id.id})
        self.assertNotIn(far.partner_id.id, notified)

    def test_own_leads_salesperson_dropped(self):
        """A salesperson limited to their own leads isn't told about others."""
        own = self._user('notify_own', self.co_a,
                         groups='base.group_user,sales_team.group_sale_salesman')
        self._enable('user', user=own)
        # Their rule also shows unassigned leads, so the lead has to belong
        # to someone else for this case to mean anything. Ingest only
        # assigns the Page's default salesperson.
        self.page.sudo().lead_user_id = self._user('notify_owner', self.co_a)
        with self._patch_graph():
            lead = self.Ingest.ingest_leadgen(self.page, 'LG1', 'manual')
        self.assertTrue(lead.user_id)
        self.assertNotEqual(lead.user_id, own)
        self.assertFalse(self._alert_messages(lead))

    def test_nobody_qualifies_posts_nothing(self):
        """When no recipient can read the lead, no alert message exists."""
        group = self.env['res.groups'].create({'name': 'Meta Notify Far'})
        self._user('notify_far1', self.co_b, group=group)
        self._user('notify_far2', self.co_b, group=group)
        self._enable('group', group=group)
        with self._patch_graph():
            lead = self.Ingest.ingest_leadgen(self.page, 'LG1', 'manual')
        self.assertTrue(lead.exists())
        self.assertEqual(lead.meta_leadgen_id, 'LG1')
        self.assertFalse(self._alert_messages(lead))

    def test_dropped_recipient_log_has_ids_only(self):
        """The skipped-recipient line names ids, not people or lead data."""
        user = self._user('notify_far', self.co_b)
        self._enable('user', user=user)
        # assertLogs sees nothing from this logger under the test runner,
        # so the module logger is patched instead.
        with self._patch_graph(), \
                mock.patch.object(meta_lead_ingest, '_logger') as log:
            lead = self.Ingest.ingest_leadgen(self.page, 'LG1', 'manual')
        lines = [line for line in self._logged(log, 'info')
                 if 'skipped new-lead alert' in line]
        self.assertEqual(len(lines), 1)
        self.assertIn('user id=%s' % user.id, lines[0])
        self.assertIn('lead id=%s' % lead.id, lines[0])
        for private in (user.name, user.login, user.email, lead.name,
                        lead.email_from, 'Jane'):
            self.assertNotIn(private, lines[0])

    def test_check_error_drops_only_that_recipient(self):
        """A failing access check skips that user and still alerts the rest."""
        group = self.env['res.groups'].create({'name': 'Meta Notify Pair'})
        broken = self._user('notify_broken', self.co_a, group=group)
        fine = self._user('notify_fine', self.co_a, group=group)
        self._enable('group', group=group)
        Lead = type(self.env['crm.lead'])
        original = Lead.has_access

        def has_access(records, operation):
            if records.env.uid == broken.id:
                raise RuntimeError("rule failed for %s" % broken.email)
            return original(records, operation)

        with self._patch_graph(), \
                mock.patch.object(Lead, 'has_access', has_access), \
                mock.patch.object(meta_lead_ingest, '_logger') as log:
            lead = self.Ingest.ingest_leadgen(self.page, 'LG1', 'manual')
        self.assertEqual(self._inbox_recipients(lead), {fine.partner_id.id})
        lines = [line for line in self._logged(log, 'warning')
                 if 'access check failed' in line]
        self.assertEqual(len(lines), 1)
        self.assertIn('user id=%s' % broken.id, lines[0])
        self.assertIn('crm.lead id=%s' % lead.id, lines[0])
        for private in (broken.name, broken.login, broken.email, lead.name):
            self.assertNotIn(private, lines[0])


@tagged('post_install', '-at_install')
class TestEscaping(IngestFixtureMixin, TransactionCase):
    """Text from the ad form shows as text, in the description and in alerts.

    Anyone can fill in a Meta lead form, so an answer or a name holding HTML
    must never turn into a live link or markup in Odoo.
    """

    def _ingest_with(self, leadgen_id, *extra, field_data=None):
        if field_data is None:
            field_data = [
                {'name': 'email', 'values': ['jane@example.com']},
                {'name': 'full_name', 'values': ['Jane Doe']},
            ]
        payload = self._fake_lead(id=leadgen_id,
                                  field_data=field_data + list(extra))
        with self._patch_graph(fetch_lead=payload):
            return self.Ingest.ingest_leadgen(self.page, leadgen_id, 'manual')

    # ---- description block ------------------------------------------------

    def test_answer_markup_stored_as_text(self):
        """A link typed into an answer is stored escaped, never as a link."""
        raw = '<a href="https://evil">click</a>'
        lead = self._ingest_with('LGESC1', {'name': 'site', 'values': [raw]})
        description = str(lead.description)
        self.assertIn('class="o_meta_lead_qa"', description)
        self.assertIn('&lt;a href=', description)
        self.assertNotIn('<a href', description)
        answer = lead.answer_ids.filtered(lambda a: a.question_key == 'site')
        self.assertEqual(len(answer), 1)
        # The answer row keeps the raw text; only the HTML copy is escaped.
        self.assertEqual(answer.value, raw)
        self.assertEqual(answer.label, answer.question_key)

    def test_append_keeps_existing_html(self):
        """A matched lead's notes stay HTML when the block is added after them."""
        existing = self.Lead.create({
            'name': 'Existing', 'email_from': 'jane@example.com',
            'description': '<p>Called back</p>',
        })
        lead = self._ingest_with('LGESC2', {'name': 'site', 'values': ['x']})
        self.assertEqual(lead, existing)
        description = str(lead.description)
        self.assertIn('<p>Called back</p>', description)
        self.assertNotIn('&lt;p&gt;', description)
        self.assertEqual(description.count('o_meta_lead_qa'), 1)
        self.assertIn('site: x', description)

    def test_ampersand_and_quotes_round_trip(self):
        """& and quotes come back as the same text, stored as entities."""
        raw = 'Tom & "Jerry\'s" shop'
        lead = self._ingest_with('LGESC3', {'name': 'shop', 'values': [raw]})
        description = str(lead.description)
        self.assertIn('Tom &amp; ', description)
        self.assertIn('shop: Tom & "Jerry\'s" shop',
                      html2plaintext(description))
        answer = lead.answer_ids.filtered(lambda a: a.question_key == 'shop')
        self.assertEqual(answer.value, raw)

    # ---- notification bodies ----------------------------------------------

    def test_alert_shows_lead_name_as_text(self):
        """A lead name holding HTML shows as text in the inbox alert."""
        reader = new_test_user(
            self.env, 'escape_reader',
            groups='base.group_user,sales_team.group_sale_salesman_all_leads')
        ICP = self.env['ir.config_parameter'].sudo()
        ICP.set_str(NOTIFY_ENABLED_PARAM, '1')
        ICP.set_str(NOTIFY_TARGET_PARAM, 'user')
        ICP.set_str(NOTIFY_USER_PARAM, str(reader.id))
        # Title the lead after the person who filled in the form.
        ICP.set_str(LEAD_NAME_PARAM, '{contact_name}')
        lead = self._ingest_with('LGESC4', field_data=[
            {'name': 'email', 'values': ['bold@example.com']},
            {'name': 'full_name', 'values': ['<b>x</b>']},
        ])
        self.assertEqual(lead.name, '<b>x</b>')
        message = self.env['mail.message'].search([
            ('model', '=', 'crm.lead'), ('res_id', '=', lead.id),
            ('message_type', '=', 'user_notification'),
        ])
        self.assertEqual(len(message), 1)
        body = str(message.body)
        self.assertIn('&lt;b&gt;x&lt;/b&gt;', body)
        self.assertNotIn('<b>x</b>', body)
        self.assertEqual(message.subject, _ALERT_SUBJECT)
        self.assertIn('-openerp-meta-lead-alert@', message.message_id)

    def test_token_dead_mail_escapes_account_name(self):
        """An account name holding HTML shows as text in the token email."""
        account = self.env['meta.account'].create({
            'name': '<i>Acme</i>', 'account_id': 'ACCESC',
            'app_id': 'app_test', 'app_secret': 'secret_test',
            'access_token': 'tok_esc',
        })
        admin = new_test_user(
            self.env, 'escape_admin', email='escape_admin@example.com',
            groups='base.group_user,meta_lead_ads.group_meta_admin')
        account._send_token_dead_mail(admin)
        mail = self.env['mail.mail'].search([
            ('email_to', '=', 'escape_admin@example.com')])
        self.assertEqual(len(mail), 1)
        self.assertIn('&lt;i&gt;Acme&lt;/i&gt;', mail.body_html)
        self.assertNotIn('<i>Acme</i>', mail.body_html)
        self.assertTrue(mail.body_html.startswith('<p>'))
        self.assertTrue(mail.body_html.endswith('</p>'))
        # The subject is plain text, so it keeps the name as typed.
        self.assertEqual(mail.subject,
                         'Meta Lead Ads: access token invalid for <i>Acme</i>')

    def test_duplicate_note_names_ids_only(self):
        """The also-matches note lists lead ids and nothing typed by anyone."""
        first = self.Lead.create({'name': 'First',
                                  'email_from': 'dup@example.com'})
        second = self.Lead.create({'name': '<b>Second</b>',
                                   'email_from': 'dup@example.com'})
        lead = self._ingest_with('LGESC5', field_data=[
            {'name': 'email', 'values': ['dup@example.com']},
            {'name': 'full_name', 'values': ['Jane Doe']},
        ])
        self.assertIn(lead, first | second)
        other = (first | second) - lead
        lead.invalidate_recordset()
        notes = lead.message_ids.filtered(
            lambda m: 'Consider merging' in (m.body or ''))
        self.assertEqual(len(notes), 1)
        body = str(notes.body)
        self.assertIn('#%d' % other.id, body)
        self.assertNotIn('Second', body)
        self.assertNotIn('<', body.replace('<p>', '').replace('</p>', ''))
