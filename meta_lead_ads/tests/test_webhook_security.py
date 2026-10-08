# Meta Lead Ads Integration for Odoo — © 2026 CoreSys Builders. All rights reserved.
# Licensed under the Odoo Proprietary License v1.0 (OPL-1).
# Unauthorized copying, redistribution, or resale of this software, in whole or in
# part, via any medium, is strictly prohibited and constitutes a license violation.
# OPL-1: https://www.odoo.com/documentation/18.0/legal/licenses.html#odoo-apps

"""Access test for meta.webhook.event.raw_payload.

The queued payload holds the lead's personal data, so like
meta.sync.log.raw_payload it is restricted to Meta admins.
"""
from odoo.tests.common import TransactionCase, tagged
from odoo.exceptions import AccessError


@tagged('post_install', '-at_install')
class TestMetaWebhookSecurity(TransactionCase):
    """A non-admin can't read meta.webhook.event.raw_payload."""

    def setUp(self):
        super().setUp()
        base_internal = self.env.ref('base.group_user')
        user_group = self.env.ref('meta_lead_ads.group_meta_user')
        self.meta_user = self.env['res.users'].create({
            'name': 'Meta U', 'login': 'wh_meta_u',
            'groups_id': [(6, 0, [base_internal.id, user_group.id])]})

    def test_raw_payload_acl(self):
        event = self.env['meta.webhook.event'].create({
            'leadgen_id': 'LG_ACL', 'page_id': 'PG1',
            'raw_payload': '{"pii":"secret"}'})
        # Naming a groups= field in read() raises instead of dropping it.
        with self.assertRaises(AccessError):
            event.with_user(self.meta_user).read(['raw_payload'])
