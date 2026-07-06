# CoreSys Builders — https://coresysbuilders.com
# Copyright (C) CoreSys Builders. License OPL-1.
import ast
import uuid

from odoo import _, api, fields, models
from odoo.exceptions import UserError

# The bypass that lets the sanctioned auto-run skip the mixin's own re-gate is a
# per-process random secret, NOT a boolean. The context KEY is public, but only a
# caller holding this exact value — generated once per worker at import and never
# persisted or returned to any client — can satisfy the equality check. A client
# supplying coresys_approval_bypass_gate=True (or any guessed value) fails it, so
# the gate cannot be forged from RPC context.
_APPROVAL_GATE_TOKEN = uuid.uuid4().hex


def _approval_bypass_context():
    # The engine's final-approval hook opens the one sanctioned re-gate bypass by
    # layering this context on the auto-run call. Kept module-level so the engine
    # imports the value rather than hardcoding a forgeable literal.
    return {'coresys_approval_bypass_gate': _APPROVAL_GATE_TOKEN}


def approval_gate_bypassed(env):
    # Equality against the in-memory token — never a truthiness check.
    return env.context.get('coresys_approval_bypass_gate') == _APPROVAL_GATE_TOKEN


class ApprovalMixin(models.AbstractModel):
    _name = 'coresys.approval.mixin'
    _description = 'Approval Gating Mixin'

    # Target models override these two. The gated method is the action the gate
    # guards (e.g. the confirm button); the watched fields are the bridge default
    # fallback used when a matched category declares none (D-34/D-37).
    _approval_gated_method = False
    _approval_watch_fields = []

    approval_state = fields.Char(
        string='Approval Status',
        compute='_compute_approval_state',
        help="State of the latest approval request about this record, if any.",
    )

    def _approval_bypass_active(self):
        # True only for the sanctioned auto-run carrying the in-memory token.
        return approval_gate_bypassed(self.env)

    def _approval_is_consumed(self):
        # False by default; a bridge overrides this to True once the gated action
        # has already run (the record reached a confirmed/consumed state), so its
        # request is never invalidated after the fact.
        return False

    def _approval_company(self):
        if 'company_id' in self._fields:
            return self.company_id
        return self.env.company

    def _compute_approval_state(self):
        # Batched: one search over every referencing request, latest-first, then
        # a first-seen map. Never a per-record search inside the loop.
        Request = self.env['coresys.approval.request'].sudo()
        records = self.filtered('id')
        refs = ['%s,%d' % (self._name, rec.id) for rec in records]
        state_by_ref = {}
        if refs:
            for req in Request.search([('reference', 'in', refs)], order='id desc'):
                target = req.reference
                if not target:
                    continue
                key = '%s,%d' % (target._name, target.id)
                state_by_ref.setdefault(key, req.state)
        for rec in self:
            ref = '%s,%d' % (self._name, rec.id) if rec.id else False
            rec.approval_state = state_by_ref.get(ref, False) if ref else False

    def _matched_approval_categories(self):
        # Active categories for this model whose (already save-validated) domain
        # matches the record. The domain is reused verbatim, not re-validated.
        self.ensure_one()
        cats = self.env['coresys.approval.category'].sudo().search([
            ('model_name', '=', self._name),
            ('active', '=', True),
            '|', ('company_id', '=', False),
                 ('company_id', 'in', self._approval_company().ids),
        ])
        return cats.filtered(
            lambda c: bool(self.filtered_domain(ast.literal_eval(c.domain or '[]'))))

    def _active_request_for(self, cats):
        # The record's live requests for the given categories. Refused/cancelled
        # are terminal and excluded, so they never block a fresh submit.
        self.ensure_one()
        return self.env['coresys.approval.request'].sudo().search([
            ('reference', '=', '%s,%d' % (self._name, self.id)),
            ('category_id', 'in', cats.ids),
            ('state', 'in', ('draft', 'to_approve', 'approved')),
        ], order='id desc')

    def _approval_blocking_categories(self):
        # The single shared gate decision, reused by a bridge write backstop:
        #   >1 match  -> hard UserError (admins keep domains mutually exclusive),
        #   0 matches -> empty (no approval required, proceed),
        #   already-approved live request -> empty (cleared, proceed),
        #   otherwise -> the one matched category (block + submit).
        self.ensure_one()
        cats = self._matched_approval_categories()
        if len(cats) > 1:
            raise UserError(_(
                "This record matches %d approval categories; resolve the "
                "ambiguity so exactly one applies.", len(cats)))
        if not cats:
            return self.env['coresys.approval.category']
        active = self._active_request_for(cats)
        if active.filtered(lambda r: r.state == 'approved'):
            return self.env['coresys.approval.category']
        return cats

    def _ensure_submitted_request(self, cats):
        # Concurrency-safe: a row-lock on the target record serializes racing
        # confirm/RPC transactions so the second reuses the first's request
        # instead of creating a duplicate. self._table is a trusted model
        # identifier (mirrors the engine's _lock_active_line), not user input.
        self.ensure_one()
        self.env.cr.execute(
            'SELECT id FROM "%s" WHERE id = %%s FOR UPDATE' % self._table,
            [self.id])
        existing = self._active_request_for(cats)
        if existing.filtered(lambda r: r.state == 'approved'):
            return
        if existing.filtered(lambda r: r.state == 'to_approve'):
            return
        draft = existing.filtered(lambda r: r.state == 'draft')
        if draft:
            # Reuse and submit the existing draft — no duplicate active request.
            draft[:1].action_submit()
            return
        request = self.env['coresys.approval.request'].create({
            'category_id': cats.id,
            'reference': '%s,%d' % (self._name, self.id),
            'requester_id': self.env.user.id,
            'company_id': self._approval_company().id,
        })
        # Let action_submit's ValidationError/UserError propagate (fail-closed,
        # D-31): the shared transaction rolls back both the request and the
        # caller's action rather than leaving a half-submitted request.
        request.action_submit()

    def _approval_guard(self):
        # Returns True to proceed, False to block. On block it ensures a submitted
        # request exists and DOES NOT raise, so the request persists and the
        # caller returns a notification instead of confirming (D-30).
        self.ensure_one()
        if self._approval_bypass_active():
            return True
        cats = self._approval_blocking_categories()
        if not cats:
            return True
        self._ensure_submitted_request(cats)
        return False

    def _approval_blocked_notification(self):
        # The persistence-safe UI signal that replaces a raise: the record is
        # confirmed automatically once approved (D-33), so it never asks the user
        # to confirm again.
        return {
            'type': 'ir.actions.client',
            'tag': 'display_notification',
            'params': {
                'title': _("Sent for approval"),
                'message': _("It will be confirmed automatically once approved."),
                'type': 'warning',
                'sticky': False,
            },
        }

    def action_submit_for_approval(self):
        # Manual smart-button entry to the same gate the gated action drives.
        self.ensure_one()
        if not self._approval_guard():
            return self._approval_blocked_notification()
        return True

    def _run_approval_gated_action(self):
        # Called by the engine auto-run under the token bypass (D-33/D-34).
        self.ensure_one()
        method = self._approval_gated_method
        if not method:
            return False
        return getattr(self, method)()

    def _reset_requests_to_draft(self, requests):
        # The shared reset primitive: drive each request back to draft through the
        # engine's sudo reset shape — never a plain non-sudo state write, which the
        # request's own guard rejects.
        for req in requests:
            req.line_ids.sudo().unlink()
            req.sudo().write({'state': 'draft'})
            req._log_response(action='reset')
            req.sudo().message_post(body=_(
                "Reset to draft: a watched field changed after submission; "
                "re-submit for re-approval."))

    def _invalidate_pending_approval(self):
        # Unconditional reset of the record's live requests — the entry a bridge
        # line hook calls when a material line changed. Covers approved-but-
        # unconfirmed requests, and skips already-consumed records.
        targets = self.filtered(lambda r: not r._approval_is_consumed())
        if not targets:
            return
        refs = ['%s,%d' % (self._name, rec.id) for rec in targets if rec.id]
        if not refs:
            return
        reqs = self.env['coresys.approval.request'].sudo().search([
            ('reference', 'in', refs),
            ('state', 'in', ('to_approve', 'approved')),
        ])
        self._reset_requests_to_draft(reqs)

    def _invalidate_watched(self, changed_keys):
        # Keyed off the record's ACTIVE requests, not off a post-write category
        # re-match: an edit that makes the record STOP matching its category still
        # invalidates. One batched request search, no search inside the loop.
        # The caller (write) passes the records that were un-consumed BEFORE the
        # write; consumed state is deliberately NOT re-checked here because a
        # same-call transition into a confirmed state would otherwise mask a
        # watched-field change made in that same write and skip the reset.
        if not self or not changed_keys:
            return
        targets = self
        refs = ['%s,%d' % (self._name, rec.id) for rec in targets if rec.id]
        if not refs:
            return
        active_reqs = self.env['coresys.approval.request'].sudo().search([
            ('reference', 'in', refs),
            ('state', 'in', ('to_approve', 'approved')),
        ])
        record_by_ref = {
            '%s,%d' % (self._name, rec.id): rec for rec in targets if rec.id}
        collected = self.env['coresys.approval.request']
        for req in active_reqs:
            target = req.reference
            if not target:
                continue
            record = record_by_ref.get('%s,%d' % (target._name, target.id))
            if not record:
                continue
            watched = set(record._approval_watch_fields or []) | {
                name.strip()
                for name in (req.category_id.approval_watch_fields or '').split(',')
                if name.strip()
            }
            if watched & changed_keys:
                collected |= req
        self._reset_requests_to_draft(collected)

    def write(self, vals):
        if self._approval_bypass_active():
            return super().write(vals)
        # Snapshot the not-yet-consumed records BEFORE the write. Odoo writes
        # scalar fields (e.g. state) to cache before o2m fields, so a single
        # write that both confirms the record and mutates a watched field would
        # read as already-consumed by the time invalidation runs, skip resetting
        # the now-stale approval, and let the gate clear it. Capturing the
        # pre-write set keeps that stale approval reset for re-approval.
        unconsumed = self.filtered(lambda r: not r._approval_is_consumed())
        res = super().write(vals)
        unconsumed._invalidate_watched(set(vals))
        return res
