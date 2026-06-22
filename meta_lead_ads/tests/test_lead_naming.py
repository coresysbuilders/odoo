# Meta Lead Ads Integration for Odoo — © 2026 CoreSys Builders. All rights reserved.
# Licensed under the Odoo Proprietary License v1.0 (OPL-1).
# Unauthorized copying, redistribution, or resale of this software, in whole or in
# part, via any medium, is strictly prohibited and constitutes a license violation.
# OPL-1: https://www.odoo.com/documentation/18.0/legal/licenses.html#odoo-apps

"""Tests for the lead-name render helper and Settings round-trip.

These tests pin the contracts:
  - ``meta.lead.ingest.render_lead_name(template, token_map)`` — drop-empty +
    tidy-separator, separator-agnostic, never-empty, never-raises.
  - ``meta.lead.ingest._validate_lead_name_template(template)`` — raises
    ``ValidationError`` on unknown OR malformed-brace tokens.
  - ``res.config.settings.lead_name_template`` get/set round-trip through
    ``ir.config_parameter`` with an in-method admin gate on ``set_values``.
  - the wired create path: ``ingest_leadgen`` titles a created ``crm.lead`` via the
    configured template (default byte-for-byte + custom) without introducing a
    second create path.
  - the defensive create path: a junk ICP value written outside Settings must NOT
    drop a paid lead — render falls back, the lead is still created.

The public naming constants are imported from ``models/const.py`` so the U+2022
bullet stays byte-exact and is never retyped.

The ``assertRaises`` blocks are single-statement (a tuple argument TypeErrors).
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
    """Pure render-helper + validator + Settings round-trip contracts."""

    def setUp(self):
        super().setUp()
        self.Ingest = self.env['meta.lead.ingest']
        # set_values + the retro action carry an in-method group_meta_admin gate.
        # TransactionCase runs as the SUPERUSER (uid=1), who is NOT a Meta admin,
        # so the Settings round-trip must be driven as an explicit group_meta_admin
        # user (mirrors TestRetroRename / TestSecurity). group_meta_admin has the
        # res.config.settings ACL (security/ir.model.access.csv) so create works.
        base_internal = self.env.ref('base.group_user')
        admin_group = self.env.ref('meta_lead_ads.group_meta_admin')
        self.meta_admin = self.env['res.users'].create({
            'name': 'Naming Admin', 'login': 'naming_admin',
            'groups_id': [(6, 0, [base_internal.id, admin_group.id])]})

    # ---- render helper edge cases (drop-empty + tidy, sep-agnostic) --------

    def test_render_cases(self):
        """Table-driven render contract. Each tuple pins the behavior: a token
        that resolves empty vanishes AND consumes its dangling separator;
        leading/trailing separators are trimmed; doubles collapse; the result is
        never empty; real (non-separator) literals are preserved; and a
        legitimate value that itself contains a separator is NOT corrupted."""
        cases = [
            # (1) default with full form_name+date → exact (byte-exact bullet).
            (DEFAULT_LEAD_NAME,
             {'form_name': 'Spring', 'date': '2026-06-18'},
             "Meta Lead • Spring • 2026-06-18"),
            # (2) empty {form_name} in the middle of the default → drop + tidy.
            (DEFAULT_LEAD_NAME,
             {'form_name': '', 'date': '2026-06-18'},
             "Meta Lead • 2026-06-18"),
            # (3) empty leading token → leading separator dropped.
            ("{form_name} • {campaign_name}",
             {'form_name': '', 'campaign_name': 'Q2'},
             "Q2"),
            # (4) empty trailing token → trailing separator dropped.
            ("{form_name} • {campaign_name}",
             {'form_name': 'Spring', 'campaign_name': ''},
             "Spring"),
            # (5) all tokens empty → never-empty fallback.
            ("{form_name} • {adset_name} • {ad_name}", {}, "Meta Lead"),
            # (6) NON-• separator (/), one/two/three empty → sep-agnostic.
            ("{campaign_name} / {adset_name} / {ad_name}",
             {'campaign_name': 'Q2', 'adset_name': '', 'ad_name': 'Carousel'},
             "Q2 / Carousel"),
            ("{campaign_name} / {adset_name} / {ad_name}",
             {'campaign_name': 'Q2', 'adset_name': '', 'ad_name': ''},
             "Q2"),
            ("{campaign_name} / {adset_name} / {ad_name}",
             {'campaign_name': '', 'adset_name': '', 'ad_name': ''},
             "Meta Lead"),
            # (7) real trailing literal preserved (NOT a dangling separator).
            ("Lead from {form_name}",
             {'form_name': ''},
             "Lead from"),
            # (8) token VALUE contains a • — the tidy regex must NOT corrupt it.
            ("{form_name}",
             {'form_name': 'A • B'},
             "A • B"),
            # (9) token VALUE contains a / — preserved verbatim.
            ("{form_name} / {campaign_name}",
             {'form_name': 'A/B', 'campaign_name': 'Q2'},
             "A/B / Q2"),
        ]
        for tmpl, tm, expected in cases:
            self.assertEqual(
                self.Ingest.render_lead_name(tmpl, tm), expected,
                "template=%r map=%r" % (tmpl, tm))

    def test_default_matches_old_title(self):
        """The default template must reproduce the previous f-string title
        byte-for-byte. Build the expected via ``%``-format so any drift in
        DEFAULT_LEAD_NAME (especially the U+2022 bullet) fails the test."""
        expected = "Meta Lead • %s • %s" % ('X', '2026-06-18')
        rendered = self.Ingest.render_lead_name(
            DEFAULT_LEAD_NAME, {'form_name': 'X', 'date': '2026-06-18'})
        self.assertEqual(rendered, expected)

    # ---- Settings get/set round-trip ---------------------------------------

    def test_settings_roundtrip(self):
        """A template saved through ``res.config.settings.set_values`` lands in
        ``ir.config_parameter`` under LEAD_NAME_PARAM and reads back through
        ``get_values``. ``set_values`` now carries an in-method admin gate, so
        run it as the env admin (a member of group_meta_admin)."""
        Settings = self.env['res.config.settings'].with_user(self.meta_admin)
        s = Settings.create({'lead_name_template': "X {form_name}"})
        s.set_values()
        param = self.env['ir.config_parameter'].sudo().get_param(LEAD_NAME_PARAM)
        self.assertEqual(param, "X {form_name}")
        self.assertEqual(
            Settings.new({}).get_values()['lead_name_template'], "X {form_name}")

    # ---- unknown-token + malformed-brace rejection -------------------------

    def test_unknown_token_rejected(self):
        """A bare \\w+ token not in VALID_TOKENS is rejected at ``set_values``
        BEFORE the param write (validate before persisting)."""
        s = self.env['res.config.settings'].with_user(self.meta_admin).create(
            {'lead_name_template': "Meta {foo} Lead"})
        with self.assertRaises(ValidationError):
            s.set_values()

    def test_malformed_brace_rejected(self):
        """Pin the malformed-brace policy:
          - bare \\w+ inside single braces NOT in VALID_TOKENS → reject;
          - non-\\w brace content (``-``, space, empty) is an INVALID token →
            reject (not silently passed through);
          - doubled braces ``{{...}}`` are an ESCAPED LITERAL → do NOT raise.
        VALID_TOKENS anchors what is legal; everything else braced is rejected.
        """
        for tmpl in ("{foo-bar}", "{}", "{form bar}"):
            with self.assertRaises(ValidationError):
                self.Ingest._validate_lead_name_template(tmpl)
        # Positive case: a real token plus an escaped {{literal}} must NOT raise.
        self.assertIsNone(
            self.Ingest._validate_lead_name_template(
                "Lead {form_name} {{literal}}"))

    # ---- wired create-path naming ------------------------------------------


@tagged('post_install', '-at_install')
class TestIngestNaming(IngestFixtureMixin, TransactionCase):
    """The configured template must drive the title of a REAL created lead
    through the single ``ingest_leadgen`` / ``_create_lead`` path — no second
    create path is introduced."""

    def test_ingest_naming_default_byte_for_byte(self):
        """With LEAD_NAME_PARAM UNSET, a created lead's name equals the previous
        byte-for-byte title for that fixture's form_name + created_time day
        (asserted at the create boundary, not just the helper)."""
        # Ensure the param is unset → default path.
        self.env['ir.config_parameter'].sudo().set_param(LEAD_NAME_PARAM, '')
        payload = self._fake_lead(form_name='Contact Us')
        with self._patch_graph(fetch_lead=payload):
            lead = self.Ingest.ingest_leadgen(self.page, 'LG1', 'manual')
        day = payload['created_time'][:10]
        expected = "Meta Lead • %s • %s" % ('Contact Us', day)
        self.assertEqual(lead.name, expected)
        # The create path stamps the title-provenance flag.
        self.assertTrue(lead.meta_name_is_generated)

    def test_ingest_naming_custom_template(self):
        """A custom template set via ir.config_parameter drives the created
        lead's title through the same single create path."""
        self.env['ir.config_parameter'].sudo().set_param(
            LEAD_NAME_PARAM, "{form_name}")
        payload = self._fake_lead(form_name='Contact Us')
        with self._patch_graph(fetch_lead=payload):
            lead = self.Ingest.ingest_leadgen(self.page, 'LG1', 'manual')
        self.assertEqual(lead.name, 'Contact Us')

    def test_ingest_defensive_junk_param(self):
        """A GARBAGE template written directly via set_param (bypassing Settings
        validation) must NOT drop a paid lead: the create path renders
        DEFENSIVELY (it must NOT call the hard validator), so the lead is still
        created with a non-empty name — a paid lead is never dropped by a bad
        ICP value written outside Settings."""
        self.env['ir.config_parameter'].sudo().set_param(
            LEAD_NAME_PARAM, "{foo-bar}")
        payload = self._fake_lead(form_name='Contact Us')
        with self._patch_graph(fetch_lead=payload):
            lead = self.Ingest.ingest_leadgen(self.page, 'LG1', 'manual')
        self.assertTrue(lead)
        self.assertTrue(lead.name)


@tagged('post_install', '-at_install')
class TestConnectionStatus(TransactionCase):
    """The Settings connection badge distinguishes 'no active account' from
    'an active account that simply has not been token-tested yet'."""

    def _status(self):
        return self.env['res.config.settings'].create({}).meta_connection_status

    def test_status_none_when_no_account(self):
        self.assertEqual(self._status(), 'none')

    def test_status_untested_when_account_present_but_not_token_tested(self):
        # access_status is nullable until a token test runs — must NOT read 'none'.
        self.env['meta.account'].create({'name': 'Acme', 'account_id': 'ACT1'})
        self.assertEqual(self._status(), 'untested')

    def test_status_reflects_best_tested_account(self):
        self.env['meta.account'].create({
            'name': 'Acme', 'account_id': 'ACT2',
            'access_status': 'lead_retrieval_granted'})
        self.assertEqual(self._status(), 'lead_retrieval_granted')
