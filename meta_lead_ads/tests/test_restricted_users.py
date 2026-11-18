# Meta Lead Ads Integration for Odoo — © 2026 CoreSys Builders. All rights reserved.
# Licensed under the Odoo Proprietary License v1.0 (OPL-1).
# Unauthorized copying, redistribution, or resale of this software, in whole or in
# part, via any medium, is strictly prohibited and constitutes a license violation.
# OPL-1: https://www.odoo.com/documentation/20.0/legal/licenses.html#odoo-apps

"""Lead forms and Meta actions as users with only the rights they need.

These run as the real restricted user, through get_views and web_read like
the browser does, so a field the user can't read shows up as an error here
instead of on someone's lead form.
"""
from contextlib import contextmanager
from datetime import date

from lxml import etree

from odoo import Command
from odoo.exceptions import AccessError, ValidationError
from odoo.tests import Form
from odoo.tests.common import TransactionCase, new_test_user, tagged

from odoo.addons.meta_lead_ads.models.const import (
    MAPPABLE_TTYPES, MAPPING_BLOCKED_ADDON_FIELDS, MAPPING_BLOCKED_FIELDS,
    MAPPING_BLOCKED_PREFIXES,
    MAPPING_FORBIDDEN_FIELDS, MAPPING_TECHNICAL_FIELDS,
    MAPPING_TECHNICAL_PREFIXES)
from odoo.addons.meta_lead_ads.models.meta_field_mapping import _like_prefix

from .common_typed_targets import LinkTargetsMixin, TypedTargetsMixin
from .test_ingest import IngestFixtureMixin
from .test_multi_company import MultiCompanyFixtureMixin

_SALES = 'base.group_user,sales_team.group_sale_salesman_all_leads'
_META_LINKS = ('meta_form_id_ref', 'meta_page_id_ref', 'meta_account_id')


