# Meta Lead Ads Integration for Odoo — © 2026 CoreSys Builders. All rights reserved.
# Licensed under the Odoo Proprietary License v1.0 (OPL-1).
# Unauthorized copying, redistribution, or resale of this software, in whole or in
# part, via any medium, is strictly prohibited and constitutes a license violation.
# OPL-1: https://www.odoo.com/documentation/18.0/legal/licenses.html#odoo-apps

"""Tests for the token-health cron on meta.account.

``TestMetaTokenHealth`` exercises ``meta.account._cron_token_health`` /
``_alert_token_dead``. The cron re-checks each active account via the existing
``action_test_connection`` (patched on its CLASS to simulate a token going dead)
and, whenever the post-check token is invalid AND no open token-health activity
exists, raises a single de-duplicated alert: a To-Do ``mail.activity`` plus a
token-free ``mail.mail`` to the ``group_meta_admin`` users.

The cases cover:
  * the empty-admin fallback (schedule the activity to env.uid, never create a
    mail.mail with an empty email_to);
  * pre-existing-invalid alerting (an account that is already dead with no
    valid->invalid transition still alerts exactly once, then dedups on the
    next run via the open activity).

Odoo 18 conventions used throughout:
  1. Patch ``action_test_connection`` on ``type(...)`` (the CLASS), never a
     recordset.
  2. Use ``search_count(...)`` -- the legacy count kwarg is removed.
  3. ``assertRaises`` takes a SINGLE exception class, never a tuple.
"""
from unittest import mock

from odoo.tests.common import TransactionCase, tagged

TODO_XMLID = 'mail.mail_activity_data_todo'
ALERT_SUMMARY = 'Meta token invalid/expired'   # mirrors _alert_token_dead


class TokenHealthFixtureMixin:
    """A meta.account starting token_valid=True + an admin user (in
    group_meta_admin) carrying an email, so the email-recipient assertion has a
    target. The account CLASS is captured for class-patching."""

    def setUp(self):
        super().setUp()
        self.Account = self.env['meta.account']
        self.Activity = self.env['mail.activity']
        self.Mail = self.env['mail.mail']
        self.AccountClass = type(self.Account)
        self.admin_group = self.env.ref('meta_lead_ads.group_meta_admin')
        base_internal = self.env.ref('base.group_user')
        # An admin WITH an email -> a real mail.mail recipient.
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


