# Meta Lead Ads Integration for Odoo — © 2026 CoreSys Builders. All rights reserved.
# Licensed under the Odoo Proprietary License v1.0 (OPL-1).
# Unauthorized copying, redistribution, or resale of this software, in whole or in
# part, via any medium, is strictly prohibited and constitutes a license violation.
# OPL-1: https://www.odoo.com/documentation/18.0/legal/licenses.html#odoo-apps

"""Dedup fidelity: country-aware phone keys, exact email matching and how a
submission that matches several leads is resolved and recorded.

The phonenumbers library is optional. Fallback tests patch it away, so they
run everywhere; tests that need the real library skip when it's missing.
"""
import unittest
from contextlib import nullcontext
from unittest import mock

from odoo.tests.common import TransactionCase, tagged
from odoo.tools import SQL

from odoo.addons.meta_lead_ads.models import meta_lead_ingest, phone_key

from .test_ingest import IngestFixtureMixin
from .test_multi_company import MultiCompanyFixtureMixin

key = phone_key.phone_dedup_key


def _fallback():
    return mock.patch.object(phone_key, 'phonenumbers', None)


def _modes():
    """The fallback always, plus the library path when it's installed."""
    modes = [('fallback', _fallback)]
    if phone_key.phonenumbers:
        modes.append(('library', nullcontext))
    return modes


EG = ('EG', 20)
US = ('US', 1)
CA = ('CA', 1)

# Egyptian numbers in local form: mobiles on each operator prefix plus a
# Cairo and an Alexandria landline.
EGYPT_NUMBERS = ['01001234567', '01112345678', '01223456789',
                 '01512345678', '0223456789', '034567890']
NANP_NUMBERS = ['2015550123', '2125550123', '4155550123', '9175550123']


