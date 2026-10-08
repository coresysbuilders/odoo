# Meta Lead Ads Integration for Odoo — © 2026 CoreSys Builders. All rights reserved.
# Licensed under the Odoo Proprietary License v1.0 (OPL-1).
# Unauthorized copying, redistribution, or resale of this software, in whole or in
# part, via any medium, is strictly prohibited and constitutes a license violation.
# OPL-1: https://www.odoo.com/documentation/19.0/legal/licenses.html#odoo-apps

from odoo import fields, models


class MetaLeadAnswer(models.Model):
    _name = 'meta.lead.answer'
    _description = 'Meta Lead Form Answer (unmapped question capture)'
    _order = 'lead_id, sequence, id'

    # `value` is Meta's values array joined with ", " for display. The full
    # original array is kept in meta.sync.log.raw_payload (admin only).
    lead_id = fields.Many2one('crm.lead', string='Lead', required=True,
                              ondelete='cascade', index=True)
    sequence = fields.Integer(default=10)
    question_key = fields.Char(string='Meta Question Key', required=True)
    label = fields.Char(string='Question')
    value = fields.Char(string='Answer')

    def action_promote_to_field(self):
        # Open the promote wizard prefilled from this answer. The field name is
        # derived from question_key, not the label. Admin-only: the button has
        # groups=, the wizard's ACL is admin-only, and action_promote checks
        # the group again.
        self.ensure_one()
        return {
            'type': 'ir.actions.act_window',
            'name': 'Promote to Field',
            'res_model': 'meta.promote.answer',
            'view_mode': 'form',
            'target': 'new',
            'context': {
                'default_answer_id': self.id,
                'default_question_key': self.question_key,
                'default_field_label': self.label,
            },
        }
