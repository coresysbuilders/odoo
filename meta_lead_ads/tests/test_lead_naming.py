# Meta Lead Ads Integration for Odoo — © 2026 CoreSys Builders. All rights reserved.
# Licensed under the Odoo Proprietary License v1.0 (OPL-1).
# Unauthorized copying, redistribution, or resale of this software, in whole or in
# part, via any medium, is strictly prohibited and constitutes a license violation.
# OPL-1: https://www.odoo.com/documentation/19.0/legal/licenses.html#odoo-apps

"""Tests for lead-name templates.

Covers ``render_lead_name`` (empty tokens and their separators drop out, the
result is never empty), ``_validate_lead_name_template``, the Settings
round-trip, and the title ingest gives a real lead, including when the stored
template is junk.

The naming constants are imported from models/const.py so the bullet in the
default template is never retyped by hand.
"""
from unittest import mock

from odoo.tests.common import TransactionCase, tagged
from odoo.exceptions import ValidationError

from odoo.addons.meta_lead_ads.models.const import (
    DEFAULT_LEAD_NAME, LEAD_NAME_PARAM, VALID_TOKENS,
)

from .test_ingest import IngestFixtureMixin


@tagged('post_install', '-at_install')
class TestLeadNameRender(TransactionCase):
    """Render helper, validator and Settings round-trip."""

    def setUp(self):
        super().setUp()
        self.Ingest = self.env['meta.lead.ingest']
        # set_values checks group_meta_admin itself, and the test superuser
        # isn't in that group, so use a dedicated Meta admin.
        base_internal = self.env.ref('base.group_user')
        admin_group = self.env.ref('meta_lead_ads.group_meta_admin')
        self.meta_admin = self.env['res.users'].create({
            'name': 'Naming Admin', 'login': 'naming_admin',
            'group_ids': [(6, 0, [base_internal.id, admin_group.id])]})

    # ---- render helper ------------------------------------------------------

    def test_render_cases(self):
        """Empty tokens drop out with their separator, any separator works,
        and values that contain a separator are left alone."""
        cases = [
            # (1) default template, all values present.
            (DEFAULT_LEAD_NAME,
             {'form_name': 'Spring', 'date': '2026-06-18'},
             "Meta Lead • Spring • 2026-06-18"),
            # (2) empty token in the middle.
            (DEFAULT_LEAD_NAME,
             {'form_name': '', 'date': '2026-06-18'},
             "Meta Lead • 2026-06-18"),
            # (3) empty leading token.
            ("{form_name} • {campaign_name}",
             {'form_name': '', 'campaign_name': 'Q2'},
             "Q2"),
            # (4) empty trailing token.
            ("{form_name} • {campaign_name}",
             {'form_name': 'Spring', 'campaign_name': ''},
             "Spring"),
            # (5) everything empty falls back to "Meta Lead".
            ("{form_name} • {adset_name} • {ad_name}", {}, "Meta Lead"),
            # (6) slash separator with one, two and three empty tokens.
            ("{campaign_name} / {adset_name} / {ad_name}",
             {'campaign_name': 'Q2', 'adset_name': '', 'ad_name': 'Carousel'},
             "Q2 / Carousel"),
            ("{campaign_name} / {adset_name} / {ad_name}",
             {'campaign_name': 'Q2', 'adset_name': '', 'ad_name': ''},
             "Q2"),
            ("{campaign_name} / {adset_name} / {ad_name}",
             {'campaign_name': '', 'adset_name': '', 'ad_name': ''},
             "Meta Lead"),
            # (7) plain literal text is kept.
            ("Lead from {form_name}",
             {'form_name': ''},
             "Lead from"),
            # (8) a value containing the bullet is not touched by the tidy-up.
            ("{form_name}",
             {'form_name': 'A • B'},
             "A • B"),
            # (9) same for a slash inside a value.
            ("{form_name} / {campaign_name}",
             {'form_name': 'A/B', 'campaign_name': 'Q2'},
             "A/B / Q2"),
        ]
        for tmpl, tm, expected in cases:
            self.assertEqual(
                self.Ingest.render_lead_name(tmpl, tm), expected,
                "template=%r map=%r" % (tmpl, tm))

    def test_default_matches_old_title(self):
        """The default template reproduces the old hard-coded title exactly."""
        expected = "Meta Lead • %s • %s" % ('X', '2026-06-18')
        rendered = self.Ingest.render_lead_name(
            DEFAULT_LEAD_NAME, {'form_name': 'X', 'date': '2026-06-18'})
        self.assertEqual(rendered, expected)

    # ---- Settings round-trip -------------------------------------------------

    def test_settings_roundtrip(self):
        """A template saved in Settings lands in ir.config_parameter and reads
        back through get_values."""
        Settings = self.env['res.config.settings'].with_user(self.meta_admin)
        s = Settings.create({'lead_name_template': "X {form_name}"})
        s.set_values()
        param = self.env['ir.config_parameter'].sudo().get_param(LEAD_NAME_PARAM)
        self.assertEqual(param, "X {form_name}")
        self.assertEqual(
            Settings.new({}).get_values()['lead_name_template'], "X {form_name}")

    # ---- unknown and malformed tokens ----------------------------------------

    def test_unknown_token_rejected(self):
        """An unknown token is rejected by set_values before anything is saved."""
        s = self.env['res.config.settings'].with_user(self.meta_admin).create(
            {'lead_name_template': "Meta {foo} Lead"})
        with self.assertRaises(ValidationError):
            s.set_values()

    def test_malformed_brace_rejected(self):
        """Malformed braces are rejected; doubled braces are an escaped literal.

        Anything in single braces that isn't in VALID_TOKENS fails, including
        ``{}``, ``{foo-bar}`` and ``{form bar}``. ``{{literal}}`` is allowed.
        """
        for tmpl in ("{foo-bar}", "{}", "{form bar}"):
            with self.assertRaises(ValidationError):
                self.Ingest._validate_lead_name_template(tmpl)
        self.assertIsNone(
            self.Ingest._validate_lead_name_template(
                "Lead {form_name} {{literal}}"))

    # ---- naming through ingest -------------------------------------------------


