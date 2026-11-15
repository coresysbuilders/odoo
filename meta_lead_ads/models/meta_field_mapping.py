# Meta Lead Ads Integration for Odoo — © 2026 CoreSys Builders. All rights reserved.
# Licensed under the Odoo Proprietary License v1.0 (OPL-1).
# Unauthorized copying, redistribution, or resale of this software, in whole or in
# part, via any medium, is strictly prohibited and constitutes a license violation.
# OPL-1: https://www.odoo.com/documentation/19.0/legal/licenses.html#odoo-apps

from odoo import api, fields, models, _
from odoo.exceptions import AccessError, ValidationError
from odoo.tools.translate import LazyTranslate

from .const import (
    MAPPABLE_TTYPES, MAPPING_BLOCKED_ADDON_FIELDS, MAPPING_BLOCKED_COMODELS,
    MAPPING_BLOCKED_FIELDS, MAPPING_BLOCKED_PREFIXES,
    MAPPING_FORBIDDEN_FIELDS, MAPPING_TECHNICAL_FIELDS,
    MAPPING_TECHNICAL_PREFIXES, is_blocked_target,
)

_lt = LazyTranslate(__name__)


def _like_prefix(prefix):
    # =like passes the pattern through as is, and an unescaped _ would
    # match any character, unlike the startswith check below.
    return (prefix.replace('\\', '\\\\').replace('%', '\\%')
            .replace('_', '\\_') + '%')


# Shared by the crm_field_id domain, the dropdown, the name gate and the
# constraint, so all four accept the same fields.
_TARGET_TERMS = [
    ('model', '=', 'crm.lead'), ('store', '=', True), ('readonly', '=', False),
    ('ttype', 'in', list(MAPPABLE_TTYPES)),
    ('name', 'not in', sorted(MAPPING_FORBIDDEN_FIELDS)),
    ('name', 'not in', sorted(MAPPING_TECHNICAL_FIELDS)),
    ('name', 'not in', sorted(MAPPING_BLOCKED_FIELDS)),
    ('name', 'not in', sorted(MAPPING_BLOCKED_ADDON_FIELDS)),
    # Fields without a relation are let through on their own term, so they
    # never depend on how 'not in' treats an empty column.
    '|', ('relation', '=', False),
    ('relation', 'not in', sorted(MAPPING_BLOCKED_COMODELS)),
] + [('name', 'not =like', _like_prefix(prefix))
     for prefix in MAPPING_TECHNICAL_PREFIXES + MAPPING_BLOCKED_PREFIXES]
_TARGET_DOMAIN = str(_TARGET_TERMS)


def _is_technical_target(name):
    return (name in MAPPING_TECHNICAL_FIELDS
            or name.startswith(MAPPING_TECHNICAL_PREFIXES))


# Meta's standard questions fill these fields before any mapping does, so a
# mapping onto one only counts when the standard answer is missing.
_TARGET_NOTES = {
    'email_from': _lt(
        "Meta's standard Email answer fills this field first. This answer "
        "is used only when that one is missing."),
    'phone': _lt(
        "Meta's standard Phone answer fills this field first. This answer "
        "is used only when that one is missing."),
    'contact_name': _lt(
        "Meta's standard Full Name answer fills this field first. This "
        "answer is used only when that one is missing."),
    'partner_name': _lt(
        "Meta's standard Company Name answer fills this field first. This "
        "answer is used only when that one is missing."),
}


