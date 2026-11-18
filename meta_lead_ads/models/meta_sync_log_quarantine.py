# Meta Lead Ads Integration for Odoo — © 2026 CoreSys Builders. All rights reserved.
# Licensed under the Odoo Proprietary License v1.0 (OPL-1).
# Unauthorized copying, redistribution, or resale of this software, in whole or in
# part, via any medium, is strictly prohibited and constitutes a license violation.
# OPL-1: https://www.odoo.com/documentation/20.0/legal/licenses.html#odoo-apps

# Sync rows that lost both their lead and their Page but still hold a Meta
# payload belong to no company, and the sync-log record rule hides them from
# everyone. A Meta Admin still has to be able to erase them on request, so
# meta.sync.log.action_open_quarantined_rows copies the few harmless columns
# of those rows into this snapshot, and the erase below re-checks every row
# through sudo before it touches anything.
import logging

from odoo import _, api, fields, models
from odoo.exceptions import UserError

from . import meta_sync_log

_logger = logging.getLogger(__name__)


class MetaSyncLogQuarantine(models.TransientModel):
    _name = 'meta.sync.log.quarantine'
    _description = 'Quarantined Meta sync row'
    _order = 'log_ref desc'

    # Ids, status and date only. No payload, error text, Page, form or lead:
    # the rows are hidden because they hold personal data, and anything more
    # here would show it. log_ref is a plain integer rather than a link, so
    # showing the snapshot never reads the hidden row as the user.
    log_ref = fields.Integer(string='Sync Row', readonly=True, index=True)
    leadgen_id = fields.Char(string='Meta Lead ID', readonly=True)
    status = fields.Selection(selection='_status_selection', readonly=True)
    logged_at = fields.Datetime(string='Logged On', readonly=True)

    @api.model
    def _status_selection(self):
        return self.env['meta.sync.log']._fields['status'].selection

    def action_erase_personal_data(self):
        """Erase the Meta data of the quarantined rows behind these
        snapshot rows."""
        Log = self.env['meta.sync.log']
        Log._check_erase_access()
        mine = self.filtered(lambda r: r.create_uid.id == self.env.uid)
        # Any Meta Admin can create snapshot rows by hand, so a log_ref may
        # point at another company's row, or at a row that has gained a
        # company or a lead since the snapshot. A row whose payload expired
        # since then has nothing left to erase, and a tombstone written for
        # it would block that lead for good. Such refs are skipped.
        rows = Log.sudo().search([
            ('id', 'in', mine.mapped('log_ref')),
            ('company_id', '=', False),
            ('lead_id', '=', False),
            ('meta_leadgen_id', '!=', False),
            ('raw_payload', '!=', False),
            ('payload_cleared', '!=', 'erased'),
        ])
        if not rows:
            raise UserError(_(
                "These rows are no longer quarantined. Reopen the quarantined "
                "rows list."))
        limit = meta_sync_log._ERASE_ROW_LIMIT
        if len(rows) > limit:
            raise UserError(_("Select at most %(limit)s rows at a time.",
                              limit=limit))
        leadgen_ids = sorted(set(rows.mapped('meta_leadgen_id')))
        # The shared helper refuses a lead that still holds any of these
        # ids, and words the refusal for this user.
        Log.sudo()._erase_leadgen_copies(
            leadgen_ids, 'manual', details=rows._erase_details())
        mine.filtered(lambda r: r.log_ref in rows.ids).unlink()
        _logger.info("Meta quarantine erase: %s sync row(s) %s by uid=%s",
                     len(rows), rows.ids, self.env.uid)
        return {
            'type': 'ir.actions.client',
            'tag': 'display_notification',
            'params': {
                'message': _("Personal data erased from %(n)s quarantined "
                             "row(s).", n=len(rows)),
                'type': 'success',
                'sticky': False,
                'next': {'type': 'ir.actions.client', 'tag': 'soft_reload'},
            },
        }
