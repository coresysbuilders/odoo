# Meta Lead Ads Integration for Odoo — © 2026 CoreSys Builders. All rights reserved.
# Licensed under the Odoo Proprietary License v1.0 (OPL-1).
# Unauthorized copying, redistribution, or resale of this software, in whole or in
# part, via any medium, is strictly prohibited and constitutes a license violation.
# OPL-1: https://www.odoo.com/documentation/20.0/legal/licenses.html#odoo-apps

from contextlib import contextmanager
from unittest import mock

from lxml import etree

from odoo.tests import Form
from odoo.tests.common import TransactionCase, tagged
from odoo.exceptions import AccessError
from odoo.tools import SQL

LEAD_NAME_PARAM = 'meta_lead_ads.lead_name_template'
SYNC_LOG_RETENTION_PARAM = 'meta_lead_ads.sync_log_retention_days'
META_ONLY_VIEW = 'meta_lead_ads.res_config_settings_view_form_meta_only'
SENTINEL = 'meta_test_baseline'   # not 'True'/'False', so any write shows


def _find_foreign_bool_param(Settings):
    """(field name, parameter key) of a boolean setting another module owns.

    Any one will do, so the tests don't depend on a particular module.
    None when there is none; callers assert it was found.
    """
    for fname, field in Settings._fields.items():
        cp = getattr(field, 'config_parameter', None)
        if cp and field.type == 'boolean' and not cp.startswith('meta_lead_ads.'):
            return fname, cp
    return None


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
            'group_ids': [(6, 0, [base_internal.id, self.user_group.id])]})
        self.meta_admin = self.env['res.users'].create({
            'name': 'Meta A', 'login': 'meta_a',
            'group_ids': [(6, 0, [base_internal.id, self.admin_group.id])]})

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
        # Odoo 19: group_ids holds only directly assigned groups; implied
        # ones show up in all_group_ids.
        self.assertIn(self.user_group, self.meta_admin.all_group_ids)

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
            'group_ids': [(6, 0, [base_internal.id, sys_group.id])]})
        Settings = self.env['res.config.settings']
        admin_arch = Settings.with_user(self.meta_admin).get_view()['arch']
        self.assertIn('lead_name_template', admin_arch)
        self.assertIn('meta_lead_ads', admin_arch)
        sys_arch = Settings.with_user(sys_only).get_view()['arch']
        self.assertNotIn('lead_name_template', sys_arch)

    def test_non_system_meta_admin_cannot_mutate_foreign_setting(self):
        """A Meta admin without Settings rights saves the Meta values through the Save button.

        Other modules' settings are saved only for users core lets save
        settings, so the settings ACL we grant can't be used to change
        unrelated parameters.
        """
        ICP = self.env['ir.config_parameter'].sudo()
        Settings = self.env['res.config.settings']

        foreign = _find_foreign_bool_param(Settings)
        self.assertIsNotNone(foreign, "no foreign boolean setting to test with")
        fname, cp = foreign
        ICP.set_str(cp, SENTINEL)

        Settings.with_user(self.meta_admin).create({
            'lead_name_template': 'X {form_name}',
            fname: True,                          # try to flip it
        }).execute()

        self.assertEqual(ICP.get_str(LEAD_NAME_PARAM), 'X {form_name}')
        self.assertEqual(ICP.get_str(cp), SENTINEL)


