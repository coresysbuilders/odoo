# Meta Lead Ads Integration for Odoo — © 2026 CoreSys Builders. All rights reserved.
# Licensed under the Odoo Proprietary License v1.0 (OPL-1).
# Unauthorized copying, redistribution, or resale of this software, in whole or in
# part, via any medium, is strictly prohibited and constitutes a license violation.
# OPL-1: https://www.odoo.com/documentation/19.0/legal/licenses.html#odoo-apps

# Guided wizard that lets an admin connect Odoo to Meta. It reuses the Graph
# client methods and meta.account._map_token_status -- no Graph URL is built
# here and no data{}->fields mapping is duplicated (the mapper authority stays
# on meta.account). The page/form selection lines are transient children; each
# page line carries its own per-Page access_token, admin-grouped.
import secrets

from odoo import api, fields, models, _
from odoo.exceptions import UserError

from odoo.addons.meta_lead_ads.models.exceptions import (
    MetaAuthError, MetaPermanentError, MetaRateLimitError, MetaTransientError,
)


class MetaOnboardingPage(models.TransientModel):
    _name = 'meta.onboarding.page'
    _description = 'Meta Onboarding Page Line'

    wizard_id = fields.Many2one('meta.onboarding', required=True,
                                ondelete='cascade')
    selected = fields.Boolean(default=True)
    page_id = fields.Char(readonly=True)
    name = fields.Char(readonly=True)
    # The per-Page access token is secret -> admin-grouped. page_id/name are
    # not secret -> ungrouped.
    access_token = fields.Char(groups='meta_lead_ads.group_meta_admin')


class MetaOnboardingForm(models.TransientModel):
    _name = 'meta.onboarding.form'
    _description = 'Meta Onboarding Form Line'

    # Caches the reviewed forms so commit consumes exactly the reviewed set
    # (no second discovery in commit).
    wizard_id = fields.Many2one('meta.onboarding', required=True,
                                ondelete='cascade')
    page_line_id = fields.Many2one('meta.onboarding.page', ondelete='cascade')
    page_id = fields.Char(readonly=True)        # the Meta page id this form belongs to
    form_id = fields.Char(readonly=True)
    name = fields.Char(readonly=True)


