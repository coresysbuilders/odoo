# Meta Lead Ads Integration for Odoo — © 2026 CoreSys Builders. All rights reserved.
# Licensed under the Odoo Proprietary License v1.0 (OPL-1).
# Unauthorized copying, redistribution, or resale of this software, in whole or in
# part, via any medium, is strictly prohibited and constitutes a license violation.
# OPL-1: https://www.odoo.com/documentation/19.0/legal/licenses.html#odoo-apps

"""Tests for the module owning the custom lead fields it fills.

A promoted field, or a manual x_meta_ field a mapping fills, gets an
external id of this module, so uninstalling the module removes it. The id
is noupdate, so an upgrade never does.

Creating a manual field rebuilds the registry, which lives at process
level, so the test cursor rollback does not remove the field from
``_fields``. Each test therefore derives its own field names from the
method name so leftover registry entries never collide.
"""
import hashlib
from unittest import mock

from odoo.exceptions import UserError
from odoo.modules.module import load_script
from odoo.tests.common import TransactionCase, new_test_user, tagged
from odoo.tools import SQL

_SALES = 'base.group_user,sales_team.group_sale_salesman_all_leads'
POST_OWNERSHIP = 'meta_lead_ads/migrations/19.0.1.9.0/post-migrate.py'


class PromotedFieldMixin:
    """Fixtures shared by the runtime and the upgrade ownership tests."""

    def setUp(self):
        # Added before the test's savepoint, so it runs after the rollback.
        self.addCleanup(self._reload_models_if_needed)
        super().setUp()
        # Odoo 19 renamed res.users.groups_id to group_ids.
        self.env.user.group_ids |= self.env.ref(
            'meta_lead_ads.group_meta_admin')
        self.K = 'q_%s' % self._testMethodName
        self.T = self.env['meta.promote.answer']._derive_tech_name(self.K)
        # A second name for tests that need two fields.
        self.T2 = self.env['meta.promote.answer']._derive_tech_name(
            'r_%s' % self._testMethodName)
        self.Data = self.env['ir.model.data'].sudo()
        self.Mapping = self.env['meta.field.mapping']
        self.form = self._form()

    def _form(self):
        """Create an account, page and form with ids unique per call."""
        n = getattr(self, '_form_seq', 0) + 1
        self._form_seq = n
        acc = self.env['meta.account'].create(
            {'name': 'OA%d' % n, 'account_id': 'OA%d' % n})
        page = self.env['meta.page'].create(
            {'name': 'OP%d' % n, 'page_id': 'OP%d' % n, 'account_id': acc.id})
        return self.env['meta.lead.form'].create(
            {'name': 'OF%d' % n, 'form_id': 'OF%d' % n, 'page_id': page.id})

    def _reload_models_if_needed(self):
        # A field left in the registry for another model would be read on
        # every later search there, but its column went with the rollback.
        if getattr(self, '_models_changed', False):
            with self.registry.cursor() as cr:
                self.registry._setup_models__(cr)

    def _manual_field(self, name, model='crm.lead', label='Budget'):
        """A custom field made outside the wizard."""
        if model != 'crm.lead':
            self._models_changed = True
        model_rec = self.env['ir.model']._get(model)
        return self.env['ir.model.fields'].sudo().create({
            'name': name, 'field_description': label,
            'model_id': model_rec.id, 'model': model,
            'ttype': 'char', 'state': 'manual', 'store': True})

    def _short_name(self, prefix):
        return '%s%s' % (prefix, hashlib.sha1(
            self._testMethodName.encode()).hexdigest()[:10])

    def _map(self, field, key=None, form=None):
        return self.Mapping.create({
            'form_id': (form or self.form).id,
            'meta_key': key or self.K,
            'crm_field_id': field.id,
        })

    def _xmlids(self, record):
        """Every external id row of the record, any module."""
        return self.Data.search([('model', '=', record._name),
                                 ('res_id', '=', record.id)])

    def _own_rows(self, record):
        return self._xmlids(record).filtered(
            lambda d: d.module == 'meta_lead_ads')

    def _assert_field_owned(self, field):
        rows = self._xmlids(field)
        self.assertEqual(len(rows), 1, rows.mapped('complete_name'))
        self.assertEqual(rows.module, 'meta_lead_ads')
        self.assertEqual(rows.name, 'field_crm_lead__%s' % field.name)
        self.assertTrue(rows.noupdate, "an upgrade would drop the field")

    def _custom_view(self, tech_name):
        return self.env['ir.ui.view'].sudo().search(
            [('name', '=', 'crm.lead.form.meta.custom.%s' % tech_name)])

    def _look_alike_views(self, tech_name):
        """Views that only borrow the wizard's view name: one on another
        model, and one on the lead form under another parent."""
        View = self.env['ir.ui.view'].sudo()
        name = 'crm.lead.form.meta.custom.%s' % tech_name
        other_model = View.create({
            'name': name, 'model': 'res.partner', 'type': 'form',
            'arch': '<form><field name="name"/></form>',
        })
        other_parent = View.create({
            'name': name, 'model': 'crm.lead',
            'inherit_id': self.env.ref('crm.crm_lead_view_form').id,
            'arch': '<data><xpath expr="//sheet" position="inside">'
                    '<field name="%s"/></xpath></data>' % tech_name,
        })
        return other_model | other_parent

    def _assert_view_owned(self, view, tech_name):
        self.assertEqual(len(view), 1)
        rows = self._xmlids(view)
        self.assertEqual(len(rows), 1, rows.mapped('complete_name'))
        self.assertEqual(rows.module, 'meta_lead_ads')
        self.assertEqual(rows.name, 'crm_lead_form_meta_custom_%s' % tech_name)
        self.assertTrue(rows.noupdate, "an upgrade would drop the view")

    def _lead_with_answer(self, value='$5k', company=None):
        lead = self.env['crm.lead'].create({
            'name': 'Lead', 'type': 'lead',
            'company_id': (company or self.env.company).id,
            'meta_form_id_ref': self.form.id,
        })
        self.env['meta.lead.answer'].create({
            'lead_id': lead.id, 'question_key': self.K,
            'label': 'Budget', 'value': value,
        })
        return lead

    def _wizard(self, lead, user=None):
        Wizard = self.env['meta.promote.answer']
        if user is not None:
            Wizard = Wizard.with_user(user)
        return Wizard.create({'answer_id': lead.answer_ids[:1].id,
                              'lead_id': lead.id})


