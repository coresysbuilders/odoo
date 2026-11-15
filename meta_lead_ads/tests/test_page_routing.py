# Meta Lead Ads Integration for Odoo — © 2026 CoreSys Builders. All rights reserved.
# Licensed under the Odoo Proprietary License v1.0 (OPL-1).
# Unauthorized copying, redistribution, or resale of this software, in whole or in
# part, via any medium, is strictly prohibited and constitutes a license violation.
# OPL-1: https://www.odoo.com/documentation/18.0/legal/licenses.html#odoo-apps

"""Per-Page lead routing: who may set it, what it may point at, the
warning shown once a saved default stops fitting, and how ingest uses it."""
from contextlib import contextmanager
from datetime import datetime, timedelta, timezone
from unittest import mock

from lxml import etree

from odoo import Command
from odoo.exceptions import AccessError, ValidationError
from odoo.tests import Form
from odoo.tests.common import TransactionCase, new_test_user, tagged
from odoo.tools.safe_eval import safe_eval

from odoo.addons.meta_lead_ads.models import meta_lead_ingest as ingest_module
from odoo.addons.meta_lead_ads.models.exceptions import MetaPermanentError

from .test_ingest import IngestFixtureMixin
from .test_multi_company import MultiCompanyFixtureMixin


class _RoutingPathsMixin:
    """The few calls that drive each real ingest path: webhook drain,
    backfill sweep, sync-log Retry and Restore. The Graph reads are faked
    with IngestFixtureMixin._patch_graph."""

    def _drain(self, fetch_lead, leadgen_id):
        self.env['meta.webhook.event'].sudo().create({
            'leadgen_id': leadgen_id, 'page_id': self.page.page_id,
            'form_id': self.form.form_id, 'status': 'pending'})
        with self._patch_graph(fetch_lead=fetch_lead):
            self.env['meta.webhook.event']._cron_drain()
        self.env.invalidate_all()

    def _sweep(self, fetch_lead, leadgen_id):
        created = (datetime.now(timezone.utc) - timedelta(hours=1)).strftime(
            '%Y-%m-%dT%H:%M:%S+0000')
        path = '%s/leads' % self.form.form_id

        def _paged(token, called_path, params=None, app_secret=None,
                   status=None, **kw):
            if called_path != path:
                return iter([])
            return iter([{'id': leadgen_id, 'created_time': created}])

        with mock.patch.object(self.Client, '_iter_paged',
                               side_effect=_paged), \
                self._patch_graph(fetch_lead=fetch_lead):
            self.env['meta.lead.form']._cron_backfill()
        self.env.invalidate_all()

    def _retry_failed_row(self, fetch_lead, leadgen_id, user):
        # The drain fails the event for good and writes the one failed row.
        self._drain(mock.Mock(side_effect=MetaPermanentError('gone')),
                    leadgen_id)
        row = self.env['meta.sync.log'].sudo().search([
            ('meta_leadgen_id', '=', leadgen_id), ('status', '=', 'failed')])
        self.assertEqual(len(row), 1)
        with self._patch_graph(fetch_lead=fetch_lead):
            row.with_user(user).action_retry()
        self.env.invalidate_all()

    def _restore(self, fetch_lead, leadgen_id, user):
        Tomb = self.env['meta.leadgen.tombstone']
        Tomb._remember([leadgen_id], details={leadgen_id: {
            'page_id': self.page.id, 'company_id': self.co_a.id,
            'reason': 'deleted'}})
        tomb = Tomb.sudo().search([('leadgen_id', '=', leadgen_id)])
        with self._patch_graph(fetch_lead=fetch_lead):
            tomb.with_user(user).action_restore()
        self.env.invalidate_all()


