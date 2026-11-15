# Meta Lead Ads Integration for Odoo — © 2026 CoreSys Builders. All rights reserved.
# Licensed under the Odoo Proprietary License v1.0 (OPL-1).
# Unauthorized copying, redistribution, or resale of this software, in whole or in
# part, via any medium, is strictly prohibited and constitutes a license violation.
# OPL-1: https://www.odoo.com/documentation/19.0/legal/licenses.html#odoo-apps

"""Company ownership and isolation of Meta configuration and logs."""
import json
from unittest import mock

from psycopg2 import IntegrityError

from odoo import Command
from odoo.exceptions import AccessError, ValidationError
from odoo.tests.common import TransactionCase, new_test_user, tagged
from odoo.tools import mute_logger

from odoo.addons.meta_lead_ads.models.exceptions import MetaPermanentError

from .test_ingest import IngestFixtureMixin


class MultiCompanyFixtureMixin:
    """Two companies, per-company Meta Admins and a Page routed to company B.

    List this mixin first in the bases: its setUp runs the other fixtures
    first, so an account they create is reused here.
    """

    def setUp(self):
        super().setUp()
        self.co_a = self.env.company
        self.co_b = self.env['res.company'].create({'name': 'Meta Co B'})
        admin_groups = 'base.group_user,meta_lead_ads.group_meta_admin'
        self.admin_a = new_test_user(
            self.env, 'mc_admin_a', groups=admin_groups,
            company_id=self.co_a.id, company_ids=[Command.set([self.co_a.id])])
        self.admin_b = new_test_user(
            self.env, 'mc_admin_b', groups=admin_groups,
            company_id=self.co_b.id, company_ids=[Command.set([self.co_b.id])])
        self.admin_ab = new_test_user(
            self.env, 'mc_admin_ab', groups=admin_groups,
            company_id=self.co_a.id,
            company_ids=[Command.set([self.co_a.id, self.co_b.id])])
        self.sales_a = new_test_user(
            self.env, 'mc_sales_a',
            groups='base.group_user,sales_team.group_sale_salesman_all_leads',
            company_id=self.co_a.id, company_ids=[Command.set([self.co_a.id])])
        self.sales_mgr_a = new_test_user(
            self.env, 'mc_sales_mgr_a',
            groups='base.group_user,sales_team.group_sale_manager',
            company_id=self.co_a.id, company_ids=[Command.set([self.co_a.id])])
        if not getattr(self, 'account', None):
            self.account = self.env['meta.account'].create({
                'name': 'MC Acct', 'account_id': 'MCACC',
                'app_id': 'app_test', 'app_secret': 'secret_test',
                'access_token': 'tok_acct', 'company_id': self.co_a.id,
            })
        self.page_b = self.env['meta.page'].create({
            'name': 'Page B', 'page_id': 'PGB', 'access_token': 'tok_b',
            'account_id': self.account.id, 'company_id': self.co_b.id,
        })


