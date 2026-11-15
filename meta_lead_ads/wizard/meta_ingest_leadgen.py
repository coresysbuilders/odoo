# Meta Lead Ads Integration for Odoo — © 2026 CoreSys Builders. All rights reserved.
# Licensed under the Odoo Proprietary License v1.0 (OPL-1).
# Unauthorized copying, redistribution, or resale of this software, in whole or in
# part, via any medium, is strictly prohibited and constitutes a license violation.
# OPL-1: https://www.odoo.com/documentation/18.0/legal/licenses.html#odoo-apps

# Admin wizard to pull a single lead by its Meta ID. It hands off to
# ingest_leadgen like the webhook and cron do; access is admin-only.
from odoo import fields, models, _
from odoo.exceptions import UserError

from ..models.exceptions import MetaIngestRaceError, MetaLeadRemovedError


class MetaIngestLeadgen(models.TransientModel):
    _name = 'meta.ingest.leadgen'
    _description = 'Meta Ingest by Lead ID (admin manual trigger)'

    page_id = fields.Many2one('meta.page', string='Page', required=True)
    leadgen_id = fields.Char(string='Meta Lead ID', required=True)

    def action_ingest(self):
        """Ingest the lead through ingest_leadgen and open the resulting crm.lead.

        If the caller can't read the lead (no CRM rights, or it sits in a
        company they haven't selected) a plain notification is shown instead,
        without the lead's name."""
        self.ensure_one()
        leadgen_id = (self.leadgen_id or '').strip()
        if not leadgen_id:
            raise UserError(_("Paste a Meta Lead ID."))
        # A lead deleted in Odoo only comes back through Restore, whatever
        # this form was opened with. The record is found as sudo but read
        # back as the caller, so only admins who can see it learn who
        # deleted it.
        tomb = self.env['meta.leadgen.tombstone'].sudo().search(
            [('leadgen_id', '=', leadgen_id)], limit=1)
        if tomb:
            raise UserError(tomb.with_env(self.env)._refusal_message())
        try:
            lead = self.env['meta.lead.ingest'].ingest_leadgen(
                self.page_id, leadgen_id, trigger='manual')
        except MetaIngestRaceError:
            # Another import of the same lead hasn't committed yet.
            raise UserError(_(
                "This lead is being imported right now. Try again in a "
                "moment.")) from None
        except MetaLeadRemovedError:
            # Deleted after the check above.
            tomb = self.env['meta.leadgen.tombstone'].sudo().search(
                [('leadgen_id', '=', leadgen_id)], limit=1)
            message = (tomb.with_env(self.env)._refusal_message() if tomb
                       else _("This lead was deleted in Odoo and can't be "
                              "imported again."))
            raise UserError(message) from None
        return self.env['meta.lead.ingest']._lead_result_action(lead)
