# Meta Lead Ads Integration for Odoo — © 2026 CoreSys Builders. All rights reserved.
# Licensed under the Odoo Proprietary License v1.0 (OPL-1).
# Unauthorized copying, redistribution, or resale of this software, in whole or in
# part, via any medium, is strictly prohibited and constitutes a license violation.
# OPL-1: https://www.odoo.com/documentation/18.0/legal/licenses.html#odoo-apps

"""Tests for the onboarding wizard and token exchange.

Covers token exchange, debug_token, page/form discovery, status mapping and
the wizard's reconcile step. The mocked-Session helpers mirror the ones in
test_graph_client.py.
"""
import os
import json
from unittest import mock

from odoo.tests.common import TransactionCase, tagged
from odoo.exceptions import UserError

from odoo.addons.meta_lead_ads.models.exceptions import (
    MetaAuthError, MetaPermanentError, MetaRateLimitError, MetaTransientError,
)

_FIXTURES = os.path.join(os.path.dirname(__file__), 'fixtures')


def _load_json(filename):
    with open(os.path.join(_FIXTURES, filename), encoding='utf-8') as fh:
        return json.load(fh)


def _make_response(status=200, body=None, headers=None, raw_text=None):
    """Build a fake requests.Response-like object."""
    resp = mock.Mock()
    resp.status_code = status
    resp.ok = status < 400
    resp.headers = dict(headers or {})
    if raw_text is not None:
        resp.text = raw_text
        resp.json.side_effect = ValueError('No JSON object could be decoded')
    else:
        resp.text = json.dumps(body) if body is not None else ''
        resp.json.return_value = body
    return resp


