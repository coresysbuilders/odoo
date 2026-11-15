# Meta Lead Ads Integration for Odoo — © 2026 CoreSys Builders. All rights reserved.
# Licensed under the Odoo Proprietary License v1.0 (OPL-1).
# Unauthorized copying, redistribution, or resale of this software, in whole or in
# part, via any medium, is strictly prohibited and constitutes a license violation.
# OPL-1: https://www.odoo.com/documentation/19.0/legal/licenses.html#odoo-apps

# Admin wizard that turns a captured form answer into a real crm.lead field.
# This is the only place the module creates ir.model.fields records; ingestion
# never does. The module also owns those fields and their lead-form view, so
# uninstalling it removes them. Everything runs in one savepoint so a failure
# halfway leaves no orphan field, view or mapping.
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
# What a cleaned name looks like. It ends up in DDL and view arch.
_CLEAN_NAME = re.compile(r'x_meta_[a-z0-9_]+')


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
    tech_name = fields.Char(
        string='Technical Name',
        compute='_compute_tech_name', store=True, readonly=False,
        help="The field's name in the database. Lowercase letters, digits "
             "and underscores only; it always starts with x_meta_.")
    # Kept so older callers that still send it don't break. It no longer
    # skips the confirm step.
    reuse_confirmed = fields.Boolean(string='Reuse existing custom field')
    state = fields.Selection(
        [('name', 'Name'), ('confirm', 'Confirm Reuse')],
        default='name', required=True)
    # What the confirm step shows. Only for display: action_confirm_reuse
    # works it all out again, since the client can write these.
    existing_field_label = fields.Char(string='Existing Field', readonly=True)
    field_reusable = fields.Boolean(readonly=True)
    field_on_form = fields.Boolean(readonly=True)
    resume_hint = fields.Boolean(readonly=True)
    collision_note = fields.Char(readonly=True)
    usage_summary = fields.Text(string='Already Used By', readonly=True)

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

    @api.depends('question_key')
    def _compute_tech_name(self):
        # Filled when the wizard opens, so the admin sees the name before
        # clicking Promote and can change it.
        for wiz in self:
            wiz.tech_name = (self._derive_tech_name(wiz.question_key)
                             if wiz.question_key else False)

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

    @api.model
    def _clean_tech_name(self, value):
        """Make a name the admin typed safe to use as a column name.

        A name that is already clean is returned unchanged, so a derived
        name with its hash suffix survives any number of round trips.
        Deriving it again would add a second hash. Anything else is
        lowercased, cut down to [a-z0-9_], prefixed and kept within 63 bytes.
        """
        value = value or ''
        if (_CLEAN_NAME.fullmatch(value)
                and len(value.encode('utf-8')) <= _MAX_IDENT_BYTES):
            return value
        lowered = value.strip().lower()
        if lowered.startswith(_PREFIX):
            lowered = lowered[len(_PREFIX):]
        core = re.sub(r'[^a-z0-9_]+', '_', lowered)
        core = re.sub(r'_+', '_', core).strip('_')
        if not core:
            raise UserError(_(
                "That name has no usable characters left. Use letters, "
                "digits or underscores."))
        # core is plain ASCII by now, so characters and bytes are the same.
        core = core[:_MAX_IDENT_BYTES - len(_PREFIX)].rstrip('_')
        return _PREFIX + core

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

    # ----------------------------------------------------------------- checks
    def _check_admin(self):
        # The ACL already limits the wizard to admins; check again here in
        # case a step is called over RPC.
        if not self.env.user.has_group('meta_lead_ads.group_meta_admin'):
            raise AccessError(
                _("Only Meta administrators may promote answers to fields."))

    def _check_promote_access(self):
        """Run the checks every promote step needs.

        Returns the lead's form and the question key.
        """
        self._check_admin()
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

        # The form's Page may have been moved to another company since the
        # lead came in. The mapping would change how that company's future
        # leads are filled, so stop here with a clear message rather than
        # failing later, after the field and view were already made.
        if not self.env['meta.lead.form'].browse(source_form.id).has_access('read'):
            form_company = source_form.sudo().company_id
            if form_company and form_company in self.env.user.company_ids:
                raise UserError(_(
                    "Cannot promote: this lead's form now belongs to %s. "
                    "Switch to that company to promote this answer.",
                    form_company.name))
            raise UserError(_(
                "Cannot promote: this lead's form belongs to a company you "
                "don't have access to. Ask an administrator of that company "
                "to promote this answer."))
        return source_form, question_key

    def _wanted_tech_name(self, question_key):
        # An edited name is cleaned, never derived again.
        if self.tech_name:
            return self._clean_tech_name(self.tech_name)
        return self._derive_tech_name(question_key)

    def _question_mapping(self, source_form, question_key):
        # Archived rows too: a switched-off row still holds the question key,
        # and unique(form_id, meta_key) counts it.
        Mapping = self.env['meta.field.mapping']
        return Mapping.with_context(active_test=False).search([
            ('form_id', '=', source_form.id),
            ('meta_key', '=', question_key)], limit=1)

    # ------------------------------------------------------------------ reuse
    def _field_reuse_state(self, tech_name, source_form, question_key):
        """Say whether tech_name exists on leads and may take this question.

        The confirm step shows this and action_confirm_reuse enforces it, so
        both read it from here and can't disagree. Field metadata and the
        counts are read as sudo; forms are only named from mappings the user
        can read.
        """
        Lead = self.env['crm.lead']
        # A Meta admin may lack Settings rights, which ir.model.fields needs.
        # The admin check has already run.
        field = self.env['ir.model.fields'].sudo()._get('crm.lead', tech_name)
        result = {
            'exists': bool(field) or tech_name in Lead._fields,
            'field': field,
            'label': False,
            'reusable': False,
            'on_form': False,
            'resume': False,
            'collision_note': False,
            'usage_summary': False,
        }
        if not result['exists']:
            return result
        result['label'] = (field.field_description if field
                           else Lead._fields[tech_name].string)
        if not field:
            return result

        # Archived rows fill nothing, so only active ones count as users,
        # whatever context the wizard was opened with.
        Mapping = self.env['meta.field.mapping'].with_context(active_test=True)
        all_rows = Mapping.sudo().search([('crm_field_id', '=', field.id),
                                          ('active', '=', True)])
        # Another question on this form already fills the field, and a form
        # may feed a field from one question only.
        on_form = any(row.form_id.id == source_form.id
                      and row.meta_key != question_key for row in all_rows)
        # Same limits as the mapping's target check, so a reuse that saving
        # would refuse (computed or related field) is never offered.
        orm_field = Lead._fields.get(tech_name)
        reusable = (field.state == 'manual' and field.store
                    and field.ttype in ('char', 'text')
                    and not field.related
                    and orm_field is not None and not orm_field.readonly
                    and not on_form)
        result.update(
            on_form=on_form,
            reusable=reusable,
            # Probably left by a promotion that stopped halfway.
            resume=reusable and not all_rows,
        )

        readable = Mapping.search([('crm_field_id', '=', field.id),
                                   ('active', '=', True)])
        readable = readable.sorted(
            lambda m: (m.form_id.display_name or '', m.meta_key or '', m.id))
        other = readable.filtered(
            lambda m: m.meta_key != question_key
            and self._derive_tech_name(m.meta_key) == tech_name)[:1]
        if other:
            result['collision_note'] = _(
                'The question keys "%(this)s" and "%(other)s" both turn into '
                'this name.', this=question_key, other=other.meta_key)

        lines = ['%s: %s' % (m.form_id.display_name, m.meta_key)
                 for m in readable]
        # Counted by form, not by row, and never named.
        hidden = len(set(all_rows.form_id.ids) - set(readable.form_id.ids))
        if hidden == 1:
            lines.append(_("and 1 other form you can't see"))
        elif hidden:
            lines.append(_("and %s other forms you can't see", hidden))
        result['usage_summary'] = '\n'.join(lines) or False
        return result

    def _reopen(self):
        return {
            'type': 'ir.actions.act_window',
            'name': _("Promote to Field"),
            'res_model': self._name,
            'res_id': self.id,
            'view_mode': 'form',
            'target': 'new',
        }

    def _lead_action(self):
        target_lead = self.lead_id or self.answer_id.lead_id
        return {
            'type': 'ir.actions.act_window',
            'res_model': 'crm.lead',
            'res_id': target_lead.id,
            'view_mode': 'form',
            'target': 'current',
        }

    def _link_mapping(self, source_form, question_key, field):
        """Show the field on the lead form and map the question to it."""
        self._inject_view(field.name)
        # The mapping create or write below owns the field too, but an
        # already active mapping runs neither.
        self.env['meta.field.mapping']._meta_own_promoted_fields(field)
        mapping = self._question_mapping(source_form, question_key)
        if not mapping:
            # If two admins race here, unique(form_id, meta_key) rejects the
            # second insert and the caller's savepoint rolls it back.
            self.env['meta.field.mapping'].create({
                'form_id': source_form.id,
                'meta_key': question_key,
                'crm_field_id': field.id,
            })
        elif not mapping.active:
            # Point the switched-off row at the new field and turn it back
            # on; a second row for the same question can't be added.
            mapping.write({'crm_field_id': field.id, 'active': True})

    # ---------------------------------------------------------------- promote
    def action_promote(self):
        self.ensure_one()
        source_form, question_key = self._check_promote_access()
        tech_name = self._wanted_tech_name(question_key)
        self.tech_name = tech_name

        existing_map = self._question_mapping(source_form, question_key)
        if existing_map.active and existing_map.crm_field_id:
            # sudo for the label, same reason as in _field_reuse_state.
            return self._notify_already_promoted(
                existing_map.crm_field_id.sudo())

        reuse = self._field_reuse_state(tech_name, source_form, question_key)
        if reuse['exists']:
            # Never reuse from here, whatever reuse_confirmed says. The admin
            # first sees who already writes into the field.
            self.write({
                'state': 'confirm',
                'existing_field_label': reuse['label'],
                'field_reusable': reuse['reusable'],
                'field_on_form': reuse['on_form'],
                'resume_hint': reuse['resume'],
                'collision_note': reuse['collision_note'],
                'usage_summary': reuse['usage_summary'],
            })
            return self._reopen()

        with self.env.cr.savepoint():
            # Stored manual char field: what the mapping constraint accepts.
            model_rec = self.env['ir.model'].sudo()._get('crm.lead')
            field = self.env['ir.model.fields'].sudo().create({
                'name': tech_name,
                'field_description': self.field_label or question_key,
                'model_id': model_rec.id,
                'model': 'crm.lead',
                'ttype': 'char',
                'state': 'manual',
                'store': True,
            })
            self._link_mapping(source_form, question_key, field)
            self._dispatch_backfill(field, source_form)
        return self._lead_action()

    def action_confirm_reuse(self):
        self.ensure_one()
        source_form, question_key = self._check_promote_access()
        tech_name = self._wanted_tech_name(question_key)

        existing_map = self._question_mapping(source_form, question_key)
        if existing_map.active and existing_map.crm_field_id:
            return self._notify_already_promoted(
                existing_map.crm_field_id.sudo())

        # Worked out again rather than read from the confirm step: the field
        # or its mappings may have changed since, and the client can write
        # the wizard's own fields.
        reuse = self._field_reuse_state(tech_name, source_form, question_key)
        if not reuse['exists']:
            # Gone in the meantime, so this is a plain promotion again.
            return self.action_promote()
        if reuse['on_form']:
            raise UserError(_(
                "A field named %s already exists on leads and can't be "
                "reused here, because another question on this form already "
                "fills it. Pick another name.", tech_name))
        if not reuse['reusable']:
            raise UserError(_(
                "A field named %s already exists on leads and can't be "
                "reused, because only custom text fields can hold promoted "
                "answers. Pick another name.", tech_name))

        field = reuse['field']
        with self.env.cr.savepoint():
            self._link_mapping(source_form, question_key, field)
            self._dispatch_backfill(field, source_form)
        return self._lead_action()

    def action_back_to_name(self):
        self.ensure_one()
        self._check_admin()
        self.write({
            'state': 'name',
            'existing_field_label': False,
            'field_reusable': False,
            'field_on_form': False,
            'resume_hint': False,
            'collision_note': False,
            'usage_summary': False,
        })
        return self._reopen()

    # ------------------------------------------------------------------ view
    def _inject_view(self, tech_name):
        """Add the new field to the lead form through a small inherited view.

        The view name is derived from the field, so a second call finds it and
        does nothing. If the anchor view is missing, log and skip; the field
        still works without it.
        """
        View = self.env['ir.ui.view']
        view_name = 'crm.lead.form.meta.custom.%s' % tech_name
        anchor = self.env.ref(
            'meta_lead_ads.crm_lead_view_form_meta', raise_if_not_found=False)
        if not anchor:
            _logger.warning(
                "Promote: anchor view 'meta_lead_ads.crm_lead_view_form_meta' "
                "missing; skipping inherited-view injection for %s (field still "
                "created).", tech_name)
            return
        # Only a lead view under the Meta tab is the wizard's, the same rule
        # the upgrade uses. Another view that borrows the name is left alone.
        existing = View.sudo().search([
            ('name', '=', view_name),
            ('model', '=', 'crm.lead'),
            ('inherit_id', '=', anchor.id),
        ], limit=1)
        if existing:
            # Earlier releases made these views without an id.
            self._meta_own_injected_view(existing, tech_name)
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
        view = View.sudo().create({
            'name': view_name,
            'model': 'crm.lead',
            'inherit_id': anchor.id,
            'arch': arch,
        })
        self._meta_own_injected_view(view, tech_name)

    def _meta_own_injected_view(self, view, tech_name):
        """Make the module own a lead-form view the wizard added.

        Uninstall then removes the view with the field it shows. The id is
        noupdate so an upgrade, which never reloads this view, keeps it. A
        view that already has an id, of any module, is left alone.
        """
        view = view.sudo()
        Data = self.env['ir.model.data'].sudo()
        if Data.search_count([('model', '=', 'ir.ui.view'),
                              ('res_id', '=', view.id)], limit=1):
            return
        xml_name = 'crm_lead_form_meta_custom_' + tech_name
        if Data.search_count([('module', '=', 'meta_lead_ads'),
                              ('name', '=', xml_name)], limit=1):
            return
        Data.create({
            'module': 'meta_lead_ads',
            'name': xml_name,
            'model': 'ir.ui.view',
            'res_id': view.id,
            'noupdate': True,
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
        overwritten, so salespeople's edits survive. Only leads in the user's
        current companies, or with no company, are touched.
        """
        # Answers have no record rule and the admin may not be able to read
        # every lead that shares the key, so search as sudo. The promote step
        # has already run the admin check. active_test=False keeps lost
        # leads in scope.
        Answer = self.env['meta.lead.answer'].sudo().with_context(
            active_test=False)
        field_name = field.name
        question_key = self.question_key or self.answer_id.question_key

        domain = [
            ('question_key', '=', question_key),
            ('lead_id.meta_form_id_ref', '=', source_form.id),
            '|', ('lead_id.company_id', '=', False),
            ('lead_id.company_id', 'in', self.env.companies.ids),
        ]
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
                seen_leads.add(lead.id)
                value = ans.value
                if not value:
                    continue
                if lead[field_name]:
                    continue
                # Written as sudo too: this only copies the lead's own answer
                # into a field on the same lead, and the domain keeps it in
                # the user's companies. Writing as the user would roll back
                # the whole promotion on the first lead they can't edit.
                # The promoted field is not in the domain, so offset paging
                # still sees the same result set.
                lead_vals = {field_name: value}
                # The copied answer is Meta data, so the erase must find it.
                inventory = lead._meta_union_filled([field_name])
                if inventory is not None:
                    lead_vals['meta_filled_fields'] = inventory
                lead.write(lead_vals)
            offset += batch_size