@tagged('post_install', '-at_install')
class TestSalespersonLeadForm(MultiCompanyFixtureMixin, IngestFixtureMixin,
                              TransactionCase):

    def setUp(self):
        super().setUp()
        # The point of the class is a CRM user without any Meta group.
        self.assertFalse(self.sales_a.has_group('meta_lead_ads.group_meta_user'))
        self.Answer = self.env['meta.lead.answer'].sudo()

    # -- helpers --------------------------------------------------------- #
    def _meta_lead(self, name, leadgen_id, answers=2):
        lead = self.Lead.sudo().create({
            'name': name,
            'type': 'opportunity',
            'company_id': self.co_a.id,
            'meta_leadgen_id': leadgen_id,
            'meta_campaign_name': 'Spring',
            'meta_form_name': 'Contact Us',
            'meta_page_name': 'Page',
            'meta_form_id_ref': self.form.id,
            'meta_page_id_ref': self.page.id,
            'meta_account_id': self.account.id,
        })
        for i in range(answers):
            self.Answer.create({
                'lead_id': lead.id,
                'question_key': 'q%d' % i,
                'label': 'Question %d' % i,
                'value': 'Answer %d' % i,
            })
        return lead

    def _plain_lead(self, name):
        return self.Lead.sudo().create({
            'name': name, 'type': 'opportunity', 'company_id': self.co_a.id})

    def _arch(self, user, view_type='form'):
        views = self.Lead.with_user(user).get_views([(False, view_type)])
        return etree.fromstring(views['views'][view_type]['arch'])

    def _spec(self, arch):
        spec = {}
        for name in arch.xpath('//field/@name'):
            field = self.Lead._fields.get(name)
            if field is None:
                continue
            # The web client asks for sub-fields on relations; an empty
            # x2many spec would skip reading the comodel at all.
            if field.relational:
                spec[name] = {'fields': {'display_name': {}}}
            else:
                spec[name] = {}
        return spec

    def _new_user(self, login, groups):
        return new_test_user(self.env, login, groups=groups,
                             company_id=self.co_a.id)

    # -- salesperson ----------------------------------------------------- #
    def test_salesperson_opens_meta_lead_form(self):
        lead = self._meta_lead('RU open', 'LGRU_OPEN')
        Form(lead.with_user(self.sales_a))

        arch = self._arch(self.sales_a)
        # Answers follow the lead, so a salesperson gets the tab too.
        self.assertTrue(arch.xpath("//field[@name='answer_ids']"))
        self.assertTrue(arch.xpath("//page[@name='meta_answers']"))
        self.assertFalse(
            arch.xpath("//button[@name='action_promote_to_field']"))
        for name in _META_LINKS:
            self.assertFalse(arch.xpath("//field[@name='%s']" % name), name)
        self.assertTrue(arch.xpath("//field[@name='meta_leadgen_id']"))
        self.assertTrue(arch.xpath("//field[@name='meta_campaign_name']"))

    def test_salesperson_web_read_matches_view(self):
        meta = self._meta_lead('RU read', 'LGRU_READ')
        plain = self._plain_lead('RU plain')
        spec = self._spec(self._arch(self.sales_a))
        for lead in (meta, plain):
            values = lead.with_user(self.sales_a).web_read(spec)
            self.assertEqual(values[0]['id'], lead.id)
        values = meta.with_user(self.sales_a).web_read(spec)
        self.assertEqual(values[0]['meta_campaign_name'], 'Spring')
        self.assertIn('answer_ids', spec)
        self.assertEqual(
            sorted(row['id'] for row in values[0]['answer_ids']),
            sorted(meta.sudo().answer_ids.ids))
        self.assertEqual(len(values[0]['answer_ids']), 2)

    def test_salesperson_edits_and_saves_meta_lead(self):
        lead = self._meta_lead('RU edit', 'LGRU_EDIT')
        with Form(lead.with_user(self.sales_a)) as form:
            form.description = '<p>Called back</p>'
        self.assertIn('Called back', lead.description)
        self.assertEqual(lead.meta_leadgen_id, 'LGRU_EDIT')

    def test_salesperson_merges_and_deletes_meta_leads(self):
        lead1 = self._meta_lead('RU merge 1', 'LGRU_M1')
        lead2 = self._meta_lead('RU merge 2', 'LGRU_M2')
        merged = (lead1 | lead2).with_user(self.sales_a)._merge_opportunity(
            auto_unlink=True)
        self.assertEqual(len(merged), 1)
        self.assertEqual(len((lead1 | lead2).exists()), 1)
        # The merged-away lead's answers move to the one that stays.
        self.assertEqual(len(merged.sudo().answer_ids), 4)

        # Only Sales Administrators may delete leads in crm, so the delete
        # runs as one, still without a Meta group.
        self.assertFalse(
            self.sales_mgr_a.has_group('meta_lead_ads.group_meta_user'))
        lead3 = self._meta_lead('RU delete', 'LGRU_DEL')
        answers = lead3.sudo().answer_ids
        self.assertEqual(len(answers), 2)
        Form(lead3.with_user(self.sales_mgr_a))
        lead3.with_user(self.sales_mgr_a).unlink()
        self.assertFalse(lead3.exists())
        self.assertFalse(self.Answer.search([('id', 'in', answers.ids)]))

    # -- Meta groups keep their page ------------------------------------- #
    def test_meta_user_still_sees_answers(self):
        lead = self._meta_lead('RU meta user', 'LGRU_MU')
        meta_user = self._new_user(
            'ru_meta_user', _SALES + ',meta_lead_ads.group_meta_user')
        arch = self._arch(meta_user)
        self.assertTrue(arch.xpath("//field[@name='answer_ids']"))
        self.assertFalse(
            arch.xpath("//button[@name='action_promote_to_field']"))
        values = lead.with_user(meta_user).web_read(
            {'answer_ids': {'fields': {'value': {}}}})
        self.assertEqual(
            sorted(row['value'] for row in values[0]['answer_ids']),
            ['Answer 0', 'Answer 1'])
        Form(lead.with_user(meta_user))

        meta_admin = self._new_user(
            'ru_meta_admin', _SALES + ',meta_lead_ads.group_meta_admin')
        arch = self._arch(meta_admin)
        self.assertTrue(arch.xpath("//field[@name='answer_ids']"))
        self.assertTrue(
            arch.xpath("//button[@name='action_promote_to_field']"))

    # -- audit ----------------------------------------------------------- #
    def test_salesperson_views_reach_only_readable_models(self):
        for view_type in ('form', 'list', 'kanban', 'search'):
            arch = self._arch(self.sales_a, view_type)
            for name in set(arch.xpath('//field/@name')):
                field = self.Lead._fields.get(name)
                if field is None or not field.relational:
                    continue
                readable = self.env[field.comodel_name].with_user(
                    self.sales_a).has_access('read')
                if field.type in ('one2many', 'many2many'):
                    self.assertTrue(
                        readable, '%s view: %s -> %s' % (
                            view_type, name, field.comodel_name))
                elif not readable:
                    # web_read sends ids plus a sudo name for these, so only
                    # this module's own links are held to the stricter rule.
                    self.assertFalse(
                        name.startswith(('meta_', 'answer_')),
                        '%s view: %s -> %s' % (
                            view_type, name, field.comodel_name))


