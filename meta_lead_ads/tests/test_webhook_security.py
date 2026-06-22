# Meta Lead Ads Integration for Odoo — © 2026 CoreSys Builders. All rights reserved.
# Licensed under the Odoo Proprietary License v1.0 (OPL-1).
# Unauthorized copying, redistribution, or resale of this software, in whole or in
# part, via any medium, is strictly prohibited and constitutes a license violation.
# OPL-1: https://www.odoo.com/documentation/18.0/legal/licenses.html#odoo-apps

"""Test for the meta.webhook.event raw_payload PII gate.

The webhook queue's ``raw_payload`` carries the full Meta lead envelope (PII) and
must be admin-grouped, exactly like ``meta.sync.log.raw_payload``. A non-admin
Meta User who reads it explicitly gets ``AccessError`` -- in Odoo 18 a
group-gated explicit ``read()`` raises rather than silently dropping the key.
"""
from odoo.tests.common import TransactionCase, tagged
from odoo.exceptions import AccessError


@tagged('post_install', '-at_install')
class TestMetaWebhookSecurity(TransactionCase):
    """meta.webhook.event.raw_payload is admin-only; a non-admin explicit read
    raises AccessError."""

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
        # explicit read of an admin-only field raises AccessError in Odoo 18.
        with self.assertRaises(AccessError):
            event.with_user(self.meta_user).read(['raw_payload'])
