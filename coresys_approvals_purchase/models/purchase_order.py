# CoreSys Builders — https://coresysbuilders.com
# Copyright (C) CoreSys Builders. License OPL-1.
from odoo import _, api, models
from odoo.exceptions import UserError
from odoo.addons.coresys_approvals.models.approval_mixin import approval_gate_bypassed

_APPROVAL_REQUIRED_MESSAGE = (
    "This purchase order requires approval before it can be confirmed. "
    "Use Confirm to send it for approval."
)


class PurchaseOrder(models.Model):
    _name = 'purchase.order'
    _inherit = ['purchase.order', 'coresys.approval.mixin']

    _approval_gated_method = 'button_confirm'
    _approval_watch_fields = ['amount_total', 'order_line']

    # Single source of truth for the state that means "already confirmed".
    # On Odoo 19.0 'purchase' is the only confirmed state — the 'done' state was
    # removed (locking is now a separate boolean) — so the gate, the two backstops
    # and _approval_is_consumed all read this single-state set.
    _approval_confirmed_states = frozenset({'purchase'})

    def _approval_is_consumed(self):
        self.ensure_one()
        return self.state in self._approval_confirmed_states or self.state == 'cancel'

    def button_confirm(self):
        # Gate each order BEFORE super(). _approval_guard auto-submits and returns
        # False when blocked without raising, so the request persists and the PO
        # stays draft. The batch is confirmed only when nothing was blocked.
        blocked = self.browse()
        for order in self:
            if not order._approval_guard():
                blocked |= order
        if blocked:
            return blocked[:1]._approval_blocked_notification()
        return super().button_confirm()

    def write(self, vals):
        # RPC backstop: reject a raw transition into any confirmed state unless the
        # sanctioned in-memory token is present (never a forgeable context flag) or
        # we are in sudo. Reuses the shared _approval_blocking_categories decision,
        # which itself raises the D-32 ambiguity error.
        if (vals.get('state') in self._approval_confirmed_states
                and not self.env.su
                and not approval_gate_bypassed(self.env)):
            # Capture the orders actually transitioning in (not already confirmed)
            # BEFORE super, then re-gate them AFTER it. filtered_domain / amount_total
            # recompute during super(), so a line-price bump plus state:'purchase' in
            # one RPC is judged against the fresh amount_total, not the stale pre-write
            # value; the raise rolls the whole write back. The sanctioned auto-run
            # carries the bypass token and never reaches this branch.
            candidates = self.filtered(
                lambda o: o.state not in o._approval_confirmed_states)
            res = super().write(vals)
            for order in candidates:
                if order._approval_blocking_categories():
                    raise UserError(_(_APPROVAL_REQUIRED_MESSAGE))
            return res
        return super().write(vals)

    @api.model_create_multi
    def create(self, vals_list):
        # Create-bypass backstop: catch create({'state': 'purchase'}); the raise
        # rolls the create back.
        records = super().create(vals_list)
        if not self.env.su and not approval_gate_bypassed(self.env):
            for order in records.filtered(
                    lambda o: o.state in o._approval_confirmed_states):
                if order._approval_blocking_categories():
                    raise UserError(_(_APPROVAL_REQUIRED_MESSAGE))
        return records


class PurchaseOrderLine(models.Model):
    _inherit = 'purchase.order.line'

    # Material fields whose edit changes the order's amount_total, so a pending or
    # approved-but-unconfirmed request on the parent must be invalidated.
    _APPROVAL_LINE_WATCH = {
        'product_qty', 'price_unit', 'tax_ids', 'product_id', 'discount'}

    @api.model_create_multi
    def create(self, vals_list):
        lines = super().create(vals_list)
        if not self.env.su and not approval_gate_bypassed(self.env):
            lines.order_id._invalidate_pending_approval()
        return lines

    def write(self, vals):
        res = super().write(vals)
        if (not self.env.su and not approval_gate_bypassed(self.env)
                and self._APPROVAL_LINE_WATCH & set(vals)):
            self.order_id._invalidate_pending_approval()
        return res

    def unlink(self):
        orders = self.order_id
        res = super().unlink()
        if not self.env.su and not approval_gate_bypassed(self.env):
            orders._invalidate_pending_approval()
        return res
