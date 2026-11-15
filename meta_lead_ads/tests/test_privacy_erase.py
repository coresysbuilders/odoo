# Meta Lead Ads Integration for Odoo — © 2026 CoreSys Builders. All rights reserved.
# Licensed under the Odoo Proprietary License v1.0 (OPL-1).
# Unauthorized copying, redistribution, or resale of this software, in whole or in
# part, via any medium, is strictly prohibited and constitutes a license violation.
# OPL-1: https://www.odoo.com/documentation/18.0/legal/licenses.html#odoo-apps

"""Erasing a Meta lead's personal data, and the record of which lead
fields Meta filled that the erase relies on. Also the erase of sync and
queue rows that never became a lead."""
import json
from unittest.mock import patch

from lxml import etree

from odoo import _, fields
from odoo.api import call_kw
from odoo.exceptions import AccessError, UserError
from odoo.fields import Command
from odoo.tests.common import TransactionCase, new_test_user, tagged
from odoo.tools import format_date
from odoo.tools.safe_eval import safe_eval

from odoo.addons.meta_lead_ads.models import crm_lead as crm_lead_module
from odoo.addons.meta_lead_ads.models import (
    meta_sync_log as meta_sync_log_module)
from odoo.addons.meta_lead_ads.models.exceptions import MetaLeadRemovedError

from .common_typed_targets import TypedTargetsMixin
from .test_ingest import IngestFixtureMixin
from .test_multi_company import MultiCompanyFixtureMixin

_ERASED_NAME = 'Meta lead (personal data erased)'
_LEGACY = ("This lead was imported before Odoo recorded which fields Meta "
           "filled, so a field whose mapping was changed earlier may still "
           "hold a Meta answer.")
_HISTORY_REMOVED = ("Where a field was kept, the entries in its history that "
                    "showed a value from the stored Meta data were removed.")
_HISTORY_UNCHECKED = "Review them and their history"
_EXPIRED = "the stored copy of a merged Meta lead has expired"
_RETENTION = ("The stored Meta data is kept only for the sync log retention "
              "period, 14 days by default.")


class EraseFixtureMixin(TypedTargetsMixin):
    """Meta lead helpers shared by the inventory and erase tests."""

    # A plain text field like the ones the promote wizard creates.
    _TYPED_FIELDS = dict(TypedTargetsMixin._TYPED_FIELDS, x_meta_budget_text=(
        'char', 'Budget Text', {}))

    def _map_row(self, key, field_name, **vals):
        if field_name in self.TYPED:
            field = self.TYPED[field_name]
        else:
            field = self.env['ir.model.fields']._get('crm.lead', field_name)
        return self.env['meta.field.mapping'].create(dict(
            vals, form_id=self.form.id, meta_key=key, crm_field_id=field.id))

    def _ingest(self, leadgen_id, field_data):
        with self._patch_graph(fetch_lead=self._fake_lead(
                id=leadgen_id, field_data=field_data)):
            return self.Ingest.ingest_leadgen(self.page, leadgen_id, 'manual')

    def _contact(self, n, extra=()):
        data = [
            {'name': 'email', 'values': ['person%s@example.com' % n]},
            {'name': 'full_name', 'values': ['Person %s' % n]},
            {'name': 'phone_number', 'values': ['+1 555 010 %04d' % n]},
        ]
        data.extend(extra)
        return data

    def _make_legacy(self, lead):
        # As a lead ingested before the inventory existed.
        self.env.flush_all()
        self.env.cr.execute(
            "UPDATE crm_lead SET meta_filled_fields = NULL WHERE id = %s",
            [lead.id])
        self.env.invalidate_all()

    def _filled(self, lead):
        return lead.sudo().meta_filled_fields

    def _give_sales(self, user):
        user.groups_id |= self.env.ref(
            'sales_team.group_sale_salesman_all_leads')

    def _erase(self, leads, user=None):
        user = user or self.admin_a
        return leads.with_user(user).action_meta_erase_personal_data()

    def _flush_tracking(self):
        # Tracking values are written by a precommit hook, which a test
        # transaction never reaches on its own.
        self.env.flush_all()
        self.env.cr.precommit.run()
        self.env.invalidate_all()

    def _notes(self, lead):
        return lead.sudo().message_ids.filtered(
            lambda m: 'Personal data erased by' in (m.body or ''))

    def _queue_row(self, leadgen_id):
        return self.env['meta.webhook.event'].sudo().create({
            'leadgen_id': leadgen_id, 'page_id': 'PG1', 'form_id': 'F1',
            'status': 'done', 'raw_payload': '{"leadgen_id": "%s"}' % leadgen_id})

    def _copies(self, leadgen_id):
        logs = self.env['meta.sync.log'].sudo().search(
            [('meta_leadgen_id', '=', leadgen_id)])
        events = self.env['meta.webhook.event'].sudo().search(
            [('leadgen_id', '=', leadgen_id)])
        return logs, events

    def _assert_copies_cleared(self, leadgen_id):
        logs, events = self._copies(leadgen_id)
        self.assertTrue(logs, leadgen_id)
        self.assertTrue(events, leadgen_id)
        for row in list(logs) + list(events):
            self.assertFalse(row.raw_payload, leadgen_id)
            self.assertEqual(row.payload_cleared, 'erased', leadgen_id)

    def _assert_copies_kept(self, leadgen_id):
        logs, events = self._copies(leadgen_id)
        self.assertTrue(logs, leadgen_id)
        self.assertTrue(events, leadgen_id)
        for row in list(logs) + list(events):
            self.assertTrue(row.raw_payload, leadgen_id)
            self.assertFalse(row.payload_cleared, leadgen_id)


@tagged('post_install', '-at_install')
class TestFilledFieldInventory(EraseFixtureMixin, MultiCompanyFixtureMixin,
                               IngestFixtureMixin, TransactionCase):

    def test_new_lead_lists_every_field_meta_filled(self):
        self._map_row('job', 'function')
        self._map_row('ready_on', 'x_meta_typed_date')
        lead = self._ingest('LGINV_NEW', self._contact(1, [
            {'name': 'job', 'values': ['Buyer']},
            {'name': 'ready_on', 'values': ['2026-07-01']},
            {'name': 'what_is_your_budget', 'values': ['$5k']},
        ]))
        self.assertEqual(lead.function, 'Buyer')
        self.assertEqual(
            self._filled(lead),
            ',contact_name,email_from,function,phone,x_meta_typed_date,')

    def test_lead_without_mapped_answers_is_still_tracked(self):
        lead = self._ingest('LGINV_NONE', [
            {'name': 'what_is_your_budget', 'values': ['$5k']}])
        self.assertEqual(self._filled(lead), ',')

    def test_enrichment_lists_only_fields_this_submission_fills(self):
        self._map_row('job', 'function')
        seed = self.Lead.create({
            'name': 'Seed', 'type': 'lead', 'user_id': False,
            'team_id': False, 'email_from': 'seed@example.com',
            'company_id': self.co_a.id})
        self.assertFalse(seed.function)
        lead = self._ingest('LGINV_ENR', [
            {'name': 'email', 'values': ['seed@example.com']},
            {'name': 'job', 'values': ['Buyer']},
        ])
        self.assertEqual(lead, seed)
        self.assertEqual(lead.function, 'Buyer')
        self.assertEqual(self._filled(lead), ',function,')

    def test_promote_backfill_adds_the_new_field(self):
        tracked = self._ingest('LGINV_PRO', self._contact(2, [
            {'name': 'what_is_your_budget', 'values': ['$5k']}]))
        legacy = self._ingest('LGINV_PROL', self._contact(3, [
            {'name': 'what_is_your_budget', 'values': ['$9k']}]))
        self._make_legacy(legacy)
        field = self.TYPED['x_meta_budget_text']
        wizard = self.env['meta.promote.answer'].new(
            {'question_key': 'what_is_your_budget'})
        wizard._run_backfill(field, self.form)
        self.assertEqual(tracked.x_meta_budget_text, '$5k')
        self.assertEqual(legacy.x_meta_budget_text, '$9k')
        self.assertEqual(
            self._filled(tracked),
            ',contact_name,email_from,phone,x_meta_budget_text,')
        # A legacy lead stays legacy, so the erase keeps its wider sweep.
        self.assertFalse(self._filled(legacy))

    def test_merge_gives_the_head_both_inventories(self):
        self._map_row('job', 'function')
        first = self._ingest('LGINV_M1', self._contact(4, [
            {'name': 'job', 'values': ['Buyer']}]))
        second = self._ingest('LGINV_M2', [
            {'name': 'email', 'values': ['other@example.com']},
            {'name': 'ready_on', 'values': ['x']}])
        head = (first | second)._merge_opportunity(auto_unlink=True)
        self.assertEqual(len((first | second).exists()), 1)
        self.assertEqual(
            self._filled(head),
            ',contact_name,email_from,function,phone,')

    def test_merge_with_legacy_lead_leaves_head_untracked(self):
        first = self._ingest('LGINV_L1', self._contact(5))
        second = self._ingest('LGINV_L2', self._contact(6))
        self._make_legacy(second)
        head = (first | second)._merge_opportunity(auto_unlink=True)
        self.assertFalse(self._filled(head))

    def test_merge_with_plain_lead_keeps_meta_inventory(self):
        meta = self._ingest('LGINV_P1', self._contact(7))
        plain = self.Lead.create({
            'name': 'Plain', 'type': 'lead', 'function': 'Owner',
            'company_id': self.co_a.id})
        head = (meta | plain)._merge_opportunity(auto_unlink=True)
        self.assertEqual(self._filled(head),
                         ',contact_name,email_from,phone,')

    def test_merge_into_a_plain_head_lists_only_copied_fields(self):
        # A hand-made opportunity keeps its own values in a merge; only
        # the fields crm filled from the Meta lead are Meta's.
        self._map_row('job', 'function')
        for n, legacy in ((9, False), (10, True)):
            meta = self._ingest('LGINV_PH%d' % n, self._contact(n, [
                {'name': 'job', 'values': ['Buyer']}]))
            if legacy:
                self._make_legacy(meta)
            plain = self.Lead.create({
                'name': 'Own deal %d' % n, 'type': 'opportunity',
                'phone': '+1 555 999 %04d' % n, 'city': 'Springfield',
                'company_id': self.co_a.id})
            head = (meta | plain)._merge_opportunity(auto_unlink=True)
            self.assertEqual(head, plain)
            self.assertEqual(head.phone, '+1 555 999 %04d' % n)
            self.assertEqual(head.function, 'Buyer')
            self.assertEqual(self._filled(head),
                             ',contact_name,email_from,function,', legacy)

    def test_inventory_refused_over_rpc(self):
        sales_group = self.env.ref('sales_team.group_sale_salesman_all_leads')
        self.admin_a.groups_id |= sales_group
        lead = self._ingest('LGINV_RPC', self._contact(8))
        for user in (self.admin_a, self.sales_a):
            with self.assertRaises(AccessError):
                self.Lead.with_user(user).create({
                    'name': 'Planted', 'meta_filled_fields': ',phone,'})
            with self.assertRaises(AccessError):
                lead.with_user(user).write({'meta_filled_fields': ','})
        planted = self.Lead.with_user(self.sales_a).with_context(
            default_meta_filled_fields=',phone,').create({'name': 'Default'})
        self.assertFalse(self._filled(planted))
        self.assertEqual(self._filled(lead), ',contact_name,email_from,phone,')


