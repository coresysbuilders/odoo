# Meta Lead Ads Integration for Odoo — © 2026 CoreSys Builders. All rights reserved.
# Licensed under the Odoo Proprietary License v1.0 (OPL-1).
# Unauthorized copying, redistribution, or resale of this software, in whole or in
# part, via any medium, is strictly prohibited and constitutes a license violation.
# OPL-1: https://www.odoo.com/documentation/19.0/legal/licenses.html#odoo-apps

# Meta Lead Ads settings: lead title template, new-lead notifications, data
# retention and the app-wide lead read pause.
#
# The Meta values are global ir.config_parameter values. They are not bound
# through the fields' config_parameter option because they are validated
# before saving, and because a Meta admin without Settings rights must be able
# to save them without saving anything else.
#
# _save_meta_settings is the one place that validates and saves them, and it
# checks group_meta_admin itself: the view groups= only hides the UI and does
# not stop a direct RPC call.
# - A Meta admin without Administration rights saves through execute, which
#   for them runs _save_meta_settings and nothing else: no other app's
#   settings and no module install.
# - set_values is only for users core lets save settings (env.is_admin()),
#   and raises for anyone else.
# - A Settings admin who isn't a Meta admin saves every other setting, but a
#   changed Meta value is refused. Unchanged ones are skipped, so a bad stored
#   value can't block their save.
#
# The retro rename writes under sudo too and checks the group the same way.
# The connection status reads access_status only, never the app secret or
# tokens.
import logging

from odoo import _, api, fields, models
from odoo.exceptions import AccessError, UserError, ValidationError

from .const import (
    DEFAULT_LEAD_NAME, LEAD_NAME_PARAM,
    NOTIFY_ENABLED_PARAM, NOTIFY_TARGET_PARAM,
    NOTIFY_USER_PARAM, NOTIFY_GROUP_PARAM,
    SYNC_LOG_RETENTION_PARAM, QUEUE_DONE_RETENTION_PARAM,
    FAILED_RETENTION_PARAM, RETENTION_MAX_DAYS,
)

