# Meta Lead Ads Integration for Odoo — © 2026 CoreSys Builders. All rights reserved.
# Licensed under the Odoo Proprietary License v1.0 (OPL-1).
# Unauthorized copying, redistribution, or resale of this software, in whole or in
# part, via any medium, is strictly prohibited and constitutes a license violation.
# OPL-1: https://www.odoo.com/documentation/18.0/legal/licenses.html#odoo-apps

"""Tests for the idempotent lead ingestion service.

Pins the contract of
``meta.lead.ingest.ingest_leadgen(page, leadgen_id, trigger='webhook', raw=None)``
and the ``meta.lead.answer`` capture model. The whole pipeline runs with no
network: ``fetch_lead`` and ``resolve_name`` are mocked on
``type(self.env['meta.graph.client'])`` (the class, never a recordset --
recordsets are read-only to ``mock.patch.object``).

Conventions used throughout:
  - ``assertRaises`` takes a single exception class, never a tuple.
  - Use ``search_count(...)``; the legacy ``count=`` kwarg on ``search`` was
    removed in Odoo 18.
  - A group-gated read raises ``AccessError`` rather than silently omitting.
  - Patch the Graph client method on ``type(client)`` (the class), never on a
    recordset.
  - IntegrityError tests use a savepoint + ``mute_logger('odoo.sql_db')``.

Scenarios covered include: replay after a dedup match (no re-fetch, no
duplicate answers, leadgen id stamped); concurrency through the service
(savepoint catch -> skipped_idempotent); a per-form override cannot remap a
canonical key; a malformed leadgen_id is rejected before any Graph call;
open-only scope (archived/won leads spawn a new lead); partial attribution
resolves only the missing names; a match already carrying a different
leadgen_id spawns a new lead with the old id untouched; and two distinct Meta
leads on one email stay independently idempotent.
"""
from unittest import mock

from psycopg2 import IntegrityError

from odoo.tests.common import TransactionCase, tagged
from odoo.tools import mute_logger
from odoo.exceptions import AccessError

from odoo.addons.meta_lead_ads.models.exceptions import MetaPermanentError


