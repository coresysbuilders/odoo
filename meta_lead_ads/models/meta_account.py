# Meta Lead Ads Integration for Odoo — © 2026 CoreSys Builders. All rights reserved.
# Licensed under the Odoo Proprietary License v1.0 (OPL-1).
# Unauthorized copying, redistribution, or resale of this software, in whole or in
# part, via any medium, is strictly prohibited and constitutes a license violation.
# OPL-1: https://www.odoo.com/documentation/19.0/legal/licenses.html#odoo-apps

import logging
from datetime import datetime, timezone, timedelta

from odoo import _, api, fields, models
from odoo.exceptions import UserError

from .exceptions import (
    MetaAuthError, MetaPermanentError, MetaRateLimitError, MetaTransientError,
)

_logger = logging.getLogger(__name__)

# Shared by activity_schedule() and the dedup search_count in _alert_token_dead,
# so the two always match.
_TOKEN_DEAD_SUMMARY = 'Meta token invalid/expired'

# Scheduler self-check. Every Meta cron stamps CRON_HEARTBEAT_PARAM when it runs;
# the post-install hook sets INSTALLED_AT_PARAM. The drain cron runs every
# minute, so 15 minutes without a heartbeat means the scheduler has stopped,
# not just that the server was briefly busy.
CRON_HEARTBEAT_PARAM = 'meta_lead_ads.cron_last_run'
INSTALLED_AT_PARAM = 'meta_lead_ads.installed_at'
SCHEDULER_STALE_MINUTES = 15


