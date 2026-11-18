# Meta Lead Ads Integration for Odoo — © 2026 CoreSys Builders. All rights reserved.
# Licensed under the Odoo Proprietary License v1.0 (OPL-1).
# Unauthorized copying, redistribution, or resale of this software, in whole or in
# part, via any medium, is strictly prohibited and constitutes a license violation.
# OPL-1: https://www.odoo.com/documentation/20.0/legal/licenses.html#odoo-apps

"""Tests for the onboarding wizard and token exchange.

Covers token exchange, debug_token, page/form discovery, status mapping and
the wizard's reconcile step. The mocked-Session helpers mirror the ones in
test_graph_client.py.
"""
import datetime
import os
import json
from unittest import mock

from odoo.tests.common import TransactionCase, new_test_user, tagged
from odoo.exceptions import AccessError, UserError, ValidationError

from odoo.addons.meta_lead_ads.models import meta_account as account_model
from odoo.addons.meta_lead_ads.models.exceptions import (
    MetaAuthError, MetaPermanentError, MetaRateLimitError, MetaTransientError,
)
from odoo.addons.meta_lead_ads.models.meta_sync_log import (
    _PAGE_ARCHIVED_MESSAGE)

from .test_ingest import IngestFixtureMixin
from .test_multi_company import MultiCompanyFixtureMixin

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


_DEFAULT_PAGES = (('PG1', 'Page', 'tok_test'), ('PGB', 'Page B', 'tok_b'))
_PG1_ONLY = (('PG1', 'Page', 'tok_test'),)


class OnboardingFlowMixin:
    """Wizard helpers shared by the onboarding classes.

    List it before IngestFixtureMixin, whose own _patch_graph fakes lead
    reads instead of the onboarding calls.
    """

    def _owner_data(self, owner, application='Owner App', app_id='app_test'):
        return {'is_valid': True, 'user_id': owner, 'application': application,
                'app_id': app_id, 'type': 'SYSTEM_USER',
                'scopes': ['leads_retrieval']}

    def _wizard(self, user, companies=None, **vals):
        Wizard = self.env['meta.onboarding'].with_user(user)
        if companies is not None:
            Wizard = Wizard.with_context(allowed_company_ids=companies.ids)
        base = {'app_id': 'app_test', 'app_secret': 'SECRET_APP',
                'access_token': 'USER_TOKEN'}
        base.update(vals)
        return Wizard.create(base)

    def _sudo_account(self, owner, company, **vals):
        base = {'name': 'Existing %s' % owner, 'account_id': owner,
                'app_id': 'app_test', 'company_id': company.id}
        base.update(vals)
        return self.env['meta.account'].sudo().create(base)

    def _assert_plain_user_error(self, func, *args):
        # AccessError subclasses UserError, so check the exact type: a raw
        # record rule failure must not pass as a clean message.
        with self.assertRaises(UserError) as ctx:
            func(*args)
        self.assertIs(type(ctx.exception), UserError)
        return str(ctx.exception)

    def _accounts(self, owner):
        return self.env['meta.account'].sudo().with_context(
            active_test=False).search([('account_id', '=', owner)])

    def _all_forms(self, form_id):
        return self.env['meta.lead.form'].sudo().with_context(
            active_test=False).search([('form_id', '=', form_id)])

    def _commit_wizard(self, user, forms_by_page, wizard_vals=None,
                       pages=_DEFAULT_PAGES, companies=None):
        """Wizard at the review step, one line per (page id, name, token).

        forms_by_page maps a Meta page id to the reviewed (form id, name)
        pairs, as action_discover_forms would have stored them.
        """
        wizard = self._wizard(user, companies=companies, state='forms',
                              **(wizard_vals or {}))
        Line = self.env['meta.onboarding.page'].with_user(user)
        FormLine = self.env['meta.onboarding.form'].with_user(user)
        if companies is not None:
            Line = Line.with_context(allowed_company_ids=companies.ids)
            FormLine = FormLine.with_context(
                allowed_company_ids=companies.ids)
        for pid, name, token in pages:
            line = Line.create({'wizard_id': wizard.id, 'selected': True,
                                'page_id': pid, 'name': name,
                                'access_token': token})
            for fid, fname in forms_by_page.get(pid, []):
                FormLine.create({'wizard_id': wizard.id,
                                 'page_line_id': line.id, 'page_id': pid,
                                 'form_id': fid, 'name': fname})
        return wizard

    def _patch_graph(self, forms_by_page, debug=None, pages=_DEFAULT_PAGES):
        """Fake the onboarding Graph calls; the mocks land in self.graph.

        ``debug`` is merged over a valid answer for self.account's owner.
        """
        answer = {'is_valid': True, 'user_id': self.account.account_id,
                  'application': 'Acct', 'app_id': 'app_test',
                  'type': 'SYSTEM_USER', 'scopes': ['leads_retrieval']}
        answer.update(debug or {})
        listed = [{'id': pid, 'name': name, 'access_token': token}
                  for pid, name, token in pages]

        def discover_forms(page_token, page_id, app_secret=None):
            return [{'id': fid, 'name': name}
                    for fid, name in forms_by_page.get(page_id, [])]

        Client = type(self.env['meta.graph.client'])
        self.graph = {
            'debug_token': mock.patch.object(
                Client, 'debug_token', return_value=answer).start(),
            'exchange_token': mock.patch.object(
                Client, 'exchange_token',
                return_value=('LONG_TOKEN', 0)).start(),
            'discover_pages': mock.patch.object(
                Client, 'discover_pages', return_value=listed).start(),
            'page_subscription_status': mock.patch.object(
                Client, 'page_subscription_status',
                return_value={'data': []}).start(),
            'discover_forms': mock.patch.object(
                Client, 'discover_forms', side_effect=discover_forms).start(),
        }
        self.addCleanup(mock.patch.stopall)
        return self.graph['discover_forms']