@tagged('post_install', '-at_install')
class TestErase(EraseFixtureMixin, MultiCompanyFixtureMixin,
                IngestFixtureMixin, TransactionCase):

    def setUp(self):
        super().setUp()
        self._give_sales(self.admin_a)

    def _note_text(self, user=None):
        user = user or self.admin_a
        env = self.env(user=user)
        return _("Personal data erased by %(user)s on %(date)s. The linked "
                 "contact and hand-written messages were not changed.",
                 user=user.name,
                 date=format_date(env, fields.Date.context_today(
                     env['crm.lead'])))

    def _mapped_lead(self, leadgen_id, n):
        self.row = self._map_row('job', 'function')
        lead = self._ingest(leadgen_id, self._contact(n, [
            {'name': 'job', 'values': ['Buyer']}]))
        self.assertEqual(lead.function, 'Buyer')
        return lead

    # -- what goes and what stays ---------------------------------------- #
    def test_full_erase(self):
        self._map_row('town', 'city')
        dated = self._map_row('ready_on', 'x_meta_typed_date')
        lead = self._ingest('LGER_FULL', self._contact(11, [
            {'name': 'company_name', 'values': ['Acme']},
            {'name': 'town', 'values': ['Springfield']},
            {'name': 'ready_on', 'values': ['2026-07-01']},
            {'name': 'what_is_your_budget', 'values': ['$5k']},
        ]))
        dated.active = False
        self.assertEqual(lead.partner_name, 'Acme')
        email, phone = lead.email_from, lead.phone
        partner = self.env['res.partner'].create({
            'name': 'Person 11', 'email': email, 'phone': phone})
        lead.write({
            'partner_id': partner.id, 'user_id': self.sales_a.id,
            'expected_revenue': 5000, 'street': 'Main St 1',
            'street2': 'Floor 2', 'zip': '12345',
            'country_id': self.env.ref('base.us').id,
        })
        self._queue_row('LGER_FULL')
        self.assertTrue(lead.answer_ids)
        self.assertEqual(lead.city, 'Springfield')
        self.assertTrue(lead.x_meta_typed_date)
        kept_fields = ('stage_id', 'user_id', 'team_id', 'type',
                       'company_id', 'expected_revenue', 'partner_id',
                       'meta_campaign_name', 'meta_campaign_id',
                       'meta_form_name', 'meta_leadgen_id', 'source_id',
                       'medium_id', 'campaign_id')
        kept = {name: lead[name] for name in kept_fields}
        self.assertTrue(kept['campaign_id'])
        self._flush_tracking()
        last_tracking = self.env['mail.tracking.value'].sudo().search(
            [], order='id desc', limit=1).id

        result = self._erase(lead)
        self.assertEqual(result['tag'], 'display_notification')
        self._flush_tracking()

        for name in ('contact_name', 'partner_name', 'email_from', 'phone',
                     'street', 'street2', 'zip', 'city', 'state_id',
                     'country_id', 'x_meta_typed_date', 'email_normalized',
                     'phone_sanitized', 'meta_phone_normalized'):
            self.assertFalse(lead[name], name)
        self.assertEqual(lead.name, _ERASED_NAME)
        self.assertFalse(lead.meta_name_is_generated)
        self.assertFalse(lead.sudo().answer_ids)
        self.assertTrue(lead.meta_personal_data_erased_at)
        for name, value in kept.items():
            self.assertEqual(lead[name], value, name)
        self.assertEqual(partner.email, email)
        self.assertEqual(partner.phone, phone)
        self.assertFalse(self.env['mail.tracking.value'].sudo().search(
            [('id', '>', last_tracking)]))
        self._assert_copies_cleared('LGER_FULL')
        notes = self._notes(lead)
        self.assertEqual(len(notes), 1)
        self.assertIn(self._note_text(), notes.body)
        self.assertNotIn('imported before', notes.body)
        self.assertNotIn(email, notes.body)

    def test_tracking_of_cleared_fields_removed(self):
        lead = self._ingest('LGER_TRK', self._contact(12))
        self._flush_tracking()
        email, phone = lead.email_from, lead.phone
        lead.write({'email_from': 'second@example.com',
                    'phone': '+1 555 010 9999', 'expected_revenue': 700})
        self._flush_tracking()
        Tracking = self.env['mail.tracking.value'].sudo()
        on_lead = [('mail_message_id.model', '=', 'crm.lead'),
                   ('mail_message_id.res_id', '=', lead.id)]
        found = Tracking.search(on_lead)
        self.assertTrue(Tracking.search(
            on_lead + [('old_value_char', '=', email)]),
            [(t.field_id.name, t.old_value_char, t.new_value_char)
             for t in found])
        kept = Tracking.search(
            on_lead + [('field_id.name', '=', 'expected_revenue')])
        self.assertTrue(kept)
        # A change still waiting for its tracking row when the erase runs
        # must not be written with the old value afterwards.
        lead.write({'email_from': 'third@example.com'})

        self._erase(lead)
        self._flush_tracking()

        values = ['second@example.com', 'third@example.com', email, phone,
                  '+1 555 010 9999']
        self.assertFalse(Tracking.search(on_lead + [
            '|', ('old_value_char', 'in', values),
            ('new_value_char', 'in', values)]))
        self.assertFalse(Tracking.search(on_lead + [
            ('field_id.name', 'in', ['email_from', 'phone'])]))
        # Other tracked changes stay.
        self.assertTrue(kept.exists())

    def test_merged_in_copies_cleared(self):
        first = self._ingest('LGER_MG1', self._contact(13))
        second = self._ingest('LGER_MG2', self._contact(14))
        self._queue_row('LGER_MG1')
        self._queue_row('LGER_MG2')
        head = (first | second)._merge_opportunity(auto_unlink=True)
        self._erase(head)
        self._assert_copies_cleared('LGER_MG1')
        self._assert_copies_cleared('LGER_MG2')

    # -- mappings that changed since the lead arrived -------------------- #
    def test_erase_after_mapping_retargeted(self):
        lead = self._mapped_lead('LGER_RT', 15)
        self.row.crm_field_id = self.env['ir.model.fields']._get(
            'crm.lead', 'website')
        self._erase(lead)
        self.assertFalse(lead.function)

    def test_erase_after_mapping_deleted(self):
        lead = self._mapped_lead('LGER_DL', 16)
        self.row.unlink()
        self._erase(lead)
        self.assertFalse(lead.function)

    def test_erase_after_form_link_cleared(self):
        lead = self._mapped_lead('LGER_FL', 17)
        lead.sudo().meta_form_id_ref = False
        self._erase(lead)
        self.assertFalse(lead.function)

    def test_archived_mapping_row_on_legacy_lead(self):
        lead = self._mapped_lead('LGER_AR', 18)
        self._make_legacy(lead)
        self.row.active = False
        self._erase(lead)
        self.assertFalse(lead.function)

    def test_erase_legacy_lead_without_inventory(self):
        lead = self._mapped_lead('LGER_LG', 19)
        lead.sudo().write({'x_meta_budget_text': '$5k',
                           'x_meta_typed_amount': 12.5})
        self._make_legacy(lead)
        self.row.unlink()
        self._erase(lead)
        # Nothing says Meta filled it any more: the documented limit.
        self.assertEqual(lead.function, 'Buyer')
        self.assertFalse(lead.x_meta_budget_text)
        self.assertFalse(lead.x_meta_typed_amount)
        self.assertFalse(lead.email_from)
        note = self._notes(lead)
        self.assertIn(self._note_text(), note.body)
        self.assertIn(_LEGACY, note.body)

    def test_renamed_lead_keeps_its_name(self):
        lead = self._ingest('LGER_RN', self._contact(20))
        lead.name = 'Called about the roof'
        self._erase(lead)
        self.assertEqual(lead.name, 'Called about the roof')
        self.assertFalse(lead.email_from)

    # -- who may erase, and how much ------------------------------------- #
    def test_salesperson_without_meta_admin_refused(self):
        lead = self._ingest('LGER_SP', self._contact(21))
        with self.assertRaises(AccessError):
            self._erase(lead, self.sales_a)
        self.assertTrue(lead.email_from)
        self.assertFalse(lead.meta_personal_data_erased_at)

    def test_other_company_meta_admin_refused(self):
        self._give_sales(self.admin_b)
        lead = self._ingest('LGER_CB', self._contact(22))
        self.assertEqual(lead.company_id, self.co_a)
        with self.assertRaises(AccessError):
            self._erase(lead, self.admin_b)
        self.assertTrue(lead.email_from)

    def test_erased_marker_refused_over_rpc(self):
        lead = self._ingest('LGER_MK', self._contact(23))
        with self.assertRaises(AccessError):
            lead.with_user(self.admin_a).write(
                {'meta_personal_data_erased_at': fields.Datetime.now()})
        with self.assertRaises(AccessError):
            self.Lead.with_user(self.admin_a).create({
                'name': 'Planted',
                'meta_personal_data_erased_at': fields.Datetime.now()})

    def _push_erase_date_back(self, lead):
        self.env.flush_all()
        self.env.cr.execute(
            "UPDATE crm_lead SET meta_personal_data_erased_at = %s "
            "WHERE id = %s", ['2026-01-01 08:00:00', lead.id])
        self.env.invalidate_all()

    def test_erase_again_on_an_untouched_lead_only_adds_a_note(self):
        erased = self._ingest('LGER_AG1', self._contact(24))
        self._queue_row('LGER_AG1')
        self._erase(erased)
        self._assert_copies_cleared('LGER_AG1')
        fields_after = {name: erased[name] for name in (
            'name', 'email_from', 'phone', 'contact_name', 'description',
            'stage_id', 'user_id')}
        self._push_erase_date_back(erased)
        logs, events = self._copies('LGER_AG1')
        copies = {('meta.sync.log', i) for i in logs.ids}
        copies |= {('meta.webhook.event', i) for i in events.ids}
        written = []

        def spy(model):
            original = type(self.env[model]).write

            def write(records, vals):
                written.extend((records._name, i) for i in records.ids)
                return original(records, vals)
            return patch.object(type(self.env[model]), 'write', write)

        fresh = self._ingest('LGER_AG2', self._contact(25))
        with spy('meta.sync.log'), spy('meta.webhook.event'):
            self._erase(erased | fresh)
        # Rows already cleared aren't written again: their write date keeps
        # counting towards the queue's clean-up.
        self.assertFalse(copies & set(written))
        self.assertFalse(fresh.email_from)
        for name, value in fields_after.items():
            self.assertEqual(erased[name], value, name)
        self._assert_copies_cleared('LGER_AG1')
        self.assertFalse(self.env['meta.leadgen.tombstone'].sudo().search(
            [('leadgen_id', '=', 'LGER_AG1')]))
        # The date is the latest erase; each erase leaves its own note.
        self.assertGreater(erased.meta_personal_data_erased_at,
                           fields.Datetime.to_datetime('2026-01-01 08:00:00'))
        self.assertEqual(len(self._notes(erased)), 2)
        self.assertEqual(len(self._notes(fresh)), 1)

    def test_erase_again_clears_data_typed_after_the_first_erase(self):
        lead = self._mapped_lead('LGER_TY', 26)
        self._erase(lead)
        self._flush_tracking()
        typed = {'email_from': 'typed26@example.com',
                 'phone': '+1 555 010 7726', 'contact_name': 'Typed Person',
                 'function': 'Typed Buyer', 'city': 'Typed Town'}
        lead.with_user(self.admin_a).write(typed)
        self._flush_tracking()
        Tracking = self.env['mail.tracking.value'].sudo()
        on_lead = [('mail_message_id.model', '=', 'crm.lead'),
                   ('mail_message_id.res_id', '=', lead.id),
                   ('new_value_char', '=', 'typed26@example.com')]
        self.assertTrue(Tracking.search(on_lead))

        self._erase(lead)
        self._flush_tracking()

        for name in typed:
            self.assertFalse(lead[name], name)
        self.assertFalse(lead.email_normalized)
        self.assertFalse(Tracking.search(on_lead))
        self.assertEqual(len(self._notes(lead)), 2)

    def test_erase_again_keeps_the_cap(self):
        leads = self.Lead
        for n in range(3):
            leads |= self._ingest('LGER_AC%d' % n, self._contact(35 + n))
        self._erase(leads[:2])
        with patch.object(crm_lead_module, '_META_ERASE_LIMIT', 2):
            # Erased leads count towards the cap like the others.
            with self.assertRaisesRegex(UserError, 'at most 2'):
                self._erase(leads)
        self.assertTrue(leads[2].email_from)
        self.assertEqual(len(self._notes(leads[0])), 1)

    def test_plain_lead_refused(self):
        plain = self.Lead.create({'name': 'Plain', 'email_from': 'p@x.com'})
        with self.assertRaisesRegex(UserError, 'Select at least one Meta lead'):
            self._erase(plain)
        self.assertEqual(plain.email_from, 'p@x.com')

    def test_cap_refused_before_any_change(self):
        leads = self.Lead
        for n in range(3):
            leads |= self._ingest('LGER_CAP%d' % n, self._contact(30 + n))
        with patch.object(crm_lead_module, '_META_ERASE_LIMIT', 2):
            with self.assertRaisesRegex(UserError, 'at most 2'):
                self._erase(leads)
            self._erase(leads[:2])
        self.assertFalse(leads[2].meta_personal_data_erased_at)
        self.assertTrue(leads[2].email_from)
        self.assertTrue(all(leads[:2].mapped('meta_personal_data_erased_at')))