@tagged('post_install', '-at_install')
class TestOnboarding(TransactionCase):

    def _client(self):
        return self.env['meta.graph.client']

    def _patched_session(self, response=None, side_effect=None):
        """Patch _get_session so its .request returns `response` or raises
        `side_effect`."""
        client = self._client()
        # Patch the class: Odoo recordsets don't allow setattr of a method.
        get_session = mock.patch.object(type(client), '_get_session').start()
        self.addCleanup(mock.patch.stopall)
        session = get_session.return_value
        if side_effect is not None:
            session.request.side_effect = side_effect
        else:
            session.request.return_value = response
        return client, session

    # ==================================================================
    # exchange_token (the OAuth endpoint takes no access_token)
    # ==================================================================

    def test_exchange_token_parses_access_token_and_expiry(self):
        client, session = self._patched_session(
            _make_response(200, _load_json('oauth_exchange.json')))
        result = client.exchange_token('APPID', 'SECRET', 'SHORT')
        self.assertEqual(result, ('LONG_LIVED_USER_TOKEN', 5183944))

    def test_exchange_token_sends_no_access_token_param(self):
        client, session = self._patched_session(
            _make_response(200, _load_json('oauth_exchange.json')))
        client.exchange_token('APPID', 'SECRET', 'SHORT')
        params = session.request.call_args.kwargs['params']
        self.assertNotIn('access_token', params)
        self.assertEqual(params['fb_exchange_token'], 'SHORT')
        self.assertEqual(params['client_id'], 'APPID')
        self.assertEqual(params['client_secret'], 'SECRET')
        self.assertEqual(params['grant_type'], 'fb_exchange_token')

    def test_exchange_token_auth_failure_raises_meta_auth(self):
        client, session = self._patched_session(
            _make_response(400, _load_json('debug_token_error_190.json')))
        with self.assertRaises(MetaAuthError):
            client.exchange_token('APPID', 'SECRET', 'SHORT')

    # ==================================================================
    # debug_token (valid, plus both invalid shapes)
    # ==================================================================

    def test_debug_token_returns_data_object(self):
        client, session = self._patched_session(
            _make_response(200, _load_json('debug_token_valid_sut.json')))
        result = client.debug_token('APPID', 'SECRET', 'INPUT')
        self.assertIs(result['is_valid'], True)
        self.assertEqual(result['type'], 'SYSTEM_USER')
        self.assertEqual(result['expires_at'], 0)
        self.assertIn('leads_retrieval', result['scopes'])
        self.assertEqual(result['user_id'], '777000000000001')

    def test_debug_token_uses_app_token_form(self):
        # Authenticates with the app token 'APPID|SECRET'; input_token is the
        # token being inspected.
        client, session = self._patched_session(
            _make_response(200, _load_json('debug_token_valid_sut.json')))
        client.debug_token('APPID', 'SECRET', 'INPUT')
        params = session.request.call_args.kwargs['params']
        self.assertEqual(params['input_token'], 'INPUT')
        self.assertEqual(params['access_token'], 'APPID|SECRET')

    def test_debug_token_transport_190_raises_meta_auth(self):
        # HTTP 400 with a top-level error 190.
        client, session = self._patched_session(
            _make_response(400, _load_json('debug_token_error_190.json')))
        with self.assertRaises(MetaAuthError):
            client.debug_token('APPID', 'SECRET', 'BAD')

    def test_debug_token_http200_invalid_body_returned(self):
        # HTTP 200 with data.is_valid false and a nested data.error. Nothing is
        # raised; the data object comes back as-is and the status mapper below
        # turns it into auth_failed.
        client, session = self._patched_session(
            _make_response(200, _load_json('debug_token_invalid_200.json')))
        data = client.debug_token('APPID', 'SECRET', 'BAD')
        self.assertIs(data['is_valid'], False)
        self.assertTrue(data.get('error'))

    # ==================================================================
    # discover_pages pagination
    # ==================================================================

    def test_discover_pages_follows_pagination(self):
        client, session = self._patched_session(side_effect=[
            _make_response(200, _load_json('me_accounts_page1.json')),
            _make_response(200, _load_json('me_accounts_page2.json')),
        ])
        pages = client.discover_pages('USER_TOKEN')
        self.assertEqual(len(pages), 2)
        self.assertEqual([p['id'] for p in pages],
                         ['111111111111111', '333333333333333'])
        self.assertEqual(pages[0]['access_token'], 'PAGE_TOKEN_A')
        self.assertEqual(pages[1]['access_token'], 'PAGE_TOKEN_B')
        # Second page was actually fetched.
        self.assertEqual(session.request.call_count, 2)

    def test_discover_pages_second_call_uses_after_cursor_not_next_url(self):
        # Page two must be fetched with after=CURSOR, not by passing the
        # paging.next URL in as the path.
        client, session = self._patched_session(side_effect=[
            _make_response(200, _load_json('me_accounts_page1.json')),
            _make_response(200, _load_json('me_accounts_page2.json')),
        ])
        client.discover_pages('USER_TOKEN')
        second_params = session.request.call_args_list[1].kwargs['params']
        self.assertEqual(second_params.get('after'), 'CURSOR_AFTER_1')
        # session.request always gets a full base URL, so '://' is fine. A
        # smuggled paging.next would show up as a query string on the URL.
        # The strict path check is in test_graph_client.
        for call in session.request.call_args_list:
            args, kwargs = call
            path_like = (args[1] if len(args) > 1 else kwargs.get('url')) or ''
            self.assertNotIn('?', path_like)

    # ==================================================================
    # discover_forms pagination
    # ==================================================================

    def test_discover_forms_follows_pagination(self):
        client, session = self._patched_session(side_effect=[
            _make_response(200, _load_json('leadgen_forms_page1.json')),
            _make_response(200, _load_json('leadgen_forms_page2.json')),
        ])
        forms = client.discover_forms('PAGE_TOKEN_A', '111111111111111')
        self.assertEqual(len(forms), 2)
        self.assertEqual([f['id'] for f in forms], ['form_aaa', 'form_bbb'])
        self.assertEqual(session.request.call_count, 2)

    # ==================================================================
    # _map_token_status (granted / dev / HTTP-200-invalid)
    # ==================================================================

    def _map(self, fixture):
        """Run a debug_token `data` fixture through the wizard's status mapper."""
        wizard = self.env['meta.onboarding']
        return wizard._map_token_status(_load_json(fixture)['data'])

    def test_status_mapping_lead_retrieval_granted(self):
        status = self._map('debug_token_valid_sut.json')
        self.assertIs(status['token_valid'], True)
        self.assertIn('SYSTEM_USER', status['token_type'])
        self.assertIs(status['leads_retrieval_granted'], True)
        self.assertEqual(status['access_status'], 'lead_retrieval_granted')
        # expires_at 0 means the token never expires.
        self.assertFalse(status['expires_at'])

    def test_status_mapping_dev_test_only(self):
        # Valid token without leads_retrieval: test leads only.
        status = self._map('debug_token_dev_no_leads_retrieval.json')
        self.assertIs(status['leads_retrieval_granted'], False)
        self.assertEqual(status['access_status'], 'dev_test_only')

    def test_status_mapping_http200_invalid_is_auth_failed(self):
        # A 200 body with is_valid false must not read as healthy.
        status = self._map('debug_token_invalid_200.json')
        self.assertIs(status['token_valid'], False)
        self.assertEqual(status['access_status'], 'auth_failed')

    # ==================================================================
    # token never surfaced on a validate error
    # ==================================================================

    def test_token_never_logged_on_validate_error(self):
        # The UserError raised on a failed validate must not contain the token.
        secret = 'EAALsecretTOKEN_must_not_leak_ZZZ'
        wizard = self.env['meta.onboarding'].create({
            'app_id': '100000000000001',
            'app_secret': 'SECRET_APP',
            'access_token': secret,
        })
        with mock.patch.object(
                type(self.env['meta.graph.client']), 'debug_token',
                side_effect=MetaAuthError('boom')):
            with self.assertRaises(UserError) as ctx:
                wizard.action_validate()
        self.assertNotIn(secret, str(ctx.exception))

    # ==================================================================
    # reconcile (unselected vs disappeared) and account identity
    # ==================================================================

    def _make_account(self, account_id='777000000000001', app_id='100000000000001'):
        return self.env['meta.account'].create({
            'name': 'Acme', 'account_id': account_id, 'app_id': app_id})

    def test_reconcile_is_additive(self):
        # Pages still discovered are kept or created; pages no longer
        # discovered are archived, not deleted.
        acc = self._make_account()
        self.env['meta.page'].create({
            'name': 'Acme Storefront', 'page_id': '111111111111111',
            'account_id': acc.id})
        gone = self.env['meta.page'].create({
            'name': 'Acme Gone', 'page_id': '555555555555555',
            'account_id': acc.id})
        wizard = self.env['meta.onboarding'].create({
            'app_id': '100000000000001', 'app_secret': 'SECRET_APP',
            'access_token': 'USER_TOKEN'})
        discovered = (_load_json('me_accounts_page1.json')['data']
                      + _load_json('me_accounts_page2.json')['data'])
        with mock.patch.object(
                type(self.env['meta.graph.client']), 'discover_pages',
                return_value=discovered):
            wizard._reconcile_pages(acc, discovered, selected_ids={
                '111111111111111', '333333333333333'})
        kept = self.env['meta.page'].search([('page_id', '=', '111111111111111')])
        self.assertEqual(len(kept), 1)             # not duplicated
        self.assertTrue(self.env['meta.page'].search([
            ('page_id', '=', '333333333333333')]))  # new page created
        self.assertFalse(gone.active)               # absent -> active=False
        # Archived rows need active_test=False to be found.
        self.assertTrue(self.env['meta.page'].with_context(
            active_test=False).search_count([
                ('page_id', '=', '555555555555555')]))  # not unlinked

    def test_reconcile_unselected_but_present_stays_active(self):
        # Leaving a page unselected doesn't archive it; only disappearing from
        # discovery does.
        acc = self._make_account()
        existing = self.env['meta.page'].create({
            'name': 'Acme Storefront', 'page_id': '111111111111111',
            'account_id': acc.id})
        discovered = _load_json('me_accounts_unselected_present.json')['data']
        wizard = self.env['meta.onboarding'].create({
            'app_id': '100000000000001', 'app_secret': 'SECRET_APP',
            'access_token': 'USER_TOKEN'})
        # 111... is left out of the selection but is still discovered.
        wizard._reconcile_pages(acc, discovered,
                                selected_ids={'444444444444444'})
        self.assertTrue(existing.active)

    def test_account_identity_uses_token_owner_not_app_id(self):
        # The account is keyed on the token owner (user_id), not app_id, so two
        # system user tokens under one app give two accounts.
        owner1 = _load_json('debug_token_valid_sut.json')['data']
        owner2 = _load_json('debug_token_valid_sut_owner2.json')['data']
        wizard = self.env['meta.onboarding']
        acc1 = wizard._get_or_create_account(owner1)
        acc2 = wizard._get_or_create_account(owner2)
        self.assertNotEqual(acc1.id, acc2.id)
        self.assertEqual(acc1.account_id, '777000000000001')
        self.assertEqual(acc2.account_id, '777000000000002')
        self.assertEqual(acc1.app_id, '100000000000001')
        self.assertEqual(acc2.app_id, '100000000000001')
        self.assertFalse(self.env['meta.account'].search([
            ('account_id', '=', '100000000000001')]))

    # ==================================================================
    # imported forms default to sync_enabled=True
    # ==================================================================

    def test_imported_forms_default_sync_enabled(self):
        acc = self._make_account()
        page = self.env['meta.page'].create({
            'name': 'Acme Storefront', 'page_id': '111111111111111',
            'account_id': acc.id})
        discovered_forms = (_load_json('leadgen_forms_page1.json')['data']
                            + _load_json('leadgen_forms_page2.json')['data'])
        wizard = self.env['meta.onboarding'].create({
            'app_id': '100000000000001', 'app_secret': 'SECRET_APP',
            'access_token': 'USER_TOKEN'})
        with mock.patch.object(
                type(self.env['meta.graph.client']), 'discover_forms',
                return_value=discovered_forms):
            wizard._reconcile_forms(page, discovered_forms)
        forms = self.env['meta.lead.form'].search([
            ('form_id', 'in', ['form_aaa', 'form_bbb'])])
        self.assertEqual(len(forms), 2)
        for form in forms:
            self.assertTrue(form.sync_enabled)
