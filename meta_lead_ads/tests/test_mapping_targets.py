# Meta Lead Ads Integration for Odoo — © 2026 CoreSys Builders. All rights reserved.
# Licensed under the Odoo Proprietary License v1.0 (OPL-1).
# Unauthorized copying, redistribution, or resale of this software, in whole or in
# part, via any medium, is strictly prohibited and constitutes a license violation.
# OPL-1: https://www.odoo.com/documentation/18.0/legal/licenses.html#odoo-apps

"""Fields a form answer may never fill, and mappings switched off for that.

The salesperson, team, stage, pipeline values, customer link, UTM and title
are set by the module or the sales team. Mappings onto them are refused for
everyone, superuser and import included, and rows saved before the block
stay archived until they point somewhere else.
"""
import re
from contextlib import contextmanager
from unittest import mock

from psycopg2 import IntegrityError

from odoo import Command
from odoo.exceptions import AccessError, ValidationError
from odoo.tests.common import TransactionCase, new_test_user, tagged
from odoo.tools import SQL, mute_logger
from odoo.tools.translate import translate_sql_constraint

from odoo.addons.meta_lead_ads.models import meta_lead_ingest
from odoo.addons.meta_lead_ads.models.const import (
    MAPPING_BLOCKED_ADDON_FIELDS, MAPPING_BLOCKED_FIELDS, is_blocked_target)

from .common_typed_targets import LinkTargetsMixin
from .test_ingest import IngestFixtureMixin
from .test_multi_company import MultiCompanyFixtureMixin

_BLOCKED_COPY = "can't be filled from a form answer"
_REACTIVATE_COPY = "can't be turned back on"
_SAMPLE_BLOCKED = ('user_id', 'team_id', 'stage_id', 'name', 'partner_id',
                   'date_deadline', 'expected_revenue', 'priority',
                   'probability', 'lost_reason_id', 'recurring_plan',
                   'campaign_id', 'source_id', 'medium_id')


def legacy_mapping(env, form, name, meta_key='fav_colour', **sql_vals):
    """A mapping row as an older version saved it.

    The ORM refuses a blocked target now, so the row is created on city
    and its target (and any other column in ``sql_vals``, such as active
    or disabled_reason) is put back with SQL.
    """
    Field = env['ir.model.fields'].sudo()
    mapping = env['meta.field.mapping'].sudo().create({
        'form_id': form.id, 'meta_key': meta_key,
        'crm_field_id': Field._get('crm.lead', 'city').id})
    env.flush_all()
    sets = [SQL("crm_field_id = %s", Field._get('crm.lead', name).id)]
    sets += [SQL("%s = %s", SQL.identifier(column), value)
             for column, value in sql_vals.items()]
    env.cr.execute(SQL("UPDATE meta_field_mapping SET %s WHERE id = %s",
                       SQL(", ").join(sets), mapping.id))
    env.invalidate_all()
    return mapping