@tagged('post_install', '-at_install')
class TestUnlinkScrub(EraseFixtureMixin, MultiCompanyFixtureMixin,
                      IngestFixtureMixin, TransactionCase):

    # A long text and a choice that an admin chose to track.
    _TYPED_FIELDS = dict(
        EraseFixtureMixin._TYPED_FIELDS,
        x_meta_visit_note=('text', 'Visit Note', {'tracking': 1}),
        x_meta_visit_stage=('selection', 'Visit Stage', {
            'tracking': 1, 'selection_ids': [
                Command.create({'value': 'hot', 'name': 'Ready to buy',
                                'sequence': 1}),
                Command.create({'value': 'cold', 'name': 'Not now',
                                'sequence': 2}),
            ]}))

    def _meta_lead(self, leadgen_id, n):
        lead = self._ingest(leadgen_id, self._contact(n))
        self._queue_row(leadgen_id)
        self._assert_copies_kept(leadgen_id)
        return lead

    def _merge(self, leads):
        head = leads.with_user(self.sales_a)._merge_opportunity(
            auto_unlink=True)
        self.assertEqual(len(leads.exists()), 1)
        return head.with_user(self.env.user)

    def _delete(self, lead):
        # Only Sales Administrators may delete leads in crm.
        lead.with_user(self.sales_mgr_a).unlink()
        self.assertFalse(lead.exists())

    def _reason(self, leadgen_id):
        return self.env['meta.leadgen.tombstone'].sudo().search(
            [('leadgen_id', '=', leadgen_id)]).removal_reason

    def test_delete_clears_own_copies(self):
        lead = self._meta_lead('LGUS_DEL', 41)
        self._delete(lead)
        self._assert_copies_cleared('LGUS_DEL')
        self.assertEqual(self._reason('LGUS_DEL'), 'deleted')

    def test_merge_keeps_copies(self):
        first = self._meta_lead('LGUS_MA', 42)
        second = self._meta_lead('LGUS_MB', 43)
        head = self._merge(first | second)
        tail_id = 'LGUS_MB' if head == first else 'LGUS_MA'
        self._assert_copies_kept('LGUS_MA')
        self._assert_copies_kept('LGUS_MB')
        self.assertEqual(self._reason(tail_id), 'merged')

    def test_delete_survivor_clears_merged_copies(self):
        first = self._meta_lead('LGUS_SA', 44)
        second = self._meta_lead('LGUS_SB', 45)
        head = self._merge(first | second)
        self._delete(head)
        self._assert_copies_cleared('LGUS_SA')
        self._assert_copies_cleared('LGUS_SB')

    def test_delete_end_of_merge_chain_clears_all(self):
        first = self._meta_lead('LGUS_CA', 46)
        second = self._meta_lead('LGUS_CB', 47)
        third = self._meta_lead('LGUS_CC', 48)
        head = self._merge(first | second)
        head = self._merge(head | third)
        self._delete(head)
        for leadgen_id in ('LGUS_CA', 'LGUS_CB', 'LGUS_CC'):
            self._assert_copies_cleared(leadgen_id)

    def test_delete_plain_lead_holding_a_merged_meta_lead(self):
        meta = self._meta_lead('LGUS_PL', 49)
        # crm keeps an opportunity over a lead as the merge head.
        plain = self.Lead.create({
            'name': 'Plain', 'type': 'opportunity',
            'company_id': self.co_a.id})
        head = self._merge(meta | plain)
        self.assertEqual(head, plain)
        self._delete(head)
        self._assert_copies_cleared('LGUS_PL')

    def test_delete_erased_lead_records_erased(self):
        self._give_sales(self.admin_a)
        lead = self._meta_lead('LGUS_ER', 50)
        self._erase(lead)
        self._delete(lead)
        self.assertEqual(self._reason('LGUS_ER'), 'erased')
        self._assert_copies_cleared('LGUS_ER')

    def _tomb(self, leadgen_id):
        return self.env['meta.leadgen.tombstone'].sudo().search(
            [('leadgen_id', '=', leadgen_id)])

    def _assert_never_fetched_again(self, leadgen_id):
        tomb = self._tomb(leadgen_id)
        with self._patch_graph():
            with self.assertRaisesRegex(UserError, "can't be fetched from "
                                                   "Meta again"):
                tomb.with_user(self.admin_a).action_restore()
            with self.assertRaises(MetaLeadRemovedError):
                self.Ingest.ingest_leadgen(self.page, leadgen_id, 'manual')
            self._fetch_lead.assert_not_called()
        self.assertTrue(tomb.exists())

    def test_erase_survivor_marks_merged_leads_erased(self):
        self._give_sales(self.admin_a)
        first = self._meta_lead('LGUS_EA', 51)
        second = self._meta_lead('LGUS_EB', 52)
        head = self._merge(first | second)
        head_id = head.meta_leadgen_id
        tail_id = 'LGUS_EB' if head_id == 'LGUS_EA' else 'LGUS_EA'
        self.assertEqual(self._reason(tail_id), 'merged')
        self._erase(head)
        self.assertEqual(self._reason(tail_id), 'erased')
        # The merge link stays, so a later merge or delete still finds it.
        self.assertEqual(self._tomb(tail_id).merged_into_id, head)
        self._assert_copies_cleared(tail_id)
        # The delete empties the merge link; the record must still hold.
        self._delete(head)
        self.assertFalse(self._tomb(tail_id).merged_into_id)
        for leadgen_id in (head_id, tail_id):
            self.assertEqual(self._reason(leadgen_id), 'erased')
            self._assert_never_fetched_again(leadgen_id)

    def test_erased_lead_merged_away_stays_erased(self):
        self._give_sales(self.admin_a)
        erased = self._meta_lead('LGUS_XA', 53)
        self._erase(erased)
        other = self._meta_lead('LGUS_XB', 54)
        # crm keeps an opportunity over a lead as the merge head.
        other.sudo().write({'type': 'opportunity'})
        head = self._merge(erased | other)
        self.assertEqual(head, other)
        self.assertEqual(self._reason('LGUS_XA'), 'erased')
        self.assertEqual(self._tomb('LGUS_XA').merged_into_id, other)
        self.assertIn('LGUS_XA', other._meta_merged_in_leadgen_ids())
        self._delete(other)
        self.assertEqual(self._reason('LGUS_XB'), 'deleted')
        self._assert_never_fetched_again('LGUS_XA')

    def _messages_of(self, lead):
        return self.env['mail.message'].sudo().search(
            [('model', '=', 'crm.lead'), ('res_id', '=', lead.id)])

    def _titled(self, lead, title):
        # A title template holding the contact's name, and a note typed on
        # the lead that a merge moves along.
        lead.sudo().write({'name': title, 'meta_name_is_generated': True})
        lead.message_post(body='Spoke to them, call again Monday',
                          message_type='comment',
                          subtype_xmlid='mail.mt_note')

    def _quoting(self, lead, needles):
        return self._messages_of(lead).filtered(lambda m: any(
            needle in (m.body or '') or needle in (m.subject or '')
            for needle in needles))

    def test_erase_again_clears_a_lead_merged_in_after_the_erase(self):
        self._give_sales(self.admin_a)
        head = self._meta_lead('LGUS_RA', 55)
        self._erase(head)
        # crm keeps an opportunity over a lead as the merge head.
        head.sudo().write({'type': 'opportunity'})
        tail = self._ingest('LGUS_RB', self._contact(56, [
            {'name': 'what_is_your_budget', 'values': ['$7k']}]))
        self._queue_row('LGUS_RB')
        self._assert_copies_kept('LGUS_RB')
        self._titled(tail, 'Person 56 roof job')
        tail_data = [tail.email_from, tail.phone, tail.name, '$7k']
        self.assertEqual(self._merge(head | tail), head)
        # crm brought the tail's contact data, answers and answer block
        # over, quoted them in its merge note and put the tail's title
        # into the subject of the messages it moved.
        self.assertEqual(head.email_from, tail_data[0])
        self.assertTrue(head.sudo().answer_ids)
        self.assertIn('$7k', head.description)
        self.assertTrue(self._quoting(head, tail_data))
        self.assertEqual(self._reason('LGUS_RB'), 'merged')
        # The id's own rows send the admin to the lead.
        with self.assertRaisesRegex(
                UserError, 'Lead #%s holds this Meta lead' % head.id):
            self.env['meta.sync.log']._erase_leadgen_copies(
                ['LGUS_RB'], 'manual')

        self._erase(head)

        for name in ('email_from', 'phone', 'contact_name',
                     'email_normalized'):
            self.assertFalse(head[name], name)
        self.assertFalse(head.sudo().answer_ids)
        self.assertNotIn('$7k', head.description or '')
        self.assertFalse(self._quoting(head, tail_data))
        # The hand-written note stays.
        self.assertTrue(self._messages_of(head).filtered(
            lambda m: 'call again Monday' in (m.body or '')))
        self.assertEqual(len(self._notes(head)), 2)
        self._assert_copies_cleared('LGUS_RA')
        self._assert_copies_cleared('LGUS_RB')
        tomb = self._tomb('LGUS_RB')
        self.assertEqual(tomb.removal_reason, 'erased')
        self.assertEqual(tomb.merged_into_id, head)
        self._assert_never_fetched_again('LGUS_RB')
        self._delete(head)
        self.assertEqual(self._reason('LGUS_RB'), 'erased')
        self._assert_never_fetched_again('LGUS_RB')

    def test_merge_note_removed_on_erase(self):
        # The same holds for a merge before the first erase.
        self._give_sales(self.admin_a)
        first = self._meta_lead('LGUS_NA', 57)
        second = self._meta_lead('LGUS_NB', 58)
        self._titled(first, 'Person 57 kitchen')
        self._titled(second, 'Person 58 garden')
        data = [first.email_from, second.email_from, first.phone,
                second.phone, first.name, second.name]
        head = self._merge(first | second)
        self.assertTrue(self._quoting(head, data))
        self.assertTrue(self._messages_of(head).filtered(
            lambda m: 'crm_lead_merge_summary' in (m.body or '')))
        self._erase(head)
        self.assertFalse(self._quoting(head, data))
        self.assertEqual(len(self._messages_of(head).filtered(
            lambda m: 'call again Monday' in (m.body or ''))), 2)
        self.assertFalse(self._messages_of(head).filtered(
            lambda m: 'crm_lead_merge_summary' in (m.body or '')))

    def _erase_button_hidden(self, user, record):
        view_id = self.env.ref('crm.crm_lead_view_form').id
        xpath = "//header/button[@name='action_meta_erase_personal_data']"
        arch = etree.fromstring(self.env['crm.lead'].with_user(
            user).get_view(view_id, 'form')['arch'])
        buttons = arch.xpath(xpath)
        if not buttons:
            return True
        record = record.with_user(user)
        return bool(safe_eval(buttons[0].get('invisible') or 'False', {
            'meta_leadgen_id': record.meta_leadgen_id,
            'meta_personal_data_erased_at': str(
                record.meta_personal_data_erased_at or '') or False,
            'meta_holds_merged_leads': record.meta_holds_merged_leads,
        }))

    def test_erase_button_shown_on_erased_leads_for_meta_admins(self):
        self._give_sales(self.admin_a)
        lead = self._meta_lead('LGUS_BT', 59)
        self._erase(lead)
        plain = self.Lead.create({'name': 'Plain', 'company_id': self.co_a.id})
        hidden = self._erase_button_hidden
        self.assertTrue(lead.meta_personal_data_erased_at)
        self.assertFalse(hidden(self.admin_a, lead))
        self.assertTrue(hidden(self.admin_a, plain))
        self.assertTrue(hidden(self.sales_a, lead))
        with self.assertRaises(AccessError):
            self._erase(lead, self.sales_a)

    def _own_deal(self, n):
        # A hand-made opportunity: crm keeps it over a lead as the merge
        # head, and it never gets a Meta lead id.
        return self.Lead.create({
            'name': 'ACME deck %d' % n, 'type': 'opportunity',
            'partner_name': 'ACME', 'function': 'Owner',
            'phone': '+1 555 999 %04d' % n, 'city': 'Springfield',
            'description': '<p>Own notes about ACME</p>',
            'company_id': self.co_a.id})

    def _merge_into_own_deal(self, leadgen_id, n):
        tail = self._ingest(leadgen_id, self._contact(n, [
            {'name': 'what_is_your_budget', 'values': ['$7k']}]))
        self._queue_row(leadgen_id)
        self._titled(tail, 'Person %d deck' % n)
        tail_data = [tail.email_from, tail.phone, tail.name, '$7k']
        head = self._own_deal(n)
        self.assertEqual(self._merge(head | tail), head)
        self.assertFalse(head.meta_leadgen_id)
        # crm filled the empty fields from the Meta lead, moved its answers
        # and answer block over and quoted it all in its merge note.
        self.assertEqual(head.email_from, tail_data[0])
        self.assertEqual(head.contact_name, 'Person %d' % n)
        self.assertTrue(head.sudo().answer_ids)
        self.assertIn('$7k', head.description)
        self.assertTrue(self._quoting(head, tail_data))
        self.assertEqual(self._reason(leadgen_id), 'merged')
        return head, tail_data

    def test_erase_meta_lead_merged_into_a_hand_made_opportunity(self):
        self._give_sales(self.admin_a)
        head, tail_data = self._merge_into_own_deal('LGUS_HM', 61)
        other = self._own_deal(62)
        self.assertTrue(head.meta_holds_merged_leads)
        self.assertFalse(other.meta_holds_merged_leads)
        self.assertFalse(self._erase_button_hidden(self.admin_a, head))
        self.assertTrue(self._erase_button_hidden(self.admin_a, other))
        self.assertTrue(self._erase_button_hidden(self.sales_a, head))
        # The id's own rows send the admin to the lead, which can now act.
        with self.assertRaisesRegex(
                UserError, 'Lead #%s holds this Meta lead' % head.id):
            self.env['meta.sync.log']._erase_leadgen_copies(
                ['LGUS_HM'], 'manual')
        with self.assertRaises(AccessError):
            self._erase(head, self.sales_a)

        self._erase(head)

        # What the merge brought over from the Meta lead goes.
        for name in ('email_from', 'contact_name', 'email_normalized'):
            self.assertFalse(head[name], name)
        self.assertFalse(head.sudo().answer_ids)
        self.assertNotIn('$7k', head.description or '')
        self.assertFalse(self._quoting(head, tail_data))
        self.assertFalse(self._messages_of(head).filtered(
            lambda m: 'crm_lead_merge_summary' in (m.body or '')))
        self._flush_tracking()
        values = self.env['mail.tracking.value'].sudo().search([
            ('mail_message_id.model', '=', 'crm.lead'),
            ('mail_message_id.res_id', '=', head.id)])
        for value in values:
            for old_new in (value.old_value_char, value.new_value_char):
                self.assertNotIn(tail_data[0], old_new or '')
        # The opportunity's own data stays.
        self.assertEqual(head.name, 'ACME deck 61')
        self.assertEqual(head.phone, '+1 555 999 0061')
        self.assertEqual(head.partner_name, 'ACME')
        self.assertEqual(head.function, 'Owner')
        self.assertEqual(head.city, 'Springfield')
        self.assertIn('Own notes about ACME', head.description)
        self.assertTrue(self._messages_of(head).filtered(
            lambda m: 'call again Monday' in (m.body or '')))
        note = self._notes(head)
        self.assertEqual(len(note), 1)
        self.assertTrue(head.meta_personal_data_erased_at)
        # The Meta lead's copies and record are erased, the link kept.
        self._assert_copies_cleared('LGUS_HM')
        tomb = self._tomb('LGUS_HM')
        self.assertEqual(tomb.removal_reason, 'erased')
        self.assertEqual(tomb.merged_into_id, head)
        self._assert_never_fetched_again('LGUS_HM')
        # Still offered, as for an erased Meta lead.
        self.assertFalse(self._erase_button_hidden(self.admin_a, head))
        self._delete(head)
        self.assertEqual(self._reason('LGUS_HM'), 'erased')
        self._assert_never_fetched_again('LGUS_HM')

    def _answers_tab_hidden(self, user, record):
        view_id = self.env.ref('crm.crm_lead_view_form').id
        arch = etree.fromstring(self.env['crm.lead'].with_user(
            user).get_view(view_id, 'form')['arch'])
        pages = arch.xpath("//page[@name='meta_answers']")
        if not pages:
            return True
        record = record.with_user(user)
        # The form gives a list field's ids to the expression.
        return bool(safe_eval(pages[0].get('invisible') or 'False', {
            'meta_leadgen_id': record.meta_leadgen_id,
            'answer_ids': record.answer_ids.ids,
        }))

    def test_answers_tab_shown_on_a_lead_holding_merged_answers(self):
        self._give_sales(self.admin_a)
        head = self._merge_into_own_deal('LGUS_TAB', 95)[0]
        head.sudo().user_id = self.sales_a
        plain = self._own_deal(96)
        meta = self._meta_lead('LGUS_TAB2', 94)
        own_only = new_test_user(
            self.env, 'tab_own_sales',
            groups='base.group_user,sales_team.group_sale_salesman',
            company_id=self.co_a.id,
            company_ids=[Command.set([self.co_a.id])])
        hidden = self._answers_tab_hidden
        for user in (self.admin_a, self.sales_a):
            self.assertFalse(hidden(user, head), user.login)
            self.assertFalse(hidden(user, meta), user.login)
            self.assertTrue(hidden(user, plain), user.login)
            self.assertIn('$7k', head.with_user(user).answer_ids.mapped(
                'value'))
        # A salesperson who can't open the lead doesn't get its answers.
        self.assertFalse(self.Lead.with_user(own_only).search(
            [('id', '=', head.id)]))
        self.assertFalse(self.env['meta.lead.answer'].with_user(
            own_only).search([('lead_id', '=', head.id)]))

        self._erase(head)

        self.assertFalse(head.sudo().answer_ids)
        self.assertTrue(hidden(self.admin_a, head))
        self.assertTrue(hidden(self.sales_a, head))

    def _tracked_fields(self, lead):
        return set(self.env['mail.tracking.value'].sudo().search([
            ('mail_message_id.model', '=', 'crm.lead'),
            ('mail_message_id.res_id', '=', lead.id),
        ]).mapped('field_id.name'))

    def test_erase_head_without_a_record_of_merged_fields(self):
        # A merge made before Odoo recorded what it brought over. A field
        # goes only when the Meta lead's stored data holds its value; the
        # opportunity's own phone, company and city stay.
        self._give_sales(self.admin_a)
        head, tail_data = self._merge_into_own_deal('LGUS_HU', 63)
        self._flush_tracking()
        head.write({'phone': '+1 555 999 1063'})
        self._flush_tracking()
        self.assertIn('phone', self._tracked_fields(head))
        self._make_legacy(head)
        self._erase(head)
        for name in ('email_from', 'contact_name', 'email_normalized'):
            self.assertFalse(head[name], name)
        self.assertEqual(head.phone, '+1 555 999 1063')
        self.assertEqual(head.partner_name, 'ACME')
        self.assertEqual(head.city, 'Springfield')
        self.assertEqual(head.function, 'Owner')
        self.assertEqual(head.name, 'ACME deck 63')
        self.assertFalse(self._quoting(head, tail_data))
        # Only the history of the cleared fields goes.
        self._flush_tracking()
        tracked = self._tracked_fields(head)
        self.assertIn('phone', tracked)
        self.assertFalse(tracked & {'email_from', 'contact_name'}, tracked)
        note = self._notes(head)
        self.assertIn('only when its value was found in the stored Meta '
                      'data', note.body)
        self.assertIn('values not found in that data and were kept: City, '
                      'Company Name, Phone.', note.body)
        self.assertNotIn('be checked against', note.body)
        self.assertEqual(self._reason('LGUS_HU'), 'erased')
        self._assert_copies_cleared('LGUS_HU')

    def test_unrecorded_merge_matches_retyped_values(self):
        # Case, spacing and phone formatting don't hide a Meta value.
        self._give_sales(self.admin_a)
        head, _tail_data = self._merge_into_own_deal('LGUS_HN', 72)
        self._make_legacy(head)
        head.write({'email_from': ' PERSON72@Example.COM ',
                    'contact_name': '  person   72 ',
                    'phone': '(1) 555-010-0072'})
        self._erase(head)
        for name in ('email_from', 'contact_name', 'phone'):
            self.assertFalse(head[name], name)
        self.assertEqual(head.partner_name, 'ACME')
        self.assertEqual(head.city, 'Springfield')

    def test_unrecorded_merge_without_meta_data_left(self):
        # The stored copy of the Meta lead has expired, so nothing can be
        # checked: the fields stay and the note names them.
        self._give_sales(self.admin_a)
        head, tail_data = self._merge_into_own_deal('LGUS_HX', 73)
        self._make_legacy(head)
        logs, _events = self._copies('LGUS_HX')
        logs.write({'raw_payload': False, 'payload_cleared': 'expired'})
        self._erase(head)
        self.assertEqual(head.email_from, tail_data[0])
        self.assertEqual(head.contact_name, 'Person 73')
        self.assertEqual(head.phone, '+1 555 999 0073')
        self.assertEqual(head.city, 'Springfield')
        note = self._notes(head)
        self.assertIn('be checked against the Meta data', note.body)
        self.assertIn('City, Company Name, Contact Name, Email, Phone',
                      note.body)
        # The rest of the erase still runs.
        self.assertFalse(head.sudo().answer_ids)
        self.assertNotIn('$7k', head.description or '')
        self.assertFalse(self._messages_of(head).filtered(
            lambda m: 'crm_lead_merge_summary' in (m.body or '')))
        self.assertEqual(self._reason('LGUS_HX'), 'erased')

    def _deal_with_two_meta_leads(self, n):
        # One Meta lead brings the name and phone, the other the email.
        first = self._ingest('LGUS_TT%dA' % n, [
            {'name': 'full_name', 'values': ['Person %d' % n]},
            {'name': 'phone_number', 'values': ['+1 555 010 %04d' % n]}])
        second = self._ingest('LGUS_TT%dB' % n, [
            {'name': 'email', 'values': ['person%d@example.com' % n]}])
        head = self._own_deal(n)
        head.write({'phone': False})
        self.assertEqual(self._merge(head | first | second), head)
        self.assertEqual(head.contact_name, 'Person %d' % n)
        self.assertEqual(head.phone, '+1 555 010 %04d' % n)
        self.assertEqual(head.email_from, 'person%d@example.com' % n)
        return head

    def _assert_two_meta_leads_erased(self, head, n):
        for name in ('email_from', 'contact_name', 'phone'):
            self.assertFalse(head[name], name)
        self.assertEqual(head.partner_name, 'ACME')
        self.assertEqual(head.function, 'Owner')
        self.assertEqual(head.city, 'Springfield')
        for suffix in 'AB':
            self.assertEqual(self._reason('LGUS_TT%d%s' % (n, suffix)),
                             'erased')

    def test_erase_head_holding_two_meta_leads(self):
        self._give_sales(self.admin_a)
        head = self._deal_with_two_meta_leads(76)
        self._erase(head)
        self._assert_two_meta_leads_erased(head, 76)

    def test_erase_unrecorded_head_holding_two_meta_leads(self):
        self._give_sales(self.admin_a)
        head = self._deal_with_two_meta_leads(77)
        self._make_legacy(head)
        self._erase(head)
        self._assert_two_meta_leads_erased(head, 77)
        self.assertNotIn('be checked against', self._notes(head).body)

    def test_erase_head_after_two_merges(self):
        # The second merge adds to what the first one recorded.
        self._give_sales(self.admin_a)
        head, _tail_data = self._merge_into_own_deal('LGUS_TS1', 78)
        head.write({'phone': False})
        tail = self._ingest('LGUS_TS2', self._contact(79))
        self.assertEqual(self._merge(head | tail), head)
        self.assertEqual(head.phone, '+1 555 010 0079')
        self.assertEqual(head.email_from, 'person78@example.com')
        self._erase(head)
        for name in ('email_from', 'contact_name', 'phone'):
            self.assertFalse(head[name], name)
        self.assertEqual(head.partner_name, 'ACME')
        self.assertEqual(head.city, 'Springfield')
        self.assertEqual(self._reason('LGUS_TS1'), 'erased')
        self.assertEqual(self._reason('LGUS_TS2'), 'erased')

    def test_recorded_field_changed_since_the_merge_kept(self):
        # Typed over after the merge: no longer the Meta lead's value.
        self._give_sales(self.admin_a)
        head, tail_data = self._merge_into_own_deal('LGUS_HC', 80)
        self.assertIn(',email_from,', self._filled(head))
        head.write({'email_from': 'buyer@acme.example'})
        self._erase(head)
        self.assertEqual(head.email_from, 'buyer@acme.example')
        self.assertFalse(head.contact_name)
        note = self._notes(head)
        self.assertIn('hold the Meta value now', note.body)
        self.assertIn('Email', note.body)

    def _tracked_texts(self, lead, name):
        values = self.env['mail.tracking.value'].sudo().search([
            ('mail_message_id.model', '=', 'crm.lead'),
            ('mail_message_id.res_id', '=', lead.id),
            ('field_id.name', '=', name)])
        return [(v.old_value_char or v.old_value_text or '',
                 v.new_value_char or v.new_value_text or '') for v in values]

    def _retype_meta_email(self, leadgen_id, n):
        # The merge puts the Meta email on the deal, then a salesperson
        # types over it twice.
        head, tail_data = self._merge_into_own_deal(leadgen_id, n)
        self._flush_tracking()
        head.write({'email_from': 'buyer@acme.example'})
        self._flush_tracking()
        head.write({'email_from': 'sales@acme.example'})
        self._flush_tracking()
        before = self._tracked_texts(head, 'email_from')
        self.assertTrue(any(tail_data[0] in pair for pair in before), before)
        return head, tail_data

    def test_kept_field_history_quoting_meta_removed(self):
        # The field is kept, but its history must not show the Meta
        # address any more. The salesperson's own change stays.
        self._give_sales(self.admin_a)
        head, _tail_data = self._retype_meta_email('LGUS_HH', 82)
        self._erase(head)
        self._flush_tracking()
        self.assertEqual(head.email_from, 'sales@acme.example')
        self.assertFalse(head.contact_name)
        self.assertEqual(self._tracked_texts(head, 'email_from'),
                         [('buyer@acme.example', 'sales@acme.example')])
        note = self._notes(head)
        self.assertIn('hold the Meta value now', note.body)
        self.assertIn(_HISTORY_REMOVED, note.body)

    def test_unrecorded_kept_field_history_quoting_meta_removed(self):
        # Without a record of the merge the typed email is the lead's own
        # value, so it stays, but not the history quoting the Meta one.
        # The own phone's history quotes no Meta value and stays whole.
        self._give_sales(self.admin_a)
        head, _tail_data = self._retype_meta_email('LGUS_HL', 83)
        head.write({'phone': '+1 555 999 1083'})
        self._flush_tracking()
        self._make_legacy(head)
        self._erase(head)
        self._flush_tracking()
        self.assertEqual(head.email_from, 'sales@acme.example')
        self.assertEqual(head.phone, '+1 555 999 1083')
        self.assertFalse(head.contact_name)
        self.assertEqual(self._tracked_texts(head, 'email_from'),
                         [('buyer@acme.example', 'sales@acme.example')])
        self.assertEqual(self._tracked_texts(head, 'phone'),
                         [('+1 555 999 0083', '+1 555 999 1083')])
        note = self._notes(head)
        self.assertIn('values not found in that data and were kept: City, '
                      'Company Name, Email, Phone.', note.body)
        self.assertIn(_HISTORY_REMOVED, note.body)
        self.assertNotIn('be checked against', note.body)

    def test_note_names_an_expired_copy(self):
        # The daily cleanup removed the Meta lead's copy with the old sync
        # rows. Nothing can be proven, so the history stays too, and the
        # note says why.
        self._give_sales(self.admin_a)
        head, tail_data = self._retype_meta_email('LGUS_HE', 84)
        self._make_legacy(head)
        self.env.cr.execute(
            "UPDATE meta_sync_log SET create_date = create_date - "
            "interval '15 days' WHERE meta_leadgen_id = %s", ['LGUS_HE'])
        self.env.invalidate_all()
        self.env['meta.sync.log'].sudo()._cron_vacuum_logs()
        self.assertFalse(self._copies('LGUS_HE')[0])
        self._erase(head)
        self._flush_tracking()
        self.assertEqual(head.contact_name, 'Person 84')
        self.assertTrue(any(tail_data[0] in pair for pair in
                            self._tracked_texts(head, 'email_from')))
        note = self._notes(head)
        self.assertIn(_EXPIRED, note.body)
        self.assertIn(_HISTORY_UNCHECKED, note.body)
        self.assertIn('Contact Name, Email, Phone', note.body)
        self.assertNotIn(_HISTORY_REMOVED, note.body)

    def test_unchecked_link_field_history_named_unchecked(self):
        # Every copy is there, but a country can't be told apart, so it
        # stays; the copy didn't expire and the note doesn't say so.
        self._give_sales(self.admin_a)
        head, _tail_data = self._merge_into_own_deal('LGUS_HK', 85)
        head.write({'country_id': self.env.ref('base.be').id})
        self._make_legacy(head)
        self._erase(head)
        self.assertEqual(head.country_id, self.env.ref('base.be'))
        note = self._notes(head)
        self.assertIn(_HISTORY_UNCHECKED, note.body)
        self.assertIn('if needed: Country.', note.body)
        self.assertNotIn(_EXPIRED, note.body)

    def _deal_with_corrected_meta_phone(self, leadgen_id, n):
        # A salesperson rewrites the Meta lead's phone, then corrects the
        # number, and merges the lead into a deal that has its own phone,
        # changed once by hand. The deal keeps its phone, so the merge
        # records no copy of it, and crm moves the Meta lead's messages to
        # the deal with their tracked changes.
        tail = self.Lead.browse(self._ingest(leadgen_id, self._contact(n)).id)
        meta_phone = tail.phone
        rewritten = '(1) 555-010-%04d' % n
        self._flush_tracking()
        tail.write({'phone': rewritten})
        self._flush_tracking()
        tail.write({'phone': '+1 555 010 %04d' % (n + 9000)})
        self._flush_tracking()
        self.assertEqual(len(self._tracked_texts(tail, 'phone')), 2)
        head = self._own_deal(n)
        self._flush_tracking()
        own_change = (head.phone, '+1 555 999 %04d' % (n + 1000))
        head.write({'phone': own_change[1]})
        self._flush_tracking()
        self.assertEqual(self._merge(head | tail), head)
        self._flush_tracking()
        self.assertNotIn(',phone,', self._filled(head))
        moved = self._tracked_texts(head, 'phone')
        self.assertIn((meta_phone, rewritten), moved)
        self.assertIn((rewritten, '+1 555 010 %04d' % (n + 9000)), moved)
        return head, meta_phone, own_change

    def test_moved_history_of_a_field_the_merge_left_removed(self):
        # The moved changes show the Meta number, the second one only by
        # its digits. They go; the deal's phone and own change stay.
        self._give_sales(self.admin_a)
        head, _meta_phone, own_change = self._deal_with_corrected_meta_phone(
            'LGUS_HP', 86)
        self._erase(head)
        self._flush_tracking()
        self.assertEqual(head.phone, own_change[1])
        self.assertFalse(head.email_from)
        self.assertEqual(self._tracked_texts(head, 'phone'), [own_change])
        self.assertIn(_HISTORY_REMOVED, self._notes(head).body)

    def test_unrecorded_emptied_field_history_removed(self):
        # Without a record of the merge, an email emptied since has no
        # value to check, but its history still shows the Meta address.
        self._give_sales(self.admin_a)
        head, tail_data = self._merge_into_own_deal('LGUS_HB', 87)
        self._flush_tracking()
        head.write({'email_from': False})
        self._flush_tracking()
        self.assertIn((tail_data[0], ''),
                      self._tracked_texts(head, 'email_from'))
        self._make_legacy(head)
        self._erase(head)
        self._flush_tracking()
        self.assertFalse(head.email_from)
        self.assertEqual(self._tracked_texts(head, 'email_from'), [])
        self.assertEqual(head.phone, '+1 555 999 0087')
        self.assertIn(_HISTORY_REMOVED, self._notes(head).body)

    def test_history_proven_by_the_copy_left_removed(self):
        # Two Meta leads merged into a deal Odoo has no record of, and the
        # copy of one has expired. The phone typed over the merged one
        # can't be checked and stays, but the copy left still proves the
        # merged number, so the entries showing it go.
        self._give_sales(self.admin_a)
        head = self._deal_with_two_meta_leads(88)
        meta_phone = head.phone
        self._flush_tracking()
        head.write({'phone': '+1 555 999 2088'})
        self._flush_tracking()
        self.assertIn((meta_phone, '+1 555 999 2088'),
                      self._tracked_texts(head, 'phone'))
        self._make_legacy(head)
        logs, _events = self._copies('LGUS_TT88B')
        self.assertTrue(logs)
        logs.write({'raw_payload': False, 'payload_cleared': 'expired'})
        self._erase(head)
        self._flush_tracking()
        self.assertEqual(head.phone, '+1 555 999 2088')
        self.assertEqual(head.email_from, 'person88@example.com')
        self.assertFalse(head.contact_name)
        left = self._tracked_texts(head, 'phone')
        self.assertFalse([pair for pair in left if meta_phone in pair], left)
        note = self._notes(head)
        self.assertIn(_EXPIRED, note.body)
        self.assertIn('if needed: City, Company Name, Email, Phone.',
                      note.body)
        self.assertIn(_HISTORY_REMOVED, note.body)

    def test_moved_text_and_choice_history_removed(self):
        # A long text and a choice answer, changed on the Meta lead before
        # the merge into a deal with values of its own. The text is kept
        # in the long text columns of the history, the choice by its label.
        self._give_sales(self.admin_a)
        self._map_row('visit_note', 'x_meta_visit_note')
        self._map_row('visit_stage', 'x_meta_visit_stage')
        tail = self.Lead.browse(self._ingest('LGUS_HQ', self._contact(89, [
            {'name': 'visit_note', 'values': ['Gate B, after 5pm']},
            {'name': 'visit_stage', 'values': ['hot']}])).id)
        self.assertEqual(tail.x_meta_visit_stage, 'hot')
        self._flush_tracking()
        tail.write({'x_meta_visit_note': 'Gate C',
                    'x_meta_visit_stage': 'cold'})
        head = self._own_deal(89)
        self._flush_tracking()
        head.write({'x_meta_visit_note': 'Own plan',
                    'x_meta_visit_stage': 'cold'})
        self._flush_tracking()
        head.write({'x_meta_visit_note': 'Own plan, updated'})
        self._flush_tracking()
        self.assertEqual(self._merge(head | tail), head)
        self._flush_tracking()
        rows = self.env['mail.tracking.value'].sudo().search([
            ('mail_message_id.model', '=', 'crm.lead'),
            ('mail_message_id.res_id', '=', head.id)])
        self.assertIn('Gate B, after 5pm', rows.mapped('old_value_text'))
        self.assertIn('Ready to buy', rows.mapped('old_value_char'))
        self._erase(head)
        self._flush_tracking()
        self.assertEqual(head.x_meta_visit_note, 'Own plan, updated')
        self.assertEqual(head.x_meta_visit_stage, 'cold')
        self.assertEqual(
            sorted(self._tracked_texts(head, 'x_meta_visit_note')),
            [('', 'Own plan'), ('Own plan', 'Own plan, updated')])
        self.assertEqual(self._tracked_texts(head, 'x_meta_visit_stage'),
                         [('', 'Not now')])

    def test_history_of_a_lead_not_erased_untouched(self):
        # The same Meta number typed on another deal, which isn't erased:
        # its history stays.
        self._give_sales(self.admin_a)
        head, meta_phone, own_change = self._deal_with_corrected_meta_phone(
            'LGUS_HO', 90)
        other = self._own_deal(91)
        self._flush_tracking()
        other.write({'phone': meta_phone})
        self._flush_tracking()
        other.write({'phone': '+1 555 999 1091'})
        self._flush_tracking()
        before = self._tracked_texts(other, 'phone')
        self.assertIn((meta_phone, '+1 555 999 1091'), before)
        self._erase(head)
        self._flush_tracking()
        self.assertEqual(self._tracked_texts(head, 'phone'), [own_change])
        self.assertEqual(self._tracked_texts(other, 'phone'), before)

    def test_two_holders_erased_in_one_call(self):
        # Each deal's history is checked against its own Meta leads: the
        # first deal's change to the second one's Meta number stays.
        self._give_sales(self.admin_a)
        first, _meta_phone, own_change = self._deal_with_corrected_meta_phone(
            'LGUS_HW1', 92)
        second, tail_data = self._merge_into_own_deal('LGUS_HW2', 93)
        self._flush_tracking()
        second.write({'email_from': False})
        self._flush_tracking()
        self._make_legacy(second)
        other_number = '+1 555 010 0093'
        first.write({'phone': other_number})
        self._flush_tracking()
        first.write({'phone': own_change[1]})
        self._flush_tracking()
        self._erase(first | second)
        self._flush_tracking()
        self.assertEqual(sorted(self._tracked_texts(first, 'phone')), sorted([
            own_change, (own_change[1], other_number),
            (other_number, own_change[1])]))
        self.assertEqual(self._tracked_texts(second, 'email_from'), [])
        self.assertEqual(second.phone, '+1 555 999 0093')
        self.assertEqual(self._reason('LGUS_HW1'), 'erased')
        self.assertEqual(self._reason('LGUS_HW2'), 'erased')
        for lead in first | second:
            self.assertIn(_HISTORY_REMOVED, self._notes(lead).body)

    def test_confirm_text_names_the_copy_retention(self):
        self._give_sales(self.admin_a)
        Lead = self.env['crm.lead'].with_user(self.admin_a)
        xpath = "//header/button[@name='action_meta_erase_personal_data']"
        for view_type, xmlid in (('form', 'crm.crm_lead_view_form'),
                                 ('list', 'crm.crm_case_tree_view_leads'),
                                 ('list', 'crm.crm_case_tree_view_oppor')):
            arch = etree.fromstring(Lead.get_view(
                self.env.ref(xmlid).id, view_type)['arch'])
            buttons = arch.xpath(xpath)
            self.assertEqual(len(buttons), 1, xmlid)
            self.assertIn(_RETENTION, buttons[0].get('confirm'), xmlid)

    def test_holder_flag_readable_by_meta_admins_only(self):
        self._give_sales(self.admin_a)
        head, _tail_data = self._merge_into_own_deal('LGUS_HR', 81)
        self.assertTrue(
            head.with_user(self.admin_a).meta_holds_merged_leads)
        with self.assertRaises(AccessError):
            head.with_user(self.sales_a).read(['meta_holds_merged_leads'])
        # The salesperson's form still loads.
        view_id = self.env.ref('crm.crm_lead_view_form').id
        views = self.env['crm.lead'].with_user(self.sales_a).get_views(
            [(view_id, 'form')])
        names = list(views['models']['crm.lead']['fields'])
        self.assertNotIn('meta_holds_merged_leads', names)
        self.assertTrue(head.with_user(self.sales_a).read(names))

    def test_lead_holding_no_meta_lead_still_refused(self):
        self._give_sales(self.admin_a)
        first = self._own_deal(64)
        second = self.Lead.create({'name': 'Second', 'type': 'lead',
                                   'company_id': self.co_a.id})
        head = self._merge(first | second)
        self.assertFalse(head.meta_holds_merged_leads)
        with self.assertRaisesRegex(UserError, 'Select at least one Meta lead'):
            self._erase(head)
        self.assertEqual(head.phone, '+1 555 999 0064')

    def _post(self, lead, subject):
        return lead.message_post(body='About %s' % subject, subject=subject,
                                 message_type='comment',
                                 subtype_xmlid='mail.mt_note')

    def test_moved_subjects_only_rewritten_in_crm_form(self):
        self._give_sales(self.admin_a)
        head = self._meta_lead('LGUS_SQ1', 65)
        head.sudo().write({'type': 'opportunity'})
        own = self._post(head, 'Quote for you')
        tail = self._meta_lead('LGUS_SQ2', 66)
        self._titled(tail, 'Quote')
        moved = self._post(tail, 'Hello')
        self.assertEqual(self._merge(head | tail), head)
        self.assertEqual(moved.subject, 'From Quote: Hello')
        later = self._post(head, 'From Quote: later')
        self._erase(head)
        placeholder = _ERASED_NAME
        # crm's own subjects lose the title; others are left alone.
        self.assertEqual(own.subject, 'Quote for you')
        self.assertEqual(later.subject, 'From Quote: later')
        self.assertEqual(moved.subject, 'From %s: Hello' % placeholder)
        subjects = self._messages_of(head).mapped('subject')
        self.assertIn('From %s' % placeholder, subjects)
        self.assertEqual(
            sorted(s for s in subjects if s and 'Quote' in s),
            ['From Quote: later', 'Quote for you'])

    def test_moved_subject_with_spaced_title_rewritten(self):
        self._give_sales(self.admin_a)
        head = self._meta_lead('LGUS_SP1', 67)
        head.sudo().write({'type': 'opportunity'})
        tail = self._meta_lead('LGUS_SP2', 68)
        tail.sudo().write({'name': 'Person 68  roof\tjob',
                           'meta_name_is_generated': True})
        moved = self._post(tail, 'Hello')
        self.assertEqual(self._merge(head | tail), head)
        self.assertEqual(moved.subject, 'From Person 68  roof\tjob: Hello')
        self._erase(head)
        self.assertEqual(moved.subject, 'From %s: Hello' % _ERASED_NAME)
        self.assertFalse(self._quoting(head, ['roof']))

    def test_subjects_of_a_merge_chain_rewritten(self):
        self._give_sales(self.admin_a)
        head = self._meta_lead('LGUS_CH1', 69)
        # Both are opportunities in the second merge; the more likely one
        # is kept.
        head.sudo().write({'type': 'opportunity', 'probability': 99})
        middle = self._meta_lead('LGUS_CH2', 70)
        middle.sudo().write({'type': 'opportunity'})
        self._titled(middle, 'Person 70 shed')
        first = self._meta_lead('LGUS_CH3', 71)
        self._titled(first, 'Person 71 porch')
        moved = self._post(first, 'Hello')
        self.assertEqual(self._merge(middle | first), middle)
        self.assertEqual(self._merge(head | middle), head)
        self.assertEqual(
            moved.subject, 'From Person 70 shed: From Person 71 porch: Hello')
        self._erase(head)
        self.assertEqual(moved.subject, 'From %s: From %s: Hello' % (
            _ERASED_NAME, _ERASED_NAME))
        self.assertFalse(self._quoting(head, ['shed', 'porch']))

    def test_erase_button_only_for_meta_admins(self):
        self._give_sales(self.admin_a)
        views = {
            'form': self.env.ref('crm.crm_lead_view_form').id,
            'list': self.env.ref('crm.crm_case_tree_view_leads').id,
        }
        Lead = self.env['crm.lead']
        checks = [('form', views['form']), ('list', views['list']),
                  ('list', self.env.ref('crm.crm_case_tree_view_oppor').id)]
        xpath = ("//header/button"
                 "[@name='action_meta_erase_personal_data']")
        for view_type, view_id in checks:
            arch = etree.fromstring(Lead.with_user(self.admin_a).get_view(
                view_id, view_type)['arch'])
            buttons = arch.xpath(xpath)
            self.assertEqual(len(buttons), 1, (view_type, view_id))
            confirm = buttons[0].get('confirm')
            self.assertIn('200 leads', confirm)
            self.assertIn('Where Odoo has no record of the merge', confirm)
            arch = etree.fromstring(Lead.with_user(self.sales_a).get_view(
                view_id, view_type)['arch'])
            self.assertFalse(arch.xpath(xpath), (view_type, view_id))


