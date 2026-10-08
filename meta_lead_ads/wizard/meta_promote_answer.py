# Meta Lead Ads Integration for Odoo — © 2026 CoreSys Builders. All rights reserved.
# Licensed under the Odoo Proprietary License v1.0 (OPL-1).
# Unauthorized copying, redistribution, or resale of this software, in whole or in
# part, via any medium, is strictly prohibited and constitutes a license violation.
# OPL-1: https://www.odoo.com/documentation/19.0/legal/licenses.html#odoo-apps

# Admin wizard that turns a captured form answer into a real crm.lead field.
# This is the only place the module creates ir.model.fields records; ingestion
# never does. Everything runs in one savepoint so a failure halfway leaves no
# orphan field, view or mapping.
import hashlib
import logging
import re
from xml.sax.saxutils import quoteattr

from odoo import api, fields, models, _
from odoo.exceptions import UserError, AccessError

_logger = logging.getLogger(__name__)

# Postgres caps identifiers at 63 bytes, not characters.
_MAX_IDENT_BYTES = 63
_PREFIX = 'x_meta_'


class MetaPromoteAnswer(models.TransientModel):
    _name = 'meta.promote.answer'
    _description = 'Promote Meta Answer to crm.lead Field (admin)'

    # The answer-line button passes default_answer_id; the other values are
    # computed from it so they match however the wizard is opened.
    answer_id = fields.Many2one('meta.lead.answer', required=True,
                                ondelete='cascade')
    lead_id = fields.Many2one('crm.lead', string='Lead')
    question_key = fields.Char(
        string='Meta Question Key',
        compute='_compute_from_answer', store=True, readonly=False)
    field_label = fields.Char(
        string='Field Label',
        compute='_compute_from_answer', store=True, readonly=False)
    tech_name = fields.Char(string='Technical Name', readonly=True)
    reuse_confirmed = fields.Boolean(string='Reuse existing custom field')

    @api.depends('answer_id')
    def _compute_from_answer(self):
        for wiz in self:
            answer = wiz.answer_id
            if answer:
                # question_key is stable across languages; the label is only
                # used for the field's display name.
                if not wiz.question_key:
                    wiz.question_key = answer.question_key
                if not wiz.field_label:
                    wiz.field_label = answer.label or answer.question_key
            else:
                wiz.question_key = wiz.question_key or False
                wiz.field_label = wiz.field_label or False

    # ------------------------------------------------------------------ name
    @api.model
    def _derive_tech_name(self, question_key):
        """Build a safe ``x_meta_<key>`` column name from a Meta question_key.

        The key comes from the form author, so it is reduced to [a-z0-9_].
        A key with nothing usable left becomes ``x_meta_<sha1[:8]>``. Long keys
        are trimmed and get a hash of the full original key appended, so two
        keys with the same long prefix still get different names.
        """
        raw = question_key or ''
        # Hash the original key so trimmed names stay distinct.
        h = hashlib.sha1(raw.encode('utf-8')).hexdigest()[:8]

        lowered = raw.strip().lower()
        # Avoid x_meta_x_meta_... when the key already carries the prefix.
        if lowered.startswith(_PREFIX):
            lowered = lowered[len(_PREFIX):]
        core = re.sub(r'[^a-z0-9_]+', '_', lowered)
        core = re.sub(r'_+', '_', core).strip('_')

        if not core:
            return _PREFIX + h

        suffix = '_' + h
        candidate = _PREFIX + core + suffix
        if len(candidate.encode('utf-8')) <= _MAX_IDENT_BYTES:
            # Short keys get a readable name with no hash: 'budget' becomes
            # 'x_meta_budget'.
            plain = _PREFIX + core
            if len(plain.encode('utf-8')) <= _MAX_IDENT_BYTES:
                return plain
            return candidate

        # Too long: trim the middle part by bytes, keep prefix and hash.
        budget = _MAX_IDENT_BYTES - len(_PREFIX.encode('utf-8')) \
            - len(suffix.encode('utf-8'))
        trimmed = core.encode('utf-8')[:budget].decode('utf-8', 'ignore')
        trimmed = trimmed.strip('_') or h
        return _PREFIX + trimmed + suffix

    # ----------------------------------------------------------------- notify
    def _notify_already_promoted(self, field):
        """Tell the user this question already has a field, instead of raising."""
        return {
            'type': 'ir.actions.client',
            'tag': 'display_notification',
            'params': {
                'message': _("This question is already promoted to %s.",
                             field.field_description or field.name),
                'type': 'warning',
                'sticky': False,
            },
        }

    # ---------------------------------------------------------------- promote
    def action_promote(self):
        self.ensure_one()
        # The ACL already limits the wizard to admins; check again here in
        # case the method is called over RPC.
        if not self.env.user.has_group('meta_lead_ads.group_meta_admin'):
            raise AccessError(
                _("Only Meta administrators may promote answers to fields."))

        answer = self.answer_id
        source_form = answer.lead_id.meta_form_id_ref
        question_key = self.question_key or answer.question_key

        # The lead may have no form (never synced, or deleted since), and the
        # mapping needs one. Stop before creating anything.
        if not source_form:
            raise UserError(_(
                "Cannot promote: this lead has no associated Meta form. The "
                "form may have been deleted or was never discovered. Re-link "
                "the lead to a form first."))

        tech_name = self._derive_tech_name(question_key)
        self.tech_name = tech_name

        with self.env.cr.savepoint():
            Mapping = self.env['meta.field.mapping']
            # Reading ir.model.fields needs Settings rights, which a Meta admin
            # may not have. The group check above is the real gate, so sudo.
            IMF = self.env['ir.model.fields'].sudo()

            existing_map = Mapping.search([
                ('form_id', '=', source_form.id),
                ('meta_key', '=', question_key)], limit=1)
            existing_field = IMF.search([
                ('model', '=', 'crm.lead'),
                ('name', '=', tech_name)], limit=1)

            reusable = (
                bool(existing_field)
                and existing_field.state == 'manual'
                and existing_field.store
                and existing_field.ttype in ('char', 'text'))

            if existing_map and existing_map.crm_field_id:
                return self._notify_already_promoted(existing_map.crm_field_id)

            if existing_field and not existing_map and reusable:
                # An earlier promote created the field but died before the
                # mapping. Pick up where it left off.
                field = existing_field
            else:
                # Name taken: only reuse a stored manual char/text field, and
                # only if the admin ticked the reuse box.
                name_exists = (tech_name in self.env['crm.lead']._fields) \
                    or bool(existing_field)
                if name_exists:
                    if not (self.reuse_confirmed and reusable):
                        raise UserError(_(
                            "A field named %s already exists on Leads. Choose a "
                            "different name, or confirm reuse of the existing "
                            "custom field.") % tech_name)
                    field = existing_field
                else:
                    # Stored manual char field: what the mapping constraint
                    # accepts.
                    model_rec = self.env['ir.model'].sudo()._get('crm.lead')
                    field = IMF.sudo().create({
                        'name': tech_name,
                        'field_description': self.field_label or question_key,
                        'model_id': model_rec.id,
                        'model': 'crm.lead',
                        'ttype': 'char',
                        'state': 'manual',
                        'store': True,
                    })

            self._inject_view(tech_name)

            # If two admins race here, unique(form_id, meta_key) rejects the
            # second insert and the savepoint rolls it back.
            if not existing_map:
                Mapping.create({
                    'form_id': source_form.id,
                    'meta_key': question_key,
                    'crm_field_id': field.id,
                })

            self._dispatch_backfill(field, source_form)

        target_lead = self.lead_id or answer.lead_id
        return {
            'type': 'ir.actions.act_window',
            'res_model': 'crm.lead',
            'res_id': target_lead.id,
            'view_mode': 'form',
            'target': 'current',
        }

    # ------------------------------------------------------------------ view
    def _inject_view(self, tech_name):
        """Add the new field to the lead form through a small inherited view.

        The view name is derived from the field, so a second call finds it and
        does nothing. If the anchor view is missing, log and skip; the field
        still works without it.
        """
        View = self.env['ir.ui.view']
        view_name = 'crm.lead.form.meta.custom.%s' % tech_name
        if View.sudo().search([('name', '=', view_name)], limit=1):
            return
        anchor = self.env.ref(
            'meta_lead_ads.crm_lead_view_form_meta', raise_if_not_found=False)
        if not anchor:
            _logger.warning(
                "Promote: anchor view 'meta_lead_ads.crm_lead_view_form_meta' "
                "missing; skipping inherited-view injection for %s (field still "
                "created).", tech_name)
            return
        # tech_name is already [a-z0-9_], but quote it anyway so the arch can't
        # break if the sanitizer changes. quoteattr adds the quotes itself.
        arch = (
            '<data>'
            '<xpath expr="//group[@name=\'meta_custom_fields\']" '
            'position="inside">'
            '<field name=%s/>'
            '</xpath>'
            '</data>'
        ) % quoteattr(tech_name)
        View.sudo().create({
            'name': view_name,
            'model': 'crm.lead',
            'inherit_id': anchor.id,
            'arch': arch,
        })

    # --------------------------------------------------------------- backfill
    def _dispatch_backfill(self, field, source_form):
        """Rebuild the registry so the new field is usable, then backfill now."""
        # Fail loudly if the column still isn't registered after the rebuild;
        # otherwise the backfill writes would be silently dropped.
        self.env['ir.model.fields'].invalidate_model()
        # Odoo 19 renamed Registry.setup_models to _setup_models__. There is no
        # public equivalent; core calls it by this name too.
        self.env.registry._setup_models__(self.env.cr)
        self.env['crm.lead'].invalidate_model()
        assert field.name in self.env['crm.lead']._fields, (
            "promoted field %s not registered after registry rebuild" % field.name)
        self._run_backfill(field, source_form)

    def _run_backfill(self, field, source_form, batch_size=500):
        """Copy stored answers for this question into the new field.

        Only leads from source_form are touched; the same key on another form
        is left alone. Per lead, the first answer (by sequence) wins. Empty
        answers are skipped, and a field that already has a value is never
        overwritten, so salespeople's edits survive.
        """
        Answer = self.env['meta.lead.answer']
        field_name = field.name
        question_key = self.question_key or self.answer_id.question_key

        domain = [('question_key', '=', question_key)]
        offset = 0
        seen_leads = set()
        while True:
            # Ordered so the first row seen for a lead is its first answer.
            answers = Answer.search(domain, offset=offset, limit=batch_size,
                                    order='lead_id, sequence, id')
            if not answers:
                break
            for ans in answers:
                lead = ans.lead_id
                if lead.id in seen_leads:
                    continue
                if lead.meta_form_id_ref != source_form:
                    continue
                seen_leads.add(lead.id)
                value = ans.value
                if not value:
                    continue
                if lead[field_name]:
                    continue
                lead.write({field_name: value})
            offset += batch_size
