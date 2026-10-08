# Meta Lead Ads Integration for Odoo — © 2026 CoreSys Builders. All rights reserved.
# Licensed under the Odoo Proprietary License v1.0 (OPL-1).
# Unauthorized copying, redistribution, or resale of this software, in whole or in
# part, via any medium, is strictly prohibited and constitutes a license violation.
# OPL-1: https://www.odoo.com/documentation/18.0/legal/licenses.html#odoo-apps

from odoo.tests.common import TransactionCase, tagged


@tagged('post_install', '-at_install')
class TestSyncLog(TransactionCase):
    """meta.sync.log fields, selection values and defaults."""

    def test_create_and_defaults(self):
        log = self.env['meta.sync.log'].create({
            'meta_leadgen_id': 'LG1', 'trigger': 'webhook'})
        self.assertEqual(log.status, 'pending')
        self.assertEqual(log.retry_count, 0)
        self.assertEqual(log.trigger, 'webhook')

        f = self.env['meta.sync.log']._fields
        for k in ['meta_leadgen_id', 'status', 'trigger', 'retry_count',
                  'error_message', 'match_key', 'lead_id', 'raw_payload']:
            self.assertIn(k, f)

        sopts = dict(f['status'].selection)
        for s in ['pending', 'success', 'skipped_duplicate', 'failed']:
            self.assertIn(s, sopts)

        self.assertEqual(f['lead_id'].comodel_name, 'crm.lead')
        mopts = dict(f['match_key'].selection)
        for m in ['email', 'phone', 'none']:
            self.assertIn(m, mopts)
