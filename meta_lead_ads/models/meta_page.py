# Meta Lead Ads Integration for Odoo — © 2026 CoreSys Builders. All rights reserved.
# Licensed under the Odoo Proprietary License v1.0 (OPL-1).
# Unauthorized copying, redistribution, or resale of this software, in whole or in
# part, via any medium, is strictly prohibited and constitutes a license violation.
# OPL-1: https://www.odoo.com/documentation/20.0/legal/licenses.html#odoo-apps

import logging

from psycopg2 import errorcodes
from psycopg2.errors import LockNotAvailable, SerializationFailure

from odoo import _, api, fields, models
from odoo.exceptions import AccessError, UserError, ValidationError

_logger = logging.getLogger(__name__)

# Fields that decide who gets this Page's leads and how they're created.
_ROUTING_FIELDS = ('lead_user_id', 'lead_team_id', 'lead_type')


class MetaPage(models.Model):
    _name = 'meta.page'
    _description = 'Meta Page'

    # The ORM does not enforce readonly on write, so without a guard an RPC
    # call could move this Page between companies by writing it directly.
    _derived_company_fields = ('lead_company_id',)

    name = fields.Char(required=True)
    page_id = fields.Char(string='Meta Page ID', required=True, index=True)
    active = fields.Boolean(default=True)
    # Page access token, admin-only. Don't expose it through a computed or
    # related field, or a sudo() read on behalf of a non-admin.
    access_token = fields.Char(string='Page Access Token',
                               groups='meta_lead_ads.group_meta_admin')
    account_id = fields.Many2one('meta.account', string='Account',
                                 required=True, ondelete='cascade')
    # Kept as a plain field on purpose. A computed field with readonly=False
    # would be recomputed, and the override lost, whenever the account's
    # company changes.
    company_id = fields.Many2one(
        'res.company', string='Company Override', ondelete='set null',
        help="Leads from this Page go to this company. Leave empty to use "
             "the account's company.")
    # Where this Page's leads actually go. Stored so record rules and SQL can
    # use a plain column.
    lead_company_id = fields.Many2one(
        'res.company', string='Lead Company',
        compute='_compute_lead_company_id', store=True, index=True)
    # No check_company on these two: company_id is only the override, the
    # company that counts is lead_company_id, checked in _check_lead_routing.
    # The domain lives here and not in the view because it needs the sales
    # group's id. all_group_ids includes implied groups (groups_id on 18).
    lead_user_id = fields.Many2one(
        'res.users', string='Default Salesperson', ondelete='set null',
        domain=lambda self: (
            "[('share', '=', False), ('company_ids', 'in', lead_company_id), "
            "('all_group_ids', 'in', [%d])]"
            % self.env.ref('sales_team.group_sale_salesman').id),
        help="Assigned to new leads from this Page. Leave empty to create "
             "them unassigned. An existing lead only gets this salesperson "
             "if it has none.")
    lead_team_id = fields.Many2one(
        'crm.team', string='Default Sales Team', ondelete='set null',
        help="Set on new leads from this Page. With no salesperson set, the "
             "team's own assignment rules can pick the lead up. Left empty, "
             "a new lead with a default salesperson gets that salesperson's "
             "team. An existing lead only gets this team if it has none.")
    # With the Leads feature off, a record created as a lead never shows in
    # the pipeline, so a new Page creates opportunities then (as crm does).
    lead_type = fields.Selection(
        [('lead', 'Lead'), ('opportunity', 'Opportunity')],
        string='Create As', required=True,
        default=lambda self: ('lead'
                              if self.env.user.has_group('crm.group_use_lead')
                              else 'opportunity'),
        help="New leads from this Page are created as leads or as "
             "opportunities. Leads Odoo matches to an existing record keep "
             "their current type.")
    # Live, so a user archived or moved after it was picked shows up here
    # instead of failing quietly at ingest.
    routing_issue = fields.Char(compute='_compute_routing_issue',
                                compute_sudo=True)
    form_ids =fields.One2many('meta.lead.form', 'page_id', string='Lead Forms')
    form_count = fields.Integer(compute='_compute_form_count', store=True)
    # Set when Meta throttles lead reads for this Page. The webhook queue
    # leaves its events alone until then, including ones that arrive later.
    # No default, so upgrading does not rewrite the table.
    lead_fetch_paused_until = fields.Datetime(
        string='Lead Reads Paused Until', copy=False, readonly=True)

    # Odoo 19 uses models.Constraint instead of _sql_constraints.
    _page_id_uniq = models.Constraint(
        'unique(page_id)',
        'A Meta Page with this ID already exists.',
    )

    @api.depends('form_ids')
    def _compute_form_count(self):
        for rec in self:
            rec.form_count = len(rec.form_ids)

    @api.depends('company_id', 'account_id.company_id')
    def _compute_lead_company_id(self):
        for page in self:
            page.lead_company_id = page.company_id or page.account_id.company_id

    # group_ids is groups_id on 18.
    @api.depends('lead_user_id', 'lead_user_id.active', 'lead_user_id.share',
                 'lead_user_id.company_ids', 'lead_user_id.group_ids',
                 'lead_team_id', 'lead_team_id.active',
                 'lead_team_id.company_id', 'lead_team_id.use_leads',
                 'lead_team_id.use_opportunities', 'lead_type',
                 'lead_company_id')
    def _compute_routing_issue(self):
        for page in self:
            company = page.lead_company_id
            user = page.lead_user_id
            team = page.lead_team_id
            issues = []
            if user:
                name = self._routing_name(user)
                if not user.active:
                    issues.append(_(
                        "The default salesperson %(name)s is archived. New "
                        "leads from this Page arrive without a salesperson "
                        "until you pick someone else.", name=name))
                elif user.share:
                    issues.append(_(
                        "The default salesperson %(name)s isn't an internal "
                        "user anymore. New leads from this Page arrive "
                        "without a salesperson until you pick someone else.",
                        name=name))
                elif not self._has_sales_rights(user):
                    issues.append(_(
                        "The default salesperson %(name)s no longer has Sales "
                        "rights. New leads from this Page arrive without a "
                        "salesperson until you pick someone else.", name=name))
                elif company and company not in user.company_ids:
                    issues.append(_(
                        "The default salesperson %(name)s can't work in "
                        "%(company)s. New leads from this Page arrive without "
                        "a salesperson until you pick someone else.",
                        name=name, company=company.display_name))
            if team:
                name = self._routing_name(team)
                if not team.active:
                    issues.append(_(
                        "The default sales team %(name)s is archived. New "
                        "leads from this Page arrive without a team until "
                        "you pick another one.", name=name))
                elif team.company_id and team.company_id != company:
                    issues.append(_(
                        "The default sales team %(name)s belongs to another "
                        "company. New leads from this Page arrive without a "
                        "team until you pick another one.", name=name))
                # Only a warning: the lead is still created and kept on the
                # team, it just doesn't show in that team's view.
                elif page.lead_type == 'lead' and not team.use_leads:
                    issues.append(_(
                        "The default sales team %(name)s doesn't use leads, so "
                        "new leads from this Page won't show in its pipeline. "
                        "Pick a team that uses leads or create opportunities "
                        "instead.", name=name))
                elif (page.lead_type == 'opportunity'
                        and not team.use_opportunities):
                    issues.append(_(
                        "The default sales team %(name)s doesn't use the "
                        "pipeline, so new opportunities from this Page won't "
                        "show in it. Pick a team with a pipeline or create "
                        "leads instead.", name=name))
            page.routing_issue = ' '.join(issues) or False

    @api.model
    def _routing_name(self, record):
        # Archived users and teams still need a name in the message.
        return record.sudo().with_context(active_test=False).display_name

    @api.model
    def _has_sales_rights(self, user):
        # Without it the user can't open the leads they'd be given.
        return user.sudo().has_group('sales_team.group_sale_salesman')

    @api.model
    def _salesperson_fits(self, user, company):
        """True if ``user`` can own new leads in ``company``: active,
        internal, in that company and with Sales rights. The save check
        and routing_issue apply the same tests one by one, so they can say
        which one failed."""
        return bool(user.active and not user.share
                    and (not company or company in user.company_ids)
                    and self._has_sales_rights(user))

    # Routing is checked in create/write, not @api.constrains, because
    # constraint methods always run as sudo and would let anyone through.
    @api.model_create_multi
    def create(self, vals_list):
        self._check_derived_company_vals(vals_list)
        routing = [[f for f in _ROUTING_FIELDS if f in vals]
                   for vals in vals_list]
        if not self.env.su and any(routing):
            self._check_routing_admin()
        records = super().create(vals_list)
        if not self.env.su:
            records._check_page_routing()
        for record, fnames in zip(records, routing):
            if fnames:
                record._check_lead_routing(fnames)
        return records

    def write(self, vals):
        self._check_derived_company_vals([vals])
        routing = [f for f in _ROUTING_FIELDS if f in vals]
        if not self.env.su and routing:
            self._check_routing_admin()
        if not self.env.su and 'account_id' in vals:
            # Taking a Page away from an account is a routing change for
            # that account too, so its readers decide it.
            Account = self.env['meta.account']
            for rec in self.sudo():
                if not Account.browse(rec.account_id.id).has_access('read'):
                    raise ValidationError(_(
                        "You can only route Pages of an account you can "
                        "access."))
        reactivated = (self.filtered(lambda p: not p.active)
                       if vals.get('active') else self.browse())
        res = super().write(vals)
        # Picking who gets the leads is a routing change too, so it needs
        # the same reader of the account inside the lead company.
        if not self.env.su and ('company_id' in vals or 'account_id' in vals
                                or routing):
            self._check_page_routing()
        # Only the defaults being written are checked. A company or account
        # change that leaves an old default out of place is shown as a
        # warning instead, so it can't block the move.
        if routing:
            self._check_lead_routing(routing)
        # Unarchive and the onboarding re-connect both come through here;
        # leads held while the Page was archived go back in line on their
        # own.
        if reactivated:
            Event = self.env['meta.webhook.event'].sudo()
            Event._requeue_archived_holds(reactivated)
        return res

    def unlink(self):
        # The database drops the Page's forms and their mappings without
        # calling either unlink, so the fields they filled are released here.
        forms = self.env['meta.lead.form'].sudo().with_context(
            active_test=False).search([('page_id', 'in', self.ids)])
        mapped = forms._meta_mapped_crm_fields()
        res = super().unlink()
        self.env['meta.field.mapping']._meta_release_unused_fields(mapped)
        return res

    def _check_routing_admin(self):
        if not self.env.user.has_group('meta_lead_ads.group_meta_admin'):
            raise AccessError(_(
                "Only Meta administrators can change a Page's lead routing."))

    def _check_lead_routing(self, fnames):
        """Refuse a default salesperson or team, among ``fnames``, that
        can't take this Page's leads.

        Runs for superuser too: these are data rules, not permissions. Same
        tests as routing_issue, so what saves is what ingest will use.
        """
        for rec in self.sudo():
            company = rec.lead_company_id
            user = rec.lead_user_id
            if 'lead_user_id' in fnames and user:
                name = self._routing_name(user)
                if not user.active:
                    raise ValidationError(_(
                        "%(user)s is archived. Pick an active salesperson.",
                        user=name))
                if user.share:
                    raise ValidationError(_(
                        "%(user)s isn't an internal user, so they can't own "
                        "leads. Pick a salesperson from your team.",
                        user=name))
                if not self._has_sales_rights(user):
                    raise ValidationError(_(
                        "%(user)s doesn't have Sales rights, so they can't "
                        "work the leads from this Page. Pick a salesperson "
                        "with access to CRM.", user=name))
                if company and company not in user.company_ids:
                    raise ValidationError(_(
                        "%(user)s can't work in %(company)s, the company "
                        "this Page's leads go to. Pick a salesperson from "
                        "that company.",
                        user=name, company=company.display_name))
            team = rec.lead_team_id
            if 'lead_team_id' in fnames and team:
                name = self._routing_name(team)
                if not team.active:
                    raise ValidationError(_(
                        "The sales team %(team)s is archived. Pick an active "
                        "team.", team=name))
                if team.company_id and team.company_id != company:
                    raise ValidationError(_(
                        "The sales team %(team)s belongs to another company. "
                        "Pick a team from %(company)s, or one shared by all "
                        "companies.",
                        team=name, company=company.display_name))

    def _check_derived_company_vals(self, vals_list):
        if self.env.su:
            return
        for vals in vals_list:
            if any(name in vals for name in self._derived_company_fields):
                raise AccessError(_(
                    "The company on this record is set automatically and "
                    "can't be changed directly."))

    def _check_page_routing(self):
        # Routing decides which company receives this Page's leads, so only
        # someone who can see the account and belongs to the destination
        # company may set it. Runs on every create, and on writes of
        # company_id, account_id or the lead routing defaults, so edits of
        # the name or token are not affected. Superuser skips it.
        Account = self.env['meta.account']
        user_companies = self.env.user.company_ids
        # Values are read as sudo: after the write the Page may already sit
        # in a company the writer can't read, which is what we report on.
        for rec in self.sudo():
            account = Account.browse(rec.account_id.id)
            if not account.has_access('read'):
                raise ValidationError(_(
                    "You can only route Pages of an account you can access."))
            if rec.lead_company_id not in user_companies:
                raise ValidationError(_(
                    "You can only assign a company you belong to."))

    def _pause_lead_fetch(self, until):
        """Hold back lead reads for this Page until ``until``.

        Plain SQL on purpose: write() runs routing checks this bookkeeping
        does not need, and the condition keeps the pause from ever moving
        earlier. The row is locked with NOWAIT, so a busy Page skips the
        write instead of stalling the caller. Returns False when skipped.
        """
        self.ensure_one()
        self.flush_recordset(['lead_fetch_paused_until'])
        cr = self.env.cr
        try:
            with cr.savepoint():
                # A busy Page is an expected answer, not a bad query.
                cr.execute("SELECT id FROM meta_page WHERE id = %s "
                           "FOR UPDATE NOWAIT", (self.id,),
                           log_exceptions=False)
                cr.execute(
                    "UPDATE meta_page SET lead_fetch_paused_until = %s "
                    "WHERE id = %s AND (lead_fetch_paused_until IS NULL "
                    "OR lead_fetch_paused_until < %s)",
                    (until, self.id, until))
        except (LockNotAvailable, SerializationFailure):
            return self._lead_fetch_pause_skipped()
        except Exception as e:
            # Some psycopg2 builds raise a plain OperationalError with the
            # pgcode instead.
            if getattr(e, 'pgcode', None) in (
                    errorcodes.LOCK_NOT_AVAILABLE,
                    errorcodes.SERIALIZATION_FAILURE):
                return self._lead_fetch_pause_skipped()
            raise
        self.invalidate_recordset(['lead_fetch_paused_until'])
        return True

    def _lead_fetch_pause_skipped(self):
        # The event's own wait still holds it back, so skipping is safe.
        _logger.info("meta_lead_ads: page id=%s is busy; lead read pause "
                     "not saved this time", self.id)
        return False

    def action_resume_lead_reads(self):
        """End this Page's lead read pause now and let the queue run.

        For a pause that outlived its reason, e.g. a waiting event that
        finished some other way. Makes no Graph call: if Meta still
        refuses, the drain sets a new pause from Meta's new estimate.
        Events that wait on their own keep their wait; Retry on the queue
        ends it.
        """
        # The button is hidden from others, but RPC reaches this directly.
        if (not self.env.su
                and not self.env.user.has_group('meta_lead_ads.group_meta_admin')):
            raise AccessError(_("Meta Admin access required."))
        # Company rules: only Pages the admin may change.
        self.check_access('write')
        paused = self.filtered('lead_fetch_paused_until')
        if paused:
            cr = self.env.cr
            busy = _("Lead reads for this Page are being updated right now. "
                     "Try again in a minute.")
            try:
                with cr.savepoint():
                    # The drain keeps a Page it has just paused locked until
                    # its run ends; the click must not wait for that.
                    cr.execute("SELECT id FROM meta_page WHERE id IN %s "
                               "FOR UPDATE NOWAIT", (tuple(paused.ids),),
                               log_exceptions=False)
                    paused._clear_lead_fetch_pause()
            except (LockNotAvailable, SerializationFailure):
                raise UserError(busy) from None
            except Exception as e:
                if getattr(e, 'pgcode', None) in (
                        errorcodes.LOCK_NOT_AVAILABLE,
                        errorcodes.SERIALIZATION_FAILURE):
                    raise UserError(busy) from None
                raise
            self.env.ref('meta_lead_ads.cron_meta_webhook_drain').sudo()._trigger()
        return {
            'type': 'ir.actions.client',
            'tag': 'display_notification',
            'params': {
                'message': _("Lead reads resumed. Waiting leads are fetched "
                             "shortly."),
                'type': 'success',
                'sticky': False,
            },
        }

    def _clear_lead_fetch_pause(self):
        """End the lead read pause now.

        Lets a Meta Admin end the wait early from the queue or the Page;
        nothing clears it automatically.
        """
        if not self.ids:
            return
        self.flush_recordset(['lead_fetch_paused_until'])
        self.env.cr.execute(
            "UPDATE meta_page SET lead_fetch_paused_until = NULL "
            "WHERE id IN %s", (tuple(self.ids),))
        self.invalidate_recordset(['lead_fetch_paused_until'])

    def action_open_forms(self):
        # No create=False in the context: Meta Users already lack create access
        # on meta.lead.form so the New button hides itself, and admins should
        # still be able to add forms from here.
        self.ensure_one()
        return {
            'type': 'ir.actions.act_window',
            'name': 'Lead Forms',
            'res_model': 'meta.lead.form',
            'view_mode': 'list,form',
            'domain': [('page_id', '=', self.id)],
            'context': {'default_page_id': self.id},
        }
