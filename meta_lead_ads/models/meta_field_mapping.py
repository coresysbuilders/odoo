# Meta Lead Ads Integration for Odoo — © 2026 CoreSys Builders. All rights reserved.
# Licensed under the Odoo Proprietary License v1.0 (OPL-1).
# Unauthorized copying, redistribution, or resale of this software, in whole or in
# part, via any medium, is strictly prohibited and constitutes a license violation.
# OPL-1: https://www.odoo.com/documentation/18.0/legal/licenses.html#odoo-apps

from odoo import api, fields, models, _
from odoo.exceptions import ValidationError

from .const import MAPPABLE_TTYPES, MAPPING_FORBIDDEN_FIELDS

# Same rules as _check_target_is_crm_lead, so the dropdown only offers fields
# the constraint accepts.
_TARGET_DOMAIN = (
    "[('model', '=', 'crm.lead'), ('store', '=', True), "
    "('readonly', '=', False), ('ttype', 'in', %r), ('name', 'not in', %r)]"
    % (list(MAPPABLE_TTYPES), sorted(MAPPING_FORBIDDEN_FIELDS)))


class MetaFieldMapping(models.Model):
    _name = 'meta.field.mapping'
    _description = 'Meta Field Mapping'

    form_id = fields.Many2one('meta.lead.form', string='Lead Form',
                              required=True, ondelete='cascade')
    meta_key = fields.Char(string='Meta Question Key', required=True)
    crm_field_id = fields.Many2one(
        'ir.model.fields', string='CRM Lead Field', required=True,
        domain=_TARGET_DOMAIN,
        ondelete='cascade',                           # drop the mapping if the field is removed
    )
    crm_field_type = fields.Selection(related='crm_field_id.ttype',
                                      string='Field Type')

    _sql_constraints = [
        ('form_meta_key_uniq', 'unique(form_id, meta_key)',
         'Each Meta question key must be mapped at most once per form.'),
    ]

    @api.constrains('crm_field_id')
    def _check_target_is_crm_lead(self):
        # Answers are converted to the target's type, so the target has to be
        # a stored, writable crm.lead field of a supported type. Writing to a
        # computed or non-stored field would fail or be silently dropped.
        for rec in self:
            field = rec.crm_field_id
            if not field:
                continue
            if field.model != 'crm.lead':
                raise ValidationError(_("The mapping target must be a crm.lead field."))
            if field.name in MAPPING_FORBIDDEN_FIELDS:
                raise ValidationError(_(
                    "The crm.lead field %s is managed by the system and cannot "
                    "be a mapping target.", field.name))
            orm_field = self.env['crm.lead']._fields.get(field.name)
            if (not field.store or field.ttype not in MAPPABLE_TTYPES
                    or orm_field is None or orm_field.readonly):
                raise ValidationError(_(
                    "The mapping target must be a stored, editable crm.lead "
                    "field of a supported type (text, selection, checkbox, "
                    "number, date, or a link to other records)."))

    @api.constrains('form_id', 'crm_field_id')
    def _check_crm_field_once_per_form(self):
        # Two questions on the same form feeding one field would overwrite each
        # other on the same lead. Across forms it's fine, since a lead comes
        # from one form only (e.g. "budget" on several forms -> one Budget field).
        for rec in self:
            if not (rec.form_id and rec.crm_field_id):
                continue
            others = self.search_count([
                ('form_id', '=', rec.form_id.id),
                ('crm_field_id', '=', rec.crm_field_id.id),
                ('id', '!=', rec.id)])
            if others:
                raise ValidationError(_(
                    "The crm.lead field %(field)s is already mapped from another "
                    "question on the form %(form)s. Map each field from at most "
                    "one question per form.",
                    field=rec.crm_field_id.name, form=rec.form_id.display_name))
