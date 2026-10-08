# Meta Lead Ads Integration for Odoo — © 2026 CoreSys Builders. All rights reserved.
# Licensed under the Odoo Proprietary License v1.0 (OPL-1).
# Unauthorized copying, redistribution, or resale of this software, in whole or in
# part, via any medium, is strictly prohibited and constitutes a license violation.
# OPL-1: https://www.odoo.com/documentation/18.0/legal/licenses.html#odoo-apps

"""Admin-only checks for the promotion wizard (``meta.promote.answer``).

Promoting an answer creates a custom field, so a non-admin is stopped twice:
the ACL blocks creating the wizard, and action_promote() checks
group_meta_admin itself in case it is called over RPC.
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
        # Internal user (base.group_user, so not a share user) with Meta
        # read access only.
        self.non_admin = self.env['res.users'].create({
            'name': 'Promote NonAdmin', 'login': 'promote_nonadmin',
            'groups_id': [(6, 0, [base_internal.id, self.user_group.id])]})
        self.meta_admin = self.env['res.users'].create({
            'name': 'Promote Admin', 'login': 'promote_admin',
            'groups_id': [(6, 0, [base_internal.id, self.admin_group.id])]})

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

    def test_non_admin_cannot_open_or_create_wizard(self):
        """A non-admin can't create the promote wizard."""
        form = self._form()
        lead, answer = self._lead_with_answer(form)
        with self.assertRaises(AccessError):
            self.env['meta.promote.answer'].with_user(self.non_admin).create({
                'answer_id': answer.id, 'lead_id': lead.id})

    def test_non_admin_direct_method_call_raises(self):
        """action_promote() raises for a non-admin even on a wizard an admin created."""
        form = self._form()
        lead, answer = self._lead_with_answer(form)
        wizard = self.env['meta.promote.answer'].with_user(
            self.meta_admin).create({
                'answer_id': answer.id, 'lead_id': lead.id})
        with self.assertRaises(AccessError):
            wizard.with_user(self.non_admin).action_promote()