_REQUIRED_KEPT = ("These fields are required and were kept; clear them by "
                  "hand if needed: Member Number.")


@tagged('post_install', '-at_install')
class TestEraseRequiredField(EraseFixtureMixin, MultiCompanyFixtureMixin,
                             IngestFixtureMixin, TransactionCase):
    """A form question mapped to a lead field that can't be left empty."""

    _TYPED_FIELDS = dict(EraseFixtureMixin._TYPED_FIELDS, x_member_no=(
        'char', 'Member Number', {'required': True}))

    @classmethod
    def setUpClass(cls):
        super().setUpClass()
        # Leads made without the answer take this value.
        cls.env['ir.default'].set('crm.lead', 'x_member_no', 'none')
        # Odoo can't add NOT NULL while older leads have no value. Fill
        # them and add it, as on a database where it went through.
        cls.env.cr.execute(
            "UPDATE crm_lead SET x_member_no = 'none' "
            "WHERE x_member_no IS NULL")
        cls.env.cr.execute(
            "ALTER TABLE crm_lead ALTER COLUMN x_member_no SET NOT NULL")

    def setUp(self):
        super().setUp()
        self._give_sales(self.admin_a)
        self._map_row('member_no', 'x_member_no')
        self.newsletter = self.env['meta.lead.form'].create({
            'name': 'Newsletter', 'form_id': 'F2', 'page_id': self.page.id})

    def _member(self, leadgen_id, number):
        lead = self._ingest(leadgen_id, self._contact(97, [
            {'name': 'member_no', 'values': [number]}]))
        self.assertEqual(lead.x_member_no, number)
        return lead

    def _note(self, lead):
        notes = self._notes(lead)
        self.assertEqual(len(notes), 1)
        return notes.body

    def _assert_contact_erased(self, lead):
        for name in ('contact_name', 'email_from', 'phone'):
            self.assertFalse(lead[name], name)
        self.assertFalse(lead.sudo().answer_ids)
        self.assertTrue(lead.meta_personal_data_erased_at)

    def test_required_mapped_field_kept_and_named(self):
        member = self._member('LGRQ_A', 'M-4471')
        with self._patch_graph(fetch_lead=self._fake_lead(
                id='LGRQ_B', form_id='F2', field_data=self._contact(98))):
            other = self.Ingest.ingest_leadgen(self.page, 'LGRQ_B', 'manual')
        self.assertEqual(other.meta_form_id_ref, self.newsletter)
        self.assertEqual(other.x_member_no, 'none')

        result = self._erase(member | other)
        self.env.flush_all()

        self.assertEqual(result['tag'], 'display_notification')
        self.assertEqual(member.x_member_no, 'M-4471')
        self.assertIn(_REQUIRED_KEPT, self._note(member))
        self._assert_contact_erased(member)
        # Its form doesn't fill the field, so nothing of it is Meta's.
        self._assert_contact_erased(other)
        self.assertEqual(other.x_member_no, 'none')
        self.assertNotIn('Member Number', self._note(other))

    def test_required_field_without_not_null(self):
        # A required field added to a table that already held leads gets
        # no NOT NULL, so a lead may still have it empty.
        self.env.cr.execute(
            "ALTER TABLE crm_lead ALTER COLUMN x_member_no DROP NOT NULL")
        member = self._member('LGRQ_N1', 'M-5502')
        empty = self._ingest('LGRQ_N2', self._contact(98))
        self.env.flush_all()
        self.env.cr.execute(
            "UPDATE crm_lead SET x_member_no = NULL WHERE id = %s",
            [empty.id])
        self.env.invalidate_all()
        self.assertFalse(empty.x_member_no)

        self._erase(member | empty)
        self.env.flush_all()

        self.assertEqual(member.x_member_no, 'M-5502')
        self.assertIn(_REQUIRED_KEPT, self._note(member))
        self._assert_contact_erased(member)
        self._assert_contact_erased(empty)
        self.assertFalse(empty.x_member_no)
        self.assertNotIn('Member Number', self._note(empty))