@tagged('post_install', '-at_install')
class TestMetaAdminMappedForm(TypedTargetsMixin, MultiCompanyFixtureMixin,
                              IngestFixtureMixin, TransactionCase):
    """Meta Admin tools on a form with field mappings, without Settings."""

    def setUp(self):
        super().setUp()
        self.crm_admin_a = new_test_user(
            self.env, 'ru_crm_admin_a',
            groups=_SALES + ',meta_lead_ads.group_meta_admin',
            company_id=self.co_a.id,
            company_ids=[Command.set([self.co_a.id])])
        # Only access-rights admins can read field metadata. These users
        # must not be able to, or the tests below prove nothing.
        for user in (self.admin_a, self.crm_admin_a):
            self.assertFalse(user.has_group('base.group_system'))
            self.assertFalse(user.has_group('base.group_erp_manager'))
            self.assertFalse(
                self.env['ir.model.fields'].with_user(user).has_access('read'))
        Field = self.env['ir.model.fields']
        Mapping = self.env['meta.field.mapping']
        Mapping.create({'form_id': self.form.id, 'meta_key': 'city',
                        'crm_field_id': Field._get('crm.lead', 'city').id})
        Mapping.create({
            'form_id': self.form.id, 'meta_key': 'start_date',
            'crm_field_id': Field._get('crm.lead', 'x_meta_typed_date').id})
        self.Tomb = self.env['meta.leadgen.tombstone']
        self.Answer = self.env['meta.lead.answer'].sudo()

    # -- helpers --------------------------------------------------------- #
    def _cold_cache(self):
        # setUp read the mapped fields as superuser. A real request starts
        # with an empty cache, so start each call the same way.
        self.env.flush_all()
        self.env.invalidate_all()

    def _payload(self, leadgen_id):
        return self._fake_lead(id=leadgen_id, field_data=[
            {'name': 'email', 'values': ['%s@example.com' % leadgen_id.lower()]},
            {'name': 'full_name', 'values': ['Mapped Person']},
            {'name': 'city', 'values': ['Cairo']},
            {'name': 'start_date', 'values': ['03/04/2026']},
        ])

    def _lead(self, leadgen_id):
        self.env.invalidate_all()
        return self.Lead.sudo().with_context(active_test=False).search(
            [('meta_leadgen_id', '=', leadgen_id)])

    def _assert_mapped(self, leadgen_id):
        lead = self._lead(leadgen_id)
        self.assertEqual(len(lead), 1)
        self.assertEqual(lead.city, 'Cairo')
        self.assertEqual(lead.x_meta_typed_date, date(2026, 4, 3))
        self.assertFalse(self.Answer.search([
            ('lead_id', '=', lead.id),
            ('question_key', 'in', ('city', 'start_date'))]))
        return lead

    # -- entry points ---------------------------------------------------- #
    def test_restore_on_mapped_form(self):
        lg = 'LGRA_RESTORE'
        self.Tomb._remember([lg], details={lg: {
            'page_id': self.page.id, 'company_id': self.co_a.id,
            'reason': 'deleted'}})
        tomb = self.Tomb.sudo().search([('leadgen_id', '=', lg)])
        self._cold_cache()
        with self._patch_graph(fetch_lead=self._payload(lg)):
            tomb.with_user(self.crm_admin_a).action_restore()
        self.assertFalse(tomb.exists())
        self._assert_mapped(lg)

    def test_ingest_by_id_on_mapped_form(self):
        lg = 'LGRA_WIZARD'
        wizard = self.env['meta.ingest.leadgen'].with_user(
            self.crm_admin_a).create({'page_id': self.page.id,
                                      'leadgen_id': lg})
        self._cold_cache()
        with self._patch_graph(fetch_lead=self._payload(lg)):
            wizard.action_ingest()
        self._assert_mapped(lg)

    def test_log_retry_on_mapped_form(self):
        lg = 'LGRA_LOG'
        row = self.env['meta.sync.log'].sudo()._record_failure(
            lg, 'webhook', 'boom', page=self.page)
        self._cold_cache()
        with self._patch_graph(fetch_lead=self._payload(lg)):
            self.env['meta.sync.log'].browse(row.ids).with_user(
                self.admin_a).action_retry()
        self._assert_mapped(lg)
        self.assertEqual(row.sudo().status, 'success')

    def test_queue_retry_on_mapped_form(self):
        lg = 'LGRA_QUEUE'
        event = self.env['meta.webhook.event'].sudo().create({
            'leadgen_id': lg, 'page_id': 'PG1', 'form_id': 'F1',
            'status': 'failed', 'error_message': 'boom'})
        self._cold_cache()
        with self._patch_graph(fetch_lead=self._payload(lg)):
            self.env['meta.webhook.event'].browse(event.ids).with_user(
                self.admin_a).action_retry()
        lead = self._assert_mapped(lg)
        self.assertEqual(event.sudo().status, 'done')
        self.assertEqual(event.sudo().lead_id, lead)

    def test_map_fields_as_meta_admin(self):
        data = self._payload('LGRA_MAP')
        self._cold_cache()
        vals, _answers, _block = self.env['meta.lead.ingest'].with_user(
            self.admin_a)._map_fields(self.page, data)
        self.assertEqual(vals['city'], 'Cairo')
        self.assertEqual(vals['x_meta_typed_date'], date(2026, 4, 3))

    def test_promote_already_promoted_as_meta_admin(self):
        lead = self.Lead.sudo().create({
            'name': 'RA promote', 'type': 'lead', 'company_id': self.co_a.id,
            'meta_leadgen_id': 'LGRA_PROMOTE',
            'meta_form_id_ref': self.form.id})
        answer = self.Answer.create({
            'lead_id': lead.id, 'question_key': 'budget',
            'label': 'Budget', 'value': '$5k'})
        self.env['meta.field.mapping'].create({
            'form_id': self.form.id, 'meta_key': 'budget',
            'crm_field_id': self.env['ir.model.fields']._get(
                'crm.lead', 'street').id})
        wizard = self.env['meta.promote.answer'].with_user(
            self.crm_admin_a).create({'answer_id': answer.id,
                                      'lead_id': lead.id})
        self._cold_cache()
        action = wizard.action_promote()
        self.assertEqual(action['type'], 'ir.actions.client')
        self.assertEqual(action['tag'], 'display_notification')
        label = self.Lead._fields['street'].get_description(
            self.env)['string']
        self.assertIn(label, action['params']['message'])

    # -- secrets stay with Meta Admins ----------------------------------- #
    def test_non_admins_still_cannot_read_secrets(self):
        meta_user = new_test_user(
            self.env, 'ru_meta_user_secrets',
            groups='base.group_user,meta_lead_ads.group_meta_user',
            company_id=self.co_a.id,
            company_ids=[Command.set([self.co_a.id])])
        for user in (meta_user, self.sales_a):
            for fname in ('access_token', 'app_secret'):
                with self.assertRaises(AccessError):
                    self.account.with_user(user).read([fname])
            with self.assertRaises(AccessError):
                self.page.with_user(user).read(['access_token'])


