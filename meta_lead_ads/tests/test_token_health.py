# Meta Lead Ads Integration for Odoo — © 2026 CoreSys Builders. All rights reserved.
# Licensed under the Odoo Proprietary License v1.0 (OPL-1).
# Unauthorized copying, redistribution, or resale of this software, in whole or in
# part, via any medium, is strictly prohibited and constitutes a license violation.
# OPL-1: https://www.odoo.com/documentation/18.0/legal/licenses.html#odoo-apps

"""Tests for the token-health cron on meta.account.

The cron re-runs ``action_test_connection`` (patched on the class here) for
each active account. When a token is invalid and there is no open alert
activity yet, it schedules one To-Do and emails the Meta admins. The email
must not contain any token or secret.
"""
from unittest import mock

from odoo import SUPERUSER_ID, Command
from odoo.tests.common import TransactionCase, new_test_user, tagged

from odoo.addons.meta_lead_ads.models import meta_account
from odoo.addons.meta_lead_ads.tests.test_multi_company import (
    MultiCompanyFixtureMixin,
)

TODO_XMLID = 'mail.mail_activity_data_todo'
ALERT_SUMMARY = 'Meta token invalid/expired'   # same as in _alert_token_dead


class TokenHealthFixtureMixin:
    """A healthy meta.account and a Meta admin with an email address."""

    def setUp(self):
        super().setUp()
        self.Account = self.env['meta.account']
        self.Activity = self.env['mail.activity']
        self.Mail = self.env['mail.mail']
        self.AccountClass = type(self.Account)
        self.admin_group = self.env.ref('meta_lead_ads.group_meta_admin')
        base_internal = self.env.ref('base.group_user')
        self.admin_user = self.env['res.users'].create({
            'name': 'Meta Admin', 'login': 'meta_admin_th',
            'email': 'admin_th@example.com',
            'groups_id': [(6, 0, [base_internal.id, self.admin_group.id])]})
        self.account = self.Account.create({
            'name': 'Acct', 'account_id': 'ACC1',
            'app_id': 'app_test', 'app_secret': 'secret_test',
            'access_token': 'tok_acct', 'token_valid': True,
        })

    def _activity_count(self, account):
        return self.Activity.search_count([
            ('res_model', '=', 'meta.account'), ('res_id', '=', account.id)])

    def _mail_for(self, account):
        return self.Mail.search([('subject', 'ilike', account.name)])

    def _flip_dead(self):
        """Side effect for a token that goes from valid to invalid."""
        def _side(self_account):
            self_account.write({'token_valid': False})
            return True
        return _side


