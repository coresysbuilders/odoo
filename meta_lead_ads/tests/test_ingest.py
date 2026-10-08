# Meta Lead Ads Integration for Odoo — © 2026 CoreSys Builders. All rights reserved.
# Licensed under the Odoo Proprietary License v1.0 (OPL-1).
# Unauthorized copying, redistribution, or resale of this software, in whole or in
# part, via any medium, is strictly prohibited and constitutes a license violation.
# OPL-1: https://www.odoo.com/documentation/19.0/legal/licenses.html#odoo-apps

"""Tests for ``meta.lead.ingest.ingest_leadgen`` and the answer capture model.

No network: ``fetch_lead`` and ``resolve_name`` are patched on the
meta.graph.client class, since mock.patch.object can't patch a recordset.
IntegrityError tests run inside a savepoint with the sql_db logger muted.
"""
from unittest import mock

from psycopg2 import IntegrityError

from odoo.tests.common import TransactionCase, tagged
from odoo.tools import mute_logger
from odoo.exceptions import AccessError

from odoo.addons.meta_lead_ads.models.exceptions import MetaPermanentError


class IngestFixtureMixin:
    """Account, page and form fixtures plus helpers to fake the Graph client."""

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
        self.form = self.env['meta.lead.form'].create({
            'name': 'Contact Us', 'form_id': 'F1', 'page_id': self.page.id,
        })
        # Patch targets go on the class; recordsets can't be patched.
        self.Client = type(self.env['meta.graph.client'])
        self.Lead = self.env['crm.lead']
        self.Ingest = self.env['meta.lead.ingest']

    def _fake_lead(self, **over):
        """Return a Graph lead payload; ``over`` replaces top-level keys."""
        data = {
            'id': 'LG1', 'created_time': '2026-06-14T10:00:00+0000',
            'field_data': [
                {'name': 'email', 'values': ['Jane@Example.com']},
                {'name': 'full_name', 'values': ['Jane Doe']},
                {'name': 'phone_number', 'values': ['+1 (555) 123-4567']},
                {'name': 'what_is_your_budget', 'values': ['$5k']},
            ],
            'campaign_id': 'C1', 'campaign_name': 'Summer',
            'adset_id': 'A1', 'adset_name': 'Set',
            'ad_id': 'AD1', 'ad_name': 'Creative',
            'form_id': 'F1', 'platform': 'fb',
        }
        data.update(over)
        return data

    def _resolve_passthrough(self, *a, payload_name=None, **k):
        """Stub for resolve_name that returns the name already in the payload."""
        return payload_name

    def _patch_graph(self, fetch_lead=None, resolve_name=None):
        """Patch fetch_lead and resolve_name on the Graph client class.

        ``fetch_lead`` can be a payload dict or a ready-made Mock.
        """
        if not isinstance(fetch_lead, mock.Mock):
            fetch_lead = mock.Mock(return_value=fetch_lead
                                   if fetch_lead is not None else self._fake_lead())
        if resolve_name is None:
            resolve_name = mock.Mock(side_effect=self._resolve_passthrough)
        self._fetch_lead = fetch_lead
        self._resolve_name = resolve_name
        return mock.patch.multiple(
            self.Client, fetch_lead=fetch_lead, resolve_name=resolve_name)


