# Meta Lead Ads Integration for Odoo — © 2026 CoreSys Builders. All rights reserved.
# Licensed under the Odoo Proprietary License v1.0 (OPL-1).
# Unauthorized copying, redistribution, or resale of this software, in whole or in
# part, via any medium, is strictly prohibited and constitutes a license violation.
# OPL-1: https://www.odoo.com/documentation/19.0/legal/licenses.html#odoo-apps

"""Tests for promoting a captured Meta question to a custom crm.lead field.

Covers the ``meta.promote.answer`` wizard (``action_promote`` and its confirm
step, ``action_confirm_reuse`` / ``action_back_to_name``), the answer-line
entry ``action_promote_to_field`` and the ``_derive_tech_name`` and
``_clean_tech_name`` sanitizers.

Promotion creates a manual field and calls ``registry.setup_models`` inline.
The registry lives at process level, so the test cursor rollback does not
remove the new ``x_meta_<key>`` field from ``_fields``. Each test therefore
derives its own field name from the method name (``self.K`` / ``self.T``) so
leftover registry entries never collide. Production isn't affected: each
promote is its own request and Odoo signals the registry reload to other
workers.
"""
import hashlib

from odoo import Command
from odoo.tests.common import TransactionCase, new_test_user, tagged
from odoo.exceptions import UserError, ValidationError

from .test_mapping_targets import legacy_mapping