@tagged('post_install', '-at_install')
class TestPhoneKey(TransactionCase):
    """phone_key.phone_dedup_key on its own."""

    def _assert_one_key(self, forms, country, expected=None):
        keys = {form: key(form, *country) for form in forms}
        values = set(keys.values())
        self.assertEqual(len(values), 1, keys)
        value = values.pop()
        self.assertTrue(value, keys)
        if expected is not None:
            self.assertEqual(value, expected)
        return value

    # ---- fallback ---------------------------------------------------------

    def test_fallback_egypt_forms_share_a_key(self):
        with _fallback():
            self._assert_one_key(
                ['01001234567', '+20 100 123 4567', '0100 123 4567',
                 '00201001234567'], EG, '201001234567')

    def test_fallback_without_country(self):
        with _fallback():
            self.assertEqual(key('+20 100 123 4567'), '201001234567')
            # No country: digits only, as before.
            self.assertEqual(key('01001234567'), '01001234567')
            self.assertEqual(key('+1 (555) 123-4567'), '5551234567')
            self.assertEqual(key('5551234567', *US), '5551234567')

    def test_fallback_country_code_without_plus(self):
        with _fallback():
            self.assertEqual(key('201001234567', *EG), '201001234567')

    # ---- both modes ---------------------------------------------------------

    def test_code_without_plus_not_added_twice(self):
        for mode, ctx in _modes():
            with self.subTest(mode=mode), ctx():
                self.assertEqual(key('201001234567', *EG), '201001234567')
                self.assertEqual(key('20 100 123 4567', *EG), '201001234567')
                for country in (US, CA):
                    self._assert_one_key(
                        ['1-555-123-4567', '5551234567', '+1 (555) 123-4567'],
                        country, '5551234567')
                self.assertEqual(key('1-555-123-4567'), '5551234567')
                self.assertEqual(key('5551234567'), '5551234567')

    def test_egypt_collision_table(self):
        for mode, ctx in _modes():
            with ctx():
                found = []
                for local in EGYPT_NUMBERS:
                    national = local[1:]
                    forms = [local, '+20 ' + national, '0020' + national,
                             '20' + national]
                    with self.subTest(mode=mode, number=local):
                        found.append(self._assert_one_key(forms, EG))
                self.assertEqual(len(set(found)), len(EGYPT_NUMBERS), found)

    def test_nanp_collision_table(self):
        for mode, ctx in _modes():
            with ctx():
                found = []
                for bare in NANP_NUMBERS:
                    forms = [bare, '1' + bare, '+1 ' + bare,
                             '(%s) %s-%s' % (bare[:3], bare[3:6], bare[6:])]
                    with self.subTest(mode=mode, number=bare):
                        found.append(self._assert_one_key(forms, US, bare))
                self.assertEqual(len(set(found)), len(NANP_NUMBERS), found)

    def test_known_overlap_nanp_20x_and_egypt(self):
        """Accepted limit (b) of phone_dedup_key: the North American '1'
        strip gives '+1 201 555 0123' the same key as '2015550123' typed
        without '+' for an Egypt company. Pinned so a change is deliberate."""
        for mode, ctx in _modes():
            with self.subTest(mode=mode), ctx():
                self.assertEqual(key('+1 201 555 0123'), '2015550123')
                self.assertEqual(key('2015550123', *EG), '2015550123')

    def test_garbage_never_raises(self):
        junk = [('',), ('   ',), (None,), (12345,), (b'0100',),
                ('abc', 'EG', 20), ('+', 'EG', 20), ('++00--', 'EG', 20),
                ('0', 'EG', 20), ('00', 'EG', 20)]
        for mode, ctx in _modes():
            for args in junk:
                with self.subTest(mode=mode, args=args), ctx():
                    self.assertFalse(key(*args))

    def test_legacy_normalizer_unchanged(self):
        Ingest = self.env['meta.lead.ingest']
        self.assertEqual(Ingest._normalize_phone('+1 (555) 123-4567'),
                         '5551234567')
        self.assertEqual(Ingest._normalize_phone('01001234567'),
                         '01001234567')

    def test_model_wrapper_reads_country(self):
        Ingest = self.env['meta.lead.ingest']
        egypt = self.env.ref('base.eg')
        with _fallback():
            self.assertEqual(Ingest._phone_key('01001234567', egypt),
                             '201001234567')
            self.assertEqual(
                Ingest._phone_key('01001234567', self.env['res.country']),
                '01001234567')

    # ---- library only -------------------------------------------------------

    @unittest.skipUnless(phone_key.phonenumbers, "phonenumbers not installed")
    def test_library_egypt_forms_share_a_key(self):
        self._assert_one_key(
            ['01001234567', '+20 100 123 4567', '0100 123 4567',
             '00201001234567'], EG, '201001234567')

    @unittest.skipUnless(phone_key.phonenumbers, "phonenumbers not installed")
    def test_library_parse_error_falls_back(self):
        error = phone_key.phonenumbers.NumberParseException(0, 'x')
        with mock.patch.object(phone_key.phonenumbers, 'parse',
                               side_effect=error):
            self.assertEqual(key('01001234567', *EG), '201001234567')
            self.assertEqual(key('+20 100 123 4567'), '201001234567')

    @unittest.skipUnless(phone_key.phonenumbers, "phonenumbers not installed")
    def test_library_invalid_number_falls_back(self):
        # 555 numbers are fictional, so the library calls them invalid.
        self.assertEqual(key('+1 (555) 123-4567'), '5551234567')
        self.assertEqual(key('+1 (555) 123-4567', *US), '5551234567')