@tagged('post_install', '-at_install')
class TestOnboardingCompany(OnboardingFlowMixin, MultiCompanyFixtureMixin,
                            IngestFixtureMixin, TransactionCase):
    """Onboarding when Meta config is split across companies."""

    # ---- account company -------------------------------------------------

    def test_wizard_company_defaults_to_current_company(self):
        wizard = self._wizard(self.admin_a)
        self.assertEqual(wizard.company_id, self.co_a)
        wizard = self._wizard(self.admin_ab, companies=self.co_b + self.co_a)
        self.assertEqual(wizard.company_id, self.co_b)

    def test_new_account_created_in_wizard_company(self):
        wizard = self._wizard(self.admin_ab, companies=self.co_a + self.co_b,
                              company_id=self.co_b.id)
        account = wizard._get_or_create_account(self._owner_data('OWNNEW'))
        self.assertEqual(account.sudo().company_id, self.co_b)
        self.assertEqual(len(self._accounts('OWNNEW')), 1)

    def test_reonboard_keeps_account_company(self):
        existing = self._sudo_account('OWNKEEP', self.co_b)
        wizard = self._wizard(self.admin_ab, companies=self.co_a + self.co_b,
                              company_id=self.co_a.id)
        account = wizard._get_or_create_account(self._owner_data(
            'OWNKEEP', application='Fresh Name'))
        self.assertEqual(account.id, existing.id)
        existing.invalidate_recordset()
        self.assertEqual(existing.company_id, self.co_b)
        self.assertEqual(existing.name, 'Fresh Name')
        self.assertEqual(existing.app_id, 'app_test')
        self.assertEqual(existing.access_status, 'lead_retrieval_granted')

    def test_reonboard_foreign_company_account_clean_error(self):
        existing = self._sudo_account('OWNX', self.co_b)
        wizard = self._wizard(self.admin_a)
        msg = self._assert_plain_user_error(
            wizard._get_or_create_account, self._owner_data('OWNX'))
        self.assertNotIn('Meta Co B', msg)
        self.assertEqual(self._accounts('OWNX'), existing)
        existing.invalidate_recordset()
        self.assertEqual(existing.name, 'Existing OWNX')

    def test_reonboard_member_unselected_company_clean_error(self):
        existing = self._sudo_account('OWNY', self.co_b)
        wizard = self._wizard(self.admin_ab, companies=self.co_a)
        self._assert_plain_user_error(
            wizard._get_or_create_account,
            self._owner_data('OWNY', application='Changed'))
        existing.invalidate_recordset()
        self.assertEqual(existing.name, 'Existing OWNY')
        self.assertEqual(existing.app_id, 'app_test')
        # With the account's company selected the same call goes through.
        wizard = self._wizard(self.admin_ab, companies=self.co_a + self.co_b)
        account = wizard._get_or_create_account(
            self._owner_data('OWNY', application='Changed'))
        self.assertEqual(account.id, existing.id)
        existing.invalidate_recordset()
        self.assertEqual(existing.name, 'Changed')
        self.assertEqual(existing.company_id, self.co_b)

    def test_new_account_in_unselected_company_clean_error(self):
        wizard = self._wizard(self.admin_ab, companies=self.co_a,
                              company_id=self.co_b.id)
        self._assert_plain_user_error(
            wizard._get_or_create_account, self._owner_data('OWNUNSEL'))
        self.assertFalse(self._accounts('OWNUNSEL'))

    def test_new_account_foreign_company_no_name_leak(self):
        # An RPC client can put any company id on the wizard.
        wizard = self._wizard(self.admin_a, company_id=self.co_b.id)
        msg = self._assert_plain_user_error(
            wizard._get_or_create_account, self._owner_data('OWNFOREIGN'))
        self.assertNotIn('Meta Co B', msg)
        self.assertFalse(self._accounts('OWNFOREIGN'))

    # ---- pages -----------------------------------------------------------

    def test_archived_page_reactivated_keeping_settings(self):
        team = self.env['crm.team'].create({'name': 'Z team',
                                            'company_id': self.co_b.id})
        page = self.env['meta.page'].sudo().create({
            'name': 'Old Z', 'page_id': 'PGZ', 'account_id': self.account.id,
            'company_id': self.co_b.id, 'lead_type': 'opportunity',
            'lead_team_id': team.id})
        page.active = False
        wizard = self._wizard(self.admin_a)
        changes = {}
        found = wizard._get_or_create_page(self.account, 'PGZ', 'Z', 'tokz',
                                           changes=changes)
        self.assertEqual(found.id, page.id)
        page.invalidate_recordset()
        self.assertTrue(page.active)
        self.assertEqual(page.name, 'Z')
        self.assertEqual(page.access_token, 'tokz')
        self.assertEqual(page.company_id, self.co_b)
        self.assertEqual(page.lead_type, 'opportunity')
        self.assertEqual(page.lead_team_id, team)
        self.assertEqual(changes, {'pages_reactivated': 1})
        # Found active the second time, so it isn't counted again.
        wizard._get_or_create_page(self.account, 'PGZ', 'Z', 'tokz',
                                   changes=changes)
        self.assertEqual(changes, {'pages_reactivated': 1})

    def test_reonboard_refreshes_overridden_page_metadata(self):
        # PGB belongs to the admin's account but its leads go to company B,
        # which admin_a can't see.
        wizard = self._wizard(self.admin_a)
        page = wizard._get_or_create_page(self.account, 'PGB', 'New name',
                                          'tok2')
        self.assertEqual(page.id, self.page_b.id)
        self.page_b.invalidate_recordset()
        self.assertEqual(self.page_b.name, 'New name')
        self.assertEqual(self.page_b.access_token, 'tok2')
        self.assertEqual(self.page_b.company_id, self.co_b)
        self.assertEqual(len(self.env['meta.page'].sudo().with_context(
            active_test=False).search([('page_id', '=', 'PGB')])), 1)

    # ---- forms -----------------------------------------------------------

    def _co_b_form(self, form_id='FB1'):
        """A form under a Page of a company B account."""
        account_b = self._sudo_account('OWNB', self.co_b)
        page = self.env['meta.page'].sudo().create({
            'name': 'Secret B Page', 'page_id': 'PGFB',
            'account_id': account_b.id})
        return self.env['meta.lead.form'].sudo().create({
            'name': 'B form', 'form_id': form_id, 'page_id': page.id})

    def test_reconcile_forms_rejects_foreign_page_form(self):
        form_b = self._co_b_form('FB1')
        page_b = form_b.page_id
        wizard = self._wizard(self.admin_a)
        msg = self._assert_plain_user_error(
            wizard._reconcile_forms, self.page.with_user(self.admin_a),
            [{'id': 'FB1', 'name': 'x'}])
        self.assertNotIn('Secret B Page', msg)
        self.assertNotIn('Meta Co B', msg)
        form_b.invalidate_recordset()
        self.assertEqual(form_b.page_id, page_b)
        self.assertEqual(form_b.name, 'B form')
        self.assertEqual(self._all_forms('FB1'), form_b)

    def test_reconcile_forms_finds_existing_form_across_rules(self):
        wizard = self._wizard(self.admin_a)
        wizard._reconcile_forms(self.page.with_user(self.admin_a),
                                [{'id': 'F1', 'name': 'Renamed'}])
        self.assertEqual(self._all_forms('F1'), self.form)
        self.form.invalidate_recordset()
        self.assertEqual(self.form.name, 'Renamed')
        self.assertTrue(self.form.active)

    def test_unticked_archived_form_stays_archived(self):
        archived = self.env['meta.lead.form'].sudo().create({
            'name': 'Old FZ', 'form_id': 'FZ', 'page_id': self.page.id})
        archived.active = False
        wizard = self._wizard(self.admin_a)
        changes = {}
        wizard._reconcile_forms(self.page.with_user(self.admin_a),
                                [{'id': 'FZ', 'name': 'New FZ'}],
                                changes=changes)
        archived.invalidate_recordset()
        self.assertFalse(archived.active)
        self.assertEqual(archived.name, 'Old FZ')
        self.assertEqual(self._all_forms('FZ'), archived)
        self.assertNotIn('forms_reactivated', changes)
        # Listed in reactivate_ids it comes back, under the listed name.
        wizard._reconcile_forms(self.page.with_user(self.admin_a),
                                [{'id': 'FZ', 'name': 'New FZ'}],
                                reactivate_ids={'FZ'}, changes=changes)
        archived.invalidate_recordset()
        self.assertTrue(archived.active)
        self.assertEqual(archived.name, 'New FZ')
        self.assertEqual(changes, {'forms_reactivated': 1})

    # ---- full Connect to Meta run ---------------------------------------

    def test_hidden_page_forms_limited_to_fresh_discovery(self):
        discover = self._patch_graph({'PGB': [('FD1', 'Discovered')]})
        wizard = self._commit_wizard(self.admin_a, {
            'PG1': [('F1', 'Contact Us')],
            'PGB': [('FD1', 'Discovered'), ('FAKE', 'Injected')]})
        wizard.action_commit()
        fd1 = self._all_forms('FD1')
        self.assertEqual(len(fd1), 1)
        self.assertEqual(fd1.page_id, self.page_b)
        self.assertFalse(self._all_forms('FAKE'))
        self.assertIn('PGB', [c.args[1] for c in discover.call_args_list])

    def test_hidden_page_archive_and_names_follow_fresh_discovery(self):
        Form = self.env['meta.lead.form'].sudo()
        fy = Form.create({'name': 'Old FY', 'form_id': 'FY',
                          'page_id': self.page_b.id})
        fy2 = Form.create({'name': 'Old FY2', 'form_id': 'FY2',
                           'page_id': self.page_b.id})
        gone = Form.create({'name': 'Old gone', 'form_id': 'FGONE',
                            'page_id': self.page_b.id})
        self._patch_graph({'PGB': [('FY', 'Meta FY'), ('FY2', 'Meta FY2')]})
        # Crafted review lines for PGB, which admin_a can't see: FY2 left
        # out and FY renamed by the client.
        wizard = self._commit_wizard(self.admin_a, {
            'PG1': [('F1', 'Contact Us')],
            'PGB': [('FY', 'Client name')]})
        wizard.action_commit()
        (fy | fy2 | gone).invalidate_recordset()
        self.assertTrue(fy.active)
        self.assertTrue(fy2.active, "Meta still lists it, so it stays active")
        self.assertFalse(gone.active, "Meta no longer lists it")
        self.assertEqual(fy.name, 'Meta FY')
        self.assertEqual(fy2.name, 'Old FY2')
        self.assertEqual((fy | fy2 | gone).page_id, self.page_b)

    def test_action_commit_full_flow_with_overridden_page(self):
        self._patch_graph({'PGB': [('FB2', 'B side form')]})
        wizard = self._commit_wizard(self.admin_a, {
            'PG1': [('F1', 'Contact Us v2'), ('F2', 'New form')],
            'PGB': [('FB2', 'B side form')]})
        wizard.action_commit()
        self.assertEqual(wizard.state, 'done')
        self.assertEqual(wizard.account_id.id, self.account.id)
        self.account.invalidate_recordset()
        self.page_b.invalidate_recordset()
        self.assertEqual(self.account.company_id, self.co_a)
        self.assertEqual(self.page_b.company_id, self.co_b)
        self.assertTrue(self.page_b.active)
        self.form.invalidate_recordset()
        self.assertEqual(self.form.name, 'Contact Us v2')
        f2 = self._all_forms('F2')
        self.assertEqual(f2.page_id, self.page)
        self.assertEqual(f2.create_uid, self.admin_a)
        self.assertEqual(self._all_forms('FB2').page_id, self.page_b)
        self.assertEqual(wizard.page_id, self.page)

    def test_action_commit_crafted_form_line_for_foreign_form(self):
        form_b = self._co_b_form('FBX')
        page_b = form_b.page_id
        self._patch_graph({})
        wizard = self._commit_wizard(self.admin_a, {
            'PG1': [('F1', 'Contact Us'), ('FBX', 'Hijacked')]})
        self._assert_plain_user_error(wizard.action_commit)
        form_b.invalidate_recordset()
        self.assertEqual(form_b.page_id, page_b)
        self.assertEqual(form_b.name, 'B form')
        self.assertTrue(form_b.active)

    def test_finish_survives_failing_status_read(self):
        """A failed status read at the end of Finish keeps what was saved."""
        self._patch_graph({'PGB': [('FB2', 'B side form')]})
        Client = type(self.env['meta.graph.client'])
        mock.patch.object(Client, 'page_subscription_status',
                          side_effect=MetaTransientError('x')).start()
        wizard = self._commit_wizard(self.admin_a, {
            'PG1': [('F1', 'Contact Us'), ('F2', 'New form')],
            'PGB': [('FB2', 'B side form')]})
        wizard.action_commit()
        self.assertEqual(wizard.state, 'done')
        self.assertEqual(wizard.account_id, self.account)
        self.assertEqual(wizard.page_id, self.page)
        self.assertEqual(self._all_forms('F2').page_id, self.page)
        self.assertEqual(self._all_forms('FB2').page_id, self.page_b)
        self.assertTrue(self._all_forms('F1').active)
        self.assertEqual(wizard.subscription_state, 'check_failed')

    def test_page_without_token_gets_a_status_not_a_crash(self):
        page = self.env['meta.page'].sudo().create({
            'name': 'No token', 'page_id': 'PGNOTOK',
            'account_id': self.account.id})
        Client = type(self.env['meta.graph.client'])
        real_status = Client.page_subscription_status
        self._patch_graph({'PG1': [('F1', 'Contact Us')]}, pages=_PG1_ONLY)
        # The real status read, with no way to reach Meta.
        mock.patch.object(Client, 'page_subscription_status',
                          real_status).start()
        mock.patch.object(Client, '_get_session', side_effect=AssertionError(
            'no Graph call expected')).start()
        wizard = self._commit_wizard(
            self.admin_a, {'PG1': [('F1', 'Contact Us')]},
            wizard_vals={'page_id': page.id}, pages=_PG1_ONLY)
        wizard.action_commit()
        self.assertEqual(wizard.state, 'done')
        self.assertEqual(wizard.account_id, self.account)
        self.assertEqual(wizard.subscription_state, 'check_failed')
        self.assertIn('no access token', wizard.subscription_hint)
        wizard.write({'subscription_state': False})
        wizard.action_refresh_subscription()
        self.assertEqual(wizard.subscription_state, 'check_failed')
        for action in (wizard.action_subscribe_page,
                       wizard.action_unsubscribe_page):
            with self.subTest(action=action.__name__):
                msg = self._assert_plain_user_error(action)
                self.assertIn('no access token', msg)


