# Meta Lead Ads Integration for Odoo — © 2026 CoreSys Builders. All rights reserved.
# Licensed under the Odoo Proprietary License v1.0 (OPL-1).
# Unauthorized copying, redistribution, or resale of this software, in whole or in
# part, via any medium, is strictly prohibited and constitutes a license violation.
# OPL-1: https://www.odoo.com/documentation/18.0/legal/licenses.html#odoo-apps

from odoo.tests.common import TransactionCase, tagged
from odoo.exceptions import ValidationError


@tagged('post_install', '-at_install')
class TestMapping(TransactionCase):
    def _form(self):
        acc = self.env['meta.account'].create({'name': 'A', 'account_id': 'A'})
        page = self.env['meta.page'].create({'name': 'P', 'page_id': 'P', 'account_id': acc.id})
        return self.env['meta.lead.form'].create({'name': 'F', 'form_id': 'F', 'page_id': page.id})

    def test_mapping_target_constrained(self):
        form = self._form()
        lead_field = self.env['ir.model.fields']._get('crm.lead', 'email_from')
        m = self.env['meta.field.mapping'].create({
            'form_id': form.id, 'meta_key': 'email', 'crm_field_id': lead_field.id})
        self.assertTrue(m)
        # a non-crm.lead field must be rejected by @api.constrains
        other = self.env['ir.model.fields'].search([('model', '!=', 'crm.lead')], limit=1)
        with self.assertRaises(ValidationError):
            self.env['meta.field.mapping'].create({
                'form_id': form.id, 'meta_key': 'x', 'crm_field_id': other.id})

    def test_mapping_target_must_be_stored_text(self):
        """The ingest writes a joined display STRING, so an override target must
        be a STORED text field. A non-text crm.lead field (monetary
        expected_revenue) is rejected — a string write to it would fail or
        clobber a typed value."""
        form = self._form()
        money_field = self.env['ir.model.fields']._get(
            'crm.lead', 'expected_revenue')
        with self.assertRaises(ValidationError):
            self.env['meta.field.mapping'].create({
                'form_id': form.id, 'meta_key': 'budget',
                'crm_field_id': money_field.id})

    def test_mapping_target_rejects_html(self):
        """Raw Meta answers must not be routed into stored HTML fields."""
        form = self._form()
        html_field = self.env['ir.model.fields'].search([
            ('model', '=', 'crm.lead'),
            ('ttype', '=', 'html'),
            ('store', '=', True),
        ], limit=1)
        if not html_field:
            return
        with self.assertRaises(ValidationError):
            self.env['meta.field.mapping'].create({
                'form_id': form.id, 'meta_key': 'html_answer',
                'crm_field_id': html_field.id})