@tagged('post_install', '-at_install')
class TestPromotedFieldOwnership(PromotedFieldMixin, TransactionCase):

    # ---- mapping create --------------------------------------------------

    def test_mapping_onto_x_meta_field_owns_it(self):
        """A mapping onto a manual x_meta_ lead field makes the module own
        it with a noupdate id named after the field."""
        field = self._manual_field(self.T)
        self.assertFalse(self._xmlids(field), "precondition")
        self._map(field)
        self._assert_field_owned(field)

    def test_owning_twice_leaves_one_row(self):
        """Owning a field that is already owned adds nothing."""
        field = self._manual_field(self.T)
        owned = self.Mapping._meta_own_promoted_fields(field)
        self.assertEqual(owned, field)
        again = self.Mapping._meta_own_promoted_fields(field)
        self.assertFalse(again)
        self._assert_field_owned(field)

    def test_field_without_prefix_not_owned(self):
        """A hand-made field whose name does not start with x_meta_ is
        never taken over, even when a mapping fills it."""
        field = self._manual_field(self._short_name('x_other_'))
        self._map(field)
        self.assertFalse(self._xmlids(field))

    def test_standard_field_not_owned(self):
        """A mapping onto a stock lead field adds no id of this module."""
        city = self.env['ir.model.fields']._get('crm.lead', 'city')
        before = self._xmlids(city)
        self._map(city)
        self.assertEqual(self._xmlids(city), before)
        self.assertFalse(self._own_rows(city))

    def test_field_owned_elsewhere_not_owned(self):
        """A field that already carries an id of another module stays
        with that module."""
        field = self._manual_field(self.T)
        other = self.Data.create({
            'module': '__custom__', 'name': field.name,
            'model': 'ir.model.fields', 'res_id': field.id})
        self._map(field)
        self.assertEqual(self._xmlids(field), other)

    def test_field_on_other_model_not_owned(self):
        """Only lead fields are ever taken over."""
        field = self._manual_field(self.T, model='res.partner')
        self.assertFalse(self.Mapping._meta_own_promoted_fields(field))
        self.assertFalse(self._xmlids(field))

    def test_module_field_never_released(self):
        """A field the module itself defines keeps its id whatever the
        mappings do."""
        field = self.env['ir.model.fields']._get(
            'crm.lead', 'meta_campaign_name')
        before = self._xmlids(field)
        self.assertTrue(before.filtered(lambda d: d.module == 'meta_lead_ads'),
                        "precondition")
        self.assertFalse(self.Mapping._meta_own_promoted_fields(field))
        self.assertFalse(self.Mapping._meta_release_unused_fields(field))
        self.assertEqual(self._xmlids(field), before)

    # ---- retarget and removal --------------------------------------------

    def test_retarget_moves_ownership(self):
        """Pointing a mapping at another field owns the new one and gives
        the old one back once nothing else uses it."""
        old = self._manual_field(self.T)
        new = self._manual_field(self.T2)
        mapping = self._map(old)
        self._assert_field_owned(old)
        mapping.write({'crm_field_id': new.id})
        self._assert_field_owned(new)
        self.assertFalse(self._xmlids(old))
        self.assertTrue(old.exists())

    def test_unlink_last_mapping_releases_field(self):
        """Removing the only mapping of an owned field without a lead-form
        view drops the id but keeps the field."""
        field = self._manual_field(self.T)
        mapping = self._map(field)
        self._assert_field_owned(field)
        mapping.unlink()
        self.assertFalse(self._xmlids(field))
        self.assertTrue(field.exists())

    def test_unlink_keeps_field_with_lead_form_view(self):
        """A field shown on the lead form by the promote wizard stays owned
        after its mapping goes."""
        field = self._manual_field(self.T)
        mapping = self._map(field)
        self.env['meta.promote.answer']._inject_view(field.name)
        self.assertTrue(self._custom_view(field.name), "precondition")
        mapping.unlink()
        self._assert_field_owned(field)

    def test_archived_mapping_keeps_ownership(self):
        """A switched-off mapping still points at the field, so the field
        stays owned when the other mapping goes."""
        field = self._manual_field(self.T)
        archived = self._map(field)
        live = self._map(field, form=self._form())
        archived.active = False
        live.unlink()
        self._assert_field_owned(field)
        self.assertEqual(self.Mapping._meta_release_unused_fields(field),
                         self.env['ir.model.fields'])

    def test_form_delete_releases_its_fields(self):
        """Deleting a lead form gives back the fields only its mappings
        filled, archived ones included, and keeps the fields.

        The database drops the form's mappings itself, without the
        mapping's unlink, so the form has to do the release.
        """
        live = self._manual_field(self.T)
        archived = self._manual_field(self.T2)
        self._map(live)
        self._map(archived, key='%s_old' % self.K).active = False
        self._assert_field_owned(live)
        self._assert_field_owned(archived)
        self.form.unlink()
        self.assertFalse(self._xmlids(live))
        self.assertFalse(self._xmlids(archived))
        self.assertTrue(live.exists() and archived.exists())

    def test_form_delete_keeps_field_used_elsewhere(self):
        """A field another form still fills stays owned when one of its
        forms is deleted."""
        field = self._manual_field(self.T)
        self._map(field)
        self._map(field, form=self._form())
        self.form.unlink()
        self._assert_field_owned(field)

    # Pages and accounts take their forms along in the database too, so
    # meta.lead.form.unlink never runs for those forms either.

    def _archived_subtree_mapping(self, field, page=None, account=None):
        """Map the field from an archived form, under an archived Page when
        an account is given, through a switched-off mapping."""
        if page is None:
            page = self.env['meta.page'].create({
                'name': 'OP_old', 'page_id': self._short_name('OP_old_'),
                'account_id': account.id, 'active': False})
        form = self.env['meta.lead.form'].create({
            'name': 'OF_old', 'form_id': self._short_name('OF_old_'),
            'page_id': page.id, 'active': False})
        mapping = self._map(field, key='%s_old' % self.K, form=form)
        mapping.active = False
        return mapping

    def test_page_delete_releases_its_fields(self):
        """Deleting a Page gives back the fields only its forms filled,
        archived forms and mappings included, and keeps the fields."""
        live = self._manual_field(self.T)
        archived = self._manual_field(self.T2)
        self._map(live)
        self._archived_subtree_mapping(archived, page=self.form.page_id)
        self._assert_field_owned(live)
        self._assert_field_owned(archived)
        self.form.page_id.unlink()
        self.assertFalse(self._xmlids(live))
        self.assertFalse(self._xmlids(archived))
        self.assertTrue(live.exists() and archived.exists())

    def test_page_delete_keeps_shared_and_viewed_fields(self):
        """A field another Page's form still fills, or one the wizard shows
        on the lead form, stays owned when the Page goes."""
        shared = self._manual_field(self.T)
        viewed = self._manual_field(self.T2)
        self._map(shared)
        self._map(shared, form=self._form())
        self._map(viewed, key='%s_view' % self.K)
        self.env['meta.promote.answer']._inject_view(viewed.name)
        self.form.page_id.unlink()
        self._assert_field_owned(shared)
        self._assert_field_owned(viewed)

    def test_account_delete_releases_its_fields(self):
        """Deleting an account gives back the fields only its Pages' forms
        filled, archived Pages included, and keeps the fields."""
        live = self._manual_field(self.T)
        archived = self._manual_field(self.T2)
        account = self.form.page_id.account_id
        self._map(live)
        self._archived_subtree_mapping(archived, account=account)
        self._assert_field_owned(live)
        self._assert_field_owned(archived)
        account.unlink()
        self.assertFalse(self._xmlids(live))
        self.assertFalse(self._xmlids(archived))
        self.assertTrue(live.exists() and archived.exists())

    def test_account_delete_keeps_shared_and_viewed_fields(self):
        """A field another account's form still fills, or one the wizard
        shows on the lead form, stays owned when the account goes."""
        shared = self._manual_field(self.T)
        viewed = self._manual_field(self.T2)
        self._map(shared)
        self._map(shared, form=self._form())
        self._map(viewed, key='%s_view' % self.K)
        self.env['meta.promote.answer']._inject_view(viewed.name)
        self.form.page_id.account_id.unlink()
        self._assert_field_owned(shared)
        self._assert_field_owned(viewed)

    # ---- restricted admin ------------------------------------------------

    def test_meta_admin_without_settings_owns_field(self):
        """A Meta Admin without Settings rights gets the same ownership
        when adding the mapping."""
        field = self._manual_field(self.T)
        admin = new_test_user(
            self.env, self._short_name('own_admin_'),
            groups=_SALES + ',meta_lead_ads.group_meta_admin')
        self.assertFalse(admin.has_group('base.group_system'))
        self.assertFalse(
            self.env['ir.model.fields'].with_user(admin).has_access('read'))
        self.env.flush_all()
        self.env.invalidate_all()
        self.Mapping.with_user(admin).create({
            'form_id': self.form.id, 'meta_key': self.K,
            'crm_field_name': field.name})
        self._assert_field_owned(field)

    # ---- promote wizard --------------------------------------------------

    def test_promote_owns_field_and_view(self):
        """Promoting an answer leaves the new field and its lead-form view
        owned by the module."""
        lead = self._lead_with_answer()
        self._wizard(lead).action_promote()
        field = self.env['ir.model.fields']._get('crm.lead', self.T)
        self.assertTrue(field)
        self._assert_field_owned(field)
        self._assert_view_owned(self._custom_view(self.T), self.T)
        self.assertEqual(lead[self.T], '$5k')

    def test_reuse_owns_field_and_view(self):
        """Reusing a hand-made x_meta_ field owns it and the view the
        wizard adds for it."""
        field = self._manual_field(self.T)
        lead = self._lead_with_answer()
        wizard = self._wizard(lead)
        wizard.action_promote()
        self.assertEqual(wizard.state, 'confirm', "precondition")
        self.assertFalse(self._xmlids(field), "the confirm step owns nothing")
        wizard.action_confirm_reuse()
        self._assert_field_owned(field)
        self._assert_view_owned(self._custom_view(self.T), self.T)

    def test_link_to_unprefixed_field_owns_view_only(self):
        """Linking a field without the x_meta_ prefix owns the view the
        wizard made, never the field itself.

        The wizard always adds the prefix to names, so the link step is
        called directly here.
        """
        field = self._manual_field(self._short_name('x_other_'))
        wizard = self._wizard(self._lead_with_answer())
        wizard._link_mapping(self.form, self.K, field)
        self.assertFalse(self._xmlids(field))
        self._assert_view_owned(self._custom_view(field.name), field.name)

    def test_inject_view_owns_existing_unowned_view(self):
        """A lead-form view an earlier release added without an id is
        owned when the wizard finds it, and no second view is made."""
        self._manual_field(self.T)
        anchor = self.env.ref('meta_lead_ads.crm_lead_view_form_meta')
        view = self.env['ir.ui.view'].sudo().create({
            'name': 'crm.lead.form.meta.custom.%s' % self.T,
            'model': 'crm.lead', 'inherit_id': anchor.id,
            'arch': '<data><xpath expr="//group[@name=\'meta_custom_fields\']"'
                    ' position="inside"><field name="%s"/></xpath></data>'
                    % self.T,
        })
        self.assertFalse(self._xmlids(view), "precondition")
        Wizard = self.env['meta.promote.answer']
        Wizard._inject_view(self.T)
        self.assertEqual(self._custom_view(self.T), view)
        self._assert_view_owned(view, self.T)
        rows = self._xmlids(view)
        Wizard._inject_view(self.T)
        self.assertEqual(self._custom_view(self.T), view)
        self.assertEqual(self._xmlids(view), rows)

    def test_inject_view_ignores_look_alike_views(self):
        """A view with the wizard's name on another model, or under another
        parent, is not the wizard's: it gets no id, and the wizard still
        adds its own view under the Meta tab."""
        self._manual_field(self.T)
        look_alikes = self._look_alike_views(self.T)
        self.env['meta.promote.answer']._inject_view(self.T)
        for view in look_alikes:
            self.assertFalse(self._xmlids(view), view.model)
        own = self._custom_view(self.T) - look_alikes
        self.assertEqual(own.inherit_id, self.env.ref(
            'meta_lead_ads.crm_lead_view_form_meta'))
        self._assert_view_owned(own, self.T)

    def test_look_alike_views_do_not_keep_a_field(self):
        """Only the wizard's own lead-form view keeps a field owned once
        its last mapping goes."""
        field = self._manual_field(self.T)
        mapping = self._map(field)
        self._look_alike_views(field.name)
        mapping.unlink()
        self.assertFalse(self._xmlids(field))
        self.assertTrue(field.exists())

    def test_failed_promotion_leaves_no_ownership(self):
        """A promotion that fails halfway leaves no id of the module for
        the field or its view."""
        # The field reached the registry before the failure; its column
        # went with the savepoint.
        self._models_changed = True
        lead = self._lead_with_answer()
        wizard = self._wizard(lead)
        Wizard = type(self.env['meta.promote.answer'])
        with mock.patch.object(Wizard, '_dispatch_backfill',
                               side_effect=UserError("stop")):
            with self.assertRaises(UserError):
                wizard.action_promote()
        self.assertFalse(self.Data.search_count([
            ('module', '=', 'meta_lead_ads'),
            ('name', 'in', ['field_crm_lead__%s' % self.T,
                            'crm_lead_form_meta_custom_%s' % self.T])]))
        self.assertFalse(self.env['ir.model.fields'].search_count([
            ('model', '=', 'crm.lead'), ('name', '=', self.T)]))

    def test_meta_admin_without_settings_promotes_and_owns(self):
        """A Meta Admin without Settings rights who promotes gets both ids."""
        admin = new_test_user(
            self.env, self._short_name('own_promoter_'),
            groups=_SALES + ',meta_lead_ads.group_meta_admin',
            company_id=self.env.company.id)
        self.assertFalse(admin.has_group('base.group_system'))
        lead = self._lead_with_answer()
        wizard = self._wizard(lead, user=admin)
        self.env.flush_all()
        self.env.invalidate_all()
        wizard.action_promote()
        field = self.env['ir.model.fields']._get('crm.lead', self.T)
        self._assert_field_owned(field)
        self._assert_view_owned(self._custom_view(self.T), self.T)


