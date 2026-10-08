# Meta Lead Ads Integration for Odoo — © 2026 CoreSys Builders. All rights reserved.
# Licensed under the Odoo Proprietary License v1.0 (OPL-1).
# Unauthorized copying, redistribution, or resale of this software, in whole or in
# part, via any medium, is strictly prohibited and constitutes a license violation.
# OPL-1: https://www.odoo.com/documentation/18.0/legal/licenses.html#odoo-apps

from odoo.tests.common import TransactionCase, tagged
from psycopg2 import IntegrityError
from odoo.tools import mute_logger


@tagged('post_install', '-at_install')
class TestMetaLeadDedup(TransactionCase):
    """DB-level uniqueness on crm.lead.meta_leadgen_id."""

    def test_leadgen_id_unique(self):
        self.env['crm.lead'].create({'name': 'Lead A', 'meta_leadgen_id': 'LG1'})
        # The UNIQUE constraint only fires on flush; the savepoint flushes and
        # lets the test transaction carry on after the error. assertRaises
        # needs a single class here, a tuple raises TypeError.
        with self.assertRaises(IntegrityError), mute_logger('odoo.sql_db'):
            with self.env.cr.savepoint():
                self.env['crm.lead'].create({'name': 'Lead B', 'meta_leadgen_id': 'LG1'})

    def test_non_meta_leads_unaffected(self):
        # Postgres treats NULLs as distinct, so ordinary leads never collide.
        l1 = self.env['crm.lead'].create({'name': 'Plain 1'})
        l2 = self.env['crm.lead'].create({'name': 'Plain 2'})
        self.assertTrue(l1 and l2)


@tagged('post_install', '-at_install')
class TestMetaLeadFields(TransactionCase):
    """The meta_* fields on crm.lead: presence, types and m2o settings."""

    def test_meta_fields_present(self):
        fields = self.env['crm.lead']._fields
        for fname in [
            'meta_leadgen_id', 'meta_campaign_id', 'meta_campaign_name',
            'meta_adset_id', 'meta_adset_name', 'meta_ad_id', 'meta_ad_name',
            'meta_form_id', 'meta_form_name', 'meta_page_id', 'meta_page_name',
            'meta_platform', 'meta_form_id_ref', 'meta_page_id_ref', 'meta_account_id']:
            self.assertIn(fname, fields)

        opts = dict(fields['meta_platform'].selection)
        self.assertIn('facebook', opts)
        self.assertIn('instagram', opts)

        self.assertEqual(fields['meta_form_id_ref'].comodel_name, 'meta.lead.form')
        self.assertEqual(fields['meta_page_id_ref'].comodel_name, 'meta.page')
        self.assertEqual(fields['meta_account_id'].comodel_name, 'meta.account')

        # Duplicating a lead must not copy the unique leadgen id.
        self.assertFalse(fields['meta_leadgen_id'].copy)
        # The UNIQUE constraint already creates an index.
        self.assertFalse(fields['meta_leadgen_id'].index)
        # Deleting a page, form or account must not delete its leads.
        for fn in ['meta_form_id_ref', 'meta_page_id_ref', 'meta_account_id']:
            self.assertEqual(fields[fn].ondelete, 'set null')
