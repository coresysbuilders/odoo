# Meta Lead Ads Integration for Odoo — © 2026 CoreSys Builders. All rights reserved.
# Licensed under the Odoo Proprietary License v1.0 (OPL-1).
# Unauthorized copying, redistribution, or resale of this software, in whole or in
# part, via any medium, is strictly prohibited and constitutes a license violation.
# OPL-1: https://www.odoo.com/documentation/19.0/legal/licenses.html#odoo-apps

"""Submission time, attribution on dedup matches, and typed field mapping.

``meta_submitted_at`` stores Meta's ``created_time`` as naive UTC. A lead
matched by dedup gets Meta attribution only on its blank fields and keeps
its UTM source. Mapped answers are converted to the target field type; one
that doesn't convert cleanly is kept as a meta.lead.answer row instead of
being guessed.
"""
from datetime import date, datetime

from odoo.tests.common import TransactionCase, tagged

from .test_ingest import IngestFixtureMixin


@tagged('post_install', '-at_install')
class TestLeadDataGaps(IngestFixtureMixin, TransactionCase):

    def _map(self, key, field_name, form=None):
        return self.env['meta.field.mapping'].create({
            'form_id': (form or self.form).id, 'meta_key': key,
            'crm_field_id': self.env['ir.model.fields']._get(
                'crm.lead', field_name).id})

    # ---- submission time -------------------------------------------------

    def test_submitted_at_stored_on_create(self):
        with self._patch_graph():
            lead = self.Ingest.ingest_leadgen(self.page, 'LG1', 'manual')
        self.assertEqual(lead.meta_submitted_at, datetime(2026, 6, 14, 10, 0))

    def test_submitted_at_converted_to_utc(self):
        payload = self._fake_lead(created_time='2026-06-14T01:30:00+0200')
        with self._patch_graph(fetch_lead=payload):
            lead = self.Ingest.ingest_leadgen(self.page, 'LG1', 'manual')
        self.assertEqual(lead.meta_submitted_at, datetime(2026, 6, 13, 23, 30))
        # The title's {date} uses the same UTC day as the stored column.
        self.assertTrue(lead.name.endswith('2026-06-13'))

    def test_malformed_created_time_still_creates_lead(self):
        payload = self._fake_lead(created_time='not-a-date')
        with self._patch_graph(fetch_lead=payload):
            lead = self.Ingest.ingest_leadgen(self.page, 'LG1', 'manual')
        self.assertTrue(lead)
        self.assertFalse(lead.meta_submitted_at)

    # ---- dedup-match attribution ------------------------------------------

    def test_match_path_writes_attribution_blank_only(self):
        website = self.env['utm.source'].create({'name': 'Website test src'})
        existing = self.Lead.create({
            'name': 'Existing', 'email_from': 'jane@example.com',
            'type': 'lead', 'source_id': website.id,
            'meta_form_name': 'Hand-set form'})
        with self._patch_graph():
            lead = self.Ingest.ingest_leadgen(self.page, 'LG1', 'manual')
        self.assertEqual(lead, existing)
        self.assertEqual(existing.meta_campaign_name, 'Summer')
        self.assertEqual(existing.meta_adset_name, 'Set')
        self.assertEqual(existing.meta_ad_name, 'Creative')
        self.assertEqual(existing.meta_platform, 'facebook')
        self.assertEqual(existing.meta_submitted_at, datetime(2026, 6, 14, 10, 0))
        self.assertEqual(existing.meta_page_id_ref, self.page)
        # Blank-only: a value already on the lead is not overwritten...
        self.assertEqual(existing.meta_form_name, 'Hand-set form')
        # ...including its first-touch UTM source; blank UTM fields are filled.
        self.assertEqual(existing.source_id, website)
        self.assertEqual(existing.medium_id,
                         self.env.ref('meta_lead_ads.utm_medium_paid_social'))
        self.assertEqual(existing.campaign_id.name, 'Summer')

    # ---- typed mapping ----------------------------------------------------

    def test_typed_mapping_converts_answers(self):
        Tag = self.env['crm.tag']
        vip = Tag.create({'name': 'VIP test tag'})
        hot = Tag.create({'name': 'Hot test tag'})
        priority_field = self.Lead._fields['priority']
        key, label = priority_field._description_selection(self.env)[-1]
        self._map('budget', 'expected_revenue')
        self._map('country', 'country_id')
        self._map('interests', 'tag_ids')
        self._map('urgency', 'priority')
        self._map('deadline', 'date_deadline')
        payload = self._fake_lead(field_data=[
            {'name': 'email', 'values': ['typed@example.com']},
            {'name': 'budget', 'values': ['$5,000']},
            {'name': 'country', 'values': ['egypt']},
            {'name': 'interests', 'values': ['VIP test tag', 'hot TEST tag']},
            {'name': 'urgency', 'values': [label]},
            {'name': 'deadline', 'values': ['2026-07-01']},
        ])
        with self._patch_graph(fetch_lead=payload):
            lead = self.Ingest.ingest_leadgen(self.page, 'LG1', 'manual')
        self.assertEqual(lead.expected_revenue, 5000.0)
        self.assertEqual(lead.country_id, self.env.ref('base.eg'))
        self.assertEqual(lead.tag_ids, vip | hot)
        self.assertEqual(lead.priority, key)
        self.assertEqual(lead.date_deadline, date(2026, 7, 1))
        # Converted answers are not duplicated as unmapped answer rows.
        self.assertFalse(lead.answer_ids)

    def test_country_matched_by_code(self):
        self._map('country', 'country_id')
        payload = self._fake_lead(field_data=[
            {'name': 'email', 'values': ['code@example.com']},
            {'name': 'country', 'values': ['EG']}])
        with self._patch_graph(fetch_lead=payload):
            lead = self.Ingest.ingest_leadgen(self.page, 'LG1', 'manual')
        self.assertEqual(lead.country_id, self.env.ref('base.eg'))

    def test_unconvertible_answers_are_kept_not_guessed(self):
        self._map('budget', 'expected_revenue')
        self._map('country', 'country_id')
        self._map('interests', 'tag_ids')
        payload = self._fake_lead(field_data=[
            {'name': 'email', 'values': ['bad@example.com']},
            {'name': 'budget', 'values': ['$5k']},
            {'name': 'country', 'values': ['Atlantis']},
            {'name': 'interests', 'values': ['No such tag xyz']},
        ])
        with self._patch_graph(fetch_lead=payload):
            lead = self.Ingest.ingest_leadgen(self.page, 'LG1', 'manual')
        self.assertFalse(lead.expected_revenue)
        self.assertFalse(lead.country_id)
        self.assertFalse(lead.tag_ids)
        kept = {a.question_key: a.value for a in lead.answer_ids}
        self.assertEqual(kept, {'budget': '$5k', 'country': 'Atlantis',
                                'interests': 'No such tag xyz'})

    def test_wildcards_do_not_match_records(self):
        """'%' and '_' in an answer are literal, not =ilike wildcards."""
        self._map('country', 'country_id')
        payload = self._fake_lead(field_data=[
            {'name': 'email', 'values': ['wild@example.com']},
            {'name': 'country', 'values': ['Egyp_']}])
        with self._patch_graph(fetch_lead=payload):
            lead = self.Ingest.ingest_leadgen(self.page, 'LG1', 'manual')
        self.assertFalse(lead.country_id)

    def test_coerce_rules(self):
        coerce = self.Ingest._coerce_answer
        fields_ = self.Lead._fields
        self.assertEqual(coerce(fields_['color'], ['3'], '3'), (True, 3))
        self.assertEqual(coerce(fields_['color'], ['3.5'], '3.5'), (False, None))
        self.assertEqual(coerce(fields_['color'], ['99999999999'], ''),
                         (False, None))
        self.assertEqual(coerce(fields_['expected_revenue'], ['EGP 1,250.50'], ''),
                         (True, 1250.5))
        self.assertEqual(coerce(fields_['active'], ['Yes'], 'Yes'), (True, True))
        self.assertEqual(coerce(fields_['active'], ['no'], 'no'), (True, False))
        self.assertEqual(coerce(fields_['active'], ['maybe'], 'maybe'),
                         (False, None))
        self.assertEqual(coerce(fields_['date_deadline'], ['07/01/2026'], ''),
                         (True, date(2026, 7, 1)))
        # Several values for a single-valued target are not guessed between.
        self.assertEqual(coerce(fields_['country_id'], ['Egypt', 'France'], ''),
                         (False, None))
        # Text targets keep the joined display string.
        self.assertEqual(coerce(fields_['city'], ['A', 'B'], 'A, B'),
                         (True, 'A, B'))
