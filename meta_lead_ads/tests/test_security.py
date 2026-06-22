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
        # base.group_user makes these realistic internal (non-share) users;
        # without it Odoo sets share=True (portal semantics) and ACL tests can
        # pass for the wrong reasons. group_meta_admin implies group_meta_user
        # via implied_ids, so the admin auto-gains it -- no need to add it.
        base_internal = self.env.ref('base.group_user')
        self.meta_user = self.env['res.users'].create({
            'name': 'Meta U', 'login': 'meta_u',
            'groups_id': [(6, 0, [base_internal.id, self.user_group.id])]})
        self.meta_admin = self.env['res.users'].create({
            'name': 'Meta A', 'login': 'meta_a',
            'groups_id': [(6, 0, [base_internal.id, self.admin_group.id])]})

    def test_meta_user_readonly(self):
        self.env['meta.account'].create({'name': 'X', 'account_id': 'A'})
        # read allowed
        self.env['meta.account'].with_user(self.meta_user).search([])
        # create denied
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

    # ---- field-level groups= on token / app_secret -----------------------
    # The secret fields (access_token / app_secret) carry
    # groups='meta_lead_ads.group_meta_admin'. App ID is NOT secret — it must
    # stay readable.

    def test_non_admin_cannot_read_account_token(self):
        acc = self.env['meta.account'].create({
            'name': 'X', 'account_id': 'A', 'app_id': '100',
            'access_token': 'SECRET_TOK', 'app_secret': 'SECRET_APP'})
        # Non-secret fields stay readable; App ID is NOT secret.
        rec = acc.with_user(self.meta_user).read(['name', 'app_id'])
        self.assertIn('name', rec[0])
        self.assertIn('app_id', rec[0])
        # Odoo 18: an EXPLICIT read() of a groups=-gated field RAISES AccessError
        # (it is only silently dropped on an implicit read-all / a group-gated
        # view) — mirrors the test_sync_log_security pattern.
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
        # Non-secret field readable; explicit read of the gated token RAISES on
        # Odoo 18 (field-level groups=).
        rec = page.with_user(self.meta_user).read(['name'])
        self.assertIn('name', rec[0])
        with self.assertRaises(AccessError):
            page.with_user(self.meta_user).read(['access_token'])

    # ---- Meta Settings section gated to group_meta_admin -------------------
    # A groups=-gated FIELD read raises AccessError on Odoo 18 — so the whole
    # Meta <app> section is gated, and a non-Meta system admin must never render
    # the Meta fields. Asserted via the resolved view architecture, not raw
    # ir.ui.view XML.

    def test_non_meta_admin_cannot_see_settings_section(self):
        """The Meta Settings <app> (and its lead_name_template field) renders for
        a group_meta_admin user but NOT for a base.group_system-only admin who is
        not in group_meta_admin."""
        base_internal = self.env.ref('base.group_user')
        sys_group = self.env.ref('base.group_system')
        sys_only = self.env['res.users'].create({
            'name': 'Sys Only', 'login': 'sec_sysonly',
            'groups_id': [(6, 0, [base_internal.id, sys_group.id])]})
        Settings = self.env['res.config.settings']
        # Admin (group_meta_admin) sees the Meta section + field.
        admin_arch = Settings.with_user(self.meta_admin).get_view()['arch']
        self.assertIn('lead_name_template', admin_arch)
        self.assertIn('meta_lead_ads', admin_arch)
        # The non-Meta system admin's resolved arch hides the gated section.
        sys_arch = Settings.with_user(sys_only).get_view()['arch']
        self.assertNotIn('lead_name_template', sys_arch)

    # ---- the res.config.settings ACL is not a settings back door -----------

    def test_non_system_meta_admin_cannot_mutate_foreign_setting(self):
        """A group_meta_admin user who is NOT base.group_system CAN save the
        Meta template, but set_values must NOT become a back door to mutate other
        modules' global settings — super().set_values() is delegated only for a
        full Settings admin. self.meta_admin (from setUp) is a non-system Meta admin."""
        ICP = self.env['ir.config_parameter'].sudo()
        Settings = self.env['res.config.settings']
        LEAD_NAME_PARAM = 'meta_lead_ads.lead_name_template'

        # Find ANY foreign (non-Meta) boolean config-parameter settings field to
        # prove containment without coupling to a specific dependency.
        foreign = None
        for fname, f in Settings._fields.items():
            cp = getattr(f, 'config_parameter', None)
            if cp and f.type == 'boolean' and not cp.startswith('meta_lead_ads.'):
                foreign = (fname, cp)
                break

        create_vals = {'lead_name_template': 'X {form_name}'}
        if foreign:
            fname, cp = foreign
            ICP.set_param(cp, 'meta_wr01_baseline')   # sentinel != 'True'/'False'
            create_vals[fname] = True                 # attempt to flip it via super()

        Settings.with_user(self.meta_admin).create(create_vals).set_values()

        # Positive: the non-system Meta admin DID persist the Meta template.
        self.assertEqual(ICP.get_param(LEAD_NAME_PARAM), 'X {form_name}')
        if foreign:
            # Containment: the foreign setting is untouched because super() was
            # skipped for this non-system Meta admin.
            self.assertEqual(ICP.get_param(foreign[1]), 'meta_wr01_baseline')
