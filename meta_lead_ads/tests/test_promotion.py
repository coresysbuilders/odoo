# Meta Lead Ads Integration for Odoo — © 2026 CoreSys Builders. All rights reserved.
# Licensed under the Odoo Proprietary License v1.0 (OPL-1).
# Unauthorized copying, redistribution, or resale of this software, in whole or in
# part, via any medium, is strictly prohibited and constitutes a license violation.
# OPL-1: https://www.odoo.com/documentation/19.0/legal/licenses.html#odoo-apps

"""Tests for promoting a captured Meta question to a custom crm.lead field.

Covers the ``meta.promote.answer`` wizard (``action_promote``), the answer-line
entry ``action_promote_to_field`` and the ``_derive_tech_name`` sanitizer.

Promotion creates a manual field and calls ``registry.setup_models`` inline.
The registry lives at process level, so the test cursor rollback does not
remove the new ``x_meta_<key>`` field from ``_fields``. Each test therefore
derives its own field name from the method name (``self.K`` / ``self.T``) so
leftover registry entries never collide. Production isn't affected: each
promote is its own request and Odoo signals the registry reload to other
workers.
"""
from odoo.tests.common import TransactionCase, tagged
from odoo.exceptions import UserError


@tagged('post_install', '-at_install')
class TestPromotion(TransactionCase):
    # ---- fixtures --------------------------------------------------------

    def setUp(self):
        super().setUp()
        # Promotion checks has_group inside the method; act as a Meta admin.
        # The non-admin path is covered by TestPromotionSecurity.
        # Odoo 19 renamed res.users.groups_id to group_ids.
        self.env.user.group_ids |= self.env.ref(
            'meta_lead_ads.group_meta_admin')
        # Question key unique to this test, so the leftover registry fields
        # from other tests can't collide with it.
        self.K = 'q_%s' % self._testMethodName
        self.T = self.env['meta.promote.answer']._derive_tech_name(self.K)

    def _form(self):
        """Create an account, page and form with ids unique per call."""
        n = getattr(self, '_form_seq', 0) + 1
        self._form_seq = n
        acc = self.env['meta.account'].create(
            {'name': 'A%d' % n, 'account_id': 'A%d' % n})
        page = self.env['meta.page'].create(
            {'name': 'P%d' % n, 'page_id': 'P%d' % n, 'account_id': acc.id})
        return self.env['meta.lead.form'].create(
            {'name': 'F%d' % n, 'form_id': 'F%d' % n, 'page_id': page.id})

    def _lead_with_answer(self, form, question_key=None, label='Budget',
                          value='$5k', form_ref=None):
        """Create a lead with one answer row, linked to its source form.

        ``question_key`` defaults to ``self.K``. Pass ``form_ref=False`` to
        leave the lead without a source form.
        """
        if question_key is None:
            question_key = self.K
        ref = form if form_ref is None else form_ref
        lead = self.env['crm.lead'].create({
            'name': 'Lead', 'type': 'lead',
            'meta_form_id_ref': ref.id if ref else False,
        })
        self.env['meta.lead.answer'].create({
            'lead_id': lead.id, 'question_key': question_key,
            'label': label, 'value': value,
        })
        return lead

    def _answer(self, lead):
        """Return the lead's first answer row."""
        return lead.answer_ids[:1]

    def _promote(self, lead, user=None):
        """Open the promote wizard on the lead's first answer and run it."""
        answer = self._answer(lead)
        Wizard = self.env['meta.promote.answer']
        if user is not None:
            Wizard = Wizard.with_user(user)
        wizard = Wizard.create({
            'answer_id': answer.id,
            'lead_id': lead.id,
        })
        return wizard.action_promote()

    def _new_field(self, tech_name):
        """Return the crm.lead ir.model.fields row for ``tech_name``, if any."""
        return self.env['ir.model.fields'].search([
            ('model', '=', 'crm.lead'), ('name', '=', tech_name)], limit=1)

    def _custom_view_name(self, tech_name):
        """Return the name promotion gives the field's inherited view."""
        return 'crm.lead.form.meta.custom.%s' % tech_name

    # ---- field creation --------------------------------------------------

    def test_promote_creates_manual_char_field(self):
        """Promotion creates a stored manual char field x_meta_<key> on crm.lead."""
        form = self._form()
        lead = self._lead_with_answer(form)
        self._promote(lead)
        field = self._new_field(self.T)
        self.assertTrue(field, "expected %s field on crm.lead" % self.T)
        self.assertEqual(field.state, 'manual')
        self.assertEqual(field.ttype, 'char')
        self.assertTrue(field.store)
        self.assertEqual(field.model, 'crm.lead')

    # ---- sanitizer, base case --------------------------------------------

    def test_sanitizer_base_case(self):
        """_derive_tech_name turns a messy label into a clean lowercase x_meta_ name."""
        name = self.env['meta.promote.answer']._derive_tech_name(
            "What's your Budget?? (USD)")
        self.assertTrue(name.startswith('x_meta_'))
        self.assertEqual(name, name.lower())
        self.assertNotIn(' ', name)
        self.assertNotIn('__', name)
        self.assertFalse(name.endswith('_'))
        self.assertIn('budget', name)

    # ---- collision -------------------------------------------------------

    def test_promote_collision_blocks_and_never_clobbers(self):
        """An existing incompatible field at the derived name blocks promotion and is left as is.

        The sanitizer always adds ``x_meta_``, so it can't hit a stock column.
        The collision is staged with a manual integer field, which can't be
        reused for a char answer.
        """
        form = self._form()
        lead = self._lead_with_answer(form, value='High')
        model_rec = self.env['ir.model']._get('crm.lead')
        self.env['ir.model.fields'].sudo().create({
            'name': self.T, 'field_description': 'Pre-existing',
            'model_id': model_rec.id, 'model': 'crm.lead',
            'ttype': 'integer', 'state': 'manual', 'store': True})
        before_ttype = self._new_field(self.T).ttype
        self.assertEqual(before_ttype, 'integer', "precondition")
        with self.assertRaises(UserError):
            self._promote(lead)
        self.assertEqual(self._new_field(self.T).ttype, before_ttype)

    # ---- no source form --------------------------------------------------

    def test_promote_blocks_when_no_source_form(self):
        """Without a source form, promotion raises UserError and creates nothing."""
        form = self._form()
        lead = self._lead_with_answer(form, form_ref=False)
        self.assertFalse(lead.meta_form_id_ref)
        with self.assertRaises(UserError):
            self._promote(lead)
        self.assertFalse(
            self._new_field(self.T),
            "no field must be created when the source form is missing")
        self.assertEqual(
            self.env['meta.field.mapping'].search_count(
                [('meta_key', '=', self.K)]), 0,
            "no mapping row on the null-form path")
        self.assertEqual(
            self.env['ir.ui.view'].search_count([
                ('name', '=', self._custom_view_name(self.T))]), 0,
            "no inherited view on the null-form path")
        if 'meta.promote.backfill' in self.env:
            self.assertEqual(
                self.env['meta.promote.backfill'].search_count(
                    [('meta_key', '=', self.K)]), 0,
                "no pending-backfill marker on the null-form path")

    # ---- mapping row -----------------------------------------------------

    def test_promote_creates_single_mapping_on_source_form(self):
        """Promotion adds one mapping row, on the source form, pointing at the new field."""
        form = self._form()
        lead = self._lead_with_answer(form)
        self._promote(lead)
        mappings = self.env['meta.field.mapping'].search(
            [('form_id', '=', form.id), ('meta_key', '=', self.K)])
        self.assertEqual(len(mappings), 1)
        self.assertEqual(mappings.crm_field_id, self._new_field(self.T))

    # ---- backfill --------------------------------------------------------

    def test_backfill_writes_value_into_new_field(self):
        """Promotion backfills the answer value into the new field on existing leads."""
        form = self._form()
        lead = self._lead_with_answer(form, value='$5k')
        self._promote(lead)
        self.assertEqual(lead[self.T], '$5k')

    # ---- re-promote ------------------------------------------------------

    def test_repromote_is_noop(self):
        """Promoting the same form and key again creates no duplicate field, mapping or view."""
        form = self._form()
        lead = self._lead_with_answer(form)
        self._promote(lead)
        self._promote(lead)
        self.assertEqual(
            self.env['ir.model.fields'].search_count([
                ('model', '=', 'crm.lead'), ('name', '=', self.T)]), 1)
        self.assertEqual(
            self.env['meta.field.mapping'].search_count([
                ('form_id', '=', form.id), ('meta_key', '=', self.K)]), 1)
        self.assertEqual(
            self.env['ir.ui.view'].search_count([
                ('name', '=', self._custom_view_name(self.T))]), 1)

    # ---- inherited view --------------------------------------------------

    def test_promote_creates_inherited_view(self):
        """Promotion adds an inherited crm.lead form view that shows the new field."""
        form = self._form()
        lead = self._lead_with_answer(form)
        self._promote(lead)
        anchor = self.env.ref('meta_lead_ads.crm_lead_view_form_meta')
        view = self.env['ir.ui.view'].search([
            ('model', '=', 'crm.lead'),
            ('name', '=', self._custom_view_name(self.T))], limit=1)
        self.assertTrue(view, "expected a per-field inherited view")
        self.assertEqual(view.inherit_id, anchor)
        self.assertIn(self.T, view.arch_db or '')

    # ---- ingest after promotion ------------------------------------------

    def test_integration_loop_autofills_after_promotion(self):
        """After promotion, a newly ingested lead fills x_meta_<key> through the normal mapping."""
        from unittest import mock
        form = self._form()
        page = form.page_id
        seed = self._lead_with_answer(form, value='seed')
        self._promote(seed)
        field = self._new_field(self.T)
        self.assertTrue(field)
        Client = type(self.env['meta.graph.client'])
        Ingest = self.env['meta.lead.ingest']
        payload = {
            'id': 'LGNEW', 'created_time': '2026-06-14T10:00:00+0000',
            'field_data': [
                {'name': 'email', 'values': ['new@example.com']},
                {'name': 'full_name', 'values': ['New Lead']},
                {'name': self.K, 'values': ['$9k']},
            ],
            'campaign_id': 'C1', 'campaign_name': 'Summer',
            # Must be the promoted form, or _map_fields won't find the mapping.
            'form_id': form.form_id, 'platform': 'fb',
        }
        with mock.patch.multiple(
                Client,
                fetch_lead=mock.Mock(return_value=payload),
                resolve_name=mock.Mock(
                    side_effect=lambda *a, payload_name=None, **k: payload_name)):
            new_lead = Ingest.ingest_leadgen(page, 'LGNEW', 'manual')
        self.assertEqual(new_lead[self.T], '$9k')

    # ---- view anchor -----------------------------------------------------

    def test_meta_custom_fields_anchor_renders(self):
        """The meta_custom_fields group is present in the composed crm.lead form view."""
        from lxml import etree
        anchor = self.env.ref('meta_lead_ads.crm_lead_view_form_meta')
        composed = self.env['crm.lead'].get_view(view_id=anchor.id,
                                                 view_type='form')
        arch = etree.fromstring(composed['arch'])
        groups = arch.xpath("//group[@name='meta_custom_fields']")
        self.assertTrue(
            groups,
            "the static 'meta_custom_fields' anchor group must render into the "
            "composed crm.lead form arch (the runtime injection target)")

    # ---- answer-line entry point -----------------------------------------

    def test_answer_line_entry_point_opens_wizard(self):
        """action_promote_to_field on an answer row returns an action."""
        form = self._form()
        lead = self._lead_with_answer(form)
        answer = self._answer(lead)
        result = answer.action_promote_to_field()
        self.assertTrue(result)

    # ---- sanitizer edge cases --------------------------------------------

    def test_sanitizer_edge_cases(self):
        """_derive_tech_name handles empty, symbol-only, non-ASCII, prefixed and long keys."""
        derive = self.env['meta.promote.answer']._derive_tech_name

        # Blank input falls back to x_meta_<hash>, never a bare prefix.
        empty = derive('   ')
        self.assertTrue(empty.startswith('x_meta_'))
        self.assertNotEqual(empty, 'x_meta_')
        self.assertNotEqual(empty, 'x_meta')
        self.assertTrue(len(empty) > len('x_meta_'))

        # Punctuation and emoji only: same hash fallback.
        punct = derive('??? \U0001F3AF')
        self.assertTrue(punct.startswith('x_meta_'))
        self.assertNotEqual(punct, 'x_meta_')

        # Non-ASCII stays within the 63-byte limit.
        nonascii = derive('ميزانية')
        self.assertTrue(nonascii.startswith('x_meta_'))
        self.assertLessEqual(len(nonascii.encode('utf-8')), 63)

        # An already prefixed key is not prefixed twice.
        already = derive('x_meta_budget')
        self.assertFalse(already.startswith('x_meta_x_meta_'))
        self.assertTrue(already.startswith('x_meta_'))

        # Long keys that truncate the same still differ, because the hash
        # suffix is taken from the full key.
        prefix = 'a' * 60
        name_a = derive(prefix + 'AAAA')
        name_b = derive(prefix + 'BBBB')
        self.assertNotEqual(name_a, name_b)

        # The limit is in UTF-8 bytes, not characters.
        multibyte = derive('م' * 40)
        self.assertLessEqual(len(multibyte.encode('utf-8')), 63)

    # ---- duplicate view --------------------------------------------------

    def test_no_duplicate_inherited_view(self):
        """Promoting twice leaves a single inherited view for the field."""
        form = self._form()
        lead = self._lead_with_answer(form)
        self._promote(lead)
        self._promote(lead)
        self.assertEqual(
            self.env['ir.ui.view'].search_count([
                ('name', '=', self._custom_view_name(self.T))]), 1)

    # ---- partial failure recovery ----------------------------------------

    def test_recover_field_without_mapping(self):
        """If the field exists but its mapping is missing, promotion adds the mapping instead of failing."""
        form = self._form()
        lead = self._lead_with_answer(form, value='$5k')
        # Field without a mapping, as left by an interrupted promotion.
        model_row = self.env['ir.model']._get('crm.lead')
        self.env['ir.model.fields'].sudo().create({
            'name': self.T, 'model_id': model_row.id,
            'model': 'crm.lead', 'ttype': 'char', 'state': 'manual',
            'store': True, 'field_description': 'Budget'})
        self.assertEqual(
            self.env['meta.field.mapping'].search_count([
                ('form_id', '=', form.id), ('meta_key', '=', self.K)]), 0)
        self._promote(lead)
        mapping = self.env['meta.field.mapping'].search([
            ('form_id', '=', form.id), ('meta_key', '=', self.K)], limit=1)
        self.assertTrue(mapping, "promote must create the missing mapping")
        self.assertEqual(mapping.crm_field_id, self._new_field(self.T))

    # ---- multiple forms / repeated promote -------------------------------

    def test_promote_same_question_on_second_form_reuses_field(self):
        """Promoting the same question from a second form reuses the field and adds a mapping."""
        form_a, form_b = self._form(), self._form()
        self._promote(self._lead_with_answer(form_a))
        field = self._new_field(self.T)
        self.assertTrue(field)
        lead_b = self._lead_with_answer(form_b, value='$9k')
        self._promote(lead_b)
        self.assertEqual(self.env['ir.model.fields'].search_count([
            ('model', '=', 'crm.lead'), ('name', '=', self.T)]), 1)
        mappings = self.env['meta.field.mapping'].search(
            [('crm_field_id', '=', field.id)])
        self.assertEqual(mappings.form_id, form_a | form_b)
        self.assertEqual(lead_b[self.T], '$9k')

    def test_double_promote_single_mapping(self):
        """Two promotes of the same form and key give one mapping and one field.

        This runs sequentially; across workers the unique(form_id, meta_key)
        constraint on meta.field.mapping is what stops a duplicate.
        """
        form = self._form()
        lead = self._lead_with_answer(form)
        self._promote(lead)
        self._promote(lead)
        self.assertEqual(
            self.env['meta.field.mapping'].search_count([
                ('form_id', '=', form.id), ('meta_key', '=', self.K)]), 1)
        self.assertEqual(
            self.env['ir.model.fields'].search_count([
                ('model', '=', 'crm.lead'), ('name', '=', self.T)]), 1)

    # ---- backfill rules --------------------------------------------------

    def test_backfill_filter_and_conflict_rules(self):
        """Backfill only fills blank fields on leads from the source form.

        Leads from other forms, empty answers and already filled fields are
        left alone. With several answers for one key the first by sequence
        wins, and long values are written unchanged.
        """
        form = self._form()
        other_form = self._form()

        target = self._lead_with_answer(form, value='$5k')
        # Same key on another form: must not be backfilled.
        cross = self._lead_with_answer(other_form, value='other')
        # Edited by hand later to check backfill doesn't overwrite it.
        prefilled = self._lead_with_answer(form, value='ignored')
        empty_lead = self._lead_with_answer(form, value='')
        # Two answers for the same key.
        multi = self.env['crm.lead'].create({
            'name': 'Multi', 'type': 'lead', 'meta_form_id_ref': form.id})
        self.env['meta.lead.answer'].create({
            'lead_id': multi.id, 'question_key': self.K,
            'sequence': 1, 'value': 'first'})
        self.env['meta.lead.answer'].create({
            'lead_id': multi.id, 'question_key': self.K,
            'sequence': 2, 'value': 'second'})
        long_value = 'X' * 5000
        over = self._lead_with_answer(form, value=long_value)

        self._promote(target)

        self.assertFalse(cross[self.T])
        self.assertEqual(target[self.T], '$5k')
        self.assertFalse(empty_lead[self.T])
        self.assertEqual(multi[self.T], 'first')
        self.assertEqual(over[self.T], long_value)

        # Hand-edit a value and promote again: the edit is kept.
        prefilled[self.T] = 'HAND_EDITED'
        self._promote(target)
        self.assertEqual(prefilled[self.T], 'HAND_EDITED')