@tagged('post_install', '-at_install')
class TestOnboardingAppLock(OnboardingFlowMixin, IngestFixtureMixin,
                            TransactionCase):
    """One Meta app per database and one App Secret on every account."""

    def setUp(self):
        super().setUp()
        self.Account = self.env['meta.account']
        self.admin = new_test_user(
            self.env, login='applock_admin',
            groups='base.group_user,meta_lead_ads.group_meta_admin')

    def _new(self, account_id, **vals):
        base = {'name': 'Acct %s' % account_id, 'account_id': account_id}
        base.update(vals)
        return self.Account.sudo().create(base)

    def _all(self):
        return self.Account.sudo().with_context(active_test=False).search([])

    def _rows(self):
        # Straight from the table, so a value kept only in the cache can't
        # hide a change that reached the database.
        self.Account.invalidate_model()
        self.env.cr.execute(
            "SELECT id, app_id, app_secret, active, company_id"
            " FROM meta_account ORDER BY id")
        return self.env.cr.fetchall()

    def _secrets(self):
        return {row[0]: row[2] for row in self._rows()}

    def _plant_app(self, account, app_id):
        # Simulates a database that already held two apps before upgrading.
        self.env.flush_all()
        self.env.cr.execute(
            "UPDATE meta_account SET app_id = %s WHERE id = %s",
            (app_id, account.id))
        self.Account.invalidate_model()

    def _refusal(self, func, *args, **kwargs):
        with self.assertRaises(UserError) as ctx:
            func(*args, **kwargs)
        self.assertIs(type(ctx.exception), UserError)
        return str(ctx.exception)

    def _own_data(self, app_id='app_test'):
        return self._owner_data(self.account.account_id, application='Acct',
                                app_id=app_id)

    # ---- one app ------------------------------------------------------

    def test_second_app_refused_on_create(self):
        for Account in (self.Account.with_user(self.admin),
                        self.Account.sudo()):
            with self.subTest(sudo=Account.env.su):
                msg = self._refusal(Account.create, {
                    'name': 'Other', 'account_id': 'OTHER1',
                    'app_id': 'other_app'})
                self.assertIn('app_test', msg)
                self.assertIn('other_app', msg)
                self.assertIn('archive every account of app app_test', msg)
        self.assertFalse(self._all().filtered(
            lambda a: a.account_id == 'OTHER1'))

    def test_caller_without_rights_gets_access_error_not_app_ids(self):
        # The app rule runs before the ORM's own access check, so without
        # its own check a user with no rights would be told which app the
        # database uses.
        for login, groups in (
                ('app_plain', 'base.group_user'),
                ('app_reader', 'base.group_user,meta_lead_ads.group_meta_user'),
                ('app_portal', 'base.group_portal')):
            user = new_test_user(self.env, login=login, groups=groups)
            Account = self.Account.with_user(user)
            with self.subTest(login=login):
                with self.assertRaises(AccessError):
                    Account.create({'name': 'Other', 'account_id': 'NOPE1',
                                    'app_id': 'other_app'})
                with self.assertRaises(AccessError):
                    self.account.with_user(user).write(
                        {'app_id': 'other_app', 'active': True})
                with self.assertRaises(AccessError):
                    self.account.with_user(user).write(
                        {'app_secret': 'secret_nope'})
                # The form save must not reach Meta for such a caller.
                with self._patch_debug() as debug:
                    with self.assertRaises(AccessError):
                        self.account.with_user(user).web_save(
                            {'app_secret': 'secret_nope'}, {})
                    with self.assertRaises(AccessError):
                        Account.web_save(
                            {'app_secret': 'secret_nope', 'name': 'Other',
                             'account_id': 'NOPE1', 'app_id': 'app_test',
                             'access_token': 'tok'}, {})
                    debug.assert_not_called()
        self.assertFalse(self._all().filtered(
            lambda a: a.account_id == 'NOPE1'))

    def test_default_app_id_context_refused_on_create(self):
        Account = self.Account.with_user(self.admin).with_context(
            default_app_id='other_app')
        msg = self._refusal(Account.create, {
            'name': 'Ctx', 'account_id': 'CTX1'})
        self.assertIn('other_app', msg)
        self.assertFalse(self._all().filtered(
            lambda a: a.account_id == 'CTX1'))

    def test_active_account_cannot_change_app(self):
        second = self._new('SAME2', app_id='app_test')
        msg = self._refusal(second.write, {'app_id': 'other_app'})
        self.assertIn('app_test', msg)
        self.assertIn('other_app', msg)
        # Even as the only active account it keeps its app.
        second.write({'active': False})
        msg = self._refusal(self.account.write, {'app_id': 'other_app'})
        self.assertIn('other_app', msg)
        self.account.invalidate_recordset()
        self.assertEqual(self.account.app_id, 'app_test')

    def test_unarchive_of_other_app_account_refused(self):
        old = self._new('OLD1', app_id='old_app', active=False)
        msg = self._refusal(old.action_unarchive)
        self.assertIn("It can't be reactivated while app app_test is in use",
                      msg)
        self.assertIn('Meta app old_app', msg)
        msg = self._refusal(old.write, {'active': True})
        self.assertIn('belongs to Meta app old_app', msg)
        old.invalidate_recordset()
        self.assertFalse(old.active)

    def test_reactivation_with_current_app_allowed(self):
        old = self._new('OLD2', app_id='old_app', active=False)
        page = self.env['meta.page'].sudo().create({
            'name': 'Old page', 'page_id': 'PGOLD', 'account_id': old.id})
        form = self.env['meta.lead.form'].sudo().create({
            'name': 'Old form', 'form_id': 'FOLD', 'page_id': page.id})
        old.write({'active': True, 'app_id': 'app_test'})
        old.invalidate_recordset()
        self.assertTrue(old.active)
        self.assertEqual(old.app_id, 'app_test')
        self.assertEqual(page.account_id, old)
        self.assertEqual(form.page_id, page)
        self.assertIn(page, old.page_ids)

    def test_unarchive_two_apps_in_one_write_refused(self):
        self.account.write({'active': False})
        first = self._new('TWO1', app_id='old_app', active=False)
        second = self._new('TWO2', app_id='third_app', active=False)
        self._refusal((first | second).write, {'active': True})
        (first | second).invalidate_recordset()
        self.assertFalse(first.active)
        self.assertFalse(second.active)

    def test_archived_accounts_do_not_lock(self):
        old = self._new('OLD3', app_id='old_app', active=False)
        # An archived account of another app doesn't block the current one.
        same = self._new('SAME3', app_id='app_test')
        old.write({'name': 'Still archived'})
        # Once every account of the current app is archived, a new app may
        # be connected.
        (self.account | same).write({'active': False})
        fresh = self._new('NEW3', app_id='third_app')
        self.assertTrue(fresh.active)
        self.assertEqual(fresh.app_id, 'third_app')

    def test_empty_app_id_is_neutral(self):
        blank = self._new('EMPTY1')
        empty = self._new('EMPTY2', app_id='')
        spaces = self._new('EMPTY3', app_id='   ')
        self.assertFalse(blank.app_id)
        self.assertFalse(empty.app_id)
        self.assertFalse(spaces.app_id)
        second = self._new('EMPTY4', app_id='app_test')
        second.write({'app_id': False})
        self.assertFalse(second.app_id)
        blank.write({'app_id': 'app_test'})
        self.assertEqual(blank.app_id, 'app_test')

    def test_empty_recordset_check_is_noop(self):
        second = self._new('PLANT1', app_id='app_test')
        self._plant_app(second, 'other_app')
        self.assertIsNone(self.Account.browse()._check_single_app({}))

    def test_noop_write_in_two_app_database_allowed(self):
        second = self._new('PLANT2', app_id='app_test')
        self._plant_app(second, 'other_app')
        self.account.write({'name': 'Renamed'})
        second.write({'name': 'Renamed too'})
        self.account.write({'active': True})
        second.write({'active': True, 'app_id': 'other_app'})
        self.account.write({'token_valid': True,
                            'access_status': 'lead_retrieval_granted'})
        second.write({'active': False})
        second.invalidate_recordset()
        self.assertFalse(second.active)
        self.assertEqual(self.account.name, 'Renamed')

    def test_check_app_id_allowed_helper(self):
        self._refusal(self.Account._check_app_id_allowed, 'other_app')
        for app_id in ('app_test', ' app_test ', '', False):
            with self.subTest(app_id=app_id):
                self.assertIsNone(self.Account._check_app_id_allowed(app_id))

    def test_app_id_whitespace_normalized(self):
        padded = self._new('WS1', app_id=' app_test ')
        self.assertEqual(padded.app_id, 'app_test')
        self._refusal(self._new, 'WS2', app_id=' other_app ')
        self._refusal(padded.write, {'app_id': ' other_app '})
        # A row saved with spaces before the upgrade is the same app.
        self._plant_app(padded, ' app_test ')
        same = self._new('WS3', app_id='app_test')
        self.assertTrue(same.active)
        self.assertEqual(self.Account._normalize_app_id('  '), False)

    def test_refused_write_caught_in_python_leaves_row_unchanged(self):
        self._new('CAUGHT2', app_id='app_test', app_secret='secret_test')
        before = self._rows()
        caught = False
        try:
            self.account.sudo().write({'app_id': 'other_app',
                                       'app_secret': 'secret_x'})
        except UserError:
            caught = True
        self.assertTrue(caught)
        self.assertEqual(self._rows(), before)

    def test_refused_unarchive_caught_in_python_stays_archived(self):
        old = self._new('OLDC', app_id='old_app', active=False)
        before = self._rows()
        caught = False
        try:
            old.write({'active': True})
        except UserError:
            caught = True
        self.assertTrue(caught)
        self.assertEqual(self._rows(), before)
        old.invalidate_recordset()
        self.assertFalse(old.active)

    def test_refusal_names_no_secret(self):
        msgs = [
            self._refusal(self.Account.sudo().create, {
                'name': 'Leak', 'account_id': 'LEAK1', 'app_id': 'other_app',
                'app_secret': 'secret_leak', 'access_token': 'tok_leak'}),
            self._refusal(self.account.write, {'app_id': 'other_app'}),
            self._refusal(self._new('OLDL', app_id='old_app',
                                    active=False).write, {'active': True}),
        ]
        for msg in msgs:
            for value in ('secret_test', 'tok_acct', 'secret_leak',
                          'tok_leak'):
                self.assertNotIn(value, msg)

    # ---- account identity ---------------------------------------------

    def test_same_owner_same_app_reconnect_keeps_account(self):
        count = len(self._all())
        wizard = self.env['meta.onboarding'].create({
            'app_id': 'app_test', 'app_secret': 'secret_test',
            'access_token': 'USER_TOKEN'})
        account = wizard._get_or_create_account(self._own_data())
        self.assertEqual(account.id, self.account.id)
        self.assertEqual(len(self._all()), count)
        self.assertEqual(len(self._all().filtered(
            lambda a: a.account_id == self.account.account_id)), 1)
        self.assertEqual(self.account.app_id, 'app_test')

    def test_refused_reconnect_leaves_credentials_unchanged(self):
        wizard = self.env['meta.onboarding'].create({
            'app_id': 'other_app', 'app_secret': 'secret_other',
            'access_token': 'USER_TOKEN'})
        before = self._rows()
        error = None
        try:
            wizard._get_or_create_account(self._own_data('other_app'))
        except UserError as exc:
            error = exc
        self.assertIs(type(error), UserError)
        self.assertIn('other_app', str(error))
        self.assertEqual(self._rows(), before)
        self.account.invalidate_recordset()
        self.assertEqual(self.account.app_id, 'app_test')
        self.assertEqual(self.account.app_secret, 'secret_test')
        self.assertEqual(self.account.access_token, 'tok_acct')

    # ---- wizard -------------------------------------------------------

    def _plant_other_app(self):
        """An active account of another app, as an old database has it."""
        other = self._new('OTHERAPP', app_id='app_test', active=False)
        self.env.flush_all()
        self.env.cr.execute(
            "UPDATE meta_account SET app_id = 'other_app', active = true"
            " WHERE id = %s", (other.id,))
        self.Account.invalidate_model()
        return other

    def test_wizard_app_id_help_mentions_one_app(self):
        info = self.env['meta.onboarding'].with_user(self.admin).fields_get(
            ['app_id'])
        self.assertIn('One Meta app per database', info['app_id']['help'])

    def test_wizard_refuses_second_app_before_graph(self):
        self._patch_graph({})
        for exchange in (False, True):
            with self.subTest(exchange=exchange):
                wizard = self._wizard(self.admin, app_id='other_app',
                                      exchange_short_lived=exchange)
                msg = self._refusal(wizard.action_validate)
                self.assertIn('connected to Meta app app_test', msg)
                self.assertIn('To use app other_app', msg)
                self.assertEqual(wizard.state, 'credentials')
                self.assertFalse(wizard.token_owner_id)
        for name in ('exchange_token', 'debug_token', 'discover_pages'):
            self.graph[name].assert_not_called()

    def test_wizard_refusal_names_no_secret_or_token(self):
        self._patch_graph({}, debug={'app_id': 'zzz'})
        vals = {'app_secret': 'secret_typed', 'access_token': 'tok_typed'}
        msgs = [
            self._refusal(self._wizard(
                self.admin, app_id='other_app', **vals).action_validate),
            self._refusal(self._wizard(self.admin, **vals).action_validate),
            self._refusal(self._commit_wizard(
                self.admin, {}, wizard_vals=vals,
                pages=_PG1_ONLY).action_commit),
        ]
        self.assertIn('belongs to app zzz, not app app_test', msgs[1])
        self.assertIn('belongs to app zzz, not app app_test', msgs[2])
        for msg in msgs:
            for value in ('secret_typed', 'tok_typed', 'secret_test',
                          'tok_acct', 'tok_test'):
                self.assertNotIn(value, msg)

    def test_validate_refuses_token_of_another_app(self):
        self._patch_graph({}, debug={'app_id': 'zzz'})
        wizard = self._wizard(self.admin)
        msg = self._refusal(wizard.action_validate)
        self.assertEqual(msg, "Meta reports this token belongs to app zzz, "
                              "not app app_test. Check the App ID and try "
                              "again.")
        self.graph['debug_token'].assert_called_once()
        self.graph['discover_pages'].assert_not_called()
        self.assertEqual(wizard.state, 'credentials')
        self.assertFalse(wizard.token_owner_id)
        self.assertFalse(wizard.token_valid)
        self.assertFalse(wizard.access_status)
        self.assertFalse(wizard.page_line_ids)
        # Meta's App ID with spaces, or none at all, is the typed app.
        for app_id in (' app_test ', None):
            with self.subTest(app_id=app_id):
                self.graph['debug_token'].return_value = dict(
                    self._own_data(), app_id=app_id)
                self.graph['discover_pages'].reset_mock()
                wizard = self._wizard(self.admin)
                wizard.action_validate()
                self.graph['discover_pages'].assert_called_once()
                self.assertEqual(wizard.state, 'pages')
                self.assertEqual(wizard.token_owner_id,
                                 self.account.account_id)

    def test_finish_checks_app_before_graph(self):
        self._patch_graph({'PG1': [('F1', 'Contact Us')]})
        wizard = self._commit_wizard(
            self.admin, {'PG1': [('F1', 'Contact Us')]}, pages=_PG1_ONLY)
        # Since Validate, the account was archived and another app came in.
        self.account.write({'active': False})
        self._plant_other_app()
        msg = self._refusal(wizard.action_commit)
        self.assertIn('connected to Meta app other_app', msg)
        self.assertIn('To use app app_test', msg)
        for name in ('debug_token', 'discover_pages', 'discover_forms'):
            self.graph[name].assert_not_called()
        self.assertEqual(wizard.state, 'forms')
        self.account.invalidate_recordset()
        self.assertFalse(self.account.active)
        self.assertEqual(self.account.access_token, 'tok_acct')

    def test_finish_refuses_token_of_another_app(self):
        self._patch_graph({'PG1': [('F1', 'Contact Us')]},
                          debug={'app_id': 'zzz'})
        wizard = self._commit_wizard(
            self.admin, {'PG1': [('F1', 'Renamed')]}, pages=_PG1_ONLY)
        before = self._rows()
        msg = self._refusal(wizard.action_commit)
        self.assertIn('belongs to app zzz, not app app_test', msg)
        self.assertEqual(self._rows(), before)
        self.graph['debug_token'].assert_called_once()
        for name in ('discover_pages', 'discover_forms'):
            self.graph[name].assert_not_called()
        self.assertEqual(wizard.state, 'forms')
        self.account.invalidate_recordset()
        self.form.invalidate_recordset()
        self.assertEqual(self.account.access_token, 'tok_acct')
        self.assertEqual(self.form.name, 'Contact Us')

    def test_wizard_secret_rotation_reaches_every_account(self):
        archived = self._new('ROT1', app_id='app_test',
                             app_secret='secret_test', active=False)
        count = len(self._all())
        self._patch_graph({'PG1': [('F1', 'Contact Us')]})
        wizard = self._commit_wizard(
            self.admin, {'PG1': [('F1', 'Contact Us')]},
            wizard_vals={'app_secret': 'secret_rot'}, pages=_PG1_ONLY)
        wizard.action_commit()
        self.graph['debug_token'].assert_called_once_with(
            'app_test', 'secret_rot', 'USER_TOKEN')
        secrets = self._secrets()
        self.assertEqual(set(secrets.values()), {'secret_rot'})
        self.assertIn(archived.id, secrets)
        self.assertEqual(len(self._all()), count)
        self.assertTrue(wizard.is_update)
        self.assertEqual(wizard.onboarding_summary.split('\n'), [
            'Token replaced.', 'App Secret updated on 2 accounts.'])

    def test_same_app_reconnect_updates_same_account(self):
        count = len(self._all())
        self._patch_graph({'PG1': [('F1', 'Contact Us')]},
                          debug={'application': 'Fresh name'})
        wizard = self._commit_wizard(
            self.admin, {'PG1': [('F1', 'Contact Us')]},
            wizard_vals={'app_secret': 'secret_test',
                         'access_token': 'tok_new'},
            pages=_PG1_ONLY)
        wizard.action_commit()
        self.assertEqual(wizard.state, 'done')
        self.assertEqual(wizard.account_id, self.account)
        self.assertEqual(len(self._all()), count)
        self.account.invalidate_recordset()
        self.assertTrue(self.account.active)
        self.assertEqual(self.account.app_id, 'app_test')
        self.assertEqual(self.account.access_token, 'tok_new')
        self.assertEqual(self.account.name, 'Fresh name')
        self.assertTrue(wizard.is_update)
        self.assertEqual(wizard.onboarding_summary, 'Token replaced.')

    # ---- shared App Secret --------------------------------------------

    def test_secret_write_takes_app_lock(self):
        Model = type(self.Account)
        original = Model._lock_single_app
        with mock.patch.object(Model, '_lock_single_app', autospec=True,
                               side_effect=original) as lock:
            self.account.write({'app_secret': 'secret_new'})
        self.assertGreaterEqual(lock.call_count, 1)
        self.assertEqual(self.account.app_secret, 'secret_new')

    def test_rejected_company_change_keeps_other_secrets(self):
        other = self.env['res.company'].create({'name': 'Other Co'})
        self._new('COMP2', app_id='app_test', app_secret='secret_test')
        company = self.account.company_id
        caught = False
        try:
            self.account.with_user(self.admin).write({
                'company_id': other.id, 'app_secret': 'secret_new'})
        except ValidationError:
            caught = True
        self.assertTrue(caught)
        self.assertEqual(set(self._secrets().values()), {'secret_test'})
        self.account.invalidate_recordset()
        self.assertEqual(self.account.company_id, company)

    def test_secret_rotation_reaches_every_account(self):
        archived = self._new('ARCH1', app_id='app_test',
                             app_secret='secret_test', active=False)
        bare = self._new('BARE1')
        self.assertIsNone(self._secrets()[bare.id])
        self.account.write({'app_secret': 'secret_new'})
        secrets = self._secrets()
        self.assertEqual(set(secrets.values()), {'secret_new'})
        self.assertIn(archived.id, secrets)
        self.assertIn(bare.id, secrets)

    def test_secret_on_create_propagates(self):
        archived = self._new('ARCH2', active=False)
        created = self._new('NEWSEC', app_id='app_test',
                            app_secret='secret_created')
        secrets = self._secrets()
        self.assertEqual(set(secrets.values()), {'secret_created'})
        self.assertIn(archived.id, secrets)
        self.assertIn(created.id, secrets)

    def test_secret_rotation_is_logged_without_the_secret(self):
        self._new('LOG2', app_id='app_test')
        with mock.patch.object(account_model, '_logger') as logger:
            self.account.write({'app_secret': 'secret_logged'})
        logger.info.assert_called_once()
        call = logger.info.call_args
        self.assertEqual(call.args[1:], (1, self.env.uid))
        self.assertNotIn('secret_logged', repr(call))

    def test_empty_secret_never_propagated(self):
        second = self._new('EMP2', app_id='app_test',
                           app_secret='secret_test')
        for blank in (False, ''):
            with self.subTest(blank=blank):
                self.account.write({'app_secret': blank})
                second.invalidate_recordset()
                self.assertEqual(second.app_secret, 'secret_test')
                self.assertEqual(
                    self.Account._propagate_app_secret(blank), 0)
        self._new('EMP3', app_secret='')
        second.invalidate_recordset()
        self.assertEqual(second.app_secret, 'secret_test')

    def test_webhook_secret_is_the_shared_secret(self):
        self._new('WH2', app_id='app_test', app_secret='secret_wh')
        self.assertEqual(self.Account.sudo()._webhook_app_secret(),
                         'secret_wh')
        self.assertEqual(self.account.app_secret, 'secret_wh')

    def test_client_context_cannot_skip_propagation(self):
        archived = self._new('CTXA', app_id='app_test', active=False)
        Admin = self.Account.with_user(self.admin).with_context(
            meta_secret_sync=True)
        self.account.with_env(Admin.env).write({'app_secret': 'secret_new'})
        secrets = self._secrets()
        self.assertEqual(set(secrets.values()), {'secret_new'})
        self.assertIn(archived.id, secrets)
        Admin.create({'name': 'Ctx new', 'account_id': 'CTXNEW',
                      'app_id': 'app_test', 'app_secret': 'secret_newer'})
        self.assertEqual(set(self._secrets().values()), {'secret_newer'})
        self.assertEqual(self.Account.sudo()._webhook_app_secret(),
                         'secret_newer')

    def test_propagation_reaches_fixed_point(self):
        self._new('FP2', app_id='app_test')
        self._new('FP3', active=False)
        self.account.write({'app_secret': 'secret_fp'})
        before = self._rows()
        self.assertEqual(self.account._propagate_app_secret('secret_fp'), 0)
        self.assertEqual(self.Account._propagate_app_secret('secret_fp'), 0)
        self.assertEqual(self._rows(), before)

    def test_app_secret_help_warns_database_wide(self):
        info = self.Account.with_user(self.admin).fields_get(['app_secret'])
        help_text = info['app_secret']['help']
        self.assertIn('Every Meta account in this database', help_text)
        self.assertIn('archived ones included', help_text)

    # ---- secret typed on the account form ------------------------------

    def _patch_debug(self, **kwargs):
        return mock.patch.object(type(self.env['meta.graph.client']),
                                 'debug_token', **kwargs)

    def _form_save(self, vals, account=None):
        account = self.account if account is None else account
        return account.with_user(self.admin).web_save(vals, {})

    def test_form_secret_change_checked_with_meta(self):
        self._new('FORM2', app_id='app_test', app_secret='secret_test')
        with self._patch_debug(return_value={
                'is_valid': True, 'app_id': 'app_test'}) as debug:
            self._form_save({'app_secret': 'secret_new'})
        debug.assert_called_once_with('app_test', 'secret_new', 'tok_acct')
        self.assertEqual(set(self._secrets().values()), {'secret_new'})

    def test_form_secret_rejected_by_meta_saves_nothing(self):
        self._new('FORM3', app_id='app_test', app_secret='secret_test')
        cases = (
            {'side_effect': MetaAuthError('Invalid OAuth access token')},
            {'return_value': {'is_valid': True, 'app_id': 'zzz'}},
        )
        for kwargs in cases:
            with self.subTest(kwargs=kwargs), self._patch_debug(**kwargs):
                msg = self._refusal(self._form_save,
                                    {'app_secret': 'secret_bad'})
                self.assertIn(
                    "Meta didn't accept this App Secret for app app_test",
                    msg)
                self.assertEqual(set(self._secrets().values()),
                                 {'secret_test'})

    def test_form_secret_meta_unreachable_saves_nothing(self):
        for error in (MetaTransientError('timeout'),
                      MetaRateLimitError('throttled'),
                      MetaPermanentError('unknown')):
            with self.subTest(error=type(error).__name__), \
                    self._patch_debug(side_effect=error):
                msg = self._refusal(self._form_save,
                                    {'app_secret': 'secret_bad'})
                self.assertIn("Couldn't reach Meta to check the new App "
                              "Secret", msg)
                self.assertEqual(set(self._secrets().values()),
                                 {'secret_test'})

    def test_form_secret_needs_app_and_token(self):
        Account = self.Account.with_user(self.admin)
        no_token = self._new('NOTOK', app_id='app_test')
        with self._patch_debug() as debug:
            msg = self._refusal(Account.web_save, {
                'name': 'N', 'account_id': 'NEW1', 'app_id': 'app_test',
                'app_secret': 'x'}, {})
            self.assertIn('Add the App ID and access token first', msg)
            self._refusal(self._form_save, {'app_secret': 'x'},
                          account=no_token)
        debug.assert_not_called()
        self.assertFalse(self._all().filtered(
            lambda a: a.account_id == 'NEW1'))
        self.assertEqual(set(self._secrets().values()) - {None},
                         {'secret_test'})

    def test_form_unchanged_secret_skips_meta(self):
        with self._patch_debug() as debug:
            self._form_save({'app_secret': 'secret_test'})
            self._form_save({'name': 'Renamed on the form'})
            self._form_save({'app_secret': False})
        debug.assert_not_called()
        self.account.invalidate_recordset()
        self.assertEqual(self.account.name, 'Renamed on the form')

    def test_plain_write_does_not_call_meta(self):
        self._new('PLAIN2', app_id='app_test', app_secret='secret_test')
        with self._patch_debug() as debug:
            self.account.with_user(self.admin).write(
                {'app_secret': 'secret_plain'})
        debug.assert_not_called()
        self.assertEqual(set(self._secrets().values()), {'secret_plain'})

    def test_form_secret_messages_name_no_secret(self):
        typed = 'secret_typed_value'
        msgs = []
        for kwargs in ({'side_effect': MetaAuthError(
                            'bad %s tok_acct' % typed)},
                       {'side_effect': MetaTransientError(
                            'down %s tok_acct' % typed)},
                       {'return_value': {'app_id': 'zzz'}}):
            with self._patch_debug(**kwargs):
                msgs.append(self._refusal(self._form_save,
                                          {'app_secret': typed}))
        no_token = self._new('NOTOK2', app_id='app_test')
        msgs.append(self._refusal(self._form_save, {'app_secret': typed},
                                  account=no_token))
        for msg in msgs:
            for value in (typed, 'secret_test', 'tok_acct'):
                self.assertNotIn(value, msg)

    # ---- secret saved on another app's account --------------------------

    def test_form_secret_on_archived_old_app_account_refused(self):
        self._new('SAMEOLD', app_id='app_test', app_secret='secret_test')
        old = self._new('OLDSEC', app_id='old_app', active=False,
                        access_token='tok_old')
        before = self._secrets()
        # Meta would accept the old app's own secret, so only the app rule
        # can stop it.
        with self._patch_debug(return_value={
                'is_valid': True, 'app_id': 'old_app'}) as debug:
            msg = self._refusal(self._form_save,
                                {'app_secret': 'secret_old'}, account=old)
        debug.assert_not_called()
        self.assertIn('belongs to Meta app old_app', msg)
        self.assertIn('connected to Meta app app_test', msg)
        self.assertNotIn('secret_old', msg)
        for writer in (old.with_user(self.admin), old.sudo()):
            with self.subTest(sudo=writer.env.su):
                self._refusal(writer.write, {'app_secret': 'secret_old'})
        self.assertEqual(self._secrets(), before)
        self.assertEqual(self.Account.sudo()._webhook_app_secret(),
                         'secret_test')

    def test_secret_in_two_app_database_refused(self):
        second = self._new('PLANTSEC', app_id='app_test',
                           access_token='tok_b')
        self._plant_app(second, 'other_app')
        before = self._secrets()
        for target, app in ((second, 'other_app'), (self.account, 'app_test')):
            with self.subTest(app=app):
                with self._patch_debug(return_value={
                        'is_valid': True, 'app_id': app}) as debug:
                    msg = self._refusal(self._form_save,
                                        {'app_secret': 'secret_b'},
                                        account=target)
                debug.assert_not_called()
                self.assertIn('more than one Meta app', msg)
                self._refusal(target.sudo().write, {'app_secret': 'secret_b'})
        self.assertEqual(self._secrets(), before)

    def test_secret_on_created_other_app_account_refused(self):
        before = self._secrets()
        vals = {'name': 'Old', 'account_id': 'OLDNEW', 'app_id': 'old_app',
                'active': False}
        for Account in (self.Account.with_user(self.admin),
                        self.Account.sudo()):
            with self.subTest(sudo=Account.env.su):
                self._refusal(Account.create,
                              dict(vals, app_secret='secret_old'))
                self._refusal(
                    Account.with_context(default_app_secret='secret_old').create,
                    vals)
        with self._patch_debug(return_value={
                'is_valid': True, 'app_id': 'old_app'}) as debug:
            self._refusal(self.Account.with_user(self.admin).web_save,
                          dict(vals, app_secret='secret_old',
                               access_token='tok_old'), {})
        debug.assert_not_called()
        self.assertFalse(self._accounts('OLDNEW'))
        self.assertEqual(self._secrets(), before)

    def test_secret_writes_that_stay_allowed(self):
        # A rotation on the current app still reaches an old app's account.
        old_kept = self._new('OLDKEPT', app_id='old_app', active=False)
        self.account.write({'app_secret': 'secret_rot'})
        self.assertEqual(self._secrets()[old_kept.id], 'secret_rot')
        # The secret the database already uses changes nothing, wherever
        # it is saved.
        old_kept.write({'app_secret': 'secret_rot'})
        # An account without an App ID.
        bare = self._new('BAREOK')
        bare.write({'app_secret': 'secret_bare'})
        self.assertEqual(set(self._secrets().values()), {'secret_bare'})
        # An archived account of the current app.
        archived = self._new('ARCHOK', app_id='app_test', active=False)
        archived.with_user(self.admin).write({'app_secret': 'secret_arch'})
        self.assertEqual(set(self._secrets().values()), {'secret_arch'})
        # An old-app account brought back on the current app in one write.
        old = self._new('OLDOK', app_id='old_app', active=False)
        old.write({'active': True, 'app_id': 'app_test',
                   'app_secret': 'secret_back'})
        self.assertEqual(set(self._secrets().values()), {'secret_back'})
        # No active account left: the first connect of a new app.
        self._all().write({'active': False})
        self._new('FIRSTOK', app_id='new_app', app_secret='secret_first')
        self.assertEqual(set(self._secrets().values()), {'secret_first'})
        # And an archived account of a database with no active app.
        self._all().write({'active': False})
        self._new('ARCHNEW', app_id='third_app', active=False,
                  app_secret='secret_third')
        self.assertEqual(set(self._secrets().values()), {'secret_third'})


