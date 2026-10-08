# Meta Lead Ads Integration for Odoo — © 2026 CoreSys Builders. All rights reserved.
# Licensed under the Odoo Proprietary License v1.0 (OPL-1).
# Unauthorized copying, redistribution, or resale of this software, in whole or in
# part, via any medium, is strictly prohibited and constitutes a license violation.
# OPL-1: https://www.odoo.com/documentation/19.0/legal/licenses.html#odoo-apps

# Meta Lead Ads settings: lead title template and new-lead notifications.
#
# The title template is one global ir.config_parameter value. It is not bound
# with config_parameter= because set_values has to validate it before saving.
# Both methods that write under sudo (set_values and the retro rename) check
# group_meta_admin themselves; the view groups= only hides the UI and does not
# stop a direct RPC call. The connection status reads access_status only,
# never the app secret or tokens.
import logging

from odoo import _, api, fields, models
from odoo.exceptions import AccessError

from .const import (
    DEFAULT_LEAD_NAME, LEAD_NAME_PARAM,
    NOTIFY_ENABLED_PARAM, NOTIFY_TARGET_PARAM,
    NOTIFY_USER_PARAM, NOTIFY_GROUP_PARAM,
)

_logger = logging.getLogger(__name__)

# Retro rename works in batches so a big lead table never sits in the ORM
# cache all at once.
_RETRO_RENAME_BATCH = 500

# Sample values for the live preview in the settings form. Never saved.
_SAMPLE = {
    'form_name': 'Contact Us',
    'campaign_name': 'Spring Sale',
    'adset_name': 'Lookalike 1%',
    'ad_name': 'Carousel A',
    'contact_name': 'Jane Doe',
    'page_name': 'Acme Page',
    'date': '2026-06-18',
}

# Ranking used to show the best status across accounts (higher is better).
_STATUS_RANK = {
    'lead_retrieval_granted': 3,
    'dev_test_only': 2,
    'auth_failed': 1,
}

