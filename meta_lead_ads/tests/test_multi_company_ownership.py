# Meta Lead Ads Integration for Odoo — © 2026 CoreSys Builders. All rights reserved.
# Licensed under the Odoo Proprietary License v1.0 (OPL-1).
# Unauthorized copying, redistribution, or resale of this software, in whole or in
# part, via any medium, is strictly prohibited and constitutes a license violation.
# OPL-1: https://www.odoo.com/documentation/19.0/legal/licenses.html#odoo-apps

"""Company ownership cannot be changed around the record rules."""
import json

from odoo import Command
from odoo.exceptions import AccessError, UserError, ValidationError
from odoo.tests.common import TransactionCase, new_test_user, tagged

from .test_ingest import IngestFixtureMixin
from .test_multi_company import MultiCompanyFixtureMixin


@tagged('post_install', '-at_install')
class TestMultiCompanyOwnership(MultiCompanyFixtureMixin, IngestFixtureMixin,
                                TransactionCase):

    def _queue(self, leadgen_id, page_id='PG1'):
        payload = {'object': 'page', 'entry': [{
            'id': page_id,
            'changes': [{'field': 'leadgen', 'value': {
                'leadgen_id': leadgen_id, 'page_id': page_id,
                'form_id': 'F1'}}],
        }]}
        Event = self.env['meta.webhook.event'].sudo()
        Event._ingest_payload(payload, raw=json.dumps(payload).encode())
        return Event.search([('leadgen_id', '=', leadgen_id)])

    def _reload(self, *records):
        for rec in records:
            rec.invalidate_recordset()

    def _foreign_config(self):
        """An account, Page and form that live entirely in company B."""
        account_b = self.env['meta.account'].sudo().create({
            'name': 'Acct B', 'account_id': 'ACCB', 'company_id': self.co_b.id})
        page = self.env['meta.page'].sudo().create({
            'name': 'Page B2', 'page_id': 'PGB2', 'account_id': account_b.id})
        form = self.env['meta.lead.form'].sudo().create({
            'name': 'Form B', 'form_id': 'FB', 'page_id': page.id})
        return account_b, page, form

    def _assert_refused(self, call):
        # A create can be stopped by the company rule before our own check
        # runs; either way nothing may be stored.
        try:
            with self.env.cr.savepoint():
                call()
        except (AccessError, ValidationError):
            return
        self.fail("a cross-company create was accepted")

    def _city_field(self):
        return self.env['ir.model.fields']._get('crm.lead', 'city')

    # ---- derived company fields ----------------------------------------

    def test_page_lead_company_not_writable(self):
        with self.assertRaises(AccessError):
            self.page.with_user(self.admin_a).write(
                {'lead_company_id': self.co_b.id})
        self._reload(self.page)
        self.assertEqual(self.page.lead_company_id, self.co_a)
        # Privileged code is not stopped by the guard.
        self.page.with_user(self.admin_a).sudo().write(
            {'lead_company_id': self.co_a.id})

    def test_form_company_not_writable(self):
        with self.assertRaises(AccessError):
            self.form.with_user(self.admin_a).write(
                {'company_id': self.co_b.id})
        self._reload(self.page, self.form)
        self.assertEqual(self.form.company_id, self.co_a)
        # The related field would have pushed the value onto the Page.
        self.assertEqual(self.page.lead_company_id, self.co_a)
        self.assertFalse(self.page.company_id)
        self.assertEqual(self.page_b.lead_company_id, self.co_b)

    def test_sync_log_company_not_writable(self):
        Log = self.env['meta.sync.log']
        log = Log._record('LGA', 'cron', 'failed', page=self.page)
        self.assertEqual(log.company_id, self.co_a)
        with self.assertRaises(AccessError):
            log.with_user(self.admin_a).write({'company_id': self.co_b.id})
        self._reload(log)
        self.assertEqual(log.company_id, self.co_a)

        with self.assertRaises(AccessError):
            Log.with_user(self.admin_a).create({
                'meta_leadgen_id': 'LGX', 'trigger': 'manual',
                'status': 'failed', 'company_id': self.co_a.id})
        self.assertFalse(Log.sudo().search([('meta_leadgen_id', '=', 'LGX')]))

    def test_webhook_event_company_not_writable(self):
        event = self._queue('LGE')
        self.assertEqual(event.company_id, self.co_a)
        as_admin = event.with_user(self.admin_a)
        with self.assertRaises(AccessError):
            as_admin.write({'company_id': self.co_b.id})
        with self.assertRaises(AccessError):
            as_admin.write({'page_company_id': self.co_b.id})
        self._reload(event)
        self.assertEqual(event.page_company_id, self.co_a)
        self.assertEqual(event.company_id, self.co_a)

        Event = self.env['meta.webhook.event']
        with self.assertRaises(AccessError):
            Event.with_user(self.admin_a).create({
                'leadgen_id': 'LGEX', 'page_id': 'PG1',
                'page_company_id': self.co_a.id})
        self.assertFalse(Event.sudo().search([('leadgen_id', '=', 'LGEX')]))

    def test_privileged_paths_unaffected(self):
        event = self._queue('LGP', page_id='PGB')
        self.assertEqual(event.page_company_id, self.co_b)
        self.assertEqual(event.company_id, self.co_b)

        log = self.env['meta.sync.log'].with_user(self.admin_a).sudo()._record(
            'LGS', 'cron', 'failed', page=self.page_b)
        self.assertEqual(log.company_id, self.co_b)

        self.page.with_user(self.admin_a).write({'name': 'Renamed'})
        self.assertEqual(self.page.name, 'Renamed')
        own_log = self.env['meta.sync.log']._record(
            'LGO', 'cron', 'failed', page=self.page)
        own_log.with_user(self.admin_a).write({'status': 'success'})
        self.assertEqual(own_log.status, 'success')
        self.assertEqual(own_log.company_id, self.co_a)

    # ---- routing through relations -------------------------------------

    def test_page_account_change_to_foreign_account_rejected(self):
        account_b = self.env['meta.account'].sudo().create({
            'name': 'Acct B', 'account_id': 'ACCB', 'company_id': self.co_b.id})
        with self.assertRaises(ValidationError):
            self.page.with_user(self.admin_a).write({'account_id': account_b.id})
        self._reload(self.page)
        self.assertEqual(self.page.account_id, self.account)
        self.assertEqual(self.page.lead_company_id, self.co_a)

    def test_page_account_change_needs_current_account_access(self):
        # page_b sits on company A's account and sends leads to B. admin_b
        # can't read account A, so may not move the Page onto B's account.
        account_b = self.env['meta.account'].sudo().create({
            'name': 'Acct B', 'account_id': 'ACCB', 'company_id': self.co_b.id})
        try:
            with self.env.cr.savepoint():
                self.page_b.with_user(self.admin_b).write(
                    {'account_id': account_b.id})
        except ValidationError:
            pass
        else:
            self.fail("a Page was taken from an account the writer can't read")
        self._reload(self.page_b)
        self.assertEqual(self.page_b.account_id, self.account)
        # Someone who can read both accounts may still move it.
        self.page_b.with_user(self.admin_ab).with_context(
            allowed_company_ids=[self.co_a.id, self.co_b.id]).write(
            {'account_id': account_b.id})
        self.assertEqual(self.page_b.account_id, account_b)
        self.assertEqual(self.page_b.lead_company_id, self.co_b)

    def test_clear_override_to_foreign_company_rejected(self):
        # Clearing the override would send page_b's leads to the account's
        # company A, which admin_b does not belong to.
        with self.assertRaises(ValidationError):
            self.page_b.with_user(self.admin_b).write({'company_id': False})
        self._reload(self.page_b)
        self.assertEqual(self.page_b.company_id, self.co_b)
        self.assertEqual(self.page_b.lead_company_id, self.co_b)

    def test_override_change_requires_account_access(self):
        # admin_b sees page_b but not its account in A, so may not route it,
        # not even to the company it already uses.
        page = self.page_b.with_user(self.admin_b)
        with self.assertRaises(ValidationError):
            page.write({'company_id': self.co_b.id})
        page.write({'name': 'Page B renamed'})
        self._reload(self.page_b)
        self.assertEqual(self.page_b.name, 'Page B renamed')
        self.assertEqual(self.page_b.company_id, self.co_b)

    def test_member_routing_allowed(self):
        page = self.page.with_user(self.admin_ab).with_context(
            allowed_company_ids=[self.co_a.id, self.co_b.id])
        page.write({'company_id': self.co_b.id})
        self.assertEqual(self.page.lead_company_id, self.co_b)
        page.write({'company_id': False})
        self.assertEqual(self.page.lead_company_id, self.co_a)

    def test_form_reparent_to_unreadable_page_rejected(self):
        _account_b, page_b2, _form_b = self._foreign_config()
        with self.assertRaises(ValidationError):
            self.form.with_user(self.admin_a).write({'page_id': page_b2.id})
        self._reload(self.form)
        self.assertEqual(self.form.page_id, self.page)
        self.assertEqual(self.form.company_id, self.co_a)

        Form = self.env['meta.lead.form'].with_user(self.admin_a)
        self._assert_refused(lambda: Form.create({
            'name': 'Injected', 'form_id': 'FX', 'page_id': page_b2.id}))
        self.assertFalse(self.env['meta.lead.form'].sudo().with_context(
            active_test=False).search([('form_id', '=', 'FX')]))

    def test_mapping_on_unreadable_form_rejected(self):
        _account_b, _page_b2, form_b = self._foreign_config()
        Mapping = self.env['meta.field.mapping'].with_user(self.admin_a)
        self._assert_refused(lambda: Mapping.create({
            'form_id': form_b.id, 'meta_key': 'city',
            'crm_field_id': self._city_field().id}))
        self.assertFalse(self.env['meta.field.mapping'].sudo().search(
            [('form_id', '=', form_b.id)]))

        own = Mapping.create({'form_id': self.form.id, 'meta_key': 'city',
                              'crm_field_id': self._city_field().id})
        self.assertEqual(own.form_id, self.form)
        with self.assertRaises(ValidationError):
            own.write({'form_id': form_b.id})
        self._reload(own)
        self.assertEqual(own.form_id, self.form)

    # ---- links that decide the company of a log row or event -----------

    def _sales_admin_b(self):
        """A company B Meta Admin who can also work with B's leads."""
        return new_test_user(
            self.env, 'mc_sales_admin_b',
            groups='base.group_user,meta_lead_ads.group_meta_admin,'
                   'sales_team.group_sale_salesman_all_leads',
            company_id=self.co_b.id, company_ids=[Command.set([self.co_b.id])])

    def _lead_in(self, company, name):
        return self.env['crm.lead'].create({
            'name': name, 'type': 'lead', 'company_id': company.id,
            'user_id': False, 'team_id': False})

    def _assert_write_refused(self, record, vals):
        try:
            with self.env.cr.savepoint():
                record.write(vals)
        except (AccessError, ValidationError):
            self._reload(record)
            return
        self.fail("a write of %s was accepted" % sorted(vals))

    def test_sync_log_links_need_read_access(self):
        user_b = self._sales_admin_b()
        lead_a = self._lead_in(self.co_a, 'Lead A')
        lead_b = self._lead_in(self.co_b, 'Lead B')
        log = self.env['meta.sync.log']._record(
            'LGLB', 'cron', 'failed', error='boom', page=self.page_b)
        self.assertEqual(log.company_id, self.co_b)
        as_b = log.with_user(user_b)
        # Either link would move B's row into company A's logs.
        for vals in ({'page_id': self.page.id}, {'lead_id': lead_a.id}):
            self._assert_write_refused(as_b, vals)
            self.assertEqual(log.page_id, self.page_b)
            self.assertFalse(log.lead_id)
            self.assertEqual(log.company_id, self.co_b)
        # Records the writer can read are still accepted.
        as_b.write({'lead_id': lead_b.id})
        self.assertEqual(log.lead_id, lead_b)
        self.assertEqual(log.company_id, self.co_b)

    def test_webhook_event_links_need_read_access(self):
        user_b = self._sales_admin_b()
        lead_a = self._lead_in(self.co_a, 'Lead A')
        lead_b = self._lead_in(self.co_b, 'Lead B')
        event = self._queue('LGEL', page_id='PGB')
        self.assertEqual(event.company_id, self.co_b)
        as_b = event.with_user(user_b)
        # A lead moves the event at once; a Meta page id moves it on the
        # next drain, which also ingests through that Page.
        for vals in ({'lead_id': lead_a.id}, {'page_id': 'PG1'}):
            self._assert_write_refused(as_b, vals)
            self.assertFalse(event.lead_id)
            self.assertEqual(event.page_id, 'PGB')
            self.assertEqual(event.company_id, self.co_b)
        as_b.write({'lead_id': lead_b.id})
        self.assertEqual(event.lead_id, lead_b)
        self.assertEqual(event.company_id, self.co_b)

    def test_company_less_rows_read_only_for_company_admins(self):
        Log = self.env['meta.sync.log']
        shared_log = Log._record('LGSH', 'webhook', 'failed',
                                 error='Unknown page; cannot process event')
        shared_event = self._queue('LGSHE', page_id='PGUNKNOWN')
        self.assertFalse(shared_log.company_id)
        self.assertFalse(shared_event.company_id)
        for rec in (shared_log, shared_event):
            as_b = rec.with_user(self.admin_b)
            # Still listed for every company.
            self.assertEqual(as_b.search_count([('id', '=', rec.id)]), 1)
            self._assert_write_refused(as_b, {'error_message': 'tampered'})
            with self.assertRaises(AccessError), self.env.cr.savepoint():
                as_b.unlink()
            self._reload(rec)
            self.assertTrue(rec.exists())
            self.assertNotEqual(rec.error_message, 'tampered')
        self._assert_refused(lambda: Log.with_user(self.admin_b).create({
            'meta_leadgen_id': 'LGNEW', 'trigger': 'manual',
            'status': 'failed', 'error_message': 'planted'}))
        self.assertFalse(Log.sudo().search([('meta_leadgen_id', '=', 'LGNEW')]))
        Event = self.env['meta.webhook.event']
        self._assert_refused(lambda: Event.with_user(self.admin_b).create({
            'leadgen_id': 'LGNEWE', 'page_id': 'PGUNKNOWN'}))
        self.assertFalse(Event.sudo().search([('leadgen_id', '=', 'LGNEWE')]))

    def test_admin_retry_own_company_row(self):
        log = self.env['meta.sync.log']._record(
            'LG1', 'webhook', 'failed', error='boom', page=self.page)
        with self._patch_graph():
            log.with_user(self.admin_a).action_retry()
        self._reload(log)
        self.assertEqual(log.status, 'success')
        self.assertEqual(log.lead_id.meta_leadgen_id, 'LG1')
        self.assertEqual(log.company_id, self.co_a)
        self.assertEqual(self.env['meta.sync.log'].search_count(
            [('meta_leadgen_id', '=', 'LG1')]), 1)

    def test_admin_retry_after_page_moved_clean_error(self):
        lead = self.env['crm.lead'].create({
            'name': 'Moved', 'type': 'lead', 'company_id': self.co_a.id,
            'user_id': False, 'team_id': False})
        log = self.env['meta.sync.log']._record(
            'LGMV', 'webhook', 'failed', error='boom', page=self.page,
            lead=lead)
        # The Page now sends leads to B; the row follows its lead in A.
        self.page.company_id = self.co_b
        self.assertEqual(log.company_id, self.co_a)
        with self._patch_graph(), self.assertRaises(UserError) as ctx:
            log.with_user(self.admin_a).action_retry()
        self.assertIs(type(ctx.exception), UserError)
        self.assertEqual(self._fetch_lead.call_count, 0)
        self._reload(log)
        self.assertEqual(log.status, 'failed')
        self.assertEqual(log.retry_count, 0)
        self.assertEqual(log.error_message, 'boom')

    def test_admin_retry_company_less_row_clean_error(self):
        log = self.env['meta.sync.log']._record(
            'LGNC', 'webhook', 'failed', error='Unexpected drain error')
        self.assertFalse(log.company_id)
        with self.assertRaises(UserError) as ctx:
            log.with_user(self.admin_b).action_retry()
        self.assertIs(type(ctx.exception), UserError)
        self._reload(log)
        self.assertEqual(log.retry_count, 0)
        self.assertEqual(log.error_message, 'Unexpected drain error')

    # ---- links to Meta records that only show a name --------------------

    def _sales_meta_user(self, login, company):
        return new_test_user(
            self.env, login,
            groups='base.group_user,meta_lead_ads.group_meta_user,'
                   'sales_team.group_sale_salesman_all_leads',
            company_id=company.id, company_ids=[Command.set([company.id])])

    def test_lead_meta_links_need_read_access(self):
        account_b, page_b2, form_b = self._foreign_config()
        lead = self._lead_in(self.co_a, 'Lead A')
        links = ({'meta_account_id': account_b.id},
                 {'meta_page_id_ref': page_b2.id},
                 {'meta_form_id_ref': form_b.id})
        # A plain salesperson, and a company A Meta User who can read A's
        # Meta records but none of B's.
        user_a = self._sales_meta_user('mc_sales_meta_a', self.co_a)
        for user in (self.sales_a, user_a):
            for vals in links:
                self._assert_write_refused(lead.with_user(user), vals)
                self.assertFalse(lead[next(iter(vals))])
                Lead = self.env['crm.lead'].with_user(user)
                self._assert_refused(lambda: Lead.create(dict(
                    vals, name='Planted %s' % next(iter(vals)),
                    type='lead')))
        self.assertFalse(self.env['crm.lead'].search(
            [('name', '=like', 'Planted %')]))
        # Records the writer can read are still accepted.
        lead.with_user(user_a).write({'meta_account_id': self.account.id,
                                      'meta_page_id_ref': self.page.id,
                                      'meta_form_id_ref': self.form.id})
        self.assertEqual(lead.meta_account_id, self.account)
        self.assertEqual(lead.meta_form_id_ref, self.form)

    def test_copy_of_routed_lead_keeps_readable_links(self):
        # A lead from page_b keeps company A's account, which B can't read.
        lead = self.env['crm.lead'].create({
            'name': 'Routed', 'type': 'lead', 'company_id': self.co_b.id,
            'user_id': False, 'team_id': False,
            'meta_account_id': self.account.id,
            'meta_page_id_ref': self.page_b.id})
        user_b = self._sales_meta_user('mc_sales_meta_b', self.co_b)
        copy = lead.with_user(user_b).copy()
        self.assertEqual(copy.meta_page_id_ref, self.page_b)
        self.assertFalse(copy.sudo().meta_account_id)
        self.assertEqual(copy.company_id, self.co_b)

    def test_onboarding_wizard_links_need_read_access(self):
        account_b, page_b2, _form_b = self._foreign_config()
        Wizard = self.env['meta.onboarding'].with_user(self.admin_a)
        for vals in ({'page_id': page_b2.id}, {'account_id': account_b.id}):
            self._assert_refused(lambda: Wizard.create(dict(vals)))
            wizard = Wizard.create({})
            self._assert_write_refused(wizard, vals)
            self.assertFalse(wizard[next(iter(vals))])
        wizard = Wizard.create({'page_id': self.page.id,
                                'account_id': self.account.id})
        self.assertEqual(wizard.page_id, self.page)
        self.assertEqual(wizard.account_id, self.account)

    # ---- deleting an account --------------------------------------------

    def test_account_with_pages_routed_elsewhere_not_deleted(self):
        # self.account is company A's and feeds page_b, which sends leads
        # to B. Deleting it would delete page_b and its forms with it.
        for account in (self.account.with_user(self.admin_a),
                        self.account.sudo()):
            with self.assertRaises(UserError) as ctx, \
                    self.env.cr.savepoint():
                account.unlink()
            self.assertIs(type(ctx.exception), UserError)
            self.assertIn('Archive', str(ctx.exception))
            self.assertNotIn('Meta Co B', str(ctx.exception))
        self._reload(self.account, self.page_b)
        self.assertTrue(self.account.exists())
        self.assertTrue(self.page_b.exists())
        # Archiving stays possible.
        self.account.with_user(self.admin_a).write({'active': False})
        self.assertFalse(self.account.active)

    def test_account_with_own_pages_can_be_deleted(self):
        account = self.env['meta.account'].with_user(self.admin_a).create({
            'name': 'Own only', 'account_id': 'ACCOWN',
            'company_id': self.co_a.id})
        page = self.env['meta.page'].with_user(self.admin_a).create({
            'name': 'Own page', 'page_id': 'PGOWN', 'account_id': account.id,
            'company_id': self.co_a.id})
        account.unlink()
        self.assertFalse(account.exists())
        self.assertFalse(page.exists())
