# Meta Lead Ads Integration for Odoo — © 2026 CoreSys Builders. All rights reserved.
# Licensed under the Odoo Proprietary License v1.0 (OPL-1).
# Unauthorized copying, redistribution, or resale of this software, in whole or in
# part, via any medium, is strictly prohibited and constitutes a license violation.
# OPL-1: https://www.odoo.com/documentation/19.0/legal/licenses.html#odoo-apps

"""Company resolution and company-scoped dedup in ingest_leadgen."""
import logging
import time
import uuid
from contextlib import contextmanager
from unittest import mock

from psycopg2.errors import SerializationFailure

from odoo import Command, api
from odoo.api import SUPERUSER_ID
from odoo.exceptions import AccessError, ValidationError
from odoo.modules.registry import Registry
from odoo.tests.common import (BaseCase, TransactionCase, get_db_name,
                               new_test_user, tagged)
from odoo.tools import mute_logger

from odoo.addons.meta_lead_ads.models import meta_lead_ingest as ingest_module
from odoo.addons.meta_lead_ads.models.exceptions import MetaPermanentError

from .test_ingest import IngestFixtureMixin
from .test_mapping_targets import legacy_mapping
from .test_multi_company import MultiCompanyFixtureMixin

_logger = logging.getLogger(__name__)


@contextmanager
def capture_sql_log():
    """Yield the list of records odoo.sql_db logs while the block runs."""
    records = []

    class _Keep(logging.Handler):
        def emit(self, record):
            records.append(record)

    handler = _Keep(logging.DEBUG)
    logger = logging.getLogger('odoo.sql_db')
    logger.addHandler(handler)
    try:
        yield records
    finally:
        logger.removeHandler(handler)


