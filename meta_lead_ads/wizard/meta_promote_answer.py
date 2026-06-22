# Meta Lead Ads Integration for Odoo — © 2026 CoreSys Builders. All rights reserved.
# Licensed under the Odoo Proprietary License v1.0 (OPL-1).
# Unauthorized copying, redistribution, or resale of this software, in whole or in
# part, via any medium, is strictly prohibited and constitutes a license violation.
# OPL-1: https://www.odoo.com/documentation/18.0/legal/licenses.html#odoo-apps

# Admin manual-trigger wizard following the same transient-model pattern as the
# onboarding wizard. This wizard is the only place that deliberately creates an
# ir.model.fields record -- it never runs from the ingest path. Admin-only: the
# ACL grants access to group_meta_admin only (no group_meta_user row) and the
# entry button on the answer line is admin-gated. The whole promote runs inside
# an explicit cr.savepoint() so a partial failure leaves no dangling field /
# view / mapping behind.
import hashlib
import logging
import re
from xml.sax.saxutils import quoteattr

from odoo import api, fields, models, _
from odoo.exceptions import UserError, AccessError

_logger = logging.getLogger(__name__)

# Postgres identifier limit is 63 BYTES (not chars). Reserve room for the
# 'x_meta_' prefix (7 bytes) and the '_<8 hex>' hash suffix (9 bytes) when the
# core must be trimmed.
_MAX_IDENT_BYTES = 63
_PREFIX = 'x_meta_'


