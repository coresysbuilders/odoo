# CoreSys Builders — https://coresysbuilders.com
# Copyright (C) CoreSys Builders. License OPL-1.
from odoo import api, fields, models
from odoo.exceptions import ValidationError


class ApprovalLevel(models.Model):
    _name = 'coresys.approval.level'
    _description = 'Approval Level'
    _order = 'sequence, id'

    name = fields.Char(
        string='Level',
        required=True,
        help='Name of this approval step, such as "Manager" or "Finance".',
    )
    sequence = fields.Integer(
        default=10,
        help="Drag to reorder the approval steps.",
    )
    category_id = fields.Many2one(
        'coresys.approval.category',
        string='Approval Category',
        required=True,
        ondelete='cascade',
        index=True,
    )
    approver_group_id = fields.Many2one(
        'res.groups',
        string='Approver Group',
        help="Everyone in this group may approve at this step.",
    )
    approver_user_ids = fields.Many2many(
        'res.users',
        string='Specific Approvers',
        help="Named users who may approve at this step, in addition to any "
             "approver group.",
    )
    company_id = fields.Many2one(
        'res.company',
        string='Company',
        related='category_id.company_id',
        store=True,
        index=True,
    )

    @api.constrains('approver_group_id', 'approver_user_ids', 'category_id')
    def _check_approver_source(self):
        # category_id (required, always in create vals) anchors the check so it
        # also fires when a level is created with neither approver field set —
        # @api.constrains only runs when a listed field is present in the vals.
        for level in self:
            if not level.approver_group_id and not level.approver_user_ids:
                raise ValidationError(
                    'Level "%(level)s" has no approver. Assign an approver '
                    'group, one or more specific approvers, or both before '
                    'saving.' % {'level': level.name}
                )
