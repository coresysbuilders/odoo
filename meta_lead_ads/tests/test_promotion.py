# Meta Lead Ads Integration for Odoo — © 2026 CoreSys Builders. All rights reserved.
# Licensed under the Odoo Proprietary License v1.0 (OPL-1).
# Unauthorized copying, redistribution, or resale of this software, in whole or in
# part, via any medium, is strictly prohibited and constitutes a license violation.
# OPL-1: https://www.odoo.com/documentation/18.0/legal/licenses.html#odoo-apps

"""Tests for promoting a captured Meta question to a custom crm.lead field.

These tests pin the contract of the promotion wizard (``meta.promote.answer``
with ``action_promote`` / ``action_promote_to_field`` and the
``_derive_tech_name`` sanitizer).

Conventions:
  - ``@tagged('post_install', '-at_install')`` so crm + the module are fully
    installed (the ``crm_lead_view_form_meta`` anchor exists).
  - ``assertRaises`` takes a SINGLE exception class, never a tuple (a tuple
    TypeErrors at runtime on Odoo's TransactionCase).
  - Use ``search_count(...)``; the legacy ``count=`` kwarg was removed in
    Odoo 18.
  - Promoted-field invariants the @api.constrains on meta.field.mapping pins:
    crm_field_id must be model=='crm.lead', store==True, ttype in (char, text).
    No setUp here weakens that constraint.

Test isolation note (inline backfill): promotion creates a ``state='manual'``
field and calls ``registry.setup_models`` inline. That mutates the
process-level registry, which TransactionCase's per-test cursor rollback does
NOT revert — a promoted ``x_meta_<key>`` field lingers in the in-memory
``_fields`` after the test. So every functional test derives a field name
unique to the test method (``self.K`` / ``self.T``); no two tests share a
derived column name, so the lingering registry entries never collide.
Production is unaffected (each promote is its own request and Odoo signals the
registry reload across workers).

Identifiers asserted against:
  - model ``meta.promote.answer`` (the wizard)
  - ``action_promote`` (server method; in-method has_group gate)
  - ``action_promote_to_field`` (alias / entry from the answer line)
  - ``_derive_tech_name`` (the sanitizer; ``x_meta_`` prefix, 63-byte cap,
    deterministic ``x_meta_<hash>`` fallback)
"""
from odoo.tests.common import TransactionCase, tagged
from odoo.exceptions import UserError


