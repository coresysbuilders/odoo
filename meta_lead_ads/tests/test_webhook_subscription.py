# Meta Lead Ads Integration for Odoo — © 2026 CoreSys Builders. All rights reserved.
# Licensed under the Odoo Proprietary License v1.0 (OPL-1).
# Unauthorized copying, redistribution, or resale of this software, in whole or in
# part, via any medium, is strictly prohibited and constitutes a license violation.
# OPL-1: https://www.odoo.com/documentation/18.0/legal/licenses.html#odoo-apps

"""Tests for the page subscription edge + the wizard verify_token/URL surface +
the badge state mapping.

All Graph traffic is mocked: ``_request`` is patched on ``type(client)`` (the
class) and call args are asserted -- the three subscription methods are thin
``_request`` wrappers (the only place a Graph URL is built). Covers:

  * ``test_subscribe_args`` / ``test_status_and_unsubscribe`` -- subscribe POST /
    status GET / unsubscribe DELETE route through ``_request`` against
    ``'%s/subscribed_apps' % page.page_id`` with ``subscribed_fields='leadgen'``
    + ``app_secret=page.account_id.app_secret``.
  * ``test_app_secret_deterministic`` --
    ``meta.account._webhook_app_secret()`` returns the lowest-id account's secret
    (deterministic ``search([], order='id asc', limit=1)``), stable across calls.
  * ``test_verify_token_and_url`` -- the verify_token is generated once and
    stable across calls; the webhook URL == ``web.base.url`` + the route.
  * ``test_badge_state_mapping`` -- the wizard's
    ``_refresh_subscription_status`` maps a mocked ``page_subscription_status``
    ``data[]`` to ``subscription_state`` (subscribed when the leadgen field is
    present, not_subscribed otherwise). Token-free automated guard for the badge.
"""
from unittest import mock

from odoo.tests.common import TransactionCase, tagged

WEBHOOK_PATH = '/meta_lead_ads/webhook'
VERIFY_TOKEN_PARAM = 'meta_lead_ads.webhook_verify_token'


class SubscriptionFixtureMixin:
    """meta.account -> meta.page chain + the graph-client class for patching
    ``_request``."""

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
    """Subscription edge + verify_token/URL + deterministic secret + badge
    mapping."""

    def test_subscribe_args(self):
        """subscribe_page builds POST {page_id}/subscribed_apps with
        subscribed_fields='leadgen' + app_secret=page.account_id.app_secret,
        routed through _request."""
        with mock.patch.object(self.ClientClass, '_request') as m:
            self.client.subscribe_page(self.page)
        self.assertEqual(m.call_count, 1)
        args, kwargs = m.call_args
        # path is the bare '<page_id>/subscribed_apps' (positional after token).
        self.assertIn('%s/subscribed_apps' % self.page.page_id, args)
        self.assertEqual(kwargs.get('method'), 'POST')
        self.assertEqual(
            (kwargs.get('params') or {}).get('subscribed_fields'), 'leadgen')
        self.assertEqual(kwargs.get('app_secret'),
                         self.page.account_id.app_secret)

    def test_status_and_unsubscribe(self):
        """page_subscription_status -> GET; unsubscribe_page -> DELETE; both
        against the same '<page_id>/subscribed_apps' path via _request."""
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
        """With more than one meta.account, _webhook_app_secret() returns the
        lowest-id account's secret (search([], order='id asc', limit=1)) and is
        stable across calls."""
        second = self.env['meta.account'].create({
            'name': 'Acct2', 'account_id': 'ACC2',
            'app_id': 'app_test2', 'app_secret': 'secret_two',
            'access_token': 'tok_acct2',
        })
        self.assertGreater(second.id, self.account.id)
        secret1 = self.env['meta.account']._webhook_app_secret()
        secret2 = self.env['meta.account']._webhook_app_secret()
        self.assertEqual(secret1, self.account.app_secret)   # lowest-id wins
        self.assertEqual(secret1, secret2)                   # stable

    def test_verify_token_and_url(self):
        """The verify_token getter generates a value once and returns the same
        value on a second call (stable); the webhook URL == web.base.url + the
        route."""
        wizard = self.env['meta.onboarding'].create({})
        tok1 = wizard._ensure_verify_token()
        tok2 = wizard._ensure_verify_token()
        self.assertTrue(tok1)
        self.assertEqual(tok1, tok2)
        # the stored config param matches the generated token.
        stored = self.env['ir.config_parameter'].sudo().get_param(
            VERIFY_TOKEN_PARAM)
        self.assertEqual(stored, tok1)
        # the webhook URL == web.base.url + route.
        base = self.env['ir.config_parameter'].sudo().get_param('web.base.url')
        self.assertEqual(wizard._webhook_url(), '%s%s' % (base, WEBHOOK_PATH))

    def test_badge_state_mapping(self):
        """_refresh_subscription_status maps a mocked page_subscription_status
        data[] to subscription_state -> 'subscribed' when the leadgen field is
        present, 'not_subscribed' otherwise. Token-free automated guard for the
        badge (live Subscribe stays a human check)."""
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