@tagged('post_install', '-at_install')
class TestMetaTokenHealth(TokenHealthFixtureMixin, TransactionCase):
    """Alerting, alert dedup and per-account isolation in the token cron."""

    def _stay_dead(self):
        """Side effect for a token that was already invalid before the run."""
        def _side(self_account):
            self_account.write({'token_valid': False})
            return True
        return _side

    def test_alert_on_invalid(self):
        """A token that goes invalid gets a To-Do activity on its account."""
        with mock.patch.object(self.AccountClass, 'action_test_connection',
                               autospec=True, side_effect=self._flip_dead()):
            self.Account._cron_token_health()
        self.assertEqual(self.Activity.search_count([
            ('res_model', '=', 'meta.account'),
            ('res_id', '=', self.account.id)]), 1)

    def test_email_admins(self):
        """Admins get an email naming the account, with no token or secret."""
        with mock.patch.object(self.AccountClass, 'action_test_connection',
                               autospec=True, side_effect=self._flip_dead()):
            self.Account._cron_token_health()
        mails = self._mail_for(self.account)
        self.assertTrue(mails)
        self.assertIn('admin_th@example.com',
                      ','.join(mails.mapped('email_to') or []))
        blob = ' '.join((mails.mapped('subject') or [])
                        + (mails.mapped('body_html') or []))
        for secret in ('tok_acct', 'secret_test', 'app_test'):
            self.assertNotIn(secret, blob)
        self.assertIn(self.account.name, blob)

    def test_no_realert(self):
        """A second run with the alert still open sends nothing new."""
        with mock.patch.object(self.AccountClass, 'action_test_connection',
                               autospec=True, side_effect=self._flip_dead()):
            self.Account._cron_token_health()
            acts_after_first = self._activity_count(self.account)
            mails_after_first = len(self._mail_for(self.account))
            self.Account._cron_token_health()
            acts_after_second = self._activity_count(self.account)
            mails_after_second = len(self._mail_for(self.account))
        self.assertEqual(acts_after_first, 1)
        self.assertEqual(acts_after_second, acts_after_first)
        self.assertEqual(mails_after_second, mails_after_first)

    def test_190_and_isvalid_false(self):
        """An OAuth 190 error and an is_valid:false reply each alert once.

        Both end up as token_valid=False, so the patch just writes that. Two
        accounts keep the alert counts separate.
        """
        acc_190 = self.Account.create({
            'name': 'Acct190', 'account_id': 'ACC190',
            'app_id': 'app_test', 'app_secret': 'secret_test',
            'access_token': 't',
            'token_valid': True})
        acc_isvalid = self.Account.create({
            'name': 'AcctIsValid', 'account_id': 'ACCIV',
            'app_id': 'app_test', 'app_secret': 'secret_test',
            'access_token': 't',
            'token_valid': True})

        def _both_paths(self_account):
            self_account.write({'token_valid': False})
            return True

        # Archive the fixture account so only the two above are checked.
        self.account.active = False
        with mock.patch.object(self.AccountClass, 'action_test_connection',
                               autospec=True, side_effect=_both_paths):
            self.Account._cron_token_health()
        self.assertEqual(self._activity_count(acc_190), 1)
        self.assertEqual(self._activity_count(acc_isvalid), 1)

    def test_no_admin_fallback(self):
        """With no Meta admins, the activity is still created but no email is.

        The activity falls back to the current user; an email with an empty
        recipient list would just fail to send.
        """
        self.admin_group.users.write(
            {'groups_id': [(3, self.admin_group.id)]})
        self.assertFalse(self.admin_group.users)
        with mock.patch.object(self.AccountClass, 'action_test_connection',
                               autospec=True, side_effect=self._flip_dead()):
            self.Account._cron_token_health()
        self.assertEqual(self._activity_count(self.account), 1)
        self.assertEqual(len(self._mail_for(self.account)), 0)

    def test_pre_existing_invalid_alerts(self):
        """An account that was already invalid alerts once, then stays quiet.

        No valid-to-invalid change happens here, so the alert has to be driven
        by the current state plus the open-activity check.
        """
        dead = self.Account.create({
            'name': 'AlreadyDead', 'account_id': 'ACC_DEAD',
            'app_id': 'app_test', 'app_secret': 'secret_test',
            'access_token': 't',
            'token_valid': False})
        self.account.active = False
        with mock.patch.object(self.AccountClass, 'action_test_connection',
                               autospec=True, side_effect=self._stay_dead()):
            self.Account._cron_token_health()
            self.assertEqual(self._activity_count(dead), 1)
            self.assertEqual(len(self._mail_for(dead)), 1)
            # The open activity from the first run blocks a repeat.
            self.Account._cron_token_health()
            self.assertEqual(self._activity_count(dead), 1)
            self.assertEqual(len(self._mail_for(dead)), 1)

    def test_account_isolation(self):
        """A crash on one account doesn't stop the sweep for the next one.

        Each account is checked in its own savepoint, so an unexpected
        exception on the first still lets the second get its alert.
        """
        self.account.active = False
        acc_boom = self.Account.create({
            'name': 'Boom', 'account_id': 'ACC_BOOM',
            'app_id': 'app_test', 'app_secret': 'secret_test',
            'access_token': 't',
            'token_valid': True})
        acc_ok = self.Account.create({
            'name': 'Handled', 'account_id': 'ACC_OK',
            'app_id': 'app_test', 'app_secret': 'secret_test',
            'access_token': 't',
            'token_valid': True})

        def _side(self_account):
            if self_account.id == acc_boom.id:
                # A plain Exception, not a UserError.
                raise Exception('unexpected boom (no token logged)')
            self_account.write({'token_valid': False})
            return True

        with mock.patch.object(self.AccountClass, 'action_test_connection',
                               autospec=True, side_effect=_side):
            self.Account._cron_token_health()
        self.assertEqual(self._activity_count(acc_ok), 1)


