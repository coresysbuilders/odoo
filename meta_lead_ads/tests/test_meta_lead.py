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
        # First Meta lead with a leadgen id succeeds.
        self.env['crm.lead'].create({'name': 'Lead A', 'meta_leadgen_id': 'LG1'})
        # A second lead carrying the SAME meta_leadgen_id is rejected at the DB
        # level. Two things to watch for here:
        #   1. assertRaises takes a SINGLE exception class, never a tuple
        #      -- TransactionCase.assertRaises calls issubclass on the arg, so a
        #      tuple raises TypeError instead of catching the IntegrityError.
        #   2. the UNIQUE constraint fires on FLUSH, not on create();
        #      cr.savepoint() forces the flush and isolates the aborted subtxn so
        #      the outer test transaction can continue.
        with self.assertRaises(IntegrityError), mute_logger('odoo.sql_db'):
            with self.env.cr.savepoint():
                self.env['crm.lead'].create({'name': 'Lead B', 'meta_leadgen_id': 'LG1'})

    def test_non_meta_leads_unaffected(self):
        # Two ordinary CRM leads with NO meta_leadgen_id (both NULL) BOTH succeed.
        # Proves the Postgres UNIQUE treats multiple NULLs as distinct, so
        # non-Meta leads are never blocked by the dedup constraint.
        l1 = self.env['crm.lead'].create({'name': 'Plain 1'})
        l2 = self.env['crm.lead'].create({'name': 'Plain 2'})
        self.assertTrue(l1 and l2)


@tagged('post_install', '-at_install')
class TestMetaLeadFields(TransactionCase):
    """The full meta_* field set on crm.lead.

    Locks the schema contract: field presence, types and m2o attributes.
    """

    def test_meta_fields_present(self):
        fields = self.env['crm.lead']._fields
        for fname in [
            'meta_leadgen_id', 'meta_campaign_id', 'meta_campaign_name',
            'meta_adset_id', 'meta_adset_name', 'meta_ad_id', 'meta_ad_name',
            'meta_form_id', 'meta_form_name', 'meta_page_id', 'meta_page_name',
            'meta_platform', 'meta_form_id_ref', 'meta_page_id_ref', 'meta_account_id']:
            self.assertIn(fname, fields)

        # meta_platform is a Selection exposing facebook + instagram.
        opts = dict(fields['meta_platform'].selection)
        self.assertIn('facebook', opts)
        self.assertIn('instagram', opts)

        # The three m2o navigation refs point at the right comodels.
        self.assertEqual(fields['meta_form_id_ref'].comodel_name, 'meta.lead.form')
        self.assertEqual(fields['meta_page_id_ref'].comodel_name, 'meta.page')
        self.assertEqual(fields['meta_account_id'].comodel_name, 'meta.account')

        # copy=False so duplicating a lead in the UI does not carry the unique
        # leadgen id (which would instantly violate the constraint).
        self.assertFalse(fields['meta_leadgen_id'].copy)
        # NO redundant index=True -- the UNIQUE constraint already creates the
        # backing index; a second index would be dead weight.
        self.assertFalse(fields['meta_leadgen_id'].index)
        # m2o navigation links are delete-safe -- deleting a meta.page /
        # meta.lead.form / meta.account must NEVER cascade-delete the lead.
        for fn in ['meta_form_id_ref', 'meta_page_id_ref', 'meta_account_id']:
            self.assertEqual(fields[fn].ondelete, 'set null')
