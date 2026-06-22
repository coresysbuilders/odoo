# Meta Lead Ads Integration for Odoo — © 2026 CoreSys Builders. All rights reserved.
# Licensed under the Odoo Proprietary License v1.0 (OPL-1).
# Unauthorized copying, redistribution, or resale of this software, in whole or in
# part, via any medium, is strictly prohibited and constitutes a license violation.
# OPL-1: https://www.odoo.com/documentation/18.0/legal/licenses.html#odoo-apps

from odoo import api, fields, models


class MetaPage(models.Model):
    _name = 'meta.page'
    _description = 'Meta Page'

    name = fields.Char(required=True)
    page_id = fields.Char(string='Meta Page ID', required=True, index=True)
    active = fields.Boolean(default=True)
    # Per-Page access token. Secret -> field-level groups=: the key is stripped
    # from a non-admin ORM read and from the view. Never mirrored into a
    # computed/related field and never sudo()-read into a non-admin context.
    access_token = fields.Char(string='Page Access Token',
                               groups='meta_lead_ads.group_meta_admin')
    account_id = fields.Many2one('meta.account', string='Account',
                                 required=True, ondelete='cascade')
    form_ids = fields.One2many('meta.lead.form', 'page_id', string='Lead Forms')
    form_count = fields.Integer(compute='_compute_form_count', store=True)

    _sql_constraints = [
        ('page_id_uniq', 'unique(page_id)',
         'A Meta Page with this ID already exists.'),
    ]

    @api.depends('form_ids')
    def _compute_form_count(self):
        for rec in self:
            rec.form_count = len(rec.form_ids)

    def action_open_forms(self):
        # The child list's "New" button is auto-hidden for users without create
        # ACL. Meta Users have perm_create=0 on meta.lead.form, so they cannot
        # create here; Meta Admins (full CRUD) can still create via drill-down.
        # No context={'create': False} override -- that would wrongly block
        # legitimate admin creation.
        self.ensure_one()
        return {
            'type': 'ir.actions.act_window',
            'name': 'Lead Forms',
            'res_model': 'meta.lead.form',
            'view_mode': 'list,form',
            'domain': [('page_id', '=', self.id)],
            'context': {'default_page_id': self.id},
        }