@tagged('post_install', '-at_install')
class TestPageRouting(MultiCompanyFixtureMixin, IngestFixtureMixin,
                      TransactionCase):

    def setUp(self):
        super().setUp()
        self.Page = self.env['meta.page']
        self.team_a = self._team('Routing Team A', self.co_a)
        self.team_b = self._team('Routing Team B', self.co_b)

    # -- helpers --------------------------------------------------------- #
    def _team(self, name, company, use_leads=True, use_opportunities=True):
        # Teams take both types unless a test says otherwise, so the type
        # warning stays out of tests about something else.
        return self.env['crm.team'].create({
            'name': name, 'company_id': company.id if company else False,
            'use_leads': use_leads, 'use_opportunities': use_opportunities})

    def _internal_user(self, login, companies,
                       groups='base.group_user,sales_team.group_sale_salesman'):
        return new_test_user(
            self.env, login, groups=groups,
            company_id=companies[0].id,
            company_ids=[Command.set([c.id for c in companies])])

    def _drop_sales_rights(self, user):
        sales = self.env.ref('sales_team.group_sale_salesman')
        user.sudo().write({'groups_id': [Command.unlink(sales.id)]})
        self.assertFalse(user.has_group('sales_team.group_sale_salesman'))
        self.assertFalse(user.share)

    def _user_domain(self, user):
        fields = self.Page.with_user(user).fields_get(['lead_user_id'],
                                                      ['domain'])
        return fields['lead_user_id']['domain']

    @contextmanager
    def _refused(self, exception, regex):
        # assertRaisesRegex opens no savepoint (assertRaises does), so the
        # refused write would stay in the cache and be flushed later.
        with self.assertRaisesRegex(exception, regex), \
                self.env.cr.savepoint():
            yield

    def _meta_user(self):
        return new_test_user(
            self.env, 'pr_meta_user',
            groups='base.group_user,meta_lead_ads.group_meta_user',
            company_id=self.co_a.id, company_ids=[Command.set([self.co_a.id])])

    def _arch(self, user, view_type='form'):
        views = self.Page.with_user(user).get_views([(False, view_type)])
        return etree.fromstring(views['views'][view_type]['arch'])

    def _page_vals(self, page_id, **extra):
        vals = {'name': 'Routed %s' % page_id, 'page_id': page_id,
                'account_id': self.account.id}
        vals.update(extra)
        return vals

    # -- defaults and who may set them ----------------------------------- #
    def _admin_with_leads(self, login):
        user = new_test_user(
            self.env, login,
            groups='base.group_user,meta_lead_ads.group_meta_admin,'
                   'crm.group_use_lead',
            company_id=self.co_a.id, company_ids=[Command.set([self.co_a.id])])
        self.assertTrue(user.has_group('crm.group_use_lead'))
        return user

    def _admin_without_leads(self):
        # The Leads feature is off by default, so admin_a lacks the group.
        self.assertFalse(self.admin_a.has_group('crm.group_use_lead'))
        return self.admin_a

    def test_new_page_defaults(self):
        page = self.Page.with_user(self._admin_with_leads('pr_leads_on')).create(
            self._page_vals('PGR0'))
        self.assertEqual(page.lead_type, 'lead')
        self.assertFalse(page.lead_user_id)
        self.assertFalse(page.lead_team_id)
        self.assertFalse(page.routing_issue)

    def test_new_page_creates_opportunities_when_leads_off(self):
        page = self.Page.with_user(self._admin_without_leads()).create(
            self._page_vals('PGR0B'))
        self.assertEqual(page.lead_type, 'opportunity')

    def test_explicit_lead_type_wins(self):
        with_leads = self._admin_with_leads('pr_leads_explicit')
        page = self.Page.with_user(with_leads).create(
            self._page_vals('PGR0C', lead_type='opportunity'))
        self.assertEqual(page.lead_type, 'opportunity')
        page = self.Page.with_user(self._admin_without_leads()).create(
            self._page_vals('PGR0D', lead_type='lead'))
        self.assertEqual(page.lead_type, 'lead')

    def test_onboarding_page_follows_leads_setting(self):
        # The wizard creates Pages without a lead_type, so the default rule
        # applies there too.
        Onboarding = self.env['meta.onboarding']
        page = Onboarding.with_user(self._admin_without_leads())\
            ._get_or_create_page(self.account, 'PGR0E', 'Wizard Off', 'tok_e')
        self.assertEqual(page.lead_type, 'opportunity')
        with_leads = self._admin_with_leads('pr_leads_wizard')
        page = Onboarding.with_user(with_leads)._get_or_create_page(
            self.account, 'PGR0F', 'Wizard On', 'tok_f')
        self.assertEqual(page.lead_type, 'lead')

    def _account_b(self):
        return self.env['meta.account'].create({
            'name': 'Routing Acct B', 'account_id': 'ACCRB',
            'company_id': self.co_b.id})

    def test_create_without_routing_keys_keeps_company_check(self):
        # A Page sent to a company admin_a isn't in is stopped by the company
        # rule before the routing check gets a say.
        with self.assertRaises(AccessError):
            self.Page.with_user(self.admin_a).create(
                self._page_vals('PGR1', company_id=self.co_b.id))
        # Overriding to admin_a's own company doesn't make a foreign account
        # routable. Only the create-time routing check catches this one.
        with self._refused(
                ValidationError,
                "You can only route Pages of an account you can access."):
            self.Page.with_user(self.admin_a).create(self._page_vals(
                'PGR1B', account_id=self._account_b().id,
                company_id=self.co_a.id))
        page = self.Page.with_user(self.admin_a).create(self._page_vals('PGR2'))
        self.assertEqual(page.lead_company_id, self.co_a)

    def test_create_with_routing_keys(self):
        page = self.Page.with_user(self.admin_a).create(self._page_vals(
            'PGR3', lead_user_id=self.sales_a.id, lead_type='opportunity'))
        self.assertEqual(page.lead_user_id, self.sales_a)
        self.assertEqual(page.lead_type, 'opportunity')
        with self.assertRaises(AccessError):
            self.Page.with_user(self.admin_a).create(self._page_vals(
                'PGR4', company_id=self.co_b.id,
                lead_user_id=self.sales_a.id, lead_type='opportunity'))
        with self._refused(
                ValidationError,
                "You can only route Pages of an account you can access."):
            self.Page.with_user(self.admin_a).create(self._page_vals(
                'PGR4B', account_id=self._account_b().id,
                company_id=self.co_a.id, lead_user_id=self.sales_a.id,
                lead_type='opportunity'))

    def test_admin_sets_routing(self):
        self.page.with_user(self.admin_a).write({
            'lead_user_id': self.sales_a.id,
            'lead_team_id': self.team_a.id,
            'lead_type': 'opportunity',
        })
        self.assertEqual(self.page.lead_user_id, self.sales_a)
        self.assertEqual(self.page.lead_team_id, self.team_a)
        self.assertEqual(self.page.lead_type, 'opportunity')
        self.assertFalse(self.page.routing_issue)

    def test_meta_user_cannot_change_routing(self):
        meta_user = self._meta_user()
        with self.assertRaises(AccessError):
            self.page.with_user(meta_user).write({'lead_type': 'opportunity'})
        self.assertEqual(self.page.lead_type, 'lead')

    # -- save-time refusals ---------------------------------------------- #
    def test_portal_user_refused(self):
        portal = new_test_user(
            self.env, 'pr_portal', groups='base.group_portal',
            company_id=self.co_a.id, company_ids=[Command.set([self.co_a.id])])
        self.assertTrue(portal.share)
        for page in (self.page.with_user(self.admin_a), self.page.sudo()):
            with self.subTest(su=page.env.su), \
                    self._refused(ValidationError,
                                           "isn't an internal user"):
                page.write({'lead_user_id': portal.id})
        self.assertFalse(self.page.lead_user_id)

    def test_archived_user_refused(self):
        user = self._internal_user('pr_archived', [self.co_a])
        user.sudo().write({'active': False})
        for page in (self.page.with_user(self.admin_a), self.page.sudo()):
            with self.subTest(su=page.env.su), \
                    self._refused(ValidationError, "is archived"):
                page.write({'lead_user_id': user.id})
        self.assertFalse(self.page.lead_user_id)

    def test_archived_team_refused(self):
        self.team_a.sudo().write({'active': False})
        for page in (self.page.with_user(self.admin_a), self.page.sudo()):
            with self.subTest(su=page.env.su), \
                    self._refused(ValidationError, "is archived"):
                page.write({'lead_team_id': self.team_a.id})
        self.assertFalse(self.page.lead_team_id)

    def test_user_of_other_company_refused(self):
        user_b = self._internal_user('pr_user_b', [self.co_b])
        with self._refused(ValidationError, "can't work in"):
            self.page.with_user(self.admin_a).write({'lead_user_id': user_b.id})
        self.assertFalse(self.page.lead_user_id)

    def test_team_of_other_company_refused(self):
        with self._refused(ValidationError,
                                    "belongs to another company"):
            self.page.with_user(self.admin_a).write(
                {'lead_team_id': self.team_b.id})
        self.assertFalse(self.page.lead_team_id)

    def test_team_without_company_saved(self):
        shared = self._team('Routing Shared', False)
        self.page.with_user(self.admin_a).write({'lead_team_id': shared.id})
        self.assertEqual(self.page.lead_team_id, shared)
        self.assertFalse(self.page.routing_issue)

    def test_superuser_out_of_company_refused(self):
        user_b = self._internal_user('pr_user_b_su', [self.co_b])
        with self._refused(ValidationError, "can't work in"):
            self.page.sudo().write({'lead_user_id': user_b.id})
        with self._refused(ValidationError,
                                    "belongs to another company"):
            self.page.sudo().write({'lead_team_id': self.team_b.id})
        with self._refused(ValidationError, "can't work in"):
            self.Page.sudo().create(self._page_vals(
                'PGR5', lead_user_id=user_b.id))

    def test_user_without_sales_rights_refused(self):
        user = self._internal_user('pr_no_sales', [self.co_a],
                                   groups='base.group_user')
        self.assertFalse(user.share)
        for page in (self.page.with_user(self.admin_a), self.page.sudo()):
            with self.subTest(su=page.env.su), \
                    self._refused(ValidationError,
                                  "doesn't have Sales rights"):
                page.write({'lead_user_id': user.id})
        with self._refused(ValidationError, "doesn't have Sales rights"):
            self.Page.sudo().create(self._page_vals(
                'PGR6', lead_user_id=user.id))
        self.assertFalse(self.page.lead_user_id)

    def test_implied_sales_rights_accepted(self):
        # All-leads access implies the plain sales group.
        user = self._internal_user(
            'pr_all_leads', [self.co_a],
            groups='base.group_user,sales_team.group_sale_salesman_all_leads')
        self.page.with_user(self.admin_a).write({'lead_user_id': user.id})
        self.assertEqual(self.page.lead_user_id, user)
        self.assertFalse(self.page.routing_issue)

    # -- defaults that stop fitting later -------------------------------- #
    def _route(self, user=None, team=None):
        vals = {}
        if user:
            vals['lead_user_id'] = user.id
        if team:
            vals['lead_team_id'] = team.id
        self.page.with_user(self.admin_a).write(vals)
        self.assertFalse(self.page.routing_issue)

    def test_issue_when_user_archived(self):
        user = self._internal_user('pr_later_archived', [self.co_a])
        self._route(user=user)
        user.sudo().write({'active': False})
        self.assertIn("is archived", self.page.routing_issue)
        self.assertIn(user.name, self.page.routing_issue)

    def test_issue_when_user_leaves_company(self):
        user = self._internal_user('pr_later_moved', [self.co_a, self.co_b])
        self._route(user=user)
        user.sudo().write({'company_id': self.co_b.id,
                           'company_ids': [Command.set([self.co_b.id])]})
        self.assertIn("can't work in", self.page.routing_issue)
        self.assertIn(self.co_a.name, self.page.routing_issue)

    def test_issue_when_team_archived(self):
        self._route(team=self.team_a)
        self.team_a.sudo().write({'active': False})
        self.assertIn("is archived", self.page.routing_issue)
        self.assertIn(self.team_a.name, self.page.routing_issue)

    def test_issue_when_team_moves_company(self):
        self._route(team=self.team_a)
        self.team_a.sudo().write({'company_id': self.co_b.id})
        self.assertIn("belongs to another company", self.page.routing_issue)

    def test_issue_when_user_loses_sales_rights(self):
        user = self._internal_user('pr_later_no_sales', [self.co_a])
        self._route(user=user)
        self._drop_sales_rights(user)
        issue = self.page.routing_issue
        self.assertIn("no longer has Sales rights", issue)
        self.assertIn(user.name, issue)

    def test_issue_team_without_leads_on_lead_page(self):
        team = self._team('Routing No Leads', self.co_a, use_leads=False)
        page = self.page.with_user(self.admin_a)
        page.write({'lead_type': 'lead', 'lead_team_id': team.id})
        self.assertEqual(self.page.lead_team_id, team)
        issue = self.page.routing_issue
        self.assertIn("doesn't use leads", issue)
        self.assertIn(team.name, issue)
        # Creating opportunities instead clears it.
        page.write({'lead_type': 'opportunity'})
        self.assertFalse(self.page.routing_issue)

    def test_issue_team_without_pipeline_on_opportunity_page(self):
        team = self._team('Routing No Pipeline', self.co_a,
                          use_opportunities=False)
        page = self.page.with_user(self.admin_a)
        page.write({'lead_type': 'opportunity', 'lead_team_id': team.id})
        self.assertEqual(self.page.lead_team_id, team)
        self.assertIn("doesn't use the pipeline", self.page.routing_issue)
        page.write({'lead_type': 'lead'})
        self.assertFalse(self.page.routing_issue)

    def test_issue_when_team_stops_using_leads(self):
        self.page.with_user(self.admin_a).write({'lead_type': 'lead'})
        self._route(team=self.team_a)
        self.team_a.sudo().write({'use_leads': False})
        self.assertIn("doesn't use leads", self.page.routing_issue)

    def test_two_issues_two_sentences(self):
        user = self._internal_user('pr_two_issues', [self.co_a])
        self._route(user=user, team=self.team_a)
        user.sudo().write({'active': False})
        self.team_a.sudo().write({'company_id': self.co_b.id})
        issue = self.page.routing_issue
        self.assertEqual(issue.count("New leads from this Page"), 2)
        self.assertIn("someone else. The default sales team", issue)
        self.assertTrue(issue.startswith("The default salesperson"))

    def test_stale_default_does_not_block_other_routing_writes(self):
        user = self._internal_user('pr_stale', [self.co_a])
        self._route(user=user)
        user.sudo().write({'active': False})
        page = self.page.with_user(self.admin_a)
        page.write({'lead_type': 'opportunity'})
        page.write({'lead_team_id': self.team_a.id})
        self.assertEqual(self.page.lead_type, 'opportunity')
        self.assertEqual(self.page.lead_team_id, self.team_a)
        self.assertEqual(self.page.lead_user_id, user)
        self.assertIn("is archived", self.page.routing_issue)

    def test_company_change_warns_instead_of_refusing(self):
        self._route(user=self.sales_a, team=self.team_a)
        self.page.with_user(self.admin_ab).write({'company_id': self.co_b.id})
        self.assertEqual(self.page.lead_company_id, self.co_b)
        issue = self.page.routing_issue
        self.assertIn("can't work in %s" % self.co_b.name, issue)
        self.assertIn("belongs to another company", issue)

    # -- views ----------------------------------------------------------- #
    def test_form_shows_routing_to_meta_admin(self):
        arch = self._arch(self.admin_a)
        group = arch.xpath("//group[@name='lead_routing']")
        self.assertEqual(len(group), 1)
        names = group[0].xpath(".//field/@name")
        self.assertEqual(names, ['lead_type', 'lead_user_id', 'lead_team_id'])
        alert = arch.xpath(
            "//div[contains(@class, 'alert-warning')]"
            "/field[@name='routing_issue']")
        self.assertEqual(len(alert), 1)
        # The routing domains read lead_company_id, so it has to be in the
        # view even for a user without the multi-company group.
        self.assertFalse(self.admin_a.has_group('base.group_multi_company'))
        self.assertTrue(arch.xpath("//field[@name='lead_company_id']"))

    def test_form_hides_routing_from_meta_user(self):
        arch = self._arch(self._meta_user())
        self.assertFalse(arch.xpath("//group[@name='lead_routing']"))
        for fname in ('lead_user_id', 'lead_team_id', 'lead_type',
                      'routing_issue'):
            self.assertFalse(arch.xpath("//field[@name='%s']" % fname), fname)

    def test_list_warns_on_routing_issue(self):
        arch = self._arch(self.admin_a, 'list')
        self.assertEqual(arch.get('decoration-warning'), 'routing_issue')
        self.assertTrue(arch.xpath("//field[@name='routing_issue']"))
        self.assertEqual(
            arch.xpath("//field[@name='lead_user_id']/@optional"), ['show'])

    def test_form_domains_offer_lead_company_staff(self):
        arch = self._arch(self.admin_a)
        ctx = {'lead_company_id': self.co_a.id}
        # The user domain is on the field, not in the view.
        self.assertFalse(arch.xpath("//field[@name='lead_user_id']/@domain"))
        user_domain = safe_eval(self._user_domain(self.admin_a), ctx)
        team_domain = safe_eval(
            arch.xpath("//field[@name='lead_team_id']/@domain")[0], ctx)
        portal = new_test_user(
            self.env, 'pr_portal_dom', groups='base.group_portal',
            company_id=self.co_a.id, company_ids=[Command.set([self.co_a.id])])
        user_b = self._internal_user('pr_user_b_dom', [self.co_b])
        users = self.env['res.users'].search(user_domain)
        self.assertIn(self.sales_a, users)
        self.assertNotIn(portal, users)
        self.assertNotIn(user_b, users)
        shared = self._team('Routing Shared Dom', False)
        teams = self.env['crm.team'].search(team_domain)
        self.assertIn(self.team_a, teams)
        self.assertIn(shared, teams)
        self.assertNotIn(self.team_b, teams)

    def test_salesperson_domain_needs_sales_group(self):
        domain = self._user_domain(self.admin_a)
        self.assertIn("groups_id", domain)
        self.assertIn(
            str(self.env.ref('sales_team.group_sale_salesman').id), domain)

    def test_salesperson_domain_behaviour(self):
        domain = safe_eval(self._user_domain(self.admin_a),
                           {'lead_company_id': self.co_a.id})
        direct = self._internal_user('pr_dom_direct', [self.co_a])
        implied = self._internal_user(
            'pr_dom_implied', [self.co_a],
            groups='base.group_user,sales_team.group_sale_salesman_all_leads')
        plain = self._internal_user('pr_dom_plain', [self.co_a],
                                    groups='base.group_user')
        portal = new_test_user(
            self.env, 'pr_dom_portal', groups='base.group_portal',
            company_id=self.co_a.id, company_ids=[Command.set([self.co_a.id])])
        users = self.env['res.users'].with_user(self.admin_a).search(domain)
        self.assertIn(direct, users)
        self.assertIn(implied, users)
        self.assertNotIn(plain, users)
        self.assertNotIn(portal, users)

    def test_form_edit_as_meta_admin(self):
        with Form(self.page.with_user(self.admin_a)) as form:
            form.lead_type = 'opportunity'
            form.lead_user_id = self.sales_a
            form.lead_team_id = self.team_a
            self.assertFalse(form.routing_issue)
        self.assertEqual(self.page.lead_type, 'opportunity')
        self.assertEqual(self.page.lead_user_id, self.sales_a)
        self.assertEqual(self.page.lead_team_id, self.team_a)


