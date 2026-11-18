# Meta Lead Ads Integration for Odoo — © 2026 CoreSys Builders. All rights reserved.
# Licensed under the Odoo Proprietary License v1.0 (OPL-1).
# Unauthorized copying, redistribution, or resale of this software, in whole or in
# part, via any medium, is strictly prohibited and constitutes a license violation.
# OPL-1: https://www.odoo.com/documentation/19.0/legal/licenses.html#odoo-apps

# Step-by-step wizard for connecting Odoo to Meta: validate the token, pick
# Pages, review forms, then save. All Graph calls go through meta.graph.client
# and token status mapping lives on meta.account.
import secrets

from odoo import api, fields, models, _
from odoo.exceptions import UserError, ValidationError

from odoo.addons.meta_lead_ads.models.exceptions import (
    MetaAuthError, MetaGraphError, MetaPermanentError, MetaRateLimitError,
    MetaTransientError,
)


class MetaOnboardingPage(models.TransientModel):
    _name = 'meta.onboarding.page'
    _description = 'Meta Onboarding Page Line'

    wizard_id = fields.Many2one('meta.onboarding', required=True,
                                ondelete='cascade')
    selected = fields.Boolean(default=True)
    page_id = fields.Char(readonly=True)
    name = fields.Char(readonly=True)
    access_token = fields.Char(groups='meta_lead_ads.group_meta_admin')
    # Only drives the badge and the default tick. Finish looks the Page up
    # again, since anything on a line can be written over RPC.
    archived = fields.Boolean(readonly=True)
    # Set when another Meta account in Odoo already holds this Page. Finish
    # refuses such a Page, so the line can't be ticked.
    other_account = fields.Boolean(readonly=True)
    line_status = fields.Selection([('archived', 'Archived'),
                                    ('other_account', 'Another account')],
                                   string='Status',
                                   compute='_compute_line_status')

    @api.depends('archived', 'other_account')
    def _compute_line_status(self):
        for rec in self:
            rec.line_status = ('other_account' if rec.other_account
                               else 'archived' if rec.archived else False)


class MetaOnboardingForm(models.TransientModel):
    _name = 'meta.onboarding.form'
    _description = 'Meta Onboarding Form Line'

    # The forms the admin reviewed. They pick what Finish creates, updates or
    # brings back; Finish still asks Meta again before archiving anything.
    wizard_id = fields.Many2one('meta.onboarding', required=True,
                                ondelete='cascade')
    page_line_id = fields.Many2one('meta.onboarding.page', ondelete='cascade')
    page_id = fields.Char(readonly=True)        # Meta page id
    form_id = fields.Char(readonly=True)
    name = fields.Char(readonly=True)
    # Ticking only matters for a form archived in Odoo. An active or new
    # form is always kept.
    selected = fields.Boolean(string='Include', default=True)
    archived = fields.Boolean(readonly=True)
    line_status = fields.Selection([('archived', 'Archived')],
                                   string='Status',
                                   compute='_compute_line_status')

    @api.depends('archived')
    def _compute_line_status(self):
        for rec in self:
            rec.line_status = 'archived' if rec.archived else False


