# CoreSys Builders — https://coresysbuilders.com
# Copyright (C) CoreSys Builders. License OPL-1.
from odoo import _, api, fields, models
from odoo.exceptions import AccessError

# The line ACL is read-only for every group (Plan 02-01); create/write/unlink
# are blocked for non-superusers by the overrides below. Every engine mutation
# of a line — the submit-time create, a decision write, a reset unlink — runs
# under a narrow, post-authorization sudo inside a request action method. The
# action methods are therefore the sole mutation path and no non-READ ACL
# surface exists to reason about.
PROTECTED_LINE_FIELDS = {'approver_user_ids', 'sequence', 'request_id', 'name'}
DECISION_FIELDS = {'status', 'decided_by', 'decided_on', 'reason'}
GUARDED_LINE_FIELDS = PROTECTED_LINE_FIELDS | DECISION_FIELDS


class ApprovalRequestLine(models.Model):
    _name = 'coresys.approval.request.line'
    _description = 'Approval Request Line'
    _order = 'sequence, id'

    request_id = fields.Many2one(
        'coresys.approval.request',
        string='Approval Request',
        required=True,
        ondelete='cascade',
        index=True,
    )
    name = fields.Char(
        string='Level',
        help="The level this snapshot row was frozen from at submission.",
    )
    sequence = fields.Integer(default=10)
    # Plain frozen M2M copied at submit — never related/computed (ENG-01).
    approver_user_ids = fields.Many2many('res.users',
        string='Eligible Approvers',
        help="The approvers resolved and frozen for this level at submission. "
             "Later changes to the category never alter this set.")
    status = fields.Selection(
        [('pending', 'Pending'), ('approved', 'Approved'), ('refused', 'Refused')],
        default='pending',
        required=True,
    )
    company_id = fields.Many2one(
        'res.company',
        string='Company',
        related='request_id.company_id',
        store=True,
        index=True,
    )
    is_active_line = fields.Boolean(
        string='Active Level',
        compute='_compute_is_active_line',
        compute_sudo=True,
        help="True only for the level currently awaiting a decision.",
    )
    decided_by = fields.Many2one(
        'res.users',
        string='Decided By',
        readonly=True,
    )
    decided_on = fields.Datetime(
        string='Decided On',
        readonly=True,
    )
    reason = fields.Text(
        string='Decision Reason',
        readonly=True,
    )

    @api.depends('status', 'sequence', 'request_id.state', 'request_id.line_ids.status')
    def _compute_is_active_line(self):
        for line in self:
            request = line.request_id
            line.is_active_line = (
                request.state == 'to_approve'
                and line == request._active_line()
            )

    @api.model_create_multi
    def create(self, vals_list):
        if not self.env.su:
            raise AccessError(_(
                "Approval snapshot lines can only be created by submitting a "
                "request."))
        return super().create(vals_list)

    def write(self, vals):
        if not self.env.su and GUARDED_LINE_FIELDS & vals.keys():
            raise AccessError(_(
                "Approval snapshot lines are engine-managed; decisions go "
                "through the request's Approve and Refuse actions."))
        return super().write(vals)

    def unlink(self):
        if not self.env.su:
            raise AccessError(_(
                "Approval snapshot lines can only be removed by resetting a "
                "request."))
        return super().unlink()