@tagged('post_install', '-at_install')
class TestIngestPipeline(IngestFixtureMixin, TransactionCase):
    """ingest_leadgen end to end, with the Graph client mocked."""

    # ---- idempotency -----------------------------------------------------

    def test_create_then_idempotent_skip(self):
        """A repeated leadgen_id returns the same lead without a second fetch."""
        fetch = mock.Mock(return_value=self._fake_lead())
        with self._patch_graph(fetch_lead=fetch):
            lead1 = self.Ingest.ingest_leadgen(self.page, 'LG1', 'manual')
            lead2 = self.Ingest.ingest_leadgen(self.page, 'LG1', 'manual')
        self.assertEqual(lead1, lead2)
        self.assertEqual(
            self.Lead.search_count([('meta_leadgen_id', '=', 'LG1')]), 1)
        # The existing-lead lookup short-circuits the second call.
        self.assertEqual(fetch.call_count, 1)
        self.assertTrue(self.env['meta.sync.log'].search_count(
            [('meta_leadgen_id', '=', 'LG1'),
             ('status', '=', 'skipped_idempotent')]))

    def test_concurrency_integrityerror_treated_as_skip(self):
        """The UNIQUE constraint on meta_leadgen_id rejects a second row."""
        self.Lead.create({'name': 'L A', 'meta_leadgen_id': 'LG1'})
        with self.assertRaises(IntegrityError), mute_logger('odoo.sql_db'):
            with self.env.cr.savepoint():
                self.Lead.create({'name': 'L B', 'meta_leadgen_id': 'LG1'})

    def test_concurrency_service_path_skip(self):
        """A create that loses the UNIQUE race returns the existing lead.

        ``_find_by_leadgen_id`` is patched to return nothing, so the service
        tries to insert a row that already exists, as a concurrent worker would.
        """
        existing = self.Lead.create({'name': 'Pre', 'meta_leadgen_id': 'LG1'})
        existing.flush_recordset()
        empty = self.Lead.browse()
        with self._patch_graph(), mute_logger('odoo.sql_db'), \
                mock.patch.object(type(self.Ingest), '_find_by_leadgen_id',
                                  return_value=empty):
            lead = self.Ingest.ingest_leadgen(self.page, 'LG1', 'manual')
        self.assertEqual(lead, existing)
        self.assertEqual(lead.meta_leadgen_id, 'LG1')
        self.assertEqual(
            self.Lead.search_count([('meta_leadgen_id', '=', 'LG1')]), 1)
        self.assertTrue(self.env['meta.sync.log'].search_count(
            [('meta_leadgen_id', '=', 'LG1'),
             ('status', '=', 'skipped_idempotent')]))

    def test_replay_after_dedup_match(self):
        """Replaying a lead that was merged by email is skipped without a fetch."""
        matched = self.Lead.create({
            'name': 'Existing', 'email_from': 'jane@example.com',
            'type': 'lead'})
        with self._patch_graph():
            self.Ingest.ingest_leadgen(self.page, 'LG1', 'manual')
        self.assertEqual(matched.meta_leadgen_id, 'LG1')
        answer_count_1 = len(matched.answer_ids)
        fetch2 = mock.Mock(return_value=self._fake_lead())
        with self._patch_graph(fetch_lead=fetch2):
            self.Ingest.ingest_leadgen(self.page, 'LG1', 'manual')
        self.assertEqual(fetch2.call_count, 0)
        self.assertEqual(len(matched.answer_ids), answer_count_1)
        self.assertEqual(
            self.Lead.search_count([('meta_leadgen_id', '=', 'LG1')]), 1)
        self.assertTrue(self.env['meta.sync.log'].search_count(
            [('meta_leadgen_id', '=', 'LG1'),
             ('status', '=', 'skipped_idempotent')]))

    def test_dedup_match_with_existing_different_leadgen_id_creates_new(self):
        """A lead already tied to another leadgen_id is not merged into."""
        old = self.Lead.create({
            'name': 'Old', 'email_from': 'jane@example.com',
            'meta_leadgen_id': 'LGOLD', 'type': 'lead'})
        before = self.Lead.search_count(
            [('email_from', '=ilike', 'jane@example.com')])
        payload = self._fake_lead(id='LGNEW', field_data=[
            {'name': 'email', 'values': ['jane@example.com']},
            {'name': 'full_name', 'values': ['Jane Doe']}])
        with self._patch_graph(fetch_lead=payload):
            new = self.Ingest.ingest_leadgen(self.page, 'LGNEW', 'manual')
        after = self.Lead.search_count(
            [('email_from', '=ilike', 'jane@example.com')])
        self.assertEqual(after, before + 1)
        self.assertEqual(old.meta_leadgen_id, 'LGOLD')
        self.assertNotEqual(new, old)
        self.assertEqual(new.meta_leadgen_id, 'LGNEW')
        log = self.env['meta.sync.log'].search(
            [('meta_leadgen_id', '=', 'LGNEW')], limit=1)
        self.assertEqual(log.status, 'success')
        self.assertEqual(log.match_key, 'none')

    def test_two_distinct_meta_leads_same_email_preserve_idempotency_for_both(self):
        """Two Meta leads with the same email each stay idempotent on replay."""
        seed = self.Lead.create({
            'name': 'Seed', 'email_from': 'jane@example.com', 'type': 'lead'})
        # LG1 merges into the seed lead, which has no leadgen_id yet.
        with self._patch_graph(fetch_lead=self._fake_lead(id='LG1')):
            self.Ingest.ingest_leadgen(self.page, 'LG1', 'manual')
        self.assertEqual(seed.meta_leadgen_id, 'LG1')
        lg1_answers = len(seed.answer_ids)
        # LG2 gets its own lead because the seed now belongs to LG1.
        with self._patch_graph(fetch_lead=self._fake_lead(
                id='LG2', field_data=[
                    {'name': 'email', 'values': ['jane@example.com']},
                    {'name': 'full_name', 'values': ['Jane Doe']}])):
            lead2 = self.Ingest.ingest_leadgen(self.page, 'LG2', 'manual')
        self.assertEqual(
            self.Lead.search_count([('email_from', '=ilike',
                                     'jane@example.com')]), 2)
        self.assertEqual(seed.meta_leadgen_id, 'LG1')
        self.assertEqual(lead2.meta_leadgen_id, 'LG2')
        # Replaying either id must not fetch again or add answers.
        refetch1 = mock.Mock(return_value=self._fake_lead(id='LG1'))
        with self._patch_graph(fetch_lead=refetch1):
            self.Ingest.ingest_leadgen(self.page, 'LG1', 'manual')
        self.assertEqual(refetch1.call_count, 0)
        self.assertEqual(len(seed.answer_ids), lg1_answers)
        refetch2 = mock.Mock(return_value=self._fake_lead(id='LG2'))
        with self._patch_graph(fetch_lead=refetch2):
            self.Ingest.ingest_leadgen(self.page, 'LG2', 'manual')
        self.assertEqual(refetch2.call_count, 0)
        self.assertEqual(
            self.Lead.search_count([('meta_leadgen_id', '=', 'LG1')]), 1)
        self.assertEqual(
            self.Lead.search_count([('meta_leadgen_id', '=', 'LG2')]), 1)

    # ---- field mapping ---------------------------------------------------

    def test_type_is_lead(self):
        """An ingested lead is created with type='lead'."""
        with self._patch_graph():
            lead = self.Ingest.ingest_leadgen(self.page, 'LG1', 'manual')
        self.assertEqual(lead.type, 'lead')

    def test_canonical_field_map(self):
        """Standard Meta keys land on the matching crm.lead fields."""
        payload = self._fake_lead(field_data=[
            {'name': 'email', 'values': ['Jane@Example.com']},
            {'name': 'full_name', 'values': ['Jane Doe']},
            {'name': 'phone_number', 'values': ['+1 (555) 123-4567']},
            {'name': 'company_name', 'values': ['Acme Corp']}])
        with self._patch_graph(fetch_lead=payload):
            lead = self.Ingest.ingest_leadgen(self.page, 'LG1', 'manual')
        self.assertEqual(lead.contact_name, 'Jane Doe')
        self.assertTrue((lead.email_from or '').lower() == 'jane@example.com')
        self.assertIn('555', lead.phone or '')
        self.assertEqual(lead.partner_name, 'Acme Corp')

    def test_per_form_override_map(self):
        """A per-form mapping sends a custom question to its crm.lead field."""
        crm_field = self.env['ir.model.fields'].search(
            [('model', '=', 'crm.lead'), ('name', '=', 'function')], limit=1)
        self.env['meta.field.mapping'].create({
            'form_id': self.form.id, 'meta_key': 'job_title',
            'crm_field_id': crm_field.id})
        payload = self._fake_lead(field_data=[
            {'name': 'email', 'values': ['jane@example.com']},
            {'name': 'full_name', 'values': ['Jane Doe']},
            {'name': 'job_title', 'values': ['CTO']}])
        with self._patch_graph(fetch_lead=payload):
            lead = self.Ingest.ingest_leadgen(self.page, 'LG1', 'manual')
        self.assertEqual(lead.function, 'CTO')

    def test_override_cannot_remap_canonical(self):
        """A per-form mapping can't redirect a standard key such as email."""
        website_field = self.env['ir.model.fields'].search(
            [('model', '=', 'crm.lead'), ('name', '=', 'website')], limit=1)
        self.env['meta.field.mapping'].create({
            'form_id': self.form.id, 'meta_key': 'email',
            'crm_field_id': website_field.id})
        payload = self._fake_lead(field_data=[
            {'name': 'email', 'values': ['jane@example.com']},
            {'name': 'full_name', 'values': ['Jane Doe']}])
        with self._patch_graph(fetch_lead=payload):
            lead = self.Ingest.ingest_leadgen(self.page, 'LG1', 'manual')
        self.assertTrue((lead.email_from or '').lower() == 'jane@example.com')
        self.assertNotEqual((lead.website or '').lower(), 'jane@example.com')

    def test_unmapped_question_captured(self):
        """Unmapped answers are kept as answer rows, in the description and log."""
        with self._patch_graph():
            lead = self.Ingest.ingest_leadgen(self.page, 'LG1', 'manual')
        budget = lead.answer_ids.filtered(
            lambda a: a.question_key == 'what_is_your_budget')
        self.assertTrue(budget)
        self.assertEqual(budget[0].value, '$5k')
        self.assertIn('$5k', lead.description or '')
        log = self.env['meta.sync.log'].search(
            [('meta_leadgen_id', '=', 'LG1'), ('status', '=', 'success')],
            limit=1)
        self.assertTrue(log.raw_payload)

    # ---- attribution -----------------------------------------------------

    def test_attribution_names_no_graph_call(self):
        """Names present in the payload are used without calling resolve_name."""
        resolve = mock.Mock(side_effect=self._resolve_passthrough)
        with self._patch_graph(resolve_name=resolve):
            lead = self.Ingest.ingest_leadgen(self.page, 'LG1', 'manual')
        self.assertEqual(lead.meta_campaign_name, 'Summer')
        self.assertEqual(lead.meta_adset_name, 'Set')
        self.assertEqual(lead.meta_ad_name, 'Creative')
        self.assertEqual(resolve.call_count, 0)

    def test_partial_attribution_resolve_only_missing(self):
        """resolve_name is only called for names missing from the payload."""
        payload = self._fake_lead()
        payload.pop('adset_name', None)
        payload.pop('ad_name', None)
        resolve = mock.Mock(side_effect=lambda *a, payload_name=None, **k:
                            payload_name or 'resolved')
        with self._patch_graph(fetch_lead=payload, resolve_name=resolve):
            self.Ingest.ingest_leadgen(self.page, 'LG1', 'manual')
        for call in resolve.call_args_list:
            self.assertNotIn('campaign', call.args)
        # At most one call each for adset, ad and form.
        self.assertTrue(resolve.call_count >= 1)
        self.assertTrue(resolve.call_count <= 3)

    def test_utm_get_or_create_no_dup(self):
        """Two leads in one campaign share a single utm.campaign record."""
        with self._patch_graph(fetch_lead=self._fake_lead(id='LGA', field_data=[
                {'name': 'email', 'values': ['a@example.com']}])):
            lead_a = self.Ingest.ingest_leadgen(self.page, 'LGA', 'manual')
        with self._patch_graph(fetch_lead=self._fake_lead(id='LGB', field_data=[
                {'name': 'email', 'values': ['b@example.com']}])):
            self.Ingest.ingest_leadgen(self.page, 'LGB', 'manual')
        self.assertEqual(self.env['utm.campaign'].search_count(
            [('name', '=ilike', 'Summer')]), 1)
        self.assertEqual(lead_a.source_id,
                         self.env.ref('utm.utm_source_facebook'))
        self.assertEqual(
            lead_a.medium_id,
            self.env.ref('meta_lead_ads.utm_medium_paid_social'))

    # ---- dedup -----------------------------------------------------------

    def test_dedup_email_branch(self):
        """An open lead with the same email is updated instead of duplicated."""
        existing = self.Lead.create({
            'name': 'Existing', 'email_from': 'jane@example.com',
            'phone': False, 'type': 'lead'})
        before = self.Lead.search_count([])
        with self._patch_graph():
            lead = self.Ingest.ingest_leadgen(self.page, 'LG1', 'manual')
        self.assertEqual(lead, existing)
        self.assertEqual(self.Lead.search_count([]), before)
        self.assertIn('555', existing.phone or '')
        self.assertTrue(existing.answer_ids)
        self.assertEqual(existing.meta_leadgen_id, 'LG1')
        log = self.env['meta.sync.log'].search(
            [('meta_leadgen_id', '=', 'LG1')], limit=1)
        self.assertEqual(log.match_key, 'email')

    def test_dedup_phone_branch(self):
        """Without an email, a differently formatted phone still matches."""
        existing = self.Lead.create({
            'name': 'Existing', 'phone': '5551234567', 'type': 'lead'})
        payload = self._fake_lead(field_data=[
            {'name': 'phone_number', 'values': ['+1 (555) 123-4567']},
            {'name': 'full_name', 'values': ['Jane Doe']}])
        before = self.Lead.search_count([])
        with self._patch_graph(fetch_lead=payload):
            lead = self.Ingest.ingest_leadgen(self.page, 'LG1', 'manual')
        self.assertEqual(lead, existing)
        self.assertEqual(self.Lead.search_count([]), before)
        log = self.env['meta.sync.log'].search(
            [('meta_leadgen_id', '=', 'LG1')], limit=1)
        self.assertEqual(log.match_key, 'phone')

    def test_dedup_archived_creates_new(self):
        """An archived lead with the same email is not reused."""
        self.Lead.create({
            'name': 'Archived', 'email_from': 'jane@example.com',
            'active': False, 'type': 'lead'})
        before = self.Lead.search_count(
            ['|', ('active', '=', True), ('active', '=', False),
             ('email_from', '=ilike', 'jane@example.com')])
        with self._patch_graph():
            lead = self.Ingest.ingest_leadgen(self.page, 'LG1', 'manual')
        after = self.Lead.search_count(
            ['|', ('active', '=', True), ('active', '=', False),
             ('email_from', '=ilike', 'jane@example.com')])
        self.assertEqual(after, before + 1)
        self.assertTrue(lead.active)
        self.assertEqual(lead.meta_leadgen_id, 'LG1')

    def test_dedup_won_creates_new(self):
        """A won lead with the same email is not reused."""
        won_stage = self.env['crm.stage'].search(
            [('is_won', '=', True)], limit=1)
        if not won_stage:
            won_stage = self.env['crm.stage'].create(
                {'name': 'Won', 'is_won': True})
        self.Lead.create({
            'name': 'Won', 'email_from': 'jane@example.com',
            'stage_id': won_stage.id, 'active': True, 'type': 'lead'})
        before = self.Lead.search_count(
            [('email_from', '=ilike', 'jane@example.com')])
        with self._patch_graph():
            lead = self.Ingest.ingest_leadgen(self.page, 'LG1', 'manual')
        after = self.Lead.search_count(
            [('email_from', '=ilike', 'jane@example.com')])
        self.assertEqual(after, before + 1)
        self.assertEqual(lead.meta_leadgen_id, 'LG1')

    def test_enrich_not_clobber(self):
        """Merging only fills blank fields; hand-edited values are kept."""
        existing = self.Lead.create({
            'name': 'Existing', 'phone': '5551234567',
            'email_from': 'edited@hand.example',
            'contact_name': 'Hand Edited', 'type': 'lead'})
        payload = self._fake_lead(field_data=[
            {'name': 'phone_number', 'values': ['+1 (555) 123-4567']},
            {'name': 'email', 'values': ['jane@example.com']},
            {'name': 'full_name', 'values': ['Jane Doe']}])
        with self._patch_graph(fetch_lead=payload):
            self.Ingest.ingest_leadgen(self.page, 'LG1', 'manual')
        self.assertEqual(existing.email_from, 'edited@hand.example')
        self.assertEqual(existing.contact_name, 'Hand Edited')

    # ---- contactless leads + synthetic subject ---------------------------

    def test_contactless_lead_created_success(self):
        """A lead with no email or phone is still created, not dropped."""
        payload = self._fake_lead(field_data=[
            {'name': 'what_is_your_budget', 'values': ['$5k']}])
        with self._patch_graph(fetch_lead=payload):
            lead = self.Ingest.ingest_leadgen(self.page, 'LG1', 'manual')
        self.assertTrue(lead)
        self.assertTrue(lead.name)
        log = self.env['meta.sync.log'].search(
            [('meta_leadgen_id', '=', 'LG1')], limit=1)
        self.assertEqual(log.status, 'success')

    def test_synthetic_subject(self):
        """The lead name is built from form and date; the person goes in
        contact_name. The regex checks the exact bullet separator."""
        with self._patch_graph():
            lead = self.Ingest.ingest_leadgen(self.page, 'LG1', 'manual')
        self.assertRegex(
            lead.name, r'^Meta Lead • .* • \d{4}-\d{2}-\d{2}$')
        self.assertEqual(lead.contact_name, 'Jane Doe')

    # ---- input guard -----------------------------------------------------

    def test_leadgen_id_rejected(self):
        """A leadgen_id with '/' or only spaces is rejected before any fetch."""
        fetch = mock.Mock(return_value=self._fake_lead())
        with self._patch_graph(fetch_lead=fetch):
            with self.assertRaises(MetaPermanentError):
                self.Ingest.ingest_leadgen(self.page, '123/abc', 'manual')
            with self.assertRaises(MetaPermanentError):
                self.Ingest.ingest_leadgen(self.page, '   ', 'manual')
        self.assertEqual(fetch.call_count, 0)
        self.assertEqual(
            self.Lead.search_count([('meta_leadgen_id', '=', '123/abc')]), 0)