@tagged('post_install', '-at_install')
class TestDedupFidelity(MultiCompanyFixtureMixin, IngestFixtureMixin,
                        TransactionCase):
    """The stored key and the phone step of the dedup lookup."""

    def setUp(self):
        super().setUp()
        self.egypt = self.env.ref('base.eg')
        self.saudi = self.env.ref('base.sa')
        self.usa = self.env.ref('base.us')
        self.co_a.partner_id.country_id = self.egypt

    def _payload(self, leadgen_id, phone, extra=()):
        field_data = [{'name': 'full_name', 'values': ['Jane Doe']},
                      {'name': 'phone_number', 'values': [phone]}]
        field_data.extend(extra)
        return self._fake_lead(id=leadgen_id, field_data=field_data)

    def _ingest(self, leadgen_id, phone, extra=()):
        with self._patch_graph(fetch_lead=self._payload(
                leadgen_id, phone, extra)):
            return self.Ingest.ingest_leadgen(self.page, leadgen_id, 'manual')

    def _match_key(self, leadgen_id):
        return self.env['meta.sync.log'].sudo().search(
            [('meta_leadgen_id', '=', leadgen_id)]).match_key

    def _seed(self, company, phone, **vals):
        values = {'name': 'Seed', 'type': 'lead', 'user_id': False,
                  'team_id': False, 'phone': phone,
                  'company_id': company.id if company else False}
        values.update(vals)
        return self.env['crm.lead'].create(values)

    def _stored(self, lead):
        """The key as written in the table, not the cached value."""
        self.env.flush_all()
        self.env.cr.execute(
            "SELECT meta_phone_normalized FROM crm_lead WHERE id = %s",
            [lead.id])
        return self.env.cr.fetchone()[0]

    def _map_country(self):
        self.env['meta.field.mapping'].create({
            'form_id': self.form.id, 'meta_key': 'country',
            'crm_field_id': self.env['ir.model.fields']._get(
                'crm.lead', 'country_id').id})

    def _check_egypt_local_matches_international(self):
        seed = self._seed(self.co_a, '+20 100 123 4567')
        self.assertEqual(self._stored(seed), '201001234567')
        lead = self._ingest('LGEG1', '01001234567')
        self.assertEqual(lead, seed)
        self.assertEqual(self._match_key('LGEG1'), 'phone')

    def test_egypt_local_matches_international(self):
        with _fallback():
            self._check_egypt_local_matches_international()

    @unittest.skipUnless(phone_key.phonenumbers, "phonenumbers not installed")
    def test_egypt_local_matches_international_library(self):
        self._check_egypt_local_matches_international()

    def test_lead_country_wins(self):
        with _fallback():
            self._map_country()
            seed = self._seed(self.co_a, '0501234567',
                              country_id=self.saudi.id)
            self.assertEqual(self._stored(seed), '966501234567')
            lead = self._ingest('LGSA1', '0501234567', extra=[
                {'name': 'country', 'values': ['SA']}])
            self.assertEqual(lead, seed)
            self.assertEqual(self._match_key('LGSA1'), 'phone')

    def test_key_follows_phone_country_and_company(self):
        with _fallback():
            self.co_b.partner_id.country_id = self.saudi
            lead = self._seed(self.co_a, '01001234567')
            self.assertEqual(self._stored(lead), '201001234567')
            lead.phone = '01112345678'
            self.assertEqual(self._stored(lead), '201112345678')
            lead.country_id = self.usa
            self.assertEqual(self._stored(lead), '1112345678')
            lead.country_id = False
            self.assertEqual(self._stored(lead), '201112345678')
            lead.company_id = self.co_b
            self.assertEqual(self._stored(lead), '9661112345678')

    def test_company_partner_country_change_recomputes(self):
        """The company's own country field isn't stored, so the depends goes
        through its partner. Changing that country must reach the leads."""
        with _fallback():
            lead = self._seed(self.co_a, '01001234567')
            self.assertEqual(self._stored(lead), '201001234567')
            self.co_a.partner_id.country_id = self.saudi
            self.assertEqual(self._stored(lead), '9661001234567')

    def test_reverse_directions(self):
        cases = [('201001234567', '01001234567'),
                 ('01001234567', '+20 100 123 4567'),
                 ('01001234567', '201001234567')]
        for mode, ctx in _modes():
            for index, (stored, incoming) in enumerate(cases):
                leadgen_id = 'LGREV%s%s' % (mode, index)
                with self.subTest(mode=mode, stored=stored,
                                  incoming=incoming), ctx():
                    seed = self._seed(self.co_a, stored)
                    self.assertEqual(self._ingest(leadgen_id, incoming), seed)
                    self.assertEqual(self._match_key(leadgen_id), 'phone')

    def test_nanp_unchanged(self):
        self.co_a.partner_id.country_id = self.usa
        for mode, ctx in _modes():
            leadgen_id = 'LGUS%s' % mode
            with self.subTest(mode=mode), ctx():
                seed = self._seed(self.co_a, '1-555-123-4567')
                self.assertEqual(self._stored(seed), '5551234567')
                lead = self._ingest(leadgen_id, '+1 (555) 123-4567')
                self.assertEqual(lead, seed)
                self.assertEqual(self._match_key(leadgen_id), 'phone')

    def test_legacy_key_still_matches(self):
        with _fallback():
            seed = self._seed(self.co_a, '01001234567')
            self._stored(seed)
            # A row written before the upgrade recompute.
            self.env.cr.execute(
                "UPDATE crm_lead SET meta_phone_normalized = %s WHERE id = %s",
                ['01001234567', seed.id])
            self.env.invalidate_all()
            lead = self._ingest('LGLEG', '01001234567')
            self.assertEqual(lead, seed)
            self.assertEqual(self._match_key('LGLEG'), 'phone')

    def test_blank_company_lead_keeps_digits(self):
        """A lead with no company and no country has nothing to read its
        number in, so it keeps the digits-only key. A local form from an
        Egypt company still finds it through the legacy value; an
        international form does not."""
        with _fallback():
            seed = self._seed(False, '01001234567')
            self.assertEqual(self._stored(seed), '01001234567')
            lead = self._ingest('LGBL1', '+20 100 123 4567')
            self.assertNotEqual(lead, seed)
            self.assertEqual(self._match_key('LGBL1'), 'none')
            lead = self._ingest('LGBL2', '01001234567')
            self.assertEqual(lead, seed)
            self.assertEqual(self._match_key('LGBL2'), 'phone')

    # ---- email matching and candidate choice ----------------------------

    def _email_lead(self, company, email, phone=False, **vals):
        return self._seed(company, phone, email_from=email, **vals)

    def _find(self, email=None, phone=None, company=None, leadgen_id='LGFIND'):
        vals = {}
        if email:
            vals['email_from'] = email
        if phone:
            vals['phone'] = phone
        return self.Ingest._find_dedup_match(
            self.env['crm.lead'].sudo(), vals,
            self.co_a if company is None else company, leadgen_id=leadgen_id)

    def _set_write_date(self, lead, when):
        # Every row written in one test transaction shares now(), so the
        # dates are set directly.
        self.env.flush_all()
        self.env.cr.execute(
            "UPDATE crm_lead SET write_date = %s WHERE id = %s",
            [when, lead.id])
        self.env.invalidate_all()

    def test_email_wildcards_never_match(self):
        self._email_lead(self.co_a, 'axb@x.com')
        self.assertFalse(self._find('a_b@x.com').lead)
        self._email_lead(self.co_a, '100x@x.com')
        self.assertFalse(self._find('100%@x.com').lead)
        exact = self._email_lead(self.co_a, '100%@x.com')
        self.assertEqual(self._find('100%@x.com').lead, exact)

    def test_email_padding_on_either_side(self):
        seed = self._email_lead(self.co_a, 'jane@x.com')
        found = self._find('  Jane@X.com ')
        self.assertEqual(found.lead, seed)
        self.assertEqual(found.key, 'email')
        padded = self._email_lead(self.co_a, 'pad@x.com')
        self.env.flush_all()
        # The ORM trims Char values, so the padding is written in SQL.
        self.env.cr.execute(
            "UPDATE crm_lead SET email_from = %s WHERE id = %s",
            [' pad@x.com ', padded.id])
        self.env.invalidate_all()
        self.assertEqual(padded.email_from, ' pad@x.com ')
        self.assertEqual(self._find('pad@x.com').lead, padded)

    def test_accent_never_matches_with_unaccent(self):
        seed = self._email_lead(self.co_a, 'jose@x.com')

        def fake(x):
            return SQL("translate(%s, 'éÉ', 'eE')", x)

        with mock.patch.object(self.env.registry, 'unaccent', fake):
            # The fake is live: a plain =ilike now ignores the accent.
            self.assertEqual(self.env['crm.lead'].search(
                [('email_from', '=ilike', 'josé@x.com')]), seed)
            self.assertFalse(self._find('josé@x.com').lead)

    def test_most_recently_updated_wins(self):
        low = self._email_lead(self.co_a, 'same@x.com')
        high = self._email_lead(self.co_a, 'same@x.com')
        self.assertLess(low.id, high.id)
        cases = [('2026-01-02', '2026-01-01', low),
                 ('2026-01-01', '2026-01-02', high),
                 ('2026-01-01', '2026-01-01', high)]
        for low_date, high_date, expected in cases:
            with self.subTest(low=low_date, high=high_date):
                self._set_write_date(low, low_date)
                self._set_write_date(high, high_date)
                found = self._find('same@x.com')
                self.assertEqual(found.lead, expected)
                self.assertEqual(found.others,
                                 [((low | high) - expected, ('email',))])

    def test_candidates_are_labelled_and_ordered(self):
        with _fallback():
            winner = self._email_lead(self.co_a, 'jane@x.com')
            second = self._email_lead(self.co_a, 'jane@x.com')
            both = self._email_lead(self.co_a, 'jane@x.com',
                                    phone='+20 100 123 4567')
            by_phone = self._seed(self.co_a, '+20 100 123 4567')
            old_format = self._seed(self.co_a, '01001234567')
            self._stored(old_format)
            self.env.cr.execute(
                "UPDATE crm_lead SET meta_phone_normalized = %s "
                "WHERE id = %s", ['01001234567', old_format.id])
            for lead, when in [(winner, '2026-01-05'), (second, '2026-01-04'),
                               (both, '2026-01-03'), (by_phone, '2026-01-02'),
                               (old_format, '2026-01-01')]:
                self._set_write_date(lead, when)
            found = self._find('jane@x.com', '01001234567')
            self.assertEqual(found.lead, winner)
            self.assertEqual(found.key, 'email')
            self.assertEqual(found.others, [
                (second, ('email',)),
                (both, ('email', 'phone')),
                (by_phone, ('phone',)),
                (old_format, ('phone, old format',)),
            ])
            self.assertEqual(found.total, 5)
            self.assertEqual(found.total, 1 + len(found.others))
            self.assertFalse(found.truncated)

    def test_email_wins_and_phone_lead_is_listed(self):
        with _fallback():
            by_email = self._email_lead(self.co_a, 'a@x.com')
            by_phone = self._seed(self.co_a, '01001234567')
            found = self._find('a@x.com', '01001234567')
            self.assertEqual(found.lead, by_email)
            self.assertEqual(found.others, [(by_phone, ('phone',))])

    def test_blank_company_pool_comes_after_company(self):
        mine = self._email_lead(self.co_a, 'pool@x.com')
        blank = self._email_lead(False, 'pool@x.com')
        self._set_write_date(mine, '2026-01-01')
        self._set_write_date(blank, '2026-01-09')
        found = self._find('pool@x.com')
        self.assertEqual(found.lead, mine)
        self.assertEqual(found.others, [(blank, ('email',))])

    def test_closed_and_foreign_leads_never_listed(self):
        mine = self._email_lead(self.co_a, 'only@x.com')
        theirs = self._email_lead(self.co_b, 'only@x.com')
        self._email_lead(self.co_a, 'only@x.com', active=False)
        won = self.env['crm.stage'].create({'name': 'Won', 'is_won': True})
        self._email_lead(self.co_a, 'only@x.com', stage_id=won.id)
        taken = self._email_lead(self.co_a, 'only@x.com')
        taken.sudo().meta_leadgen_id = 'LGTAKEN'
        found = self._find('only@x.com')
        self.assertEqual(found.lead, mine)
        self.assertEqual(found.others, [])
        self.assertEqual(found.total, 1)
        # Searching for company B finds only its own lead.
        found_b = self._find('only@x.com', company=self.co_b)
        self.assertEqual(found_b.lead, theirs)
        self.assertEqual(found_b.others, [])

    def test_no_match(self):
        found = self._find('nobody@x.com', '01009999999')
        self.assertFalse(found.lead)
        self.assertIsNone(found.key)
        self.assertEqual(found.others, [])
        self.assertEqual(found.total, 0)
        self.assertFalse(found.truncated)

    def test_capped_search_counts_in_sql(self):
        for _i in range(5):
            self._email_lead(self.co_a, 'shared@x.com')
        with mock.patch.object(meta_lead_ingest, '_DEDUP_CANDIDATE_LIMIT', 3):
            found = self._find('shared@x.com')
        self.assertTrue(found.truncated)
        self.assertEqual(found.total, 5)
        self.assertLessEqual(len(found.others), 3)

    # ---- what a multi-match leaves behind -------------------------------

    def _ingest_contact(self, leadgen_id, email=None, phone=None):
        field_data = [{'name': 'full_name', 'values': ['Jane Doe']}]
        if email:
            field_data.append({'name': 'email', 'values': [email]})
        if phone:
            field_data.append({'name': 'phone_number', 'values': [phone]})
        with self._patch_graph(fetch_lead=self._fake_lead(
                id=leadgen_id, field_data=field_data)):
            return self.Ingest.ingest_leadgen(self.page, leadgen_id, 'manual')

    def _log(self, leadgen_id):
        return self.env['meta.sync.log'].sudo().search(
            [('meta_leadgen_id', '=', leadgen_id)])

    def _merge_notes(self, lead):
        lead.invalidate_recordset()
        return lead.message_ids.filtered(
            lambda m: 'Consider merging' in (m.body or ''))

    def _dated(self, *leads):
        """Give ``leads`` falling write dates, newest first."""
        for day, lead in enumerate(leads):
            self._set_write_date(lead, '2026-02-%02d' % (28 - day))

    def test_single_candidate_logged(self):
        seed = self._email_lead(self.co_a, 'one@x.com')
        self.assertEqual(self._ingest_contact('LGONE', 'one@x.com'), seed)
        log = self._log('LGONE')
        self.assertEqual(log.match_key, 'email')
        self.assertEqual(log.match_candidates, 1)
        self.assertFalse(log.match_candidates_truncated)
        self.assertFalse(log.match_other_ids)
        self.assertFalse(seed.message_ids.filtered(
            lambda m: 'Also matches' in (m.body or '')))

    def test_multi_match_logged_and_noted(self):
        first = self._email_lead(self.co_a, 'three@x.com')
        second = self._email_lead(self.co_a, 'three@x.com')
        third = self._email_lead(self.co_a, 'three@x.com')
        self._dated(first, second, third)
        self.assertEqual(self._ingest_contact('LGTHREE', 'three@x.com'), first)
        log = self._log('LGTHREE')
        self.assertEqual(log.match_candidates, 3)
        self.assertFalse(log.match_candidates_truncated)
        self.assertEqual(log.match_other_ids, '%d (email), %d (email)' % (
            second.id, third.id))
        notes = self._merge_notes(first)
        self.assertEqual(len(notes), 1)
        self.assertEqual(notes.subtype_id, self.env.ref('mail.mt_note'))
        self.assertTrue(notes.is_internal)
        self.assertIn('#%d' % second.id, notes.body)
        self.assertIn('#%d' % third.id, notes.body)
        self.assertIn('merging', notes.body)
        self.assertNotIn('three@x.com', notes.body)
        self.assertFalse(self._merge_notes(second))
        self.assertFalse(self._merge_notes(third))

    def test_other_leads_labelled_on_log(self):
        with _fallback():
            by_email = self._email_lead(self.co_a, 'mix@x.com')
            both = self._email_lead(self.co_a, 'mix@x.com',
                                    phone='01001234567')
            by_phone = self._seed(self.co_a, '+20 100 123 4567')
            old_format = self._seed(self.co_a, '01001234567')
            self._stored(old_format)
            self.env.cr.execute(
                "UPDATE crm_lead SET meta_phone_normalized = %s "
                "WHERE id = %s", ['01001234567', old_format.id])
            self._dated(by_email, both, by_phone, old_format)
            lead = self._ingest_contact('LGMIX', 'mix@x.com', '01001234567')
            self.assertEqual(lead, by_email)
            self.assertEqual(
                self._log('LGMIX').match_other_ids,
                '%d (email, phone), %d (phone), %d (phone, old format)' % (
                    both.id, by_phone.id, old_format.id))

    def test_email_lead_updated_and_phone_lead_listed(self):
        with _fallback():
            lead_a = self._email_lead(self.co_a, 'split@x.com')
            lead_b = self._seed(self.co_a, '01001234567')
            lead = self._ingest_contact('LGSPLIT', 'split@x.com',
                                        '01001234567')
            self.assertEqual(lead, lead_a)
            self.assertEqual(lead_a.meta_leadgen_id, 'LGSPLIT')
            self.assertFalse(lead_b.meta_leadgen_id)
            log = self._log('LGSPLIT')
            self.assertEqual(log.match_key, 'email')
            self.assertEqual(log.match_other_ids, '%d (phone)' % lead_b.id)
            self.assertEqual(log.match_candidates, 2)

    def test_long_list_is_cut(self):
        leads = [self._email_lead(self.co_a, 'many@x.com') for _i in range(12)]
        self._dated(*leads)
        self._ingest_contact('LGMANY', 'many@x.com')
        log = self._log('LGMANY')
        self.assertEqual(log.match_candidates, 12)
        self.assertFalse(log.match_candidates_truncated)
        self.assertTrue(log.match_other_ids.endswith(', and 1 more'))
        listed = log.match_other_ids[:-len(', and 1 more')].split(', ')
        self.assertEqual(listed, ['%d (email)' % lead.id
                                  for lead in leads[1:11]])
        note = self._merge_notes(leads[0])
        self.assertIn('and 1 more', note.body)

    def test_capped_list_says_more(self):
        for _i in range(5):
            self._email_lead(self.co_a, 'cap@x.com')
        with mock.patch.object(meta_lead_ingest, '_DEDUP_CANDIDATE_LIMIT', 3):
            lead = self._ingest_contact('LGCAP', 'cap@x.com')
        log = self._log('LGCAP')
        self.assertEqual(log.match_candidates, 5)
        self.assertTrue(log.match_candidates_truncated)
        self.assertTrue(log.match_other_ids.endswith(
            ', and more (3+ matches)'))
        self.assertIn('3+', self._merge_notes(lead).body)

    def test_failing_note_keeps_lead(self):
        first = self._email_lead(self.co_a, 'fail@x.com')
        second = self._email_lead(self.co_a, 'fail@x.com')
        self._dated(first, second)
        Lead = type(self.env['crm.lead'])
        real_log = Lead._message_log

        def failing_log(record, **kwargs):
            # Field tracking logs through the same method; only the merge
            # note fails.
            if 'Consider merging' in str(kwargs.get('body', '')):
                raise RuntimeError('mail down')
            return real_log(record, **kwargs)

        with mock.patch.object(Lead, '_message_log', failing_log), \
                self.assertLogs('odoo.addons.meta_lead_ads.models.'
                                'meta_lead_ingest', 'WARNING') as logs:
            lead = self._ingest_contact('LGFAIL', 'fail@x.com')
        self.assertEqual(lead, first)
        self.assertEqual(first.meta_leadgen_id, 'LGFAIL')
        log = self._log('LGFAIL')
        self.assertEqual(log.status, 'success')
        self.assertEqual(log.match_candidates, 2)
        output = '\n'.join(logs.output)
        self.assertIn('duplicate note failed for lead id=%d' % first.id,
                      output)
        self.assertNotIn('fail@x.com', output)
        self.assertFalse(self._merge_notes(first))

    def test_outer_rollback_removes_log_and_note(self):
        first = self._email_lead(self.co_a, 'undo@x.com')
        second = self._email_lead(self.co_a, 'undo@x.com')
        self._dated(first, second)

        class _Abort(Exception):
            pass

        try:
            with self.env.cr.savepoint():
                self._ingest_contact('LGUNDO', 'undo@x.com')
                self.env.flush_all()
                # Both exist before the rollback.
                self.assertTrue(self._log('LGUNDO'))
                self.assertTrue(self._merge_notes(first))
                raise _Abort()
        except _Abort:
            pass
        self.env.invalidate_all()
        self.assertFalse(self._log('LGUNDO'))
        self.assertFalse(self._merge_notes(first))
        self.assertFalse(first.meta_leadgen_id)