@tagged('post_install', '-at_install')
class TestIngestNaming(IngestFixtureMixin, TransactionCase):
    """The configured template titles leads created by ingest_leadgen."""

    def test_ingest_naming_default_byte_for_byte(self):
        """With no template set, a new lead gets the old default title."""
        self.env['ir.config_parameter'].sudo().set_param(LEAD_NAME_PARAM, '')
        payload = self._fake_lead(form_name='Contact Us')
        with self._patch_graph(fetch_lead=payload):
            lead = self.Ingest.ingest_leadgen(self.page, 'LG1', 'manual')
        day = payload['created_time'][:10]
        expected = "Meta Lead • %s • %s" % ('Contact Us', day)
        self.assertEqual(lead.name, expected)
        self.assertTrue(lead.meta_name_is_generated)

    def test_ingest_naming_custom_template(self):
        """A custom template from ir.config_parameter is used for the title."""
        self.env['ir.config_parameter'].sudo().set_param(
            LEAD_NAME_PARAM, "{form_name}")
        payload = self._fake_lead(form_name='Contact Us')
        with self._patch_graph(fetch_lead=payload):
            lead = self.Ingest.ingest_leadgen(self.page, 'LG1', 'manual')
        self.assertEqual(lead.name, 'Contact Us')

    def test_ingest_defensive_junk_param(self):
        """A junk template written straight to ir.config_parameter doesn't stop
        the lead being created; ingest falls back instead of validating."""
        self.env['ir.config_parameter'].sudo().set_param(
            LEAD_NAME_PARAM, "{foo-bar}")
        payload = self._fake_lead(form_name='Contact Us')
        with self._patch_graph(fetch_lead=payload):
            lead = self.Ingest.ingest_leadgen(self.page, 'LG1', 'manual')
        self.assertTrue(lead)
        self.assertTrue(lead.name)


@tagged('post_install', '-at_install')
class TestConnectionStatus(TransactionCase):
    """The Settings badge tells 'no account' apart from 'account not tested
    yet'."""

    def _status(self):
        return self.env['res.config.settings'].create({}).meta_connection_status

    def test_status_none_when_no_account(self):
        self.assertEqual(self._status(), 'none')

    def test_status_untested_when_account_present_but_not_token_tested(self):
        # access_status stays empty until a token test runs.
        self.env['meta.account'].create({'name': 'Acme', 'account_id': 'ACT1'})
        self.assertEqual(self._status(), 'untested')

    def test_status_reflects_best_tested_account(self):
        self.env['meta.account'].create({
            'name': 'Acme', 'account_id': 'ACT2',
            'access_status': 'lead_retrieval_granted'})
        self.assertEqual(self._status(), 'lead_retrieval_granted')