@tagged('post_install', '-at_install')
class TestPageRoutingIngest(MultiCompanyFixtureMixin, IngestFixtureMixin,
                            _RoutingPathsMixin, TransactionCase):
    """The Page's routing on the leads ingest creates or matches."""

    def setUp(self):
        super().setUp()
        self.team_a = self._team('Ingest Team A', self.co_a)
        self.team_x = self._team('Ingest Team X', self.co_a)

    # -- helpers --------------------------------------------------------- #
    def _team(self, name, company, members=()):
        return self.env['crm.team'].create({
            'name': name, 'company_id': company.id if company else False,
            'use_leads': True, 'use_opportunities': True,
            'member_ids': [Command.link(u.id) for u in members]})

    def _internal_user(self, login, companies):
        return new_test_user(
            self.env, login,
            groups='base.group_user,sales_team.group_sale_salesman',
            company_id=companies[0].id,
            company_ids=[Command.set([c.id for c in companies])])

    def _set_routing(self, **vals):
        # Saved as superuser: the save check still applies, the admin gate
        # doesn't get in the way.
        self.page.sudo().write(vals)

    def _payload(self, leadgen_id, email):
        return self._fake_lead(id=leadgen_id, field_data=[
            {'name': 'full_name', 'values': ['Jane Doe']},
            {'name': 'email', 'values': [email]},
        ])

    def _ingest(self, leadgen_id, email=None):
        email = email or '%s@routing.test' % leadgen_id.lower()
        with self._patch_graph(fetch_lead=self._payload(leadgen_id, email)), \
                mock.patch.object(ingest_module, '_logger') as logger:
            lead = self.Ingest.ingest_leadgen(self.page, leadgen_id, 'manual')
        self._warnings = [c.args[0] % c.args[1:]
                          for c in logger.warning.call_args_list]
        return lead

    def _routing_warnings(self):
        return [line for line in self._warnings if 'default sal' in line]

    def _assert_one_warning(self, kind, record, *secrets):
        lines = self._routing_warnings()
        self.assertEqual(len(lines), 1, lines)
        self.assertIn("page id=%s default %s id=%s doesn't fit company id=%s"
                      % (self.page.id, kind, record.id, self.co_a.id),
                      lines[0])
        for secret in secrets:
            self.assertNotIn(secret, lines[0])

    # -- the snapshot ---------------------------------------------------- #
    def test_snapshot_keys(self):
        self._set_routing(lead_type='opportunity', lead_user_id=self.sales_a.id,
                          lead_team_id=self.team_a.id)
        routing = self.Ingest._page_routing_vals(self.page, self.co_a)
        self.assertEqual(routing, {'user_id': self.sales_a.id,
                                   'team_id': self.team_a.id,
                                   'type': 'opportunity'})
        self.assertEqual(
            self.Ingest._page_routing_vals(self.env['meta.page'], self.co_a),
            {'type': 'lead'})

    def test_snapshot_checks_only_what_is_asked(self):
        user = self._internal_user('ri_snap_archived', [self.co_a])
        self._set_routing(lead_user_id=user.id, lead_team_id=self.team_a.id)
        user.sudo().write({'active': False})
        with mock.patch.object(ingest_module, '_logger') as logger:
            routing = self.Ingest._page_routing_vals(
                self.page, self.co_a, fnames=('team_id',))
        self.assertEqual(routing, {'team_id': self.team_a.id})
        self.assertFalse(logger.warning.call_args_list)

    # -- new leads -------------------------------------------------------- #
    def test_no_defaults_as_superuser_unassigned(self):
        lead = self._ingest('RI1')
        self.assertFalse(lead.user_id)
        self.assertEqual(lead.type, 'lead')

    def test_no_defaults_manual_ingest_by_admin_unassigned(self):
        # admin_a works in the lead's company, so crm would happily make
        # them the salesperson.
        self.assertIn(self.co_a, self.admin_a.company_ids)
        wizard = self.env['meta.ingest.leadgen'].with_user(self.admin_a).create(
            {'page_id': self.page.id, 'leadgen_id': 'RI2'})
        with self._patch_graph(
                fetch_lead=self._payload('RI2', 'ri2@routing.test')):
            wizard.action_ingest()
        lead = self.env['crm.lead'].sudo().search(
            [('meta_leadgen_id', '=', 'RI2')])
        self.assertEqual(len(lead), 1)
        self.assertEqual(lead.create_uid, self.admin_a)
        self.assertFalse(lead.user_id)

    def test_default_salesperson_gets_their_team(self):
        team_s = self._team('Ingest Team S', self.co_a, members=[self.sales_a])
        self._set_routing(lead_user_id=self.sales_a.id)
        lead = self._ingest('RI3')
        self.assertEqual(lead.user_id, self.sales_a)
        self.assertEqual(lead.team_id, team_s)

    def test_default_team_only(self):
        self._set_routing(lead_team_id=self.team_a.id)
        lead = self._ingest('RI4')
        self.assertEqual(lead.team_id, self.team_a)
        self.assertFalse(lead.user_id)

    def test_salesperson_outside_team_both_kept(self):
        # sales_a's own team would win if crm worked the team out again.
        self._team('Ingest Team Own', self.co_a, members=[self.sales_a])
        self.assertNotIn(self.sales_a, self.team_x.member_ids)
        self._set_routing(lead_user_id=self.sales_a.id,
                          lead_team_id=self.team_x.id)
        lead = self._ingest('RI5')
        self.assertEqual(lead.user_id, self.sales_a)
        self.assertEqual(lead.team_id, self.team_x)

    def test_opportunity_page_uses_team_first_stage(self):
        self._set_routing(lead_type='opportunity', lead_team_id=self.team_a.id)
        lead = self._ingest('RI6')
        self.assertEqual(lead.type, 'opportunity')
        self.assertEqual(lead.team_id, self.team_a)
        stage = self.env['crm.lead']._stage_find(
            team_id=self.team_a.id, domain=[('fold', '=', False)])
        self.assertTrue(stage)
        self.assertEqual(lead.stage_id, stage)

    # -- defaults that stopped fitting ------------------------------------ #
    def test_archived_salesperson_skipped(self):
        user = self._internal_user('ri_archived', [self.co_a])
        self._set_routing(lead_user_id=user.id)
        user.sudo().write({'active': False})
        lead = self._ingest('RI7')
        self.assertTrue(lead.exists())
        self.assertFalse(lead.user_id)
        self._assert_one_warning('salesperson', user, user.name, user.login)

    def test_salesperson_out_of_company_skipped(self):
        user = self._internal_user('ri_moved', [self.co_a, self.co_b])
        self._set_routing(lead_user_id=user.id)
        user.sudo().write({'company_id': self.co_b.id,
                           'company_ids': [Command.unlink(self.co_a.id)]})
        self.assertNotIn(self.co_a, user.company_ids)
        lead = self._ingest('RI8')
        self.assertFalse(lead.user_id)
        self._assert_one_warning('salesperson', user, user.name, user.login)

    def test_salesperson_without_sales_rights_skipped(self):
        user = self._internal_user('ri_no_sales', [self.co_a])
        self._set_routing(lead_user_id=user.id)
        sales = self.env.ref('sales_team.group_sale_salesman')
        user.sudo().write({'groups_id': [Command.unlink(sales.id)]})
        self.assertFalse(user.has_group('sales_team.group_sale_salesman'))
        lead = self._ingest('RI12')
        self.assertTrue(lead.exists())
        self.assertFalse(lead.user_id)
        self._assert_one_warning('salesperson', user, user.name, user.login)
        self.assertIn("has no Sales rights", self._routing_warnings()[0])

    def test_team_type_mismatch_still_routes(self):
        # Only a warning on the Page: ingest keeps the team.
        team = self._team('Ingest No Leads', self.co_a)
        team.write({'use_leads': False})
        self._set_routing(lead_type='lead', lead_team_id=team.id)
        self.assertIn("doesn't use leads", self.page.routing_issue)
        lead = self._ingest('RI13')
        self.assertEqual(lead.team_id, team)
        self.assertEqual(lead.type, 'lead')
        self.assertFalse(self._routing_warnings())

    def test_archived_team_skipped(self):
        self._set_routing(lead_team_id=self.team_a.id)
        self.team_a.sudo().write({'active': False})
        lead = self._ingest('RI9')
        self.assertTrue(lead.exists())
        self.assertNotEqual(lead.team_id, self.team_a)
        self._assert_one_warning('sales team', self.team_a, self.team_a.name)

    def test_team_moved_company_skipped(self):
        self._set_routing(lead_team_id=self.team_a.id)
        self.team_a.sudo().write({'company_id': self.co_b.id})
        lead = self._ingest('RI10')
        self.assertTrue(lead.exists())
        self.assertNotEqual(lead.team_id, self.team_a)
        self.assertEqual(lead.company_id, self.co_a)
        self._assert_one_warning('sales team', self.team_a, self.team_a.name)

    def test_fitting_defaults_log_nothing(self):
        self._set_routing(lead_user_id=self.sales_a.id,
                          lead_team_id=self.team_a.id)
        self._ingest('RI11')
        self.assertFalse(self._routing_warnings())

    # -- matched leads ----------------------------------------------------- #
    def _match(self, email, **vals):
        values = {'name': 'Open %s' % email, 'email_from': email,
                  'company_id': self.co_a.id, 'user_id': False}
        values.update(vals)
        return self.env['crm.lead'].create(values)

    def test_match_without_owner_gets_page_routing(self):
        match = self._match('rm1@routing.test')
        self._set_routing(lead_user_id=self.sales_a.id,
                          lead_team_id=self.team_a.id)
        lead = self._ingest('RM1', 'rm1@routing.test')
        self.assertEqual(lead, match)
        self.assertEqual(lead.user_id, self.sales_a)
        self.assertEqual(lead.team_id, self.team_a)

    def test_match_owner_never_reassigned(self):
        match = self._match('rm2@routing.test', user_id=self.sales_mgr_a.id,
                            team_id=self.team_x.id)
        self._set_routing(lead_user_id=self.sales_a.id,
                          lead_team_id=self.team_a.id)
        lead = self._ingest('RM2', 'rm2@routing.test')
        self.assertEqual(lead, match)
        self.assertEqual(lead.user_id, self.sales_mgr_a)
        self.assertEqual(lead.team_id, self.team_x)
        self.assertEqual(lead.meta_leadgen_id, 'RM2')

    def test_match_owner_no_warning_for_stale_default(self):
        user = self._internal_user('rm_stale', [self.co_a])
        self._set_routing(lead_user_id=user.id)
        user.sudo().write({'active': False})
        self._match('rm3@routing.test', user_id=self.sales_mgr_a.id,
                    team_id=self.team_x.id)
        lead = self._ingest('RM3', 'rm3@routing.test')
        self.assertEqual(lead.user_id, self.sales_mgr_a)
        self.assertFalse(self._routing_warnings())

    def test_match_with_team_gets_salesperson_keeps_team(self):
        team_y = self._team('Ingest Team Y', self.co_a)
        self.assertNotIn(self.sales_a, team_y.member_ids)
        # sales_a's own team would win if crm worked the team out again.
        self._team('Ingest Team Own', self.co_a, members=[self.sales_a])
        match = self._match('rm4@routing.test', team_id=team_y.id)
        self._set_routing(lead_user_id=self.sales_a.id,
                          lead_team_id=self.team_a.id)
        lead = self._ingest('RM4', 'rm4@routing.test')
        self.assertEqual(lead, match)
        self.assertEqual(lead.user_id, self.sales_a)
        self.assertEqual(lead.team_id, team_y)

    def test_match_type_never_changes(self):
        self._set_routing(lead_type='opportunity')
        match = self._match('rm5@routing.test', type='lead')
        lead = self._ingest('RM5', 'rm5@routing.test')
        self.assertEqual(lead, match)
        self.assertEqual(lead.type, 'lead')
        self._set_routing(lead_type='lead')
        opportunity = self._match('rm6@routing.test', type='opportunity')
        lead = self._ingest('RM6', 'rm6@routing.test')
        self.assertEqual(lead, opportunity)
        self.assertEqual(lead.type, 'opportunity')

    def test_match_stale_default_left_empty(self):
        user = self._internal_user('rm_stale_blank', [self.co_a])
        self._set_routing(lead_user_id=user.id)
        user.sudo().write({'active': False})
        match = self._match('rm7@routing.test')
        lead = self._ingest('RM7', 'rm7@routing.test')
        self.assertEqual(lead, match)
        self.assertFalse(lead.user_id)
        self.assertEqual(lead.meta_leadgen_id, 'RM7')
        self._assert_one_warning('salesperson', user, user.name, user.login)

    # -- every ingest path ------------------------------------------------- #
    def _lead_of(self, leadgen_id):
        return self.env['crm.lead'].sudo().with_context(
            active_test=False).search([('meta_leadgen_id', '=', leadgen_id)])

    def _assert_routed(self, leadgen_id):
        lead = self._lead_of(leadgen_id)
        self.assertEqual(len(lead), 1)
        self.assertEqual(lead.user_id, self.sales_a)
        return lead

    def test_webhook_drain_routes(self):
        self._set_routing(lead_user_id=self.sales_a.id)
        self._drain(self._payload('RP1', 'rp1@routing.test'), 'RP1')
        self._assert_routed('RP1')

    def test_backfill_sweep_routes(self):
        self._set_routing(lead_user_id=self.sales_a.id)
        self._sweep(self._payload('RP2', 'rp2@routing.test'), 'RP2')
        self._assert_routed('RP2')

    def test_sync_log_retry_routes(self):
        self._set_routing(lead_user_id=self.sales_a.id)
        self._retry_failed_row(self._payload('RP3', 'rp3@routing.test'),
                               'RP3', self.admin_a)
        lead = self._assert_routed('RP3')
        self.assertEqual(lead.create_uid, self.admin_a)

    def test_restore_routes(self):
        restorer = new_test_user(
            self.env, 'ri_restorer',
            groups='base.group_user,sales_team.group_sale_salesman_all_leads,'
                   'meta_lead_ads.group_meta_admin',
            company_id=self.co_a.id, company_ids=[Command.set([self.co_a.id])])
        self._set_routing(lead_user_id=self.sales_a.id)
        self._restore(self._payload('RP4', 'rp4@routing.test'), 'RP4',
                      restorer)
        lead = self._assert_routed('RP4')
        self.assertEqual(lead.create_uid, restorer)
