# Meta Lead Ads Integration for Odoo — © 2026 CoreSys Builders. All rights reserved.
# Licensed under the Odoo Proprietary License v1.0 (OPL-1).
# Unauthorized copying, redistribution, or resale of this software, in whole or in
# part, via any medium, is strictly prohibited and constitutes a license violation.
# OPL-1: https://www.odoo.com/documentation/19.0/legal/licenses.html#odoo-apps

"""Tests for the retroactive-rename action.

``res.config.settings.action_retro_rename_meta_leads()`` re-titles ONLY Meta leads
that are STILL machine-generated — recognized by the stored ``meta_name_is_generated``
provenance flag (set on create, cleared by any manual ``name`` write), NOT by a
default-shape skeleton. So a lead generated under ANY template is eligible while a
human-edited title — even one shaped like the default — is never clobbered. It never
touches a non-Meta lead, and (a deliberately conservative policy) SKIPS all leads
outright when the ACTIVE template contains ``{date}`` (there is no stored Meta
create-date to trust). It carries an in-method admin gate that raises
``AccessError`` for a non-``group_meta_admin`` user even if that user is
``base.group_system``.

``assertRaises`` blocks are single-statement (a tuple argument TypeErrors).
"""
from unittest import mock

from odoo.tests.common import TransactionCase, tagged
from odoo.exceptions import AccessError

from odoo.addons.meta_lead_ads.models import res_config_settings as _rcs

LEAD_NAME_PARAM = 'meta_lead_ads.lead_name_template'


