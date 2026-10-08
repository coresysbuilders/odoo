# Meta Lead Ads Integration for Odoo — © 2026 CoreSys Builders. All rights reserved.
# Licensed under the Odoo Proprietary License v1.0 (OPL-1).
# Unauthorized copying, redistribution, or resale of this software, in whole or in
# part, via any medium, is strictly prohibited and constitutes a license violation.
# OPL-1: https://www.odoo.com/documentation/19.0/legal/licenses.html#odoo-apps

"""Tests for the token-health cron on meta.account.

The cron re-runs ``action_test_connection`` (patched on the class here) for
each active account. When a token is invalid and there is no open alert
activity yet, it schedules one To-Do and emails the Meta admins. The email
must not contain any token or secret.
"""
from unittest import mock

from odoo.tests.common import TransactionCase, tagged

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
            'group_ids': [(6, 0, [base_internal.id, self.admin_group.id])]})
        self.account = self.Account.create({
            'name': 'Acct', 'account_id': 'ACC1',
            'app_id': 'app_test', 'app_secret': 'secret_test',
            'access_token': 'tok_acct', 'token_valid': True,
        })

    def _activity_count(self, account):
        return self.Activity.search_count([
            ('res_model', '=', 'meta.account'), ('res_id', '=', account.id)])

    def _mail_for(self, account):
        # Match on the alert subject, not just the account name: Odoo 19 also
        # sends an activity-assignment mail whose subject is the record name.
        return self.Mail.search([
            ('subject', 'ilike', 'access token invalid for %s' % account.name)])


@tagged('post_install', '-at_install')
class TestMetaTokenHealth(TokenHealthFixtureMixin, TransactionCase):
    """Alerting, alert dedup and per-account isolation in the token cron."""

    def _flip_dead(self):
        """Side effect for a token that goes from valid to invalid."""
        def _side(self_account):
            self_account.write({'token_valid': False})
            return True
        return _side

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
            'app_id': 'a', 'app_secret': 's', 'access_token': 't',
            'token_valid': True})
        acc_isvalid = self.Account.create({
            'name': 'AcctIsValid', 'account_id': 'ACCIV',
            'app_id': 'a', 'app_secret': 's', 'access_token': 't',
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
        # Odoo 19 renamed res.groups.users to user_ids.
        self.admin_group.user_ids.write(
            {'group_ids': [(3, self.admin_group.id)]})
        self.assertFalse(self.admin_group.user_ids)
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
            'app_id': 'a', 'app_secret': 's', 'access_token': 't',
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
            'app_id': 'a', 'app_secret': 's', 'access_token': 't',
            'token_valid': True})
        acc_ok = self.Account.create({
            'name': 'Handled', 'account_id': 'ACC_OK',
            'app_id': 'a', 'app_secret': 's', 'access_token': 't',
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