class ResConfigSettings(models.TransientModel):
    _inherit = 'res.config.settings'

    # Read and written in get_values/set_values so it can be validated first.
    lead_name_template = fields.Char(
        string="Lead title template", default=DEFAULT_LEAD_NAME)
    lead_name_preview = fields.Char(
        string="Preview", compute='_compute_lead_name_preview')
    meta_connection_status = fields.Selection(
        [('auth_failed', 'Authentication failed'),
         ('dev_test_only', 'Development mode — test leads only'),
         ('lead_retrieval_granted',
          'leads_retrieval granted (App Review still gates production)'),
         ('untested', 'Active account — not yet token-tested'),
         ('none', 'No active Meta account')],
        string="Connection status",
        compute='_compute_meta_connection_status')

    # Notification settings, stored the same way as the template so a Meta
    # admin without full Settings rights can still change them.
    meta_notify_enabled = fields.Boolean(
        string="Notify on new lead",
        help="Send an Odoo notification when a new lead arrives from Meta.")
    meta_notify_target_type = fields.Selection(
        [('user', 'Specific user'), ('group', 'Role / group')],
        string="Notify", default='user')
    meta_notify_user_id = fields.Many2one(
        'res.users', string="User to notify")
    meta_notify_group_id = fields.Many2one(
        'res.groups', string="Role to notify")

    # ------------------------------------------------------------------ #
    # ir.config_parameter read/write
    # ------------------------------------------------------------------ #
    def get_values(self):
        res = super().get_values()
        ICP = self.env['ir.config_parameter'].sudo()
        res['lead_name_template'] = ICP.get_param(
            LEAD_NAME_PARAM, DEFAULT_LEAD_NAME)
        res['meta_notify_enabled'] = ICP.get_param(NOTIFY_ENABLED_PARAM) == '1'
        res['meta_notify_target_type'] = ICP.get_param(
            NOTIFY_TARGET_PARAM, 'user')
        notify_uid = ICP.get_param(NOTIFY_USER_PARAM)
        res['meta_notify_user_id'] = int(notify_uid) if notify_uid else False
        notify_gid = ICP.get_param(NOTIFY_GROUP_PARAM)
        res['meta_notify_group_id'] = int(notify_gid) if notify_gid else False
        return res

    def set_values(self):
        # We write global params under sudo, and the view groups= does not
        # stop a direct RPC call, so check the group here.
        if not self.env.user.has_group('meta_lead_ads.group_meta_admin'):
            raise AccessError(_(
                "Only Meta administrators can change Meta Lead Ads settings."))
        # An empty template falls back to the default; validate and save the
        # same value.
        template = self.lead_name_template or DEFAULT_LEAD_NAME
        # Reject unknown or malformed tokens before anything is saved.
        self.env['meta.lead.ingest']._validate_lead_name_template(template)
        ICP = self.env['ir.config_parameter'].sudo()
        ICP.set_param(LEAD_NAME_PARAM, template)
        # Many2one values are stored as the id string; empty clears the key.
        ICP.set_param(NOTIFY_ENABLED_PARAM, '1' if self.meta_notify_enabled else '0')
        ICP.set_param(NOTIFY_TARGET_PARAM, self.meta_notify_target_type or 'user')
        ICP.set_param(
            NOTIFY_USER_PARAM,
            str(self.meta_notify_user_id.id) if self.meta_notify_user_id else '')
        ICP.set_param(
            NOTIFY_GROUP_PARAM,
            str(self.meta_notify_group_id.id) if self.meta_notify_group_id else '')
        # super().set_values() saves every other module's settings too. Meta
        # admins have access to this model, so without this check a crafted
        # RPC call could change unrelated settings. Only Settings admins get
        # the super() call; a Meta admin saves the Meta params above and stops.
        if self.env.user.has_group('base.group_system'):
            return super().set_values()
        return False

    # ------------------------------------------------------------------ #
    # Live preview. Errors are swallowed so the form keeps working while
    # the admin types; set_values does the real validation.
    # ------------------------------------------------------------------ #
    @api.depends('lead_name_template')
    def _compute_lead_name_preview(self):
        Ingest = self.env['meta.lead.ingest']
        for rec in self:
            try:
                rec.lead_name_preview = Ingest.render_lead_name(
                    rec.lead_name_template, _SAMPLE)
            except Exception as exc:
                # render_lead_name shouldn't raise, but the preview must never
                # break the form. Log it in case it does.
                _logger.debug("lead-name preview render failed: %s", exc)
                rec.lead_name_preview = _("(invalid template)")

    # ------------------------------------------------------------------ #
    # Connection status. The sudo read touches access_status only, never
    # the app secret or tokens.
    # ------------------------------------------------------------------ #
    @api.depends_context('uid')
    def _compute_meta_connection_status(self):
        # Same value for every record, so compute it once.
        accounts = self.env['meta.account'].sudo().search(
            [('active', '=', True)], order='id asc')
        # access_status stays empty until the token is tested; show that as
        # 'untested' rather than 'No active account'.
        if not accounts:
            best = 'none'
        else:
            tested = [s for s in accounts.mapped('access_status') if s]
            best = (max(tested, key=lambda s: _STATUS_RANK.get(s, 0))
                    if tested else 'untested')
        for rec in self:
            rec.meta_connection_status = best

    # ------------------------------------------------------------------ #
    # Retroactive rename
    # ------------------------------------------------------------------ #
    def action_retro_rename_meta_leads(self):
        """Re-apply the current title template to existing Meta leads.

        Only Meta leads whose title is still the generated one
        (meta_name_is_generated) are touched, so titles a user edited by hand
        are kept. If the template uses {date}, leads without a stored
        meta_submitted_at are skipped: create_date is only an approximation
        and is not good enough for renaming. Requires group_meta_admin, even
        for Settings admins, because names are written under sudo. Only
        writes name; never creates leads.
        """
        self.ensure_one()
        if not self.env.user.has_group('meta_lead_ads.group_meta_admin'):
            raise AccessError(_(
                "Only Meta administrators can re-apply the lead title template."))

        template = self.lead_name_template or DEFAULT_LEAD_NAME
        # Reject a broken template before any bulk write.
        self.env['meta.lead.ingest']._validate_lead_name_template(template)

        Ingest = self.env['meta.lead.ingest']
        Lead = self.env['crm.lead'].sudo()
        domain = [('meta_leadgen_id', '!=', False),
                  ('meta_name_is_generated', '=', True)]
        # With {date}, only rename leads that have the real Meta submission time.
        uses_date = '{date}' in template
        skipped_date = 0
        if uses_date:
            skipped_date = Lead.search_count(
                domain + [('meta_submitted_at', '=', False)])
            domain = domain + [('meta_submitted_at', '!=', False)]
        # Renamed leads keep meta_name_is_generated=True and still match the
        # domain, so offset paging doesn't skip any rows.
        renamed = 0
        offset = 0
        while True:
            leads = Lead.search(domain, limit=_RETRO_RENAME_BATCH,
                                offset=offset, order='id')
            if not leads:
                break
            for lead in leads:
                token_map = lead._meta_token_map()
                generated_now = Ingest.render_lead_name(template, token_map)
                if generated_now == lead.name:
                    continue
                # Pass the flag explicitly, otherwise the crm.lead write
                # override treats this as a manual rename and clears it.
                lead.write({'name': generated_now,
                            'meta_name_is_generated': True})
                renamed += 1
            offset += _RETRO_RENAME_BATCH
            # Flush, then drop the batch from the cache to keep memory flat.
            leads.flush_recordset()
            leads.invalidate_recordset()
        # Report hand-edited leads too, so "0 re-titled" has an explanation.
        skipped_manual = Lead.search_count(
            [('meta_leadgen_id', '!=', False),
             ('meta_name_is_generated', '=', False)])
        return self._retro_notification(renamed, skipped_manual=skipped_manual,
                                        skipped_date=skipped_date)

    def _retro_notification(self, renamed, skipped_manual=0, skipped_date=0):
        message = _("%s lead(s) re-titled.") % renamed
        if skipped_manual:
            message += _(
                " %s manually-edited lead(s) left unchanged.") % skipped_manual
        if skipped_date:
            message += _(
                " %s lead(s) without a stored Meta submission date left "
                "unchanged ({date} template).") % skipped_date
        return {
            'type': 'ir.actions.client',
            'tag': 'display_notification',
            'params': {
                'title': _("Retroactive rename"),
                'message': message,
                'type': 'success',
                'sticky': False,
            },
        }