# Settings field -> retention parameter it is stored in.
_RETENTION_FIELDS = (
    ('meta_sync_log_retention_days', SYNC_LOG_RETENTION_PARAM),
    ('meta_queue_done_retention_days', QUEUE_DONE_RETENTION_PARAM),
    ('meta_failed_retention_days', FAILED_RETENTION_PARAM),
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

    # Read in get_values and saved by _save_meta_settings, which validates it.
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

    # Retention, in days. Stored like the fields above, and refused below 1
    # or above RETENTION_MAX_DAYS in _save_meta_settings.
    meta_sync_log_retention_days = fields.Integer(
        string="Sync log retention (days)")
    meta_queue_done_retention_days = fields.Integer(
        string="Processed webhook rows (days)")
    meta_failed_retention_days = fields.Integer(
        string="Failed rows (days)")

    # End of the app-wide lead read pause, empty when none is running.
    meta_lead_fetch_paused_until = fields.Datetime(
        string="Lead reads paused until",
        compute='_compute_meta_lead_fetch_paused_until')

    # Shown in the Connection block when the active accounts hold more than
    # one Meta app. Display only: it has no parameter and is never saved.
    meta_app_conflict_message = fields.Char(compute='_compute_meta_app_conflict_message')  # noqa: E501

    def _compute_display_name(self):
        # Core takes the name from the settings action, read as the current
        # user, and on Odoo 19 only Administration users may read actions.
        # The browser asks for this name when it opens and saves the form,
        # so a Meta admin got an access error on Meta Leads > Settings.
        # Same lookup with sudo; only the action's name comes back.
        action = self.env['ir.actions.act_window'].sudo().search(
            [('res_model', '=', self._name)], limit=1)
        for rec in self:
            rec.display_name = action.name or self._name

    def _meta_only_caller(self):
        """A Meta admin whom core does not let save settings."""
        return (not self.env.is_admin()
                and self.env.user.has_group('meta_lead_ads.group_meta_admin'))

    def _meta_own_values(self, vals):
        """The entries of vals that Meta Lead Ads owns, plus the company.

        Anything else on a settings record can write elsewhere when the
        record is created or changed: an editable related field or a field
        with an inverse writes to its target, and some inverses run under
        sudo. company_id has a required default and no inverse.
        """
        return {name: value for name, value in vals.items()
                if name == 'company_id'
                or (name in self._fields
                    and self._fields[name]._module == 'meta_lead_ads')}

    @api.model
    def default_get(self, fields):
        res = super().default_get(fields)
        if self._meta_only_caller():
            # Their record only carries the Meta values (execute saves
            # nothing else for them). Another app's default would be
            # written back by create(): hr's module_hr_attendance to the
            # company, which they may not write.
            own = self._meta_own_values(res)
            # A required plain column still needs a value or the insert
            # fails (sale_stock's default_picking_policy). It stays on this
            # record. The field's own default is used, so the record shows
            # nothing of the database's settings.
            for name in fields:
                field = self._fields.get(name)
                if (name in own or not field or not field.required
                        or not field.store or field.related or field.inverse
                        or field.compute):
                    continue
                if field.default is not None:
                    value = field.convert_to_cache(
                        field.default(self), self, validate=False)
                    own[name] = field.convert_to_write(value, self)
                elif name in res:
                    own[name] = res[name]
            res = own
        return res

    @api.model_create_multi
    def create(self, vals_list):
        if self._meta_only_caller():
            # A direct call can name any field. Without this, an inverse
            # such as portal's Customer API Keys runs as sudo.
            vals_list = [self._meta_own_values(vals) for vals in vals_list]
        return super().create(vals_list)

    def write(self, vals):
        if self._meta_only_caller():
            vals = self._meta_own_values(vals)
        return super().write(vals)

    # ------------------------------------------------------------------ #
    # ir.config_parameter read/write
    # ------------------------------------------------------------------ #
    def get_values(self):
        res = super().get_values()
        res.update(self._get_meta_values())
        return res

    def _get_meta_values(self):
        """Stored Meta values, as the settings form shows them."""
        ICP = self.env['ir.config_parameter'].sudo()
        res = {}
        res['lead_name_template'] = ICP.get_param(
            LEAD_NAME_PARAM, DEFAULT_LEAD_NAME)
        res['meta_notify_enabled'] = ICP.get_param(NOTIFY_ENABLED_PARAM) == '1'
        res['meta_notify_target_type'] = ICP.get_param(
            NOTIFY_TARGET_PARAM, 'user')
        notify_uid = ICP.get_param(NOTIFY_USER_PARAM)
        res['meta_notify_user_id'] = int(notify_uid) if notify_uid else False
        notify_gid = ICP.get_param(NOTIFY_GROUP_PARAM)
        res['meta_notify_group_id'] = int(notify_gid) if notify_gid else False
        # Read through the same helper as the cleanup job, so a stored 0 or
        # junk value shows the number of days actually used.
        Log = self.env['meta.sync.log']
        for field_name, key in _RETENTION_FIELDS:
            res[field_name] = Log._retention_days(key)
        return res

    def _meta_values_from_record(self):
        """The Meta values on this settings record, keyed like _get_meta_values."""
        res = {
            'lead_name_template': self.lead_name_template,
            'meta_notify_enabled': self.meta_notify_enabled,
            'meta_notify_target_type': self.meta_notify_target_type,
            'meta_notify_user_id': self.meta_notify_user_id,
            'meta_notify_group_id': self.meta_notify_group_id,
        }
        for field_name, _key in _RETENTION_FIELDS:
            res[field_name] = self[field_name]
        return res

    @api.model
    def _meta_param_values(self, values):
        """Map each parameter key to the string to store for the given values.

        Used both to save and to tell whether anything changed, so the two
        always agree. Many2one values may be a record or an id.
        """
        def _id_str(value):
            if isinstance(value, models.BaseModel):
                value = value.id
            return str(value) if value else ''

        res = {
            # An empty template falls back to the default.
            LEAD_NAME_PARAM: values['lead_name_template'] or DEFAULT_LEAD_NAME,
            NOTIFY_ENABLED_PARAM: '1' if values['meta_notify_enabled'] else '0',
            NOTIFY_TARGET_PARAM: values['meta_notify_target_type'] or 'user',
            # Stored as the id string; empty clears the key.
            NOTIFY_USER_PARAM: _id_str(values['meta_notify_user_id']),
            NOTIFY_GROUP_PARAM: _id_str(values['meta_notify_group_id']),
        }
        for field_name, key in _RETENTION_FIELDS:
            res[key] = str(int(values[field_name] or 0))
        return res

    def _save_meta_settings(self):
        """Validate and save the Meta values of this record.

        Only Meta admins (or sudo) may change them. Anyone else gets an
        error for a changed value, and nothing is saved or validated when
        nothing changed.
        """
        new = self._meta_param_values(self._meta_values_from_record())
        may_edit = (self.env.su
                    or self.env.user.has_group('meta_lead_ads.group_meta_admin'))
        if not may_edit:
            # The params are written under sudo, so this is the check.
            if new != self._meta_param_values(self._get_meta_values()):
                raise AccessError(_(
                    "Only Meta administrators can change Meta Lead Ads settings."))
            return
        # Checked before anything is saved. There is no "keep forever", and
        # the cleanup reads a longer value as the default.
        if any(not 1 <= self[field_name] <= RETENTION_MAX_DAYS
               for field_name, _key in _RETENTION_FIELDS):
            raise ValidationError(_(
                "Retention must be between 1 and %(max)s days.",
                max=RETENTION_MAX_DAYS))
        # Reject unknown or malformed tokens before anything is saved.
        self.env['meta.lead.ingest']._validate_lead_name_template(
            new[LEAD_NAME_PARAM])
        ICP = self.env['ir.config_parameter'].sudo()
        for key, value in new.items():
            ICP.set_param(key, value)

    def set_values(self):
        # Raise rather than skip: a module loaded after this one wraps this
        # method, and only an exception rolls back what it wrote around its
        # super() call. Meta admins without Administration rights save
        # through execute instead.
        if not self.env.is_admin():
            raise AccessError(_("Only administrators can change the settings"))
        self._save_meta_settings()
        return super().set_values()

    def execute(self):
        self.ensure_one()
        if self._meta_only_caller():
            # Core refuses these users. They may save the Meta values and
            # nothing else: set_values and core execute would also save other
            # apps' settings and install modules.
            self.with_context(active_test=False)._save_meta_settings()
            return {'type': 'ir.actions.client', 'tag': 'reload'}
        return super().execute()

    # ------------------------------------------------------------------ #
    # Live preview. Errors are swallowed so the form keeps working while
    # the admin types; _save_meta_settings does the real validation.
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
    @api.depends_context('uid', 'allowed_company_ids')
    def _compute_meta_connection_status(self):
        # Read before sudo so a forged company context is refused. Accounts
        # of the selected companies count, and so does an account in another
        # company that feeds a Page routed into one of them. Only the status
        # code comes out, never anything about the account itself.
        company_ids = self.env.companies.ids
        # Same value for every record, so compute it once.
        accounts = self.env['meta.account'].sudo().search(
            [('active', '=', True),
             '|', ('company_id', 'in', company_ids),
             ('page_ids.lead_company_id', 'in', company_ids)],
            order='id asc')
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

    def _compute_meta_app_conflict_message(self):
        # Carries the number of apps only, so it is safe to show to a Meta
        # admin of any company.
        message = self.env['meta.account']._app_conflict_message()
        for rec in self:
            rec.meta_app_conflict_message = message

    # ------------------------------------------------------------------ #
    # App-wide lead read pause. Meta can throttle the whole app; the queue
    # then waits for every Page, and this is where an admin can end it.
    # ------------------------------------------------------------------ #
    def _compute_meta_lead_fetch_paused_until(self):
        until = self.env['meta.webhook.event']._app_lead_fetch_paused_until()
        if not until or until <= fields.Datetime.now():
            until = False
        for rec in self:
            rec.meta_lead_fetch_paused_until = until

    def action_resume_app_lead_reads(self):
        """End the app-wide lead read pause now and let the queue run.

        Makes no Graph call: if Meta still refuses, the drain sets a new
        pause from Meta's new estimate. The setting is shared by every
        company, like the throttle it records.
        """
        # The view groups= only hides the button; RPC reaches this directly.
        if (not self.env.su
                and not self.env.user.has_group('meta_lead_ads.group_meta_admin')):
            raise AccessError(_("Meta Admin access required."))
        if not self.env['meta.webhook.event']._clear_app_lead_fetch_pause():
            raise UserError(_(
                "Lead reads are being updated right now. Try again in a "
                "minute."))
        self.env.ref('meta_lead_ads.cron_meta_webhook_drain').sudo()._trigger()
        return {
            'type': 'ir.actions.client',
            'tag': 'display_notification',
            'params': {
                'message': _("Lead reads resumed. Waiting leads are fetched "
                             "shortly."),
                'type': 'success',
                'sticky': False,
                'next': {'type': 'ir.actions.client', 'tag': 'reload'},
            },
        }

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

        Only leads of the companies selected in the switcher, plus leads
        with no company, are renamed or counted.
        """
        self.ensure_one()
        if not self.env.user.has_group('meta_lead_ads.group_meta_admin'):
            raise AccessError(_(
                "Only Meta administrators can re-apply the lead title template."))
        # Read before sudo: here Odoo refuses an allowed_company_ids the user
        # doesn't belong to. The sudo searches below skip record rules, so
        # each one carries this company filter.
        companies = self.env.companies

        template = self.lead_name_template or DEFAULT_LEAD_NAME
        # Reject a broken template before any bulk write.
        self.env['meta.lead.ingest']._validate_lead_name_template(template)

        Ingest = self.env['meta.lead.ingest']
        Lead = self.env['crm.lead'].sudo()
        domain = [('meta_leadgen_id', '!=', False),
                  ('meta_name_is_generated', '=', True),
                  ('company_id', 'in', companies.ids + [False])]
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
             ('meta_name_is_generated', '=', False),
             ('company_id', 'in', companies.ids + [False])])
        return self._retro_notification(renamed, skipped_manual=skipped_manual,
                                        skipped_date=skipped_date,
                                        companies=companies)

    def _retro_notification(self, renamed, skipped_manual=0, skipped_date=0,
                            companies=None):
        message = _("%s lead(s) re-titled.") % renamed
        if skipped_manual:
            message += _(
                " %s manually-edited lead(s) left unchanged.") % skipped_manual
        if skipped_date:
            message += _(
                " %s lead(s) without a stored Meta submission date left "
                "unchanged ({date} template).") % skipped_date
        # These are the user's own selected companies, so naming them shows
        # nothing foreign.
        if companies:
            message += _(" Companies: %s.") % ", ".join(
                companies.mapped('name'))
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