class IngestFixtureMixin:
    """Shared fixture chain + Graph-client class-patch idiom for both test
    classes. Builds meta.account -> meta.page -> meta.lead.form and exposes
    ``self.Client`` (the meta.graph.client class) and ``_fake_lead``."""

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
        # Patch the Graph client on the class, never on a recordset.
        self.Client = type(self.env['meta.graph.client'])
        self.Lead = self.env['crm.lead']
        self.Ingest = self.env['meta.lead.ingest']

    def _fake_lead(self, **over):
        """Return a Graph lead-read payload dict. ``over`` shallow-overrides
        top-level keys (e.g. ``id='LG2'``, ``field_data=[...]``)."""
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
        """resolve_name stub: echo the payload name so a payload-carried *_name
        triggers zero Graph traffic."""
        return payload_name

    def _patch_graph(self, fetch_lead=None, resolve_name=None):
        """Return a context manager patching both Graph methods on the class.
        ``fetch_lead`` may be a return value (dict) OR a mock.Mock; default
        ``resolve_name`` echoes the payload name."""
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
    """End-to-end behavior of ingest_leadgen with a fully mocked Graph
    client."""

    # ---- idempotency -----------------------------------------------------

    def test_create_then_idempotent_skip(self):
        """Ingesting the same leadgen_id twice yields one lead; the second call
        does not re-fetch and logs skipped_idempotent."""
        fetch = mock.Mock(return_value=self._fake_lead())
        with self._patch_graph(fetch_lead=fetch):
            lead1 = self.Ingest.ingest_leadgen(self.page, 'LG1', 'manual')
            lead2 = self.Ingest.ingest_leadgen(self.page, 'LG1', 'manual')
        self.assertEqual(lead1, lead2)
        self.assertEqual(
            self.Lead.search_count([('meta_leadgen_id', '=', 'LG1')]), 1)
        # The pre-check short-circuits the 2nd ingest -> only one Graph fetch.
        self.assertEqual(fetch.call_count, 1)
        self.assertTrue(self.env['meta.sync.log'].search_count(
            [('meta_leadgen_id', '=', 'LG1'),
             ('status', '=', 'skipped_idempotent')]))

    def test_concurrency_integrityerror_treated_as_skip(self):
        """The DB UNIQUE contract: a direct second create with the same
        leadgen_id raises IntegrityError on flush. Pins that the constraint
        exists; the service-path catch is the next test."""
        self.Lead.create({'name': 'L A', 'meta_leadgen_id': 'LG1'})
        with self.assertRaises(IntegrityError), mute_logger('odoo.sql_db'):
            with self.env.cr.savepoint():
                self.Lead.create({'name': 'L B', 'meta_leadgen_id': 'LG1'})

    def test_concurrency_service_path_skip(self):
        """Force ingest past its pre-check so the create/flush path itself
        races the UNIQUE constraint: the service catches IntegrityError,
        re-reads the existing lead, logs skipped_idempotent, no duplicate.

        The pre-check seam ``meta.lead.ingest._find_by_leadgen_id(leadgen_id)``
        returns a crm.lead recordset (empty when unseen). Patching it to return
        an empty recordset once drives the create path even though the row
        already exists."""
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
        """First ingest matches by email and stamps meta_leadgen_id;
        re-ingesting the same id hits the pre-check -> no re-fetch, no duplicate
        answers, skipped_idempotent."""
        matched = self.Lead.create({
            'name': 'Existing', 'email_from': 'jane@example.com',
            'type': 'lead'})
        with self._patch_graph():
            self.Ingest.ingest_leadgen(self.page, 'LG1', 'manual')
        self.assertEqual(matched.meta_leadgen_id, 'LG1')   # stamped
        answer_count_1 = len(matched.answer_ids)
        fetch2 = mock.Mock(return_value=self._fake_lead())
        with self._patch_graph(fetch_lead=fetch2):
            self.Ingest.ingest_leadgen(self.page, 'LG1', 'manual')
        self.assertEqual(fetch2.call_count, 0)             # no re-fetch
        self.assertEqual(len(matched.answer_ids), answer_count_1)  # no dup
        self.assertEqual(
            self.Lead.search_count([('meta_leadgen_id', '=', 'LG1')]), 1)
        self.assertTrue(self.env['meta.sync.log'].search_count(
            [('meta_leadgen_id', '=', 'LG1'),
             ('status', '=', 'skipped_idempotent')]))

    def test_dedup_match_with_existing_different_leadgen_id_creates_new(self):
        """An open lead already carrying a different leadgen_id (LGOLD) is not a
        usable merge target for LGNEW (same email) -> a new lead is created and
        the old lead's id is left untouched."""
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
        self.assertEqual(after, before + 1)                # a NEW lead created
        self.assertEqual(old.meta_leadgen_id, 'LGOLD')     # NOT overwritten
        self.assertNotEqual(new, old)
        self.assertEqual(new.meta_leadgen_id, 'LGNEW')
        log = self.env['meta.sync.log'].search(
            [('meta_leadgen_id', '=', 'LGNEW')], limit=1)
        self.assertEqual(log.status, 'success')
        self.assertEqual(log.match_key, 'none')            # created, not merged

    def test_two_distinct_meta_leads_same_email_preserve_idempotency_for_both(self):
        """LG1 merges onto an un-stamped open lead; LG2 (same email) spawns a
        second lead; re-ingesting either is skipped_idempotent with no re-fetch
        and no duplicate answers; idempotency holds independently for both
        ids."""
        seed = self.Lead.create({
            'name': 'Seed', 'email_from': 'jane@example.com', 'type': 'lead'})
        # (1) LG1 merges onto the un-stamped lead.
        with self._patch_graph(fetch_lead=self._fake_lead(id='LG1')):
            self.Ingest.ingest_leadgen(self.page, 'LG1', 'manual')
        self.assertEqual(seed.meta_leadgen_id, 'LG1')
        lg1_answers = len(seed.answer_ids)
        # (2) LG2 same email -> a SECOND lead (seed is already Meta-claimed).
        with self._patch_graph(fetch_lead=self._fake_lead(
                id='LG2', field_data=[
                    {'name': 'email', 'values': ['jane@example.com']},
                    {'name': 'full_name', 'values': ['Jane Doe']}])):
            lead2 = self.Ingest.ingest_leadgen(self.page, 'LG2', 'manual')
        self.assertEqual(
            self.Lead.search_count([('email_from', '=ilike',
                                     'jane@example.com')]), 2)
        self.assertEqual(seed.meta_leadgen_id, 'LG1')      # id unchanged
        self.assertEqual(lead2.meta_leadgen_id, 'LG2')
        # (3) re-ingest LG1 -> skipped, no re-fetch, no dup answers.
        refetch1 = mock.Mock(return_value=self._fake_lead(id='LG1'))
        with self._patch_graph(fetch_lead=refetch1):
            self.Ingest.ingest_leadgen(self.page, 'LG1', 'manual')
        self.assertEqual(refetch1.call_count, 0)
        self.assertEqual(len(seed.answer_ids), lg1_answers)
        # (4) re-ingest LG2 -> likewise.
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
        """Canonical keys always map -- full_name->contact_name,
        email->email_from, phone_number->phone, company_name->partner_name."""
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
        """A per-form override of a new meta_key lands on its crm field
        (overrides add mappings)."""
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
        """A per-form override that tries to remap the canonical key 'email'
        does not redirect the canonical field -- canonical wins."""
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
        """An unmapped question -> a meta.lead.answer row + a description line;
        the raw payload is preserved in the success sync-log row."""
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
        """*_name populated straight from the payload with zero resolve_name
        calls."""
        resolve = mock.Mock(side_effect=self._resolve_passthrough)
        with self._patch_graph(resolve_name=resolve):
            lead = self.Ingest.ingest_leadgen(self.page, 'LG1', 'manual')
        self.assertEqual(lead.meta_campaign_name, 'Summer')
        self.assertEqual(lead.meta_adset_name, 'Set')
        self.assertEqual(lead.meta_ad_name, 'Creative')
        self.assertEqual(resolve.call_count, 0)

    def test_partial_attribution_resolve_only_missing(self):
        """resolve_name is called only for the missing names; zero calls for
        present ones. Payload carries campaign_name but omits adset_name /
        ad_name / form name."""
        payload = self._fake_lead()
        payload.pop('adset_name', None)
        payload.pop('ad_name', None)
        resolve = mock.Mock(side_effect=lambda *a, payload_name=None, **k:
                            payload_name or 'resolved')
        with self._patch_graph(fetch_lead=payload, resolve_name=resolve):
            self.Ingest.ingest_leadgen(self.page, 'LG1', 'manual')
        # campaign_name was present -> never resolved.
        for call in resolve.call_args_list:
            self.assertNotIn('campaign', call.args)
        # one resolve per missing name (adset, ad, form) -> non-zero, small.
        self.assertTrue(resolve.call_count >= 1)
        self.assertTrue(resolve.call_count <= 3)

    def test_utm_get_or_create_no_dup(self):
        """Two leads in the same campaign -> one utm.campaign; FB -> stock
        source; medium -> seeded Paid Social. Two distinct emails so dedup
        does not interfere."""
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
        """Match an open un-stamped lead by email, enrich blank fields only,
        append answers, match_key=email, and stamp meta_leadgen_id on the
        matched lead."""
        existing = self.Lead.create({
            'name': 'Existing', 'email_from': 'jane@example.com',
            'phone': False, 'type': 'lead'})
        before = self.Lead.search_count([])
        with self._patch_graph():
            lead = self.Ingest.ingest_leadgen(self.page, 'LG1', 'manual')
        self.assertEqual(lead, existing)                   # linked, not created
        self.assertEqual(self.Lead.search_count([]), before)
        self.assertIn('555', existing.phone or '')         # blank field filled
        self.assertTrue(existing.answer_ids)               # answers appended
        self.assertEqual(existing.meta_leadgen_id, 'LG1')  # stamped
        log = self.env['meta.sync.log'].search(
            [('meta_leadgen_id', '=', 'LG1')], limit=1)
        self.assertEqual(log.match_key, 'email')

    def test_dedup_phone_branch(self):
        """Match by normalized phone (+1 (555)... vs 5551234567) when there is
        no email; match_key=phone."""
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
        """Same email on an archived (active=False) lead -> a new lead is
        created (matching is open-only)."""
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
        """Same email on a won (stage_id.is_won) active lead -> a new lead is
        created, not the won one matched (matching is open-only)."""
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
        """Matching by phone must not overwrite a non-empty salesperson-edited
        email_from / contact_name -- only blank fields are filled."""
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
        """No email and no phone -> a lead is still created, sync-log
        status=success, synthetic name set. Never drop a real paid lead."""
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
        """The subject is always "Meta Lead • {form} • {date}" regardless of
        contact data; the submitter's name lives in contact_name. The separator
        is asserted literally (•) so a regression to another glyph is
        caught."""
        with self._patch_graph():
            lead = self.Ingest.ingest_leadgen(self.page, 'LG1', 'manual')
        self.assertRegex(
            lead.name, r'^Meta Lead • .* • \d{4}-\d{2}-\d{2}$')
        self.assertEqual(lead.contact_name, 'Jane Doe')

    # ---- input guard -----------------------------------------------------

    def test_leadgen_id_rejected(self):
        """A '/'-bearing or whitespace-only leadgen_id is rejected with
        MetaPermanentError before any Graph call; fetch_lead is not called and
        no lead is created for that id."""
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
    """The admin-only "Ingest by leadgen_id" wizard (meta.ingest.leadgen) is a
    thin wrapper over ingest_leadgen('manual') and is admin-gated."""

    def test_wizard_calls_service(self):
        """The wizard routes through ingest_leadgen(..., 'manual'), returns an
        act_window pointing at the created/returned crm.lead, and writes a
        sync-log row with trigger='manual'."""
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
        """The wizard model is admin-only: a non-admin Meta User
        (group_meta_user only) cannot use it -> AccessError."""
        base_internal = self.env.ref('base.group_user')
        meta_user_group = self.env.ref('meta_lead_ads.group_meta_user')
        non_admin = self.env['res.users'].create({
            'name': 'WZ User', 'login': 'wz_user',
            'groups_id': [(6, 0, [base_internal.id, meta_user_group.id])]})
        with self.assertRaises(AccessError):
            self.env['meta.ingest.leadgen'].with_user(non_admin).create({
                'page_id': self.page.id, 'leadgen_id': 'LG1'})