class MetaOnboarding(models.TransientModel):
    _name = 'meta.onboarding'
    _description = 'Meta Onboarding Wizard'

    state = fields.Selection(
        [('credentials', 'Credentials'), ('pages', 'Select Pages'),
         ('forms', 'Review Forms'), ('done', 'Done')],
        default='credentials')
    # App ID is public; the App Secret and token are admin-only.
    app_id = fields.Char(
        help="One Meta app per database. While accounts of another app are "
             "active, a different App ID is refused.")
    app_secret = fields.Char(groups='meta_lead_ads.group_meta_admin')
    access_token = fields.Char(string='Token',
                               groups='meta_lead_ads.group_meta_admin')
    exchange_short_lived = fields.Boolean(
        string='This is a short-lived User token (exchange it)')
    page_line_ids = fields.One2many('meta.onboarding.page', 'wizard_id')
    form_line_ids = fields.One2many('meta.onboarding.form', 'wizard_id')
    has_archived_pages = fields.Boolean(compute='_compute_has_archived_pages')
    has_other_account_pages = fields.Boolean(
        compute='_compute_has_archived_pages')
    account_id = fields.Many2one('meta.account')
    company_id = fields.Many2one(
        'res.company', string='Company', required=True,
        default=lambda self: self.env.company,
        help="Company that receives leads from this connection. Pages can "
             "override it later.")
    token_owner_id = fields.Char(readonly=True)   # meta.account is keyed on this
    # Display copies of the token status; same selection as meta.account.
    # Granted permission is not the same as production-ready: App Review
    # still applies.
    token_valid = fields.Boolean(readonly=True)
    access_status = fields.Selection(
        [('auth_failed', 'Authentication failed'),
         ('dev_test_only', 'Development mode — test leads only'),
         ('lead_retrieval_granted',
          'leads_retrieval granted (App Review still gates production)')],
        readonly=True)
    leads_retrieval_granted = fields.Boolean(readonly=True)
    # Filled by Finish. Plain text, one line per change, so the Done step
    # never renders markup and never shows a token or secret.
    onboarding_summary = fields.Text(readonly=True)
    is_update = fields.Boolean(readonly=True)

    # ------------------------------------------------------------------
    # Per-page webhook subscription + verify_token/URL surface
    # ------------------------------------------------------------------
    # Target of the subscribe/unsubscribe buttons. The wizard is admin-only,
    # which is what keeps the verify token below away from regular users.
    page_id = fields.Many2one('meta.page', string='Page')
    # Filled by _refresh_subscription_status. Empty means the Page changed
    # and hasn't been checked yet.
    subscription_state = fields.Selection(
        [('subscribed', 'Subscribed'), ('not_subscribed', 'Not subscribed'),
         ('other_app', 'Another app subscribed'),
         ('check_failed', "Couldn't check")],
        readonly=True)
    subscription_hint = fields.Char(readonly=True)
    # Shown after commit so the admin can paste them into Meta's webhook
    # settings. The verify token is a shared secret; don't expose it to
    # non-admins through a related or computed field elsewhere.
    webhook_url = fields.Char(readonly=True)
    webhook_verify_token = fields.Char(string='Verify Token', readonly=True)

    @api.depends('page_line_ids.archived', 'page_line_ids.other_account')
    def _compute_has_archived_pages(self):
        for rec in self:
            rec.has_archived_pages = any(rec.page_line_ids.mapped('archived'))
            rec.has_other_account_pages = any(
                rec.page_line_ids.mapped('other_account'))

    # The form shows the linked Page and account by name, so an id typed in
    # over RPC must point at a record the admin can already read.
    @api.model_create_multi
    def create(self, vals_list):
        records = super().create(vals_list)
        if not self.env.su:
            records._check_link_access()
        return records

    def write(self, vals):
        res = super().write(vals)
        if not self.env.su and ('page_id' in vals or 'account_id' in vals):
            self._check_link_access()
        return res

    def _check_link_access(self):
        Page = self.env['meta.page']
        Account = self.env['meta.account']
        for rec in self.sudo():
            if ((rec.page_id
                 and not Page.browse(rec.page_id.id).has_access('read'))
                    or (rec.account_id and not Account.browse(
                        rec.account_id.id).has_access('read'))):
                raise ValidationError(_(
                    "You can only pick a Page or account you can access."))

    # ------------------------------------------------------------------
    # helpers
    # ------------------------------------------------------------------
    def _ensure_verify_token(self):
        """Return the webhook verify token, generating it on first use."""
        ICP = self.env['ir.config_parameter'].sudo()
        key = 'meta_lead_ads.webhook_verify_token'
        tok = ICP.get_param(key)
        if not tok:
            tok = secrets.token_urlsafe(32)
            ICP.set_param(key, tok)
        return tok

    def _webhook_url(self):
        """Return the public webhook URL built from web.base.url."""
        base = self.env['ir.config_parameter'].sudo().get_param(
            'web.base.url') or ''
        return '%s%s' % (base, '/meta_lead_ads/webhook')

    def _not_checked_hint(self):
        return _("Press Refresh status to check this Page.")

    def _no_page_token_hint(self):
        return _("This Page has no access token in Odoo, so Meta can't be "
                 "asked about it. Run Connect to Meta again and include "
                 "this Page.")

    def _refresh_subscription_status(self):
        """Ask Meta whether this app gets the Page's leads.

        Only a subscribed_apps row whose id is this account's App ID counts,
        and only when it lists leadgen. Another app's subscription doesn't
        bring leads here. Never raises: a failed or odd answer shows
        "Couldn't check" so Finish and the buttons still go through.
        """
        self.ensure_one()
        if not self.page_id:
            self.write({'subscription_state': False,
                        'subscription_hint': self._not_checked_hint()})
            return
        failed = {
            'subscription_state': 'check_failed',
            'subscription_hint': _(
                "Meta didn't answer the status check. The connection is "
                "saved. Press Refresh status to try again."),
        }
        app_id = (self.page_id.sudo().account_id.app_id or '').strip()
        if not self.page_id.sudo().access_token:
            # Meta leaves the token out for some Pages, and the Graph call
            # can't be made without one.
            self.write({'subscription_state': 'check_failed',
                        'subscription_hint': self._no_page_token_hint()})
            return
        try:
            data = self.env['meta.graph.client'].page_subscription_status(
                self.page_id)
        except MetaGraphError:
            # The message may carry Meta's text, so it is never shown.
            self.write(failed)
            return
        # A body we can't read must not raise past here and undo Finish.
        rows = data.get('data') if isinstance(data, dict) else ()
        if rows is None:
            rows = []
        if not isinstance(rows, list) or not all(
                isinstance(row, dict)
                and (row.get('subscribed_fields') is None
                     or isinstance(row.get('subscribed_fields'), list))
                for row in rows):
            self.write(failed)
            return
        # Without an App ID nothing can be matched, not even a row with no id.
        mine = [row for row in rows
                if app_id and str(row.get('id') or '') == app_id]

        def has_leadgen(row):
            return 'leadgen' in (row.get('subscribed_fields') or [])

        if any(has_leadgen(row) for row in mine):
            state, hint = 'subscribed', False
        elif mine:
            state = 'not_subscribed'
            hint = _("This app is subscribed to the Page but not to leadgen, "
                     "so no leads arrive. Press Subscribe to add it.")
        elif any(has_leadgen(row) for row in rows):
            state = 'other_app'
            hint = _("Another Meta app gets this Page's leads, but app "
                     "%(app_id)s doesn't. Press Subscribe to add this app. "
                     "The other app keeps its subscription.", app_id=app_id)
        else:
            state = 'not_subscribed'
            hint = _("This app isn't subscribed to the Page's leads yet. "
                     "Press Subscribe.")
        self.write({'subscription_state': state, 'subscription_hint': hint})

    def action_refresh_subscription(self):
        """Read the Page's subscription again; never raises."""
        self.ensure_one()
        self._refresh_subscription_status()
        return self._reload()

    @api.onchange('page_id')
    def _onchange_page_id_subscription(self):
        # The old status belongs to the previous Page. Asking Meta here
        # would cost a call per pick, so wait for Refresh status.
        self.subscription_state = False
        self.subscription_hint = self._not_checked_hint()

    @api.model
    def _map_token_status(self, data):
        """Map a debug_token response to status values (see meta.account)."""
        return self.env['meta.account']._map_token_status(data)

    def _check_token_app(self, data):
        """Refuse a token that Meta says belongs to another app.

        Returns the App ID to store: Meta's when it sent one, else the typed
        one (debug_token doesn't always return app_id).
        """
        Account = self.env['meta.account']
        typed = Account._normalize_app_id(self.app_id)
        token_app = Account._normalize_app_id((data or {}).get('app_id'))
        if token_app and token_app != typed:
            raise UserError(_(
                "Meta reports this token belongs to app %(token_app)s, not app %(app)s. Check the App ID and try again.",  # noqa: E501
                token_app=token_app, app=typed))
        return token_app or typed

    def _onboarding_summary(self, changes):
        """Plain text lines for the Done step, one per change.

        Only fixed sentences, counts, App IDs and a company name the admin
        already passed the company checks for. Never a token, a secret or
        Meta's own text.
        """
        if not changes.get('existing'):
            return False
        lines = []
        if changes.get('reactivated'):
            lines.append(_("Archived connection reactivated."))
        if changes.get('app_changed'):
            old, new = changes['app_changed']
            lines.append(_("Meta app changed from %(old)s to %(new)s.",
                           old=old, new=new))
        if changes.get('token_replaced'):
            lines.append(_("Token replaced."))
        count = changes.get('secret_count') or 0
        if count == 1:
            lines.append(_("App Secret updated on 1 account."))
        elif count:
            lines.append(_("App Secret updated on %(count)s accounts.",
                           count=count))
        count = changes.get('pages_reactivated') or 0
        if count == 1:
            lines.append(_("1 archived Page reactivated."))
        elif count:
            lines.append(_("%(count)s archived Pages reactivated.",
                           count=count))
        count = changes.get('forms_reactivated') or 0
        if count == 1:
            lines.append(_("1 archived lead form reactivated."))
        elif count:
            lines.append(_("%(count)s archived lead forms reactivated.",
                           count=count))
        if changes.get('company'):
            lines.append(_(
                "Company stays %(company)s. Change it on the account if leads should go to another company.",  # noqa: E501
                company=changes['company']))
        if not lines:
            # Status, scopes and the name may still have been refreshed,
            # so don't claim that nothing changed.
            lines.append(_(
                "Token and App Secret unchanged. Token status checked again."))
        return '\n'.join(lines)

    @api.model
    def _archived_ids(self, model, key, meta_ids):
        """Return the Meta ids among ``meta_ids`` whose record is archived.

        sudo, because the Page or form may send leads to a company the admin
        can't see. Only the ids go back, never anything read from the record.
        """
        meta_ids = [i for i in meta_ids if i]
        if not meta_ids:
            return set()
        found = self.env[model].sudo().with_context(active_test=False).search(
            [(key, 'in', meta_ids), ('active', '=', False)])
        return set(found.mapped(key))

    @api.model
    def _page_states(self, owner, page_ids):
        """Return (archived, elsewhere) for the Meta Page ids ``page_ids``.

        ``archived`` are Pages archived under the account of the token owner
        ``owner``, ``elsewhere`` Pages that another account holds, active or
        not. Finish refuses those, so they must not be offered as Pages to
        bring back. sudo for the same reason as _archived_ids; only Meta ids
        come back.
        """
        page_ids = [i for i in page_ids if i]
        if not page_ids:
            return set(), set()
        account = self.env['meta.account'].sudo().with_context(
            active_test=False).search([('account_id', '=', owner)], limit=1) \
            if owner else self.env['meta.account']
        found = self.env['meta.page'].sudo().with_context(
            active_test=False).search([('page_id', 'in', page_ids)])
        archived = {page.page_id for page in found
                    if page.account_id == account and not page.active}
        elsewhere = {page.page_id for page in found
                     if page.account_id != account}
        return archived, elsewhere

    def _reload(self):
        """Reopen this wizard record so the next step is shown."""
        self.ensure_one()
        return {'type': 'ir.actions.act_window', 'res_model': self._name,
                'res_id': self.id, 'view_mode': 'form', 'target': 'new'}

    # ------------------------------------------------------------------
    # identity + reconcile
    # ------------------------------------------------------------------
    @api.model
    def _get_or_create_account(self, data, changes=None):
        """Find or create the meta.account for the token's owner.

        Accounts are keyed on the token owner, not the app, so two owners on
        one app give two accounts. On an existing account only the fields Meta
        owns (name, owner, app_id, status) are refreshed. An archived account
        is reactivated once the company checks pass. ``changes``, when given,
        collects what happened for the Done summary.
        """
        if changes is None:
            changes = {}
        status = self._map_token_status(data)
        owner = status.get('token_owner_id')
        # account_id is required and unique, so without an owner we would
        # either merge unrelated connections or hit an IntegrityError.
        if not owner:
            raise UserError(_("Meta did not return a token owner id for this "
                              "token, so no account can be created. Use a "
                              "System User token (Business Manager)."))
        app_id = (data or {}).get('app_id')
        Account = self.env['meta.account']
        # Look past record rules and the archive filter: account_id is unique,
        # so a row we can't see would otherwise turn into a failed insert.
        account = Account.sudo().with_context(active_test=False).search(
            [('account_id', '=', owner)], limit=1)
        vals = {
            'name': (data or {}).get('application') or owner,
            'account_id': owner,
            'token_owner_id': owner,
            'app_id': app_id,
        }
        vals.update(status)
        user = self.env.user
        if account:
            # Checking the selected companies first turns a record rule
            # failure into a message the admin can act on. Only name the
            # company when the admin belongs to it.
            if not self.env.su and account.company_id not in user.company_ids:
                raise UserError(_(
                    "This Meta connection is already set up in a company you "
                    "don't have access to. Ask an administrator of that "
                    "company to update it."))
            if not self.env.su and account.company_id not in self.env.companies:
                raise UserError(_(
                    "This Meta connection belongs to %s, which is not selected "
                    "in the company switcher. Select it and run the wizard "
                    "again.", account.company_id.display_name))
            # Both company checks passed, so an archived account can come
            # back. Its Pages, forms and mappings stay linked to it.
            if not account.active:
                vals['active'] = True
                changes['reactivated'] = True
            stored_app = Account._normalize_app_id(account.app_id)
            new_app = Account._normalize_app_id(vals.get('app_id'))
            # meta.account refuses this on an active account, so it only
            # gets through for an archived one coming back on the current app.
            if stored_app and new_app and stored_app != new_app:
                changes['app_changed'] = (stored_app, new_app)
            changes['existing'] = True
            # The company stays as it is; only an admin changes it. Naming it
            # is fine here, the admin passed both company checks above.
            if self.company_id and self.company_id != account.company_id:
                changes['company'] = account.company_id.display_name
            account = account.with_env(self.env)
            account.write(vals)
            return account
        company = self.company_id or self.env.company
        if not self.env.su:
            # The wizard company can be set to anything over RPC, so don't
            # name it unless the admin belongs to it.
            if company not in user.company_ids:
                raise UserError(_(
                    "You can't connect Meta to a company you don't belong to."))
            if company not in self.env.companies:
                # sudo: the company rule hides unselected companies, even
                # ones the admin belongs to.
                raise UserError(_(
                    "Select %s in the company switcher before connecting it.",
                    company.sudo().display_name))
        vals['company_id'] = company.id
        return Account.create(vals)

    @api.model
    def _get_or_create_page(self, account, pid, name, access_token, changes=None):  # noqa: E501
        """Find or create the meta.page under this account.

        page_id is globally unique. If another account already owns the page
        we refuse, rather than silently moving it (and its forms) across or
        failing on the unique constraint. An archived Page of this account
        comes back with its routing, sync settings and company override.
        """
        if changes is None:
            changes = {}
        Page = self.env['meta.page']
        # page_id is unique across companies and archived rows, so look for it
        # past both.
        AllPages = Page.sudo().with_context(active_test=False)
        pvals = {'name': name, 'page_id': pid, 'account_id': account.id,
                 'access_token': access_token, 'active': True}
        page = AllPages.search([('page_id', '=', pid),
                                ('account_id', '=', account.id)], limit=1)
        if page:
            if not page.active:
                changes['pages_reactivated'] = (
                    changes.get('pages_reactivated', 0) + 1)
            # Meta leaves the token out for some Pages. Keep the one we have
            # rather than wiping a working token.
            if not access_token:
                pvals.pop('access_token')
            # The account is the admin's own, but its Page may send leads to
            # a company the admin can't see, so refresh it as sudo. Only
            # Meta-owned fields; the company override and manual edits stay.
            page.write(pvals)
            return page.with_env(self.env)
        other = AllPages.search(
            [('page_id', '=', pid), ('account_id', '!=', account.id)], limit=1)
        if other:
            raise UserError(_(
                "This Page is already onboarded under a different Meta "
                "account. Onboard it from that account, or remove it there "
                "first; it will not be moved automatically."))
        return Page.create(pvals)

    @api.model
    def _reconcile_pages(self, account, discovered, selected_ids=None,
                         complete=True):
        """Sync pages with what Meta returned.

        Selected pages are created or updated. Pages Meta no longer returns are
        archived, never deleted, but only when ``complete`` says the listing
        was read to the end. Pages that still exist but weren't selected this
        time are left as they are.
        """
        Page = self.env['meta.page']
        if selected_ids is None:
            selected_ids = {p.get('id') for p in discovered}
        discovered_ids = [p.get('id') for p in discovered]
        for p in discovered:
            pid = p.get('id')
            if pid not in selected_ids:
                continue
            self._get_or_create_page(account, pid, p.get('name'),
                                     p.get('access_token'))
        if not complete:
            # A cut-short listing leaves Pages out that still exist.
            return
        # sudo so Pages of this account that send leads to another company
        # are archived too.
        Page.sudo().search([('account_id', '=', account.id),
                            ('page_id', 'not in', discovered_ids)]).write(
            {'active': False})

    @api.model
    def _reconcile_forms(self, page, discovered_forms, privileged=False,
                         keep_ids=None, reactivate_ids=None, changes=None):
        """Create or update the page's forms; archive the ones not listed.

        New forms get sync_enabled from the field default. An existing form
        keeps whatever sync_enabled the admin set. With privileged=True the
        writes run as sudo; the caller must only pass form ids and names Meta
        itself returned for this Page. ``keep_ids``, when given, replaces
        the listed ids as the set of forms that must not be archived.

        An archived form only comes back when its id is in
        ``reactivate_ids``; otherwise it is left exactly as it is. Coming
        back only sets it active (and its name), so its mappings, sync
        setting and backfill position carry on from where they were.
        """
        if changes is None:
            changes = {}
        Form = self.env['meta.lead.form']
        Writer = Form.sudo() if privileged else Form
        # form_id is unique across companies and archived rows.
        AllForms = Form.sudo().with_context(active_test=False)
        discovered_ids = [f.get('id') for f in discovered_forms]
        if keep_ids is None:
            keep_ids = discovered_ids
        reactivate_ids = set(reactivate_ids or ())
        for f in discovered_forms:
            fid = f.get('id')
            found = AllForms.search([('form_id', '=', fid)], limit=1)
            if found:
                # A Meta form id belongs to one Page, so another parent means
                # stale or crafted input. Moving it would carry that Page's
                # mappings into another company.
                if found.page_id != page:
                    raise UserError(_(
                        "A reviewed lead form is already linked to another "
                        "Page. It was not moved."))
                if not found.active:
                    if fid not in reactivate_ids:
                        continue
                    changes['forms_reactivated'] = (
                        changes.get('forms_reactivated', 0) + 1)
                fvals = {'active': True}
                if f.get('name'):
                    fvals['name'] = f.get('name')
                Writer.browse(found.id).write(fvals)
            else:
                Writer.create({'name': f.get('name') or fid, 'form_id': fid,
                               'page_id': page.id})
        Writer.search([('page_id', '=', page.id),
                       ('form_id', 'not in', list(keep_ids))]).write(
            {'active': False})

    # ------------------------------------------------------------------
    # stepped actions
    # ------------------------------------------------------------------
    def action_validate(self):
        """Check the credentials with Meta and list the Pages the token can see.

        Error messages never include the token or secret.
        """
        self.ensure_one()
        if not (self.app_id and self.app_id.strip()):
            raise UserError(_("App ID is required."))
        if not (self.app_secret and self.app_secret.strip()):
            raise UserError(_("App Secret is required."))
        if not (self.access_token and self.access_token.strip()):
            raise UserError(_("Paste a System User token (or a short-lived "
                              "User token to exchange)."))
        # Refuse a second app before the token goes anywhere near Meta.
        self.env['meta.account']._check_app_id_allowed(self.app_id.strip())
        client = self.env['meta.graph.client']
        token = self.access_token
        try:
            if self.exchange_short_lived:
                token, _exp = client.exchange_token(
                    self.app_id, self.app_secret, self.access_token)
            data = client.debug_token(self.app_id, self.app_secret, token)
        except MetaAuthError:
            raise UserError(_("That token is invalid or expired. Paste a "
                              "current System User token and try again."))
        except (MetaPermanentError, MetaRateLimitError, MetaTransientError):
            raise UserError(_("Could not reach Meta to validate the token. "
                              "Check the App ID / App Secret and try again."))
        status = self._map_token_status(data)
        if not status.get('token_valid'):
            # Meta answers 200 with is_valid=false for a bad token.
            raise UserError(_("Meta reports this token is not valid. Paste a "
                              "current System User token and try again."))
        # Another app's token would list Pages this app can't serve, and
        # its status would be stored on the wizard as if it were ours.
        self._check_token_app(data)
        self.write({
            'access_token': token,           # long-lived if it was exchanged
            'token_owner_id': status.get('token_owner_id'),
            'token_valid': status.get('token_valid'),
            'access_status': status.get('access_status'),
            'leads_retrieval_granted': status.get('leads_retrieval_granted'),
        })
        pages = client.discover_pages(token, app_secret=self.app_secret)
        self.page_line_ids.unlink()
        archived, elsewhere = self._page_states(
            status.get('token_owner_id'), [p.get('id') for p in pages])
        self.env['meta.onboarding.page'].create([{
            'wizard_id': self.id,
            'selected': p.get('id') not in archived | elsewhere,
            'archived': p.get('id') in archived,
            'other_account': p.get('id') in elsewhere,
            'page_id': p.get('id'), 'name': p.get('name'),
            'access_token': p.get('access_token'),
        } for p in pages])
        self.state = 'pages'
        return self._reload()

    def action_discover_forms(self):
        """List the forms of the selected Pages for the admin to review."""
        self.ensure_one()
        client = self.env['meta.graph.client']
        selected = self.page_line_ids.filtered('selected')
        if not selected:
            raise UserError(_("Select at least one Page to continue."))
        self.form_line_ids.unlink()
        form_vals = []
        for pl in selected:
            forms = client.discover_forms(pl.access_token, pl.page_id,
                                          app_secret=self.app_secret)
            for f in forms:
                form_vals.append({
                    'wizard_id': self.id, 'page_line_id': pl.id,
                    'page_id': pl.page_id, 'form_id': f.get('id'),
                    'name': f.get('name'),
                })
        archived = self._archived_ids('meta.lead.form', 'form_id',
                                      [v['form_id'] for v in form_vals])
        for vals in form_vals:
            vals['archived'] = vals['form_id'] in archived
            vals['selected'] = not vals['archived']
        self.env['meta.onboarding.form'].create(form_vals)
        self.state = 'forms'
        return self._reload()

    def action_commit(self):
        """Save the account, the selected Pages and the reviewed forms.

        The review lines pick which forms are created, updated or brought
        back. Each selected Page's forms are also re-listed from Meta here,
        so a missing review line never archives a form Meta still lists, and
        only a form Meta still lists can be brought back. Pages are re-listed
        so any that disappeared get archived. Nothing is deleted.
        """
        self.ensure_one()
        # Another app may have been connected since Validate. Check again
        # before any credential is sent to Meta. The lock in meta.account
        # still guards the write below.
        self.env['meta.account']._check_app_id_allowed(self.app_id)
        client = self.env['meta.graph.client']
        # Turn Graph failures into a plain UserError with no token in it.
        try:
            data = client.debug_token(self.app_id, self.app_secret,
                                      self.access_token)
            # Before the Pages are listed, so another app's token lists
            # nothing and never reaches the account.
            app_id = self._check_token_app(data)
            pages_status = {}
            fresh_pages = client.discover_pages(self.access_token,
                                                app_secret=self.app_secret,
                                                status=pages_status)
        except MetaAuthError:
            raise UserError(_("That token expired before onboarding could "
                              "finish. Go back and re-validate with a current "
                              "System User token."))
        except (MetaPermanentError, MetaRateLimitError, MetaTransientError):
            raise UserError(_("Could not reach Meta to finish onboarding. "
                              "Try Finish again in a moment."))
        data = dict(data or {})
        data['app_id'] = app_id
        changes = {}
        account = self._get_or_create_account(data, changes=changes)
        # Read before the write so the summary says what really changed.
        old_token = account.sudo().access_token
        if changes.get('existing'):
            changes['secret_count'] = len(
                self.env['meta.account']._accounts_needing_secret(
                    self.app_secret))
        changes['token_replaced'] = (bool(changes.get('existing'))
                                     and old_token != self.access_token)
        # meta.account copies the secret to every account.
        account.write({'app_secret': self.app_secret,
                       'access_token': self.access_token})
        selected = self.page_line_ids.filtered('selected')
        for pl in selected:
            # The review lines are the client's: any of them can be edited
            # or deleted over RPC. So every Page's forms are listed again,
            # and a form Meta still lists is never archived for lack of a
            # line.
            try:
                fresh_forms = client.discover_forms(
                    pl.access_token, pl.page_id, app_secret=self.app_secret)
            except MetaAuthError:
                raise UserError(_("A Page token expired before onboarding "
                                  "could finish. Go back and re-validate with "
                                  "a current System User token."))
            except (MetaPermanentError, MetaRateLimitError,
                    MetaTransientError):
                raise UserError(_("Could not reach Meta to finish "
                                  "onboarding. Try Finish again in a "
                                  "moment."))
            fresh = {f.get('id'): f for f in fresh_forms or [] if f.get('id')}
            page = self._get_or_create_page(account, pl.page_id, pl.name,
                                             pl.access_token, changes=changes)
            page_form_lines = self.form_line_ids.filtered(
                lambda l, pid=pl.page_id: l.page_id == pid)
            # Archived is read from the database, not from the line. A
            # ticked archived form only comes back while Meta lists it.
            db_archived = self._archived_ids(
                'meta.lead.form', 'form_id', page_form_lines.mapped('form_id'))
            reactivate_ids = {fl.form_id for fl in page_form_lines
                              if fl.selected and fl.form_id in db_archived}
            reactivate_ids &= set(fresh)
            # An active or new form is kept whatever its tick says.
            cached = [{'id': fl.form_id, 'name': fl.name}
                      for fl in page_form_lines
                      if fl.form_id not in db_archived
                      or fl.form_id in reactivate_ids]
            if page.has_access('read'):
                keep_ids = {c['id'] for c in cached} | set(fresh)
                self._reconcile_forms(page, cached, keep_ids=list(keep_ids),
                                      reactivate_ids=reactivate_ids,
                                      changes=changes)
                continue
            # The admin can't see this Page's company, so only form ids Meta
            # itself returns for it are written. The lines only pick which;
            # names and the archive list come from Meta's fresh answer.
            confirmed = [{'id': f['id'], 'name': fresh[f['id']].get('name')}
                         for f in cached if f['id'] in fresh]
            self._reconcile_forms(page, confirmed, privileged=True,
                                  keep_ids=list(fresh),
                                  reactivate_ids=reactivate_ids,
                                  changes=changes)
        selected_ids = {pl.page_id for pl in selected}
        self._reconcile_pages(account, fresh_pages, selected_ids=selected_ids,
                              complete=not pages_status.get('truncated'))
        self.account_id = account.id
        self.write({
            'is_update': bool(changes.get('existing')),
            'onboarding_summary': self._onboarding_summary(changes),
        })
        self.state = 'done'
        # Show the verify token and URL to paste into the Meta app's webhook
        # settings (Page, leadgen field), and load the subscription status.
        self.webhook_verify_token = self._ensure_verify_token()
        self.webhook_url = self._webhook_url()
        if not self.page_id:
            first_page = self.page_line_ids.filtered('selected')[:1]
            if first_page:
                self.page_id = self.env['meta.page'].search(
                    [('page_id', '=', first_page.page_id)], limit=1).id
        if self.page_id:
            self._refresh_subscription_status()
        return self._reload()

    # ------------------------------------------------------------------
    # Webhook subscription
    # ------------------------------------------------------------------
    def action_subscribe_page(self):
        """Subscribe the selected page to the leadgen webhook."""
        self.ensure_one()
        if not self.page_id:
            raise UserError(_("Select a Page before subscribing."))
        if not self.page_id.sudo().access_token:
            raise UserError(self._no_page_token_hint())
        client = self.env['meta.graph.client']
        try:
            client.subscribe_page(self.page_id)
        except MetaAuthError:
            raise UserError(_("That page token is invalid or expired. "
                              "Re-validate the connection and try again."))
        except (MetaPermanentError, MetaRateLimitError, MetaTransientError):
            raise UserError(_("Could not reach Meta to subscribe this Page. "
                              "Try again in a moment."))
        self._refresh_subscription_status()
        return self._reload()

    def action_unsubscribe_page(self):
        """Unsubscribe the selected page from the leadgen webhook.

        This removes the app's whole subscription for the page, not just the
        leadgen field.
        """
        self.ensure_one()
        if not self.page_id:
            raise UserError(_("Select a Page before unsubscribing."))
        if not self.page_id.sudo().access_token:
            raise UserError(self._no_page_token_hint())
        client = self.env['meta.graph.client']
        try:
            client.unsubscribe_page(self.page_id)
        except MetaAuthError:
            raise UserError(_("That page token is invalid or expired. "
                              "Re-validate the connection and try again."))
        except (MetaPermanentError, MetaRateLimitError, MetaTransientError):
            raise UserError(_("Could not reach Meta to unsubscribe this Page. "
                              "Try again in a moment."))
        self._refresh_subscription_status()
        return self._reload()