@tagged('post_install', '-at_install')
class TestMappingTargets(MultiCompanyFixtureMixin, IngestFixtureMixin,
                         TransactionCase):

    def setUp(self):
        super().setUp()
        self.Field = self.env['ir.model.fields'].sudo()
        self.Mapping = self.env['meta.field.mapping']
        self.SuMapping = self.Mapping.sudo()
        self.AdminMapping = self.Mapping.with_user(self.admin_a)

    # ------------------------------------------------------------------ #
    # Helpers
    # ------------------------------------------------------------------ #
    def _cold_cache(self):
        # Superuser fixtures leave field records cached, and cached values
        # skip the access check a real request would hit.
        self.env.flush_all()
        self.env.invalidate_all()

    def _target(self, name):
        return self.Field._get('crm.lead', name)

    @contextmanager
    def _refused(self, message):
        # Several checks raise ValidationError; the message tells which.
        with self.assertRaises(ValidationError) as cm:
            yield
        self.assertRegex(str(cm.exception), message)

    def _legacy_mapping(self, name, meta_key='fav_colour', form=None,
                        **sql_vals):
        return legacy_mapping(self.env, form or self.form, name,
                              meta_key=meta_key, **sql_vals)

    def _city_mapping(self, meta_key='city_q', form=None, **vals):
        return self.SuMapping.create(dict({
            'form_id': (form or self.form).id, 'meta_key': meta_key,
            'crm_field_id': self._target('city').id}, **vals))

    def _rows(self, meta_key):
        return self.SuMapping.with_context(active_test=False).search_count([
            ('form_id', '=', self.form.id), ('meta_key', '=', meta_key)])

    # ------------------------------------------------------------------ #
    # One list, read by the picker and every check
    # ------------------------------------------------------------------ #
    def test_is_blocked_target(self):
        for name in ('user_id', 'recurring_plan', 'recurring_anything',
                     'active', 'meta_leadgen_id', 'name', 'source_id',
                     'partner_assigned_id', 'date_partner_assign'):
            self.assertTrue(is_blocked_target(name), name)
        for name in ('city', 'x_meta_budget', 'color', '', None):
            self.assertFalse(is_blocked_target(name), name)
        # Any link to a user or contact, whatever the field is called.
        for comodel in ('res.partner', 'res.users'):
            self.assertTrue(is_blocked_target('x_ref_id', comodel), comodel)
        for name, comodel in (('country_id', 'res.country'),
                              ('tag_ids', 'crm.tag'), ('x_ref_id', None),
                              ('x_ref_id', False), ('', 'res.partner'),
                              (None, None)):
            self.assertFalse(is_blocked_target(name, comodel),
                             (name, comodel))

    def test_blocked_names_exist_on_crm_lead(self):
        # A renamed field would leave a stale entry that blocks nothing.
        Lead = self.env['crm.lead']
        for name in MAPPING_BLOCKED_FIELDS:
            self.assertIn(name, Lead._fields, name)

    def test_partner_assignment_fields_refused_without_the_addon(self):
        # They only exist with website_crm_partner_assign, so they are kept
        # out of the list above and the picker must still work without them.
        self.assertFalse(MAPPING_BLOCKED_ADDON_FIELDS & MAPPING_BLOCKED_FIELDS)
        names = {name for name, _label in self.Mapping._offered_targets()}
        self.assertIn('city', names)
        self.assertFalse(names & MAPPING_BLOCKED_ADDON_FIELDS)

    def test_picker_drops_blocked_keeps_business(self):
        names = {name for name, _label in self.Mapping._offered_targets()}
        self.assertFalse(names & MAPPING_BLOCKED_FIELDS)
        self.assertFalse([n for n in names if n.startswith('recurring_')])
        Lead = self.env['crm.lead']
        keep = {'city', 'street', 'street2', 'zip', 'country_id', 'state_id',
                'function', 'website', 'email_cc', 'lang_id', 'tag_ids',
                'contact_name', 'email_from', 'phone', 'partner_name'}
        for name in keep:
            if name in Lead._fields:
                self.assertIn(name, names)

    def test_fields_get_offers_no_blocked_name(self):
        self._cold_cache()
        options = dict(self.AdminMapping.fields_get(
            ['crm_field_name'])['crm_field_name']['selection'])
        self.assertIn('city', options)
        for name in options:
            self.assertFalse(is_blocked_target(name), name)

    # ------------------------------------------------------------------ #
    # Saving onto a blocked field
    # ------------------------------------------------------------------ #
    def test_create_by_name_refused_for_admin_and_superuser(self):
        for name in _SAMPLE_BLOCKED:
            for Mapping in (self.AdminMapping, self.SuMapping):
                with self.subTest(name=name, su=Mapping.env.su), \
                        self._refused(_BLOCKED_COPY):
                    Mapping.create({'form_id': self.form.id,
                                    'meta_key': 'q_%s' % name,
                                    'crm_field_name': name})
        self.assertEqual(self.Mapping.search_count(
            [('form_id', '=', self.form.id)]), 0)

    def test_create_by_field_id_refused_for_superuser(self):
        for name in _SAMPLE_BLOCKED:
            with self.subTest(name=name), self._refused(_BLOCKED_COPY):
                self.SuMapping.create({'form_id': self.form.id,
                                       'meta_key': 'q_%s' % name,
                                       'crm_field_id': self._target(name).id})

    def test_refusal_names_the_field_label(self):
        label = self._target('user_id').field_description
        with self._refused(re.escape(label)):
            self.AdminMapping.create({'form_id': self.form.id,
                                      'meta_key': 'owner',
                                      'crm_field_name': 'user_id'})

    def test_inactive_create_refused(self):
        with self._refused(_BLOCKED_COPY):
            self.SuMapping.create({'form_id': self.form.id,
                                   'meta_key': 'owner', 'active': False,
                                   'crm_field_id': self._target('user_id').id})
        with self._refused(_BLOCKED_COPY):
            self.AdminMapping.create({'form_id': self.form.id,
                                      'meta_key': 'owner', 'active': False,
                                      'crm_field_name': 'user_id'})
        self.assertEqual(self._rows('owner'), 0)

    def test_retarget_active_row_refused(self):
        row = self._city_mapping()
        with self._refused(_BLOCKED_COPY):
            row.write({'crm_field_id': self._target('user_id').id})
        with self._refused(_BLOCKED_COPY):
            row.with_user(self.admin_a).write({'crm_field_name': 'team_id'})
        self.assertEqual(row.crm_field_id.name, 'city')

    def test_retarget_archived_row_refused(self):
        row = self._city_mapping()
        row.action_archive()
        with self._refused(_BLOCKED_COPY):
            row.write({'crm_field_id': self._target('user_id').id})
        with self.assertRaises(ValidationError):
            row.with_user(self.admin_a).write({'crm_field_name': 'user_id'})
        self.assertFalse(row.active)
        self.assertEqual(row.crm_field_id.name, 'city')

    def test_archive_legacy_row_with_target_unchanged(self):
        # Before the upgrade runs, a legacy row is still active on a
        # blocked field. Switching it off must work for anyone allowed.
        for env_mapping in (self.AdminMapping, self.SuMapping):
            row = self._legacy_mapping(
                'user_id', meta_key='owner_%s' % env_mapping.env.uid)
            row = env_mapping.browse(row.id)
            self._cold_cache()
            row.action_archive()
            self.assertFalse(row.active)
            self.assertEqual(row.sudo().crm_field_id.name, 'user_id')
            row.write({'meta_key': 'owner_renamed_%s' % env_mapping.env.uid})
            self.assertEqual(row.sudo().crm_field_id.name, 'user_id')

    def test_import_refuses_blocked_target(self):
        self._cold_cache()
        ok = self.AdminMapping.load(
            ['form_id/.id', 'meta_key', 'crm_field_name'],
            [[str(self.form.id), 'imp_city', 'city']])
        self.assertTrue(ok['ids'], ok['messages'])

        by_id = self.AdminMapping.load(
            ['form_id/.id', 'meta_key', 'crm_field_id/.id'],
            [[str(self.form.id), 'imp_owner',
              str(self._target('user_id').id)]])
        self.assertFalse(by_id['ids'])
        text = ' '.join(m['message'] for m in by_id['messages'])
        self.assertIn(_BLOCKED_COPY, text)

        # user_id is not in the picker list, so the importer drops the value
        # and the row then fails on the required target.
        with mute_logger('odoo.sql_db'):
            by_name = self.AdminMapping.load(
                ['form_id/.id', 'meta_key', 'crm_field_name'],
                [[str(self.form.id), 'imp_owner', 'user_id']])
        self.assertFalse(by_name['ids'])
        self.assertEqual(self._rows('imp_owner'), 0)

    # ------------------------------------------------------------------ #
    # Disabled rows
    # ------------------------------------------------------------------ #
    def test_disabled_reason_is_module_owned(self):
        row = self._city_mapping()
        with self.assertRaises(AccessError):
            row.with_user(self.admin_a).write({'disabled_reason': 'x'})
        with self.assertRaises(AccessError):
            self.AdminMapping.create({'form_id': self.form.id,
                                      'meta_key': 'with_reason',
                                      'crm_field_name': 'city',
                                      'disabled_reason': 'x'})
        row.sudo().write({'disabled_reason': 'Set by the upgrade'})
        self.assertEqual(row.disabled_reason, 'Set by the upgrade')

    def test_unarchive_refused_until_retargeted(self):
        row = self._legacy_mapping('user_id', active=False,
                                   disabled_reason='Disabled on upgrade')
        for env_mapping in (self.AdminMapping, self.SuMapping):
            self._cold_cache()
            with self._refused(_REACTIVATE_COPY):
                env_mapping.browse(row.id).action_unarchive()
        self.assertFalse(row.active)

        admin_row = self.AdminMapping.browse(row.id)
        self._cold_cache()
        admin_row.write({'crm_field_name': 'city'})
        admin_row.action_unarchive()
        self.assertTrue(row.active)
        self.assertFalse(row.disabled_reason)
        self.assertEqual(row.crm_field_id.name, 'city')

    def test_retarget_disabled_row_clears_reason(self):
        # Fixed but left off: the row no longer counts as turned off by the
        # upgrade, so the form notice and the list banner drop it.
        for env_mapping, vals in (
                (self.SuMapping, {'crm_field_id': self._target('city').id}),
                (self.AdminMapping, {'crm_field_name': 'city'})):
            with self.subTest(su=env_mapping.env.su):
                row = self._legacy_mapping(
                    'user_id', meta_key='owner_%s' % env_mapping.env.uid,
                    active=False, disabled_reason='Disabled on upgrade')
                self.form.invalidate_recordset()
                before = self.form.disabled_mapping_count
                self._cold_cache()
                env_mapping.with_context(active_test=False).browse(
                    row.id).write(vals)
                self.assertFalse(row.active)
                self.assertFalse(row.disabled_reason)
                self.assertEqual(row.crm_field_id.name, 'city')
                self.form.invalidate_recordset()
                self.assertEqual(self.form.disabled_mapping_count, before - 1)

    def test_retarget_disabled_row_to_blocked_field_keeps_reason(self):
        row = self._legacy_mapping('team_id', active=False,
                                   disabled_reason='Disabled on upgrade')
        with self._refused(_BLOCKED_COPY):
            row.write({'crm_field_id': self._target('user_id').id})
        self._cold_cache()
        with self.assertRaises(ValidationError):
            self.AdminMapping.with_context(active_test=False).browse(
                row.id).write({'crm_field_name': 'user_id'})
        self.assertEqual(row.disabled_reason, 'Disabled on upgrade')
        self.assertEqual(row.crm_field_id.name, 'team_id')
        self.form.invalidate_recordset()
        self.assertEqual(self.form.disabled_mapping_count, 1)

    def test_retarget_active_row_leaves_reason_alone(self):
        row = self._city_mapping()
        row.with_user(self.admin_a).write({'crm_field_name': 'street'})
        self.assertTrue(row.active)
        self.assertFalse(row.disabled_reason)
        self.assertEqual(row.crm_field_id.name, 'street')

    def test_archived_row_keeps_label(self):
        row = self._legacy_mapping('user_id', active=False,
                                   disabled_reason='Disabled on upgrade')
        self._cold_cache()
        admin_row = self.AdminMapping.with_context(
            active_test=False).browse(row.id)
        self.assertEqual(admin_row.crm_field_name, 'user_id')
        labels = dict(self.AdminMapping._selection_crm_field_name())
        self.assertIn('user_id', labels)
        self.assertIn(self._target('user_id').field_description,
                      labels['user_id'])

    def test_unoffered_targets_hide_archived_target(self):
        # The label stays in the selection, but the picker is told the
        # field can't be picked, on every row.
        archived = self._legacy_mapping('user_id', active=False,
                                        disabled_reason='Disabled on upgrade')
        city = self._city_mapping()
        self._cold_cache()
        rows = self.AdminMapping.with_context(active_test=False).browse(
            (archived | city).ids)
        for row in rows:
            self.assertEqual(row.unoffered_targets, ['user_id'])

    def test_unoffered_targets_empty_without_old_rows(self):
        row = self._city_mapping()
        self._cold_cache()
        # An empty Json list reads back as False.
        self.assertFalse(self.AdminMapping.browse(row.id).unoffered_targets)

    def test_unoffered_targets_skip_other_company_rows(self):
        form_b = self.env['meta.lead.form'].sudo().create({
            'name': 'Form B', 'form_id': 'FB', 'page_id': self.page_b.id})
        self._legacy_mapping('user_id', form=form_b, active=False,
                             disabled_reason='Disabled on upgrade')
        row = self._city_mapping()
        self._cold_cache()
        self.assertFalse(self.AdminMapping.browse(row.id).unoffered_targets)
        self.assertIn('user_id', self.SuMapping.browse(row.id)
                      .unoffered_targets)

    def test_unoffered_targets_on_a_new_row(self):
        # A new row gets its values from onchange, not from a read: once
        # without a form (full list, mapping form) and once with the form
        # set, as a new line on the form's Field Mappings tab.
        self._legacy_mapping('user_id', active=False,
                             disabled_reason='Disabled on upgrade')
        self._cold_cache()
        spec = {'meta_key': {}, 'form_id': {}, 'crm_field_name': {},
                'unoffered_targets': {}}
        for values in ({}, {'form_id': self.form.id}):
            with self.subTest(values=values):
                result = self.AdminMapping.onchange(dict(values), [], spec)
                self.assertEqual(result['value']['unoffered_targets'],
                                 ['user_id'])

    def test_unoffered_targets_on_a_new_form_line(self):
        # On the form's Field Mappings tab the line also goes through the
        # lead form's own onchange, with the line's fields in a sub-spec.
        # The first call for a new form returns the whole line; a later
        # call on the open form returns only what changed. Neither may hand
        # the picker an empty list.
        self._legacy_mapping('user_id', active=False,
                             disabled_reason='Disabled on upgrade')
        self._cold_cache()
        Form = self.env['meta.lead.form'].with_user(self.admin_a)
        spec = {'mapping_ids': {'fields': {
            'meta_key': {}, 'crm_field_name': {}, 'unoffered_targets': {}}}}
        line = (Command.CREATE, 'virtual_1', {'meta_key': 'new_q'})

        result = Form.onchange({'mapping_ids': [line]}, [], spec)
        created = [cmd[2] for cmd in result['value']['mapping_ids']
                   if cmd[0] == Command.CREATE]
        self.assertEqual(len(created), 1)
        self.assertEqual(created[0]['unoffered_targets'], ['user_id'])

        result = Form.browse(self.form.id).onchange(
            {'mapping_ids': [line]}, ['mapping_ids'], spec)
        # The client keeps the list it got for the new row unless this
        # call sends another one.
        seen = ['user_id']
        for cmd in result['value'].get('mapping_ids', []):
            if (cmd[0] in (Command.CREATE, Command.UPDATE)
                    and 'unoffered_targets' in cmd[2]):
                seen = cmd[2]['unoffered_targets']
        self.assertEqual(seen, ['user_id'])

    def test_mapping_display_name_is_question_key(self):
        row = self._city_mapping(meta_key='budget_q')
        self.assertEqual(row.display_name, 'budget_q')

    def test_views_use_target_picker(self):
        for xmlid in ('meta_lead_ads.meta_field_mapping_view_list',
                      'meta_lead_ads.meta_field_mapping_view_form',
                      'meta_lead_ads.meta_lead_form_view_form'):
            arch = self.env.ref(xmlid).arch
            self.assertRegex(
                arch, r'name="crm_field_name"[^>]*widget="meta_target_selection"',
                xmlid)
        self.assertIn('open_form_view="1"', self.env.ref(
            'meta_lead_ads.meta_field_mapping_view_list').arch)

    def test_unarchive_duplicate_target_refused(self):
        self._city_mapping(meta_key='city_a')
        archived = self._city_mapping(meta_key='city_b', active=False)
        with self._refused('already mapped from another question'):
            archived.action_unarchive()

    # The Disabled Mappings list opens with active_test off, so these
    # writes come in with it off too.
    def test_unarchive_next_to_archived_twin(self):
        self._city_mapping(meta_key='city_a', active=False)
        twin = self._city_mapping(meta_key='city_b', active=False)
        twin.with_context(active_test=False).write({'active': True})
        self.assertTrue(twin.active)

    def test_retarget_next_to_archived_twin(self):
        self._city_mapping(meta_key='city_a', active=False)
        row = self._city_mapping(meta_key='street_q',
                                 crm_field_id=self._target('street').id)
        row.with_context(active_test=False).write({'crm_field_name': 'city'})
        self.assertEqual(row.crm_field_id.name, 'city')

    def test_unarchive_duplicate_refused_without_active_test(self):
        self._city_mapping(meta_key='city_a')
        archived = self._city_mapping(meta_key='city_b', active=False)
        with self._refused('already mapped from another question'):
            archived.with_context(active_test=False).action_unarchive()
        self.assertFalse(archived.active)

    def test_same_key_as_archived_row_hits_unique(self):
        self._city_mapping(meta_key='budget_q', active=False)
        with mute_logger('odoo.sql_db'), \
                self.assertRaises(IntegrityError) as cm, \
                self.env.cr.savepoint():
            self.SuMapping.create({
                'form_id': self.form.id, 'meta_key': 'budget_q',
                'crm_field_id': self._target('street').id})
            self.env.flush_all()
        # Odoo 18 has no _sql_error_to_message; this is the text it shows.
        message = translate_sql_constraint(
            self.env.cr, cm.exception.diag.constraint_name, 'en_US')
        self.assertIn('possibly a disabled one', message)
        self.assertIn('retarget', message)

    # ------------------------------------------------------------------ #
    # Canonical targets
    # ------------------------------------------------------------------ #
    def test_target_note_on_canonical_targets(self):
        expected = {'email_from': 'Email', 'phone': 'Phone',
                    'contact_name': 'Full Name',
                    'partner_name': 'Company Name'}
        for target, label in expected.items():
            row = self.SuMapping.create({
                'form_id': self.form.id, 'meta_key': 'custom_%s' % target,
                'crm_field_id': self._target(target).id})
            self.assertIn("Meta's standard %s answer fills this field first"
                          % label, row.target_note)
            self.assertIn('used only when that one is missing',
                          row.target_note)

    def test_target_note_empty_elsewhere(self):
        self.assertFalse(self._city_mapping().target_note)

    # ------------------------------------------------------------------ #
    # What the form shows about its disabled mappings
    # ------------------------------------------------------------------ #
    def test_disabled_count_and_notice_for_several_rows(self):
        self._legacy_mapping('user_id', meta_key='owner', active=False,
                             disabled_reason='Disabled on upgrade')
        self._legacy_mapping('team_id', meta_key='branch', active=False,
                             disabled_reason='Disabled on upgrade')
        # Archived by hand: no reason, so not counted.
        self._city_mapping(meta_key='old_city', active=False)
        self.form.invalidate_recordset()
        self.assertEqual(self.form.disabled_mapping_count, 2)
        self.assertTrue(self.form.disabled_mapping_notice.startswith(
            "2 mappings on this form were turned off"))

    def test_disabled_notice_for_one_row_and_none(self):
        self.assertEqual(self.form.disabled_mapping_count, 0)
        self.assertFalse(self.form.disabled_mapping_notice)
        self._legacy_mapping('user_id', meta_key='owner', active=False,
                             disabled_reason='Disabled on upgrade')
        self.form.invalidate_recordset()
        self.assertEqual(self.form.disabled_mapping_count, 1)
        self.assertTrue(self.form.disabled_mapping_notice.startswith(
            "One mapping on this form was turned off"))

    def test_mapping_count_follows_archiving(self):
        row = self._city_mapping()
        self._city_mapping(meta_key='street_q',
                           crm_field_id=self._target('street').id)
        self.assertEqual(self.form.mapping_count, 2)
        row.action_archive()
        self.env.flush_all()
        self.form.invalidate_recordset()
        self.assertEqual(self.form.mapping_count, 1)

    def test_mapping_count_ignores_caller_active_test(self):
        row = self._city_mapping()
        self._city_mapping(meta_key='street_q',
                           crm_field_id=self._target('street').id)
        row.with_context(active_test=False).action_archive()
        self.env.flush_all()
        self.form.invalidate_recordset()
        self.assertEqual(self.form.mapping_count, 1)
        # A recompute run with active_test off sees archived rows in
        # mapping_ids.
        form = self.form.with_context(active_test=False)
        form._compute_mapping_count()
        self.assertEqual(form.mapping_count, 1)

    def test_open_disabled_mappings_action(self):
        action = self.form.with_user(
            self.admin_a).action_open_disabled_mappings()
        self.assertEqual(action['type'], 'ir.actions.act_window')
        self.assertEqual(action['res_model'], 'meta.field.mapping')
        self.assertEqual(action['domain'], [('form_id', '=', self.form.id)])
        self.assertIs(action['context']['active_test'], False)
        self.assertEqual(action['context']['search_default_filter_disabled'],
                         1)

    def test_open_page_routing_is_admin_only(self):
        action = self.form.with_user(self.admin_a).action_open_page_routing()
        self.assertEqual(action['res_model'], 'meta.page')
        self.assertEqual(action['res_id'], self.form.page_id.id)
        self.assertEqual(action['view_mode'], 'form')
        meta_user = new_test_user(
            self.env, 'targets_meta_user',
            groups='base.group_user,meta_lead_ads.group_meta_user',
            company_id=self.co_a.id,
            company_ids=[Command.set([self.co_a.id])])
        with self.assertRaises(AccessError):
            self.form.with_user(meta_user).action_open_page_routing()