class RowEraseMixin:
    """Sync-log and queue rows of Meta leads that never became a lead."""

    def _payload(self, leadgen_id):
        return json.dumps({'id': leadgen_id, 'field_data': [
            {'name': 'email',
             'values': ['%s@example.com' % leadgen_id.lower()]}]})

    def _log_row(self, leadgen_id, status='failed', page=None, **vals):
        return self.env['meta.sync.log'].sudo().create(dict({
            'meta_leadgen_id': leadgen_id, 'trigger': 'cron',
            'status': status, 'raw_payload': self._payload(leadgen_id),
            'page_id': page.id if page else False,
        }, **vals))

    def _event(self, leadgen_id, status='pending', page_id='PG1',
               company=None, **vals):
        return self.env['meta.webhook.event'].sudo().create(dict({
            'leadgen_id': leadgen_id, 'page_id': page_id, 'form_id': 'F1',
            'status': status,
            'page_company_id': company.id if company else False,
            'raw_payload': self._payload(leadgen_id),
        }, **vals))

    def _logs(self, leadgen_id):
        return self.env['meta.sync.log'].sudo().search(
            [('meta_leadgen_id', '=', leadgen_id)])

    def _events(self, leadgen_id):
        return self.env['meta.webhook.event'].sudo().search(
            [('leadgen_id', '=', leadgen_id)])

    def _tomb(self, leadgen_id):
        return self.env['meta.leadgen.tombstone'].sudo().search(
            [('leadgen_id', '=', leadgen_id)])

    def _assert_erased(self, *recordsets):
        for rows in recordsets:
            self.assertTrue(rows)
            for row in rows.sudo():
                self.assertFalse(row.raw_payload, row)
                self.assertEqual(row.payload_cleared, 'erased', row)

    def _ingest_lead(self, leadgen_id, n):
        data = self._fake_lead(id=leadgen_id, field_data=[
            {'name': 'email', 'values': ['rowlead%s@example.com' % n]},
            {'name': 'full_name', 'values': ['Row Person %s' % n]}])
        with self._patch_graph(fetch_lead=data):
            return self.Ingest.ingest_leadgen(self.page, leadgen_id, 'manual')

    def _give_sales(self, user):
        user.groups_id |= self.env.ref(
            'sales_team.group_sale_salesman_all_leads')


