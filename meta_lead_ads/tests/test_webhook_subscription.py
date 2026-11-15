# Meta Lead Ads Integration for Odoo — © 2026 CoreSys Builders. All rights reserved.
# Licensed under the Odoo Proprietary License v1.0 (OPL-1).
# Unauthorized copying, redistribution, or resale of this software, in whole or in
# part, via any medium, is strictly prohibited and constitutes a license violation.
# OPL-1: https://www.odoo.com/documentation/19.0/legal/licenses.html#odoo-apps

"""Tests for page webhook subscription, the wizard's verify token and URL,
and the subscription badge.

Graph calls are mocked by patching ``_request`` on the client class; the
subscribe/status/unsubscribe methods are thin wrappers around it.
"""
from unittest import mock

from odoo.tests.common import TransactionCase, tagged

from odoo.addons.meta_lead_ads.models.exceptions import (
    MetaAuthError, MetaPermanentError, MetaRateLimitError, MetaTransientError,
)

WEBHOOK_PATH = '/meta_lead_ads/webhook'
VERIFY_TOKEN_PARAM = 'meta_lead_ads.webhook_verify_token'


class SubscriptionFixtureMixin:
    """Account and page fixtures plus the graph client class for patching."""

    def setUp(self):
        super().setUp()
        self.account = self.env['meta.account'].create({
            'name': 'Acct', 'account_id': 'ACC1',
            'app_id': 'app_test', 'app_secret': 'secret_test',
            'access_token': 'tok_acct',
        })
        self.page = self.env['meta.page'].create({
            'name': 'Page', 'page_id': 'PG1',
            'access_token': 'tok_test', 'account_id': self.account.id,
        })
        self.client = self.env['meta.graph.client']
        self.ClientClass = type(self.client)


