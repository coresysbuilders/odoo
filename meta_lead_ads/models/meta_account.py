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

# Single source of truth for the token-health activity summary. Referenced by
# both activity_schedule(summary=...) and the dedup search_count in
# _alert_token_dead, so the two can never drift apart.
_TOKEN_DEAD_SUMMARY = 'Meta token invalid/expired'

# Scheduler self-check. The crons stamp CRON_HEARTBEAT_PARAM every time they run;
# INSTALLED_AT_PARAM is set by the post-install hook. The health check compares
# their age against this tolerance so it can tell, without depending on cron
# itself, whether Odoo's scheduler is actually processing our jobs. The drain
# cron runs every minute, so 15 minutes is ~15 missed runs — well clear of a
# momentarily busy server but quick enough to surface a truly stalled scheduler.
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
    # Credentials. App ID is public app metadata, not a secret: it stays
    # ungrouped so a Meta User can read it. The secret fields (App Secret,
    # Access Token) carry field-level groups= so the key is stripped from a
    # non-admin ORM read and from the view. token_owner_id is the stable
    # account identity from the token owner (user_id/profile_id) and is just an
    # identifier, so it stays ungrouped.
    app_id = fields.Char(string='App ID')   # public app metadata; no groups=
    app_secret = fields.Char(string='App Secret',
                             groups='meta_lead_ads.group_meta_admin')
    access_token = fields.Char(string='Access Token',
                               groups='meta_lead_ads.group_meta_admin')
    token_owner_id = fields.Char(string='Token Owner ID', readonly=True,
                                 index=True)
    page_ids = fields.One2many('meta.page', 'account_id', string='Pages')
    page_count = fields.Integer(compute='_compute_page_count', store=True)

    # Connection status. Not secret -- these leak no token, so they stay
    # readable by a Meta User (no groups=). The readiness label is
    # 'lead_retrieval_granted': debug_token cannot confirm Advanced Access /
    # Business Verification, so it must not claim a production-ready state.
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

    # Scheduler self-check (computed live, never stored): leads only sync if
    # Odoo's job scheduler is actually running our crons, which silently fails
    # on a misconfigured server. These surface that state on the account form.
    cron_status = fields.Selection(
        [('ok', 'Running'), ('pending', 'Waiting for first run'),
         ('stalled', 'Not running'), ('disabled', 'Disabled')],
        string='Scheduler', compute='_compute_cron_status')
    cron_status_message = fields.Char(compute='_compute_cron_status')

    # Odoo 19: models.Constraint replaces the removed _sql_constraints list.
    _account_id_uniq = models.Constraint(
        'unique(account_id)',
        'A Meta Account with this ID already exists.',
    )

    @api.depends_context('uid')
    def _compute_cron_status(self):
        # Global state, identical for every account record — compute once.
        health = self._scheduler_health()
        for rec in self:
            rec.cron_status = health['status']
            rec.cron_status_message = health['message']

    @api.depends('page_ids')
    def _compute_page_count(self):
        for rec in self:
            rec.page_count = len(rec.page_ids)

    def action_open_pages(self):
        # The child list's "New" button is auto-hidden for users without
        # create ACL. Meta Users have perm_create=0 on meta.page, so they
        # cannot create here; Meta Admins (full CRUD) can still create via
        # drill-down. No context={'create': False} override -- that would
        # wrongly block legitimate admin creation.
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
        """Translate a debug_token data{} object into status-field values.
        Returns a dict suitable for write()/create().

        An HTTP-200 introspection with is_valid:false (and usually a nested
        data.error) must map to auth_failed -- never report healthy on an
        invalid token. expires_at == 0 means a non-expiring System User token,
        so store empty. The readiness derivation is leads_retrieval_granted =
        is_valid AND 'leads_retrieval' in scopes: a valid token is not a
        production-ready connection, and debug_token cannot confirm Advanced
        Access / Business Verification. The connect wizard calls this same
        mapper to avoid duplicating the data{}->fields logic.
        """
        if not data or data.get('error') or not data.get('is_valid'):
            return {'token_valid': False, 'access_status': 'auth_failed',
                    'token_owner_id': (data or {}).get('user_id')
                    or (data or {}).get('profile_id'),
                    'last_checked': fields.Datetime.now()}
        scopes = data.get('scopes') or []
        leads = 'leads_retrieval' in scopes
        # expires_at comes straight from parsed Graph JSON; a malformed/forged
        # (but is_valid) body could carry a stringized or non-numeric value.
        # Coerce defensively so datetime.fromtimestamp cannot crash the mapper.
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
        """Return the single designated app's App Secret for HMAC
        verification. Read under sudo() by the public webhook controller; it
        must never be logged, echoed, or returned to a client.

        Selection rule: the lowest-id account that actually has a non-empty App
        Secret, via an explicit order='id asc'. The model's default order is by
        name, so a bare search here would pick the alphabetically-first account;
        the explicit id ordering makes the secret pick stable and independent of
        the display name across calls.

        Secret-less accounts are skipped. The pick is filtered in Python (not a
        domain on the groups= secret field) so it never trips a field-access
        check, and it reads .app_secret directly. Otherwise a lowest-id account
        with an empty secret would shadow a later account that had one,
        returning '' and bouncing every inbound webhook with a 403 -- silently
        disabling ingestion for the configured app. Now an empty-secret first
        account is skipped; only an all-empty install still returns falsy.

        A true multi-app seam -- per-event page_id -> account -> that app's
        secret routing, plus an is_webhook_app selector field -- is out of
        scope here. The first-with-secret rule is the deliberate simplification.
        """
        accounts = self.search([], order='id asc')
        for account in accounts:
            if account.app_secret:
                return account.app_secret
        return accounts[:1].app_secret

    def action_test_connection(self):
        """Re-run debug_token and refresh the persistent status.

        Error handling is token-free: never put the token/secret or raw Meta
        JSON in a UserError or log.
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
        # Also classifies an HTTP-200 is_valid:false body as auth_failed.
        self._apply_token_status(data)
        return True

    @api.model
    def _cron_token_health(self):
        """Daily token-health sweep.

        Re-check every active account's token via the existing
        action_test_connection() path -- the real, only entry point.
        action_test_connection already persists token_valid=False +
        access_status='auth_failed' on both the code-190 OAuthException path
        and the HTTP-200 is_valid:false body, raising UserError to signal an
        auth failure.

        The inner except is narrow and state-driven: we catch only UserError --
        the documented auth-failure signal -- because the alert decision reads
        the post-call token_valid state, not the presence or absence of the
        exception.

        Per-account isolation: each account's body runs inside its own
        `with self.env.cr.savepoint():`, wrapped by an outer `except Exception`
        that logs token-free (account.id only -- no token/secret/raw body/
        page-id/display-name) and continues the sweep. So a genuinely
        unexpected (non-UserError) failure on one account -- including an
        _alert_token_dead failure -- no longer aborts the remaining accounts.
        This mirrors meta.lead.form._cron_backfill's per-form savepoint +
        id-only broad catch.

        Alert whenever the post-call token is invalid (`not
        account.token_valid`) -- not only on a valid->invalid transition. This
        covers both a fresh transition and an account that was already invalid.
        The open-activity dedup inside _alert_token_dead is what prevents an
        alert storm.
        """
        for account in self.search([('active', '=', True)]):
            try:
                # Per-account savepoint (sibling isolation): all of this
                # account's work -- the auth-test write and the state-driven
                # alert -- is contained. If _alert_token_dead() raises after
                # action_test_connection() mutated the token-health fields, this
                # savepoint rolls back that account's writes for this sweep (a
                # partial, un-alerted update is not persisted; it is re-attempted
                # next sweep) and the outer catch continues to the next account.
                with self.env.cr.savepoint():
                    try:
                        account.action_test_connection()
                    except UserError:
                        # Documented auth-failure signal; token_valid was already
                        # persisted False by action_test_connection. Decision
                        # below is state-driven, so swallowing this is correct.
                        pass
                    if not account.token_valid:
                        account._alert_token_dead()
            except Exception:
                # Genuinely-unexpected (non-UserError) error on this account:
                # isolate it, log token-free referencing only account.id (no
                # token/secret/raw body/page-id/display-name), and continue the
                # sweep so the remaining accounts are still checked.
                _logger.exception(
                    "Meta token-health: unexpected error on account id=%s "
                    "(no token/secret logged)", account.id)

    def _alert_token_dead(self):
        """Raise a single de-duplicated, token-free alert for a dead token.
        Schedules a To-Do mail.activity on this account and (only when a real
        admin recipient exists) emails the Meta Admins.

        Open-activity dedup: an open token-health To-Do already present
        suppresses both a second activity and a second email. The search_count
        keys on the shared _TOKEN_DEAD_SUMMARY constant. mail.activity rows are
        deleted by Odoo when an activity is marked done, so this matches open
        activities only by construction -- a completed To-Do does not
        permanently suppress a future re-alert. No active/done-state term is
        added to the domain (that would re-introduce permanent suppression).

        Token-free: the activity note, email subject, and email body reference
        self.name only -- never a token, App Secret, or raw Meta JSON.
        """
        self.ensure_one()
        existing = self.env['mail.activity'].search_count([
            ('res_model', '=', 'meta.account'),
            ('res_id', '=', self.id),
            ('summary', '=', _TOKEN_DEAD_SUMMARY),
        ])
        if not existing:
            # Odoo 19 renamed res.groups.users -> user_ids (members explicitly in
            # this group). group_meta_admin is a leaf admin group, so its 18.0
            # .users set was exactly its direct members; user_ids is the faithful
            # equivalent (all_user_ids would over-broaden to members of implying
            # groups and alert non-admins).
            admins = self.env.ref('meta_lead_ads.group_meta_admin').user_ids
            # Assign to a Meta Admin, or fall back to the cron/current user
            # when the admin group is empty.
            self.activity_schedule(
                'mail.mail_activity_data_todo',
                summary=_TOKEN_DEAD_SUMMARY,
                note=_('Re-run the Connect to Meta wizard with a current '
                       'System User token.'),
                user_id=(admins[:1].id or self.env.uid),
            )
            # Filter out admins without an email; never send a mail.mail with
            # an empty email_to.
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
    # Scheduler self-check (SYNC reliability). The whole pipeline depends on
    # Odoo's cron worker actually running; on a misconfigured server (a
    # dbfilter with no db_name, max_cron_threads=0, or --no-cron) the worker
    # sits idle and leads silently never sync. These helpers detect that and
    # tell the admin how to fix it.
    # ------------------------------------------------------------------ #
    @api.model
    def _ping_scheduler_heartbeat(self):
        """Record that a cron just ran. Called at the start of every Meta cron,
        so a fresh value proves the scheduler is alive."""
        self.env['ir.config_parameter'].sudo().set_param(
            CRON_HEARTBEAT_PARAM, fields.Datetime.to_string(fields.Datetime.now()))

    @api.model
    def _scheduler_health(self):
        """Classify whether the scheduler is running our jobs, reading the
        heartbeat plus the drain cron's nextcall. Both come from stored state,
        so this works even when cron is NOT running — exactly the case it must
        catch. Returns a dict with a status code and an admin-facing message."""
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
        # A recent heartbeat is positive proof the scheduler is processing jobs.
        if last_run and now - last_run <= stale:
            return {'status': 'ok',
                    'message': _("Scheduler healthy — a Meta cron last ran %s.")
                    % self._humanize_ago(now - last_run)}
        # No recent heartbeat. A nextcall frozen well in the past confirms the
        # worker is idle (a healthy scheduler keeps nextcall at or near now).
        if drain.nextcall and now - drain.nextcall > stale:
            return {'status': 'stalled', 'message': self._scheduler_fix_hint()}
        # Freshly installed and not yet confirmed — give cron a few minutes.
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
        """Manual 'Check Scheduler' button — re-run the health check and report
        it as a notification."""
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