@tagged('post_install', '-at_install')
class TestLinkTargets(LinkTargetsMixin, MultiCompanyFixtureMixin,
                      IngestFixtureMixin, TransactionCase):
    """Fields an admin added that link the lead to a contact or a user.

    Apart from the class above because a failed import resets the
    registry, which drops the fields made for the whole class.
    """

    def setUp(self):
        super().setUp()
        self.Field = self.env['ir.model.fields'].sudo()
        self.Mapping = self.env['meta.field.mapping']
        self.SuMapping = self.Mapping.sudo()
        self.AdminMapping = self.Mapping.with_user(self.admin_a)

    _cold_cache = TestMappingTargets._cold_cache
    _target = TestMappingTargets._target
    _refused = TestMappingTargets._refused
    _legacy_mapping = TestMappingTargets._legacy_mapping

    def test_links_to_users_and_contacts_not_offered(self):
        names = {name for name, _label in self.Mapping._offered_targets()}
        self._cold_cache()
        options = dict(self.AdminMapping.fields_get(
            ['crm_field_name'])['crm_field_name']['selection'])
        for name in self.LINKS:
            self.assertNotIn(name, names)
            self.assertNotIn(name, options)
        # Links to anything else stay on the list.
        Lead = self.env['crm.lead']
        for name in ('country_id', 'state_id', 'lang_id', 'tag_ids'):
            if name in Lead._fields:
                self.assertIn(name, names)
                self.assertIn(name, options)

    def test_links_to_users_and_contacts_refused(self):
        for name in self.LINKS:
            for Mapping in (self.AdminMapping, self.SuMapping):
                with self.subTest(name=name, su=Mapping.env.su), \
                        self._refused(_BLOCKED_COPY):
                    Mapping.create({'form_id': self.form.id,
                                    'meta_key': 'q_%s' % name,
                                    'crm_field_name': name})
            with self.subTest(name=name, by='id'), \
                    self._refused(_BLOCKED_COPY):
                self.SuMapping.create({'form_id': self.form.id,
                                       'meta_key': 'q_%s' % name,
                                       'crm_field_id': self._target(name).id})
        self.assertEqual(self.Mapping.with_context(
            active_test=False).search_count(
                [('form_id', '=', self.form.id)]), 0)

    def test_archived_link_row_stays_off(self):
        row = self._legacy_mapping('x_referrer_id', active=False,
                                   disabled_reason='Disabled on upgrade')
        for env_mapping in (self.AdminMapping, self.SuMapping):
            self._cold_cache()
            with self._refused(_REACTIVATE_COPY):
                env_mapping.browse(row.id).action_unarchive()
        self.assertFalse(row.active)


