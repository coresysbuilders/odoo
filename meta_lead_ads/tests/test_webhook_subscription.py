# Meta Lead Ads Integration for Odoo — © 2026 CoreSys Builders. All rights reserved.
# Licensed under the Odoo Proprietary License v1.0 (OPL-1).
# Unauthorized copying, redistribution, or resale of this software, in whole or in
# part, via any medium, is strictly prohibited and constitutes a license violation.
# OPL-1: https://www.odoo.com/documentation/18.0/legal/licenses.html#odoo-apps

"""Tests for page webhook subscription, the wizard's verify token and URL,
and the subscription badge.

Graph calls are mocked by patching ``_request`` on the client class; the
subscribe/status/unsubscribe methods are thin wrappers around it.
"""
from unittest import mock

from odoo.tests.common import TransactionCase, tagged

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
        """With several accounts, the lowest-id account's secret is used every time."""
        second = self.env['meta.account'].create({
            'name': 'Acct2', 'account_id': 'ACC2',
            'app_id': 'app_test2', 'app_secret': 'secret_two',
            'access_token': 'tok_acct2',
        })
        self.assertGreater(second.id, self.account.id)
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

    def test_badge_state_mapping(self):
        """The badge shows subscribed only when leadgen is in subscribed_fields."""
        wizard = self.env['meta.onboarding'].create({'page_id': self.page.id})
        with mock.patch.object(
                self.ClientClass, 'page_subscription_status',
                return_value={'data': [{'subscribed_fields': ['leadgen']}]}):
            wizard._refresh_subscription_status()
        self.assertEqual(wizard.subscription_state, 'subscribed')
        with mock.patch.object(
                self.ClientClass, 'page_subscription_status',
                return_value={'data': []}):
            wizard._refresh_subscription_status()
        self.assertEqual(wizard.subscription_state, 'not_subscribed')
