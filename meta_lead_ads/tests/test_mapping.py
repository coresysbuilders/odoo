# Meta Lead Ads Integration for Odoo — © 2026 CoreSys Builders. All rights reserved.
# Licensed under the Odoo Proprietary License v1.0 (OPL-1).
# Unauthorized copying, redistribution, or resale of this software, in whole or in
# part, via any medium, is strictly prohibited and constitutes a license violation.
# OPL-1: https://www.odoo.com/documentation/19.0/legal/licenses.html#odoo-apps

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
        other = self.env['ir.model.fields'].search([('model', '!=', 'crm.lead')], limit=1)
        with self.assertRaises(ValidationError):
            self.env['meta.field.mapping'].create({
                'form_id': form.id, 'meta_key': 'x', 'crm_field_id': other.id})

    def test_mapping_accepts_typed_targets(self):
        """Typed crm.lead fields (monetary, relational, date, selection) are valid targets."""
        form = self._form()
        Field = self.env['ir.model.fields']
        for key, fname in (('budget', 'expected_revenue'),
                           ('country', 'country_id'),
                           ('interests', 'tag_ids'),
                           ('deadline', 'date_deadline'),
                           ('priority', 'priority')):
            m = self.env['meta.field.mapping'].create({
                'form_id': form.id, 'meta_key': key,
                'crm_field_id': Field._get('crm.lead', fname).id})
            self.assertEqual(m.crm_field_type, Field._get('crm.lead', fname).ttype)

    def test_mapping_rejects_system_and_readonly_targets(self):
        """System fields, company_id, our own meta_* columns and readonly fields are rejected."""
        form = self._form()
        Field = self.env['ir.model.fields']
        for fname in ('company_id', 'create_date', 'meta_campaign_name',
                      'meta_leadgen_id', 'meta_submitted_at'):
            with self.assertRaises(ValidationError, msg=fname):
                self.env['meta.field.mapping'].create({
                    'form_id': form.id, 'meta_key': 'k_%s' % fname,
                    'crm_field_id': Field._get('crm.lead', fname).id})
        readonly = Field.search([('model', '=', 'crm.lead'), ('store', '=', True),
                                 ('readonly', '=', True),
                                 ('ttype', 'in', ('char', 'integer', 'float'))],
                                limit=1)
        if readonly:
            with self.assertRaises(ValidationError):
                self.env['meta.field.mapping'].create({
                    'form_id': form.id, 'meta_key': 'ro',
                    'crm_field_id': readonly.id})

    def test_same_field_once_per_form(self):
        """Two questions on one form can't target the same field; one would overwrite the other."""
        form = self._form()
        city = self.env['ir.model.fields']._get('crm.lead', 'city')
        self.env['meta.field.mapping'].create({
            'form_id': form.id, 'meta_key': 'city', 'crm_field_id': city.id})
        with self.assertRaises(ValidationError):
            self.env['meta.field.mapping'].create({
                'form_id': form.id, 'meta_key': 'town',
                'crm_field_id': city.id})

    def test_same_field_across_forms_allowed(self):
        """Different forms may target the same field, since a lead comes from one form."""
        form_a = self._form()
        form_b = self.env['meta.lead.form'].create(
            {'name': 'F2', 'form_id': 'F2', 'page_id': form_a.page_id.id})
        city = self.env['ir.model.fields']._get('crm.lead', 'city')
        self.env['meta.field.mapping'].create({
            'form_id': form_a.id, 'meta_key': 'city', 'crm_field_id': city.id})
        m_b = self.env['meta.field.mapping'].create({
            'form_id': form_b.id, 'meta_key': 'your_city',
            'crm_field_id': city.id})
        self.assertTrue(m_b)

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