@tagged('post_install', '-at_install')
class TestMetaSettingsAccess(TransactionCase):
    """Who can save the Meta settings, and what else their Save touches."""

    @classmethod
    def setUpClass(cls):
        super().setUpClass()
        internal = cls.env.ref('base.group_user')
        meta_admin = cls.env.ref('meta_lead_ads.group_meta_admin')
        system = cls.env.ref('base.group_system')
        erp_manager = cls.env.ref('base.group_erp_manager')
        Users = cls.env['res.users']
        cls.meta_only = Users.create({
            'name': 'Settings Meta Only', 'login': 'settings_meta_only',
            'group_ids': [(6, 0, [internal.id, meta_admin.id])]})
        cls.sys_only = Users.create({
            'name': 'Settings Sys Only', 'login': 'settings_sys_only',
            'group_ids': [(6, 0, [internal.id, system.id])]})
        cls.erp_meta = Users.create({
            'name': 'Settings Erp Meta', 'login': 'settings_erp_meta',
            'group_ids': [(6, 0, [internal.id, erp_manager.id,
                                  meta_admin.id])]})
        cls.plain = Users.create({
            'name': 'Settings Plain', 'login': 'settings_plain',
            'group_ids': [(6, 0, [internal.id])]})
        cls.ICP = cls.env['ir.config_parameter'].sudo()
        cls.Settings = cls.env['res.config.settings']
        cls.ICP.set_str(LEAD_NAME_PARAM, 'Before {form_name}')

    def _foreign_bool_param(self):
        foreign = _find_foreign_bool_param(self.Settings)
        self.assertIsNotNone(foreign, "no foreign boolean setting to test with")
        return foreign

    @contextmanager
    def _watch_save(self):
        """Wrap set_values and _install_modules so a test sees if they ran."""
        cls = type(self.Settings)
        with mock.patch.object(cls, '_install_modules', autospec=True,
                               side_effect=cls._install_modules) as install, \
                mock.patch.object(cls, 'set_values', autospec=True,
                                  side_effect=cls.set_values) as set_values:
            yield install, set_values

    def _settings_as(self, user, vals):
        return self.Settings.with_user(user).create(vals)

    # ---- Meta admin without Administration rights ---------------------------

    def test_meta_admin_saves_meta_settings_with_save_button(self):
        """No erp_manager, no group_system: the Meta-only screen saves with execute."""
        with Form(self.Settings.with_user(self.meta_only),
                  view=META_ONLY_VIEW) as form:
            form.lead_name_template = 'Lead {form_name}'
            form.meta_sync_log_retention_days = 30
        with self._watch_save() as (install, set_values):
            action = form.record.execute()
        install.assert_not_called()
        set_values.assert_not_called()
        self.assertEqual(action, {'type': 'ir.actions.client', 'tag': 'reload'})
        self.assertEqual(self.ICP.get_str(LEAD_NAME_PARAM), 'Lead {form_name}')
        self.assertEqual(self.ICP.get_str(SYNC_LOG_RETENTION_PARAM), '30')

    def test_meta_admin_opens_and_saves_settings_like_the_web_client(self):
        """The browser also asks for display_name when it opens and saves.

        Core finds that name by reading the settings action, which only
        Administration users may do on Odoo 19. Form() never asks for it.
        """
        Settings = self.Settings.with_user(self.meta_only)
        self.assertFalse(self.env['ir.actions.act_window'].with_user(
            self.meta_only).has_access('read'))
        expected = self.env['ir.actions.act_window'].search(
            [('res_model', '=', 'res.config.settings')], limit=1).name
        spec = {'display_name': {}, 'lead_name_template': {},
                'meta_sync_log_retention_days': {}}
        opened = Settings.onchange({}, [], spec)
        self.assertEqual(opened['value']['display_name'], expected)
        saved = Settings.web_save({'lead_name_template': 'Web {form_name}',
                                   'meta_sync_log_retention_days': 40}, spec)
        self.assertEqual(saved[0]['display_name'], expected)
        settings = Settings.browse(saved[0]['id'])
        self.assertEqual(settings.execute(),
                         {'type': 'ir.actions.client', 'tag': 'reload'})
        self.assertEqual(self.ICP.get_str(LEAD_NAME_PARAM), 'Web {form_name}')
        self.assertEqual(self.ICP.get_str(SYNC_LOG_RETENTION_PARAM), '40')

    def test_meta_admin_save_ignores_other_apps_company_defaults(self):
        """Another app's field tied to the company must not block the save.

        create() writes the default of an editable related field back to
        the company. hr's module_hr_attendance does that whenever its
        default (from module state) differs from the company flag, and a
        Meta admin may not write res.company. A stored default on mail's
        email_primary_color plays that part here.
        """
        company = self.env.company
        current = company.email_primary_color
        other = '#123456' if current != '#123456' else '#654321'
        self.env['ir.default'].set(
            'res.config.settings', 'email_primary_color', other)
        self.assertEqual(self.Settings.with_user(self.sys_only).default_get(
            ['email_primary_color'])['email_primary_color'], other)
        Settings = self.Settings.with_user(self.meta_only)
        spec = {'lead_name_template': {}, 'meta_sync_log_retention_days': {}}
        Settings.onchange({}, [], spec)
        saved = Settings.web_save({'lead_name_template': 'Co {form_name}',
                                   'meta_sync_log_retention_days': 25}, spec)
        Settings.browse(saved[0]['id']).execute()
        self.assertEqual(self.ICP.get_str(LEAD_NAME_PARAM), 'Co {form_name}')
        self.assertEqual(self.ICP.get_str(SYNC_LOG_RETENTION_PARAM), '25')
        company.invalidate_recordset(['email_primary_color'])
        self.assertEqual(company.email_primary_color, current)

    def test_meta_admin_cannot_run_other_apps_inverses(self):
        """Naming another app's field in create, write or a save must do
        nothing.

        portal's Customer API Keys writes a system parameter. On Odoo 20 it
        is a config_parameter field, saved by execute() under sudo.
        """
        self.assertIn('portal_allow_api_keys', self.Settings._fields)
        key = 'portal.allow_api_keys'
        self.ICP.set_str(key, False)
        rec = self._settings_as(self.meta_only, {
            'portal_allow_api_keys': True,
            'lead_name_template': 'Own {form_name}'})
        rec.with_user(self.meta_only).write({'portal_allow_api_keys': True})
        rec.with_user(self.meta_only).execute()
        self.env.flush_all()
        self.assertFalse(self.ICP.get_bool(key))
        self.assertEqual(rec.lead_name_template, 'Own {form_name}')
        # An Access Rights user keeps the full behaviour.
        own = self._settings_as(self.sys_only, {'portal_allow_api_keys': True})
        own.with_user(self.sys_only).execute()
        self.env.flush_all()
        self.assertTrue(self.ICP.get_bool(key))

    def test_meta_admin_defaults_hold_only_meta_values(self):
        """Their record carries the Meta values and the company, no more."""
        Settings = self.Settings.with_user(self.meta_only)
        names = list(self.Settings._fields)
        mine = Settings.default_get(names)
        own = {n for n, f in self.Settings._fields.items()
               if f._module == 'meta_lead_ads'}
        # plus required plain columns, which the insert needs
        own |= {n for n, f in self.Settings._fields.items()
                if f.required and f.store and not f.related
                and not f.inverse and not f.compute}
        self.assertTrue(set(mine) <= own | {'company_id'}, set(mine) - own)
        self.assertIn('lead_name_template', mine)
        # Context defaults do not get around it.
        ctx = Settings.with_context(default_portal_allow_api_keys=True)
        self.assertNotIn('portal_allow_api_keys', ctx.default_get(names))
        # Settings admins keep everyone's defaults.
        full = self.Settings.with_user(self.sys_only).default_get(names)
        self.assertIn('portal_allow_api_keys', full)

    def test_meta_admin_record_fills_other_apps_required_columns(self):
        """Another app's required setting is a NOT NULL column here.

        sale_stock's default_picking_policy is one on production. The Meta
        admin's record gets its plain default, or create fails.
        """
        Settings = self.Settings
        name = next((
            n for n, f in sorted(Settings._fields.items())
            if f.store and not f.related and not f.inverse and not f.compute
            and f.default is not None and f._module != 'meta_lead_ads'
            and n != 'company_id' and f.type in ('selection', 'char', 'integer')
            and f.default(Settings) not in (None, False)), None)
        self.assertIsNotNone(name, "no stored setting of another app with a default")
        field = Settings._fields[name]
        self.patch(field, 'required', True)
        self.env.cr.execute(SQL('DELETE FROM res_config_settings'))
        self.env.cr.execute(SQL(
            'ALTER TABLE res_config_settings ALTER COLUMN %s SET NOT NULL',
            SQL.identifier(name)))
        mine = Settings.with_user(self.meta_only).default_get([name, 'lead_name_template'])
        self.assertEqual(mine.get(name), field.default(Settings))
        rec = self._settings_as(self.meta_only, {
            name: 'not this', 'lead_name_template': 'Own {form_name}'})
        self.assertEqual(rec[name], field.default(Settings))
        self.assertEqual(rec.lead_name_template, 'Own {form_name}')

    def test_meta_admin_save_leaves_other_settings_and_modules(self):
        fname, cp = self._foreign_bool_param()
        self.ICP.set_str(cp, SENTINEL)
        module = next(
            (m for m in self.Settings._get_classified_fields()['module']
             if m.state == 'uninstalled'), None)
        self.assertIsNotNone(module, "no uninstalled module setting to flip")
        settings = self._settings_as(self.meta_only, {
            'lead_name_template': 'Meta {form_name}',
            fname: True,
            'module_%s' % module.name: True,
        })
        with self._watch_save() as (install, set_values):
            settings.execute()
        install.assert_not_called()
        set_values.assert_not_called()
        self.assertEqual(self.ICP.get_str(cp), SENTINEL)
        module.invalidate_recordset(['state'])
        self.assertEqual(module.state, 'uninstalled')
        self.assertEqual(self.ICP.get_str(LEAD_NAME_PARAM), 'Meta {form_name}')

    def test_meta_admin_direct_set_values_refused(self):
        """A direct set_values call (RPC) saves nothing at all."""
        fname, cp = self._foreign_bool_param()
        self.ICP.set_str(cp, SENTINEL)
        settings = self._settings_as(self.meta_only, {
            'lead_name_template': 'X {form_name}', fname: True})
        with self.assertRaises(AccessError):
            settings.set_values()
        self.assertEqual(self.ICP.get_str(LEAD_NAME_PARAM), 'Before {form_name}')
        self.assertEqual(self.ICP.get_str(cp), SENTINEL)

    def test_user_without_either_group_cannot_save(self):
        settings = self.Settings.create({}).with_user(self.plain)
        with self.assertRaises(AccessError):
            settings.execute()
        with self.assertRaisesRegex(
                AccessError, 'Only administrators can change the settings'):
            settings.set_values()
        self.assertEqual(self.ICP.get_str(LEAD_NAME_PARAM), 'Before {form_name}')

    # ---- Settings admin who isn't a Meta admin -------------------------------

    def test_settings_admin_saves_other_setting(self):
        fname, cp = self._foreign_bool_param()
        self.ICP.set_str(cp, False)
        self._settings_as(self.sys_only, {fname: True}).execute()
        self.assertEqual(self.ICP.get_str(cp), 'True')
        self.assertEqual(self.ICP.get_str(LEAD_NAME_PARAM), 'Before {form_name}')

    def test_settings_admin_changing_meta_value_refused(self):
        fname, _cp = self._foreign_bool_param()
        settings = self._settings_as(self.sys_only, {
            'lead_name_template': 'Changed {form_name}', fname: True})
        with self.assertRaisesRegex(
                AccessError,
                'Only Meta administrators can change Meta Lead Ads settings.'):
            settings.execute()
        self.assertEqual(self.ICP.get_str(LEAD_NAME_PARAM), 'Before {form_name}')

    def test_unchanged_junk_retention_passes(self):
        """A bad stored retention doesn't block a save that doesn't touch it."""
        fname, cp = self._foreign_bool_param()
        self.ICP.set_str(cp, False)
        self.ICP.set_str(SYNC_LOG_RETENTION_PARAM, '0')
        # The form shows the default in use and warns about the stored value.
        with self.assertLogs('odoo.addons.meta_lead_ads.models.meta_sync_log',
                             'WARNING'):
            self._settings_as(self.sys_only, {fname: True}).execute()
        self.assertEqual(self.ICP.get_str(SYNC_LOG_RETENTION_PARAM), '0')
        self.assertEqual(self.ICP.get_str(cp), 'True')

    # ---- other users core lets save settings --------------------------------

    def test_erp_manager_meta_admin_saves_only_meta_settings(self):
        # On Odoo 20 saving the other apps' settings needs Settings rights
        # (mail's save reads ir.asset), so an Access Rights administrator
        # saves the Meta values only, like a Meta admin without rights.
        fname, cp = self._foreign_bool_param()
        self.ICP.set_str(cp, False)
        self._settings_as(self.erp_meta, {
            'lead_name_template': 'Erp {form_name}', fname: True}).execute()
        self.assertFalse(self.ICP.get_bool(cp))
        self.assertEqual(self.ICP.get_str(LEAD_NAME_PARAM), 'Erp {form_name}')

    def test_superuser_save_still_works(self):
        self.assertTrue(self.env.su)
        self.Settings.create({'lead_name_template': 'Su {form_name}'}).execute()
        self.assertEqual(self.ICP.get_str(LEAD_NAME_PARAM), 'Su {form_name}')

    # ---- screens and menu ---------------------------------------------------

    def _meta_app(self, arch):
        apps = etree.fromstring(arch).xpath("//app[@name='meta_lead_ads']")
        self.assertEqual(len(apps), 1)
        return apps[0]

    def test_meta_only_view_shows_only_meta_app(self):
        view = self.env.ref(META_ONLY_VIEW)
        self.assertFalse(view.inherit_id)
        arch = self.Settings.with_user(self.meta_only).get_view(
            view.id, 'form')['arch']
        self.assertIn('lead_name_template', arch)
        self.assertIn('name="meta_lead_ads"', arch)
        self.assertEqual(arch.count('<app'), 1)

    def test_default_settings_view_unchanged(self):
        base = self.env.ref('base.res_config_settings_view_form')
        for user in (self.meta_only, self.sys_only):
            with self.subTest(user=user.login):
                view = self.Settings.with_user(user).get_view()
                self.assertEqual(view['id'], base.id)

    def test_meta_settings_blocks_match_settings_app(self):
        """Both screens show the same Meta blocks, fields and buttons."""
        Settings = self.Settings.with_user(self.meta_only)
        full = self._meta_app(Settings.get_view()['arch'])
        meta_only = self._meta_app(Settings.get_view(
            self.env.ref(META_ONLY_VIEW).id, 'form')['arch'])

        def names(app):
            return {(el.tag, el.get('name'))
                    for el in app.iter('field', 'button')}

        def blocks(app):
            return [el.get('name') for el in app.iter('block')]

        self.assertTrue(names(full))
        self.assertEqual(names(meta_only), names(full))
        self.assertEqual(blocks(meta_only), blocks(full))
        self.assertEqual(len(blocks(full)), 5)

    def test_meta_settings_menu_for_meta_admins_only(self):
        menu = self.env.ref('meta_lead_ads.menu_meta_leads_settings')
        self.assertEqual(menu.action.view_id, self.env.ref(META_ONLY_VIEW))
        Menu = self.env['ir.ui.menu']
        self.assertIn(menu.id,
                      Menu.with_user(self.meta_only)._visible_menu_ids())
        self.assertNotIn(menu.id,
                         Menu.with_user(self.sys_only)._visible_menu_ids())
