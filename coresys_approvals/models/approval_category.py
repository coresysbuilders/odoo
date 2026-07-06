# CoreSys Builders — https://coresysbuilders.com
# Copyright (C) CoreSys Builders. License OPL-1.
import ast

from odoo import api, fields, models
from odoo.exceptions import ValidationError


class ApprovalCategory(models.Model):
    _name = 'coresys.approval.category'
    _description = 'Approval Category'
    _order = 'name'

    name = fields.Char(
        required=True,
        help="Name of this approval configuration as it appears to managers "
             "and in approval requests.",
    )
    model_id = fields.Many2one(
        'ir.model',
        string='Applies To',
        required=True,
        ondelete='cascade',
        help="The Odoo document type this approval applies to. Any model can "
             "be targeted, including custom ones.",
    )
    model_name = fields.Char(
        related='model_id.model',
        string='Model Name',
        store=True,
    )
    domain = fields.Char(
        string='Applicability Condition',
        default='[]',
        help="Optional filter deciding when approval is required — for example, "
             "only orders above a set amount. Leave empty to require approval "
             "for every record of this type.",
    )
    approval_watch_fields = fields.Char(
        string='Watched Fields',
        help="Comma-separated field names that reset a pending or "
             "approved-but-unconfirmed request back to draft when changed — for "
             "example, the total or the order lines. Leave empty to keep a "
             "submitted request valid regardless of later edits.",
    )
    company_id = fields.Many2one(
        'res.company',
        string='Company',
        index=True,
        help="Restrict this configuration to one company. Leave empty to share "
             "it across all companies.",
    )
    allow_self_approval = fields.Boolean(
        string='Allow Self-Approval',
        default=False,
        help="When enabled, the person who submits a request may also approve "
             "it. Off by default.",
    )
    active = fields.Boolean(default=True)
    level_ids = fields.One2many(
        'coresys.approval.level',
        'category_id',
        string='Approval Levels',
        help="The sequence of approval steps a request passes through, in "
             "order from top to bottom.",
    )

    @api.constrains('domain')
    def _check_domain(self):
        for category in self:
            raw = (category.domain or '').strip()
            if not raw:
                continue
            try:
                parsed = ast.literal_eval(raw)
            except (ValueError, SyntaxError) as exc:
                raise ValidationError(
                    "The applicability condition is not a valid domain: %s" % exc
                ) from exc
            if not isinstance(parsed, (list, tuple)):
                raise ValidationError(
                    "The applicability condition must be a domain expression, "
                    "for example [('amount_total', '>', 5000)]."
                )
            for leaf in parsed:
                if isinstance(leaf, str):
                    if leaf not in ('&', '|', '!'):
                        raise ValidationError(
                            "Unknown domain operator %r in the applicability "
                            "condition." % leaf
                        )
                elif not (isinstance(leaf, (list, tuple)) and len(leaf) == 3):
                    raise ValidationError(
                        "Each condition must be a (field, operator, value) "
                        "triplet or a logical operator."
                    )

    @api.constrains('approval_watch_fields')
    def _check_watch_fields(self):
        for category in self:
            raw = (category.approval_watch_fields or '').strip()
            if not raw:
                continue
            if not category.model_id:
                continue
            model_fields = self.env[category.model_name]._fields
            for name in (part.strip() for part in raw.split(',')):
                if not name:
                    continue
                if name not in model_fields:
                    raise ValidationError(
                        "The watched field %(field)r is not a field of "
                        "%(model)s." % {
                            'field': name,
                            'model': category.model_name,
                        }
                    )