@tagged('post_install', '-at_install')
class TestPromotion(TransactionCase):
    # ---- fixtures --------------------------------------------------------

    def setUp(self):
        super().setUp()
        # The promote action is admin-gated via an in-method has_group check.
        # Functional tests act as a Meta admin; the non-admin rejection path is
        # covered by TestPromotionSecurity.
        self.env.user.groups_id |= self.env.ref(
            'meta_lead_ads.group_meta_admin')
        # Per-test-unique question key + its derived technical name. Unique per
        # method so the INLINE setup_models registry leak (not reverted by the
        # cursor rollback) never collides across tests.
        self.K = 'q_%s' % self._testMethodName
        self.T = self.env['meta.promote.answer']._derive_tech_name(self.K)

    def _form(self):
        """Account -> page -> form chain. IDs are unique per call so a single
        test can build more than one form chain without tripping the
        account/page/form UNIQUE constraints (used by the cross-form tests)."""
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
        """Create a crm.lead carrying ONE meta.lead.answer row for
        ``question_key`` (defaults to the per-test ``self.K``) and link the
        lead's source form via meta_form_id_ref (so the wizard can resolve the
        source form). ``form_ref`` overrides the linked form (pass False to
        exercise the no-source-form guard)."""
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
        """The first meta.lead.answer row on a lead."""
        return lead.answer_ids[:1]

    def _promote(self, lead, user=None):
        """Build the promote wizard for a lead's first answer and invoke its
        server method. Centralizes the identifiers so a contract change touches
        one place."""
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
        """The ir.model.fields row for a promoted crm.lead column, or empty."""
        return self.env['ir.model.fields'].search([
            ('model', '=', 'crm.lead'), ('name', '=', tech_name)], limit=1)

    def _custom_view_name(self, tech_name):
        """The deterministic name of the per-field inherited view (search-
        before-create key for duplicate-view avoidance)."""
        return 'crm.lead.form.meta.custom.%s' % tech_name

    # ---- (1) field creation ----------------------------------------------

    def test_promote_creates_manual_char_field(self):
        """Promote creates a state='manual', ttype='char', store=True field
        named x_meta_<sanitized> on crm.lead."""
        form = self._form()
        lead = self._lead_with_answer(form)
        self._promote(lead)
        field = self._new_field(self.T)
        self.assertTrue(field, "expected %s field on crm.lead" % self.T)
        self.assertEqual(field.state, 'manual')
        self.assertEqual(field.ttype, 'char')
        self.assertTrue(field.store)
        self.assertEqual(field.model, 'crm.lead')

    # ---- (2) sanitizer base case -----------------------------------------

    def test_sanitizer_base_case(self):
        """_derive_tech_name lowercases, maps non-[a-z0-9_] to _, collapses
        repeats, trims, and prepends x_meta_ for a representative ugly key."""
        name = self.env['meta.promote.answer']._derive_tech_name(
            "What's your Budget?? (USD)")
        self.assertTrue(name.startswith('x_meta_'))
        self.assertEqual(name, name.lower())
        self.assertNotIn(' ', name)
        self.assertNotIn('__', name)
        self.assertFalse(name.endswith('_'))
        self.assertIn('budget', name)

    # ---- (3) collision (never clobber) -----------------------------------

    def test_promote_collision_blocks_and_never_clobbers(self):
        """When the derived name already exists and is NOT a reuse-eligible
        manual char/text stored field, promote raises UserError and never
        clobbers the existing field definition.

        The sanitizer always prepends ``x_meta_`` (it can never derive onto a
        stock crm.lead column), so a genuine collision is staged by
        pre-creating a NON-reuse-eligible field (manual but ttype='integer') at
        the derived name."""
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
            # reuse_confirmed is not set -> a non-reuse-eligible collision blocks.
            self._promote(lead)
        # never clobbered: the pre-existing field keeps its definition.
        self.assertEqual(self._new_field(self.T).ttype, before_ttype)

    # ---- (4) no source form — full no-side-effects ------------------------

    def test_promote_blocks_when_no_source_form(self):
        """A lead carrying an answer but with meta_form_id_ref == False (form
        never discovered or deleted) must raise UserError (single class) and
        leave NO side effects: no x_meta_ field, no mapping row, no inherited
        view, no pending-backfill marker."""
        form = self._form()
        lead = self._lead_with_answer(form, form_ref=False)
        self.assertFalse(lead.meta_form_id_ref)
        with self.assertRaises(UserError):
            self._promote(lead)
        # FULL no-side-effects.
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

    # ---- (5) exactly one mapping row on the source form ------------------

    def test_promote_creates_single_mapping_on_source_form(self):
        """Exactly one meta.field.mapping row is created, on the source form
        only, with meta_key==question_key and crm_field_id == the new field."""
        form = self._form()
        lead = self._lead_with_answer(form)
        self._promote(lead)
        mappings = self.env['meta.field.mapping'].search(
            [('form_id', '=', form.id), ('meta_key', '=', self.K)])
        self.assertEqual(len(mappings), 1)
        self.assertEqual(mappings.crm_field_id, self._new_field(self.T))

    # ---- (6) backfill ----------------------------------------------------

    def test_backfill_writes_value_into_new_field(self):
        """Backfill writes meta.lead.answer.value into the new field on every
        existing lead carrying that (form, key). Reads answer.value directly
        (the deterministic display string)."""
        form = self._form()
        lead = self._lead_with_answer(form, value='$5k')
        self._promote(lead)
        self.assertEqual(lead[self.T], '$5k')

    # ---- (7) re-promote no-op --------------------------------------------

    def test_repromote_is_noop(self):
        """Re-promoting an already-promoted (form, key) is a no-op: no
        duplicate field, no duplicate mapping row, no duplicate inherited
        view."""
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

    # ---- (8) inherited view injection ------------------------------------

    def test_promote_creates_inherited_view(self):
        """An inherited ir.ui.view (model crm.lead, inherit_id ==
        meta_lead_ads.crm_lead_view_form_meta) is created injecting the new
        field."""
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

    # ---- (9) integration loop (mocked Graph client) ----------------------

    def test_integration_loop_autofills_after_promotion(self):
        """After promotion, a fresh ingest_leadgen of a lead carrying that
        question auto-fills x_meta_<key> via the _map_fields consumption path —
        zero ingest-code change. Uses the mocked Graph client idiom from
        test_ingest.py (patch on type(client), never a recordset)."""
        from unittest import mock
        form = self._form()
        page = form.page_id
        # promote first so the mapping row + field exist.
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
            # MUST match the promoted form so _map_fields finds the mapping.
            'form_id': form.form_id, 'platform': 'fb',
        }
        with mock.patch.multiple(
                Client,
                fetch_lead=mock.Mock(return_value=payload),
                resolve_name=mock.Mock(
                    side_effect=lambda *a, payload_name=None, **k: payload_name)):
            new_lead = Ingest.ingest_leadgen(page, 'LGNEW', 'manual')
        self.assertEqual(new_lead[self.T], '$9k')

    # ---- (8b) static anchor render proof ---------------------------------

    def test_meta_custom_fields_anchor_renders(self):
        """The static ``meta_custom_fields`` anchor group must resolve into the
        composed crm.lead form arch — i.e. the runtime field injection has a
        real xpath target — not merely be present as unparsed source text.
        Asserts against the composed view via ``get_view`` (Odoo 18) on the
        crm_lead_view_form_meta lineage."""
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

    # ---- (9b) answer-line entry point (action_promote_to_field) -----------

    def test_answer_line_entry_point_opens_wizard(self):
        """The answer-line entry ``action_promote_to_field`` on a
        meta.lead.answer row opens / drives the promote wizard (the action a
        user clicks from a captured answer)."""
        form = self._form()
        lead = self._lead_with_answer(form)
        answer = self._answer(lead)
        result = answer.action_promote_to_field()
        self.assertTrue(result)

    # ---- (10) sanitizer edge cases ---------------------------------------

    def test_sanitizer_edge_cases(self):
        """_derive_tech_name edge cases: empty/whitespace, punctuation/emoji-
        only, non-ASCII, already-prefixed, long-key truncation collision, and
        the 63-byte cap."""
        derive = self.env['meta.promote.answer']._derive_tech_name

        # (a) EMPTY/whitespace -> deterministic x_meta_<hash>, NEVER bare.
        empty = derive('   ')
        self.assertTrue(empty.startswith('x_meta_'))
        self.assertNotEqual(empty, 'x_meta_')
        self.assertNotEqual(empty, 'x_meta')
        self.assertTrue(len(empty) > len('x_meta_'))

        # (b) PUNCTUATION/EMOJI-only -> same deterministic-hash fallback.
        punct = derive('??? \U0001F3AF')   # "??? 🎯"
        self.assertTrue(punct.startswith('x_meta_'))
        self.assertNotEqual(punct, 'x_meta_')

        # (c) NON-ASCII -> safe x_meta_-prefixed within the byte cap.
        nonascii = derive('ميزانية')
        self.assertTrue(nonascii.startswith('x_meta_'))
        self.assertLessEqual(len(nonascii.encode('utf-8')), 63)

        # (d) ALREADY-x_meta_-prefixed -> MUST NOT double-prefix.
        already = derive('x_meta_budget')
        self.assertFalse(already.startswith('x_meta_x_meta_'))
        self.assertTrue(already.startswith('x_meta_'))

        # (e) LONG-KEY TRUNCATION COLLISION -> distinct names because the hash
        #     suffix is computed from the FULL original key.
        prefix = 'a' * 60
        name_a = derive(prefix + 'AAAA')
        name_b = derive(prefix + 'BBBB')
        self.assertNotEqual(name_a, name_b)

        # (f) BYTE CAP -> a multi-byte name's UTF-8 encoding is <= 63 BYTES.
        multibyte = derive('م' * 40)
        self.assertLessEqual(len(multibyte.encode('utf-8')), 63)

    # ---- (11) duplicate inherited view avoidance -------------------------

    def test_no_duplicate_inherited_view(self):
        """Promoting the same (form, key) twice creates AT MOST ONE inherited
        view named crm.lead.form.meta.custom.<tech_name> (search-before-create
        proof)."""
        form = self._form()
        lead = self._lead_with_answer(form)
        self._promote(lead)
        self._promote(lead)
        self.assertEqual(
            self.env['ir.ui.view'].search_count([
                ('name', '=', self._custom_view_name(self.T))]), 1)

    # ---- (12) partial-failure recovery -----------------------------------

    def test_recover_field_without_mapping(self):
        """A half-promoted state — the x_meta_ field exists but NO mapping row
        for (form, key) — must RECOVER on promote (create the missing mapping +
        view + backfill), NOT raise a spurious collision UserError and NOT
        silently no-op leaving the mapping missing."""
        form = self._form()
        lead = self._lead_with_answer(form, value='$5k')
        # Pre-create the field directly (no mapping) to simulate the half state.
        model_row = self.env['ir.model']._get('crm.lead')
        self.env['ir.model.fields'].sudo().create({
            'name': self.T, 'model_id': model_row.id,
            'model': 'crm.lead', 'ttype': 'char', 'state': 'manual',
            'store': True, 'field_description': 'Budget'})
        self.assertEqual(
            self.env['meta.field.mapping'].search_count([
                ('form_id', '=', form.id), ('meta_key', '=', self.K)]), 0)
        # Promote must recover, not raise collision.
        self._promote(lead)
        mapping = self.env['meta.field.mapping'].search([
            ('form_id', '=', form.id), ('meta_key', '=', self.K)], limit=1)
        self.assertTrue(mapping, "promote must create the missing mapping")
        self.assertEqual(mapping.crm_field_id, self._new_field(self.T))

    # ---- (13) concurrency backstop ---------------------------------------

    def test_double_promote_single_mapping(self):
        """Two SEQUENTIAL promotes of the same (form, key) — the in-test
        stand-in for a concurrent race — yield exactly ONE mapping row and ONE
        field. The TRUE multi-worker race backstop is the DB
        unique(form_id, meta_key) SQL constraint on meta.field.mapping."""
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

    # ---- (14) backfill conflict / filter semantics -----------------------

    def test_backfill_filter_and_conflict_rules(self):
        """Backfill filter + conflict rules:
          (i)   a lead on a DIFFERENT form carrying the same question_key is
                NOT backfilled (filter by source_form + question_key + linked
                lead, not answer value alone);
          (ii)  a target lead whose new field is already non-empty is NOT
                clobbered (enrich-blank-only);
          (iii) an answer row with empty/falsy value is skipped (no write);
          (iv)  MULTIPLE answer rows for the same (lead, question_key) -> a
                deterministic SINGLE write (first by sequence, id per _order);
          (v)   an over-length value is written as-is (no crash).
        """
        form = self._form()
        other_form = self._form()

        # target on the SOURCE form (will be backfilled).
        target = self._lead_with_answer(form, value='$5k')
        # (i) DIFFERENT-form lead with the same key -> must NOT be backfilled.
        cross = self._lead_with_answer(other_form, value='other')
        # (ii) a lead on the source form whose field gets hand-edited later.
        prefilled = self._lead_with_answer(form, value='ignored')
        # (iii) a lead on the source form with an empty answer value.
        empty_lead = self._lead_with_answer(form, value='')
        # (iv) a lead with TWO answer rows for the same key -> deterministic.
        multi = self.env['crm.lead'].create({
            'name': 'Multi', 'type': 'lead', 'meta_form_id_ref': form.id})
        self.env['meta.lead.answer'].create({
            'lead_id': multi.id, 'question_key': self.K,
            'sequence': 1, 'value': 'first'})
        self.env['meta.lead.answer'].create({
            'lead_id': multi.id, 'question_key': self.K,
            'sequence': 2, 'value': 'second'})
        # (v) an over-length value.
        long_value = 'X' * 5000
        over = self._lead_with_answer(form, value=long_value)

        self._promote(target)

        # (i) cross-form lead untouched.
        self.assertFalse(cross[self.T])
        # source-form target filled.
        self.assertEqual(target[self.T], '$5k')
        # (iii) empty-value lead skipped (no write -> falsy).
        self.assertFalse(empty_lead[self.T])
        # (iv) deterministic single write: first by sequence, id.
        self.assertEqual(multi[self.T], 'first')
        # (v) over-length written as-is.
        self.assertEqual(over[self.T], long_value)

        # (ii) enrich-blank-only: hand-edit a value, re-run backfill, no clobber.
        prefilled[self.T] = 'HAND_EDITED'
        self._promote(target)
        self.assertEqual(prefilled[self.T], 'HAND_EDITED')
