# Meta Lead Ads Integration for Odoo — © 2026 CoreSys Builders. All rights reserved.
# Licensed under the Odoo Proprietary License v1.0 (OPL-1).
# Unauthorized copying, redistribution, or resale of this software, in whole or in
# part, via any medium, is strictly prohibited and constitutes a license violation.
# OPL-1: https://www.odoo.com/documentation/18.0/legal/licenses.html#odoo-apps

from odoo.tests.common import TransactionCase, tagged
from odoo.exceptions import AccessError


@tagged('post_install', '-at_install')
class TestSecurity(TransactionCase):
    def setUp(self):
        super().setUp()
        self.user_group = self.env.ref('meta_lead_ads.group_meta_user')
        self.admin_group = self.env.ref('meta_lead_ads.group_meta_admin')
        # Without base.group_user Odoo makes these share (portal) users and
        # the ACL tests can pass for the wrong reason. group_meta_admin
        # already implies group_meta_user.
        base_internal = self.env.ref('base.group_user')
        self.meta_user = self.env['res.users'].create({
            'name': 'Meta U', 'login': 'meta_u',
            'groups_id': [(6, 0, [base_internal.id, self.user_group.id])]})
        self.meta_admin = self.env['res.users'].create({
            'name': 'Meta A', 'login': 'meta_a',
            'groups_id': [(6, 0, [base_internal.id, self.admin_group.id])]})

    def test_meta_user_readonly(self):
        self.env['meta.account'].create({'name': 'X', 'account_id': 'A'})
        self.env['meta.account'].with_user(self.meta_user).search([])
        with self.assertRaises(AccessError):
            self.env['meta.account'].with_user(self.meta_user).create(
                {'name': 'Y', 'account_id': 'B'})

    def test_meta_admin_full_crud(self):
        acc = self.env['meta.account'].with_user(self.meta_admin).create(
            {'name': 'Z', 'account_id': 'C'})
        acc.with_user(self.meta_admin).write({'name': 'Z2'})
        acc.with_user(self.meta_admin).unlink()

    def test_admin_implies_user(self):
        self.assertIn(self.user_group, self.meta_admin.groups_id)

    # access_token and app_secret are restricted to group_meta_admin.
    # App ID is not a secret and stays readable.

    def test_non_admin_cannot_read_account_token(self):
        acc = self.env['meta.account'].create({
            'name': 'X', 'account_id': 'A', 'app_id': '100',
            'access_token': 'SECRET_TOK', 'app_secret': 'SECRET_APP'})
        rec = acc.with_user(self.meta_user).read(['name', 'app_id'])
        self.assertIn('name', rec[0])
        self.assertIn('app_id', rec[0])
        # Reading a groups= field by name raises; it is only dropped
        # silently when reading all fields.
        with self.assertRaises(AccessError):
            acc.with_user(self.meta_user).read(['access_token'])
        with self.assertRaises(AccessError):
            acc.with_user(self.meta_user).read(['app_secret'])

    def test_admin_can_read_account_token(self):
        acc = self.env['meta.account'].create({
            'name': 'X', 'account_id': 'A', 'app_id': '100',
            'access_token': 'SECRET_TOK', 'app_secret': 'SECRET_APP'})
        rec = acc.with_user(self.meta_admin).read(['access_token'])
        self.assertEqual(rec[0]['access_token'], 'SECRET_TOK')

    def test_non_admin_cannot_read_page_token(self):
        acc = self.env['meta.account'].create({'name': 'X', 'account_id': 'A'})
        page = self.env['meta.page'].create({
            'name': 'P', 'page_id': 'PG1', 'account_id': acc.id,
            'access_token': 'PAGE_SECRET'})
        rec = page.with_user(self.meta_user).read(['name'])
        self.assertIn('name', rec[0])
        with self.assertRaises(AccessError):
            page.with_user(self.meta_user).read(['access_token'])

    # A groups= field raises AccessError on read, so the whole Meta settings
    # block is hidden from users outside group_meta_admin. Checked on the
    # resolved view arch, not the raw ir.ui.view.

    def test_non_meta_admin_cannot_see_settings_section(self):
        """The Meta settings block shows for a Meta admin but not a plain system admin."""
        base_internal = self.env.ref('base.group_user')
        sys_group = self.env.ref('base.group_system')
        sys_only = self.env['res.users'].create({
            'name': 'Sys Only', 'login': 'sec_sysonly',
            'groups_id': [(6, 0, [base_internal.id, sys_group.id])]})
        Settings = self.env['res.config.settings']
        admin_arch = Settings.with_user(self.meta_admin).get_view()['arch']
        self.assertIn('lead_name_template', admin_arch)
        self.assertIn('meta_lead_ads', admin_arch)
        sys_arch = Settings.with_user(sys_only).get_view()['arch']
        self.assertNotIn('lead_name_template', sys_arch)

    def test_non_system_meta_admin_cannot_mutate_foreign_setting(self):
        """A Meta admin without Settings rights saves the Meta template but no other module's setting.

        set_values() only calls super() for base.group_system users, so the
        settings ACL we grant can't be used to change unrelated parameters.
        """
        ICP = self.env['ir.config_parameter'].sudo()
        Settings = self.env['res.config.settings']
        LEAD_NAME_PARAM = 'meta_lead_ads.lead_name_template'

        # Any non-Meta boolean config parameter will do; avoid depending on
        # a particular module.
        foreign = None
        for fname, f in Settings._fields.items():
            cp = getattr(f, 'config_parameter', None)
            if cp and f.type == 'boolean' and not cp.startswith('meta_lead_ads.'):
                foreign = (fname, cp)
                break

        create_vals = {'lead_name_template': 'X {form_name}'}
        if foreign:
            fname, cp = foreign
            ICP.set_param(cp, 'meta_test_baseline')   # sentinel, not 'True'/'False'
            create_vals[fname] = True                 # try to flip it

        Settings.with_user(self.meta_admin).create(create_vals).set_values()

        self.assertEqual(ICP.get_param(LEAD_NAME_PARAM), 'X {form_name}')
        if foreign:
            self.assertEqual(ICP.get_param(foreign[1]), 'meta_test_baseline')