@tagged('post_install', '-at_install')
class TestMultiCompany(MultiCompanyFixtureMixin, IngestFixtureMixin,
                       TransactionCase):

    def _payload(self, leadgen_id, page_id='PG1', form_id='F1'):
        return {'object': 'page', 'entry': [{
            'id': page_id,
            'changes': [{'field': 'leadgen', 'value': {
                'leadgen_id': leadgen_id, 'page_id': page_id,
                'form_id': form_id}}],
        }]}

    def _queue(self, leadgen_id, page_id='PG1'):
        payload = self._payload(leadgen_id, page_id)
        Event = self.env['meta.webhook.event'].sudo()
        Event._ingest_payload(payload, raw=json.dumps(payload).encode())
        return Event.search([('leadgen_id', '=', leadgen_id)])

    def _lead(self, company, name='MC lead'):
        return self.env['crm.lead'].create({
            'name': name, 'type': 'lead', 'company_id': company.id})

    def _move_lead(self, lead, company):
        # Clear the assignee and team so crm's own company coherence check
        # doesn't object to the move.
        lead.write({'company_id': company.id, 'user_id': False,
                    'team_id': False})

    # ---- company fields --------------------------------------------------

    def test_account_company_defaults_and_required(self):
        account = self.env['meta.account'].create({
            'name': 'Default Co', 'account_id': 'ACCDEF'})
        self.assertEqual(account.company_id, self.env.company)
        try:
            with mute_logger('odoo.sql_db'), self.env.cr.savepoint():
                self.env['meta.account'].create({
                    'name': 'No Co', 'account_id': 'ACCNONE',
                    'company_id': False})
                self.env.flush_all()
        except (ValidationError, IntegrityError):
            pass
        else:
            self.fail("an account without a company was accepted")

    def test_page_lead_company_resolution(self):
        self.assertEqual(self.account.company_id, self.co_a)
        self.assertFalse(self.page.company_id)
        self.assertEqual(self.page.lead_company_id, self.co_a)
        self.assertEqual(self.page_b.company_id, self.co_b)
        self.assertEqual(self.page_b.lead_company_id, self.co_b)

    def test_account_company_change_keeps_override(self):
        co_c = self.env['res.company'].create({'name': 'Meta Co C'})
        self.page_b.company_id = co_c
        self.account.company_id = self.co_b
        self.assertEqual(self.page.lead_company_id, self.co_b)
        self.assertEqual(self.page_b.company_id, co_c)
        self.assertEqual(self.page_b.lead_company_id, co_c)

    def test_form_company_follows_page(self):
        self.assertEqual(self.form.company_id, self.co_a)
        self.page.company_id = self.co_b
        self.assertEqual(self.form.company_id, self.co_b)
        self.page.company_id = False
        self.assertEqual(self.form.company_id, self.co_a)

    def test_sync_log_company_follows_lead_then_page(self):
        Log = self.env['meta.sync.log']
        by_page = Log._record('LGP', 'cron', 'failed', page=self.page_b)
        self.assertEqual(by_page.company_id, self.co_b)
        lead = self._lead(self.co_a)
        by_lead = Log._record('LGL', 'manual', 'success', lead=lead)
        self.assertEqual(by_lead.company_id, self.co_a)
        blank = Log._record('LGN', 'cron', 'failed')
        self.assertFalse(blank.company_id)
        self._move_lead(lead, self.co_b)
        self.assertEqual(by_lead.company_id, self.co_b)

    def test_sync_log_lead_deleted_falls_back_to_page(self):
        lead = self._lead(self.co_a)
        log = self.env['meta.sync.log']._record(
            'LGD', 'manual', 'success', lead=lead, page=self.page_b)
        self.assertEqual(log.company_id, self.co_a)
        lead.unlink()
        self.env.flush_all()
        log.invalidate_recordset()
        self.assertFalse(log.lead_id)
        self.assertEqual(log.company_id, self.co_b)

    def test_webhook_event_company_lifecycle(self):
        event = self._queue('LG1')
        self.assertEqual(event.page_company_id, self.co_a)
        self.assertEqual(event.company_id, self.co_a)
        unknown = self._queue('LGU', page_id='PG_UNKNOWN')
        self.assertFalse(unknown.page_company_id)
        self.assertFalse(unknown.company_id)

        with self._patch_graph():
            event._process_one()
        lead = event.lead_id
        self.assertEqual(event.status, 'done')
        self.assertTrue(lead)
        self.assertEqual(event.company_id,
                         lead.company_id or event.page_company_id)

        self._move_lead(lead, self.co_b)
        self.assertEqual(event.company_id, self.co_b)

        lead.unlink()
        self.env.flush_all()
        event.invalidate_recordset()
        self.assertFalse(event.lead_id)
        self.assertEqual(event.company_id, event.page_company_id)
        self.assertEqual(event.company_id, self.co_a)

    def test_webhook_event_page_company_refreshed_on_failure(self):
        event = self._queue('LGF')
        self.assertEqual(event.page_company_id, self.co_a)
        self.page.company_id = self.co_b
        failing = mock.Mock(side_effect=MetaPermanentError('gone'))
        with self._patch_graph(fetch_lead=failing):
            event._process_one()
        self.assertEqual(event.status, 'failed')
        self.assertEqual(event.page_company_id, self.co_b)
        self.assertEqual(event.company_id, self.co_b)
        log = self.env['meta.sync.log'].search(
            [('meta_leadgen_id', '=', 'LGF'), ('status', '=', 'failed')])
        self.assertEqual(log.page_id, self.page)
        self.assertEqual(log.company_id, self.co_b)

    def test_unexpected_drain_error_row_keeps_page(self):
        event = self._queue('LGUD', page_id='PGB')
        Ingest = type(self.env['meta.lead.ingest'])
        with mock.patch.object(Ingest, 'ingest_leadgen',
                               side_effect=ValueError('unexpected')), \
                mute_logger('odoo.addons.meta_lead_ads.models.meta_webhook_event'):
            self.env['meta.webhook.event']._cron_drain()
        self.assertEqual(event.status, 'failed')
        Log = self.env['meta.sync.log']
        log = Log.search([('meta_leadgen_id', '=', 'LGUD'),
                          ('status', '=', 'failed')])
        self.assertEqual(len(log), 1)
        self.assertEqual(log.page_id, self.page_b)
        self.assertEqual(log.company_id, self.co_b)
        # Only the Page's company sees the row, and its admin can retry it.
        self.assertEqual(Log.with_user(self.admin_b).search_count(
            [('id', '=', log.id)]), 1)
        self.assertEqual(Log.with_user(self.admin_a).search_count(
            [('id', '=', log.id)]), 0)

    def test_page_override_outside_user_companies_rejected(self):
        page = self.page.with_user(self.admin_a)
        with self.assertRaises(ValidationError):
            page.write({'company_id': self.co_b.id})
        page.write({'company_id': self.co_a.id})
        self.assertEqual(self.page.company_id, self.co_a)

        Account = self.env['meta.account'].with_user(self.admin_a)
        with self.assertRaises(ValidationError):
            self.account.with_user(self.admin_a).write(
                {'company_id': self.co_b.id})
        # The company rule may refuse this before the create check does;
        # either way the account must not be created.
        try:
            with self.env.cr.savepoint():
                Account.create({'name': 'Foreign', 'account_id': 'ACCF',
                                'company_id': self.co_b.id})
        except (AccessError, ValidationError):
            pass
        else:
            self.fail("an account was created in a foreign company")
        self.assertFalse(self.env['meta.account'].search(
            [('account_id', '=', 'ACCF')]))
        own = Account.create({'name': 'Own', 'account_id': 'ACCO'})
        self.assertEqual(own.company_id, self.co_a)

    # ---- record rules ----------------------------------------------------

    def _company_b_config(self):
        """An account, page, form and mapping that live entirely in B."""
        account_b = self.env['meta.account'].sudo().create({
            'name': 'Acct B', 'account_id': 'ACCB', 'company_id': self.co_b.id})
        page = self.env['meta.page'].sudo().create({
            'name': 'Page B2', 'page_id': 'PGB2', 'account_id': account_b.id})
        form = self.env['meta.lead.form'].sudo().create({
            'name': 'Form B', 'form_id': 'FB', 'page_id': page.id})
        mapping = self._mapping(form)
        return account_b, page, form, mapping

    def _mapping(self, form):
        return self.env['meta.field.mapping'].sudo().create({
            'form_id': form.id, 'meta_key': 'city',
            'crm_field_id': self.env['ir.model.fields']._get(
                'crm.lead', 'city').id})

    def _visible(self, user, records, **ctx):
        Model = self.env[records._name].with_user(user)
        if ctx:
            Model = Model.with_context(**ctx)
        return Model.search([('id', 'in', records.ids)])

    def test_rules_isolate_meta_config(self):
        account_b, page_b2, form_b, mapping_b = self._company_b_config()
        mapping_a = self._mapping(self.form)
        pairs = [(self.account, account_b), (self.page, page_b2),
                 (self.form, form_b), (mapping_a, mapping_b)]
        for rec_a, rec_b in pairs:
            both = rec_a | rec_b
            self.assertEqual(self._visible(self.admin_a, both).ids, rec_a.ids,
                             rec_a._name)
            self.assertEqual(self._visible(self.admin_b, both).ids, rec_b.ids,
                             rec_b._name)
        with self.assertRaises(AccessError):
            self.env['meta.account'].with_user(self.admin_b).browse(
                self.account.id).read(['name'])

    def test_rules_isolate_logs_and_events(self):
        Log = self.env['meta.sync.log']
        log_a = Log._record('LGA', 'cron', 'failed', page=self.page)
        log_b = Log._record('LGB', 'cron', 'failed', page=self.page_b)
        logs = log_a | log_b
        self.assertEqual(self._visible(self.admin_a, logs), log_a)
        self.assertEqual(self._visible(self.admin_b, logs), log_b)
        self.assertEqual(self._visible(self.sales_mgr_a, logs), log_a)

        ev_a = self._queue('LGA')
        ev_b = self._queue('LGB', page_id='PGB')
        ev_blank = self._queue('LGU', page_id='PG_UNKNOWN')
        self.assertEqual(ev_b.company_id, self.co_b)
        self.assertFalse(ev_blank.company_id)
        events = ev_a | ev_b | ev_blank
        self.assertEqual(self._visible(self.admin_a, events), ev_a | ev_blank)
        self.assertEqual(self._visible(self.admin_b, events), ev_b | ev_blank)

    def test_blank_log_without_payload_shared(self):
        log = self.env['meta.sync.log']._record('LGN', 'cron', 'failed')
        self.assertFalse(log.company_id)
        for user in (self.admin_a, self.admin_b, self.sales_mgr_a):
            self.assertEqual(self._visible(user, log), log, user.login)

    def test_blank_log_with_payload_quarantined(self):
        lead = self._lead(self.co_a)
        log = self.env['meta.sync.log'].sudo()._record(
            'LGQ', 'manual', 'success', lead=lead,
            raw={'field_data': [{'name': 'email', 'values': ['q@example.com']}]})
        lead.unlink()
        self.env.flush_all()
        self.env.invalidate_all()
        self.assertFalse(log.company_id)
        for user in (self.admin_a, self.admin_b, self.sales_mgr_a):
            self.assertEqual(
                self.env['meta.sync.log'].with_user(user).search_count(
                    [('id', '=', log.id)]), 0, user.login)
        with self.assertRaises(AccessError):
            self.env['meta.sync.log'].with_user(self.admin_a).browse(
                log.id).read(['status'])
        self.assertEqual(
            self.env['meta.sync.log'].sudo().search([('id', '=', log.id)]), log)

    def test_rules_follow_company_switcher(self):
        account_b, page_b2, form_b, _mapping = self._company_b_config()
        accounts = self.account | account_b
        forms = self.form | form_b
        only_a = {'allowed_company_ids': [self.co_a.id]}
        both = {'allowed_company_ids': [self.co_a.id, self.co_b.id]}
        self.assertEqual(self._visible(self.admin_ab, accounts, **only_a),
                         self.account)
        self.assertEqual(self._visible(self.admin_ab, forms, **only_a),
                         self.form)
        self.assertEqual(self._visible(self.admin_ab, accounts, **both),
                         accounts)
        self.assertEqual(self._visible(self.admin_ab, forms, **both), forms)

    def test_page_override_moves_visibility(self):
        self.assertEqual(self.page_b.account_id, self.account)
        self.assertEqual(self._visible(self.admin_b, self.page_b), self.page_b)
        self.assertFalse(self._visible(self.admin_a, self.page_b))
        self.assertEqual(self._visible(self.admin_a, self.page), self.page)
