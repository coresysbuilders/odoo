# Meta Lead Ads Integration for Odoo — © 2026 CoreSys Builders. All rights reserved.
# Licensed under the Odoo Proprietary License v1.0 (OPL-1).
# Unauthorized copying, redistribution, or resale of this software, in whole or in
# part, via any medium, is strictly prohibited and constitutes a license violation.
# OPL-1: https://www.odoo.com/documentation/18.0/legal/licenses.html#odoo-apps

# Admin manual-trigger wizard following the meta_onboarding TransientModel
# idiom. This wizard is a thin wrapper -- it never creates a crm.lead itself;
# the one ingestion code path is meta.lead.ingest.ingest_leadgen. Admin-only:
# the ACL row is group_meta_admin only (no group_meta_user row) and the
# menuitem is admin-gated.
from odoo import fields, models, _
from odoo.exceptions import UserError


class MetaIngestLeadgen(models.TransientModel):
    _name = 'meta.ingest.leadgen'
    _description = 'Meta Ingest by Lead ID (admin manual trigger)'

    page_id = fields.Many2one('meta.page', string='Page', required=True)
    leadgen_id = fields.Char(string='Meta Lead ID', required=True)

    def action_ingest(self):
        """Thin wrapper over the single ingestion code path. Routes through
        ingest_leadgen(..., trigger='manual') -> the service does the fetch +
        dedup + create/link; we only return an act_window pointing at the
        resulting crm.lead. No URL is built and no lead is created here."""
        self.ensure_one()
        leadgen_id = (self.leadgen_id or '').strip()
        if not leadgen_id:
            raise UserError(_("Paste a Meta Lead ID."))
        lead = self.env['meta.lead.ingest'].ingest_leadgen(
            self.page_id, leadgen_id, trigger='manual')
        return {
            'type': 'ir.actions.act_window',
            'res_model': 'crm.lead',
            'res_id': lead.id,
            'view_mode': 'form',
            'target': 'current',
        }
