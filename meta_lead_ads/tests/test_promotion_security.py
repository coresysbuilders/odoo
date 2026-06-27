# Meta Lead Ads Integration for Odoo — © 2026 CoreSys Builders. All rights reserved.
# Licensed under the Odoo Proprietary License v1.0 (OPL-1).
# Unauthorized copying, redistribution, or resale of this software, in whole or in
# part, via any medium, is strictly prohibited and constitutes a license violation.
# OPL-1: https://www.odoo.com/documentation/19.0/legal/licenses.html#odoo-apps

"""Admin-gating tests for the promotion wizard (``meta.promote.answer``).

A non-admin internal user must not be able to drive schema mutation, neither
via the ACL row nor via a direct RPC method call. Both layers are pinned:

  (S1) ACL boundary — a non-admin (base.group_user, not group_meta_admin)
       cannot even create the wizard record -> AccessError.
  (S2) in-method has_group gate — even if the wizard is built as an admin,
       invoking the server method as a non-admin
       (``wizard.with_user(non_admin).action_promote()``) raises AccessError
       because action_promote performs an in-method
       has_group('meta_lead_ads.group_meta_admin') check beyond the ACL row.
       This proves a non-admin cannot drive schema mutation even if they reach
       the method via RPC, not merely that the UI button is hidden.

Conventions:
  - base.group_user makes the non-admin a realistic internal (non-share) user;
    without it Odoo sets share=True (portal semantics) and ACL tests pass for
    the wrong reasons.
  - assertRaises takes a single exception class, never a tuple (a tuple
    TypeErrors at runtime).
"""
from odoo.tests.common import TransactionCase, tagged
from odoo.exceptions import AccessError


@tagged('post_install', '-at_install')
class TestPromotionSecurity(TransactionCase):
    def setUp(self):
        super().setUp()
        base_internal = self.env.ref('base.group_user')
        self.user_group = self.env.ref('meta_lead_ads.group_meta_user')
        self.admin_group = self.env.ref('meta_lead_ads.group_meta_admin')
        # A non-admin internal user: base.group_user + group_meta_user, but
        # NOT group_meta_admin.
        self.non_admin = self.env['res.users'].create({
            'name': 'Promote NonAdmin', 'login': 'promote_nonadmin',
            'group_ids': [(6, 0, [base_internal.id, self.user_group.id])]})
        # An admin who CAN build the wizard (for the S2 direct-call test).
        self.meta_admin = self.env['res.users'].create({
            'name': 'Promote Admin', 'login': 'promote_admin',
            'group_ids': [(6, 0, [base_internal.id, self.admin_group.id])]})

    def _form(self):
        acc = self.env['meta.account'].create({'name': 'A', 'account_id': 'A'})
        page = self.env['meta.page'].create(
            {'name': 'P', 'page_id': 'P', 'account_id': acc.id})
        return self.env['meta.lead.form'].create(
            {'name': 'F', 'form_id': 'F', 'page_id': page.id})

    def _lead_with_answer(self, form):
        lead = self.env['crm.lead'].create({
            'name': 'Lead', 'type': 'lead', 'meta_form_id_ref': form.id})
        answer = self.env['meta.lead.answer'].create({
            'lead_id': lead.id, 'question_key': 'budget',
            'label': 'Budget', 'value': '$5k'})
        return lead, answer

    # ---- (S1) ACL boundary -----------------------------------------------

    def test_non_admin_cannot_open_or_create_wizard(self):
        """A non-admin (group_meta_user only, NOT group_meta_admin) attempting
        to create the promote wizard raises AccessError (ACL boundary)."""
        form = self._form()
        lead, answer = self._lead_with_answer(form)
        with self.assertRaises(AccessError):
            self.env['meta.promote.answer'].with_user(self.non_admin).create({
                'answer_id': answer.id, 'lead_id': lead.id})

    # ---- (S2) in-method has_group gate (defense-in-depth) -----------------

    def test_non_admin_direct_method_call_raises(self):
        """Build the wizard AS ADMIN, then invoke the SERVER METHOD directly as
        a non-admin: wizard.with_user(non_admin).action_promote() MUST raise
        AccessError because action_promote performs an in-method
        has_group('meta_lead_ads.group_meta_admin') check BEYOND the ACL row.
        Proves a non-admin cannot drive schema mutation even via RPC, not just
        that the UI button is hidden. Single-class assertRaises, NO tuple."""
        form = self._form()
        lead, answer = self._lead_with_answer(form)
        # Built by an admin (legitimately reachable).
        wizard = self.env['meta.promote.answer'].with_user(
            self.meta_admin).create({
                'answer_id': answer.id, 'lead_id': lead.id})
        with self.assertRaises(AccessError):
            wizard.with_user(self.non_admin).action_promote()
