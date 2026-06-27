# Meta Lead Ads Integration for Odoo — © 2026 CoreSys Builders. All rights reserved.
# Licensed under the Odoo Proprietary License v1.0 (OPL-1).
# Unauthorized copying, redistribution, or resale of this software, in whole or in
# part, via any medium, is strictly prohibited and constitutes a license violation.
# OPL-1: https://www.odoo.com/documentation/19.0/legal/licenses.html#odoo-apps

from odoo.tests.common import TransactionCase, tagged
from psycopg2 import IntegrityError
from odoo.tools import mute_logger


@tagged('post_install', '-at_install')
class TestHierarchy(TransactionCase):
    def test_create_hierarchy(self):
        # multi-account / multi-page / multi-form chain
        acc = self.env['meta.account'].create({'name': 'Acc A', 'account_id': 'A1'})
        acc2 = self.env['meta.account'].create({'name': 'Acc B', 'account_id': 'A2'})
        page = self.env['meta.page'].create({'name': 'Page 1', 'page_id': 'P1', 'account_id': acc.id})
        self.env['meta.page'].create({'name': 'Page 2', 'page_id': 'P2', 'account_id': acc.id})
        form = self.env['meta.lead.form'].create({'name': 'Form 1', 'form_id': 'F1', 'page_id': page.id})
        self.env['meta.field.mapping'].create({
            'form_id': form.id,
            'meta_key': 'email',
            'crm_field_id': self.env['ir.model.fields']._get('crm.lead', 'email_from').id,
        })
        self.assertEqual(acc.page_count, 2)
        self.assertEqual(page.form_count, 1)
        self.assertEqual(form.mapping_count, 1)
        self.assertTrue(acc2)

    def test_account_id_unique(self):
        self.env['meta.account'].create({'name': 'X', 'account_id': 'DUP'})
        with self.assertRaises(IntegrityError), mute_logger('odoo.sql_db'):
            with self.env.cr.savepoint():
                self.env['meta.account'].create({'name': 'Y', 'account_id': 'DUP'})

    def test_page_id_unique(self):
        acc = self.env['meta.account'].create({'name': 'X', 'account_id': 'A'})
        acc2 = self.env['meta.account'].create({'name': 'X2', 'account_id': 'A2'})
        self.env['meta.page'].create({'name': 'P', 'page_id': 'DUP', 'account_id': acc.id})
        # same parent -> rejected
        with self.assertRaises(IntegrityError), mute_logger('odoo.sql_db'):
            with self.env.cr.savepoint():
                self.env['meta.page'].create({'name': 'P2', 'page_id': 'DUP', 'account_id': acc.id})
        # DIFFERENT parent, same page_id -> STILL rejected (proves GLOBAL
        # uniqueness; would PASS under a composite unique(account_id, page_id),
        # so it must be asserted explicitly)
        with self.assertRaises(IntegrityError), mute_logger('odoo.sql_db'):
            with self.env.cr.savepoint():
                self.env['meta.page'].create({'name': 'P3', 'page_id': 'DUP', 'account_id': acc2.id})

    def test_form_id_unique(self):
        acc = self.env['meta.account'].create({'name': 'X', 'account_id': 'A'})
        page = self.env['meta.page'].create({'name': 'P', 'page_id': 'P1', 'account_id': acc.id})
        page2 = self.env['meta.page'].create({'name': 'P2', 'page_id': 'P2', 'account_id': acc.id})
        self.env['meta.lead.form'].create({'name': 'F', 'form_id': 'DUP', 'page_id': page.id})
        # same parent -> rejected
        with self.assertRaises(IntegrityError), mute_logger('odoo.sql_db'):
            with self.env.cr.savepoint():
                self.env['meta.lead.form'].create({'name': 'F2', 'form_id': 'DUP', 'page_id': page.id})
        # DIFFERENT parent, same form_id -> STILL rejected (proves GLOBAL
        # uniqueness)
        with self.assertRaises(IntegrityError), mute_logger('odoo.sql_db'):
            with self.env.cr.savepoint():
                self.env['meta.lead.form'].create({'name': 'F3', 'form_id': 'DUP', 'page_id': page2.id})

    def test_required_fields(self):
        # required=True on account_id maps to a DB NOT NULL, enforced as a
        # psycopg2 IntegrityError. Odoo's TransactionCase.assertRaises accepts
        # a single exception class only (it calls issubclass on the arg), so a
        # tuple cannot be used here — match the sibling uniqueness tests.
        with self.assertRaises(IntegrityError), mute_logger('odoo.sql_db'):
            with self.env.cr.savepoint():
                self.env['meta.account'].create({'name': 'NoId'})