@tagged('post_install', '-at_install')
class TestPromotedFieldAdoption(PromotedFieldMixin, TransactionCase):
    """The 19.0.1.9.0 upgrade owns what earlier releases promoted.

    Promotion owns its records now, so each fixture deletes the ids again
    to get the database an older release left behind.
    """

    def _forget_ownership(self, fields=None, views=None):
        self.env.flush_all()
        self.env.cr.execute(SQL("""
            DELETE FROM ir_model_data
             WHERE module = 'meta_lead_ads'
               AND ((model = 'ir.model.fields' AND res_id = ANY(%s))
                 OR (model = 'ir.ui.view' AND res_id = ANY(%s)))
        """, list(fields.ids if fields else []),
            list(views.ids if views else [])))
        self.env.invalidate_all()

    def _migrate(self, version='19.0.1.8.0'):
        mod = load_script(POST_OWNERSHIP, 'mig_ownership_post')
        self.env.flush_all()
        with mock.patch.object(mod, '_logger') as logger:
            counts = mod.migrate(self.env.cr, version)
        self.env.invalidate_all()
        return counts, logger

    def _old_promotions(self):
        """A promoted field with its view and a hand-made x_meta_ field
        filled by a switched-off mapping, none of them owned."""
        lead = self._lead_with_answer()
        self._wizard(lead).action_promote()
        promoted = self.env['ir.model.fields']._get('crm.lead', self.T)
        view = self._custom_view(self.T)
        self.assertTrue(promoted and view, "precondition")
        mapped = self._manual_field(self.T2)
        self._map(mapped, form=self._form()).active = False
        self._forget_ownership(fields=promoted | mapped, views=view)
        for record in (promoted, view, mapped):
            self.assertFalse(self._xmlids(record), "precondition")
        return promoted, view, mapped

    def test_adopts_promoted_and_mapped_fields(self):
        """The promoted field, its view and the field an archived mapping
        points at are owned again, noupdate."""
        promoted, view, mapped = self._old_promotions()
        counts, logger = self._migrate()
        self.assertEqual(counts['fields'], 2)
        self.assertEqual(counts['views'], 1)
        self.assertEqual(set(counts), {'fields', 'views', 'stamps'})
        self._assert_field_owned(promoted)
        self._assert_field_owned(mapped)
        self._assert_view_owned(view, self.T)
        # Only the counts reach the log.
        logger.info.assert_called_once()
        self.assertEqual(logger.info.call_args.args[1], counts)
        for call in logger.mock_calls:
            self.assertNotIn('x_meta_', repr(call))

    def test_field_with_only_its_view_adopted(self):
        """A field whose mapping is gone is still owned when the wizard's
        lead-form view shows it."""
        field = self._manual_field(self.T)
        self.env['meta.promote.answer']._inject_view(self.T)
        view = self._custom_view(self.T)
        self._forget_ownership(fields=field, views=view)
        counts, _logger = self._migrate()
        self.assertEqual((counts['fields'], counts['views']), (1, 1))
        self._assert_field_owned(field)
        self._assert_view_owned(view, self.T)

    def test_warns_about_fields_only_a_mapping_fills(self):
        """A field owned now only because a mapping fills it, with no
        lead-form view, would be a surprise at uninstall, so the upgrade
        warns with how many there are, and nothing else."""
        self._old_promotions()
        counts, logger = self._migrate()
        self.assertEqual(counts['fields'], 2)
        logger.warning.assert_called_once()
        message, *args = logger.warning.call_args.args
        self.assertEqual(args, [1])
        self.assertIn('uninstall', message.lower())
        self.assertIn('export', message.lower())
        # The summary line stays as it was.
        logger.info.assert_called_once()
        for call in logger.mock_calls:
            self.assertNotIn('x_meta_', repr(call))

    def test_no_warning_when_every_field_has_its_view(self):
        """Fields the wizard shows on the lead form were always the
        module's own, so owning them warns about nothing."""
        lead = self._lead_with_answer()
        self._wizard(lead).action_promote()
        promoted = self.env['ir.model.fields']._get('crm.lead', self.T)
        view_only = self._manual_field(self.T2)
        self.env['meta.promote.answer']._inject_view(self.T2)
        views = self._custom_view(self.T) | self._custom_view(self.T2)
        self._forget_ownership(fields=promoted | view_only, views=views)
        counts, logger = self._migrate()
        self.assertEqual((counts['fields'], counts['views']), (2, 2))
        logger.warning.assert_not_called()

    def test_leaves_unreferenced_and_foreign_fields(self):
        """A hand-made x_meta_ field nothing uses, a field another module
        owns and a mapped field without the prefix get no id."""
        handmade = self._manual_field(self._short_name('x_meta_hm_'))
        foreign = self._manual_field(self.T)
        custom = self.Data.create({
            'module': '__custom__', 'name': foreign.name,
            'model': 'ir.model.fields', 'res_id': foreign.id})
        self._map(foreign)
        other = self._manual_field(self._short_name('x_other_'))
        self._map(other, key='%s_other' % self.K)
        counts, _logger = self._migrate()
        self.assertEqual((counts['fields'], counts['views']), (0, 0))
        self.assertFalse(self._xmlids(handmade))
        self.assertEqual(self._xmlids(foreign), custom)
        self.assertFalse(self._xmlids(other))

    def test_view_on_other_parent_left_alone(self):
        """A view that only borrows the lead-form view name, on another
        parent, is not the wizard's: neither it nor its field is owned."""
        field = self._manual_field(self.T)
        parent = self.env.ref('crm.crm_lead_view_form')
        view = self.env['ir.ui.view'].sudo().create({
            'name': 'crm.lead.form.meta.custom.%s' % self.T,
            'model': 'crm.lead', 'inherit_id': parent.id,
            'arch': '<data><xpath expr="//sheet" position="inside">'
                    '<field name="%s"/></xpath></data>' % self.T,
        })
        counts, _logger = self._migrate()
        self.assertEqual((counts['fields'], counts['views']), (0, 0))
        self.assertFalse(self._xmlids(view))
        self.assertFalse(self._xmlids(field))

    def test_second_run_adds_nothing(self):
        """Running the upgrade again finds nothing left to own."""
        self._old_promotions()
        self._migrate()
        rows = self.Data.search_count([])
        counts, _logger = self._migrate()
        self.assertEqual((counts['fields'], counts['views']), (0, 0))
        self.assertEqual(self.Data.search_count([]), rows)

    def test_fresh_install_noop(self):
        """A fresh install has nothing to adopt and logs nothing."""
        promoted, view, mapped = self._old_promotions()
        counts, logger = self._migrate(version=None)
        self.assertIsNone(counts)
        self.assertEqual(logger.mock_calls, [])
        self.assertFalse(self._xmlids(promoted))
        self.assertFalse(self._xmlids(view))