@tagged('post_install', '-at_install')
class TestMetaTokenHealth(TokenHealthFixtureMixin, TransactionCase):
    """The token-health cron alert + open-activity dedup + the empty-admin
    fallback + pre-existing-invalid alerting."""

    def _flip_dead(self):
        """A class-patch side effect: action_test_connection finds the token dead
        and flips token_valid -> False (the valid->invalid transition)."""
        def _side(self_account):
            self_account.write({'token_valid': False})
            return True
        return _side

    def _stay_dead(self):
        """A class-patch side effect: the token is ALREADY dead and stays dead
        (no transition occurs on this run -- pre-existing-invalid case)."""
        def _side(self_account):
            self_account.write({'token_valid': False})
            return True
        return _side

    def test_alert_on_invalid(self):
        """When action_test_connection flips token_valid True->False and no open
        activity exists, _cron_token_health schedules a To-Do mail.activity on
        the failing account."""
        with mock.patch.object(self.AccountClass, 'action_test_connection',
                               autospec=True, side_effect=self._flip_dead()):
            self.Account._cron_token_health()
        self.assertEqual(self.Activity.search_count([
            ('res_model', '=', 'meta.account'),
            ('res_id', '=', self.account.id)]), 1)

    def test_email_admins(self):
        """The dead-token run creates a mail.mail whose email_to includes the
        group_meta_admin user's email; the subject/body reference the account
        NAME only and contain NO token/secret string."""
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
        """Open-activity dedup: a SECOND _cron_token_health run while the token
        stays invalid AND an open To-Do already exists creates NO additional
        activity and NO additional email -- this guards the broadened
        not-token_valid condition from storming."""
        with mock.patch.object(self.AccountClass, 'action_test_connection',
                               autospec=True, side_effect=self._flip_dead()):
            self.Account._cron_token_health()
            acts_after_first = self._activity_count(self.account)
            mails_after_first = len(self._mail_for(self.account))
            # Second run -- token still invalid, open activity present.
            self.Account._cron_token_health()
            acts_after_second = self._activity_count(self.account)
            mails_after_second = len(self._mail_for(self.account))
        self.assertEqual(acts_after_first, 1)
        self.assertEqual(acts_after_second, acts_after_first)
        self.assertEqual(mails_after_second, mails_after_first)

    def test_190_and_isvalid_false(self):
        """Both a code-190 OAuthException path AND an HTTP-200 is_valid:false
        body resolve to token_valid=False and each triggers exactly one alert.
        Modeled as two distinct accounts so the alert counts stay isolated
        (no shared-state pollution)."""
        acc_190 = self.Account.create({
            'name': 'Acct190', 'account_id': 'ACC190',
            'app_id': 'a', 'app_secret': 's', 'access_token': 't',
            'token_valid': True})
        acc_isvalid = self.Account.create({
            'name': 'AcctIsValid', 'account_id': 'ACCIV',
            'app_id': 'a', 'app_secret': 's', 'access_token': 't',
            'token_valid': True})

        def _both_paths(self_account):
            # Both the 190 OAuthException classification and the HTTP-200
            # is_valid:false body land on the SAME persisted outcome.
            self_account.write({'token_valid': False})
            return True

        # Drop the always-valid seed account from scope so it does not alert.
        self.account.active = False
        with mock.patch.object(self.AccountClass, 'action_test_connection',
                               autospec=True, side_effect=_both_paths):
            self.Account._cron_token_health()
        self.assertEqual(self._activity_count(acc_190), 1)
        self.assertEqual(self._activity_count(acc_isvalid), 1)

    def test_no_admin_fallback(self):
        """Empty-admin case: with NO users in group_meta_admin, a dead-token run
        must STILL schedule the To-Do activity (assigned to the env.uid
        fallback) but must NOT create a mail.mail with an empty email_to.
        Asserts activity == 1 AND mail == 0 for this account (empty-recipient
        guard)."""
        # Empty the admin group: remove every member (incl. our seeded admin).
        self.admin_group.users.write(
            {'groups_id': [(3, self.admin_group.id)]})
        self.assertFalse(self.admin_group.users)
        with mock.patch.object(self.AccountClass, 'action_test_connection',
                               autospec=True, side_effect=self._flip_dead()):
            self.Account._cron_token_health()
        self.assertEqual(self._activity_count(self.account), 1)
        self.assertEqual(len(self._mail_for(self.account)), 0)

    def test_pre_existing_invalid_alerts(self):
        """Pre-existing-invalid case: an account that STARTS token_valid=False
        with NO prior open activity (already dead, no valid->invalid transition
        on this run) must STILL produce EXACTLY ONE alert (activity == 1 AND one
        mail.mail) -- the broadened guard (not token_valid AND no open activity)
        covers it. A SECOND run produces no new alert (the open activity dedups
        it -- reconciles with test_no_realert)."""
        dead = self.Account.create({
            'name': 'AlreadyDead', 'account_id': 'ACC_DEAD',
            'app_id': 'a', 'app_secret': 's', 'access_token': 't',
            'token_valid': False})
        # The always-valid seed account must not alert; scope it out.
        self.account.active = False
        with mock.patch.object(self.AccountClass, 'action_test_connection',
                               autospec=True, side_effect=self._stay_dead()):
            self.Account._cron_token_health()
            self.assertEqual(self._activity_count(dead), 1)
            self.assertEqual(len(self._mail_for(dead)), 1)
            # Second run dedups via the open activity from the first run.
            self.Account._cron_token_health()
            self.assertEqual(self._activity_count(dead), 1)
            self.assertEqual(len(self._mail_for(dead)), 1)

    # ------------------------------------------------------------------ #
    # Per-account savepoint isolation: each account's check is wrapped in
    # its OWN savepoint so a genuinely-unexpected (non-UserError) failure on
    # one account does NOT abort the sweep for the remaining accounts.
    # ------------------------------------------------------------------ #
    def test_account_isolation(self):
        """TWO active accounts. Account #1's action_test_connection raises a
        genuinely-unexpected (NON-UserError) Exception; account #2 is
        invalid-but-handled (token flips False). The sweep must continue PAST
        account #1's failure (per-account savepoint) so account #2 is still
        health-checked and _alert_token_dead reaches it -- an open token-health
        To-Do activity exists on account #2 keyed on _TOKEN_DEAD_SUMMARY.

        The assertion observes ACCOUNT state, not log text; the failure log must
        be token-free (account.id only).

        The always-valid seed account is scoped out so it does not alert."""
        self.account.active = False   # drop the always-valid seed from scope
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
                # Genuinely-unexpected, NOT a UserError -- must be isolated by
                # the per-account savepoint so the sweep continues to acc_ok.
                raise Exception('unexpected boom (no token logged)')
            # acc_ok: token found invalid-but-handled.
            self_account.write({'token_valid': False})
            return True

        with mock.patch.object(self.AccountClass, 'action_test_connection',
                               autospec=True, side_effect=_side):
            self.Account._cron_token_health()
        # The sweep continued past acc_boom and alerted acc_ok exactly once.
        self.assertEqual(self._activity_count(acc_ok), 1)
