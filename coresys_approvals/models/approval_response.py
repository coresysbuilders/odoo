# CoreSys Builders — https://coresysbuilders.com
# Copyright (C) CoreSys Builders. License OPL-1.
from odoo import _, fields, models
from odoo.addons.base.models.ir_model import MODULE_UNINSTALL_FLAG
from odoo.exceptions import AccessError

# MODULE_UNINSTALL_FLAG ('_force_unlink') is defined in
# odoo.addons.base.models.ir_model — verified against the running Odoo 18 target
# source (Odoo core imports it from the same path in account/analytic). It is a
# shared-code port-isolation point: re-verify the import path and value against
# the Odoo 19 source at the Phase-5 branch port before shipping the 19.0 branch.


class ApprovalResponse(models.Model):
    _name = 'coresys.approval.response'
    _description = 'Approval Decision Record'
    _order = 'id'

    request_id = fields.Many2one(
        'coresys.approval.request',
        string='Approval Request',
        required=True,
        # An immutable audit record must not be destroyable by deleting its
        # mutable parent: 'restrict' blocks unlinking a request that still holds
        # decision history, so a Manager cannot cascade-delete the trail.
        ondelete='restrict',
        index=True,
    )
    line_id = fields.Many2one(
        'coresys.approval.request.line',
        string='Approval Level',
        ondelete='set null',
    )
    user_id = fields.Many2one(
        'res.users',
        string='Decided By',
        required=True,
    )
    delegated_for = fields.Many2one('res.users',
        string='On Behalf Of',
        help="The original approver whose authority the actor exercised via "
             "delegation.",
    )
    decided_on = fields.Datetime(
        string='Decided On',
        required=True,
        default=fields.Datetime.now,
    )
    level_name = fields.Char(
        string='Level',
        help="The level name frozen at the moment of the decision.",
    )
    action = fields.Selection(
        [
            ('submit', 'Submitted'),
            ('approve', 'Approved'),
            ('refuse', 'Refused'),
            ('reset', 'Reset'),
            ('cancel', 'Cancelled'),
        ],
        required=True,
    )
    reason = fields.Text(string='Reason')
    company_id = fields.Many2one(
        'res.company',
        string='Company',
        index=True,
    )

    def write(self, vals):
        # Reviewed 02-REVIEWS: absolute immutability retained by decision (Gemini's
        # gate-on-su suggestion deliberately NOT applied) — any Phase-5 upgrade
        # script needing these rows runs under the module-migration path, not user write().
        # The override — not the ACL — is the true lock: it raises even for
        # sudo/admin, so no Approval Manager can rewrite history.
        raise AccessError(_(
            "Approval decision records are permanent and cannot be modified."))

    def unlink(self):
        # Raise for every ordinary delete (sudo/admin included) but allow the
        # module-uninstall path so the module stays cleanly uninstallable.
        if not self.env.context.get(MODULE_UNINSTALL_FLAG):
            raise AccessError(_(
                "Approval decision records are permanent and cannot be "
                "deleted."))
        return super().unlink()