@tagged('post_install', '-at_install')
class TestMultiCompanyIngest(MultiCompanyFixtureMixin, IngestFixtureMixin,
                             TransactionCase):

    # ---- helpers ---------------------------------------------------------

    def _patch_request(self, payload):
        """Patch only the low-level Graph call, so fetch_lead still reads the
        Page token and the account App Secret itself."""
        self._request = mock.Mock(return_value=payload)
        return mock.patch.multiple(
            self.Client, _request=self._request,
            resolve_name=mock.Mock(side_effect=self._resolve_passthrough))

    def _lead_by_id(self, leadgen_id):
        return self.env['crm.lead'].sudo().with_context(
            active_test=False).search([('meta_leadgen_id', '=', leadgen_id)])

    def _logs(self, leadgen_id):
        return self.env['meta.sync.log'].sudo().search(
            [('meta_leadgen_id', '=', leadgen_id)])

    def _payload(self, leadgen_id, email='jane@example.com', phone=None,
                 form_id='F1', extra=()):
        field_data = [{'name': 'full_name', 'values': ['Jane Doe']}]
        if email:
            field_data.append({'name': 'email', 'values': [email]})
        if phone:
            field_data.append({'name': 'phone_number', 'values': [phone]})
        field_data.extend(extra)
        return self._fake_lead(id=leadgen_id, form_id=form_id,
                               field_data=field_data)

    def _company_b_page(self):
        """A Page whose account and company both sit in company B."""
        account_b = self.env['meta.account'].sudo().create({
            'name': 'Acct B', 'account_id': 'ACCB', 'app_id': 'app_test',
            'app_secret': 'secret_test', 'access_token': 'tok_acct_b',
            'company_id': self.co_b.id})
        return self.env['meta.page'].sudo().create({
            'name': 'Hidden B', 'page_id': 'PGH', 'access_token': 'tok_h',
            'account_id': account_b.id})

    def test_fixture_pages_resolve_companies(self):
        self.assertEqual(self.page.lead_company_id, self.co_a)
        self.assertEqual(self.page_b.lead_company_id, self.co_b)

    # ---- company resolution and caller checks ---------------------------

    def test_company_resolution_account_vs_page_override(self):
        with self._patch_graph(fetch_lead=self._payload('LG1', 'one@example.com')):
            lead_a = self.Ingest.ingest_leadgen(self.page, 'LG1', 'manual')
        with self._patch_graph(fetch_lead=self._payload('LG2', 'two@example.com')):
            lead_b = self.Ingest.ingest_leadgen(self.page_b, 'LG2', 'manual')
        self.assertEqual(lead_a.company_id, self.co_a)
        self.assertEqual(lead_b.company_id, self.co_b)

    def test_override_page_foreign_account_secret_read(self):
        wizard = self.env['meta.ingest.leadgen'].with_user(self.admin_b).create(
            {'page_id': self.page_b.id, 'leadgen_id': 'LGS'})
        with self._patch_request(self._payload('LGS', 'secret@example.com')):
            wizard.action_ingest()
        self.assertEqual(self._request.call_count, 1)
        call = self._request.call_args
        self.assertEqual(call.args[0], 'tok_b')
        self.assertEqual(call.kwargs.get('app_secret'), 'secret_test')
        lead = self._lead_by_id('LGS')
        self.assertEqual(len(lead), 1)
        self.assertEqual(lead.company_id, self.co_b)
        self.assertEqual(lead.create_uid, self.admin_b)

    def test_hidden_page_wizard_refused(self):
        hidden = self._company_b_page()
        wizard = self.env['meta.ingest.leadgen'].with_user(self.admin_a).create(
            {'page_id': hidden.id, 'leadgen_id': 'LGH'})
        with self._patch_request(self._payload('LGH')):
            with self.assertRaises(AccessError):
                wizard.action_ingest()
        self.assertEqual(self._request.call_count, 0)
        self.assertFalse(self._lead_by_id('LGH'))
        self.assertFalse(self._logs('LGH'))

    def test_non_admin_caller_refused(self):
        with self._patch_graph():
            with self.assertRaises(AccessError):
                self.Ingest.with_user(self.sales_a).ingest_leadgen(
                    self.page.with_user(self.sales_a), 'LGN', trigger='manual')
        self.assertEqual(self._fetch_lead.call_count, 0)
        self.assertFalse(self._lead_by_id('LGN'))

    def test_multi_selected_context_uses_destination_only(self):
        team_a = self.env['crm.team'].create({
            'name': 'Team A', 'company_id': self.co_a.id, 'use_leads': True,
            'member_ids': [Command.link(self.admin_ab.id)]})
        Ingest = self.Ingest.with_user(self.admin_ab).with_context(
            allowed_company_ids=[self.co_a.id, self.co_b.id],
            default_team_id=team_a.id, default_user_id=self.sales_a.id)
        with self._patch_graph(fetch_lead=self._payload('LGM', 'multi@example.com')):
            Ingest.ingest_leadgen(self.page_b.with_env(Ingest.env), 'LGM',
                                  trigger='manual')
        lead = self._lead_by_id('LGM')
        self.assertEqual(lead.company_id, self.co_b)
        self.assertIn(lead.team_id.company_id,
                      (self.env['res.company'], self.co_b))
        self.assertNotEqual(lead.user_id, self.sales_a)

    def _ingest_owner_answer(self, leadgen_id):
        """Ingest a page_b lead whose 'owner' answer has an old salesperson
        mapping (saved before such mappings were refused).

        Returns the lead, the submitted name and every formatted log line.
        """
        form_b = self.env['meta.lead.form'].sudo().create({
            'name': 'Form B', 'form_id': 'FB1', 'page_id': self.page_b.id})
        legacy_mapping(self.env, form_b, 'user_id', meta_key='owner')
        submitted = self.sales_a.name
        payload = self._payload(
            leadgen_id, 'owner@example.com', form_id='FB1',
            extra=[{'name': 'owner', 'values': [submitted]}])
        with self._patch_graph(fetch_lead=payload), \
                mock.patch.object(ingest_module, '_logger') as logger:
            lead = self.Ingest.ingest_leadgen(self.page_b, leadgen_id,
                                              'manual')
        lines = [c.args[0] % c.args[1:]
                 for c in (logger.info.call_args_list
                           + logger.warning.call_args_list)]
        return lead, submitted, lines

    def _assert_owner_kept(self, leadgen_id, lead, submitted, lines):
        self.assertEqual(lead.company_id, self.co_b)
        self.assertNotEqual(lead.user_id, self.sales_a)
        owner = lead.answer_ids.filtered(lambda a: a.question_key == 'owner')
        self.assertEqual(len(owner), 1)
        self.assertEqual(owner.value, submitted)
        self.assertIn("owner: %s" % submitted, lead.description or '')
        drop_lines = [line for line in lines
                      if "'owner'" in line and 'user_id' in line]
        self.assertTrue(drop_lines, lines)
        for line in drop_lines:
            self.assertNotIn(submitted, line)
        success = self._logs(leadgen_id).filtered(
            lambda l: l.status == 'success')
        self.assertEqual(len(success), 1)
        self.assertIn(submitted, success.raw_payload)
        return drop_lines

    def test_mapped_salesperson_answer_kept(self):
        lead, submitted, lines = self._ingest_owner_answer('LGO')
        self._assert_owner_kept('LGO', lead, submitted, lines)

    def test_mapped_foreign_record_kept_as_answer(self):
        # Let the old row through the blocked-field check so the company
        # check behind it still gets exercised.
        real = ingest_module.is_blocked_target
        with mock.patch.object(
                ingest_module, 'is_blocked_target',
                side_effect=lambda name, comodel=None: (
                    name != 'user_id' and real(name, comodel))):
            lead, submitted, lines = self._ingest_owner_answer('LGOF')
        drop_lines = self._assert_owner_kept('LGOF', lead, submitted, lines)
        self.assertTrue(any("names a record outside the lead's company" in line
                            for line in drop_lines), drop_lines)

    def test_idempotency_lookup_is_global(self):
        existing = self.env['crm.lead'].create({
            'name': 'B lead', 'type': 'lead', 'company_id': self.co_b.id,
            'meta_leadgen_id': 'LGX'})
        before = self.env['crm.lead'].search_count([])
        with self._patch_graph():
            lead = self.Ingest.ingest_leadgen(self.page, 'LGX', 'manual')
        self.assertEqual(lead, existing)
        self.assertEqual(self.env['crm.lead'].search_count([]), before)
        self.assertEqual(self._fetch_lead.call_count, 0)
        self.assertEqual(self._logs('LGX').status, 'skipped_idempotent')

    def test_ingest_returns_caller_env_record(self):
        Ingest = self.Ingest.with_user(self.admin_a)
        with self._patch_graph(fetch_lead=self._payload('LGE', 'env@example.com')):
            lead = Ingest.ingest_leadgen(self.page.with_env(Ingest.env),
                                         'LGE', trigger='manual')
        self.assertTrue(lead.id)
        self.assertFalse(lead.env.su)
        self.assertEqual(lead.env.uid, self.admin_a.id)

    def test_ingest_rejects_non_page_argument(self):
        with self._patch_graph():
            with self.assertRaises(MetaPermanentError):
                self.Ingest.ingest_leadgen(self.page.id, 'LGI', 'manual')
        self.assertFalse(self._lead_by_id('LGI'))

    # ---- sync-log rows, retry and the manual wizard ----------------------

    def _assert_row_keeps_company(self, log, company):
        """Delete the row's lead and check the row still has ``company``."""
        log.lead_id.unlink()
        self.env.flush_all()
        log.invalidate_recordset()
        self.assertFalse(log.lead_id)
        self.assertEqual(log.company_id, company)

    def test_idempotent_cross_company_hit_as_admin(self):
        existing = self.env['crm.lead'].create({
            'name': 'B lead', 'type': 'lead', 'company_id': self.co_b.id,
            'meta_leadgen_id': 'LGY'})
        before = self.env['crm.lead'].search_count([])
        Ingest = self.Ingest.with_user(self.admin_a)
        with self._patch_graph():
            Ingest.ingest_leadgen(self.page.with_env(Ingest.env), 'LGY',
                                  trigger='manual')
        self.assertEqual(self.env['crm.lead'].search_count([]), before)
        log = self._logs('LGY')
        self.assertEqual(len(log), 1)
        self.assertEqual(log.status, 'skipped_idempotent')
        self.assertEqual(log.lead_id, existing)
        self.assertEqual(log.company_id, self.co_b)
        self.assertEqual(log.page_id, self.page)
        self.assertEqual(log.create_uid, self.admin_a)

    def test_retry_cross_company_idempotent_hit_no_orphan(self):
        existing = self.env['crm.lead'].create({
            'name': 'B lead', 'type': 'lead', 'company_id': self.co_b.id,
            'meta_leadgen_id': 'LGZ'})
        failed = self.env['meta.sync.log']._record(
            'LGZ', 'webhook', 'failed', error='boom', page=self.page,
            raw=self._payload('LGZ'))
        self.assertEqual(failed.company_id, self.co_a)
        with self._patch_graph():
            failed.with_user(self.admin_a).action_retry()
        logs = self._logs('LGZ')
        self.assertEqual(logs, failed)
        self.assertEqual(failed.status, 'skipped_idempotent')
        self.assertEqual(failed.lead_id, existing)
        self.assertEqual(failed.page_id, self.page)
        self.assertEqual(failed.trigger, 'manual')

    def test_log_rows_carry_page(self):
        # Create path.
        with self._patch_graph(fetch_lead=self._payload('LGP1', 'p1@example.com')):
            self.Ingest.ingest_leadgen(self.page, 'LGP1', 'manual')
        created = self._logs('LGP1')
        self.assertEqual(created.status, 'success')
        self.assertEqual(created.page_id, self.page)
        self._assert_row_keeps_company(created, self.co_a)

        # Match path.
        seed = self.env['crm.lead'].create({
            'name': 'Seed', 'type': 'lead', 'company_id': self.co_a.id,
            'email_from': 'p2@example.com'})
        with self._patch_graph(fetch_lead=self._payload('LGP2', 'p2@example.com')):
            lead = self.Ingest.ingest_leadgen(self.page, 'LGP2', 'manual')
        self.assertEqual(lead, seed)
        enriched = self._logs('LGP2')
        self.assertEqual(enriched.match_key, 'email')
        self.assertEqual(enriched.page_id, self.page)
        self._assert_row_keeps_company(enriched, self.co_a)

        # Lost UNIQUE race on create.
        winner = self.env['crm.lead'].create({
            'name': 'Winner', 'type': 'lead', 'company_id': self.co_a.id,
            'meta_leadgen_id': 'LGP3'})
        winner.flush_recordset()
        with self._patch_graph(fetch_lead=self._payload('LGP3', 'p3@example.com')), \
                mute_logger('odoo.sql_db'), \
                mock.patch.object(type(self.Ingest), '_find_by_leadgen_id',
                                  return_value=self.env['crm.lead']):
            lead = self.Ingest.ingest_leadgen(self.page, 'LGP3', 'manual')
        self.assertEqual(lead, winner)
        raced = self._logs('LGP3')
        self.assertEqual(raced.status, 'skipped_idempotent')
        self.assertEqual(raced.page_id, self.page)
        self._assert_row_keeps_company(raced, self.co_a)

    def test_manual_wizard_admin_without_sales_rights(self):
        wizard = self.env['meta.ingest.leadgen'].with_user(self.admin_a).create(
            {'page_id': self.page.id, 'leadgen_id': 'LGW'})
        with self._patch_graph(fetch_lead=self._payload('LGW', 'w@example.com')):
            action = wizard.action_ingest()
        lead = self._lead_by_id('LGW')
        self.assertEqual(lead.company_id, self.co_a)
        self.assertEqual(lead.create_uid, self.admin_a)
        self.assertEqual(action.get('type'), 'ir.actions.client')
        self.assertEqual(action.get('tag'), 'display_notification')
        message = str(action['params']['message'])
        self.assertNotIn(lead.name, message)
        self.assertNotIn('Jane', message)

    def test_wizard_opens_lead_when_readable(self):
        admin_sales = new_test_user(
            self.env, 'mc_admin_sales',
            groups='base.group_user,meta_lead_ads.group_meta_admin,'
                   'sales_team.group_sale_salesman_all_leads',
            company_id=self.co_a.id,
            company_ids=[Command.set([self.co_a.id, self.co_b.id])])
        wizard = self.env['meta.ingest.leadgen'].with_user(admin_sales).create(
            {'page_id': self.page.id, 'leadgen_id': 'LGV'})
        with self._patch_graph(fetch_lead=self._payload('LGV', 'v@example.com')):
            action = wizard.action_ingest()
        lead = self._lead_by_id('LGV')
        self.assertEqual(action.get('type'), 'ir.actions.act_window')
        self.assertEqual(action.get('res_model'), 'crm.lead')
        self.assertEqual(action.get('res_id'), lead.id)

    # ---- company-scoped dedup -------------------------------------------

    def _seed(self, company, **vals):
        """An open lead in ``company`` (False for no company).

        No salesperson or team, so crm's own company coherence rules don't
        interfere with the company under test."""
        values = {'name': 'Seed', 'type': 'lead', 'user_id': False,
                  'team_id': False, 'company_id': company.id if company else False}
        values.update(vals)
        return self.env['crm.lead'].create(values)

    def _spy_writes(self, raise_when=None):
        """Patch crm.lead.write to record (ids, vals) and optionally raise."""
        LeadCls = type(self.env['crm.lead'])
        original = LeadCls.write
        calls = []

        def write(records, vals):
            calls.append((list(records.ids), dict(vals)))
            if raise_when:
                raise_when(vals)
            return original(records, vals)
        return calls, mock.patch.object(LeadCls, 'write', write)

    def test_cross_company_dedup_isolation(self):
        foreign = self._seed(self.co_a, email_from='iso@example.com')
        with self._patch_graph(fetch_lead=self._payload('LGI1', 'iso@example.com')):
            lead = self.Ingest.ingest_leadgen(self.page_b, 'LGI1', 'manual')
        self.assertNotEqual(lead, foreign)
        self.assertEqual(lead.company_id, self.co_b)
        self.assertFalse(foreign.meta_leadgen_id)
        log = self._logs('LGI1')
        self.assertEqual(log.match_key, 'none')
        self.assertFalse(log.error_message)

        foreign_phone = self._seed(self.co_a, phone='5559876543')
        with self._patch_graph(fetch_lead=self._payload(
                'LGI2', email=None, phone='+1 (555) 987-6543')):
            lead = self.Ingest.ingest_leadgen(self.page_b, 'LGI2', 'manual')
        self.assertNotEqual(lead, foreign_phone)
        self.assertEqual(lead.company_id, self.co_b)
        self.assertFalse(foreign_phone.meta_leadgen_id)
        log = self._logs('LGI2')
        self.assertEqual(log.match_key, 'none')
        self.assertFalse(log.error_message)

    def test_same_company_match_still_links(self):
        seed = self._seed(self.co_a, email_from='same@example.com')
        with self._patch_graph(fetch_lead=self._payload('LGSA', 'same@example.com')):
            lead = self.Ingest.ingest_leadgen(self.page, 'LGSA', 'manual')
        self.assertEqual(lead, seed)
        self.assertEqual(self._logs('LGSA').match_key, 'email')

    def test_same_company_wins_over_blank(self):
        blank = self._seed(False, email_from='both@example.com')
        own = self._seed(self.co_b, email_from='both@example.com')
        with self._patch_graph(fetch_lead=self._payload('LGWB', 'both@example.com')):
            lead = self.Ingest.ingest_leadgen(self.page_b, 'LGWB', 'manual')
        self.assertEqual(lead, own)
        self.assertFalse(blank.meta_leadgen_id)
        self.assertFalse(blank.company_id)

    def test_email_pools_before_phone_pools(self):
        blank = self._seed(False, email_from='order@example.com')
        own = self._seed(self.co_b, phone='5550001111')
        with self._patch_graph(fetch_lead=self._payload(
                'LGEP', 'order@example.com', phone='555-000-1111')):
            lead = self.Ingest.ingest_leadgen(self.page_b, 'LGEP', 'manual')
        self.assertEqual(lead, blank)
        self.assertEqual(blank.company_id, self.co_b)
        self.assertFalse(own.meta_leadgen_id)
        self.assertEqual(self._logs('LGEP').match_key, 'email')

    def test_blank_match_stamped(self):
        blank = self._seed(False, email_from='blank@example.com')
        with self._patch_graph(fetch_lead=self._payload('LGBS', 'blank@example.com')):
            lead = self.Ingest.ingest_leadgen(self.page_b, 'LGBS', 'manual')
        self.assertEqual(lead, blank)
        self.assertEqual(blank.company_id, self.co_b)
        self.assertEqual(blank.meta_leadgen_id, 'LGBS')
        self.assertEqual(self._logs('LGBS').match_key, 'email')

    def test_blank_match_incompatible_creates_new(self):
        blank = self._seed(False, email_from='misfit@example.com',
                           user_id=self.sales_a.id)
        calls, spy = self._spy_writes()
        with self._patch_graph(fetch_lead=self._payload('LGBI', 'misfit@example.com')), \
                spy:
            lead = self.Ingest.ingest_leadgen(self.page_b, 'LGBI', 'manual')
        self.assertNotEqual(lead, blank)
        self.assertEqual(lead.company_id, self.co_b)
        self.assertFalse(blank.company_id)
        self.assertFalse(blank.meta_leadgen_id)
        self.assertFalse([vals for ids, vals in calls
                          if blank.id in ids and 'company_id' in vals])
        self.assertEqual(self._logs('LGBI').match_key, 'none')

    def test_unrelated_validation_error_not_swallowed(self):
        blank = self._seed(False, email_from='strict@example.com')

        def refuse(vals):
            if 'meta_leadgen_id' in vals:
                raise ValidationError('x')
        _calls, spy = self._spy_writes(raise_when=refuse)
        with self._patch_graph(fetch_lead=self._payload('LGVE', 'strict@example.com')), \
                spy, self.assertRaises(ValidationError):
            self.Ingest.ingest_leadgen(self.page_b, 'LGVE', 'manual')
        self.assertFalse(self._lead_by_id('LGVE'))
        self.assertFalse(blank.meta_leadgen_id)

    def test_blank_claim_serialization_failure_falls_back(self):
        blank = self._seed(False, email_from='race@example.com')

        def conflict(vals):
            if 'company_id' in vals:
                raise SerializationFailure('concurrent update')
        _calls, spy = self._spy_writes(raise_when=conflict)
        with self._patch_graph(fetch_lead=self._payload('LGSF', 'race@example.com')), \
                spy:
            lead = self.Ingest.ingest_leadgen(self.page_b, 'LGSF', 'manual')
        self.assertNotEqual(lead, blank)
        self.assertEqual(lead.company_id, self.co_b)
        self.assertEqual(lead.meta_leadgen_id, 'LGSF')
        blank.invalidate_recordset()
        self.assertFalse(blank.company_id)
        self.assertFalse(blank.meta_leadgen_id)
        self.assertFalse(blank.answer_ids)

    def test_set_company_never_overwritten(self):
        own = self._seed(self.co_b, email_from='keep@example.com')
        calls, spy = self._spy_writes()
        with self._patch_graph(fetch_lead=self._payload('LGKC', 'keep@example.com')), \
                spy:
            lead = self.Ingest.ingest_leadgen(self.page_b, 'LGKC', 'manual')
        self.assertEqual(lead, own)
        self.assertEqual(own.company_id, self.co_b)
        self.assertFalse([vals for ids, vals in calls
                          if own.id in ids and 'company_id' in vals])

    def test_email_dedup_underscore_is_literal(self):
        other = self._seed(self.co_a, email_from='john.doe@example.com')
        with self._patch_graph(fetch_lead=self._payload(
                'LGUS1', 'john_doe@example.com')):
            lead = self.Ingest.ingest_leadgen(self.page, 'LGUS1', 'manual')
        self.assertNotEqual(lead, other)
        self.assertFalse(other.meta_leadgen_id)
        self.assertEqual(self._logs('LGUS1').match_key, 'none')
        # An address with an underscore still matches itself, in any case.
        same = self._seed(self.co_a, email_from='mary_ann@example.com')
        with self._patch_graph(fetch_lead=self._payload(
                'LGUS2', 'Mary_Ann@Example.com')):
            again = self.Ingest.ingest_leadgen(self.page, 'LGUS2', 'manual')
        self.assertEqual(again, same)
        self.assertEqual(self._logs('LGUS2').match_key, 'email')

    def test_rejected_email_match_falls_through_to_phone(self):
        # The blank lead's salesperson is in company A, so it can't join B.
        misfit = self._seed(False, email_from='fall@example.com',
                            user_id=self.sales_a.id)
        own = self._seed(self.co_b, phone='5552223333')
        with self._patch_graph(fetch_lead=self._payload(
                'LGFT1', 'fall@example.com', phone='555-222-3333')):
            lead = self.Ingest.ingest_leadgen(self.page_b, 'LGFT1', 'manual')
        self.assertEqual(lead, own)
        self.assertEqual(own.meta_leadgen_id, 'LGFT1')
        self.assertFalse(misfit.company_id)
        self.assertFalse(misfit.meta_leadgen_id)
        self.assertEqual(self._logs('LGFT1').match_key, 'phone')

    def test_claimed_match_falls_through_to_next_lead(self):
        older = self._seed(self.co_b, email_from='twice@example.com')
        # Newer, so crm's order lists it first.
        claimed = self._seed(self.co_b, email_from='twice@example.com',
                             meta_leadgen_id='LGFT_OLD')
        with self._patch_graph(fetch_lead=self._payload(
                'LGFT2', 'twice@example.com')):
            lead = self.Ingest.ingest_leadgen(self.page_b, 'LGFT2', 'manual')
        self.assertEqual(lead, older)
        self.assertEqual(older.meta_leadgen_id, 'LGFT2')
        self.assertEqual(claimed.meta_leadgen_id, 'LGFT_OLD')
        self.assertEqual(self._logs('LGFT2').match_key, 'email')

    def test_failed_blank_claim_falls_through_to_phone(self):
        blank = self._seed(False, email_from='busy@example.com')
        own = self._seed(self.co_b, phone='5554445555')

        def conflict(vals):
            if 'company_id' in vals:
                raise SerializationFailure('concurrent update')
        _calls, spy = self._spy_writes(raise_when=conflict)
        with self._patch_graph(fetch_lead=self._payload(
                'LGFT3', 'busy@example.com', phone='555-444-5555')), spy:
            lead = self.Ingest.ingest_leadgen(self.page_b, 'LGFT3', 'manual')
        self.assertEqual(lead, own)
        blank.invalidate_recordset()
        self.assertFalse(blank.meta_leadgen_id)
        self.assertEqual(self._logs('LGFT3').match_key, 'phone')

    # ---- lead company after later edits ---------------------------------

    def _sales_b(self):
        return new_test_user(
            self.env, 'mc_sales_b',
            groups='base.group_user,sales_team.group_sale_salesman_all_leads',
            company_id=self.co_b.id, company_ids=[Command.set([self.co_b.id])])

    def _set_customer(self, lead, user):
        """Set a customer with no company on ``lead`` as ``user``."""
        customer = self.env['res.partner'].create(
            {'name': 'Walk-in Customer', 'company_id': False})
        lead.with_user(user).write({'partner_id': customer.id})
        self.env.flush_all()
        lead.invalidate_recordset()

    def test_meta_lead_without_salesperson_keeps_company(self):
        # A cron whose user is outside the Page's company leaves the new
        # lead without a salesperson.
        Ingest = self.Ingest.with_user(self.admin_a).sudo()
        with self._patch_graph(fetch_lead=self._payload('LGKC1', 'kc1@example.com')):
            Ingest.ingest_leadgen(self.page_b.sudo(), 'LGKC1', 'webhook')
        lead = self._lead_by_id('LGKC1')
        self.assertEqual(lead.company_id, self.co_b)
        self.assertFalse(lead.user_id)
        self._set_customer(lead, self._sales_b())
        self.assertEqual(lead.company_id, self.co_b)

    def test_stamped_blank_lead_keeps_company(self):
        blank = self._seed(False, email_from='kc2@example.com')
        with self._patch_graph(fetch_lead=self._payload('LGKC2', 'kc2@example.com')):
            lead = self.Ingest.ingest_leadgen(self.page_b, 'LGKC2', 'manual')
        self.assertEqual(lead, blank)
        self.assertEqual(blank.company_id, self.co_b)
        self._set_customer(blank, self._sales_b())
        self.assertEqual(blank.company_id, self.co_b)

    def test_plain_lead_company_rules_unchanged(self):
        # Leads that did not come from Meta keep crm's own behaviour.
        plain = self._seed(self.co_b, email_from='plain@example.com')
        self._set_customer(plain, self._sales_b())
        self.assertFalse(plain.company_id)

    # ---- lost races keep answers out of the server log ------------------

    def _assert_no_answers_logged(self, records, *answers):
        text = '\n'.join(record.getMessage() for record in records)
        for answer in answers:
            self.assertNotIn(answer, text)

    def test_lost_create_race_logs_ids_only(self):
        winner = self.env['crm.lead'].create({
            'name': 'Winner', 'type': 'lead', 'company_id': self.co_a.id,
            'meta_leadgen_id': 'LGQ1'})
        winner.flush_recordset()
        payload = self._payload('LGQ1', 'quiet1@example.com',
                                phone='555-111-2222')
        with self._patch_graph(fetch_lead=payload), \
                mock.patch.object(type(self.Ingest), '_find_by_leadgen_id',
                                  return_value=self.env['crm.lead']), \
                capture_sql_log() as records:
            lead = self.Ingest.ingest_leadgen(self.page, 'LGQ1', 'manual')
        self.assertEqual(lead, winner)
        self.assertEqual(self._logs('LGQ1').status, 'skipped_idempotent')
        self._assert_no_answers_logged(
            records, 'quiet1@example.com', 'Jane Doe', '555-111-2222')

    def test_lost_match_race_logs_ids_only(self):
        winner = self.env['crm.lead'].create({
            'name': 'Winner', 'type': 'lead', 'company_id': self.co_a.id,
            'meta_leadgen_id': 'LGQ2'})
        seed = self._seed(self.co_a, email_from='quiet2@example.com')
        self.env.flush_all()
        with self._patch_graph(fetch_lead=self._payload(
                'LGQ2', 'quiet2@example.com')), \
                mock.patch.object(type(self.Ingest), '_find_by_leadgen_id',
                                  return_value=self.env['crm.lead']), \
                capture_sql_log() as records:
            lead = self.Ingest.ingest_leadgen(self.page, 'LGQ2', 'manual')
        self.assertEqual(lead, winner)
        seed.invalidate_recordset()
        self.assertFalse(seed.meta_leadgen_id)
        self.assertFalse(seed.contact_name)
        self._assert_no_answers_logged(
            records, 'quiet2@example.com', 'Jane Doe')