@tagged('post_install', '-at_install')
class TestTokenHealthCompany(MultiCompanyFixtureMixin, TokenHealthFixtureMixin,
                             TransactionCase):
    """Token alerts only reach users who can open the account."""

    def setUp(self):
        super().setUp()
        # new_test_user gives these the same generated address; tell them
        # apart in email_to.
        self.admin_a.email = 'th_admin_a@example.com'
        self.admin_b.email = 'th_admin_b@example.com'
        self.admin_ab.email = 'th_admin_ab@example.com'

    def _run_cron(self):
        with mock.patch.object(self.AccountClass, 'action_test_connection',
                               autospec=True, side_effect=self._flip_dead()):
            self.Account._cron_token_health()

    def _alerts(self, account):
        return self.Activity.search([
            ('res_model', '=', 'meta.account'), ('res_id', '=', account.id),
            ('summary', '=', ALERT_SUMMARY)])

    def _email_to(self, mails):
        return ','.join(mails.mapped('email_to') or [])

    def _only_admins(self, users):
        """Leave ``users`` as the only Meta Admins."""
        (self.admin_group.users - users).write(
            {'groups_id': [Command.unlink(self.admin_group.id)]})
        users.write({'groups_id': [Command.link(self.admin_group.id)]})
        self.assertEqual(self.admin_group.users, users)

    def _b_only_user(self, login, groups='base.group_user'):
        return new_test_user(
            self.env, login, groups=groups, email='%s@example.com' % login,
            company_id=self.co_b.id,
            company_ids=[Command.set([self.co_b.id])])

    def _logged(self, mocked, level):
        return [c.args[0] % c.args[1:]
                for c in getattr(mocked, level).call_args_list]

    def test_other_company_admin_not_alerted(self):
        self._run_cron()
        alerts = self._alerts(self.account)
        self.assertEqual(len(alerts), 1)
        self.assertNotEqual(alerts.user_id, self.admin_b)
        email_to = self._email_to(self._mail_for(self.account))
        self.assertNotIn(self.admin_b.email, email_to)
        self.assertIn(self.admin_user.email, email_to)

    def test_assignee_from_readers_only(self):
        """The first admin in the group is skipped if it can't read the account."""
        b_first = self._b_only_user(
            'th_b_first', groups='base.group_user,meta_lead_ads.group_meta_admin')
        # Group members come back in name order.
        b_first.name = 'Aaron First'
        self.admin_group.invalidate_recordset(['users'])
        self.assertEqual(self.admin_group.users[:1], b_first)
        self._run_cron()
        alerts = self._alerts(self.account)
        self.assertEqual(len(alerts), 1)
        self.assertNotEqual(alerts.user_id, b_first)
        self.assertIn(alerts.user_id, self.admin_group.users)
        self.assertIn(self.co_a, alerts.user_id.company_ids)

    def test_only_foreign_admins_falls_back_to_cron_user(self):
        self._only_admins(self.admin_b)
        self._run_cron()
        alerts = self._alerts(self.account)
        self.assertEqual(len(alerts), 1)
        self.assertEqual(alerts.user_id.id, SUPERUSER_ID)
        self.assertFalse(self._mail_for(self.account))

    def test_fallback_todo_moves_to_first_real_reader(self):
        self._only_admins(self.admin_b)
        self._run_cron()
        self.assertEqual(self._alerts(self.account).user_id.id, SUPERUSER_ID)
        # A Meta Admin who can open the account joins later.
        self._only_admins(self.admin_a | self.admin_b)
        mails_before = self._mail_for(self.account)
        self._run_cron()
        alerts = self._alerts(self.account)
        self.assertEqual(len(alerts), 1)
        self.assertEqual(alerts.user_id, self.admin_a)
        new_mails = self._mail_for(self.account) - mails_before
        self.assertEqual(len(new_mails), 1)
        self.assertEqual(new_mails.email_to, self.admin_a.email)
        # From then on the To-Do blocks repeats, as before.
        self._run_cron()
        self.assertEqual(self._alerts(self.account), alerts)
        self.assertEqual(self._mail_for(self.account) - mails_before,
                         new_mails)

    def test_unreadable_fallback_user_gets_nothing(self):
        self._only_admins(self.admin_b)
        cron_user = self._b_only_user('th_cron_b')
        with mock.patch.object(meta_account, '_logger') as log:
            self.account.with_user(cron_user).sudo()._alert_token_dead()
        self.assertEqual(self._activity_count(self.account), 0)
        self.assertFalse(self._mail_for(self.account))
        lines = [line for line in self._logged(log, 'warning')
                 if 'not scheduled' in line]
        self.assertEqual(len(lines), 1)
        self.assertIn('account id=%s' % self.account.id, lines[0])
        for private in (self.account.name, cron_user.login, cron_user.email,
                        self.admin_b.email):
            self.assertNotIn(private, lines[0])

    def _stale_alert(self):
        """An open alert from before the account moved to company B."""
        self.account.activity_schedule(
            'mail.mail_activity_data_todo', summary=ALERT_SUMMARY,
            user_id=self.admin_a.id)
        self.account.sudo().write({'company_id': self.co_b.id})

    def test_stale_todo_reassigned_after_company_move(self):
        self._stale_alert()
        # admin_ab could read it too; keep admin_b the only reader so the
        # mail goes to one address.
        self._only_admins(self.admin_a | self.admin_b)
        mails_before = self._mail_for(self.account)
        self.account._alert_token_dead()
        alerts = self._alerts(self.account)
        self.assertEqual(len(alerts), 1)
        self.assertEqual(alerts.user_id, self.admin_b)
        new_mails = self._mail_for(self.account) - mails_before
        self.assertEqual(len(new_mails), 1)
        self.assertEqual(new_mails.email_to, self.admin_b.email)
        # The reassigned To-Do blocks repeats, as before.
        self.account._alert_token_dead()
        self.assertEqual(len(self._alerts(self.account)), 1)
        self.assertEqual(self._mail_for(self.account) - mails_before,
                         new_mails)

    def test_stale_todo_removed_when_nobody_qualifies(self):
        self._stale_alert()
        self._only_admins(self.admin_a)
        cron_user = new_test_user(
            self.env, 'th_cron_a', email='th_cron_a@example.com',
            company_id=self.co_a.id,
            company_ids=[Command.set([self.co_a.id])])
        with mock.patch.object(meta_account, '_logger') as log:
            self.account.with_user(cron_user).sudo()._alert_token_dead()
        self.assertEqual(self._activity_count(self.account), 0)
        self.assertFalse(self._mail_for(self.account))
        lines = [line for line in self._logged(log, 'warning')
                 if 'removed token alert' in line]
        self.assertEqual(len(lines), 1)
        self.assertIn('account id=%s' % self.account.id, lines[0])
        for private in (self.account.name, self.admin_a.email,
                        cron_user.login):
            self.assertNotIn(private, lines[0])
