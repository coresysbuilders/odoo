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
    access_token = fields.Char(groups='meta_lead_ads.group_meta_admin')


class MetaOnboardingForm(models.TransientModel):
    _name = 'meta.onboarding.form'
    _description = 'Meta Onboarding Form Line'

    # The forms the admin reviewed. Commit saves exactly these rather than
    # asking Meta again.
    wizard_id = fields.Many2one('meta.onboarding', required=True,
                                ondelete='cascade')
    page_line_id = fields.Many2one('meta.onboarding.page', ondelete='cascade')
    page_id = fields.Char(readonly=True)        # Meta page id
    form_id = fields.Char(readonly=True)
    name = fields.Char(readonly=True)


class MetaOnboarding(models.TransientModel):
    _name = 'meta.onboarding'
    _description = 'Meta Onboarding Wizard'

    state = fields.Selection(
        [('credentials', 'Credentials'), ('pages', 'Select Pages'),
         ('forms', 'Review Forms'), ('done', 'Done')],
        default='credentials')
    # App ID is public; the App Secret and token are admin-only.
    app_id = fields.Char()
    app_secret = fields.Char(groups='meta_lead_ads.group_meta_admin')
    access_token = fields.Char(string='Token',
                               groups='meta_lead_ads.group_meta_admin')
    exchange_short_lived = fields.Boolean(
        string='This is a short-lived User token (exchange it)')
    page_line_ids = fields.One2many('meta.onboarding.page', 'wizard_id')
    form_line_ids = fields.One2many('meta.onboarding.form', 'wizard_id')
    account_id = fields.Many2one('meta.account')
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

    # ------------------------------------------------------------------
    # Per-page webhook subscription + verify_token/URL surface
    # ------------------------------------------------------------------
    # Target of the subscribe/unsubscribe buttons. The wizard is admin-only,
    # which is what keeps the verify token below away from regular users.
    page_id = fields.Many2one('meta.page', string='Page')
    # Filled by _refresh_subscription_status.
    subscription_state = fields.Selection(
        [('subscribed', 'Subscribed'), ('not_subscribed', 'Not Subscribed')],
        readonly=True)
    # Shown after commit so the admin can paste them into Meta's webhook
    # settings. The verify token is a shared secret; don't expose it to
    # non-admins through a related or computed field elsewhere.
    webhook_url = fields.Char(readonly=True)
    webhook_verify_token = fields.Char(string='Verify Token', readonly=True)

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

    def _refresh_subscription_status(self):
        """Ask Meta whether the page is subscribed and update subscription_state.

        The page counts as subscribed when any subscribed app lists the
        leadgen field.
        """
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
        """Map a debug_token response to status values (see meta.account)."""
        return self.env['meta.account']._map_token_status(data)

    def _reload(self):
        """Reopen this wizard record so the next step is shown."""
        self.ensure_one()
        return {'type': 'ir.actions.act_window', 'res_model': self._name,
                'res_id': self.id, 'view_mode': 'form', 'target': 'new'}

    # ------------------------------------------------------------------
    # identity + reconcile
    # ------------------------------------------------------------------
    @api.model
    def _get_or_create_account(self, data):
        """Find or create the meta.account for the token's owner.

        Accounts are keyed on the token owner, not the app, so two owners on
        one app give two accounts. On an existing account only the fields Meta
        owns (name, owner, app_id, status) are refreshed.
        """
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
        account = Account.search([('account_id', '=', owner)], limit=1)
        vals = {
            'name': (data or {}).get('application') or owner,
            'account_id': owner,
            'token_owner_id': owner,
            'app_id': app_id,
        }
        vals.update(status)
        if account:
            account.write(vals)
        else:
            account = Account.create(vals)
        return account

    @api.model
    def _get_or_create_page(self, account, pid, name, access_token):
        """Find or create the meta.page under this account.

        page_id is globally unique. If another account already owns the page
        we refuse, rather than silently moving it (and its forms) across or
        failing on the unique constraint.
        """
        Page = self.env['meta.page']
        pvals = {'name': name, 'page_id': pid, 'account_id': account.id,
                 'access_token': access_token, 'active': True}
        page = Page.search([('page_id', '=', pid),
                            ('account_id', '=', account.id)], limit=1)
        if page:
            page.write(pvals)        # only Meta-owned fields; manual edits kept
            return page
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
        """Sync pages with what Meta returned.

        Selected pages are created or updated. Pages Meta no longer returns are
        archived, never deleted. Pages that still exist but weren't selected
        this time are left as they are.
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
        Page.search([('account_id', '=', account.id),
                     ('page_id', 'not in', discovered_ids)]).write(
            {'active': False})

    @api.model
    def _reconcile_forms(self, page, discovered_forms):
        """Create or update the page's forms; archive the ones not listed.

        New forms get sync_enabled from the field default. An existing form
        keeps whatever sync_enabled the admin set.
        """
        Form = self.env['meta.lead.form']
        discovered_ids = [f.get('id') for f in discovered_forms]
        for f in discovered_forms:
            fid = f.get('id')
            form = Form.search([('form_id', '=', fid)], limit=1)
            fvals = {'name': f.get('name'), 'form_id': fid,
                     'page_id': page.id, 'active': True}
            if form:
                form.write(fvals)
            else:
                Form.create(fvals)
        Form.search([('page_id', '=', page.id),
                     ('form_id', 'not in', discovered_ids)]).write(
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
        self.write({
            'access_token': token,           # long-lived if it was exchanged
            'token_owner_id': status.get('token_owner_id'),
            'token_valid': status.get('token_valid'),
            'access_status': status.get('access_status'),
            'leads_retrieval_granted': status.get('leads_retrieval_granted'),
        })
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
        self.env['meta.onboarding.form'].create(form_vals)
        self.state = 'forms'
        return self._reload()

    def action_commit(self):
        """Save the account, the selected Pages and the reviewed forms.

        Forms are saved exactly as reviewed. Pages are re-listed from Meta so
        any that disappeared get archived. Nothing is deleted.
        """
        self.ensure_one()
        client = self.env['meta.graph.client']
        # Turn Graph failures into a plain UserError with no token in it.
        try:
            data = client.debug_token(self.app_id, self.app_secret,
                                      self.access_token)
            fresh_pages = client.discover_pages(self.access_token,
                                                app_secret=self.app_secret)
        except MetaAuthError:
            raise UserError(_("That token expired before onboarding could "
                              "finish. Go back and re-validate with a current "
                              "System User token."))
        except (MetaPermanentError, MetaRateLimitError, MetaTransientError):
            raise UserError(_("Could not reach Meta to finish onboarding. "
                              "Try Finish again in a moment."))
        # debug_token doesn't always return app_id.
        data = dict(data or {})
        data.setdefault('app_id', self.app_id)
        account = self._get_or_create_account(data)
        account.write({'app_secret': self.app_secret,
                       'access_token': self.access_token})
        # Use the reviewed form lines, not a new discovery, so we save exactly
        # what the admin saw.
        selected = self.page_line_ids.filtered('selected')
        for pl in selected:
            page = self._get_or_create_page(account, pl.page_id, pl.name,
                                             pl.access_token)
            page_form_lines = self.form_line_ids.filtered(
                lambda l, pid=pl.page_id: l.page_id == pid)
            cached = [{'id': fl.form_id, 'name': fl.name}
                      for fl in page_form_lines]
            self._reconcile_forms(page, cached)
        selected_ids = {pl.page_id for pl in selected}
        self._reconcile_pages(account, fresh_pages, selected_ids=selected_ids)
        self.account_id = account.id
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