@tagged('post_install', '-at_install')
class TestIngestWizard(IngestFixtureMixin, TransactionCase):
    """The admin-only manual ingest wizard."""

    def test_wizard_calls_service(self):
        """The wizard ingests with trigger 'manual' and opens the lead."""
        with self._patch_graph():
            wizard = self.env['meta.ingest.leadgen'].create({
                'page_id': self.page.id, 'leadgen_id': 'LG1'})
            action = wizard.action_ingest()
        self.assertIsInstance(action, dict)
        self.assertEqual(action.get('res_model'), 'crm.lead')
        res_id = action.get('res_id')
        if not res_id and action.get('domain'):
            res_id = self.Lead.search(action['domain'], limit=1).id
        self.assertTrue(res_id)
        self.assertEqual(self.Lead.browse(res_id).meta_leadgen_id, 'LG1')
        self.assertTrue(self.env['meta.sync.log'].search_count(
            [('meta_leadgen_id', '=', 'LG1'), ('trigger', '=', 'manual')]))

    def test_wizard_admin_gated(self):
        """A plain Meta user gets AccessError on the wizard."""
        base_internal = self.env.ref('base.group_user')
        meta_user_group = self.env.ref('meta_lead_ads.group_meta_user')
        non_admin = self.env['res.users'].create({
            'name': 'WZ User', 'login': 'wz_user',
            'group_ids': [(6, 0, [base_internal.id, meta_user_group.id])]})
        with self.assertRaises(AccessError):
            self.env['meta.ingest.leadgen'].with_user(non_admin).create({
                'page_id': self.page.id, 'leadgen_id': 'LG1'})