@tagged('post_install', '-at_install')
class TestRetroRename(TransactionCase):
    def setUp(self):
        super().setUp()
        # Isolate the clobber-safety contract from the {date}-skip policy: start
        # from a {date}-FREE active template.
        self.env['ir.config_parameter'].sudo().set_param(
            LEAD_NAME_PARAM, "Meta Lead • {form_name} • {campaign_name}")
        Lead = self.env['crm.lead']
        # A generated Meta lead whose name still equals the rendered template.
        # meta_name_is_generated=True marks it as a still-machine-generated title.
        self.lead_generated = Lead.create({
            'name': "Meta Lead • Spring • Q2", 'type': 'lead',
            'meta_leadgen_id': 'LG_1', 'meta_name_is_generated': True,
            'meta_form_name': 'Spring', 'meta_campaign_name': 'Q2'})
        # A Meta lead whose name was MANUALLY edited (must never be clobbered):
        # provenance flag False, as a manual name write would have cleared it.
        self.lead_manual = Lead.create({
            'name': "Hot lead — call now", 'type': 'lead',
            'meta_leadgen_id': 'LG_2', 'meta_name_is_generated': False,
            'meta_form_name': 'Spring', 'meta_campaign_name': 'Q2'})
        # A non-Meta lead (no meta_leadgen_id) — must never be touched.
        self.lead_non_meta = Lead.create({
            'name': "Walk-in", 'type': 'lead'})

        base_internal = self.env.ref('base.group_user')
        admin_group = self.env.ref('meta_lead_ads.group_meta_admin')
        self.meta_admin = self.env['res.users'].create({
            'name': 'Retro Admin', 'login': 'retro_admin',
            'group_ids': [(6, 0, [base_internal.id, admin_group.id])]})
        # A system admin who is NOT in group_meta_admin — the gate must still
        # deny this user (base.group_system is not enough).
        self.sys_only = self.env['res.users'].create({
            'name': 'Sys Only', 'login': 'retro_sysonly',
            'group_ids': [(6, 0, [base_internal.id,
                                  self.env.ref('base.group_system').id])]})

    def _retro_as(self, user):
        return (self.env['res.config.settings']
                .with_user(user).create({})
                .action_retro_rename_meta_leads())

    def test_retro_rename_only_generated(self):
        """A new {date}-free template re-titles only the still-generated lead;
        the manually-edited lead and the non-Meta lead are left UNTOUCHED."""
        self.env['ir.config_parameter'].sudo().set_param(
            LEAD_NAME_PARAM, "{form_name} — {campaign_name}")
        self._retro_as(self.meta_admin)
        self.assertEqual(self.lead_generated.name, "Spring — Q2")
        self.assertEqual(self.lead_manual.name, "Hot lead — call now")
        self.assertEqual(self.lead_non_meta.name, "Walk-in")

    def test_retro_rename_batches_every_eligible_lead(self):
        """The bounded-memory batched sweep (security M-02) must re-title EVERY
        eligible lead, including across batch boundaries — no row dropped."""
        Lead = self.env['crm.lead']
        extra = Lead.create([{
            'name': "Meta Lead • Spring • Q2", 'type': 'lead',
            'meta_leadgen_id': 'LG_B%d' % i, 'meta_name_is_generated': True,
            'meta_form_name': 'Spring', 'meta_campaign_name': 'Q2',
        } for i in range(3)])
        self.env['ir.config_parameter'].sudo().set_param(
            LEAD_NAME_PARAM, "{form_name} — {campaign_name}")
        # Force several batches over the 4 eligible leads.
        with mock.patch.object(_rcs, '_RETRO_RENAME_BATCH', 1):
            self._retro_as(self.meta_admin)
        self.assertEqual(self.lead_generated.name, "Spring — Q2")
        for lead in extra:
            self.assertEqual(lead.name, "Spring — Q2")
        # The manual-edit and non-Meta leads are still untouched.
        self.assertEqual(self.lead_manual.name, "Hot lead — call now")
        self.assertEqual(self.lead_non_meta.name, "Walk-in")

    def test_retro_rename_skips_when_template_unchanged(self):
        """Explicit idempotent no-op: when the active template still renders to
        the lead's current name, the action must ``continue`` BEFORE any write.
        Observe it via an unchanged write_date — no needless write."""
        self.lead_generated.flush_recordset()
        before = self.lead_generated.write_date
        self._retro_as(self.meta_admin)
        self.lead_generated.invalidate_recordset()
        self.assertEqual(self.lead_generated.name, "Meta Lead • Spring • Q2")
        self.assertEqual(self.lead_generated.write_date, before)

    def test_retro_rename_skips_date_templates(self):
        """Conservative policy: when the ACTIVE template contains {date},
        retro-rename SKIPS those leads outright — it does NOT trust the
        create_date approximation. The forward/preview path may still use
        create_date, but the retroactive rename refuses to."""
        self.env['ir.config_parameter'].sudo().set_param(
            LEAD_NAME_PARAM, "Meta Lead • {form_name} • {date}")
        self.lead_generated.name = "Whatever a prior template produced"
        self._retro_as(self.meta_admin)
        self.assertEqual(
            self.lead_generated.name, "Whatever a prior template produced")

    def test_retro_rename_non_admin_denied(self):
        """In-method admin gate: a base.group_system user who is NOT in
        group_meta_admin must hit AccessError at the top of the action."""
        with self.assertRaises(AccessError):
            self._retro_as(self.sys_only)

    def test_retro_renames_custom_template_generated(self):
        """A lead generated under a prior CUSTOM template (name NOT in the
        default 'Meta Lead • X • Y' shape) is still re-titled — eligibility is the
        provenance flag, not a default-shape skeleton. A shape-based check would
        silently skip these and report '0 re-titled'."""
        custom = self.env['crm.lead'].create({
            'name': "Spring — Q2", 'type': 'lead', 'meta_leadgen_id': 'LG_C',
            'meta_name_is_generated': True,
            'meta_form_name': 'Spring', 'meta_campaign_name': 'Q2'})
        self.env['ir.config_parameter'].sudo().set_param(
            LEAD_NAME_PARAM, "{form_name} / {campaign_name}")
        self._retro_as(self.meta_admin)
        self.assertEqual(custom.name, "Spring / Q2")

    def test_retro_never_clobbers_default_shaped_manual_edit(self):
        """A manual edit that RETAINS the default 'Meta Lead • X • Y' shape is
        never overwritten — the manual write cleared the provenance flag, so
        retro skips it. A shape-matching regex would have clobbered it."""
        lead = self.env['crm.lead'].create({
            'name': "Meta Lead • Spring • Q2", 'type': 'lead',
            'meta_leadgen_id': 'LG_D', 'meta_name_is_generated': True,
            'meta_form_name': 'Spring', 'meta_campaign_name': 'Q2'})
        # Salesperson manually re-titles to another default-shaped string.
        lead.write({'name': "Meta Lead • urgent • call before 5pm"})
        self.assertFalse(lead.meta_name_is_generated)
        self.env['ir.config_parameter'].sudo().set_param(
            LEAD_NAME_PARAM, "{form_name} — {campaign_name}")
        self._retro_as(self.meta_admin)
        self.assertEqual(lead.name, "Meta Lead • urgent • call before 5pm")

    def test_manual_name_write_clears_generated_flag(self):
        """Provenance flag is cleared by a manual name write, but preserved when a
        write explicitly carries it (the retro rename's own write)."""
        lead = self.env['crm.lead'].create({
            'name': "Meta Lead • Spring • Q2", 'type': 'lead',
            'meta_leadgen_id': 'LG_E', 'meta_name_is_generated': True})
        lead.write({'name': "Manually edited"})
        self.assertFalse(lead.meta_name_is_generated)
        lead.write({'name': "Regenerated", 'meta_name_is_generated': True})
        self.assertTrue(lead.meta_name_is_generated)
