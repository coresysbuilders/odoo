# Meta Lead Ads Integration for Odoo — © 2026 CoreSys Builders. All rights reserved.
# Licensed under the Odoo Proprietary License v1.0 (OPL-1).
# Unauthorized copying, redistribution, or resale of this software, in whole or in
# part, via any medium, is strictly prohibited and constitutes a license violation.
# OPL-1: https://www.odoo.com/documentation/19.0/legal/licenses.html#odoo-apps

import logging
from datetime import datetime, timezone, timedelta

from dateutil.relativedelta import relativedelta
from markupsafe import Markup

from odoo import _, api, fields, models
from odoo.exceptions import UserError, ValidationError

from . import const
from .exceptions import (
    MetaAuthError, MetaPermanentError, MetaRateLimitError, MetaTransientError,
)

_logger = logging.getLogger(__name__)

# Shared by activity_schedule() and the dedup search_count in _alert_token_dead,
# so the two always match.
_TOKEN_DEAD_SUMMARY = 'Meta token invalid/expired'

# Advisory lock key taken before any check or write that could put a second
# Meta app on the active accounts, or change the shared App Secret.
_SINGLE_APP_LOCK_KEY = 'meta_lead_ads.single_app'
# System parameter rewritten under that lock. See _lock_single_app.
_SINGLE_APP_GUARD_PARAM = 'meta_lead_ads.single_app_guard'

# Scheduler self-check. The queue drain and the backfill each stamp their own
# key in const when they run, and the drain also stamps the last run that
# finished a webhook event. The post-install hook sets INSTALLED_AT_PARAM. The
# drain cron runs every minute, so 15 minutes without a drain run means the
# scheduler has stopped, not just that the server was briefly busy.
INSTALLED_AT_PARAM = 'meta_lead_ads.installed_at'
SCHEDULER_STALE_MINUTES = 15
# Worst wins when the drain and the backfill are folded into one status.
_CRON_SEVERITY = {'ok': 0, 'pending': 1, 'backlog': 2, 'stalled': 3,
                  'disabled': 4}
# A scheduled action's interval unit as a relativedelta argument and how
# many of it one unit is. These are all the units ir.cron offers on Odoo 18
# and 19; core keeps the same table private, so the module has its own.
_CRON_INTERVAL_UNITS = {
    'minutes': ('minutes', 1),
    'hours': ('hours', 1),
    'days': ('days', 1),
    'weeks': ('days', 7),
    'months': ('months', 1),
}


