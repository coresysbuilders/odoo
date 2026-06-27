# Meta Lead Ads Integration for Odoo — © 2026 CoreSys Builders. All rights reserved.
# Licensed under the Odoo Proprietary License v1.0 (OPL-1).
# Unauthorized copying, redistribution, or resale of this software, in whole or in
# part, via any medium, is strictly prohibited and constitutes a license violation.
# OPL-1: https://www.odoo.com/documentation/19.0/legal/licenses.html#odoo-apps

"""Tests for the onboarding wizard + token exchange.

These pin the token exchange, token debug, page/form discovery, status mapping,
and the wizard commit/reconcile path.

The mocked-Session harness (_make_response / _patched_session / _load_json /
_FIXTURES) is shared with test_graph_client.py.

Note: assertRaises takes a single exception class, never a tuple.
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
    """Build a fake requests.Response-like object (shared with
    test_graph_client.py)."""
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
        """Patch the client's _get_session to return a Mock whose .request
        yields the given response (or raises side_effect). Shared with
        test_graph_client.py."""
        client = self._client()
        # Patch on the class, not the recordset instance (Odoo 18 rejects setattr
        # of a method on a recordset).
        get_session = mock.patch.object(type(client), '_get_session').start()
        self.addCleanup(mock.patch.stopall)
        session = get_session.return_value
        if side_effect is not None:
            session.request.side_effect = side_effect
        else:
            session.request.return_value = response
        return client, session

    # ==================================================================
    # exchange_token (tokenless on the OAuth endpoint)
    # ==================================================================

    def test_exchange_token_parses_access_token_and_expiry(self):
        client, session = self._patched_session(
            _make_response(200, _load_json('oauth_exchange.json')))
        result = client.exchange_token('APPID', 'SECRET', 'SHORT')
        self.assertEqual(result, ('LONG_LIVED_USER_TOKEN', 5183944))

    def test_exchange_token_sends_no_access_token_param(self):
        # The OAuth endpoint must not receive an injected bearer token.
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
        # A failed exchange raises a single exception class.
        client, session = self._patched_session(
            _make_response(400, _load_json('debug_token_error_190.json')))
        with self.assertRaises(MetaAuthError):
            client.exchange_token('APPID', 'SECRET', 'SHORT')

    # ==================================================================
    # debug_token (valid + both invalid shapes)
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
        # The app-token auth form is access_token='APPID|SECRET',
        # input_token=the token to inspect.
        client, session = self._patched_session(
            _make_response(200, _load_json('debug_token_valid_sut.json')))
        client.debug_token('APPID', 'SECRET', 'INPUT')
        params = session.request.call_args.kwargs['params']
        self.assertEqual(params['input_token'], 'INPUT')
        self.assertEqual(params['access_token'], 'APPID|SECRET')

    def test_debug_token_transport_190_raises_meta_auth(self):
        # Invalid shape (a): transport-level token-death envelope (HTTP 400,
        # top-level error 190).
        client, session = self._patched_session(
            _make_response(400, _load_json('debug_token_error_190.json')))
        with self.assertRaises(MetaAuthError):
            client.debug_token('APPID', 'SECRET', 'BAD')

    def test_debug_token_http200_invalid_body_returned(self):
        # Invalid shape (b): HTTP 200 whose data.is_valid is false with a nested
        # data.error. _handle_response does not raise (it is a 200); debug_token
        # returns the data object as-is. Mapping-to-invalid is asserted in the
        # status test below.
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
        # proves the 2nd page was actually fetched — no truncation.
        self.assertEqual(session.request.call_count, 2)

    def test_discover_pages_second_call_uses_after_cursor_not_next_url(self):
        # The bare-path guard: the 2nd call must page via after=CURSOR, never by
        # feeding paging.next (a full URL) to _request.
        client, session = self._patched_session(side_effect=[
            _make_response(200, _load_json('me_accounts_page1.json')),
            _make_response(200, _load_json('me_accounts_page2.json')),
        ])
        client.discover_pages('USER_TOKEN')
        second_params = session.request.call_args_list[1].kwargs['params']
        self.assertEqual(second_params.get('after'), 'CURSOR_AFTER_1')
        # The 2nd page rides after=CURSOR in params, never paging.next smuggled as
        # the path. session.request ALWAYS gets the fully-built base URL (so '://'
        # is expected and legitimate); the smuggling signature is a query string on
        # the URL itself — there is none, because params ride separately. The hard
        # full-URL/query path-rejection guard lives in
        # test_graph_client.test_request_rejects_full_url_path.
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
        """Feed a debug_token `data` object into the wizard's status mapper."""
        wizard = self.env['meta.onboarding']
        return wizard._map_token_status(_load_json(fixture)['data'])

    def test_status_mapping_lead_retrieval_granted(self):
        # The granted label is 'lead_retrieval_granted', not 'production_ready'.
        status = self._map('debug_token_valid_sut.json')
        self.assertIs(status['token_valid'], True)
        self.assertIn('SYSTEM_USER', status['token_type'])
        self.assertIs(status['leads_retrieval_granted'], True)
        self.assertEqual(status['access_status'], 'lead_retrieval_granted')
        # expires_at 0 = non-expiring -> empty/false.
        self.assertFalse(status['expires_at'])

    def test_status_mapping_dev_test_only(self):
        # A valid token, but leads_retrieval absent -> not production-ready.
        status = self._map('debug_token_dev_no_leads_retrieval.json')
        self.assertIs(status['leads_retrieval_granted'], False)
        self.assertEqual(status['access_status'], 'dev_test_only')

    def test_status_mapping_http200_invalid_is_auth_failed(self):
        # A 200 body with is_valid:false + data.error must never report healthy.
        status = self._map('debug_token_invalid_200.json')
        self.assertIs(status['token_valid'], False)
        self.assertEqual(status['access_status'], 'auth_failed')

    # ==================================================================
    # token never surfaced on a validate error
    # ==================================================================

    def test_token_never_logged_on_validate_error(self):
        # A validate failure raises a token-free UserError: the secret token
        # string must not appear in the message.
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
    # additive reconcile (unselected vs disappeared) + identity
    # ==================================================================

    def _make_account(self, account_id='777000000000001', app_id='100000000000001'):
        return self.env['meta.account'].create({
            'name': 'Acme', 'account_id': account_id, 'app_id': app_id})

    def test_reconcile_is_additive(self):
        # Pages still present in a fresh discovery are kept/created; pages absent
        # from the fresh discovery are deactivated (never unlinked).
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
        # Absent pages are deactivated, not unlinked — the row still exists but
        # is inactive, so the search must bypass the default active_test to see
        # it.
        self.assertTrue(self.env['meta.page'].with_context(
            active_test=False).search_count([
                ('page_id', '=', '555555555555555')]))  # not unlinked

    def test_reconcile_unselected_but_present_stays_active(self):
        # A page the admin merely left unselected but that a fresh discovery
        # still returns must not be deactivated. Only absence from the fresh
        # discovery does.
        acc = self._make_account()
        existing = self.env['meta.page'].create({
            'name': 'Acme Storefront', 'page_id': '111111111111111',
            'account_id': acc.id})
        discovered = _load_json('me_accounts_unselected_present.json')['data']
        wizard = self.env['meta.onboarding'].create({
            'app_id': '100000000000001', 'app_secret': 'SECRET_APP',
            'access_token': 'USER_TOKEN'})
        # selected set deliberately EXCLUDES 111... (unselected) but it is still
        # present in the fresh discovery.
        wizard._reconcile_pages(acc, discovered,
                                selected_ids={'444444444444444'})
        self.assertTrue(existing.active)

    def test_account_identity_uses_token_owner_not_app_id(self):
        # Account identity keys off the token owner (user_id), not the app_id.
        # Two SUT tokens under the same app -> two distinct accounts.
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
        # the app id must NEVER be used as the account identity.
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