@tagged('post_install', '-at_install')
class TestEraseRows(RowEraseMixin, MultiCompanyFixtureMixin,
                    IngestFixtureMixin, TransactionCase):

    # -- an erased lead's data does not come back ----------------------- #
    def test_backfill_after_lead_erase_stores_no_payload(self):
        lead = self._ingest_lead('LGRE_BF', 1)
        lead.action_meta_erase_personal_data()
        before = self._logs('LGRE_BF')
        self._assert_erased(before)
        item = self._fake_lead(
            id='LGRE_BF',
            created_time=fields.Datetime.now().strftime(
                '%Y-%m-%dT%H:%M:%S+0000'),
            field_data=[{'name': 'email',
                         'values': ['rowlead1@example.com']}])
        with patch.object(self.Client, '_iter_paged',
                          return_value=iter([item])):
            self.env['meta.lead.form']._cron_backfill()
        new = self._logs('LGRE_BF') - before
        self.assertEqual(new.mapped('status'), ['skipped_idempotent'])
        self._assert_erased(self._logs('LGRE_BF'))

    def test_webhook_resend_after_lead_erase_stores_no_payload(self):
        lead = self._ingest_lead('LGRE_WH', 2)
        lead.action_meta_erase_personal_data()
        before = self._logs('LGRE_WH')
        payload = {'object': 'page', 'entry': [{'id': 'PG1', 'changes': [{
            'field': 'leadgen', 'value': {
                'leadgen_id': 'LGRE_WH', 'page_id': 'PG1',
                'form_id': 'F1'}}]}]}
        Event = self.env['meta.webhook.event'].sudo()
        Event._ingest_payload(payload, raw=json.dumps(payload).encode())
        event = self._events('LGRE_WH')
        self.assertEqual(event.status, 'pending')
        with self._patch_graph():
            Event._cron_drain()
        self.assertEqual(event.status, 'done')
        self._fetch_lead.assert_not_called()
        new = self._logs('LGRE_WH') - before
        self.assertEqual(new.mapped('status'), ['skipped_idempotent'])
        self._assert_erased(self._logs('LGRE_WH'))

    def test_record_for_erased_leadgen_keeps_no_payload(self):
        Tomb = self.env['meta.leadgen.tombstone']
        Tomb._remember(['LGRE_TE'], {'LGRE_TE': {'reason': 'erased'}})
        Tomb._remember(['LGRE_TD'], {'LGRE_TD': {'reason': 'deleted'}})
        raw = {'id': 'LGRE_TE', 'field_data': [
            {'name': 'email', 'values': ['tomb@example.com']}]}
        Log = self.env['meta.sync.log']
        erased = Log._record('LGRE_TE', 'cron', 'skipped_removed', raw=raw,
                             page=self.page)
        self._assert_erased(erased)
        # Same for a Meta Admin writing without superuser rights.
        erased = Log.with_user(self.admin_a)._record(
            'LGRE_TE', 'manual', 'skipped_removed', raw=raw, page=self.page)
        self._assert_erased(erased)
        # A plain removed lead keeps what it was given.
        kept = Log._record('LGRE_TD', 'cron', 'skipped_removed', raw=raw,
                           page=self.page)
        self.assertTrue(kept.raw_payload)
        self.assertFalse(kept.payload_cleared)

    # -- Retry ----------------------------------------------------------- #
    def test_retry_refused_for_erased_rows(self):
        log = self._log_row('LGRE_RL', page=self.page, raw_payload=False,
                            payload_cleared='erased')
        event = self._event('LGRE_RQ', status='failed', company=self.co_a,
                            raw_payload=False, payload_cleared='erased')
        with self._patch_graph():
            with self.assertRaisesRegex(UserError, "so it can't be retried"):
                log.with_user(self.admin_a).action_retry()
            with self.assertRaisesRegex(UserError, "so it can't be retried"):
                event.with_user(self.admin_a).action_retry()
            self._fetch_lead.assert_not_called()
        self.assertEqual(log.status, 'failed')
        self.assertEqual(log.retry_count, 0)
        self.assertEqual(event.status, 'failed')
        self.assertFalse(self._logs('LGRE_RQ'))

    def test_expired_row_is_still_retried(self):
        log = self._log_row('LGRE_EX', page=self.page, raw_payload=False,
                            payload_cleared='expired')
        data = self._fake_lead(id='LGRE_EX', field_data=[
            {'name': 'email', 'values': ['expired@example.com']}])
        with self._patch_graph(fetch_lead=data):
            log.with_user(self.admin_a).action_retry()
        self._fetch_lead.assert_called_once()
        self.assertEqual(log.status, 'success')
        self.assertTrue(self.Lead.sudo().search(
            [('meta_leadgen_id', '=', 'LGRE_EX')]))

    # -- row erase ------------------------------------------------------- #
    def _meta_user(self):
        return new_test_user(
            self.env, 'row_erase_meta_user',
            groups='base.group_user,meta_lead_ads.group_meta_user')

    def _state(self, leadgen_id):
        logs, events = self._logs(leadgen_id), self._events(leadgen_id)
        return (sorted((r.id, r.status, bool(r.raw_payload), r.payload_cleared)
                       for r in logs),
                sorted((e.id, e.status, bool(e.raw_payload), e.payload_cleared)
                       for e in events),
                [(t.removal_reason, t.page_id.id)
                 for t in self._tomb(leadgen_id)])

    def test_failed_row_erase_clears_every_copy(self):
        failed = self._log_row('LGRW_1', page=self.page)
        event = self._event('LGRW_1', company=self.co_a)
        result = failed.with_user(self.admin_a).action_erase_personal_data()
        self.assertEqual(result['tag'], 'display_notification')
        logs = self._logs('LGRW_1')
        self.assertEqual(logs, failed)
        self._assert_erased(logs, event)
        self.assertEqual(failed.status, 'skipped_removed')
        self.assertEqual(event.status, 'done')
        self.assertFalse(event.lead_id)
        tomb = self._tomb('LGRW_1')
        self.assertEqual(tomb.removal_reason, 'erased')
        self.assertEqual(tomb.page_id, self.page)
        self.assertEqual(tomb.company_id, self.co_a)
        self.assertTrue(failed.exists() and event.exists())

    def test_skip_row_written_by_the_erase_is_marked(self):
        event = self._event('LGRW_2', company=self.co_a)
        seen = self._log_row('LGRW_2', status='skipped_idempotent',
                             page=self.page)
        event.with_user(self.admin_a).action_erase_personal_data()
        logs = self._logs('LGRW_2')
        self.assertEqual(len(logs), 2)
        skip = logs - seen
        self.assertEqual(skip.status, 'skipped_removed')
        self.assertEqual(skip.page_id, self.page)
        self._assert_erased(logs, event)
        self.assertEqual(event.status, 'done')

    def test_deleted_record_becomes_erased_and_restore_refused(self):
        Tomb = self.env['meta.leadgen.tombstone']
        Tomb._remember(['LGRW_3'], {'LGRW_3': {
            'reason': 'deleted', 'page_id': self.page.id,
            'form_id': self.form.id, 'company_id': self.co_a.id}})
        tomb = self._tomb('LGRW_3')
        created = tomb.create_date
        row = self._log_row('LGRW_3', page=self.page)
        row.with_user(self.admin_a).action_erase_personal_data()
        tomb.invalidate_recordset()
        self.assertEqual(self._tomb('LGRW_3'), tomb)
        self.assertEqual(tomb.removal_reason, 'erased')
        self.assertEqual(tomb.page_id, self.page)
        self.assertEqual(tomb.form_id, self.form)
        self.assertEqual(tomb.company_id, self.co_a)
        self.assertEqual(tomb.create_date, created)
        with self._patch_graph():
            with self.assertRaisesRegex(UserError, "can't be fetched from "
                                                   "Meta again"):
                tomb.with_user(self.admin_a).action_restore()
            self._fetch_lead.assert_not_called()
        self.assertTrue(tomb.exists())

    def test_merged_record_without_survivor_keeps_its_other_columns(self):
        Tomb = self.env['meta.leadgen.tombstone']
        Tomb._remember(['LGRW_MG'], {'LGRW_MG': {
            'reason': 'merged', 'company_id': self.co_a.id}})
        row = self._log_row('LGRW_MG', page=self.page)
        row.with_user(self.admin_a).action_erase_personal_data()
        tomb = self._tomb('LGRW_MG')
        self.assertEqual(tomb.removal_reason, 'erased')
        self.assertEqual(tomb.company_id, self.co_a)
        # Filled in where the old record had nothing.
        self.assertEqual(tomb.page_id, self.page)

    def test_erase_twice_leaves_the_same_state(self):
        self._log_row('LGRW_4', page=self.page)
        self._event('LGRW_4', company=self.co_a)
        Log = self.env['meta.sync.log']
        details = {'LGRW_4': {'page_id': self.page.id,
                              'company_id': self.co_a.id}}
        Log._erase_leadgen_copies(['LGRW_4'], 'manual', details=details)
        first = self._state('LGRW_4')
        Log._erase_leadgen_copies(['LGRW_4'], 'manual', details=details)
        self.assertEqual(self._state('LGRW_4'), first)
        self.assertEqual(len(self._tomb('LGRW_4')), 1)
        self.assertEqual(len(first[0]), 1)

    def test_lead_id_named_only_to_readers(self):
        lead_b = self.Lead.create({
            'name': 'Held in B', 'type': 'lead', 'user_id': False,
            'team_id': False, 'company_id': self.co_b.id,
            'meta_leadgen_id': 'LGRW_5'})
        event = self._event('LGRW_5', company=self.co_a)
        self.assertEqual(event.company_id, self.co_a)
        with self.assertRaises(UserError) as caught:
            event.with_user(self.admin_a).action_erase_personal_data()
        message = str(caught.exception)
        self.assertIn('A lead holds this Meta lead', message)
        self.assertNotIn(str(lead_b.id), message)
        self._give_sales(self.admin_ab)
        with self.assertRaisesRegex(
                UserError, 'Lead #%s holds this Meta lead' % lead_b.id):
            event.with_user(self.admin_ab).action_erase_personal_data()
        self.assertTrue(event.raw_payload)
        self.assertFalse(event.payload_cleared)
        self.assertFalse(self._tomb('LGRW_5'))

    def test_id_merged_into_a_live_lead_refused(self):
        self._give_sales(self.admin_a)
        survivor = self.Lead.create({
            'name': 'Survivor', 'type': 'lead', 'company_id': self.co_a.id})
        self.env['meta.leadgen.tombstone']._remember(['LGRW_6'], {'LGRW_6': {
            'reason': 'merged', 'merged_into': survivor.id,
            'company_id': self.co_a.id}})
        row = self._log_row('LGRW_6', page=self.page)
        with self.assertRaisesRegex(
                UserError, 'Lead #%s holds this Meta lead' % survivor.id):
            row.with_user(self.admin_a).action_erase_personal_data()
        self.assertTrue(row.raw_payload)
        self.assertEqual(self._tomb('LGRW_6').removal_reason, 'merged')

    def test_row_pointing_at_a_live_lead_refused(self):
        lead = self.Lead.create({
            'name': 'Other id', 'type': 'lead', 'company_id': self.co_a.id,
            'meta_leadgen_id': 'LGRW_OTHER'})
        row = self._log_row('LGRW_LK', status='success', page=self.page,
                            lead_id=lead.id)
        with self.assertRaisesRegex(UserError, 'holds this Meta lead'):
            self.env['meta.sync.log']._erase_leadgen_copies(
                ['LGRW_LK'], 'manual')
        self.assertTrue(row.raw_payload)

    def test_meta_user_and_other_company_admin_refused(self):
        row = self._log_row('LGRW_7', page=self.page)
        event = self._event('LGRW_7', company=self.co_a)
        meta_user = self._meta_user()
        with self.assertRaises(AccessError):
            row.with_user(meta_user).action_erase_personal_data()
        with self.assertRaises(AccessError):
            event.with_user(meta_user).action_erase_personal_data()
        self.env.invalidate_all()
        with self.assertRaises(AccessError):
            row.with_user(self.admin_b).action_erase_personal_data()
        with self.assertRaises(AccessError):
            event.with_user(self.admin_b).action_erase_personal_data()
        self.assertTrue(row.raw_payload)
        self.assertFalse(self._tomb('LGRW_7'))

    def test_row_cap_refused_before_any_change(self):
        rows = self.env['meta.sync.log']
        for n in range(3):
            rows |= self._log_row('LGRW_C%d' % n, page=self.page)
        with patch.object(meta_sync_log_module, '_ERASE_ROW_LIMIT', 2):
            with self.assertRaisesRegex(UserError, 'at most 2'):
                rows.with_user(self.admin_a).action_erase_personal_data()
        for row in rows:
            self.assertTrue(row.raw_payload)
            self.assertFalse(row.payload_cleared)
        self.assertFalse(self.env['meta.leadgen.tombstone'].sudo().search(
            [('leadgen_id', 'in', ['LGRW_C0', 'LGRW_C1', 'LGRW_C2'])]))

    def test_already_erased_rows_refused(self):
        row = self._log_row('LGRW_AE', page=self.page)
        row.with_user(self.admin_a).action_erase_personal_data()
        with self.assertRaisesRegex(UserError, 'Select at least one row'):
            row.with_user(self.admin_a).action_erase_personal_data()

    def test_queue_erase_clears_blank_company_log_copy(self):
        event = self._event('LGRW_8', page_id='PG_UNKNOWN')
        self.assertFalse(event.company_id)
        blank = self._log_row('LGRW_8')
        self.assertFalse(blank.company_id)
        event.with_user(self.admin_a).action_erase_personal_data()
        self._assert_erased(blank, event)
        self.assertEqual(blank.status, 'skipped_removed')
        self.assertEqual(self._tomb('LGRW_8').removal_reason, 'erased')

    def test_sweep_after_row_erase_brings_nothing_back(self):
        row = self._log_row('LGRW_9', page=self.page)
        row.with_user(self.admin_a).action_erase_personal_data()
        item = self._fake_lead(
            id='LGRW_9',
            created_time=fields.Datetime.now().strftime(
                '%Y-%m-%dT%H:%M:%S+0000'),
            field_data=[{'name': 'email',
                         'values': ['lgrw_9@example.com']}])
        with self._patch_graph(), patch.object(
                self.Client, '_iter_paged', return_value=iter([item])):
            self.env['meta.lead.form']._cron_backfill()
        self.assertFalse(self.Lead.sudo().with_context(
            active_test=False).search([('meta_leadgen_id', '=', 'LGRW_9')]))
        self._assert_erased(self._logs('LGRW_9'))

    def test_restore_hidden_for_erased_records(self):
        Tomb = self.env['meta.leadgen.tombstone'].with_user(self.admin_a)
        for view_type in ('list', 'form'):
            arch = etree.fromstring(Tomb.get_view(view_type=view_type)['arch'])
            buttons = arch.xpath("//button[@name='action_restore']")
            self.assertEqual(len(buttons), 1, view_type)
            self.assertIn("removal_reason == 'erased'",
                          buttons[0].get('invisible'), view_type)

    def test_erase_buttons_only_for_meta_admins(self):
        meta_user = self._meta_user()
        xpath = "//button[@name='action_erase_personal_data']"
        for model in ('meta.sync.log', 'meta.webhook.event'):
            for view_type in ('list', 'form'):
                arch = etree.fromstring(self.env[model].with_user(
                    self.admin_a).get_view(view_type=view_type)['arch'])
                self.assertEqual(len(arch.xpath(xpath)), 1,
                                 (model, view_type))
                self.assertIn('200 rows', arch.xpath(xpath)[0].get('confirm'))
                arch = etree.fromstring(self.env[model].with_user(
                    meta_user).get_view(view_type=view_type)['arch'])
                self.assertFalse(arch.xpath(xpath), (model, view_type))