class MetaPromoteAnswer(models.TransientModel):
    _name = 'meta.promote.answer'
    _description = 'Promote Meta Answer to crm.lead Field (admin)'

    # Seeded from the answer row via default_* on the answer-line button action.
    # The wizard derives question_key / field_label / tech_name from the answer
    # so a contract change touches one place and the values stay consistent
    # however the wizard is created.
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
                # Key off question_key (canonical), label only for the human
                # field description (multi-language safety).
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
        """Derive a Postgres-safe ``x_meta_<sanitized>[_<hash>]`` technical name
        from an attacker-influenceable Meta question_key.

        Hardening (stdlib re + hashlib only):
          - sanitize: lowercase, non-[a-z0-9_] -> '_', collapse repeats, trim;
          - never produce a bare ``x_meta_`` (empty/punctuation/emoji/non-ASCII
            cores fall back to ``x_meta_<sha1[:8]>``);
          - never double-prefix (a key already starting with ``x_meta_`` has the
            literal stripped before re-prefixing);
          - the hash suffix is derived from the FULL ORIGINAL question_key, so
            two distinct long keys sharing a 55-char prefix derive DISTINCT
            names (truncation-collision case);
          - the cap is 63 BYTES (not chars) -- the core segment is trimmed
            encode-aware so a multi-byte UTF-8 char never overflows the limit.
        """
        raw = question_key or ''
        # Deterministic short hash over the ORIGINAL key (collision/empty/long).
        h = hashlib.sha1(raw.encode('utf-8')).hexdigest()[:8]

        lowered = raw.strip().lower()
        # Strip a leading literal 'x_meta_' BEFORE re-prefixing (no double).
        if lowered.startswith(_PREFIX):
            lowered = lowered[len(_PREFIX):]
        core = re.sub(r'[^a-z0-9_]+', '_', lowered)
        core = re.sub(r'_+', '_', core).strip('_')

        if not core:
            # Empty / punctuation-only / emoji-only / non-ASCII -> never bare.
            return _PREFIX + h

        suffix = '_' + h
        # Always append the hash; trim the CORE (not prefix, not hash) until the
        # full name fits the 63-BYTE Postgres identifier cap.
        candidate = _PREFIX + core + suffix
        if len(candidate.encode('utf-8')) <= _MAX_IDENT_BYTES:
            # Short keys keep a clean readable name without the hash so the
            # common case (e.g. 'budget' -> 'x_meta_budget') is predictable.
            plain = _PREFIX + core
            if len(plain.encode('utf-8')) <= _MAX_IDENT_BYTES:
                return plain
            return candidate

        # Over-cap: trim the core encode-aware, keeping prefix + hash intact.
        budget = _MAX_IDENT_BYTES - len(_PREFIX.encode('utf-8')) \
            - len(suffix.encode('utf-8'))
        trimmed = core.encode('utf-8')[:budget].decode('utf-8', 'ignore')
        trimmed = trimmed.strip('_') or h  # never end on a bare prefix
        return _PREFIX + trimmed + suffix

    # ----------------------------------------------------------------- notify
    def _notify_already_promoted(self, field):
        """No-op surface (display_notification, never a traceback) when the
        (source_form, question_key) pair is already promoted."""
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
        # In-method admin gate -- defense-in-depth beyond the ACL row, as the
        # first statement. A non-admin reaching this server method via RPC is
        # rejected with AccessError (not UserError) so the access boundary is
        # enforced regardless of how the call arrives.
        if not self.env.user.has_group('meta_lead_ads.group_meta_admin'):
            raise AccessError(
                _("Only Meta administrators may promote answers to fields."))

        # Resolve the source form off the answer's lead (key on question_key
        # only, never label -- multi-language safety).
        answer = self.answer_id
        source_form = answer.lead_id.meta_form_id_ref
        question_key = self.question_key or answer.question_key

        # Null guard, strict, before any create. meta_form_id_ref is a nullable
        # Many2one(ondelete='set null') -> False when the lead arrived before its
        # form was discovered or the form was later deleted.
        # meta.field.mapping.form_id is required=True (NOT NULL). Early-exit with
        # no side effects (no field / view / mapping / pending marker).
        if not source_form:
            raise UserError(_(
                "Cannot promote: this lead has no associated Meta form. The "
                "form may have been deleted or was never discovered. Re-link "
                "the lead to a form first."))

        # Derive and store the technical name.
        tech_name = self._derive_tech_name(question_key)
        self.tech_name = tech_name

        # Explicit savepoint around the whole create sequence below: any late
        # failure rolls back all artifacts atomically. No cr.commit() here.
        with self.env.cr.savepoint():
            Mapping = self.env['meta.field.mapping']
            # ir.model.fields / ir.model are system-restricted models (read is
            # gated to Settings/Access-Rights). The authorization boundary for
            # promotion is the has_group gate above + the admin-only ACL, NOT
            # the operator's Settings rights — so introspect schema via sudo()
            # (mirrors the field-create sudo below). Without this, a Meta Admin
            # who is not also an Odoo system admin hits AccessError on the
            # collision-guard search (caught by UAT, missed by superuser tests).
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

            # Idempotency + recovery.
            if existing_map and existing_map.crm_field_id:
                # Already fully promoted -> no-op notification.
                return self._notify_already_promoted(existing_map.crm_field_id)

            if existing_field and not existing_map and reusable:
                # RECOVERY: field exists but mapping missing (prior promote
                # failed after the field create). Converge -- reuse the field
                # and create the missing mapping + view + backfill. NOT a
                # spurious collision, NOT a silent no-op.
                field = existing_field
            else:
                # Collision guard (strict reuse). The name exists (either a real
                # field or an ir.model.fields row) and we are not in the recovery
                # case: block unless the admin explicitly confirmed reuse and the
                # existing field is reuse-eligible (manual char/text stored).
                # Never clobber, never auto-reuse.
                name_exists = (tech_name in self.env['crm.lead']._fields) \
                    or bool(existing_field)
                if name_exists:
                    if not (self.reuse_confirmed and reusable):
                        raise UserError(_(
                            "A field named %s already exists on Leads. Choose a "
                            "different name, or confirm reuse of the existing "
                            "custom field.") % tech_name)
                    # Confirmed, reuse-eligible.
                    field = existing_field
                else:
                    # Field create -- narrow sudo(); ttype='char',
                    # state='manual', store=True so the mapping @api.constrains
                    # is satisfied.
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

            # Defensive runtime view injection (search-before-create).
            self._inject_view(tech_name)

            # Mapping row (source form only) -- only when missing. A concurrent
            # duplicate fails the unique(form_id, meta_key) DB race backstop and
            # the savepoint rolls back cleanly.
            if not existing_map:
                Mapping.create({
                    'form_id': source_form.id,
                    'meta_key': question_key,
                    'crm_field_id': field.id,
                })

            # Backfill existing leads inline, in the same transaction.
            self._dispatch_backfill(field, source_form)

        # Return to the source lead (or the answer's lead).
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
        """Defensive search-before-create injection of a per-field inherited
        view. The deterministic name is the dedup key. If the static anchor view
        is somehow absent at runtime, log a clean warning and skip the inject
        (the field still exists, so this degrades gracefully) rather than
        crashing the savepoint."""
        View = self.env['ir.ui.view']
        view_name = 'crm.lead.form.meta.custom.%s' % tech_name
        if View.sudo().search([('name', '=', view_name)], limit=1):
            return  # already injected -- no duplicate
        anchor = self.env.ref(
            'meta_lead_ads.crm_lead_view_form_meta', raise_if_not_found=False)
        if not anchor:
            _logger.warning(
                "Promote: anchor view 'meta_lead_ads.crm_lead_view_form_meta' "
                "missing; skipping inherited-view injection for %s (field still "
                "created).", tech_name)
            return
        # tech_name is already constrained to [a-z0-9_] by _derive_tech_name, but
        # quote it as an XML attribute here too so the arch stays well-formed
        # independently of the sanitizer (quoteattr returns the surrounding
        # quotes, so the placeholder is bare).
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
        """Route the backfill inline: a state='manual' char field is writable in
        the same transaction immediately after ir.model.fields.create once the
        registry has been rebuilt. There is no deferred/persistent
        pending-backfill mechanism -- the backfill runs here and now."""
        # Make the freshly-created column visible to the ORM in this transaction:
        # invalidate -> rebuild -> invalidate, then fail loud if the column did
        # not materialize rather than silently dropping writes.
        self.env['ir.model.fields'].invalidate_model()
        self.env.registry.setup_models(self.env.cr)
        self.env['crm.lead'].invalidate_model()
        assert field.name in self.env['crm.lead']._fields, (
            "promoted field %s not registered after setup_models" % field.name)
        self._run_backfill(field, source_form)

    def _run_backfill(self, field, source_form, batch_size=500):
        """Single shared implementation of the backfill filter + conflict rules.
        Reads meta.lead.answer.value directly (the deterministic display string
        the ingest wrote) -- never re-joins raw payload.

        Rules:
          - SCOPE FILTER: answers whose question_key == self.question_key AND
            whose lead belongs to source_form (lead.meta_form_id_ref ==
            source_form). A lead on a DIFFERENT form with the same key is NOT
            backfilled (per-form scope).
          - MULTI-ANSWER: the model _order ('lead_id, sequence, id') makes the
            FIRST answer per (lead, key) deterministic; we keep the first seen.
          - EMPTY-VALUE: a falsy answer.value is SKIPPED (no empty write).
          - ENRICH-BLANK-ONLY: write only when the lead's target field is
            currently falsy -- never clobber a salesperson's edit.
          - OVER-LENGTH: written as-is (char has no enforced max here).
        """
        Answer = self.env['meta.lead.answer']
        field_name = field.name
        question_key = self.question_key or self.answer_id.question_key

        domain = [('question_key', '=', question_key)]
        offset = 0
        seen_leads = set()
        while True:
            # _order = 'lead_id, sequence, id' -> first row per lead is the
            # deterministic winner; the seen_leads set enforces single write.
            answers = Answer.search(domain, offset=offset, limit=batch_size,
                                    order='lead_id, sequence, id')
            if not answers:
                break
            for ans in answers:
                lead = ans.lead_id
                if lead.id in seen_leads:
                    continue  # already handled the first answer for this lead
                # SCOPE FILTER: lead must belong to the source form.
                if lead.meta_form_id_ref != source_form:
                    continue
                seen_leads.add(lead.id)
                value = ans.value
                if not value:
                    continue  # EMPTY-VALUE rule
                if lead[field_name]:
                    continue  # ENRICH-BLANK-ONLY rule
                lead.write({field_name: value})
            offset += batch_size