class MetaOnboarding(models.TransientModel):
    _name = 'meta.onboarding'
    _description = 'Meta Onboarding Wizard'

    state = fields.Selection(
        [('credentials', 'Credentials'), ('pages', 'Select Pages'),
         ('forms', 'Review Forms'), ('done', 'Done')],
        default='credentials')
    # App ID is public app metadata -> not secret, ungrouped. App Secret and the
    # pasted token are secret -> admin-grouped. The token field is named
    # access_token to mirror meta.account.access_token.
    app_id = fields.Char()
    app_secret = fields.Char(groups='meta_lead_ads.group_meta_admin')
    access_token = fields.Char(string='Token',
                               groups='meta_lead_ads.group_meta_admin')
    exchange_short_lived = fields.Boolean(
        string='This is a short-lived User token (exchange it)')
    page_line_ids = fields.One2many('meta.onboarding.page', 'wizard_id')
    form_line_ids = fields.One2many('meta.onboarding.form', 'wizard_id')
    account_id = fields.Many2one('meta.account')
    token_owner_id = fields.Char(readonly=True)   # the identity the account keys on
    # Status mirror fields (filled from _map_token_status for display). The
    # label is lead_retrieval_granted, not production_ready -- same selection as
    # meta.account.
    token_valid = fields.Boolean(readonly=True)
    access_status = fields.Selection(
        [('auth_failed', 'Authentication failed'),
         ('dev_test_only', 'Development mode — test leads only'),
         ('lead_retrieval_granted',
          'leads_retrieval granted (App Review still gates production)')],
        readonly=True)
    leads_retrieval_granted = fields.Boolean(readonly=True)

    # ------------------------------------------------------------------
    # Per-page webhook subscription + verify_token/URL surface
    # ------------------------------------------------------------------
    # The page the Subscribe/Unsubscribe/status actions target. The whole
    # wizard action/menu/view is group_meta_admin-restricted (ACL + admin-only
    # menu), so the verify_token surfaced below is shown only to an admin.
    # meta.page itself is admin/user readable but the secret surface here
    # inherits the wizard's admin-only access.
    page_id = fields.Many2one('meta.page', string='Page')
    # Declared so the view's widget="badge" resolves to a real field; populated
    # by _refresh_subscription_status. Never let the view reference an
    # undeclared field.
    subscription_state = fields.Selection(
        [('subscribed', 'Subscribed'), ('not_subscribed', 'Not Subscribed')],
        readonly=True)
    # Read-only display surface for paste-into-Meta. webhook_verify_token is a
    # shared secret shown read-only only to the admin (the wizard is
    # admin-only) -- not a password input and not exposed to any non-admin via
    # field/related/compute/sudo.
    webhook_url = fields.Char(readonly=True)
    webhook_verify_token = fields.Char(string='Verify Token', readonly=True)

    # ------------------------------------------------------------------
    # helpers
    # ------------------------------------------------------------------
    def _ensure_verify_token(self):
        """Get-or-create the verify_token ir.config_parameter. Generated once
        with secrets.token_urlsafe(32); a second call returns the same value
        (idempotent)."""
        ICP = self.env['ir.config_parameter'].sudo()
        key = 'meta_lead_ads.webhook_verify_token'
        tok = ICP.get_param(key)
        if not tok:
            tok = secrets.token_urlsafe(32)
            ICP.set_param(key, tok)
        return tok

    def _webhook_url(self):
        """The public webhook endpoint = web.base.url + the route. No Graph URL
        is built here."""
        base = self.env['ir.config_parameter'].sudo().get_param(
            'web.base.url') or ''
        return '%s%s' % (base, '/meta_lead_ads/webhook')

    def _refresh_subscription_status(self):
        """Map a live page_subscription_status data[] response to the declared
        subscription_state field. 'subscribed' iff some subscribed_apps entry
        lists the leadgen field, else 'not_subscribed'. Token-free and directly
        callable so it can be asserted without an HTTP round-trip."""
        self.ensure_one()
        if not self.page_id:
            self.subscription_state = 'not_subscribed'
            return
        client = self.env['meta.graph.client']
        data = client.page_subscription_status(self.page_id) or {}
        subscribed = any(
            'leadgen' in (entry.get('subscribed_fields') or [])
            for entry in (data.get('data') or []))
        self.subscription_state = 'subscribed' if subscribed else 'not_subscribed'
    @api.model
    def _map_token_status(self, data):
        """Delegate to meta.account's mapper -- do not duplicate the
        data{}->status logic (single mapper authority)."""
        return self.env['meta.account']._map_token_status(data)

    def _reload(self):
        """Re-render the same wizard record in-place (stepped idiom)."""
        self.ensure_one()
        return {'type': 'ir.actions.act_window', 'res_model': self._name,
                'res_id': self.id, 'view_mode': 'form', 'target': 'new'}

    # ------------------------------------------------------------------
    # identity + reconcile
    # ------------------------------------------------------------------
    @api.model
    def _get_or_create_account(self, data):
        """get-or-create the meta.account keyed on the token owner id, never the
        app id. Two owners under one app -> two accounts. app_id is stored in
        its own field; status comes from the shared mapper. Existing accounts
        keep their manual edits -- only the Meta-authoritative fields
        (name/owner/app_id/status) are refreshed."""
        status = self._map_token_status(data)
        owner = status.get('token_owner_id')
        # account_id is required + unique; an absent owner would either collide
        # ownerless connections onto one empty-key record or raise a bare
        # IntegrityError. Fail fast and token-free before any search/create.
        if not owner:
            raise UserError(_("Meta did not return a token owner id for this "
                              "token, so no account can be created. Use a "
                              "System User token (Business Manager)."))
        app_id = (data or {}).get('app_id')
        Account = self.env['meta.account']
        account = Account.search([('account_id', '=', owner)], limit=1)
        vals = {
            'name': (data or {}).get('application') or owner,
            'account_id': owner,
            'token_owner_id': owner,
            'app_id': app_id,            # separate field -- never overload account_id
        }
        vals.update(status)
        if account:
            account.write(vals)
        else:
            account = Account.create(vals)
        return account

    @api.model
    def _get_or_create_page(self, account, pid, name, access_token):
        """get-or-create a meta.page scoped to the owning account.

        meta.page.page_id is globally unique. Searching by page_id alone and
        writing account_id would silently re-parent a page (and its
        cascade-owned forms) from another account onto this one. So scope the
        search to this account; if the same page_id already belongs to a
        different account, refuse explicitly with a token-free error instead of
        a silent re-parent or an opaque unique-constraint crash."""
        Page = self.env['meta.page']
        pvals = {'name': name, 'page_id': pid, 'account_id': account.id,
                 'access_token': access_token, 'active': True}
        page = Page.search([('page_id', '=', pid),
                            ('account_id', '=', account.id)], limit=1)
        if page:
            page.write(pvals)        # Meta-authoritative only; manual edits kept
            return page
        # Not under this account -- is it owned by another account?
        other = Page.with_context(active_test=False).search(
            [('page_id', '=', pid), ('account_id', '!=', account.id)], limit=1)
        if other:
            raise UserError(_(
                "This Page is already onboarded under a different Meta "
                "account. Onboard it from that account, or remove it there "
                "first; it will not be moved automatically."))
        return Page.create(pvals)

    @api.model
    def _reconcile_pages(self, account, discovered, selected_ids=None):
        """Additive reconcile.

        - get-or-create every selected page that the fresh discovery returned;
        - a page absent from the fresh discovery -> active=False (never unlink);
        - an unselected-but-still-present page stays active=True.
        """
        Page = self.env['meta.page']
        if selected_ids is None:
            selected_ids = {p.get('id') for p in discovered}
        discovered_ids = [p.get('id') for p in discovered]
        # get-or-create the selected, still-present pages.
        for p in discovered:
            pid = p.get('id')
            if pid not in selected_ids:
                continue
            self._get_or_create_page(account, pid, p.get('name'),
                                     p.get('access_token'))
        # Deactivate only pages that disappeared from Meta (absent from the
        # fresh discovery) -- not pages merely left unselected this session.
        Page.search([('account_id', '=', account.id),
                     ('page_id', 'not in', discovered_ids)]).write(
            {'active': False})

    @api.model
    def _reconcile_forms(self, page, discovered_forms):
        """Auto-import all forms under a page, additive.

        get-or-create on the unique form_id; new forms default sync_enabled=True
        (the field default -- not overridden here). Forms absent from the fresh
        discovery -> active=False; never unlink."""
        Form = self.env['meta.lead.form']
        discovered_ids = [f.get('id') for f in discovered_forms]
        for f in discovered_forms:
            fid = f.get('id')
            form = Form.search([('form_id', '=', fid)], limit=1)
            fvals = {'name': f.get('name'), 'form_id': fid,
                     'page_id': page.id, 'active': True}
            if form:
                form.write(fvals)        # sync_enabled (manual edit) preserved
            else:
                Form.create(fvals)       # sync_enabled defaults True
        Form.search([('page_id', '=', page.id),
                     ('form_id', 'not in', discovered_ids)]).write(
            {'active': False})

    # ------------------------------------------------------------------
    # stepped actions
    # ------------------------------------------------------------------
    def action_validate(self):
        """Step credentials -> pages. Local empty-credential validation first,
        then optional token exchange / introspect, then page discovery. All
        errors are token-free."""
        self.ensure_one()
        # Validate non-empty credentials locally, token-free, before
        # constructing the client or any Graph call.
        if not (self.app_id and self.app_id.strip()):
            raise UserError(_("App ID is required."))
        if not (self.app_secret and self.app_secret.strip()):
            raise UserError(_("App Secret is required."))
        if not (self.access_token and self.access_token.strip()):
            raise UserError(_("Paste a System User token (or a short-lived "
                              "User token to exchange)."))
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
            # HTTP-200 is_valid:false body -- surface, stay on step.
            raise UserError(_("Meta reports this token is not valid. Paste a "
                              "current System User token and try again."))
        self.write({
            'access_token': token,           # store the durable token
            'token_owner_id': status.get('token_owner_id'),
            'token_valid': status.get('token_valid'),
            'access_status': status.get('access_status'),
            'leads_retrieval_granted': status.get('leads_retrieval_granted'),
        })
        # Discover pages, build selection lines (each carries its token).
        pages = client.discover_pages(token, app_secret=self.app_secret)
        self.page_line_ids.unlink()
        self.env['meta.onboarding.page'].create([{
            'wizard_id': self.id, 'selected': True,
            'page_id': p.get('id'), 'name': p.get('name'),
            'access_token': p.get('access_token'),
        } for p in pages])
        self.state = 'pages'
        return self._reload()

    def action_discover_forms(self):
        """Step pages -> forms. Discover forms once for the selected pages and
        cache them as review lines; commit reuses these."""
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
        self.env['meta.onboarding.form'].create(form_vals)
        self.state = 'forms'
        return self._reload()

    def action_commit(self):
        """Step forms -> done. Account keyed on the token owner; get-or-create
        pages; commit exactly the cached reviewed form set (the reviewed forms
        are authoritative, no second discovery); reconcile pages against a fresh
        full discovery scoped to the selected pages. Never unlink, never build a
        Graph URL, never log the token."""
        self.ensure_one()
        client = self.env['meta.graph.client']
        # Route all live Graph calls through one try/except so a transient
        # mid-commit failure surfaces as a token-free UserError (consistent with
        # action_validate), never a raw typed exception. The token/secret are
        # never included in the message.
        try:
            # Account identity = token owner, refreshed from a fresh introspect.
            data = client.debug_token(self.app_id, self.app_secret,
                                      self.access_token)
            # Fresh full discovery -- the authority for page reconcile.
            fresh_pages = client.discover_pages(self.access_token,
                                                app_secret=self.app_secret)
        except MetaAuthError:
            raise UserError(_("That token expired before onboarding could "
                              "finish. Go back and re-validate with a current "
                              "System User token."))
        except (MetaPermanentError, MetaRateLimitError, MetaTransientError):
            raise UserError(_("Could not reach Meta to finish onboarding. "
                              "Try Finish again in a moment."))
        # Carry the app_id so _get_or_create_account stores it in its own field
        # even if the debug_token body omits it.
        data = dict(data or {})
        data.setdefault('app_id', self.app_id)
        account = self._get_or_create_account(data)
        # Persist the credentials/token on the account (Meta-authoritative).
        account.write({'app_secret': self.app_secret,
                       'access_token': self.access_token})
        # --- pages: get-or-create the selected lines; commit their cached forms ---
        # The reviewed (cached) form lines are the authoritative committed set --
        # we do not re-discover forms here. That keeps the committed forms
        # exactly what the admin reviewed and never imports forms for pages the
        # admin did not select.
        selected = self.page_line_ids.filtered('selected')
        for pl in selected:
            page = self._get_or_create_page(account, pl.page_id, pl.name,
                                             pl.access_token)
            page_form_lines = self.form_line_ids.filtered(
                lambda l, pid=pl.page_id: l.page_id == pid)
            cached = [{'id': fl.form_id, 'name': fl.name}
                      for fl in page_form_lines]
            self._reconcile_forms(page, cached)
        # --- reconcile pages only: deactivate what a fresh full discovery
        # drops, scoped to the selected pages; unselected-but-still-present
        # pages stay active and are not re-imported. No second form reconcile --
        # the cached set above is the final committed form set. ---
        selected_ids = {pl.page_id for pl in selected}
        self._reconcile_pages(account, fresh_pages, selected_ids=selected_ids)
        self.account_id = account.id
        self.state = 'done'
        # Surface the verify_token + webhook URL (read-only, admin-only) for
        # pasting into Meta App -> Webhooks -> Page -> leadgen, and prime the
        # subscription badge for the connected page(s).
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
    # Subscription actions -- thin, token-free wrappers over the single Graph
    # client (no Graph URL built here). Errors are token-free UserError,
    # following the action_commit idiom.
    # ------------------------------------------------------------------
    def action_subscribe_page(self):
        """Subscribe the selected page to the leadgen webhook."""
        self.ensure_one()
        if not self.page_id:
            raise UserError(_("Select a Page before subscribing."))
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
        """Unsubscribe the selected page from the leadgen webhook. v1 sends a
        bare DELETE (field-level granularity is a deferred live-verify item)."""
        self.ensure_one()
        if not self.page_id:
            raise UserError(_("Select a Page before unsubscribing."))
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