class MetaAccount(models.Model):
    _name = 'meta.account'
    _description = 'Meta Account'
    _inherit = ['mail.thread', 'mail.activity.mixin']
    _order = 'name, id'

    name = fields.Char(required=True)
    account_id = fields.Char(string='Meta Account ID', required=True, index=True)
    active = fields.Boolean(default=True)
    # App Secret and Access Token are admin-only (groups=), so non-admins can't
    # read them through the ORM or the form. App ID and the token owner id are
    # plain identifiers and stay visible to Meta Users.
    app_id = fields.Char(string='App ID')
    app_secret = fields.Char(string='App Secret',
                             groups='meta_lead_ads.group_meta_admin')
    access_token = fields.Char(string='Access Token',
                               groups='meta_lead_ads.group_meta_admin')
    token_owner_id = fields.Char(string='Token Owner ID', readonly=True,
                                 index=True)
    page_ids = fields.One2many('meta.page', 'account_id', string='Pages')
    page_count = fields.Integer(compute='_compute_page_count', store=True)

    # Connection status, readable by Meta Users. The best state is
    # 'lead_retrieval_granted', not "ready": debug_token can't tell us whether
    # Advanced Access or Business Verification are done.
    token_valid = fields.Boolean(string='Token Valid', readonly=True)
    token_type = fields.Char(string='Token Type', readonly=True)
    expires_at = fields.Datetime(string='Token Expires', readonly=True)
    granted_scopes = fields.Char(string='Granted Scopes', readonly=True)
    leads_retrieval_granted = fields.Boolean(string='leads_retrieval Granted',
                                             readonly=True)
    access_status = fields.Selection(
        [('auth_failed', 'Authentication failed'),
         ('dev_test_only', 'Development mode — test leads only'),
         ('lead_retrieval_granted',
          'leads_retrieval granted (App Review still gates production)')],
        string='Access Status', readonly=True)
    last_checked = fields.Datetime(string='Last Checked', readonly=True)

    # Computed live, not stored. Leads only sync if Odoo's scheduler actually
    # runs our crons, and a misconfigured server fails at that silently.
    cron_status = fields.Selection(
        [('ok', 'Running'), ('pending', 'Waiting for first run'),
         ('stalled', 'Not running'), ('disabled', 'Disabled')],
        string='Scheduler', compute='_compute_cron_status')
    cron_status_message = fields.Char(compute='_compute_cron_status')

    # Odoo 19: models.Constraint replaces _sql_constraints.
    _account_id_uniq = models.Constraint(
        'unique(account_id)',
        'A Meta Account with this ID already exists.',
    )

    @api.depends_context('uid')
    def _compute_cron_status(self):
        # Same answer for every account, so compute it once.
        health = self._scheduler_health()
        for rec in self:
            rec.cron_status = health['status']
            rec.cron_status_message = health['message']

    @api.depends('page_ids')
    def _compute_page_count(self):
        for rec in self:
            rec.page_count = len(rec.page_ids)

    def action_open_pages(self):
        # No context={'create': False}: Odoo already hides "New" for Meta Users
        # (no create ACL on meta.page), and admins still need to create here.
        self.ensure_one()
        return {
            'type': 'ir.actions.act_window',
            'name': 'Pages',
            'res_model': 'meta.page',
            'view_mode': 'list,form',
            'domain': [('account_id', '=', self.id)],
            'context': {'default_account_id': self.id},
        }

    @api.model
    def _map_token_status(self, data):
        """Turn a debug_token data{} object into status field values.

        A 200 response with is_valid false maps to auth_failed. expires_at 0
        means a non-expiring System User token and is stored empty. The connect
        wizard uses this mapper too.
        """
        if not data or data.get('error') or not data.get('is_valid'):
            return {'token_valid': False, 'access_status': 'auth_failed',
                    'token_owner_id': (data or {}).get('user_id')
                    or (data or {}).get('profile_id'),
                    'last_checked': fields.Datetime.now()}
        scopes = data.get('scopes') or []
        leads = 'leads_retrieval' in scopes
        # Coerce expires_at: a non-numeric value would crash fromtimestamp.
        exp = data.get('expires_at') or 0
        try:
            exp = int(exp)
        except (TypeError, ValueError):
            exp = 0
        return {
            'token_valid': True,
            'token_type': data.get('type'),
            'token_owner_id': data.get('user_id') or data.get('profile_id'),
            'expires_at': fields.Datetime.to_string(
                datetime.fromtimestamp(exp, tz=timezone.utc).replace(tzinfo=None)
            ) if exp else False,
            'granted_scopes': ','.join(scopes),
            'leads_retrieval_granted': leads,
            'access_status': 'lead_retrieval_granted' if leads else 'dev_test_only',
            'last_checked': fields.Datetime.now(),
        }

    def _apply_token_status(self, data):
        self.ensure_one()
        self.write(self._map_token_status(data))

    @api.model
    def _webhook_app_secret(self):
        """Return the App Secret used to verify webhook signatures.

        Called under sudo() by the public webhook controller; never log or
        return it to a client. Picks the lowest-id account that has a secret
        (explicit id order, since the default order is by name). Accounts
        without a secret are skipped, otherwise an empty one would make every
        webhook fail with 403. Per-app routing for several Meta apps is not
        supported.
        """
        accounts = self.search([], order='id asc')
        for account in accounts:
            if account.app_secret:
                return account.app_secret
        return accounts[:1].app_secret

    def action_test_connection(self):
        """Re-run debug_token and refresh the stored status.

        Error messages never include the token, secret or raw Meta response.
        """
        self.ensure_one()
        client = self.env['meta.graph.client']
        try:
            data = client.debug_token(self.app_id, self.app_secret,
                                      self.access_token)
        except MetaAuthError:
            self.write({'token_valid': False, 'access_status': 'auth_failed',
                        'last_checked': fields.Datetime.now()})
            raise UserError(_("That token is invalid or expired. Re-run the "
                              "Connect to Meta wizard with a current "
                              "System User token."))
        except (MetaPermanentError, MetaRateLimitError, MetaTransientError):
            raise UserError(_("Could not reach Meta to validate the token. "
                              "Check the App ID / App Secret and try again."))
        self._apply_token_status(data)
        return True

    @api.model
    def _cron_token_health(self):
        """Daily check of every active account's token.

        Alerts whenever the token is invalid after the check, not only on the
        transition; _alert_token_dead dedups so this doesn't spam. Each account
        runs in its own savepoint, so one unexpected failure is logged (by id
        only) and the sweep carries on.
        """
        for account in self.search([('active', '=', True)]):
            try:
                # If the alert fails after the status write, roll back this
                # account's changes so the next sweep tries again cleanly.
                with self.env.cr.savepoint():
                    try:
                        account.action_test_connection()
                    except UserError:
                        # Auth failure; token_valid is already False and the
                        # check below handles it.
                        pass
                    if not account.token_valid:
                        account._alert_token_dead()
            except Exception:
                _logger.exception(
                    "Meta token-health: unexpected error on account id=%s "
                    "(no token/secret logged)", account.id)

    def _alert_token_dead(self):
        """Schedule a To-Do and email the Meta Admins about a dead token.

        Skipped if an open To-Do with the same summary exists. Odoo deletes
        activities when they're marked done, so a closed one doesn't block the
        next alert. The note and email mention only the account name.
        """
        self.ensure_one()
        existing = self.env['mail.activity'].search_count([
            ('res_model', '=', 'meta.account'),
            ('res_id', '=', self.id),
            ('summary', '=', _TOKEN_DEAD_SUMMARY),
        ])
        if not existing:
            # Odoo 19 renamed res.groups.users to user_ids (direct members).
            # all_user_ids would also pull in users of implying groups.
            admins = self.env.ref('meta_lead_ads.group_meta_admin').user_ids
            # Fall back to the current (cron) user if there are no admins.
            self.activity_schedule(
                'mail.mail_activity_data_todo',
                summary=_TOKEN_DEAD_SUMMARY,
                note=_('Re-run the Connect to Meta wizard with a current '
                       'System User token.'),
                user_id=(admins[:1].id or self.env.uid),
            )
            # Skip admins with no email; don't send with an empty email_to.
            recipients = [e for e in admins.mapped('email') if e]
            if recipients:
                vals = {
                    'subject': _('Meta Lead Ads: access token invalid for %s')
                    % self.name,
                    'body_html': _('<p>The Meta access token for account %s is '
                                   'invalid or expired. Lead ingestion is '
                                   'paused until it is renewed.</p>')
                    % self.name,
                    'email_to': ','.join(recipients),
                }
                self.env['mail.mail'].create(vals).send()

    # ------------------------------------------------------------------ #
    # Scheduler self-check. If the cron worker never runs (dbfilter without
    # db_name, max_cron_threads=0, --no-cron) leads silently stop syncing.
    # These helpers detect that and tell the admin what to fix.
    # ------------------------------------------------------------------ #
    @api.model
    def _ping_scheduler_heartbeat(self):
        """Record that a Meta cron just ran."""
        self.env['ir.config_parameter'].sudo().set_param(
            CRON_HEARTBEAT_PARAM, fields.Datetime.to_string(fields.Datetime.now()))

    @api.model
    def _scheduler_health(self):
        """Return {'status', 'message'} describing whether our crons run.

        Reads only stored state (the heartbeat and the drain cron's nextcall),
        so it works when the scheduler itself is down.
        """
        ICP = self.env['ir.config_parameter'].sudo()
        now = fields.Datetime.now()
        stale = timedelta(minutes=SCHEDULER_STALE_MINUTES)
        drain = self.env.ref('meta_lead_ads.cron_meta_webhook_drain',
                             raise_if_not_found=False)
        if not drain or not drain.active:
            return {'status': 'disabled',
                    'message': _("The Meta webhook-drain scheduled action is "
                                 "inactive, so leads will not sync "
                                 "automatically. Re-enable it under Settings > "
                                 "Technical > Scheduled Actions.")}
        last_run = fields.Datetime.to_datetime(ICP.get_param(CRON_HEARTBEAT_PARAM))
        if last_run and now - last_run <= stale:
            return {'status': 'ok',
                    'message': _("Scheduler healthy — a Meta cron last ran %s.")
                    % self._humanize_ago(now - last_run)}
        # A running scheduler keeps nextcall close to now; one stuck in the
        # past means the worker is idle.
        if drain.nextcall and now - drain.nextcall > stale:
            return {'status': 'stalled', 'message': self._scheduler_fix_hint()}
        # Just installed: give cron a few minutes before calling it stalled.
        installed_at = fields.Datetime.to_datetime(ICP.get_param(INSTALLED_AT_PARAM))
        if installed_at and now - installed_at <= stale:
            return {'status': 'pending',
                    'message': _("Waiting for the first scheduled run to confirm "
                                 "the scheduler is active (give it a few "
                                 "minutes, then refresh).")}
        return {'status': 'stalled', 'message': self._scheduler_fix_hint()}

    @api.model
    def _scheduler_fix_hint(self):
        return _("Odoo's job scheduler does not appear to be running, so Meta "
                 "leads will NOT sync automatically. On the server, check that "
                 "the Odoo config sets 'db_name' (a 'dbfilter' alone is not "
                 "enough for cron), that 'max_cron_threads' is not 0, and that "
                 "Odoo is not started with --no-cron; then restart the service.")

    @api.model
    def _humanize_ago(self, delta):
        secs = int(delta.total_seconds())
        if secs < 90:
            return _("less than a minute ago")
        minutes = secs // 60
        if minutes < 90:
            return _("%s minutes ago") % minutes
        hours = minutes // 60
        if hours < 36:
            return _("%s hours ago") % hours
        return _("%s days ago") % (hours // 24)

    def action_check_scheduler(self):
        """Check Scheduler button: show the health check as a notification."""
        health = self._scheduler_health()
        return {
            'type': 'ir.actions.client',
            'tag': 'display_notification',
            'params': {
                'title': _("Scheduler check"),
                'message': health['message'],
                'type': 'success' if health['status'] == 'ok' else 'warning',
                'sticky': health['status'] != 'ok',
            },
        }
