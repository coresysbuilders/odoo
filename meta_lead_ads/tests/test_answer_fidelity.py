# Meta Lead Ads Integration for Odoo — © 2026 CoreSys Builders. All rights reserved.
# Licensed under the Odoo Proprietary License v1.0 (OPL-1).
# Unauthorized copying, redistribution, or resale of this software, in whole or in
# part, via any medium, is strictly prohibited and constitutes a license violation.
# OPL-1: https://www.odoo.com/documentation/19.0/legal/licenses.html#odoo-apps

"""Numbers and dates that should not be guessed when mapping answers.

An answer that is too big for a number field, or a date that could be read
two ways, is kept as a meta.lead.answer row and the lead is still created.
"""
from datetime import date, datetime, timezone

from lxml import etree

from odoo.tests import Form
from odoo.tests.common import TransactionCase, tagged

from odoo.addons.meta_lead_ads.models.meta_lead_ingest import (
    _parse_iso_fallback)

from .common_typed_targets import TypedTargetsMixin
from .test_ingest import IngestFixtureMixin


@tagged('post_install', '-at_install')
class TestAnswerFidelity(TypedTargetsMixin, IngestFixtureMixin,
                         TransactionCase):

    def _map(self, key, field_name, **extra):
        vals = {'form_id': self.form.id, 'meta_key': key,
                'crm_field_id': self.env['ir.model.fields']._get(
                    'crm.lead', field_name).id}
        vals.update(extra)
        return self.env['meta.field.mapping'].create(vals)

    def _number_targets(self):
        fields_ = self.Lead._fields
        return [fields_['expected_revenue'], fields_['probability'],
                fields_['color']]

    # ---- numbers -----------------------------------------------------------

    def test_regex_refuses_exponents_and_words(self):
        """Exponent and word forms never match _NUMBER_RE.

        They are refused by the pattern itself, before any Decimal code runs.
        This keeps it that way if the pattern is ever loosened."""
        coerce = self.Ingest._coerce_answer
        for field in self._number_targets():
            for raw in ('1e400', '1e308', 'NaN', 'inf', '-inf'):
                with self.subTest(field=field.name, raw=raw):
                    self.assertEqual(coerce(field, [raw], raw), (False, None))

    def test_decimal_guard_refuses_huge_numbers(self):
        """Plain digit strings of 1e15 or more are refused.

        The 1e15 bound is a policy choice, not a Postgres limit: Odoo rounds
        Float and Monetary values through float_round, which loses precision
        and can overflow long before the column would."""
        coerce = self.Ingest._coerce_answer
        huge = ('9' * 400, '1' + '0' * 16, '1' + '0' * 20,
                '1,000,000,000,000,000')
        for field in self._number_targets():
            for raw in huge:
                with self.subTest(field=field.name, raw=raw[:30]):
                    self.assertEqual(coerce(field, [raw], raw), (False, None))

    def test_numbers_below_the_bound_still_map(self):
        coerce = self.Ingest._coerce_answer
        fields_ = self.Lead._fields
        self.assertEqual(coerce(fields_['probability'], ['1,234.50'], ''),
                         (True, 1234.5))
        self.assertEqual(
            coerce(fields_['expected_revenue'], ['999999999999999'], ''),
            (True, 999999999999999.0))
        self.assertEqual(
            coerce(fields_['expected_revenue'], ['1000000000000000'], ''),
            (False, None))

    def test_integer_rules(self):
        coerce = self.Ingest._coerce_answer
        color = self.Lead._fields['color']
        self.assertEqual(coerce(color, ['12'], ''), (True, 12))
        self.assertEqual(coerce(color, ['-12'], ''), (True, -12))
        self.assertEqual(coerce(color, ['12.5'], ''), (False, None))
        self.assertEqual(coerce(color, [str(2 ** 31)], ''), (False, None))

    def test_huge_answer_is_kept_and_lead_created(self):
        self._map('budget', 'x_meta_typed_money')
        huge = '9' * 400
        payload = self._fake_lead(field_data=[
            {'name': 'email', 'values': ['huge@example.com']},
            {'name': 'budget', 'values': [huge]},
        ])
        with self._patch_graph(fetch_lead=payload):
            lead = self.Ingest.ingest_leadgen(self.page, 'LG1', 'manual')
        self.assertTrue(lead)
        self.assertFalse(lead.x_meta_typed_money)
        kept = {a.question_key: a.value for a in lead.answer_ids}
        self.assertEqual(kept, {'budget': huge})

    # ---- dates -------------------------------------------------------------

    def test_iso_dates_map_with_or_without_format(self):
        coerce = self.Ingest._coerce_answer
        deadline = self.Lead._fields['date_deadline']
        self.assertEqual(coerce(deadline, ['2026-12-25'], ''),
                         (True, date(2026, 12, 25)))
        for fmt in ('dmy', 'mdy', 'ymd'):
            with self.subTest(fmt=fmt):
                self.assertEqual(
                    coerce(deadline, ['2026-12-25'], '', date_format=fmt),
                    (True, date(2026, 12, 25)))
        closed = self.Lead._fields['date_closed']
        self.assertEqual(
            coerce(closed, ['2026-12-25T10:30:00+02:00'], ''),
            (True, datetime(2026, 12, 25, 8, 30)))

    def test_short_iso_times_map(self):
        coerce = self.Ingest._coerce_answer
        closed = self.Lead._fields['date_closed']
        cases = {
            '2026-12-25T10:00Z': datetime(2026, 12, 25, 10, 0),
            '2026-12-25T10:00': datetime(2026, 12, 25, 10, 0),
            '2026-12-25 10:00+05:00': datetime(2026, 12, 25, 5, 0),
            '2026-12-25T10:00:00+0000': datetime(2026, 12, 25, 10, 0),
            '2026-12-25T10:00:00.1234Z': datetime(2026, 12, 25, 10, 0, 0,
                                                  123400),
        }
        for raw, expected in cases.items():
            with self.subTest(raw=raw):
                self.assertEqual(coerce(closed, [raw], raw), (True, expected))
                # The fallback alone, which is all Python 3.10 has for these.
                parsed = _parse_iso_fallback(raw)
                if parsed.tzinfo:
                    parsed = parsed.astimezone(timezone.utc).replace(
                        tzinfo=None)
                self.assertEqual(parsed, expected)

    def test_unclear_dates_kept_without_format(self):
        coerce = self.Ingest._coerce_answer
        deadline = self.Lead._fields['date_deadline']
        for raw in ('03/04/2026', '25/12/2026', '20261225', '2026-W52-5'):
            with self.subTest(raw=raw):
                self.assertEqual(coerce(deadline, [raw], raw), (False, None))

    def test_out_of_range_date_kept_as_text(self):
        # Year 1 with a positive offset falls before datetime.min in UTC.
        coerce = self.Ingest._coerce_answer
        raw = '0001-01-01T00:00:00+05:00'
        for name in ('date_deadline', 'date_closed'):
            with self.subTest(field=name):
                self.assertEqual(coerce(self.Lead._fields[name], [raw], raw),
                                 (False, None))

    def test_declared_format_reads_the_date(self):
        coerce = self.Ingest._coerce_answer
        deadline = self.Lead._fields['date_deadline']
        self.assertEqual(
            coerce(deadline, ['03/04/2026'], '', date_format='dmy'),
            (True, date(2026, 4, 3)))
        self.assertEqual(
            coerce(deadline, ['03/04/2026'], '', date_format='mdy'),
            (True, date(2026, 3, 4)))
        self.assertEqual(
            coerce(deadline, ['2026-04-03'], '', date_format='ymd'),
            (True, date(2026, 4, 3)))
        self.assertEqual(
            coerce(deadline, ['03/04/2026'], '', date_format='ymd'),
            (False, None))
        self.assertEqual(
            coerce(deadline, ['31/02/2026'], '', date_format='dmy'),
            (False, None))

    # ---- declared date format on the mapping --------------------------------

    def _date_payload(self):
        return self._fake_lead(field_data=[
            {'name': 'email', 'values': ['dates@example.com']},
            {'name': 'start_date', 'values': ['03/04/2026']},
        ])

    def test_new_date_mapping_defaults_to_day_first(self):
        self.assertEqual(
            self._map('start_date', 'x_meta_typed_date').date_format, 'dmy')
        self.assertEqual(
            self._map('start_time', 'x_meta_typed_when').date_format, 'dmy')

    def test_new_non_date_mapping_has_no_date_format(self):
        self.assertFalse(self._map('tel', 'phone').date_format)

    def test_format_given_on_create_is_kept(self):
        self.assertEqual(self._map('start_date', 'x_meta_typed_date',
                                   date_format='mdy').date_format, 'mdy')
        self.assertFalse(self._map('due', 'x_meta_typed_date',
                                   form_id=self._second_form().id,
                                   date_format=False).date_format)

    def _second_form(self):
        return self.env['meta.lead.form'].create({
            'name': 'Second', 'form_id': 'F2', 'page_id': self.page.id})

    def test_changing_the_target_keeps_a_chosen_format(self):
        Field = self.env['ir.model.fields']
        mapping = self._map('when', 'phone')
        mapping.crm_field_id = Field._get('crm.lead', 'x_meta_typed_date')
        self.assertEqual(mapping.date_format, 'dmy')
        mapping.date_format = 'ymd'
        mapping.crm_field_id = Field._get('crm.lead', 'phone')
        # Ignored for other targets, so it is not left behind.
        self.assertFalse(mapping.date_format)
        # Moving between date targets keeps the admin's choice.
        mapping.write({'crm_field_id': Field._get('crm.lead',
                                                  'x_meta_typed_date').id,
                       'date_format': 'ymd'})
        self.assertEqual(mapping.date_format, 'ymd')
        mapping.crm_field_id = Field._get('crm.lead', 'x_meta_typed_when')
        self.assertEqual(mapping.date_format, 'ymd')
        draft = self.env['meta.field.mapping'].new(
            {'crm_field_id': mapping.crm_field_id.id, 'date_format': 'mdy'})
        draft.crm_field_id = Field._get('crm.lead', 'x_meta_typed_date')
        self.assertEqual(draft.date_format, 'mdy')

    def test_form_fills_day_first_for_a_date_target(self):
        with Form(self.env['meta.field.mapping']) as mapping:
            mapping.form_id = self.form
            mapping.meta_key = 'start_date'
            mapping.crm_field_name = 'phone'
            self.assertFalse(mapping.date_format)
            mapping.crm_field_name = 'x_meta_typed_date'
            self.assertEqual(mapping.date_format, 'dmy')
            # The admin may still clear it.
            mapping.date_format = False
        self.assertFalse(mapping.record.date_format)

    def test_ingest_reads_day_first_by_default(self):
        self._map('start_date', 'x_meta_typed_date')
        with self._patch_graph(fetch_lead=self._date_payload()):
            lead = self.Ingest.ingest_leadgen(self.page, 'LG1', 'manual')
        self.assertEqual(lead.x_meta_typed_date, date(2026, 4, 3))

    def test_ingest_uses_declared_date_format(self):
        self._map('start_date', 'x_meta_typed_date', date_format='dmy')
        with self._patch_graph(fetch_lead=self._date_payload()):
            lead = self.Ingest.ingest_leadgen(self.page, 'LG1', 'manual')
        self.assertEqual(lead.x_meta_typed_date, date(2026, 4, 3))
        self.assertFalse(lead.answer_ids.filtered(
            lambda a: a.question_key == 'start_date'))

    def test_ingest_keeps_slash_date_without_format(self):
        self._map('start_date', 'x_meta_typed_date', date_format=False)
        with self._patch_graph(fetch_lead=self._date_payload()):
            lead = self.Ingest.ingest_leadgen(self.page, 'LG1', 'manual')
        self.assertFalse(lead.x_meta_typed_date)
        kept = {a.question_key: a.value for a in lead.answer_ids}
        self.assertEqual(kept, {'start_date': '03/04/2026'})

    # ---- views --------------------------------------------------------------

    _DATE_ONLY = "crm_field_type not in ('date', 'datetime')"

    def _arch(self, model, xmlid, view_type):
        view = self.env[model].get_view(self.env.ref(xmlid).id, view_type)
        return etree.fromstring(view['arch'])

    def test_mapping_views_show_format_for_dates_only(self):
        for xmlid, view_type in (
                ('meta_lead_ads.meta_field_mapping_view_list', 'list'),
                ('meta_lead_ads.meta_field_mapping_view_form', 'form')):
            with self.subTest(view=xmlid):
                arch = self._arch('meta.field.mapping', xmlid, view_type)
                nodes = arch.xpath("//field[@name='date_format']")
                self.assertEqual(len(nodes), 1)
                self.assertEqual(nodes[0].get('invisible'), self._DATE_ONLY)

    def test_form_view_mapping_list_shows_format_for_dates_only(self):
        arch = self._arch('meta.lead.form',
                          'meta_lead_ads.meta_lead_form_view_form', 'form')
        nodes = arch.xpath(
            "//field[@name='mapping_ids']//list/field[@name='date_format']")
        self.assertEqual(len(nodes), 1)
        self.assertEqual(nodes[0].get('invisible'), self._DATE_ONLY)
        types = arch.xpath(
            "//field[@name='mapping_ids']//list/field[@name='crm_field_type']")
        self.assertEqual(len(types), 1)
        self.assertEqual(types[0].get('column_invisible'), '1')