@tagged('post_install', '-at_install')
class TestEraseQuarantined(RowEraseMixin, MultiCompanyFixtureMixin,
                           IngestFixtureMixin, TransactionCase):
    """Rows with no company that still hold a payload are hidden by the
    sync-log record rule, yet a Meta Admin must be able to erase them."""

    def setUp(self):
        super().setUp()
        # No Page and no lead, so the stored company stays blank.
        self.q1 = self._log_row('LGQ1')
        self.q1_event = self._event('LGQ1', page_id='PG_UNKNOWN')
        self.qb = self._log_row('LGQB', page=self.page_b)
        self.q2 = self._log_row('LGQ2')
        self.lead_q2 = self.Lead.create({
            'name': 'Holds LGQ2', 'type': 'lead', 'user_id': False,
            'team_id': False, 'company_id': self.co_b.id,
            'meta_leadgen_id': 'LGQ2'})
        self.q3 = self._log_row('LGQ3')
        self.meta_user = new_test_user(
            self.env, 'quarantine_meta_user',
            groups='base.group_user,meta_lead_ads.group_meta_user')
        for row in (self.q1, self.q2, self.q3):
            self.assertFalse(row.company_id)
        self.assertEqual(self.qb.company_id, self.co_b)

    def _open(self, user):
        return self.env['meta.sync.log'].with_user(
            user).action_open_quarantined_rows()

    def _snapshot(self, user, leadgen_id=None):
        domain = [('create_uid', '=', user.id)]
        if leadgen_id:
            domain.append(('leadgen_id', '=', leadgen_id))
        return self.env['meta.sync.log.quarantine'].sudo().search(
            domain).with_user(user)

    def test_rows_are_hidden_from_admins(self):
        self.assertFalse(self.env['meta.sync.log'].with_user(
            self.admin_a).search([('meta_leadgen_id', '=', 'LGQ1')]))

    def test_snapshot_lists_blank_payload_rows_only(self):
        action = self._open(self.admin_a)
        self.assertEqual(action['type'], 'ir.actions.act_window')
        self.assertEqual(action['res_model'], 'meta.sync.log.quarantine')
        snap = self._snapshot(self.admin_a)
        row = snap.filtered(lambda r: r.leadgen_id == 'LGQ1')
        self.assertEqual(row.log_ref, self.q1.id)
        self.assertEqual(row.status, 'failed')
        self.assertEqual(row.logged_at, self.q1.create_date)
        self.assertNotIn('LGQB', snap.mapped('leadgen_id'))
        self.assertNotIn(self.qb.id, snap.mapped('log_ref'))
        # Opening it again replaces the caller's snapshot.
        self._open(self.admin_a)
        self.assertEqual(len(self._snapshot(self.admin_a, 'LGQ1')), 1)
        # These rows belong to no company, so every Meta Admin lists them.
        self._open(self.admin_b)
        self.assertIn('LGQ1', self._snapshot(self.admin_b).mapped('leadgen_id'))

    def test_snapshot_holds_no_payload_or_links(self):
        Snapshot = self.env['meta.sync.log.quarantine']
        self.assertNotIn('raw_payload', Snapshot._fields)
        magic = {'id', 'display_name', 'create_uid', 'create_date',
                 'write_uid', 'write_date'}
        for name, field in Snapshot._fields.items():
            if name in magic:
                continue
            self.assertNotIn(field.type, ('text', 'html', 'binary',
                                          'many2one', 'one2many',
                                          'many2many'), name)

    def test_erase_on_demand(self):
        self._open(self.admin_a)
        snap = self._snapshot(self.admin_a, 'LGQ1')
        result = snap.action_erase_personal_data()
        self.assertEqual(result['tag'], 'display_notification')
        self.assertFalse(snap.exists())
        self.assertEqual(self._logs('LGQ1'), self.q1)
        self._assert_erased(self.q1, self.q1_event)
        self.assertEqual(self.q1.status, 'skipped_removed')
        self.assertEqual(self.q1_event.status, 'done')
        self.assertEqual(self._tomb('LGQ1').removal_reason, 'erased')
        # The cleared row is a plain shared trace now, and stays closed.
        self.env.invalidate_all()
        row = self.env['meta.sync.log'].with_user(self.admin_a).search(
            [('meta_leadgen_id', '=', 'LGQ1')])
        self.assertEqual(row, self.q1)
        self.assertFalse(row.raw_payload)
        with self._patch_graph():
            with self.assertRaisesRegex(UserError, "so it can't be retried"):
                row.action_retry()
            self._fetch_lead.assert_not_called()

    def test_forged_ref_refused(self):
        forged = self.env['meta.sync.log.quarantine'].with_user(
            self.admin_a).create({'log_ref': self.qb.id, 'leadgen_id': 'LGQB'})
        with self.assertRaisesRegex(UserError, 'no longer quarantined'):
            forged.action_erase_personal_data()
        self.assertTrue(self.qb.raw_payload)
        self.assertFalse(self.qb.payload_cleared)
        self.assertFalse(self._tomb('LGQB'))

    def test_payload_expired_since_snapshot_refused(self):
        self._open(self.admin_a)
        snap = self._snapshot(self.admin_a, 'LGQ3')
        self.assertTrue(snap)
        self.q3.write({'raw_payload': False, 'payload_cleared': 'expired'})
        with self.assertRaisesRegex(UserError, 'no longer quarantined'):
            snap.action_erase_personal_data()
        self.assertFalse(self._tomb('LGQ3'))
        self.assertEqual(self.q3.payload_cleared, 'expired')

    def test_lead_holding_the_id_refused(self):
        self._open(self.admin_a)
        with self.assertRaises(UserError) as caught:
            self._snapshot(self.admin_a, 'LGQ2').action_erase_personal_data()
        message = str(caught.exception)
        self.assertIn('A lead holds this Meta lead', message)
        self.assertNotIn(str(self.lead_q2.id), message)
        self._give_sales(self.admin_b)
        self._open(self.admin_b)
        with self.assertRaisesRegex(
                UserError, 'Lead #%s holds this Meta lead' % self.lead_q2.id):
            self._snapshot(self.admin_b, 'LGQ2').action_erase_personal_data()
        self.assertTrue(self.q2.raw_payload)
        self.assertFalse(self.q2.payload_cleared)
        self.assertFalse(self._tomb('LGQ2'))

    def test_meta_user_refused(self):
        with self.assertRaises(AccessError):
            self._open(self.meta_user)
        with self.assertRaises(AccessError):
            self.env['meta.sync.log.quarantine'].with_user(
                self.meta_user).search([])

    def test_quarantine_button_only_for_meta_admins(self):
        xpath = "//header/button[@name='action_open_quarantined_rows']"
        Log = self.env['meta.sync.log']
        arch = etree.fromstring(Log.with_user(self.admin_a).get_view(
            view_type='list')['arch'])
        self.assertEqual(len(arch.xpath(xpath)), 1)
        self.assertEqual(arch.xpath(xpath)[0].get('display'), 'always')
        arch = etree.fromstring(Log.with_user(self.meta_user).get_view(
            view_type='list')['arch'])
        self.assertFalse(arch.xpath(xpath))

    def test_quarantine_button_called_as_the_list_does(self):
        # The list sends the selected ids, or an empty list when nothing is
        # selected, as the first argument, as for any button.
        seen = self._log_row('LGQ_SEEN', page=self.page)
        Log = self.env['meta.sync.log'].with_user(self.admin_a)
        self.assertEqual(Log.search([('meta_leadgen_id', '=', 'LGQ_SEEN')]),
                         seen)
        for ids in ([], seen.ids):
            action = call_kw(Log, 'action_open_quarantined_rows', [ids], {})
            self.assertEqual(action['res_model'], 'meta.sync.log.quarantine')
            found = self._snapshot(self.admin_a).mapped('leadgen_id')
            self.assertIn('LGQ1', found)
            self.assertNotIn('LGQ_SEEN', found)
        with self.assertRaises(AccessError):
            call_kw(self.env['meta.sync.log'].with_user(self.meta_user),
                    'action_open_quarantined_rows', [[]], {})