@tagged('post_install', '-at_install')
class TestMetaSubscription(SubscriptionFixtureMixin, TransactionCase):
    """Subscription calls, verify token, webhook URL and badge state."""

    def test_subscribe_args(self):
        """subscribe_page POSTs {page_id}/subscribed_apps for leadgen with the app secret."""
        with mock.patch.object(self.ClientClass, '_request') as m:
            self.client.subscribe_page(self.page)
        self.assertEqual(m.call_count, 1)
        args, kwargs = m.call_args
        # The path is passed positionally, after the token.
        self.assertIn('%s/subscribed_apps' % self.page.page_id, args)
        self.assertEqual(kwargs.get('method'), 'POST')
        self.assertEqual(
            (kwargs.get('params') or {}).get('subscribed_fields'), 'leadgen')
        self.assertEqual(kwargs.get('app_secret'),
                         self.page.account_id.app_secret)

    def test_status_and_unsubscribe(self):
        """Status uses GET and unsubscribe uses DELETE on the same path."""
        path = '%s/subscribed_apps' % self.page.page_id
        with mock.patch.object(self.ClientClass, '_request') as m:
            self.client.page_subscription_status(self.page)
        self.assertIn(path, m.call_args.args)
        self.assertEqual(m.call_args.kwargs.get('method'), 'GET')
        with mock.patch.object(self.ClientClass, '_request') as m2:
            self.client.unsubscribe_page(self.page)
        self.assertIn(path, m2.call_args.args)
        self.assertEqual(m2.call_args.kwargs.get('method'), 'DELETE')

    def test_app_secret_deterministic(self):
        """With several accounts, every one holds the same secret and it is used every time."""
        second = self.env['meta.account'].create({
            'name': 'Acct2', 'account_id': 'ACC2',
            'app_id': 'app_test', 'app_secret': 'secret_two',
            'access_token': 'tok_acct2',
        })
        self.assertGreater(second.id, self.account.id)
        self.account.invalidate_recordset()
        self.assertEqual(self.account.app_secret, 'secret_two')
        self.assertEqual(second.app_secret, 'secret_two')
        secret1 = self.env['meta.account']._webhook_app_secret()
        secret2 = self.env['meta.account']._webhook_app_secret()
        self.assertEqual(secret1, self.account.app_secret)
        self.assertEqual(secret1, secret2)

    def test_verify_token_and_url(self):
        """The verify token is generated once and the URL is web.base.url plus the route."""
        wizard = self.env['meta.onboarding'].create({})
        tok1 = wizard._ensure_verify_token()
        tok2 = wizard._ensure_verify_token()
        self.assertTrue(tok1)
        self.assertEqual(tok1, tok2)
        stored = self.env['ir.config_parameter'].sudo().get_param(
            VERIFY_TOKEN_PARAM)
        self.assertEqual(stored, tok1)
        base = self.env['ir.config_parameter'].sudo().get_param('web.base.url')
        self.assertEqual(wizard._webhook_url(), '%s%s' % (base, WEBHOOK_PATH))

    # ---- subscription badge ---------------------------------------------

    def _status(self, answer=None, side_effect=None):
        """Refresh a new wizard's badge with Meta's answer mocked."""
        wizard = self.env['meta.onboarding'].create({'page_id': self.page.id})
        with mock.patch.object(
                self.ClientClass, 'page_subscription_status',
                return_value=answer, side_effect=side_effect):
            wizard._refresh_subscription_status()
        return wizard

    def test_this_app_with_leadgen_is_subscribed(self):
        wizard = self._status({'data': [
            {'id': 'other', 'subscribed_fields': ['feed']},
            {'id': 'app_test', 'name': 'Mine',
             'subscribed_fields': ['feed', 'leadgen']}]})
        self.assertEqual(wizard.subscription_state, 'subscribed')
        self.assertFalse(wizard.subscription_hint)

    def test_this_app_without_leadgen_is_not_subscribed(self):
        wizard = self._status(
            {'data': [{'id': 'app_test', 'subscribed_fields': ['feed']}]})
        self.assertEqual(wizard.subscription_state, 'not_subscribed')
        self.assertEqual(
            wizard.subscription_hint,
            "This app is subscribed to the Page but not to leadgen, so no "
            "leads arrive. Press Subscribe to add it.")

    def test_other_app_leadgen_is_not_subscribed(self):
        """Another app's leadgen subscription doesn't bring leads to this app."""
        wizard = self._status(
            {'data': [{'id': 'other', 'subscribed_fields': ['leadgen']}]})
        self.assertEqual(wizard.subscription_state, 'other_app')
        self.assertEqual(
            wizard.subscription_hint,
            "Another Meta app gets this Page's leads, but app app_test "
            "doesn't. Press Subscribe to add this app. The other app keeps "
            "its subscription.")

    def test_row_without_id_is_not_this_app(self):
        wizard = self._status({'data': [{'subscribed_fields': ['leadgen']}]})
        self.assertEqual(wizard.subscription_state, 'other_app')

    def test_no_rows_is_not_subscribed(self):
        for answer in ({'data': []}, {}):
            wizard = self._status(answer)
            self.assertEqual(wizard.subscription_state, 'not_subscribed')
            self.assertEqual(
                wizard.subscription_hint,
                "This app isn't subscribed to the Page's leads yet. "
                "Press Subscribe.")

    def test_no_page_is_not_checked(self):
        wizard = self.env['meta.onboarding'].create({})
        with mock.patch.object(
                self.ClientClass, 'page_subscription_status') as m:
            wizard._refresh_subscription_status()
        self.assertFalse(m.called)
        self.assertFalse(wizard.subscription_state)
        self.assertEqual(wizard.subscription_hint,
                         "Press Refresh status to check this Page.")

    def test_status_read_error_is_check_failed(self):
        """A Graph error shows Couldn't check, with none of Meta's text."""
        leaked = 'EAAGleakedtoken123'
        for cls in (MetaAuthError, MetaPermanentError, MetaRateLimitError,
                    MetaTransientError):
            wizard = self._status(side_effect=cls(
                'token %s rejected for secret_test' % leaked))
            self.assertEqual(wizard.subscription_state, 'check_failed', cls)
            self.assertEqual(
                wizard.subscription_hint,
                "Meta didn't answer the status check. The connection is "
                "saved. Press Refresh status to try again.")
            self.assertNotIn(leaked, wizard.subscription_hint)
            self.assertNotIn('secret_test', wizard.subscription_hint)

    def test_malformed_status_response_is_check_failed(self):
        for answer in (None, 'x', {'data': 'x'}, {'data': ['x']},
                       {'data': [{'id': 'app_test',
                                  'subscribed_fields': 'leadgen'}]}):
            wizard = self._status(answer)
            self.assertEqual(wizard.subscription_state, 'check_failed',
                             answer)
            self.assertIn('Press Refresh status', wizard.subscription_hint)

    def test_account_without_app_id_never_subscribed(self):
        # The model won't clear the App ID of an active account, so clear
        # it straight in the table.
        self.env.flush_all()
        self.env.cr.execute(
            "UPDATE meta_account SET app_id = NULL WHERE id = %s",
            (self.account.id,))
        self.env['meta.account'].invalidate_model()
        self.assertFalse(self.page.account_id.app_id)
        for rows in ([{'subscribed_fields': ['leadgen']}],
                     [{'id': '', 'subscribed_fields': ['leadgen']}]):
            wizard = self._status({'data': rows})
            self.assertNotEqual(wizard.subscription_state, 'subscribed')

    def test_refresh_action_reloads_and_never_raises(self):
        wizard = self.env['meta.onboarding'].create({'page_id': self.page.id})
        with mock.patch.object(
                self.ClientClass, 'page_subscription_status',
                side_effect=MetaTransientError('timeout')):
            action = wizard.action_refresh_subscription()
        self.assertEqual(action, wizard._reload())
        self.assertEqual(wizard.subscription_state, 'check_failed')
        with mock.patch.object(
                self.ClientClass, 'page_subscription_status',
                return_value={'data': [{'id': 'app_test',
                                        'subscribed_fields': ['leadgen']}]}):
            wizard.action_refresh_subscription()
        self.assertEqual(wizard.subscription_state, 'subscribed')

    def test_page_change_clears_status(self):
        """Picking another Page clears the badge without asking Meta."""
        wizard = self.env['meta.onboarding'].create({
            'page_id': self.page.id, 'subscription_state': 'subscribed'})
        with mock.patch.object(
                self.ClientClass, 'page_subscription_status') as m:
            wizard._onchange_page_id_subscription()
        self.assertFalse(m.called)
        self.assertFalse(wizard.subscription_state)
        self.assertEqual(wizard.subscription_hint,
                         "Press Refresh status to check this Page.")

    def test_subscribe_refreshes_status(self):
        wizard = self.env['meta.onboarding'].create({'page_id': self.page.id})
        mine = {'data': [{'id': 'app_test', 'subscribed_fields': ['leadgen']}]}
        with mock.patch.object(self.ClientClass, 'subscribe_page') as sub, \
                mock.patch.object(self.ClientClass,
                                  'page_subscription_status',
                                  return_value=mine):
            action = wizard.action_subscribe_page()
        self.assertEqual(sub.call_count, 1)
        self.assertEqual(action, wizard._reload())
        self.assertEqual(wizard.subscription_state, 'subscribed')
        # A failing read after a good subscribe still doesn't raise.
        with mock.patch.object(self.ClientClass, 'subscribe_page'), \
                mock.patch.object(self.ClientClass,
                                  'page_subscription_status',
                                  side_effect=MetaRateLimitError('slow')):
            wizard.action_subscribe_page()
        self.assertEqual(wizard.subscription_state, 'check_failed')

    def test_unsubscribe_refreshes_status(self):
        wizard = self.env['meta.onboarding'].create({
            'page_id': self.page.id, 'subscription_state': 'subscribed'})
        with mock.patch.object(self.ClientClass, 'unsubscribe_page') as uns, \
                mock.patch.object(self.ClientClass,
                                  'page_subscription_status',
                                  return_value={'data': []}):
            wizard.action_unsubscribe_page()
        self.assertEqual(uns.call_count, 1)
        self.assertEqual(wizard.subscription_state, 'not_subscribed')