@tagged('post_install', '-at_install')
class TestOnboardingReactivation(OnboardingFlowMixin, MultiCompanyFixtureMixin,
                                 IngestFixtureMixin, TransactionCase):
    """Reconnecting the owner of an existing or archived account."""

    def _finish(self, owner, pages, forms_by_page=None, user=None,
                companies=None, **wizard_vals):
        """Run Finish for ``owner`` and return the wizard on the Done step."""
        forms_by_page = forms_by_page or {}
        self._patch_graph(forms_by_page, debug={'user_id': owner},
                          pages=pages)
        wizard = self._commit_wizard(user or self.admin_a, forms_by_page,
                                     wizard_vals=wizard_vals, pages=pages,
                                     companies=companies)
        wizard.action_commit()
        self.assertEqual(wizard.state, 'done')
        return wizard

    def _lines(self, wizard):
        return (wizard.onboarding_summary or '').split('\n')

    def _old_app_account(self, owner, company, **vals):
        """An account archived under an old app, with a Page, form, mapping."""
        old = self._sudo_account(owner, company, app_id='old_app',
                                 active=False, **vals)
        page = self.env['meta.page'].sudo().create({
            'name': 'Old page', 'page_id': 'PG' + owner,
            'access_token': 'tok_old_page', 'account_id': old.id})
        form = self.env['meta.lead.form'].sudo().create({
            'name': 'Old form', 'form_id': 'F' + owner, 'page_id': page.id})
        crm_field = self.env['ir.model.fields'].search(
            [('model', '=', 'crm.lead'), ('name', '=', 'function')], limit=1)
        mapping = self.env['meta.field.mapping'].sudo().create({
            'form_id': form.id, 'meta_key': 'job_title',
            'crm_field_id': crm_field.id})
        return old, page, form, mapping

    def test_archived_account_reactivated_by_wizard(self):
        archived = self._sudo_account('OWNZ', self.co_a,
                                      access_token='tok_old')
        archived.active = False
        wizard = self._finish('OWNZ', (('PGZ1', 'Z page', 'tok_z'),),
                              {'PGZ1': [('FZ1', 'Z form')]})
        self.assertEqual(self._accounts('OWNZ'), archived)
        archived.invalidate_recordset()
        self.assertTrue(archived.active)
        self.assertEqual(archived.access_token, 'USER_TOKEN')
        self.assertEqual(archived.company_id, self.co_a)
        self.assertEqual(wizard.account_id, archived)
        self.assertEqual(self._all_forms('FZ1').page_id.account_id, archived)
        self.assertTrue(wizard.is_update)
        # The fixture account had another secret, this one had none.
        self.assertEqual(self._lines(wizard), [
            'Archived connection reactivated.', 'Token replaced.',
            'App Secret updated on 2 accounts.'])

    def test_reactivation_runs_company_checks_first(self):
        archived = self._sudo_account('OWNZB', self.co_b)
        archived.active = False
        # admin_a doesn't belong to company B.
        wizard = self._wizard(self.admin_a)
        msg = self._assert_plain_user_error(
            wizard._get_or_create_account, self._owner_data('OWNZB'))
        self.assertIn("company you don't have access to", msg)
        self.assertNotIn('Meta Co B', msg)
        # admin_ab belongs to B but hasn't selected it.
        wizard = self._wizard(self.admin_ab, companies=self.co_a)
        msg = self._assert_plain_user_error(
            wizard._get_or_create_account, self._owner_data('OWNZB'))
        self.assertIn('not selected in the company switcher', msg)
        archived.invalidate_recordset()
        self.assertFalse(archived.active)
        self.assertEqual(archived.name, 'Existing OWNZB')
        # With B selected it comes back.
        wizard = self._wizard(self.admin_ab, companies=self.co_a + self.co_b)
        changes = {}
        account = wizard._get_or_create_account(self._owner_data('OWNZB'),
                                                changes=changes)
        self.assertEqual(account, archived)
        archived.invalidate_recordset()
        self.assertTrue(archived.active)
        self.assertTrue(changes.get('reactivated'))
        self.assertTrue(changes.get('existing'))

    def test_archived_old_app_account_reactivated_with_current_app(self):
        old, page, form, mapping = self._old_app_account(
            'OWNOLD', self.co_a, access_token='tok_old')
        wizard = self._finish('OWNOLD',
                              (('PGOWNOLD', 'Old page', 'tok_new_page'),),
                              {'PGOWNOLD': [('FOWNOLD', 'Old form')]})
        self.assertEqual(self._accounts('OWNOLD'), old)
        for record in (old, page, form, mapping):
            record.invalidate_recordset()
        self.assertTrue(old.active)
        self.assertEqual(old.app_id, 'app_test')
        self.assertEqual(old.access_token, 'USER_TOKEN')
        self.assertEqual(page.account_id, old)
        self.assertTrue(page.active)
        self.assertEqual(form.page_id, page)
        self.assertTrue(form.active)
        self.assertTrue(mapping.exists())
        self.assertEqual(mapping.form_id, form)
        self.assertEqual(wizard.account_id, old)
        lines = self._lines(wizard)
        self.assertEqual(lines[:3], [
            'Archived connection reactivated.',
            'Meta app changed from old_app to app_test.',
            'Token replaced.'])

    def test_reconnecting_an_archived_page_requeues_its_held_leads(self):
        account = self._sudo_account('OWNRQ', self.co_a,
                                     access_token='tok_old')
        page = self.env['meta.page'].sudo().create({
            'name': 'RQ page', 'page_id': 'PGRQ1', 'active': False,
            'access_token': 'tok_rq_old', 'account_id': account.id})
        message = _PAGE_ARCHIVED_MESSAGE
        event = self.env['meta.webhook.event'].sudo().create({
            'leadgen_id': 'LGRQ1', 'page_id': 'PGRQ1', 'status': 'failed',
            'error_message': message})
        row = self.env['meta.sync.log'].sudo()._record_failure(
            'LGRQ1', 'webhook', message, page=page)
        self.assertEqual(row.page_id, page)
        cron = self.env.ref('meta_lead_ads.cron_meta_webhook_drain')
        self.assertTrue(cron.active)
        Trigger = self.env['ir.cron.trigger'].sudo()
        before = Trigger.search_count([('cron_id', '=', cron.id)])
        self._finish('OWNRQ', (('PGRQ1', 'RQ page', 'tok_rq'),),
                     {'PGRQ1': [('FRQ1', 'RQ form')]})
        page.invalidate_recordset()
        event.invalidate_recordset()
        self.assertTrue(page.active)
        self.assertEqual(event.status, 'pending')
        self.assertTrue(event.requeued)
        self.assertGreater(
            Trigger.search_count([('cron_id', '=', cron.id)]), before)

    def test_summary_company_stays(self):
        existing = self._sudo_account('OWNC', self.co_b,
                                      app_secret='SECRET_APP',
                                      access_token='USER_TOKEN')
        wizard = self._finish('OWNC', (('PGC1', 'C page', 'tok_c'),),
                              user=self.admin_ab,
                              companies=self.co_a + self.co_b,
                              company_id=self.co_a.id)
        existing.invalidate_recordset()
        self.assertEqual(existing.company_id, self.co_b)
        self.assertEqual(self._lines(wizard), [
            'Company stays Meta Co B. Change it on the account if leads '
            'should go to another company.'])

    def test_summary_credentials_unchanged(self):
        wizard = self._finish(self.account.account_id, _DEFAULT_PAGES,
                              {'PG1': [('F1', 'Contact Us')]},
                              app_secret='secret_test',
                              access_token='tok_acct')
        self.assertEqual(wizard.account_id, self.account)
        self.assertTrue(wizard.is_update)
        self.assertEqual(
            wizard.onboarding_summary,
            'Token and App Secret unchanged. Token status checked again.')

    def test_summary_never_contains_secrets(self):
        # Old app, company B, new token and secret: most lines at once.
        self._old_app_account('OWNLEAK', self.co_b,
                              access_token='tok_old_stored')
        wizard = self._finish('OWNLEAK',
                              (('PGOWNLEAK', 'Old page', 'tok_page_new'),),
                              user=self.admin_ab,
                              companies=self.co_a + self.co_b,
                              company_id=self.co_a.id,
                              app_secret='secret_typed_value',
                              access_token='tok_typed_value')
        lines = self._lines(wizard)
        self.assertEqual(len(lines), 5)
        for value in ('secret_typed_value', 'tok_typed_value',
                      'tok_old_stored', 'tok_page_new', 'tok_old_page',
                      'secret_test', 'tok_acct'):
            self.assertNotIn(value, wizard.onboarding_summary)

    def test_new_account_shows_connection_saved(self):
        wizard = self._finish('OWNNEW2', (('PGN1', 'New page', 'tok_n'),),
                              {'PGN1': [('FN1', 'New form')]})
        self.assertEqual(wizard.account_id, self._accounts('OWNNEW2'))
        self.assertFalse(wizard.is_update)
        self.assertFalse(wizard.onboarding_summary)

    def test_summary_lines_in_order(self):
        wizard = self._wizard(self.admin_a)
        summary = wizard._onboarding_summary({
            'existing': True, 'reactivated': True,
            'app_changed': ('old_app', 'app_test'), 'token_replaced': True,
            'secret_count': 3, 'pages_reactivated': 2,
            'forms_reactivated': 4, 'company': 'Meta Co B'})
        self.assertEqual(summary.split('\n'), [
            'Archived connection reactivated.',
            'Meta app changed from old_app to app_test.',
            'Token replaced.',
            'App Secret updated on 3 accounts.',
            '2 archived Pages reactivated.',
            '4 archived lead forms reactivated.',
            'Company stays Meta Co B. Change it on the account if leads '
            'should go to another company.'])
        summary = wizard._onboarding_summary({
            'existing': True, 'secret_count': 1, 'pages_reactivated': 1,
            'forms_reactivated': 1})
        self.assertEqual(summary.split('\n'), [
            'App Secret updated on 1 account.',
            '1 archived Page reactivated.',
            '1 archived lead form reactivated.'])
        self.assertFalse(wizard._onboarding_summary({}))
        self.assertFalse(wizard._onboarding_summary({'token_replaced': True}))

    def test_done_alert_has_both_variants(self):
        arch = self.env['meta.onboarding'].with_user(self.admin_a).get_view(
            self.env.ref('meta_lead_ads.meta_onboarding_view_form').id,
            'form')['arch']
        self.assertIn('role="status"', arch)
        self.assertIn('Existing connection updated.', arch)
        self.assertIn('Connection saved.', arch)
        self.assertIn('name="onboarding_summary"', arch)

    def test_helpers_accept_missing_changes(self):
        wizard = self._wizard(self.admin_a)
        account = self.account.with_user(self.admin_a)
        first = wizard._get_or_create_page(account, 'PGH', 'Helper', 'tok_h')
        second = wizard._get_or_create_page(account, 'PGH', 'Helper 2',
                                            'tok_h2', changes=None)
        self.assertEqual(first, second)
        self.assertEqual(second.name, 'Helper 2')
        self.assertEqual(second.account_id, self.account)
        wizard._reconcile_forms(first, [{'id': 'FH1', 'name': 'Form 1'}])
        wizard._reconcile_forms(first, [{'id': 'FH1', 'name': 'Form 1b'},
                                        {'id': 'FH2', 'name': 'Form 2'}],
                                changes=None)
        wizard._reconcile_forms(first, [{'id': 'FH2', 'name': 'Form 2'}],
                                changes={})
        fh1, fh2 = self._all_forms('FH1'), self._all_forms('FH2')
        self.assertEqual(fh1.name, 'Form 1b')
        self.assertFalse(fh1.active, "No longer listed, so archived")
        self.assertTrue(fh2.active)
        self.assertEqual((fh1 | fh2).page_id, first)
        for changes in ({}, None):
            with self.subTest(changes=changes):
                self.assertEqual(wizard._get_or_create_account(
                    self._owner_data(self.account.account_id),
                    changes=changes), self.account)
        self.assertEqual(wizard._get_or_create_account(
            self._owner_data(self.account.account_id)), self.account)

    # ---- archived Pages and forms ----------------------------------------

    def _archived_page(self, pid='PGZ', name='Old Z'):
        page = self.env['meta.page'].sudo().create({
            'name': name, 'page_id': pid, 'account_id': self.account.id,
            'access_token': 'tok_z'})
        page.active = False
        return page

    def _archived_form(self, fid, page=None, name=None):
        form = self.env['meta.lead.form'].sudo().create({
            'name': name or 'Old %s' % fid, 'form_id': fid,
            'page_id': (page or self.page).id})
        form.active = False
        return form

    def _review(self, forms_by_page, listed=None, pages=_PG1_ONLY):
        """Wizard on the review step. Meta lists ``listed`` at Finish.

        ``listed`` defaults to the reviewed forms.
        """
        self._patch_graph(forms_by_page if listed is None else listed,
                          pages=pages)
        return self._commit_wizard(self.admin_a, forms_by_page, pages=pages)

    def _form_line(self, wizard, fid):
        return wizard.form_line_ids.filtered(lambda l: l.form_id == fid)

    def test_discover_marks_archived_pages_unticked(self):
        self._archived_page('PGZ')
        self._patch_graph({}, pages=(('PG1', 'Page', 'tok_test'),
                                     ('PGZ', 'Z page', 'tok_z'),
                                     ('PGNEW', 'New page', 'tok_n')))
        wizard = self._wizard(self.admin_a)
        wizard.action_validate()
        lines = {l.page_id: l for l in wizard.page_line_ids}
        self.assertEqual(set(lines), {'PG1', 'PGZ', 'PGNEW'})
        self.assertTrue(lines['PGZ'].archived)
        self.assertFalse(lines['PGZ'].selected)
        self.assertEqual(lines['PGZ'].line_status, 'archived')
        for pid in ('PG1', 'PGNEW'):
            with self.subTest(pid=pid):
                self.assertFalse(lines[pid].archived)
                self.assertTrue(lines[pid].selected)
                self.assertFalse(lines[pid].line_status)
        self.assertTrue(wizard.has_archived_pages)
        wizard = self._wizard(self.admin_a)
        self.graph['discover_pages'].return_value = [
            {'id': 'PG1', 'name': 'Page', 'access_token': 'tok_test'}]
        wizard.action_validate()
        self.assertFalse(wizard.has_archived_pages)

    def test_pages_of_another_account_are_not_offered(self):
        other = self._sudo_account('OWNOTHER', self.co_a)
        Page = self.env['meta.page'].sudo()
        held = Page.create({'name': 'Other Z', 'page_id': 'PGOZ',
                            'account_id': other.id, 'access_token': 'tok_oz'})
        held.active = False
        live = Page.create({'name': 'Other Y', 'page_id': 'PGOY',
                            'account_id': other.id, 'access_token': 'tok_oy'})
        self._archived_page('PGZ')
        self._patch_graph({}, pages=(('PG1', 'Page', 'tok_test'),
                                     ('PGZ', 'Z page', 'tok_z'),
                                     ('PGOZ', 'Other Z page', 'tok_oz2'),
                                     ('PGOY', 'Other Y page', 'tok_oy2')))
        wizard = self._wizard(self.admin_a)
        wizard.action_validate()
        lines = {l.page_id: l for l in wizard.page_line_ids}
        for pid in ('PGOZ', 'PGOY'):
            with self.subTest(pid=pid):
                self.assertTrue(lines[pid].other_account)
                self.assertFalse(lines[pid].archived)
                self.assertFalse(lines[pid].selected)
                self.assertEqual(lines[pid].line_status, 'other_account')
        # Only the account's own archived Page is offered to bring back.
        self.assertTrue(lines['PGZ'].archived)
        self.assertEqual(lines['PGZ'].line_status, 'archived')
        self.assertTrue(lines['PG1'].selected)
        self.assertFalse(lines['PG1'].line_status)
        self.assertTrue(wizard.has_archived_pages)
        self.assertTrue(wizard.has_other_account_pages)
        # Finish goes through and leaves the other account's Pages alone.
        wizard.action_discover_forms()
        wizard.action_commit()
        self.assertEqual(wizard.state, 'done')
        (held | live).invalidate_recordset()
        self.assertEqual((held | live).account_id, other)
        self.assertFalse(held.active)
        self.assertTrue(live.active)
        self.assertEqual(held.access_token, 'tok_oz')
        # A token owner with no account yet: every Page Odoo holds belongs
        # to another account.
        self.graph['debug_token'].return_value = self._owner_data('OWNFRESH')
        wizard = self._wizard(self.admin_a)
        wizard.action_validate()
        lines = {l.page_id: l for l in wizard.page_line_ids}
        self.assertEqual(
            {pid for pid, line in lines.items() if line.other_account},
            {'PG1', 'PGZ', 'PGOZ', 'PGOY'})
        self.assertFalse(wizard.has_archived_pages)

    def test_page_list_marks_other_account_pages(self):
        arch = self.env['meta.onboarding'].with_user(self.admin_a).get_view(
            self.env.ref('meta_lead_ads.meta_onboarding_view_form').id,
            'form')['arch']
        self.assertIn('Another account', arch)
        self.assertIn('readonly="other_account"', arch)

    def test_discover_marks_archived_forms_unticked(self):
        self._archived_form('FZ')
        self._patch_graph({'PG1': [('F1', 'Contact Us'), ('FZ', 'Z form'),
                                   ('FNEW', 'New form')]}, pages=_PG1_ONLY)
        wizard = self._commit_wizard(self.admin_a, {}, pages=_PG1_ONLY)
        wizard.action_discover_forms()
        self.assertEqual(wizard.state, 'forms')
        lines = {l.form_id: l for l in wizard.form_line_ids}
        self.assertEqual(set(lines), {'F1', 'FZ', 'FNEW'})
        self.assertTrue(lines['FZ'].archived)
        self.assertFalse(lines['FZ'].selected)
        self.assertEqual(lines['FZ'].line_status, 'archived')
        for fid in ('F1', 'FNEW'):
            with self.subTest(fid=fid):
                self.assertFalse(lines[fid].archived)
                self.assertTrue(lines[fid].selected)

    def test_archived_form_reactivated_with_mappings_and_cursor(self):
        form = self.form
        sweep_fields = type(form)._sweep_state_fields
        # A cursor field added later must be added to this snapshot too.
        self.assertEqual(len(sweep_fields), 9)
        Field = self.env['ir.model.fields']
        mappings = self.env['meta.field.mapping'].sudo().create([{
            'form_id': form.id, 'meta_key': key,
            'crm_field_id': Field.search([('model', '=', 'crm.lead'),
                                          ('name', '=', name)], limit=1).id,
        } for key, name in (('job_title', 'function'),
                            ('site', 'website'))])
        mappings[1].active = False
        stamp = datetime.datetime(2026, 3, 1, 8, 30)
        vals = {'sync_enabled': False}
        for n, name in enumerate(sweep_fields, start=1):
            ftype = form._fields[name].type
            if ftype == 'datetime':
                vals[name] = stamp + datetime.timedelta(hours=n)
            elif ftype == 'boolean':
                vals[name] = True
            elif ftype == 'integer':
                vals[name] = 10 + n
            else:
                self.fail('No test value for %s (%s)' % (name, ftype))
        form.sudo().write(vals)
        form.invalidate_recordset()
        before = {name: form[name] for name in sweep_fields}
        AllMappings = self.env['meta.field.mapping'].sudo().with_context(
            active_test=False)
        mapping_state = {m.id: m.active for m in AllMappings.search(
            [('form_id', '=', form.id)])}
        self.assertEqual(len(mapping_state), 2)
        form.sudo().active = False
        wizard = self._review({'PG1': [('F1', 'Contact Us')]})
        wizard.action_commit()
        form.invalidate_recordset()
        self.assertEqual(self._all_forms('F1'), form)
        self.assertTrue(form.active)
        self.assertEqual({name: form[name] for name in sweep_fields}, before)
        self.assertFalse(form.sync_enabled)
        self.assertEqual({m.id: m.active for m in AllMappings.search(
            [('form_id', '=', form.id)])}, mapping_state)
        self.assertIn('1 archived lead form reactivated.',
                      self._lines(wizard))

    def test_ticked_archived_form_missing_from_meta_stays_archived(self):
        fz = self._archived_form('FZ')
        wizard = self._review({'PG1': [('F1', 'Contact Us'), ('FZ', 'Z')]},
                              listed={'PG1': [('F1', 'Contact Us')]})
        self.assertTrue(self._form_line(wizard, 'FZ').selected)
        wizard.action_commit()
        fz.invalidate_recordset()
        self.assertFalse(fz.active)
        self.assertEqual(fz.name, 'Old FZ')
        self.assertTrue(self._all_forms('F1').active)
        self.assertNotIn('1 archived lead form reactivated.',
                         self._lines(wizard))

    def test_unticked_active_form_stays_active(self):
        wizard = self._review({'PG1': [('F1', 'Renamed')]})
        self._form_line(wizard, 'F1').write({'selected': False})
        wizard.action_commit()
        self.form.invalidate_recordset()
        self.assertTrue(self.form.active)
        self.assertEqual(self.form.name, 'Renamed')

    def test_crafted_archived_flag_ignored(self):
        fz = self._archived_form('FZ')
        reviewed = {'PG1': [('F1', 'Contact Us'), ('FZ', 'Z')]}
        wizard = self._review(reviewed)
        # Over RPC the admin can write any line field, readonly or not.
        self._form_line(wizard, 'F1').write({'archived': True,
                                             'selected': False})
        self._form_line(wizard, 'FZ').write({'archived': False,
                                             'selected': False})
        wizard.action_commit()
        (self.form | fz).invalidate_recordset()
        self.assertTrue(self.form.active)
        self.assertFalse(fz.active)
        self.assertEqual(fz.name, 'Old FZ')
        self.assertNotIn('1 archived lead form reactivated.',
                         self._lines(wizard))

    def test_deleted_review_line_keeps_active_form(self):
        wizard = self._review({'PG1': [('F1', 'Client name')]},
                              listed={'PG1': [('F1', 'Meta name')]})
        self._form_line(wizard, 'F1').unlink()
        self.assertFalse(wizard.form_line_ids)
        wizard.action_commit()
        self.form.invalidate_recordset()
        self.assertTrue(self.form.active)
        self.assertEqual(self.form.name, 'Contact Us')
        self.assertIn('PG1', [c.args[1] for c in
                              self.graph['discover_forms'].call_args_list])

    def test_form_meta_no_longer_lists_is_archived(self):
        f2 = self.env['meta.lead.form'].create({
            'name': 'Gone', 'form_id': 'F2', 'page_id': self.page.id})
        wizard = self._review({'PG1': [('F1', 'Contact Us')]})
        wizard.action_commit()
        (self.form | f2).invalidate_recordset()
        self.assertTrue(self.form.active)
        self.assertFalse(f2.active)
        self.assertEqual(self._all_forms('F2'), f2)

    def test_hidden_page_reactivates_only_meta_listed_forms(self):
        # page_b sends its leads to company B, which admin_a can't see.
        self.assertFalse(self.page_b.with_user(self.admin_a).has_access('read'))
        fh1 = self._archived_form('FH1', page=self.page_b)
        fh2 = self._archived_form('FH2', page=self.page_b)
        wizard = self._review(
            {'PG1': [('F1', 'Contact Us')],
             'PGB': [('FH1', 'Client FH1'), ('FH2', 'Client FH2')]},
            listed={'PG1': [('F1', 'Contact Us')],
                    'PGB': [('FH1', 'Meta FH1')]},
            pages=_DEFAULT_PAGES)
        wizard.action_commit()
        (fh1 | fh2).invalidate_recordset()
        self.assertTrue(fh1.active)
        self.assertEqual(fh1.name, 'Meta FH1')
        self.assertFalse(fh2.active)
        self.assertEqual(fh2.name, 'Old FH2')
        self.assertEqual((fh1 | fh2).page_id, self.page_b)
        self.assertIn('1 archived lead form reactivated.',
                      self._lines(wizard))

    def test_summary_counts_reactivated_pages_and_forms(self):
        self.form.sudo().active = False
        self.page.sudo().active = False
        wizard = self._review({'PG1': [('F1', 'Contact Us')]})
        wizard.action_commit()
        self.page.invalidate_recordset()
        self.form.invalidate_recordset()
        self.assertTrue(self.page.active)
        self.assertTrue(self.form.active)
        self.assertEqual(self.page.lead_type, 'lead')
        lines = self._lines(wizard)
        self.assertIn('1 archived Page reactivated.', lines)
        self.assertIn('1 archived lead form reactivated.', lines)
        self.assertLess(lines.index('1 archived Page reactivated.'),
                        lines.index('1 archived lead form reactivated.'))

    def test_unticked_archived_page_stays_archived(self):
        pgz = self._archived_page('PGZ')
        fz = self.env['meta.lead.form'].sudo().create({
            'name': 'Z form', 'form_id': 'FZ', 'page_id': pgz.id})
        pages = (('PG1', 'Page', 'tok_test'), ('PGZ', 'New Z', 'tok_z2'))
        wizard = self._review({'PG1': [('F1', 'Contact Us')]}, pages=pages)
        wizard.page_line_ids.filtered(
            lambda l: l.page_id == 'PGZ').write({'selected': False})
        wizard.action_commit()
        pgz.invalidate_recordset()
        fz.invalidate_recordset()
        self.assertFalse(pgz.active)
        self.assertEqual(pgz.name, 'Old Z')
        self.assertEqual(pgz.access_token, 'tok_z')
        self.assertTrue(fz.active)
        self.assertNotIn('PGZ', [c.args[1] for c in
                                 self.graph['discover_forms'].call_args_list])
        self.assertNotIn('1 archived Page reactivated.', self._lines(wizard))

    def test_archived_old_app_account_with_archived_page_and_form(self):
        # The setup guide sends admins here to bring back an old app's
        # account. Pages and forms archived with it come back when ticked.
        old, page, form, mapping = self._old_app_account('OWNARC', self.co_a)
        page.active = False
        form.active = False
        wizard = self._finish('OWNARC',
                              (('PGOWNARC', 'Old page', 'tok_new_page'),),
                              {'PGOWNARC': [('FOWNARC', 'Old form')]})
        for record in (old, page, form, mapping):
            record.invalidate_recordset()
        self.assertTrue(old.active)
        self.assertEqual(old.app_id, 'app_test')
        self.assertTrue(page.active)
        self.assertEqual(page.account_id, old)
        self.assertTrue(form.active)
        self.assertEqual(mapping.form_id, form)
        self.assertTrue(mapping.active)
        lines = self._lines(wizard)
        self.assertIn('1 archived Page reactivated.', lines)
        self.assertIn('1 archived lead form reactivated.', lines)
