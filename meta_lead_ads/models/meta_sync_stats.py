# Meta Lead Ads Integration for Odoo — © 2026 CoreSys Builders. All rights reserved.
# Licensed under the Odoo Proprietary License v1.0 (OPL-1).
# Unauthorized copying, redistribution, or resale of this software, in whole or in
# part, via any medium, is strictly prohibited and constitutes a license violation.
# OPL-1: https://www.odoo.com/documentation/19.0/legal/licenses.html#odoo-apps

# Daily counts of the sync rows the retention cleanup removed, so the
# dashboard totals for a period don't drop once the cleanup has run. Counts
# only: no lead, no Meta id and no payload, so nothing here can name a
# person. The rows are written by the cleanup's own SQL, which is why the
# table has no create or write date. Several rows may share a day, company,
# trigger and status; readers add them up.
from odoo import api, fields, models


class MetaSyncStats(models.Model):
    _name = 'meta.sync.stats'
    _description = 'Sync Log Daily Totals'
    _log_access = False
    _order = 'day desc, id desc'

    day = fields.Date(string='Day', required=True, readonly=True, index=True)
    company_id = fields.Many2one('res.company', string='Company',
                                 ondelete='cascade', readonly=True,
                                 index=True)
    trigger = fields.Selection(selection='_trigger_selection',
                               string='Trigger', readonly=True)
    status = fields.Selection(selection='_status_selection',
                              string='Status', readonly=True)
    row_count = fields.Integer(string='Rows', readonly=True)

    @api.model
    def _trigger_selection(self):
        return self.env['meta.sync.log']._fields['trigger'].selection

    @api.model
    def _status_selection(self):
        return self.env['meta.sync.log']._fields['status'].selection