class MetaFieldMapping(models.Model):
    _name = 'meta.field.mapping'
    _description = 'Meta Field Mapping'
    _rec_name = 'meta_key'

    form_id = fields.Many2one('meta.lead.form', string='Lead Form',
                              required=True, ondelete='cascade')
    meta_key = fields.Char(string='Meta Question Key', required=True)
    crm_field_id = fields.Many2one(
        'ir.model.fields', string='CRM Lead Field (record)', required=True,
        domain=_TARGET_DOMAIN,
        ondelete='cascade',                           # drop the mapping if the field is removed
    )
    # The many2one picker reads ir.model.fields, which only access-rights
    # admins may read, so the views pick the target by name and the name is
    # turned into the record here.
    crm_field_name = fields.Selection(
        selection='_selection_crm_field_name', string='CRM Lead Field',
        compute='_compute_crm_field_name', inverse='_inverse_crm_field_name',
        compute_sudo=True)
    # Targets the list above keeps only so existing rows stay readable. The
    # picker widget hides them unless the row already points at one.
    unoffered_targets = fields.Json(compute='_compute_unoffered_targets')
    crm_field_type = fields.Selection(related='crm_field_id.ttype',
                                      string='Field Type')
    # Date targets start as DD/MM/YYYY. Cleared by the admin, a date answer
    # is only read when it is a full ISO date.
    date_format = fields.Selection(
        [('dmy', 'DD/MM/YYYY'), ('mdy', 'MM/DD/YYYY'), ('ymd', 'YYYY-MM-DD')],
        string='Date Format',
        compute='_compute_date_format', store=True, readonly=False,
        precompute=True,
        help="How the form writes dates, for answers that are not ISO dates "
             "(YYYY-MM-DD). DD/MM/YYYY by default. Clear it to keep any "
             "other date as a Meta answer instead of guessing the day and "
             "month order.")
    # Archived rows are skipped on ingest. The upgrade archives rows whose
    # target may no longer be filled from a form and says why here; only
    # the module writes that text.
    active = fields.Boolean(default=True)
    disabled_reason = fields.Char(string='Why It Was Disabled', readonly=True,
                                  copy=False)
    target_note = fields.Char(string='Note', compute='_compute_target_note',
                              compute_sudo=True)

    # Odoo 19 replaces _sql_constraints with models.Constraint. An archived
    # row still holds its question key, so the way back is to retarget it.
    _form_meta_key_uniq = models.Constraint(
        'unique(form_id, meta_key)',
        "This form already has a mapping for this Meta question, "
        "possibly a disabled one. Use See disabled mappings on the form "
        "and retarget that row instead of adding a new one.",
    )

    @api.model
    def _offered_targets(self):
        # Read-only metadata of crm.lead fields, limited to what the
        # constraint below accepts. Labels follow the caller's language.
        targets = self.env['ir.model.fields'].sudo().search(
            _TARGET_TERMS, order='field_description, name')
        return [(f.name, '%s (%s)' % (f.field_description, f.name))
                for f in targets]

    @api.model
    def _selection_crm_field_name(self):
        # Existing rows can still point at a field that is no longer offered.
        # Listing it keeps their cell readable; the gate still refuses it.
        # Only rows the caller can see count, so other companies' targets
        # stay out of the list. Sudo is for the field metadata only.
        offered = self._offered_targets()
        names = {name for name, _label in offered}
        # Archived rows too, or a disabled row would show an empty target.
        visible = (self.with_context(active_test=False).search([])
                   if self.browse().has_access('read') else self.browse())
        used = visible.sudo().crm_field_id.filtered(
            lambda f: f.model == 'crm.lead' and f.name not in names)
        used = used.sorted(lambda f: (f.field_description or '', f.name))
        return offered + [(f.name, '%s (%s)' % (f.field_description, f.name))
                          for f in used]

    @api.depends('crm_field_id')
    def _compute_crm_field_name(self):
        for rec in self:
            rec.crm_field_name = rec.crm_field_id.name or False

    @api.depends_context('uid', 'allowed_company_ids')
    def _compute_unoffered_targets(self):
        # Not sudo: the extra names come from the rows the caller can see.
        offered = {name for name, _label in self._offered_targets()}
        unoffered = [name for name, _label in self._selection_crm_field_name()
                     if name not in offered]
        for rec in self:
            rec.unoffered_targets = unoffered

    @api.depends('crm_field_id')
    def _compute_target_note(self):
        for rec in self:
            note = _TARGET_NOTES.get(rec.crm_field_id.name)
            rec.target_note = self.env._(note) if note else False

    def _inverse_crm_field_name(self):
        # create and write already set the record; this only catches writes
        # that reach the field some other way.
        for rec in self:
            if (rec.crm_field_name
                    and rec.crm_field_name != rec.sudo().crm_field_id.name):
                rec.crm_field_id = rec._target_field_id(rec.crm_field_name)

    @api.onchange('crm_field_name')
    def _onchange_crm_field_name(self):
        # Set the record right away so the type and date format follow the
        # pick in the form.
        for rec in self:
            name = rec.crm_field_name
            if not name:
                rec.crm_field_id = False
                continue
            saved = rec._origin.sudo().crm_field_id
            if name == saved.name:
                # Going back to the saved target is fine even if it is no
                # longer offered; it is not a new pick.
                rec.crm_field_id = saved
                continue
            try:
                rec.crm_field_id = rec._target_field_id(name)
            except ValidationError as e:
                # Undo the pick and explain, instead of an error dialog.
                rec.crm_field_name = rec.sudo().crm_field_id.name or False
                return {'warning': {
                    'title': _("Field not available"),
                    'message': e.args[0],
                }}

    def _field_label(self, name):
        # Metadata only; Meta Admins without Settings can't read
        # ir.model.fields.
        field = self.env['ir.model.fields'].sudo()._get('crm.lead', name)
        return field.field_description or name

    def _blocked_error(self, name):
        return ValidationError(_(
            "%s is set by the module or by your sales team, so it can't be "
            "filled from a form answer. The answer is still saved on each "
            "lead, under Meta Answers.", self._field_label(name)))

    def _target_field_id(self, name):
        # The name comes from the client and is looked up as sudo, so only
        # names from the offered list get through.
        field = self.env['ir.model.fields'].sudo()._get('crm.lead', name)
        if is_blocked_target(name, field.relation):
            raise self._blocked_error(name)
        if name not in dict(self._offered_targets()):
            if name in dict(self._selection_crm_field_name()):
                raise ValidationError(_(
                    "%s is no longer offered as a mapping target. Pick "
                    "another CRM lead field.", name))
            raise ValidationError(_("Pick a CRM lead field from the list."))
        return field.id

    def _vals_with_target(self, vals):
        vals = dict(vals)
        name = vals.get('crm_field_name')
        if (name and self
                and all(rec.sudo().crm_field_id.name == name for rec in self)
                and vals.get('crm_field_id') in (None, False,
                                                 self.sudo().crm_field_id.id)):
            # The client sent back the name it already had, which may be a
            # field that is no longer offered. Nothing to change.
            vals.pop('crm_field_name')
            return vals
        if name:
            field_id = self._target_field_id(name)
            if vals.get('crm_field_id') and vals['crm_field_id'] != field_id:
                raise ValidationError(_(
                    "The CRM lead field and its name don't match."))
            vals['crm_field_id'] = field_id
        return vals

    @api.depends('crm_field_id')
    def _compute_date_format(self):
        # Runs only when the target changes, so a format the admin picked or
        # cleared on the same target stays as it is.
        for rec in self:
            if rec.crm_field_id.ttype not in ('date', 'datetime'):
                rec.date_format = False
            elif not rec.date_format:
                rec.date_format = 'dmy'

    def _check_disabled_reason_vals(self, vals_list):
        # The reason is the upgrade's record of what it switched off. A
        # crafted RPC must not be able to forge or wipe it.
        if self.env.su:
            return
        if any('disabled_reason' in vals for vals in vals_list):
            raise AccessError(_(
                "Why a mapping was turned off is recorded by the module and "
                "can't be edited."))

    @api.model_create_multi
    def create(self, vals_list):
        self._check_disabled_reason_vals(vals_list)
        vals_list = [self._vals_with_target(vals) for vals in vals_list]
        records = super().create(vals_list)
        if not self.env.su:
            records._check_form_access()
        self._meta_own_promoted_fields(records.mapped('crm_field_id'))
        return records

    def write(self, vals):
        self._check_disabled_reason_vals([vals])
        if 'crm_field_name' in vals:
            vals = self._vals_with_target(vals)
        if vals.get('active'):
            vals = self._vals_for_reactivation(vals)
        old_fields = (self.mapped('crm_field_id') if 'crm_field_id' in vals
                      else None)
        res = super().write(vals)
        if 'crm_field_id' in vals:
            self._clear_stale_reason(vals['crm_field_id'])
        if not self.env.su and 'form_id' in vals:
            self._check_form_access()
        if old_fields is not None:
            new_fields = self.mapped('crm_field_id')
            self._meta_own_promoted_fields(new_fields)
            self._meta_release_unused_fields(old_fields - new_fields)
        return res

    def unlink(self):
        fields_before = self.mapped('crm_field_id')
        res = super().unlink()
        self.env['meta.field.mapping']._meta_release_unused_fields(
            fields_before)
        return res

    # ------------------------------------------------------------ ownership
    # Kept out of @api.constrains, which runs as the caller on Odoo 18.
    # Only admin paths (mapping edits and the promote wizard) get here;
    # ingestion never changes which fields the module owns.

    @api.model
    def _meta_ownable_fields(self, field_recs):
        # Custom lead fields with our prefix only. Stock fields and fields of
        # this module are never touched here.
        return field_recs.sudo().exists().filtered(
            lambda f: f.model == 'crm.lead' and f.state == 'manual'
            and (f.name or '').startswith('x_meta_'))

    @api.model
    def _meta_own_promoted_fields(self, field_recs):
        """Make the module own the custom lead fields it fills.

        Uninstalling the module removes an owned field, its column and its
        values. The id is noupdate: manual fields are never reloaded from
        the module's data, so an upgrade would otherwise delete them. A
        field that already has an id, of this module or any other, is left
        as it is. Returns the fields that got a new id.
        """
        Data = self.env['ir.model.data'].sudo()
        owned = self.env['ir.model.fields'].sudo()
        for field in self._meta_ownable_fields(field_recs):
            if Data.search_count([('model', '=', 'ir.model.fields'),
                                  ('res_id', '=', field.id)], limit=1):
                continue
            xml_name = 'field_crm_lead__' + field.name
            if Data.search_count([('module', '=', 'meta_lead_ads'),
                                  ('name', '=', xml_name)], limit=1):
                continue
            Data.create({
                'module': 'meta_lead_ads',
                'name': xml_name,
                'model': 'ir.model.fields',
                'res_id': field.id,
                'noupdate': True,
            })
            owned |= field
        return owned

    @api.model
    def _meta_release_unused_fields(self, field_recs):
        """Give back custom lead fields the module no longer fills.

        A field stays owned while any mapping, archived ones included,
        points at it, or while the promote wizard's lead-form view shows
        it. Otherwise only this module's id is removed; the field and its
        values stay. Returns the fields that lost their id.
        """
        Data = self.env['ir.model.data'].sudo()
        Mapping = self.sudo().with_context(active_test=False)
        # An archived view still names the field.
        View = self.env['ir.ui.view'].sudo().with_context(active_test=False)
        # Only a lead view under the Meta tab is the wizard's, as in the
        # wizard and the upgrade. Without the tab no view counts.
        anchor = View.env.ref('meta_lead_ads.crm_lead_view_form_meta',
                              raise_if_not_found=False)
        released = self.env['ir.model.fields'].sudo()
        for field in self._meta_ownable_fields(field_recs):
            rows = Data.search([('module', '=', 'meta_lead_ads'),
                                ('model', '=', 'ir.model.fields'),
                                ('res_id', '=', field.id)])
            if not rows:
                continue
            if Mapping.search_count([('crm_field_id', '=', field.id)],
                                    limit=1):
                continue
            if anchor and View.search_count([
                    ('name', '=', 'crm.lead.form.meta.custom.' + field.name),
                    ('model', '=', 'crm.lead'),
                    ('inherit_id', '=', anchor.id)], limit=1):
                continue
            rows.unlink()
            released |= field
        return released

    def _clear_stale_reason(self, field_id):
        # A row retargeted to a valid field no longer matches the reason it
        # was turned off for, even while it stays archived.
        target = self.env['ir.model.fields'].sudo().browse(field_id or [])
        if not target or is_blocked_target(target.name, target.relation):
            return
        stale = self.filtered(lambda m: not m.active and m.disabled_reason)
        if stale:
            # Straight to the base write, so the guard on caller-sent
            # reasons above doesn't see our own key.
            super(MetaFieldMapping, stale).write({'disabled_reason': False})

    def _vals_for_reactivation(self, vals):
        # Superuser included: an archived row on a blocked field stays off
        # until it points at a field a form answer may fill.
        archived = self.filtered(lambda rec: not rec.active)
        if not archived:
            return vals
        new_target = self.env['ir.model.fields'].sudo().browse(
            vals.get('crm_field_id') or [])
        for rec in archived:
            target = (new_target if 'crm_field_id' in vals
                      else rec.sudo().crm_field_id)
            if is_blocked_target(target.name, target.relation):
                raise ValidationError(_(
                    "This mapping can't be turned back on: %s can no longer "
                    "be filled from a form answer. Pick another target field "
                    "first.", self._field_label(target.name)))
        vals = dict(vals)
        vals['disabled_reason'] = False
        return vals

    def _check_form_access(self):
        # A mapping changes how another company's leads are filled in, so it
        # may only sit on a form the writer can see. Checked here rather than
        # in @api.constrains, which always runs as sudo.
        Form = self.env['meta.lead.form']
        for rec in self.sudo():
            if not Form.browse(rec.form_id.id).has_access('read'):
                raise ValidationError(_(
                    "You can only add mappings to a form you can access."))

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
            # Constraints only run when crm_field_id is set or changed, so
            # rows that already point at such a field survive upgrades and
            # edits to their other columns.
            if _is_technical_target(field.name):
                raise ValidationError(_(
                    "The crm.lead field %s is a technical field and cannot be "
                    "a mapping target.", field.name))
            orm_field = self.env['crm.lead']._fields.get(field.name)
            if (not field.store or field.ttype not in MAPPABLE_TTYPES
                    or orm_field is None or orm_field.readonly):
                raise ValidationError(_(
                    "The mapping target must be a stored, editable crm.lead "
                    "field of a supported type (text, selection, checkbox, "
                    "number, date, or a link to other records)."))

    @api.constrains('crm_field_id')
    def _check_target_not_blocked(self):
        # Runs on every create and every target change, archived rows and
        # superuser included, but not when only active changes. So a legacy
        # row on a blocked field can still be archived as it is, while no
        # write can point a row at one.
        for rec in self:
            field = rec.sudo().crm_field_id
            if is_blocked_target(field.name, field.relation):
                raise rec._blocked_error(field.name)

    @api.constrains('form_id', 'crm_field_id', 'active')
    def _check_crm_field_once_per_form(self):
        # Two questions on the same form feeding one field would overwrite each
        # other on the same lead. Across forms it's fine, since a lead comes
        # from one form only (e.g. "budget" on several forms -> one Budget field).
        # Archived rows fill nothing, so they only count once switched back
        # on, whatever context the write came from (the Disabled Mappings
        # list opens with active_test off).
        Mapping = self.with_context(active_test=True)
        for rec in self:
            if not (rec.active and rec.form_id and rec.crm_field_id):
                continue
            others = Mapping.search_count([
                ('form_id', '=', rec.form_id.id),
                ('crm_field_id', '=', rec.crm_field_id.id),
                ('active', '=', True),
                ('id', '!=', rec.id)])
            if others:
                raise ValidationError(_(
                    "The crm.lead field %(field)s is already mapped from another "
                    "question on the form %(form)s. Map each field from at most "
                    "one question per form.",
                    field=rec.crm_field_id.name, form=rec.form_id.display_name))
