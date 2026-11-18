# Meta Lead Ads Integration for Odoo — © 2026 CoreSys Builders. All rights reserved.
# Licensed under the Odoo Proprietary License v1.0 (OPL-1).
# Unauthorized copying, redistribution, or resale of this software, in whole or in
# part, via any medium, is strictly prohibited and constitutes a license violation.
# OPL-1: https://www.odoo.com/documentation/20.0/legal/licenses.html#odoo-apps

"""The warning shown when a database still holds more than one Meta app."""
from odoo.tests.common import TransactionCase, new_test_user, tagged
from odoo.tools.misc import file_open

from .test_ingest import IngestFixtureMixin

META_ONLY_VIEW = 'meta_lead_ads.res_config_settings_view_form_meta_only'


@tagged('post_install', '-at_install')
class TestAppConflictNotice(IngestFixtureMixin, TransactionCase):

    def setUp(self):
        super().setUp()
        self.Account = self.env['meta.account']
        self.admin = new_test_user(
            self.env, login='conflict_admin',
            groups='base.group_user,meta_lead_ads.group_meta_admin')

    def _plant(self, app_id, account_id='ACC2'):
        # The one-app rule refuses this through the ORM, so the account is
        # created archived and switched on in the table, like a database
        # that held two apps before the rule existed.
        other = self.Account.sudo().create({
            'name': 'Other', 'account_id': account_id, 'active': False,
            'access_token': 'tok_other',
        })
        self.env.flush_all()
        self.env.cr.execute(
            "UPDATE meta_account SET app_id = %s, active = true"
            " WHERE id = %s", (app_id, other.id))
        self.Account.invalidate_model()
        return other

    def _account_notice(self):
        # Read fresh, as the form does on every load.
        self.Account.invalidate_model(['app_conflict_message'])
        return self.account.with_user(self.admin).app_conflict_message

    def _settings_notice(self):
        Settings = self.env['res.config.settings'].with_user(self.admin)
        return Settings.create({}).meta_app_conflict_message

    def test_one_app_shows_no_notice(self):
        self.assertFalse(self._account_notice())
        self.assertFalse(self._settings_notice())

    def test_two_apps_show_notice_on_account(self):
        self._plant('other_app')
        notice = self._account_notice()
        self.assertTrue(notice)
        self.assertIn('active Meta accounts of 2 different Meta apps', notice)
        self.assertIn('One Meta app per database', notice)

    def test_two_apps_show_notice_in_meta_settings(self):
        self._plant('other_app')
        Settings = self.env['res.config.settings'].with_user(self.admin)
        record = Settings.create({})
        self.assertEqual(record.meta_app_conflict_message,
                         self._account_notice())
        # Display only: not one of the values the settings save compares.
        self.assertNotIn('meta_app_conflict_message',
                         record._meta_values_from_record())

    def test_padded_app_ids_count_as_one(self):
        self._plant(' app_test ')
        self.assertFalse(self._account_notice())
        self.assertFalse(self._settings_notice())

    def test_notice_gone_after_archiving_other_app(self):
        other = self._plant('other_app')
        self.assertTrue(self._account_notice())
        other.sudo().write({'active': False})
        self.assertFalse(self._account_notice())
        self.assertFalse(self._settings_notice())

    def test_notice_names_no_app_id_or_secret(self):
        self._plant('other_app')
        for notice in (self._account_notice(), self._settings_notice()):
            self.assertTrue(notice)
            for secret in ('other_app', 'app_test', 'secret_test',
                           'tok_acct', 'tok_other', 'tok_test'):
                with self.subTest(value=secret):
                    self.assertNotIn(secret, notice)

    def test_notice_field_in_both_settings_views(self):
        Settings = self.env['res.config.settings'].with_user(self.admin)
        for xmlid in ('base.res_config_settings_view_form', META_ONLY_VIEW):
            with self.subTest(view=xmlid):
                arch = Settings.get_view(
                    self.env.ref(xmlid).id, 'form')['arch']
                self.assertIn('meta_app_conflict_message', arch)

    def test_notice_on_account_form(self):
        arch = self.Account.with_user(self.admin).get_view(
            self.env.ref('meta_lead_ads.meta_account_view_form').id,
            'form')['arch']
        self.assertIn('app_conflict_message', arch)

    def test_setup_guide_has_one_app_section(self):
        with file_open('meta_lead_ads/static/doc/setup_guide.html') as fh:
            guide = fh.read()
        self.assertIn('id="one-app"', guide)
        self.assertIn('One Meta app per database', guide)
        section = guide.split('id="one-app"', 1)[1].split('<h2', 1)[0]
        self.assertIn('Connect to Meta', section)
        self.assertIn('Archive', section)
