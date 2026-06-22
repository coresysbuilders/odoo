# Meta Lead Ads Integration for Odoo — © 2026 CoreSys Builders. All rights reserved.
# Licensed under the Odoo Proprietary License v1.0 (OPL-1).
# Unauthorized copying, redistribution, or resale of this software, in whole or in
# part, via any medium, is strictly prohibited and constitutes a license violation.
# OPL-1: https://www.odoo.com/documentation/18.0/legal/licenses.html#odoo-apps

from odoo import api, fields, models, _
from odoo.exceptions import ValidationError


class MetaFieldMapping(models.Model):
    _name = 'meta.field.mapping'
    _description = 'Meta Field Mapping'

    form_id = fields.Many2one('meta.lead.form', string='Lead Form',
                              required=True, ondelete='cascade')   # per-form
    meta_key = fields.Char(string='Meta Question Key', required=True)  # free text
    crm_field_id = fields.Many2one(
        'ir.model.fields', string='CRM Lead Field', required=True,
        domain="[('model', '=', 'crm.lead')]",       # dropdown limited to crm.lead
        ondelete='cascade',                           # drop the mapping if the field is removed
    )

    _sql_constraints = [
        ('form_meta_key_uniq', 'unique(form_id, meta_key)',
         'Each Meta question key must be mapped at most once per form.'),
    ]

    @api.constrains('crm_field_id')
    def _check_target_is_crm_lead(self):
        # The ingest writes a joined display string into the target, so an
        # override target must be a stored, text-like crm.lead field. A
        # non-stored (computed) target makes match.write(...) raise; a non-text
        # target makes the string write fail or clobber a typed value.
        for rec in self:
            field = rec.crm_field_id
            if not field:
                continue
            if field.model != 'crm.lead':
                raise ValidationError("The mapping target must be a crm.lead field.")
            if not field.store or field.ttype not in ('char', 'text'):
                raise ValidationError(
                    "The mapping target must be a stored text field on crm.lead "
                    "(char/text); the Meta answer is written as text.")

    @api.constrains('crm_field_id')
    def _check_crm_field_single_active_mapping(self):
        # Cross-form clobber guard. A mapping is scoped to ONE
        # source form; this constraint additionally forbids binding the SAME
        # physical crm.lead column to more than one active mapping ACROSS forms.
        # Rationale: if two different Meta forms map their distinct question keys
        # to the same crm.lead column, real-time ingestion of leads from both
        # forms would continually overwrite that column, corrupting data. The
        # existing unique(form_id, meta_key) only blocks the duplicate (form,
        # key); this is the stricter cross-form guard. The SAME (form, key)
        # mapping stays permitted (it is the same row).
        for rec in self:
            if not rec.crm_field_id:
                continue
            others = self.env['meta.field.mapping'].search_count([
                ('crm_field_id', '=', rec.crm_field_id.id),
                ('id', '!=', rec.id)])
            if others:
                raise ValidationError(_(
                    "The crm.lead field %s is already mapped by another Meta "
                    "form. One promoted field must not be reused across forms "
                    "with different question keys (it would cause ingestion to "
                    "clobber values back and forth).") % rec.crm_field_id.name)