@tagged('post_install', '-at_install')
class TestBlankClaimConcurrency(BaseCase):
    """Two real transactions claim the same blank-company lead.

    Rows are committed, so everything created here is deleted again in a
    finally block.
    """

    @classmethod
    def setUpClass(cls):
        super().setUpClass()
        cls.registry = Registry(get_db_name())
        # Hex only, and new on every run, so a rerun can't reuse or remove
        # another run's ids.
        cls.token = uuid.uuid4().hex[:10]
        cls.lg_a = 'LGC' + cls.token + '1'
        cls.lg_b = 'LGC' + cls.token + '2'

    def _payload(self, leadgen_id):
        # No campaign: the second transaction can't see the first one's
        # utm.campaign row, and that isn't what this test is about.
        return {'id': leadgen_id, 'created_time': '2026-06-14T10:00:00+0000',
                'platform': 'fb', 'field_data': [
                    {'name': 'email', 'values': ['claim@example.com']},
                    {'name': 'full_name', 'values': ['Claim Test']}]}

    def _ingest(self, cr, page_id, leadgen_id):
        env = api.Environment(cr, SUPERUSER_ID, {})
        Client = type(env['meta.graph.client'])
        with mock.patch.multiple(
                Client,
                fetch_lead=mock.Mock(return_value=self._payload(leadgen_id)),
                resolve_name=mock.Mock(return_value=False)):
            return env['meta.lead.ingest'].ingest_leadgen(
                env['meta.page'].browse(page_id), leadgen_id, 'webhook').id

    def _cleanup(self, ids):
        mine = (self.lg_a, self.lg_b)

        def leads(env):
            env['crm.lead'].with_context(active_test=False).search(
                ['|', ('meta_leadgen_id', 'in', mine),
                 ('id', 'in', ids.get('leads', []))]).unlink()
            # The unlink records each id as removed. Same step, so a retry
            # of a failed unlink also clears what the retry writes.
            env['meta.leadgen.tombstone'].search(
                [('leadgen_id', 'in', mine)]).unlink()
        steps = [
            leads,
            lambda env: env['meta.sync.log'].search(
                [('meta_leadgen_id', 'in', mine)]).unlink(),
            lambda env: env['meta.page'].browse(ids.get('pages', [])).exists().unlink(),
            lambda env: env['meta.account'].browse(ids.get('account', [])).exists().unlink(),
            lambda env: env['res.company'].browse(ids.get('company', [])).exists().unlink(),
            lambda env: env['res.partner'].browse(ids.get('partner', [])).exists().unlink(),
        ]
        self._run_cleanup(steps)

    def _run_cleanup(self, steps):
        # A running server's crons can lock these committed rows for a
        # moment, and the account can't be deleted while its routed Page is
        # left, so failed steps are retried in order a few times.
        for attempt in range(3):
            if not steps:
                return
            if attempt:
                time.sleep(1)
            failed = []
            with self.registry.cursor() as cr:
                env = api.Environment(cr, SUPERUSER_ID, {})
                for step in steps:
                    try:
                        with cr.savepoint():
                            step(env)
                    except Exception:   # noqa: BLE001 - keep cleaning the rest
                        _logger.warning(
                            "TestBlankClaimConcurrency cleanup step failed "
                            "(attempt %s)", attempt + 1, exc_info=True)
                        failed.append(step)
            steps = failed
        if steps:
            # Rows left behind break unrelated tests in later runs, so say
            # so here rather than pass.
            self.fail("TestBlankClaimConcurrency left committed rows behind; "
                      "clean the database before the next run")

    def test_cleanup_failure_fails_the_test(self):
        def stuck(env):
            raise RuntimeError('row still locked')
        with mock.patch.object(time, 'sleep'), mute_logger(__name__), \
                self.assertRaises(AssertionError):
            self._run_cleanup([stuck])

    def test_two_transactions_claim_same_blank_lead(self):
        ids = {}
        try:
            with self.registry.cursor() as cr:
                env = api.Environment(cr, SUPERUSER_ID, {})
                co_a = env.company
                co_b = env['res.company'].create({'name': 'Claim Co B'})
                ids['company'] = [co_b.id]
                ids['partner'] = [co_b.partner_id.id]
                account = env['meta.account'].create({
                    'name': 'Claim Acct',
                    'account_id': 'CLAIMACC' + self.token,
                    'app_secret': 'secret_claim', 'access_token': 'tok_claim',
                    'company_id': co_a.id})
                ids['account'] = [account.id]
                page_a = env['meta.page'].create({
                    'name': 'Claim A', 'page_id': 'PGCA' + self.token,
                    'access_token': 'tok_ca',
                    'account_id': account.id})
                page_b = env['meta.page'].create({
                    'name': 'Claim B', 'page_id': 'PGCB' + self.token,
                    'access_token': 'tok_cb',
                    'account_id': account.id, 'company_id': co_b.id})
                ids['pages'] = [page_a.id, page_b.id]
                blank_vals = {
                    'name': 'Claim blank', 'type': 'lead', 'company_id': False,
                    'user_id': False, 'team_id': False,
                    'email_from': 'claim@example.com'}
                if 'iap_enrich_done' in env['crm.lead']._fields:
                    # Creating a lead triggers the enrichment cron, and a
                    # running server would update this row while the test
                    # holds its snapshots.
                    blank_vals['iap_enrich_done'] = True
                blank = env['crm.lead'].create(blank_vals)
                ids['leads'] = [blank.id]
                co_a_id, co_b_id, blank_id = co_a.id, co_b.id, blank.id

            cr1 = self.registry.cursor()
            cr2 = self.registry.cursor()
            try:
                # Take cr2's snapshot before cr1 claims the lead.
                cr2.execute("SELECT 1")
                self._ingest(cr1, page_a.id, self.lg_a)
                cr1.commit()
                # cr2's claim of the blank lead fails to serialize; that
                # failure is expected and handled by ingest, and the answers
                # must not reach the server log on the way.
                with capture_sql_log() as records:
                    lead_b_id = self._ingest(cr2, page_b.id, self.lg_b)
                cr2.commit()
                logged = '\n'.join(r.getMessage() for r in records)
                self.assertNotIn('claim@example.com', logged)
                self.assertNotIn('Claim Test', logged)
            finally:
                cr1.close()
                cr2.close()
            ids['leads'].append(lead_b_id)

            with self.registry.cursor() as cr:
                env = api.Environment(cr, SUPERUSER_ID, {})
                blank = env['crm.lead'].browse(blank_id)
                self.assertEqual(blank.company_id.id, co_a_id)
                self.assertEqual(blank.meta_leadgen_id, self.lg_a)
                lead_b = env['crm.lead'].browse(lead_b_id)
                self.assertNotEqual(lead_b_id, blank_id)
                self.assertEqual(lead_b.company_id.id, co_b_id)
                self.assertEqual(lead_b.meta_leadgen_id, self.lg_b)
        finally:
            self._cleanup(ids)
