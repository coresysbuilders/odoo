# Meta Lead Ads Integration for Odoo — © 2026 CoreSys Builders. All rights reserved.
# Licensed under the Odoo Proprietary License v1.0 (OPL-1).
# Unauthorized copying, redistribution, or resale of this software, in whole or in
# part, via any medium, is strictly prohibited and constitutes a license violation.
# OPL-1: https://www.odoo.com/documentation/19.0/legal/licenses.html#odoo-apps

"""Tests for ``action_retro_rename_meta_leads``.

Only Meta leads whose title is still machine-generated get renamed. That is
decided by the ``meta_name_is_generated`` flag (set on create, cleared by a
manual rename), not by matching the title's shape. Non-Meta leads are never
touched. If the active template uses ``{date}``, only leads with a stored
Meta submission time are renamed, since create_date isn't a reliable stand-in.
Only group_meta_admin may run it; base.group_system alone is not enough.
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
        # Start from a template without {date} so the {date} rule doesn't
        # interfere with the other tests.
        self.env['ir.config_parameter'].sudo().set_param(
            LEAD_NAME_PARAM, "Meta Lead • {form_name} • {campaign_name}")
        Lead = self.env['crm.lead']
        # Generated Meta lead whose name still matches the template.
        self.lead_generated = Lead.create({
            'name': "Meta Lead • Spring • Q2", 'type': 'lead',
            'meta_leadgen_id': 'LG_1', 'meta_name_is_generated': True,
            'meta_form_name': 'Spring', 'meta_campaign_name': 'Q2'})
        # Meta lead renamed by hand, so the flag is off.
        self.lead_manual = Lead.create({
            'name': "Hot lead — call now", 'type': 'lead',
            'meta_leadgen_id': 'LG_2', 'meta_name_is_generated': False,
            'meta_form_name': 'Spring', 'meta_campaign_name': 'Q2'})
        # Not a Meta lead.
        self.lead_non_meta = Lead.create({
            'name': "Walk-in", 'type': 'lead'})

        base_internal = self.env.ref('base.group_user')
        admin_group = self.env.ref('meta_lead_ads.group_meta_admin')
        self.meta_admin = self.env['res.users'].create({
            'name': 'Retro Admin', 'login': 'retro_admin',
            'group_ids': [(6, 0, [base_internal.id, admin_group.id])]})
        # Settings admin without group_meta_admin; must be refused.
        self.sys_only = self.env['res.users'].create({
            'name': 'Sys Only', 'login': 'retro_sysonly',
            'group_ids': [(6, 0, [base_internal.id,
                                  self.env.ref('base.group_system').id])]})

    def _retro_as(self, user):
        return (self.env['res.config.settings']
                .with_user(user).create({})
                .action_retro_rename_meta_leads())

    def test_retro_rename_only_generated(self):
        """Only the still-generated lead is renamed; the hand-edited and
        non-Meta leads keep their names."""
        self.env['ir.config_parameter'].sudo().set_param(
            LEAD_NAME_PARAM, "{form_name} — {campaign_name}")
        self._retro_as(self.meta_admin)
        self.assertEqual(self.lead_generated.name, "Spring — Q2")
        self.assertEqual(self.lead_manual.name, "Hot lead — call now")
        self.assertEqual(self.lead_non_meta.name, "Walk-in")

    def test_retro_rename_batches_every_eligible_lead(self):
        """The batched sweep renames every eligible lead across batch
        boundaries."""
        Lead = self.env['crm.lead']
        extra = Lead.create([{
            'name': "Meta Lead • Spring • Q2", 'type': 'lead',
            'meta_leadgen_id': 'LG_B%d' % i, 'meta_name_is_generated': True,
            'meta_form_name': 'Spring', 'meta_campaign_name': 'Q2',
        } for i in range(3)])
        self.env['ir.config_parameter'].sudo().set_param(
            LEAD_NAME_PARAM, "{form_name} — {campaign_name}")
        # Batch size 1 forces four batches.
        with mock.patch.object(_rcs, '_RETRO_RENAME_BATCH', 1):
            self._retro_as(self.meta_admin)
        self.assertEqual(self.lead_generated.name, "Spring — Q2")
        for lead in extra:
            self.assertEqual(lead.name, "Spring — Q2")
        self.assertEqual(self.lead_manual.name, "Hot lead — call now")
        self.assertEqual(self.lead_non_meta.name, "Walk-in")

    def test_retro_rename_skips_when_template_unchanged(self):
        """A lead whose name already matches the template isn't written to
        (write_date stays the same)."""
        self.lead_generated.flush_recordset()
        before = self.lead_generated.write_date
        self._retro_as(self.meta_admin)
        self.lead_generated.invalidate_recordset()
        self.assertEqual(self.lead_generated.name, "Meta Lead • Spring • Q2")
        self.assertEqual(self.lead_generated.write_date, before)

    def test_retro_rename_skips_date_templates(self):
        """With a {date} template, a lead with no stored submission time is
        skipped rather than dated from create_date."""
        self.env['ir.config_parameter'].sudo().set_param(
            LEAD_NAME_PARAM, "Meta Lead • {form_name} • {date}")
        self.lead_generated.name = "Whatever a prior template produced"
        self._retro_as(self.meta_admin)
        self.assertEqual(
            self.lead_generated.name, "Whatever a prior template produced")

    def test_retro_rename_date_template_uses_stored_submission(self):
        """With a {date} template, leads with a stored submission time are
        renamed using that date; the others are skipped."""
        dated = self.env['crm.lead'].create({
            'name': "old title", 'type': 'lead', 'meta_leadgen_id': 'LG_D',
            'meta_name_is_generated': True, 'meta_form_name': 'Spring',
            'meta_submitted_at': '2026-03-05 23:10:00'})
        self.env['ir.config_parameter'].sudo().set_param(
            LEAD_NAME_PARAM, "Meta Lead • {form_name} • {date}")
        self.lead_generated.name = "Whatever a prior template produced"
        self.lead_generated.meta_name_is_generated = True
        self._retro_as(self.meta_admin)
        self.assertEqual(dated.name, "Meta Lead • Spring • 2026-03-05")
        self.assertEqual(
            self.lead_generated.name, "Whatever a prior template produced")

    def test_retro_rename_non_admin_denied(self):
        """A base.group_system user outside group_meta_admin gets AccessError."""
        with self.assertRaises(AccessError):
            self._retro_as(self.sys_only)

    def test_retro_renames_custom_template_generated(self):
        """A lead generated under an earlier custom template is still renamed,
        because eligibility comes from the flag and not the title's shape."""
        custom = self.env['crm.lead'].create({
            'name': "Spring — Q2", 'type': 'lead', 'meta_leadgen_id': 'LG_C',
            'meta_name_is_generated': True,
            'meta_form_name': 'Spring', 'meta_campaign_name': 'Q2'})
        self.env['ir.config_parameter'].sudo().set_param(
            LEAD_NAME_PARAM, "{form_name} / {campaign_name}")
        self._retro_as(self.meta_admin)
        self.assertEqual(custom.name, "Spring / Q2")

    def test_retro_never_clobbers_default_shaped_manual_edit(self):
        """A hand-edited title that happens to look like the default is left
        alone, since the manual write cleared the flag."""
        lead = self.env['crm.lead'].create({
            'name': "Meta Lead • Spring • Q2", 'type': 'lead',
            'meta_leadgen_id': 'LG_D', 'meta_name_is_generated': True,
            'meta_form_name': 'Spring', 'meta_campaign_name': 'Q2'})
        # Salesperson retitles it to something in the default shape.
        lead.write({'name': "Meta Lead • urgent • call before 5pm"})
        self.assertFalse(lead.meta_name_is_generated)
        self.env['ir.config_parameter'].sudo().set_param(
            LEAD_NAME_PARAM, "{form_name} — {campaign_name}")
        self._retro_as(self.meta_admin)
        self.assertEqual(lead.name, "Meta Lead • urgent • call before 5pm")

    def test_manual_name_write_clears_generated_flag(self):
        """A plain name write clears the flag; a write that sets the flag
        explicitly (as the rename does) keeps it."""
        lead = self.env['crm.lead'].create({
            'name': "Meta Lead • Spring • Q2", 'type': 'lead',
            'meta_leadgen_id': 'LG_E', 'meta_name_is_generated': True})
        lead.write({'name': "Manually edited"})
        self.assertFalse(lead.meta_name_is_generated)
        lead.write({'name': "Regenerated", 'meta_name_is_generated': True})
        self.assertTrue(lead.meta_name_is_generated)