@tagged('post_install', '-at_install')
class TestPromotion(TransactionCase):
    # ---- fixtures --------------------------------------------------------

    def setUp(self):
        # Added before the test's savepoint, so it runs after the rollback.
        self.addCleanup(self._reload_models_if_needed)
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

    def _wizard(self, lead, **vals):
        """Open the promote wizard on the lead's first answer, without running it."""
        return self.env['meta.promote.answer'].create(dict({
            'answer_id': self._answer(lead).id,
            'lead_id': lead.id,
        }, **vals))

    def _reload_models_if_needed(self):
        # A computed or related field left in the registry would be written
        # on every later lead save, but its column went with the rollback.
        if getattr(self, '_models_changed', False):
            with self.registry.cursor() as cr:
                self.registry._setup_models__(cr)

    def _manual_field(self, name, ttype='char', label='Budget', **extra):
        """A custom crm.lead field made outside the wizard."""
        if extra.get('compute') or extra.get('related'):
            self._models_changed = True
        model_rec = self.env['ir.model']._get('crm.lead')
        return self.env['ir.model.fields'].sudo().create(dict({
            'name': name, 'field_description': label,
            'model_id': model_rec.id, 'model': 'crm.lead',
            'ttype': ttype, 'state': 'manual', 'store': True}, **extra))

    def _short_key(self):
        """A per-test key short enough to get no hash suffix.

        Without the hash, a second spelling of the key (other case, extra
        punctuation) turns into the same technical name.
        """
        return 'c%s' % hashlib.sha1(
            self._testMethodName.encode()).hexdigest()[:10]

    def _mappings(self, form, key):
        """Mapping rows for (form, key), archived ones included."""
        return self.env['meta.field.mapping'].with_context(
            active_test=False).search([
                ('form_id', '=', form.id), ('meta_key', '=', key)])

    def _view_count(self, tech_name):
        return self.env['ir.ui.view'].search_count([
            ('name', '=', self._custom_view_name(tech_name))])

    def _assert_confirm_step(self, wizard, action):
        """The wizard reopened itself on the confirm step."""
        self.assertEqual(action['type'], 'ir.actions.act_window')
        self.assertEqual(action['res_model'], 'meta.promote.answer')
        self.assertEqual(action['res_id'], wizard.id)
        self.assertEqual(action['target'], 'new')
        self.assertEqual(wizard.state, 'confirm')

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
        """An existing incompatible field at the derived name stops at the
        confirm step, can't be reused, and is left as is.

        The sanitizer always adds ``x_meta_``, so it can't hit a stock column.
        The collision is staged with a manual integer field, which can't be
        reused for a char answer.
        """
        form = self._form()
        lead = self._lead_with_answer(form, value='High')
        self._manual_field(self.T, ttype='integer', label='Pre-existing')
        before_ttype = self._new_field(self.T).ttype
        self.assertEqual(before_ttype, 'integer', "precondition")

        wizard = self._wizard(lead)
        self._assert_confirm_step(wizard, wizard.action_promote())
        self.assertFalse(wizard.field_reusable)
        self.assertFalse(wizard.field_on_form)
        self.assertEqual(wizard.existing_field_label, 'Pre-existing')
        self.assertFalse(wizard.usage_summary)
        with self.assertRaisesRegex(UserError, "can't be reused"):
            wizard.action_confirm_reuse()

        # The stored flag is display only: forcing it changes nothing.
        wizard.field_reusable = True
        with self.assertRaisesRegex(UserError, "can't be reused"):
            wizard.action_confirm_reuse()

        self.assertEqual(self._new_field(self.T).ttype, before_ttype)
        self.assertFalse(self._mappings(form, self.K))
        self.assertEqual(self._view_count(self.T), 0)

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
        # The web client skips anything in a notebook that is not a page.
        for group in groups:
            self.assertIn(group.getparent().tag, ('page', 'sheet'))

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
        """A field left without a mapping is only picked up after the admin
        confirms reuse; then the mapping, view and backfill are added."""
        form = self._form()
        lead = self._lead_with_answer(form, value='$5k')
        # Field without a mapping, as left by an interrupted promotion.
        field = self._manual_field(self.T)
        self.assertFalse(self._mappings(form, self.K))

        wizard = self._wizard(lead)
        self._assert_confirm_step(wizard, wizard.action_promote())
        self.assertTrue(wizard.field_reusable)
        self.assertTrue(wizard.resume_hint)
        self.assertEqual(wizard.existing_field_label, 'Budget')
        self.assertFalse(wizard.usage_summary)
        self.assertFalse(wizard.collision_note)
        self.assertFalse(self._mappings(form, self.K),
                         "the confirm step must not create a mapping")
        self.assertEqual(self._view_count(self.T), 0)
        self.assertFalse(lead[self.T])

        result = wizard.action_confirm_reuse()
        self.assertEqual(result['res_model'], 'crm.lead')
        mapping = self._mappings(form, self.K)
        self.assertTrue(mapping, "reuse must create the missing mapping")
        self.assertEqual(mapping.crm_field_id, field)
        self.assertEqual(self._view_count(self.T), 1)
        self.assertEqual(lead[self.T], '$5k')

    # ---- multiple forms / repeated promote -------------------------------

    def test_promote_same_question_on_second_form_reuses_field(self):
        """Promoting the same question from a second form asks first, names
        the first form, then reuses the field and adds a mapping."""
        form_a, form_b = self._form(), self._form()
        self._promote(self._lead_with_answer(form_a))
        field = self._new_field(self.T)
        self.assertTrue(field)
        lead_b = self._lead_with_answer(form_b, value='$9k')

        wizard = self._wizard(lead_b)
        self._assert_confirm_step(wizard, wizard.action_promote())
        self.assertTrue(wizard.field_reusable)
        self.assertFalse(wizard.resume_hint)
        self.assertEqual(wizard.usage_summary,
                         '%s: %s' % (form_a.display_name, self.K))
        # Same key on both forms: nothing to explain.
        self.assertFalse(wizard.collision_note)
        self.assertFalse(self._mappings(form_b, self.K))

        wizard.action_confirm_reuse()
        self.assertEqual(self.env['ir.model.fields'].search_count([
            ('model', '=', 'crm.lead'), ('name', '=', self.T)]), 1)
        mappings = self.env['meta.field.mapping'].search(
            [('crm_field_id', '=', field.id)])
        self.assertEqual(mappings.form_id, form_a | form_b)
        self.assertEqual(lead_b[self.T], '$9k')

    def test_second_form_key_collision_asks_and_reuses(self):
        """Another key that turns into the same name on another form stops
        at the confirm step, which names both keys, and reuse links it."""
        k1 = self._short_key()
        k2 = k1.upper() + '?'
        derive = self.env['meta.promote.answer']._derive_tech_name
        name = derive(k1)
        self.assertEqual(derive(k2), name, "precondition")
        form_a, form_b = self._form(), self._form()
        self._promote(self._lead_with_answer(form_a, question_key=k1))
        field = self._new_field(name)
        self.assertTrue(field)
        lead_b = self._lead_with_answer(form_b, question_key=k2, value='$7k')

        wizard = self._wizard(lead_b)
        self._assert_confirm_step(wizard, wizard.action_promote())
        self.assertEqual(wizard.tech_name, name)
        self.assertTrue(wizard.field_reusable)
        self.assertEqual(wizard.usage_summary,
                         '%s: %s' % (form_a.display_name, k1))
        self.assertIn('"%s"' % k1, wizard.collision_note)
        self.assertIn('"%s"' % k2, wizard.collision_note)
        self.assertFalse(self._mappings(form_b, k2))

        wizard.action_confirm_reuse()
        self.assertEqual(self._mappings(form_b, k2).crm_field_id, field)
        self.assertEqual(lead_b[name], '$7k')

    def test_same_form_key_collision_cannot_reuse(self):
        """Two keys on one form that turn into the same name: the field is
        offered for nothing, since one question per form fills a field."""
        k1 = self._short_key()
        k2 = k1.upper() + '!'
        name = self.env['meta.promote.answer']._derive_tech_name(k1)
        form = self._form()
        self._promote(self._lead_with_answer(form, question_key=k1))
        lead2 = self._lead_with_answer(form, question_key=k2)

        wizard = self._wizard(lead2)
        self._assert_confirm_step(wizard, wizard.action_promote())
        self.assertEqual(wizard.tech_name, name)
        self.assertFalse(wizard.field_reusable)
        self.assertTrue(wizard.field_on_form)
        self.assertTrue(wizard.collision_note)
        with self.assertRaisesRegex(UserError, "can't be reused"):
            wizard.action_confirm_reuse()
        self.assertFalse(self._mappings(form, k2))
        self.assertEqual(len(self._mappings(form, k1)), 1)

    def test_confirm_reuse_checks_again(self):
        """Reuse is decided again when Reuse Field is clicked, not taken from
        what the confirm step showed."""
        form = self._form()
        lead = self._lead_with_answer(form)
        field = self._manual_field(self.T)
        wizard = self._wizard(lead)
        wizard.action_promote()
        self.assertTrue(wizard.field_reusable, "precondition")
        # Meanwhile another question on the same form is mapped to the field.
        self.env['meta.field.mapping'].sudo().create({
            'form_id': form.id, 'meta_key': 'other_%s' % self.K,
            'crm_field_id': field.id})
        with self.assertRaisesRegex(UserError, "can't be reused"):
            wizard.action_confirm_reuse()
        self.assertFalse(self._mappings(form, self.K))
        self.assertEqual(self._view_count(self.T), 0)

    def test_reuse_confirmed_does_not_skip_confirm_step(self):
        """The old reuse checkbox no longer reuses without the confirm step."""
        form = self._form()
        lead = self._lead_with_answer(form)
        self._manual_field(self.T)
        wizard = self._wizard(lead, reuse_confirmed=True)
        self._assert_confirm_step(wizard, wizard.action_promote())
        self.assertFalse(self._mappings(form, self.K))
        self.assertEqual(self._view_count(self.T), 0)

    def _assert_reuse_not_offered(self, form, lead):
        """The confirm step shows the field but doesn't offer it, and Reuse
        Field refuses it too."""
        wizard = self._wizard(lead)
        self._assert_confirm_step(wizard, wizard.action_promote())
        self.assertFalse(wizard.field_reusable)
        self.assertFalse(wizard.field_on_form)
        self.assertFalse(wizard.resume_hint)
        with self.assertRaisesRegex(UserError, "can't be reused"):
            wizard.action_confirm_reuse()
        self.assertFalse(self._mappings(form, self.K))
        self.assertEqual(self._view_count(self.T), 0)

    def test_reuse_not_offered_for_computed_field(self):
        """A computed custom field can't take answers: the mapping check
        refuses it, so the confirm step must not offer it."""
        form = self._form()
        lead = self._lead_with_answer(form)
        # readonly as the field form sets it once a compute is entered
        field = self._manual_field(
            self.T, compute="for rec in self:\n    rec[%r] = rec.name"
            % self.T, depends='name', readonly=True)
        self.assertTrue(self.env['crm.lead']._fields[self.T].readonly,
                        "precondition")
        with self.assertRaises(ValidationError):
            self.env['meta.field.mapping'].sudo().create({
                'form_id': form.id, 'meta_key': 'probe_%s' % self.K,
                'crm_field_id': field.id})
        self._assert_reuse_not_offered(form, lead)

    def test_reuse_not_offered_for_related_field(self):
        """A related field writes through to another record, so it is never
        offered either, even when it was left editable."""
        form = self._form()
        lead = self._lead_with_answer(form)
        self._manual_field(self.T, related='partner_id.name')
        self.assertTrue(self._new_field(self.T).related, "precondition")
        self._assert_reuse_not_offered(form, lead)

    def test_back_to_name_clears_confirm_details(self):
        """Pick Another Name goes back with nothing stale, and a new name
        then promotes into a new field."""
        k1 = self._short_key()
        k2 = k1.upper() + '?'
        form_a, form_b = self._form(), self._form()
        self._promote(self._lead_with_answer(form_a, question_key=k1))
        lead_b = self._lead_with_answer(form_b, question_key=k2, value='$8k')
        wizard = self._wizard(lead_b)
        wizard.action_promote()
        self.assertEqual(wizard.state, 'confirm', "precondition")
        self.assertTrue(wizard.existing_field_label and wizard.usage_summary
                        and wizard.collision_note and wizard.field_reusable)
        # Set by hand so the test also covers the flags this case leaves off.
        wizard.write({'resume_hint': True, 'field_on_form': True})

        back = wizard.action_back_to_name()
        self.assertEqual(back['res_model'], 'meta.promote.answer')
        self.assertEqual(back['res_id'], wizard.id)
        self.assertEqual(back['target'], 'new')
        self.assertEqual(wizard.state, 'name')
        for fname in ('existing_field_label', 'usage_summary',
                      'collision_note', 'resume_hint', 'field_reusable',
                      'field_on_form'):
            self.assertFalse(wizard[fname], fname)

        alt = 'x_meta_alt_%s' % k1
        wizard.write({'tech_name': alt, 'field_label': 'Budget (other)'})
        result = wizard.action_promote()
        self.assertEqual(result['res_model'], 'crm.lead')
        new_field = self._new_field(alt)
        self.assertTrue(new_field)
        self.assertEqual(self._mappings(form_b, k2).crm_field_id, new_field)
        self.assertEqual(lead_b[alt], '$8k')

    def test_promote_retargets_archived_mapping(self):
        """A switched-off row for the same question is pointed at the new
        field and turned back on, instead of hitting the unique key."""
        form = self._form()
        lead = self._lead_with_answer(form, value='$3k')
        row = legacy_mapping(
            self.env, form, 'user_id', meta_key=self.K, active=False,
            disabled_reason='Disabled in an upgrade')
        self.assertFalse(row.active, "precondition")

        self._promote(lead)
        field = self._new_field(self.T)
        self.assertTrue(field)
        self.assertEqual(self._mappings(form, self.K), row)
        self.assertTrue(row.active)
        self.assertEqual(row.crm_field_id, field)
        self.assertFalse(row.disabled_reason)
        self.assertEqual(lead[self.T], '$3k')

    def _archived_rows_on_field(self, form):
        """The field exists, another question on the form has a switched-off
        row on it, and this question has a switched-off row of its own."""
        field = self._manual_field(self.T)
        self.env['meta.field.mapping'].sudo().create({
            'form_id': form.id, 'meta_key': 'other_%s' % self.K,
            'crm_field_id': field.id, 'active': False})
        row = legacy_mapping(
            self.env, form, 'user_id', meta_key=self.K, active=False,
            disabled_reason='Disabled in an upgrade')
        return field, row

    def test_reuse_ignores_archived_row_of_other_question(self):
        """A switched-off row of another question fills nothing, so it
        neither blocks reuse nor the row turned back on."""
        form = self._form()
        lead = self._lead_with_answer(form, value='$4k')
        field, row = self._archived_rows_on_field(form)

        wizard = self._wizard(lead)
        self._assert_confirm_step(wizard, wizard.action_promote())
        self.assertFalse(wizard.field_on_form)
        self.assertTrue(wizard.field_reusable)

        wizard.action_confirm_reuse()
        self.assertEqual(self._mappings(form, self.K), row)
        self.assertTrue(row.active)
        self.assertEqual(row.crm_field_id, field)
        self.assertEqual(lead[self.T], '$4k')

    def test_reuse_state_ignores_caller_active_test(self):
        """Opened with active_test off, the wizard still counts only rows
        that are on."""
        form = self._form()
        lead = self._lead_with_answer(form, value='$6k')
        field, row = self._archived_rows_on_field(form)

        wizard = self._wizard(lead).with_context(active_test=False)
        self._assert_confirm_step(wizard, wizard.action_promote())
        self.assertFalse(wizard.field_on_form)
        self.assertTrue(wizard.field_reusable)
        self.assertFalse(wizard.usage_summary)

        wizard.action_confirm_reuse()
        self.assertTrue(row.active)
        self.assertEqual(row.crm_field_id, field)
        self.assertEqual(lead[self.T], '$6k')

    # ---- wizard steps and name cleaning ---------------------------------

    def test_fresh_wizard_starts_on_name_step(self):
        """The name is filled in when the wizard opens, before Promote."""
        form = self._form()
        wizard = self._wizard(self._lead_with_answer(form))
        self.assertEqual(wizard.state, 'name')
        self.assertEqual(wizard.tech_name, self.T)

    def test_clean_tech_name(self):
        """An edited name is cleaned the same way every time and stays a
        safe x_meta_ column name within 63 bytes."""
        Wizard = self.env['meta.promote.answer']
        clean, derive = Wizard._clean_tech_name, Wizard._derive_tech_name
        self.assertEqual(clean('x_meta_budget'), 'x_meta_budget')
        self.assertEqual(clean('Budget Amount!'), 'x_meta_budget_amount')
        self.assertEqual(clean('X_META_Budget'), 'x_meta_budget')
        self.assertEqual(clean('x_meta_a"/><x'), 'x_meta_a_x')
        # Whatever the wizard derived is kept as it is, hash suffix included.
        for key in ('budget', 'Budget Amount!', 'a' * 100, 'م' * 40, '',
                    '???', 'x_meta_budget', self.K, 'ab' * 40 + 'Z'):
            name = derive(key)
            self.assertEqual(clean(name), name, key)
        for bad in ('!!!', 'x_meta_', '   ', ''):
            with self.assertRaisesRegex(UserError, 'no usable characters'):
                clean(bad)
        long_name = clean('B' * 100)
        self.assertLessEqual(len(long_name.encode('utf-8')), 63)
        self.assertRegex(long_name, r'^x_meta_[a-z0-9_]+$')
        self.assertEqual(clean(long_name), long_name)
        cut = clean('a' * 55 + '_b' * 10)
        self.assertFalse(cut.endswith('_'))
        self.assertEqual(clean(cut), cut)

    def test_promote_cleans_an_edited_name(self):
        """A messy edited name is cleaned before the field is made."""
        form = self._form()
        lead = self._lead_with_answer(form)
        k = self._short_key()
        wizard = self._wizard(lead)
        wizard.tech_name = 'My %s Field!' % k.upper()
        wizard.action_promote()
        expected = 'x_meta_my_%s_field' % k
        self.assertEqual(wizard.tech_name, expected)
        self.assertTrue(self._new_field(expected))

    def test_wizard_view_has_both_steps(self):
        """The form offers the name step and the confirm step, and no
        longer shows the reuse checkbox."""
        from lxml import etree
        views = self.env['meta.promote.answer'].get_views([(False, 'form')])
        arch = etree.fromstring(views['views']['form']['arch'])
        names = [b.get('name') for b in arch.xpath('//footer//button[@name]')]
        self.assertEqual(names.count('action_promote'), 1)
        self.assertEqual(names.count('action_confirm_reuse'), 1)
        # Pick Another Name: secondary next to Reuse Field, primary alone.
        self.assertEqual(names.count('action_back_to_name'), 2)
        self.assertFalse(arch.xpath("//field[@name='reuse_confirmed']"))
        for fname in ('state', 'tech_name', 'usage_summary',
                      'existing_field_label', 'collision_note'):
            self.assertTrue(arch.xpath("//field[@name='%s']" % fname), fname)
        reuse = arch.xpath("//button[@name='action_confirm_reuse']")[0]
        self.assertIsNone(reuse.get('confirm'))

    def test_promote_refuses_an_empty_name(self):
        form = self._form()
        lead = self._lead_with_answer(form)
        wizard = self._wizard(lead)
        wizard.tech_name = '!!!'
        with self.assertRaisesRegex(UserError, 'no usable characters'):
            wizard.action_promote()
        self.assertFalse(self._mappings(form, self.K))

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