class MetaAccount(models.Model):
    _name = 'meta.account'
    _description = 'Meta Account'
    _inherit = ['mail.thread', 'mail.activity.mixin']
    _order = 'name, id'

    name = fields.Char(required=True)
    account_id = fields.Char(string='Meta Account ID', required=True, index=True)
    # Company that receives this account's leads unless a Page overrides it.
    company_id = fields.Many2one(
        'res.company', string='Company', required=True, index=True,
        default=lambda self: self.env.company)
    active = fields.Boolean(default=True)
    # App Secret and Access Token are admin-only (groups=), so non-admins can't
    # read them through the ORM or the form. App ID and the token owner id are
    # plain identifiers and stay visible to Meta Users.
    app_id = fields.Char(string='App ID')
    app_secret = fields.Char(
        string='App Secret', groups='meta_lead_ads.group_meta_admin',
        help="Every Meta account in this database shares one app, so a new App Secret is saved on all of them, archived ones included.")  # noqa: E501
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
    # The worse of the queue drain and the backfill.
    cron_status = fields.Selection(
        [('ok', 'Running'), ('pending', 'Waiting for first run'),
         ('backlog', 'Falling behind'),
         ('stalled', 'Not running'), ('disabled', 'Disabled')],
        string='Scheduler', compute='_compute_cron_status')
    cron_status_message = fields.Char(compute='_compute_cron_status')

    # Warning for a database that still holds accounts of more than one app,
    # from before only one was allowed. Computed live rather than stored, so
    # it goes away as soon as the extra app's accounts are archived, however
    # the second app got in.
    app_conflict_message = fields.Char(compute='_compute_app_conflict_message')

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

    def _compute_app_conflict_message(self):
        # Same answer for every account, so compute it once.
        message = self._app_conflict_message()
        for rec in self:
            rec.app_conflict_message = message

    @api.depends('page_ids')
    def _compute_page_count(self):
        for rec in self:
            rec.page_count = len(rec.page_ids)

    # The company check lives in create/write rather than @api.constrains:
    # constraint methods always run as sudo, so they can't tell a privileged
    # caller from a user's RPC call.
    #
    # The one-app rule is checked here too, for every caller including sudo:
    # it protects the data, not access. It runs on the values the records
    # will have, before anything is written, so a caller that catches the
    # refusal and carries on doesn't keep a half-made change.
    @api.model_create_multi
    def create(self, vals_list):
        # The checks below look at every account, so a caller without the
        # right gets the access error first and nothing about the apps.
        if not self.env.su:
            self.check_access('create')
        vals_list = [dict(vals) for vals in vals_list]
        for vals in vals_list:
            if 'app_id' in vals:
                vals['app_id'] = self._normalize_app_id(vals['app_id'])
        context_secret = self.env.context.get('default_app_secret')
        if (self.env.context.get('default_app_id') or context_secret
                or any(vals.get('app_id') or vals.get('app_secret')
                       for vals in vals_list)):
            self._lock_single_app()
        self._check_new_accounts_apps(vals_list)
        if context_secret or any(vals.get('app_secret') for vals in vals_list):
            self._check_secret_target(*self._new_secret_targets(vals_list))
        # One savepoint for the insert, the company check and the secret
        # copy, so a refused company leaves no account and no new secret.
        with self.env.cr.savepoint():
            records = super().create(vals_list)
            if not self.env.su:
                records._check_company_in_user_companies()
            for record in records:
                secret = record.sudo().app_secret
                if secret:
                    record._propagate_app_secret(secret)
        return records

    def write(self, vals):
        if not self.env.su:
            self.check_access('write')
        vals = dict(vals)
        if 'app_id' in vals:
            vals['app_id'] = self._normalize_app_id(vals['app_id'])
        app_change = 'app_id' in vals or 'active' in vals
        secret = vals.get('app_secret')
        # A secret change rewrites every account, so it waits on the same
        # lock as an app change and two rotations can't interleave.
        if app_change or secret:
            self._lock_single_app()
        if app_change:
            projected = {
                rec.id: (bool(vals.get('active', rec.active)),
                         vals['app_id'] if 'app_id' in vals
                         else self._normalize_app_id(rec.app_id))
                for rec in self.sudo()}
            # Only records that really change are checked, so status writes
            # and same-app reconnects keep working in a database that already
            # holds two apps.
            changed = self.sudo().filtered(
                lambda rec: projected[rec.id]
                != (rec.active, self._normalize_app_id(rec.app_id)))
            changed._check_single_app(projected)
        if secret:
            self._check_secret_target(*self._secret_targets(vals))
        if not (secret or 'company_id' in vals):
            return super().write(vals)
        # The company check runs before the secret is copied anywhere, and
        # both share the write's savepoint: a refused company leaves every
        # account as it was.
        with self.env.cr.savepoint():
            res = super().write(vals)
            if 'company_id' in vals and not self.env.su:
                self._check_company_in_user_companies()
            if secret:
                self._propagate_app_secret(secret)
        return res

    def web_save(self, vals, specification, next_id=None):
        # The account form saves through web_save, so a secret typed there
        # is checked with Meta before it reaches every account. Plain write()
        # is not checked: crons, the wizard and imports must keep working
        # when Meta can't be reached, and the wizard checks the secret itself.
        if vals.get('app_secret'):
            # Before the Meta call, so a caller without the right can't use
            # this server to try secrets against Meta.
            if not self.env.su:
                self.check_access('write' if self else 'create')
            # write() and create() check this again under the lock. Here it
            # only saves a Meta call for a secret that would be refused.
            if self:
                self._check_secret_target(*self._secret_targets(vals))
            else:
                self._check_secret_target(*self._new_secret_targets([vals]))
            self._check_form_app_secret(vals)
        return super().web_save(vals, specification, next_id=next_id)

    def _check_form_app_secret(self, vals):
        """Ask Meta whether the secret typed on the form is right.

        Writes nothing. Messages never carry the secret, the token or
        Meta's own error text.
        """
        secret = vals['app_secret']
        if self:
            self.ensure_one()
            if secret == self.sudo().app_secret:
                return
        app_id = self._normalize_app_id(
            vals['app_id'] if 'app_id' in vals else self.app_id)
        token = vals.get('access_token') or (
            self.sudo().access_token if self else False)
        if not app_id or not token:
            raise UserError(_(
                "Add the App ID and access token first, or change the App Secret through Connect to Meta, which checks it with Meta."))  # noqa: E501
        rejected = UserError(_(
            "Meta didn't accept this App Secret for app %(app)s. Nothing was saved; every account keeps its current secret.",  # noqa: E501
            app=app_id))
        try:
            data = self.env['meta.graph.client'].debug_token(
                app_id, secret, token)
        except MetaAuthError:
            raise rejected from None
        except (MetaPermanentError, MetaRateLimitError, MetaTransientError):
            raise UserError(_(
                "Couldn't reach Meta to check the new App Secret. Nothing was saved; try again in a moment.")) from None  # noqa: E501
        # The token belongs to another app, so this answer doesn't confirm
        # the secret for the account's app.
        reported = self._normalize_app_id((data or {}).get('app_id'))
        if reported and reported != app_id:
            raise rejected

    def _check_company_in_user_companies(self):
        # Stops a crafted RPC call from routing leads into a company the
        # writer has no access to.
        for rec in self:
            if rec.company_id not in self.env.user.company_ids:
                raise ValidationError(_(
                    "You can only assign a company you belong to."))

    # ------------------------------------------------------------------ #
    # One Meta app per database. Webhooks are signed with the app's secret
    # and there is one webhook endpoint, so every active account must belong
    # to the same app and every account carries the same App Secret.
    # ------------------------------------------------------------------ #
    @api.model
    def _normalize_app_id(self, value):
        """Return the App ID without surrounding spaces, or False."""
        if not value:
            return False
        return str(value).strip() or False

    def _lock_single_app(self):
        # Held until the transaction ends. Taking it again in the same
        # transaction doesn't block, so nested writes are fine.
        self.env.cr.execute(
            "SELECT pg_advisory_xact_lock(hashtext(%s)::bigint)",
            (_SINGLE_APP_LOCK_KEY,))
        # The lock alone isn't enough. Odoo runs at REPEATABLE READ, so a
        # transaction that waited for it still reads its old snapshot and
        # misses the account the other one just committed. Rewriting one
        # shared row makes that transaction fail with a serialization
        # error instead, and Odoo retries it on a fresh snapshot. The
        # value is kept as it is, and the row is created if it's missing.
        self.env.cr.execute("""
            INSERT INTO ir_config_parameter (key, value, create_date,
                                             write_date)
            VALUES (%s, '1', now() AT TIME ZONE 'UTC',
                    now() AT TIME ZONE 'UTC')
            ON CONFLICT (key) DO UPDATE SET value = ir_config_parameter.value
        """, (_SINGLE_APP_GUARD_PARAM,))

    @api.model
    def _other_app_error(self, current, new):
        return UserError(_(
            "This database is connected to Meta app %(current)s. To use app "
            "%(new)s, "
            "archive every account of app %(current)s first (Meta Leads > Settings > Open Accounts).",  # noqa: E501
            current=current, new=new))

    @api.model
    def _reactivation_error(self, old, current):
        return UserError(_(
            "This account belongs to Meta app %(old)s, but this database is "
            "connected to Meta app %(current)s. "
            "It can't be reactivated while app %(current)s is in use.",
            old=old, current=current))

    @api.model
    def _active_app_ids(self, exclude_ids=()):
        """Distinct App IDs of the active accounts, lowest account id first."""
        accounts = self.sudo().with_context(active_test=True).search(
            [('app_id', '!=', False), ('app_id', '!=', ''),
             ('id', 'not in', list(exclude_ids))], order='id')
        apps = []
        for account in accounts:
            app = self._normalize_app_id(account.app_id)
            if app and app not in apps:
                apps.append(app)
        return apps

    @api.model
    def _app_conflict_message(self):
        """Warning when the active accounts hold more than one app, or False.

        The accounts are read under sudo across every company, but only the
        number of apps comes out, never an App ID.
        """
        count = len(self._active_app_ids())
        if count < 2:
            return False
        return _(
            "This database has active Meta accounts of %(count)s "
            "different Meta apps. Only one Meta app per database is supported, "  # noqa: E501
            "so reconnecting and unarchiving accounts are refused until the "
            "accounts of all but one app are archived. "
            "See \"One Meta app per database\" in the setup guide.",
            count=count)

    @api.model
    def _check_app_id_allowed(self, app_id):
        """Refuse ``app_id`` when an active account holds another app."""
        app_id = self._normalize_app_id(app_id)
        if not app_id:
            return
        self._lock_single_app()
        others = [app for app in self._active_app_ids() if app != app_id]
        if others:
            raise self._other_app_error(others[0], app_id)

    # Refuses a write that would leave active accounts on two apps. self holds
    # the records whose (active, app_id) changes and projected maps their ids
    # to the new pair. Runs before the write, with the lock already held. An
    # empty self returns at once, so a database that already holds two apps
    # can still save writes that change nothing about apps.
    def _check_single_app(self, projected):
        if not self:
            return
        records = self.sudo()
        for rec in records:
            new_active, new_app = projected[rec.id]
            old_app = self._normalize_app_id(rec.app_id)
            # An active account keeps its app. Clearing the App ID is
            # neutral, like an account that never had one.
            if (rec.active and new_active and old_app and new_app
                    and new_app != old_app):
                raise self._other_app_error(old_app, new_app)
        incoming = records.filtered(
            lambda r: projected[r.id][0] and projected[r.id][1])
        # Archiving or clearing never adds an app, so it is always allowed.
        if not incoming:
            return
        others = self._active_app_ids(exclude_ids=records.ids)
        for rec in incoming:
            new_app = projected[rec.id][1]
            other_apps = [app for app in others if app != new_app]
            other_incoming = [projected[r.id][1] for r in incoming
                              if projected[r.id][1] != new_app]
            if not other_apps and not other_incoming:
                continue
            current = (other_apps or other_incoming)[0]
            if not rec.active and new_app == self._normalize_app_id(rec.app_id):
                raise self._reactivation_error(new_app, current)
            raise self._other_app_error(current, new_app)

    @api.model
    def _check_new_accounts_apps(self, vals_list):
        """Refuse a create that would leave active accounts on two apps.

        Context defaults are part of what gets created, so they are merged
        in before the check.
        """
        defaults = self.default_get(['active', 'app_id'])
        apps = []
        for vals in vals_list:
            merged = dict(defaults, **vals)
            app = self._normalize_app_id(merged.get('app_id'))
            if merged.get('active', True) and app and app not in apps:
                apps.append(app)
        if not apps:
            return
        if len(apps) > 1:
            raise self._other_app_error(apps[0], apps[1])
        self._check_app_id_allowed(apps[0])

    def _secret_targets(self, vals):
        """Where a write of ``vals`` on self puts a new App Secret.

        Returns (targets, active_apps): a (secret, App ID) pair per record,
        and the App IDs of the records that stay or become active.
        """
        secret = vals.get('app_secret')
        targets, active_apps = [], []
        for rec in self.sudo():
            app = self._normalize_app_id(
                vals['app_id'] if 'app_id' in vals else rec.app_id)
            if secret:
                targets.append((secret, app))
            if vals.get('active', rec.active):
                active_apps.append(app)
        return targets, active_apps

    @api.model
    def _new_secret_targets(self, vals_list):
        """Same as _secret_targets for the accounts a create would add.

        Context defaults are merged in first.
        """
        defaults = self.default_get(['active', 'app_id', 'app_secret'])
        targets, active_apps = [], []
        for vals in vals_list:
            merged = dict(defaults, **vals)
            app = self._normalize_app_id(merged.get('app_id'))
            if merged.get('app_secret'):
                targets.append((merged['app_secret'], app))
            if merged.get('active', True):
                active_apps.append(app)
        return targets, active_apps

    def _check_secret_target(self, targets, active_apps):
        """Refuse a new App Secret on an account of another app.

        The secret is copied to every account, so one saved on an archived
        account of an old app, or on one app of a database that still holds
        two, would replace the secret every webhook is checked with.
        ``targets`` holds a (secret, App ID) pair per account the secret is
        written on, ``active_apps`` the App IDs of the accounts the same
        write leaves active. Accounts without an App ID, and a database
        with no active account, are not refused.
        """
        current = self._active_app_ids(exclude_ids=self.ids)
        for app in active_apps:
            if app and app not in current:
                current.append(app)
        for secret, app in targets:
            if not app or not current or current == [app]:
                continue
            # A secret that an account of the current app, or one without an
            # App ID, already holds is the one the database uses, so copying
            # it changes nothing. This is also what lets the copy made by
            # _propagate_app_secret reach the other apps' accounts.
            holders = self.sudo().with_context(active_test=False).search(
                [('app_secret', '=', secret), ('id', 'not in', self.ids)])
            holder_apps = [self._normalize_app_id(holder.app_id)
                           for holder in holders]
            if any(not held or current == [held] for held in holder_apps):
                continue
            if len(current) > 1:
                raise UserError(_(
                    "This database still has active accounts of more than one Meta app, so the App Secret, which is saved on every account, can't be changed. Nothing was saved. Archive the accounts of all but one app first (see \"One Meta app per database\" in the setup guide)."))  # noqa: E501
            raise UserError(_(
                "This account belongs to Meta app %(app)s, but this database is connected to Meta app %(current)s. The App Secret is saved on every account, so it can't be changed from this account. Nothing was saved. To use this account again, run Connect to Meta for it with app %(current)s.",  # noqa: E501
                app=app, current=current[0]))

    @api.model
    def _accounts_needing_secret(self, secret):
        # '!=' also matches accounts with no secret at all.
        return self.sudo().with_context(active_test=False).search(
            [('app_secret', '!=', secret)])

    def _propagate_app_secret(self, secret):
        """Save ``secret`` on every other account; return how many changed.

        No flag is needed to stop the nested write from looping: by the time
        it runs every account already holds the secret, so its own search
        comes back empty and it writes nothing.
        """
        if not secret:
            return 0
        others = self._accounts_needing_secret(secret) - self.sudo()
        if not others:
            return 0
        others.write({'app_secret': secret})
        # Who changed it, never what it is.
        _logger.info(
            "meta_lead_ads: App Secret saved on %s other account(s) by user %s",
            len(others), self.env.uid)
        return len(others)

    def unlink(self):
        # Pages are deleted with their account. One that sends leads to
        # another company would take that company's forms and mappings
        # along, so refuse for every caller and point to archiving.
        pages = self.env['meta.page'].sudo().with_context(
            active_test=False).search([('account_id', 'in', self.ids)])
        for account in self.sudo():
            if any(page.account_id == account
                   and page.lead_company_id != account.company_id
                   for page in pages):
                raise UserError(_(
                    "This Meta account has Pages that send leads to another "
                    "company. Archive the account instead, or move those "
                    "Pages first."))
        # The Pages' forms and mappings go in the same cascade, so the
        # fields they filled are released here.
        forms = self.env['meta.lead.form'].sudo().with_context(
            active_test=False).search([('page_id', 'in', pages.ids)])
        mapped = forms._meta_mapped_crm_fields()
        res = super().unlink()
        self.env['meta.field.mapping']._meta_release_unused_fields(mapped)
        return res

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
        return it to a client. The database holds one Meta app and a new
        secret is saved on every account, so the lowest-id account that has
        a secret holds the database's secret (explicit id order, since the
        default order is by name). Accounts without a secret are skipped,
        otherwise an empty one would make every webhook fail with 403.
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

        Only admins who can open this account are told. Skipped if an open
        To-Do with the same summary exists. Odoo deletes activities when
        they're marked done, so a closed one doesn't block the next alert.
        An open To-Do whose assignee lost access (the account moved company)
        is handed to a current reader, or removed if there is none. The note
        and email mention only the account name.
        """
        self.ensure_one()
        Ingest = self.env['meta.lead.ingest']
        # Odoo 19 renamed res.groups.users to user_ids (direct members).
        # all_user_ids would also pull in users of implying groups.
        admins = self.env.ref('meta_lead_ads.group_meta_admin').user_ids
        # The To-Do and the email carry the account name, so only admins who
        # can open this account get them.
        readers = admins.filtered(lambda u: Ingest._user_can_read(self, u))
        # The cron user is only a fallback if it could open the account too.
        fallback = self.env.user if Ingest._user_can_read(self, self.env.user) \
            else self.env['res.users']
        assignee = readers[:1] or fallback

        open_alerts = self.env['mail.activity'].search([
            ('res_model', '=', 'meta.account'),
            ('res_id', '=', self.id),
            ('summary', '=', _TOKEN_DEAD_SUMMARY),
        ])
        reassigned = False
        for activity in open_alerts:
            owner = activity.user_id
            # The superuser always passes the read check, so a To-Do parked
            # on it would never move to a Meta Admin who joins later.
            parked = bool(readers) and owner not in readers and (
                owner._is_superuser() or owner == self.env.user)
            if Ingest._user_can_read(self, owner) and not parked:
                continue
            if assignee:
                activity.user_id = assignee.id
                reassigned = True
            else:
                activity.unlink()
                _logger.warning(
                    "meta_lead_ads: removed token alert for account id=%s; "
                    "no user can read it", self.id)
        if reassigned:
            # The earlier email went to people who can no longer open this
            # account, so its current readers haven't been told yet. The
            # reassigned To-Do then blocks repeats as before.
            self._send_token_dead_mail(readers)
            return
        if open_alerts.exists():
            return
        if not assignee:
            _logger.warning(
                "meta_lead_ads: token alert for account id=%s not scheduled; "
                "no user can read it", self.id)
            return
        self.activity_schedule(
            'mail.mail_activity_data_todo',
            summary=_TOKEN_DEAD_SUMMARY,
            note=_('Re-run the Connect to Meta wizard with a current '
                   'System User token.'),
            user_id=assignee.id,
        )
        self._send_token_dead_mail(readers)

    def _send_token_dead_mail(self, readers):
        """Email ``readers`` that this account's token is dead."""
        self.ensure_one()
        # Skip readers with no email; don't send with an empty email_to.
        recipients = [e for e in readers.mapped('email') if e]
        if not recipients:
            return
        vals = {
            'subject': _('Meta Lead Ads: access token invalid for %s')
            % self.name,
            # The account name comes from Meta; Markup's % escapes it.
            'body_html': Markup(_('<p>The Meta access token for account %s '
                                  'is invalid or expired. Lead ingestion is '
                                  'paused until it is renewed.</p>'))
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
    def _stamp_cron_run(self, key, when=None):
        """Record ``when`` (default now) under ``key``, one of the health
        stamps in const."""
        self.env['ir.config_parameter'].sudo().set_param(
            key, fields.Datetime.to_string(when or fields.Datetime.now()))

    @api.model
    def _scheduler_health(self):
        """Return {'status', 'message'}: the worse of the queue drain and the
        backfill, with the banner text. See _meta_cron_health."""
        health = self._meta_cron_health(webhook=False)
        return {'status': health['status'], 'message': health['message']}

    @api.model
    def _meta_cron_health(self, webhook=True):
        """Judge the queue drain, the backfill and the webhook, each on its
        own evidence.

        Returns a plain dict:

        - ``now``: the time the check was made (naive UTC);
        - ``drain``: ``status`` (ok, pending, backlog, stalled, disabled),
          ``last_run``, ``backlog_count`` (pending webhook events older than
          ``backlog_minutes`` that the drain would take now, so not those
          held back by a Meta rate limit) and ``backlog_minutes`` (the
          threshold);
        - ``backfill``: ``status`` (ok, pending, stalled, disabled) and
          ``last_run``;
        - ``webhook``, only when ``webhook`` is true: ``status`` (failing,
          backlog, running, quiet, no_activity), ``last_ok``,
          ``failed_count`` (failed webhook rows recorded since the start of
          the last drain run that finished an event, or those of the last
          WEBHOOK_FAIL_LOOKBACK_DAYS days when there was none),
          ``backlog_count`` and
          ``backlog_minutes``;
        - ``status``: the worse of drain and backfill;
        - ``message``: the account form banner, naming the job at fault.

        ``last_run`` and ``last_ok`` are naive UTC datetimes or False. The
        account form and the dashboard both read this, so they can't
        disagree. Only stored state is read, so it works while the scheduler
        is down. Health is database-wide: counts are read under sudo, and
        the message holds counts and ages only.
        """
        ICP = self.env['ir.config_parameter'].sudo()
        now = fields.Datetime.now()

        def stamp(key):
            # A hand-edited value must not break the account form.
            try:
                return fields.Datetime.to_datetime(ICP.get_param(key)) or False
            except (TypeError, ValueError):
                return False

        installed_at = stamp(INSTALLED_AT_PARAM)
        # sudo: only Settings admins can read ir.cron, but Meta Admins open
        # the account form too. Nothing but active, nextcall and the
        # interval is read here.
        env = self.sudo().env
        drain_cron = env.ref('meta_lead_ads.cron_meta_webhook_drain',
                             raise_if_not_found=False)
        backfill_cron = env.ref('meta_lead_ads.cron_meta_backfill',
                                raise_if_not_found=False)
        # Aged by create_date: every drain attempt touches the row, so its
        # last change says nothing about how long it has waited. A lead put
        # back after its Page was reactivated is aged from that moment
        # instead: it may have arrived weeks ago, but it has only waited in
        # line since then.
        # Events the drain holds back on purpose (a retry time after a rate
        # limit, a paused Page, the app-wide pause) are waiting on Meta, not
        # stuck, and the drain's own select skips them too.
        Event = self.env['meta.webhook.event'].sudo()
        app_paused = Event._app_lead_fetch_paused_until()
        if app_paused and app_paused > now:
            backlog_count = 0
        else:
            cutoff = now - timedelta(minutes=const.QUEUE_BACKLOG_MINUTES)
            backlog_domain = [
                ('status', '=', 'pending'),
                '|', '&', ('requeued_at', '=', False),
                ('create_date', '<', cutoff),
                ('requeued_at', '<', cutoff),
                '|', ('next_attempt_at', '=', False),
                ('next_attempt_at', '<=', now)]
            paused_pages = self.env['meta.page'].sudo().with_context(
                active_test=False).search(
                    [('lead_fetch_paused_until', '>', now)]).mapped('page_id')
            if paused_pages:
                backlog_domain.append(('page_id', 'not in', paused_pages))
            backlog_count = Event.search_count(backlog_domain)

        # Queue drain. It runs every minute, so 15 minutes without a run
        # means the scheduler stopped, not that the server was busy.
        stale = timedelta(minutes=SCHEDULER_STALE_MINUTES)
        drain_last = stamp(const.DRAIN_RUN_PARAM)
        if not drain_cron or not drain_cron.active:
            drain_status = 'disabled'
        elif drain_last and now - drain_last <= stale:
            drain_status = 'backlog' if backlog_count else 'ok'
        # A running scheduler keeps nextcall close to now; one stuck in the
        # past means the worker is idle.
        elif drain_cron.nextcall and now - drain_cron.nextcall > stale:
            drain_status = 'stalled'
        # Just installed: give cron a few minutes before calling it stalled.
        elif installed_at and now - installed_at <= stale:
            drain_status = 'pending'
        else:
            drain_status = 'stalled'

        # Backfill, judged against its own interval, which an admin may have
        # slowed down. A fresh install's first run is only due after one
        # interval, so the install grace is the same window.
        backfill_last = stamp(const.BACKFILL_RUN_PARAM)
        if not backfill_cron or not backfill_cron.active:
            backfill_status = 'disabled'
        else:
            cutoff = now - self._backfill_stale_window(backfill_cron)
            if backfill_last and backfill_last >= cutoff:
                backfill_status = 'ok'
            elif backfill_cron.nextcall and backfill_cron.nextcall < cutoff:
                backfill_status = 'stalled'
            elif installed_at and installed_at >= cutoff:
                backfill_status = 'pending'
            else:
                backfill_status = 'stalled'

        if _CRON_SEVERITY[drain_status] >= _CRON_SEVERITY[backfill_status]:
            worst = drain_status
        else:
            worst = backfill_status
        result = {
            'now': now,
            'drain': {
                'status': drain_status, 'last_run': drain_last,
                'backlog_count': backlog_count,
                'backlog_minutes': const.QUEUE_BACKLOG_MINUTES,
            },
            'backfill': {'status': backfill_status,
                         'last_run': backfill_last},
            'status': worst,
            'message': self._cron_health_message(
                now, drain_status, drain_last, backfill_status,
                backfill_last, backlog_count),
        }
        if webhook:
            result['webhook'] = self._webhook_health(
                now, stamp(const.WEBHOOK_OK_PARAM), backlog_count)
        return result

    @api.model
    def _backfill_stale_window(self, cron):
        """How long the backfill may go without a run: a few of its own
        intervals."""
        unit = _CRON_INTERVAL_UNITS.get(cron.interval_type)
        if not unit:
            return timedelta(minutes=15 * const.BACKFILL_STALE_FACTOR)
        name, size = unit
        count = max(cron.interval_number, 1) * const.BACKFILL_STALE_FACTOR
        return relativedelta(**{name: count * size})

    @api.model
    def _webhook_health(self, now, last_ok, backlog_count):
        """Webhook signal for _meta_cron_health. Running needs a recent
        success; failures newer than the last success turn it to failing,
        and an old success with no such evidence is only quiet."""
        # A lead held because its Page is archived waits on a setting, not
        # on a webhook failure. A row with no Page still counts.
        domain = [('status', '=', 'failed'), ('trigger', '=', 'webhook'),
                  '|', ('page_id', '=', False), ('page_id.active', '=', True)]
        # failed_at moves when a webhook failure is recorded, and the success
        # stamp is the start of the run that finished an event, so a failure
        # in that run still counts. The cleanup doesn't move failed_at.
        if last_ok:
            domain.append(('failed_at', '>=', last_ok))
        else:
            # An old failure with no success since shouldn't keep the pill
            # red for good. It still shows on the Sync Log.
            domain.append(('failed_at', '>=', now - timedelta(
                days=const.WEBHOOK_FAIL_LOOKBACK_DAYS)))
        failed_count = self.env['meta.sync.log'].sudo().search_count(domain)
        if failed_count:
            status = 'failing'
        elif backlog_count:
            status = 'backlog'
        elif last_ok and now - last_ok <= timedelta(
                hours=const.WEBHOOK_FRESH_HOURS):
            status = 'running'
        elif last_ok:
            status = 'quiet'
        else:
            status = 'no_activity'
        return {
            'status': status, 'last_ok': last_ok,
            'failed_count': failed_count, 'backlog_count': backlog_count,
            'backlog_minutes': const.QUEUE_BACKLOG_MINUTES,
        }

    @api.model
    def _cron_health_message(self, now, drain_status, drain_last,
                             backfill_status, backfill_last, backlog_count):
        """Banner text. It names the job at fault first and says what the
        other job is doing, from that job's own status, so it agrees with
        the dashboard pills. The fix hint shows when both jobs are stalled,
        or when the drain is stalled and neither job has ever run, which is
        a fresh install with no scheduler."""
        if drain_status == 'ok' and backfill_status == 'ok':
            return _("Scheduler healthy. The queue drain last ran "
                     "%(drain_ago)s and the backfill last ran "
                     "%(backfill_ago)s.",
                     drain_ago=self._humanize_ago(now - drain_last),
                     backfill_ago=self._humanize_ago(now - backfill_last))
        if drain_status == 'stalled' and (
                backfill_status == 'stalled'
                or (not drain_last and not backfill_last)):
            return self._scheduler_fix_hint()
        # A stalled drain leads even next to a disabled backfill: it holds
        # every webhook lead, so it must not be hidden.
        if drain_status == 'stalled' or (
                _CRON_SEVERITY[drain_status]
                >= _CRON_SEVERITY[backfill_status]):
            job, status = 'drain', drain_status
        else:
            job, status = 'backfill', backfill_status
        if status == 'disabled' and job == 'drain':
            message = _("The Meta webhook-drain scheduled action is inactive, "
                        "so leads will not sync automatically. Re-enable it "
                        "under Settings > Technical > Scheduled Actions.")
            if backfill_status == 'stalled':
                message = "%s %s" % (
                    message, _("The backfill is not running either."))
            return message
        if status == 'disabled':
            return _("The Meta backfill scheduled action is inactive, so "
                     "leads a missed webhook didn't deliver are not picked "
                     "up. Re-enable it under Settings > Technical > "
                     "Scheduled Actions.")
        if status == 'stalled' and job == 'drain':
            other = self._backfill_clause(backfill_status)
            if drain_last:
                return _("The queue drain last ran %(ago)s; %(other)s. "
                         "Webhook leads wait in the queue until the drain "
                         "runs again.",
                         ago=self._humanize_ago(now - drain_last),
                         other=other)
            return _("The queue drain hasn't run yet; %(other)s. Webhook "
                     "leads wait in the queue until the drain runs.",
                     other=other)
        if status == 'stalled':
            other = self._drain_clause(drain_status, backlog_count)
            if backfill_last:
                return _("The backfill last ran %(ago)s; %(other)s. Leads a "
                         "missed webhook didn't deliver are only picked up "
                         "when the backfill runs again.",
                         ago=self._humanize_ago(now - backfill_last),
                         other=other)
            return _("The backfill hasn't run yet; %(other)s. Leads a missed "
                     "webhook didn't deliver are only picked up when the "
                     "backfill runs.",
                     other=other)
        if status == 'backlog':
            return _("The queue drain is running, but %(count)s webhook "
                     "events have waited more than %(minutes)s minutes. "
                     "Check the Sync Log for errors.",
                     count=backlog_count,
                     minutes=const.QUEUE_BACKLOG_MINUTES)
        return _("Waiting for the first scheduled run to confirm the "
                 "scheduler is active (give it a few minutes, then "
                 "refresh).")

    @api.model
    def _backfill_clause(self, status):
        """What the backfill is doing, for a message led by the drain."""
        return {
            'ok': _("the backfill is running"),
            'pending': _("the backfill hasn't run yet"),
            'stalled': _("the backfill is not running either"),
            'disabled': _("the backfill is disabled"),
        }[status]

    @api.model
    def _drain_clause(self, status, backlog_count):
        """What the queue drain is doing, for a message led by the
        backfill."""
        if status == 'backlog':
            return _("the queue drain is running, but %(count)s webhook "
                     "events have waited more than %(minutes)s minutes",
                     count=backlog_count,
                     minutes=const.QUEUE_BACKLOG_MINUTES)
        return {
            'ok': _("the queue drain is running"),
            'pending': _("the queue drain hasn't run yet"),
            'stalled': _("the queue drain is not running either"),
            'disabled': _("the queue drain is disabled"),
        }[status]

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