@tagged('post_install', '-at_install')
class TestMappingPickerMetaAdmin(LinkTargetsMixin, TypedTargetsMixin,
                                 MultiCompanyFixtureMixin, IngestFixtureMixin,
                                 TransactionCase):
    """Field mappings set up by a Meta Admin without Settings."""

    def setUp(self):
        super().setUp()
        self.assertFalse(self.admin_a.has_group('base.group_system'))
        self.assertFalse(self.admin_a.has_group('base.group_erp_manager'))
        self.assertFalse(
            self.env['ir.model.fields'].with_user(self.admin_a).has_access(
                'read'))
        self.Field = self.env['ir.model.fields']
        self.Mapping = self.env['meta.field.mapping']

    def _cold_cache(self):
        # Fixtures warm the cache as superuser, and cached field records skip
        # the access check. A real request starts cold.
        self.env.flush_all()
        self.env.invalidate_all()

    def _target(self, name):
        return self.Field._get('crm.lead', name)

    @contextmanager
    def _refused(self, message):
        # Several checks raise ValidationError, so the message tells which
        # one did. Odoo's assertRaises also rolls back the failed create.
        with self.assertRaises(ValidationError) as cm:
            yield
        self.assertRegex(str(cm.exception), message)

    def _allowed_names(self):
        # Built here on purpose instead of reusing the model's domain, so a
        # broken domain cannot make this test agree with itself.
        fields = self.Field.search([
            ('model', '=', 'crm.lead'), ('store', '=', True),
            ('readonly', '=', False), ('ttype', 'in', list(MAPPABLE_TTYPES)),
            ('name', 'not in', sorted(MAPPING_FORBIDDEN_FIELDS)),
            ('name', 'not in', sorted(MAPPING_BLOCKED_FIELDS)),
            ('name', 'not in', sorted(MAPPING_BLOCKED_ADDON_FIELDS)),
        ] + [('name', 'not =like', _like_prefix(prefix))
             for prefix in MAPPING_BLOCKED_PREFIXES])
        # Spelled out rather than read from the constant, for the same
        # reason as above.
        fields = fields.filtered(
            lambda f: f.relation not in ('res.users', 'res.partner'))
        return {n for n in fields.mapped('name')
                if n not in MAPPING_TECHNICAL_FIELDS
                and not n.startswith(MAPPING_TECHNICAL_PREFIXES)}

    def _options(self):
        self._cold_cache()
        return dict(self.Mapping.with_user(self.admin_a).fields_get(
            ['crm_field_name'])['crm_field_name']['selection'])

    def _legacy_mapping(self, name, meta_key='fav_colour', form=None):
        # A row saved before the field was taken off the list. The ORM
        # would refuse it now, so the target is put back with SQL.
        mapping = self.Mapping.create({
            'form_id': (form or self.form).id, 'meta_key': meta_key,
            'crm_field_id': self._target('city').id})
        self.env.flush_all()
        self.env.cr.execute(
            "UPDATE meta_field_mapping SET crm_field_id = %s WHERE id = %s",
            (self._target(name).id, mapping.id))
        self.env.invalidate_all()
        return mapping

    def test_technical_fields_not_offered(self):
        options = self._options()
        for name in ('message_bounce', 'color', 'iap_enrich_done',
                     'message_main_attachment_id', 'date_open',
                     'date_last_stage_update', 'reveal_id',
                     'lead_mining_request_id'):
            self.assertNotIn(name, options)
        for name in options:
            self.assertNotIn(name, MAPPING_TECHNICAL_FIELDS)
            self.assertFalse(name.startswith(MAPPING_TECHNICAL_PREFIXES),
                             name)

    def test_business_fields_offered(self):
        options = self._options()
        keep = {'city', 'phone', 'email_from', 'email_cc', 'function',
                'street', 'street2', 'zip', 'country_id', 'state_id',
                'website', 'partner_name', 'contact_name', 'tag_ids',
                'referred', 'lang_id'}
        Lead = self.env['crm.lead']
        present = {n for n in keep if n in Lead._fields}
        # Odoo renames fields between versions; most of the list must exist.
        self.assertGreaterEqual(len(present), 15)
        for name in present:
            field = self._target(name)
            if (field.store and not field.readonly
                    and field.ttype in MAPPABLE_TTYPES):
                self.assertIn(name, options)
        # The sales team's own fields are never on the list.
        for name in sorted(MAPPING_BLOCKED_FIELDS) + ['recurring_plan']:
            self.assertNotIn(name, options)

    def test_technical_target_refused(self):
        self._cold_cache()
        with self._refused('Pick a CRM lead field'):
            self.Mapping.with_user(self.admin_a).create({
                'form_id': self.form.id, 'meta_key': 'x',
                'crm_field_name': 'color'})
        with self._refused('technical field'):
            self.Mapping.create({
                'form_id': self.form.id, 'meta_key': 'y',
                'crm_field_id': self._target('message_bounce').id})
        # The record itself, sent without the name, skips the name gate.
        target = self._target('message_bounce')
        self._cold_cache()
        with self._refused('technical field'):
            self.Mapping.with_user(self.admin_a).create({
                'form_id': self.form.id, 'meta_key': 'z',
                'crm_field_id': target.id})

    def test_legacy_technical_mapping_keeps_working(self):
        mapping = self._legacy_mapping('color')
        self.assertEqual(mapping.crm_field_name, 'color')
        options = self._options()
        self.assertEqual(options.get('color'),
                         '%s (color)' % self._target('color').field_description)

        legacy = mapping.with_user(self.admin_a)
        self._cold_cache()
        legacy.write({'meta_key': 'colour'})
        legacy.write({'date_format': False})
        # A client sending back the unchanged name must not trip the gate.
        legacy.write({'crm_field_name': 'color'})
        self._cold_cache()
        with Form(legacy) as f:
            self.assertEqual(f.crm_field_name, 'color')
            f.meta_key = 'favourite_colour'
        self.assertEqual(mapping.sudo().meta_key, 'favourite_colour')
        self.assertEqual(mapping.sudo().crm_field_id, self._target('color'))

        self._cold_cache()
        with self._refused('no longer offered'):
            self.Mapping.with_user(self.admin_a).create({
                'form_id': self.form.id, 'meta_key': 'other',
                'crm_field_name': 'color'})
        # The unchanged name only passes when the record sent with it is
        # the current one too.
        city = self._target('city')
        self._cold_cache()
        with self._refused('no longer offered'):
            legacy.write({'crm_field_name': 'color', 'crm_field_id': city.id})
        self.assertEqual(mapping.sudo().crm_field_id, self._target('color'))

        self._cold_cache()
        legacy.write({'crm_field_name': 'city'})
        self.assertEqual(mapping.sudo().crm_field_id, self._target('city'))
        self.assertNotIn('color', self._options())

    def test_legacy_options_only_from_visible_rows(self):
        form_b = self.env['meta.lead.form'].create({
            'name': 'Form B', 'form_id': 'FB', 'page_id': self.page_b.id})
        self._legacy_mapping('color', form=form_b)
        self.assertNotIn('color', self._options())
        self._cold_cache()
        options_b = dict(self.Mapping.with_user(self.admin_b).fields_get(
            ['crm_field_name'])['crm_field_name']['selection'])
        self.assertIn('color', options_b)

    def test_picking_legacy_option_warns_and_reverts(self):
        self._legacy_mapping('color')
        mapping = self.Mapping.create({
            'form_id': self.form.id, 'meta_key': 'town',
            'crm_field_id': self._target('city').id})
        self.assertIn('color', self._options())
        self._cold_cache()
        with self.assertLogs('odoo.tests.form.onchange', 'WARNING') as logs:
            with Form(mapping.with_user(self.admin_a)) as f:
                f.crm_field_name = 'color'
                self.assertEqual(f.crm_field_name, 'city')
                self.assertEqual(f.crm_field_type, 'char')
        self.assertIn('no longer offered', logs.output[0])
        self.assertEqual(mapping.sudo().crm_field_id, self._target('city'))

    def test_legacy_technical_mapping_ingests(self):
        self._legacy_mapping('color')
        payload = self._fake_lead(id='LGQ_LEGACY', field_data=[
            {'name': 'email', 'values': ['legacy@example.com']},
            {'name': 'full_name', 'values': ['Legacy Person']},
            {'name': 'fav_colour', 'values': ['3']},
        ])
        with self._patch_graph(fetch_lead=payload):
            lead = self.Ingest.ingest_leadgen(self.page, 'LGQ_LEGACY',
                                              'manual')
        self.assertTrue(lead)
        self.assertEqual(lead.sudo().color, 3)

    def test_legacy_forbidden_mapping_not_filled(self):
        self._legacy_mapping('active')
        payload = self._fake_lead(id='LGQ_FORBIDDEN', field_data=[
            {'name': 'email', 'values': ['forbidden@example.com']},
            {'name': 'full_name', 'values': ['Forbidden Person']},
            {'name': 'fav_colour', 'values': ['no']},
        ])
        with self.assertLogs('odoo.addons.meta_lead_ads', 'WARNING'), \
                self._patch_graph(fetch_lead=payload):
            lead = self.Ingest.ingest_leadgen(self.page, 'LGQ_FORBIDDEN',
                                              'manual')
        lead = lead.sudo()
        self.assertTrue(lead.active)
        answer = lead.answer_ids.filtered(
            lambda a: a.question_key == 'fav_colour')
        self.assertEqual(answer.value, 'no')

    def test_views_offer_proxy_not_many2one(self):
        self._cold_cache()
        Mapping = self.Mapping.with_user(self.admin_a)
        views = Mapping.get_views([(False, 'list'), (False, 'form')])
        for view_type in ('list', 'form'):
            arch = etree.fromstring(views['views'][view_type]['arch'])
            self.assertTrue(arch.xpath("//field[@name='crm_field_name']"),
                            view_type)
            self.assertFalse(arch.xpath("//field[@name='crm_field_id']"),
                             view_type)

        views = self.env['meta.lead.form'].with_user(
            self.admin_a).get_views([(False, 'form')])
        arch = etree.fromstring(views['views']['form']['arch'])
        inline = arch.xpath("//field[@name='mapping_ids']")
        self.assertTrue(inline)
        self.assertTrue(inline[0].xpath(".//field[@name='crm_field_name']"))
        self.assertFalse(inline[0].xpath(".//field[@name='crm_field_id']"))
        sub_fields = views['models']['meta.field.mapping']['fields']
        self.assertIn('crm_field_name', sub_fields)
        self.assertNotIn('crm_field_id', sub_fields)

    def test_options_match_target_domain(self):
        self._cold_cache()
        desc = self.Mapping.with_user(self.admin_a).fields_get(
            ['crm_field_name'])['crm_field_name']
        options = dict(desc['selection'])
        self.assertEqual(set(options), self._allowed_names())
        self.assertIn('city', options)
        self.assertIn('x_meta_typed_date', options)
        for name in ('company_id', 'meta_leadgen_id', 'create_date',
                     'recurring_revenue', *MAPPING_BLOCKED_FIELDS):
            self.assertNotIn(name, options)
        city = self._target('city')
        self.assertEqual(options['city'],
                         '%s (city)' % city.field_description)

    def test_prefix_patterns_are_literal(self):
        # The domain must agree with startswith, so _ is not a wildcard.
        for prefix in MAPPING_TECHNICAL_PREFIXES + MAPPING_BLOCKED_PREFIXES:
            pattern = _like_prefix(prefix)
            for name in (prefix + 'x', prefix.replace('_', 'z') + 'x'):
                self.env.cr.execute("SELECT %s LIKE %s", (name, pattern))
                self.assertEqual(self.env.cr.fetchone()[0],
                                 name.startswith(prefix), name)

    def test_form_create_and_switch_as_meta_admin(self):
        self._cold_cache()
        with Form(self.Mapping.with_user(self.admin_a)) as f:
            f.form_id = self.form
            f.meta_key = 'start_date'
            f.crm_field_name = 'phone'
            self.assertFalse(f.date_format)
            self.assertEqual(f.crm_field_type, 'char')
            f.crm_field_name = 'x_meta_typed_date'
            self.assertEqual(f.date_format, 'dmy')
            self.assertEqual(f.crm_field_type, 'date')
        mapping = f.record.sudo()
        self.assertEqual(mapping.crm_field_id,
                         self._target('x_meta_typed_date'))
        self.assertEqual(mapping.date_format, 'dmy')

        self._cold_cache()
        with Form(f.record) as f2:
            self.assertEqual(f2.crm_field_name, 'x_meta_typed_date')
            f2.crm_field_name = 'city'
            self.assertFalse(f2.date_format)
        self.assertEqual(mapping.crm_field_id, self._target('city'))
        self.assertFalse(mapping.date_format)

    def test_inline_list_as_meta_admin(self):
        self._cold_cache()
        with Form(self.form.with_user(self.admin_a)) as f:
            with f.mapping_ids.new() as line:
                line.meta_key = 'city'
                line.crm_field_name = 'city'
        self.assertTrue(self.Mapping.search([
            ('form_id', '=', self.form.id),
            ('crm_field_id', '=', self._target('city').id)]))

    def test_unknown_or_forbidden_name_refused(self):
        Mapping = self.Mapping.with_user(self.admin_a)
        self._cold_cache()
        with self._refused('Pick a CRM lead field'):
            Mapping.create({'form_id': self.form.id, 'meta_key': 'x',
                            'crm_field_name': 'no_such_field'})
        # System fields are refused before the list is even looked at.
        self._cold_cache()
        with self._refused("can't be filled from a form answer"):
            Mapping.create({'form_id': self.form.id, 'meta_key': 'x',
                            'crm_field_name': 'company_id'})
        mapping = self.Mapping.create({
            'form_id': self.form.id, 'meta_key': 'city',
            'crm_field_id': self._target('city').id})
        self._cold_cache()
        with self._refused("can't be filled from a form answer"):
            mapping.with_user(self.admin_a).write(
                {'crm_field_name': 'meta_leadgen_id'})

    def test_mismatched_pair_refused(self):
        self._cold_cache()
        with self._refused("don't match"):
            self.Mapping.with_user(self.admin_a).create({
                'form_id': self.form.id, 'meta_key': 'x',
                'crm_field_id': self._target('city').id,
                'crm_field_name': 'phone'})

    def test_existing_paths_unchanged(self):
        # The promote wizard writes only the record.
        mapping = self.Mapping.create({
            'form_id': self.form.id, 'meta_key': 'budget',
            'crm_field_id': self._target('street').id})
        self.assertEqual(mapping.crm_field_name, 'street')