@tagged('post_install', '-at_install')
class TestPromotionMultiCompany(TransactionCase):
    """Promotion by a Meta Admin who only belongs to one company."""

    def setUp(self):
        super().setUp()
        self.co_a = self.env.company
        self.co_b = self.env['res.company'].create({'name': 'Promo Co B'})
        # Meta Admin alone gives no crm.lead rights, so add a sales group
        # like a real admin would have.
        self.admin_a = new_test_user(
            self.env, 'promo_admin_a',
            groups='base.group_user,'
                   'sales_team.group_sale_salesman_all_leads,'
                   'meta_lead_ads.group_meta_admin',
            company_id=self.co_a.id,
            company_ids=[Command.set([self.co_a.id])])
        self.K = 'q_%s' % self._testMethodName
        self.T = self.env['meta.promote.answer']._derive_tech_name(self.K)

    def _form(self, code, company):
        acc = self.env['meta.account'].create({
            'name': 'Acc %s' % code, 'account_id': 'MCACC%s' % code,
            'company_id': company.id})
        page = self.env['meta.page'].create({
            'name': 'Page %s' % code, 'page_id': 'MCPG%s' % code,
            'account_id': acc.id})
        return self.env['meta.lead.form'].create({
            'name': 'Form %s' % code, 'form_id': 'MCF%s' % code,
            'page_id': page.id})

    def _lead(self, form, company, value):
        lead = self.env['crm.lead'].create({
            'name': 'Lead %s' % value, 'type': 'lead',
            'company_id': company.id if company else False,
            'user_id': False, 'team_id': False,
            'meta_form_id_ref': form.id,
        })
        self.assertEqual(lead.company_id, company or self.env['res.company'])
        self.env['meta.lead.answer'].create({
            'lead_id': lead.id, 'question_key': self.K,
            'label': 'Budget', 'value': value,
        })
        return lead

    def test_single_company_admin_promotes_and_backfill_stays_in_company(self):
        """A Meta Admin limited to one company can promote while another
        company has leads with the same question, and the backfill only fills
        leads from the source form in the admin's companies.
        """
        form_a = self._form('A', self.co_a)
        form_b = self._form('B', self.co_b)
        self.assertEqual(form_a.company_id, self.co_a)
        self.assertEqual(form_b.company_id, self.co_b)

        lead_a = self._lead(form_a, self.co_a, 'A-value')
        lead_blank = self._lead(form_a, False, 'blank-value')
        lead_b_other_form = self._lead(form_b, self.co_b, 'B-value')
        # A company B lead can still point at a company A form.
        lead_b_same_form = self._lead(form_a, self.co_b, 'B2-value')

        wizard = self.env['meta.promote.answer'].with_user(
            self.admin_a).create({
                'answer_id': lead_a.answer_ids[:1].id,
                'lead_id': lead_a.id,
            })
        result = wizard.action_promote()
        self.assertEqual(result.get('type'), 'ir.actions.act_window')

        field = self.env['ir.model.fields'].search([
            ('model', '=', 'crm.lead'), ('name', '=', self.T)], limit=1)
        self.assertTrue(field, "the promoted field must be kept")
        self.assertTrue(self.env['meta.field.mapping'].search([
            ('form_id', '=', form_a.id), ('meta_key', '=', self.K)]))

        self.assertEqual(lead_a[self.T], 'A-value')
        self.assertEqual(lead_blank[self.T], 'blank-value')
        self.assertFalse(lead_b_other_form[self.T])
        self.assertFalse(lead_b_same_form[self.T])

    def _rerouted_lead(self):
        """A company A lead whose form's Page was later moved to company B."""
        form = self._form('R', self.co_a)
        form.page_id.company_id = self.co_b
        self.assertEqual(form.company_id, self.co_b)
        return self._lead(form, self.co_a, 'A-value')

    def _assert_nothing_created(self):
        self.assertFalse(self.env['ir.model.fields'].search([
            ('model', '=', 'crm.lead'), ('name', '=', self.T)]))
        self.assertFalse(self.env['meta.field.mapping'].search([
            ('meta_key', '=', self.K)]))
        self.assertFalse(self.env['ir.ui.view'].search([
            ('name', '=', 'crm.lead.form.meta.custom.%s' % self.T)]))

    def test_promote_refused_when_form_moved_to_foreign_company(self):
        """An admin outside the form's new company gets a plain refusal that
        does not name that company, and nothing is created.
        """
        lead = self._rerouted_lead()
        wizard = self.env['meta.promote.answer'].with_user(
            self.admin_a).create({
                'answer_id': lead.answer_ids[:1].id,
                'lead_id': lead.id,
            })
        with self.assertRaisesRegex(
                UserError, "company you don't have access to") as cm:
            wizard.action_promote()
        self.assertNotIn('Promo Co B', str(cm.exception))
        self._assert_nothing_created()

    def test_promote_refused_when_form_company_inactive(self):
        """An admin who belongs to the form's new company but has it switched
        off is told which company to switch to, and nothing is created.
        """
        admin_ab = new_test_user(
            self.env, 'promo_admin_ab',
            groups='base.group_user,'
                   'sales_team.group_sale_salesman_all_leads,'
                   'meta_lead_ads.group_meta_admin',
            company_id=self.co_a.id,
            company_ids=[Command.set([self.co_a.id, self.co_b.id])])
        lead = self._rerouted_lead()
        Wizard = self.env['meta.promote.answer'].with_user(
            admin_ab).with_context(allowed_company_ids=[self.co_a.id])
        wizard = Wizard.create({
            'answer_id': lead.answer_ids[:1].id,
            'lead_id': lead.id,
        })
        with self.assertRaisesRegex(UserError, "Promo Co B") as cm:
            wizard.action_promote()
        self.assertIn('Switch to that company', str(cm.exception))
        self._assert_nothing_created()

    # ---- who already uses the field ----------------------------------

    def _manual_field(self):
        model_rec = self.env['ir.model']._get('crm.lead')
        return self.env['ir.model.fields'].sudo().create({
            'name': self.T, 'field_description': 'Budget',
            'model_id': model_rec.id, 'model': 'crm.lead',
            'ttype': 'char', 'state': 'manual', 'store': True})

    def _map(self, form, key, field):
        return self.env['meta.field.mapping'].sudo().create({
            'form_id': form.id, 'meta_key': key, 'crm_field_id': field.id})

    def _confirm_step_as_admin_a(self, lead):
        wizard = self.env['meta.promote.answer'].with_user(
            self.admin_a).create({
                'answer_id': lead.answer_ids[:1].id,
                'lead_id': lead.id,
            })
        action = wizard.action_promote()
        self.assertEqual(action['res_id'], wizard.id)
        self.assertEqual(wizard.state, 'confirm')
        return wizard

    def test_usage_names_only_forms_the_admin_can_read(self):
        """Forms of another company are counted, never named."""
        field = self._manual_field()
        form_a = self._form('UA', self.co_a)
        form_a2 = self._form('UA2', self.co_a)
        form_b = self._form('UB', self.co_b)
        self._map(form_a2, 'seen_key', field)
        self._map(form_b, 'hidden_key', field)
        lead = self._lead(form_a, self.co_a, 'A-value')

        wizard = self._confirm_step_as_admin_a(lead)
        self.assertTrue(wizard.field_reusable)
        self.assertEqual(wizard.usage_summary.splitlines(), [
            '%s: seen_key' % form_a2.display_name,
            "and 1 other form you can't see",
        ])
        self.assertNotIn(form_b.display_name, wizard.usage_summary)
        self.assertNotIn('hidden_key', wizard.usage_summary)

    def test_usage_counts_forms_not_rows(self):
        """Two rows on one hidden form count as one form; two rows on one
        readable form are each listed and don't change that count."""
        field = self._manual_field()
        form_a = self._form('DA', self.co_a)
        form_a2 = self._form('DA2', self.co_a)
        form_b = self._form('DB', self.co_b)
        self._map(form_a2, 'seen_one', field)
        legacy_mapping(self.env, form_a2, self.T, meta_key='seen_two')
        self._map(form_b, 'hidden_one', field)
        legacy_mapping(self.env, form_b, self.T, meta_key='hidden_two')
        self.assertEqual(self.env['meta.field.mapping'].search_count([
            ('crm_field_id', '=', field.id)]), 4, "precondition")
        lead = self._lead(form_a, self.co_a, 'A-value')

        wizard = self._confirm_step_as_admin_a(lead)
        lines = wizard.usage_summary.splitlines()
        self.assertEqual(lines[-1], "and 1 other form you can't see")
        self.assertEqual(sorted(lines[:-1]), sorted([
            '%s: seen_one' % form_a2.display_name,
            '%s: seen_two' % form_a2.display_name,
        ]))