@tagged('post_install', '-at_install')
class TestMappingCollisions(LinkTargetsMixin, MultiCompanyFixtureMixin,
                            IngestFixtureMixin, TransactionCase):
    """Two answers for one lead field: which one fills it, and the other is kept."""

    # ------------------------------------------------------------------ #
    # Helpers
    # ------------------------------------------------------------------ #
    def _field_id(self, name):
        return self.env['ir.model.fields'].sudo()._get('crm.lead', name).id

    def _row(self, meta_key, target):
        return self.env['meta.field.mapping'].sudo().create({
            'form_id': self.form.id, 'meta_key': meta_key,
            'crm_field_id': self._field_id(target)})

    def _rows_on(self, target, *keys):
        # Rows saved before one target per form was enforced. Each is created
        # on its own spare field, then all are moved onto the target with SQL.
        spares = ('street', 'street2', 'zip', 'website')
        rows = self.env['meta.field.mapping']
        for key, spare in zip(keys, spares):
            rows |= self._row(key, spare)
        self.env.flush_all()
        self.env.cr.execute(SQL(
            "UPDATE meta_field_mapping SET crm_field_id = %s WHERE id IN %s",
            self._field_id(target), tuple(rows.ids)))
        self.env.invalidate_all()
        return rows

    def _map(self, *pairs, ingest=None):
        data = {'form_id': 'F1', 'field_data': [
            {'name': key, 'values': [value]} for key, value in pairs]}
        # An empty recordset is falsy, so test against None.
        ingest = self.Ingest if ingest is None else ingest
        return ingest._map_fields(self.page, data, company=self.co_a)

    @staticmethod
    def _kept(answers, key=None):
        return [(a['question_key'], a['value']) for a in answers
                if key is None or a['question_key'] == key]

    @contextmanager
    def _log_lines(self):
        lines = []
        with mock.patch.object(meta_lead_ingest, '_logger') as logger:
            yield lines
        for level, args, _kwargs in logger.method_calls:
            if args and isinstance(args[0], str):
                lines.append((level, args[0] % args[1:]))

    # ------------------------------------------------------------------ #
    # Standard answer against a custom key on the same field
    # ------------------------------------------------------------------ #
    def test_standard_email_wins_when_listed_first(self):
        self._row('work_email', 'email_from')
        vals, answers, block = self._map(('email', 'a@x.com'),
                                         ('work_email', 'b@x.com'))
        self.assertEqual(vals['email_from'], 'a@x.com')
        self.assertEqual(self._kept(answers), [('work_email', 'b@x.com')])
        self.assertIn('work_email: b@x.com', block)

    def test_standard_email_wins_when_listed_last(self):
        self._row('work_email', 'email_from')
        vals, answers, block = self._map(('work_email', 'b@x.com'),
                                         ('email', 'a@x.com'))
        self.assertEqual(vals['email_from'], 'a@x.com')
        self.assertEqual(self._kept(answers), [('work_email', 'b@x.com')])
        self.assertIn('work_email: b@x.com', block)

    def test_custom_key_fills_when_standard_missing_or_blank(self):
        self._row('work_email', 'email_from')
        cases = ([('work_email', 'b@x.com')],
                 [('email', ''), ('work_email', 'b@x.com')],
                 [('work_email', 'b@x.com'), ('email', '')])
        for pairs in cases:
            with self.subTest(pairs=pairs):
                vals, answers, _block = self._map(*pairs)
                self.assertEqual(vals['email_from'], 'b@x.com')
                self.assertFalse(self._kept(answers, 'work_email'))
                self.assertFalse(self._kept(answers, 'email'))

    def test_blank_standard_answer_is_dropped(self):
        vals, answers, block = self._map(('email', ''), ('city_q', 'Giza'))
        self.assertNotIn('email_from', vals)
        self.assertEqual(self._kept(answers), [('city_q', 'Giza')])
        self.assertNotIn('email:', block)

    # ------------------------------------------------------------------ #
    # Custom rows against each other
    # ------------------------------------------------------------------ #
    def test_lowest_mapping_id_wins_on_legacy_duplicates(self):
        first, second = self._rows_on('city', 'alt1', 'alt2')
        self.assertLess(first.id, second.id)
        for pairs in ([('alt1', 'Cairo'), ('alt2', 'Giza')],
                      [('alt2', 'Giza'), ('alt1', 'Cairo')]):
            with self.subTest(pairs=pairs):
                vals, answers, block = self._map(*pairs)
                self.assertEqual(vals['city'], 'Cairo')
                self.assertEqual(self._kept(answers), [('alt2', 'Giza')])
                self.assertIn('alt2: Giza', block)

    def test_blank_text_answer_does_not_claim_its_field(self):
        self._rows_on('city', 'alt1', 'alt2')
        vals, answers, _block = self._map(('alt1', ''), ('alt2', 'Cairo'))
        self.assertEqual(vals['city'], 'Cairo')
        self.assertFalse(self._kept(answers, 'alt1'))
        self.assertFalse(self._kept(answers, 'alt2'))

    # ------------------------------------------------------------------ #
    # One question key sent more than once
    # ------------------------------------------------------------------ #
    def test_duplicate_custom_key_uses_first_entry(self):
        self._row('alt1', 'city')
        vals, answers, block = self._map(('alt1', 'first'), ('alt1', 'second'))
        self.assertEqual(vals['city'], 'first')
        self.assertEqual(self._kept(answers), [('alt1', 'second')])
        self.assertIn('alt1: second', block)

    def test_three_duplicates_keep_two_answers_in_order(self):
        self._row('alt1', 'city')
        vals, answers, _block = self._map(
            ('alt1', 'first'), ('alt1', 'second'), ('alt1', 'third'))
        self.assertEqual(vals['city'], 'first')
        self.assertEqual(self._kept(answers),
                         [('alt1', 'second'), ('alt1', 'third')])

    def test_duplicate_standard_key_uses_first_entry(self):
        vals, answers, block = self._map(('email', 'a@x.com'),
                                         ('email', 'z@x.com'))
        self.assertEqual(vals['email_from'], 'a@x.com')
        self.assertEqual(self._kept(answers), [('email', 'z@x.com')])
        self.assertIn('email: z@x.com', block)

    # ------------------------------------------------------------------ #
    # Rows that must never fill the lead
    # ------------------------------------------------------------------ #
    def test_archived_mapping_never_fills(self):
        row = self._row('city_q', 'city')
        row.action_archive()
        self.env.flush_all()
        self.env.invalidate_all()
        for ingest in (self.Ingest,
                       self.Ingest.with_context(active_test=False)):
            with self.subTest(active_test=ingest.env.context.get(
                    'active_test', True)):
                vals, answers, block = self._map(('city_q', 'Cairo'),
                                                 ingest=ingest)
                self.assertNotIn('city', vals)
                self.assertEqual(self._kept(answers), [('city_q', 'Cairo')])
                self.assertIn('city_q: Cairo', block)

    def test_blocked_row_never_fills(self):
        legacy_mapping(self.env, self.form, 'user_id', meta_key='owner')
        submitted = self.sales_a.name
        with self._log_lines() as lines:
            vals, answers, block = self._map(('owner', submitted))
        self.assertNotIn('user_id', vals)
        self.assertEqual(self._kept(answers), [('owner', submitted)])
        self.assertIn('owner: %s' % submitted, block)
        warnings = [line for level, line in lines if level == 'warning']
        self.assertEqual(len(warnings), 1, lines)
        self.assertIn("'owner'", warnings[0])
        self.assertIn('user_id', warnings[0])
        for _level, line in lines:
            self.assertNotIn(submitted, line)

    def test_link_to_contact_row_never_fills(self):
        # The answer names a real contact, so only the block stops it.
        referrer = self.env['res.partner'].create({'name': 'Referrer Contact'})
        legacy_mapping(self.env, self.form, 'x_referrer_id',
                       meta_key='referred_by')
        with self._log_lines() as lines:
            vals, answers, block = self._map(('referred_by', referrer.name))
        self.assertNotIn('x_referrer_id', vals)
        self.assertEqual(self._kept(answers), [('referred_by', referrer.name)])
        self.assertIn('referred_by: %s' % referrer.name, block)
        warnings = [line for level, line in lines if level == 'warning']
        self.assertEqual(len(warnings), 1, lines)
        self.assertIn("'referred_by'", warnings[0])
        self.assertIn('x_referrer_id', warnings[0])

    def test_mapping_on_standard_key_ignored(self):
        legacy_mapping(self.env, self.form, 'city', meta_key='email')
        vals, answers, _block = self._map(('email', 'a@x.com'))
        self.assertEqual(vals['email_from'], 'a@x.com')
        self.assertNotIn('city', vals)
        self.assertFalse(answers)

    # ------------------------------------------------------------------ #
    # What is kept, and what the log says
    # ------------------------------------------------------------------ #
    def test_answers_keep_field_data_order(self):
        self._row('work_email', 'email_from')
        _vals, answers, block = self._map(
            ('z_q', 'last letter'), ('work_email', 'b@x.com'),
            ('email', 'a@x.com'), ('a_q', 'first letter'))
        self.assertEqual(self._kept(answers), [
            ('z_q', 'last letter'), ('work_email', 'b@x.com'),
            ('a_q', 'first letter')])
        # The block is a div holding the heading and one line per answer.
        inner = str(block).removeprefix(
            '<div class="o_meta_lead_qa">').removesuffix('</div>')
        self.assertEqual(inner.split('<br>')[1:], [
            'z_q: last letter', 'work_email: b@x.com', 'a_q: first letter'])

    def test_collision_log_names_key_and_target_only(self):
        self._row('work_email', 'email_from')
        with self._log_lines() as lines:
            self._map(('work_email', 'b@x.com'), ('email', 'a@x.com'))
        collision = [line for _level, line in lines
                     if "'work_email'" in line and 'email_from' in line]
        self.assertEqual(len(collision), 1, lines)
        for _level, line in lines:
            self.assertNotIn('b@x.com', line)
            self.assertNotIn('a@x.com', line)

    def test_ingest_keeps_custom_email_as_answer(self):
        self._row('work_email', 'email_from')
        payload = self._fake_lead(id='LGCOL1', field_data=[
            {'name': 'full_name', 'values': ['Jane Doe']},
            {'name': 'work_email', 'values': ['work@example.com']},
            {'name': 'email', 'values': ['jane@example.com']}])
        with self._patch_graph(fetch_lead=payload):
            lead = self.Ingest.ingest_leadgen(self.page, 'LGCOL1', 'manual')
        self.assertEqual(lead.email_from, 'jane@example.com')
        kept = lead.answer_ids.filtered(
            lambda a: a.question_key == 'work_email')
        self.assertEqual(kept.mapped('value'), ['work@example.com'])
        self.assertIn('work_email: work@example.com', lead.description or '')
