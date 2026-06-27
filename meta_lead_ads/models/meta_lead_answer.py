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

    # `value` holds the deterministic display string for a question — Meta's
    # field_data[].values is an array, joined by the ingest service with ", ".
    # This row is a queryable convenience view; the authoritative lossless copy
    # of the full field_data array lives in meta.sync.log.raw_payload
    # (admin-gated). Do not treat `value` as the only copy.
    lead_id = fields.Many2one('crm.lead', string='Lead', required=True,
                              ondelete='cascade', index=True)
    sequence = fields.Integer(default=10)
    question_key = fields.Char(string='Meta Question Key', required=True)
    label = fields.Char(string='Question')
    value = fields.Char(string='Answer')

    def action_promote_to_field(self):
        # Admin-discoverable entry point: the answer-row button opens the
        # meta.promote.answer wizard SEEDED from this row so it derives the
        # field name from question_key (canonical, never the label).
        # Admin-only is enforced by the button's groups= (UI hiding), the
        # admin-only ACL row on meta.promote.answer (privilege boundary), and the
        # in-method has_group gate inside action_promote (defense-in-depth).
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
