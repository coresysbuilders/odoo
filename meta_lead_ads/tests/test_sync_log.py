# Meta Lead Ads Integration for Odoo — © 2026 CoreSys Builders. All rights reserved.
# Licensed under the Odoo Proprietary License v1.0 (OPL-1).
# Unauthorized copying, redistribution, or resale of this software, in whole or in
# part, via any medium, is strictly prohibited and constitutes a license violation.
# OPL-1: https://www.odoo.com/documentation/19.0/legal/licenses.html#odoo-apps

from odoo.tests.common import TransactionCase, tagged


@tagged('post_install', '-at_install')
class TestSyncLog(TransactionCase):
    """meta.sync.log model shape + defaults.

    Locks the field set, Selection vocabularies and the pending/0 defaults.
    """

    def test_create_and_defaults(self):
        log = self.env['meta.sync.log'].create({
            'meta_leadgen_id': 'LG1', 'trigger': 'webhook'})
        self.assertEqual(log.status, 'pending')      # default status
        self.assertEqual(log.retry_count, 0)         # default retry count
        self.assertEqual(log.trigger, 'webhook')

        # All expected field keys exist on the model.
        f = self.env['meta.sync.log']._fields
        for k in ['meta_leadgen_id', 'status', 'trigger', 'retry_count',
                  'error_message', 'match_key', 'lead_id', 'raw_payload']:
            self.assertIn(k, f)

        # status vocabulary.
        sopts = dict(f['status'].selection)
        for s in ['pending', 'success', 'skipped_duplicate', 'failed']:
            self.assertIn(s, sopts)

        # lead_id m2o comodel + match_key Selection vocabulary.
        self.assertEqual(f['lead_id'].comodel_name, 'crm.lead')
        mopts = dict(f['match_key'].selection)
        for m in ['email', 'phone', 'none']:
            self.assertIn(m, mopts)
