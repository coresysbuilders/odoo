# Meta Lead Ads Integration for Odoo — © 2026 CoreSys Builders. All rights reserved.
# Licensed under the Odoo Proprietary License v1.0 (OPL-1).
# Unauthorized copying, redistribution, or resale of this software, in whole or in
# part, via any medium, is strictly prohibited and constitutes a license violation.
# OPL-1: https://www.odoo.com/documentation/18.0/legal/licenses.html#odoo-apps

from odoo.tests.common import TransactionCase, tagged
from odoo.exceptions import AccessError


@tagged('post_install', '-at_install')
class TestSyncLogSecurity(TransactionCase):
    """meta.sync.log access rights.

    Internal and CRM users can see log rows; raw_payload holds personal data
    and is admin-only.
    """

    def setUp(self):
        super().setUp()
        self.user_group = self.env.ref('meta_lead_ads.group_meta_user')
        self.admin_group = self.env.ref('meta_lead_ads.group_meta_admin')
        # Without base.group_user Odoo makes these share (portal) users and
        # the ACL tests can pass for the wrong reason. group_meta_admin
        # already implies group_meta_user.
        base_internal = self.env.ref('base.group_user')
        self.meta_user = self.env['res.users'].create({
            'name': 'Meta U', 'login': 'meta_u',
            'groups_id': [(6, 0, [base_internal.id, self.user_group.id])]})
        self.meta_admin = self.env['res.users'].create({
            'name': 'Meta A', 'login': 'meta_a',
            'groups_id': [(6, 0, [base_internal.id, self.admin_group.id])]})

    def test_non_admin_cannot_read_raw_payload(self):
        log = self.env['meta.sync.log'].create(
            {'meta_leadgen_id': 'LG1', 'raw_payload': '{"pii":"secret"}'})
        rec = log.with_user(self.meta_user).read(['status'])
        self.assertIn('status', rec[0])
        # Naming a groups= field in read() raises; it is only dropped silently
        # on a read of all fields. That makes this the stricter check.
        with self.assertRaises(AccessError):
            log.with_user(self.meta_user).read(['raw_payload'])

    def test_admin_can_read_raw_payload(self):
        log = self.env['meta.sync.log'].create(
            {'meta_leadgen_id': 'LG1', 'raw_payload': '{"pii":"secret"}'})
        rec = log.with_user(self.meta_admin).read(['raw_payload'])
        self.assertEqual(rec[0]['raw_payload'], '{"pii":"secret"}')

    def test_raw_payload_groups_attr(self):
        # Pin the exact group so a later edit can't quietly widen it.
        self.assertEqual(
            self.env['meta.sync.log']._fields['raw_payload'].groups,
            'meta_lead_ads.group_meta_admin')

    def test_acl_user_readonly(self):
        self.env['meta.sync.log'].with_user(self.meta_user).search([])
        with self.assertRaises(AccessError):
            self.env['meta.sync.log'].with_user(self.meta_user).create(
                {'meta_leadgen_id': 'X'})

    def test_acl_admin_crud(self):
        log = self.env['meta.sync.log'].with_user(self.meta_admin).create(
            {'meta_leadgen_id': 'LGA'})
        log.with_user(self.meta_admin).write({'status': 'success'})
        log.with_user(self.meta_admin).unlink()

    def test_crm_manager_reads_log_not_payload(self):
        # A CRM manager sees the log row but not raw_payload.
        mgr = self.env['res.users'].create({
            'name': 'CRM Mgr', 'login': 'crm_mgr',
            'groups_id': [(6, 0, [self.env.ref('base.group_user').id,
                                  self.env.ref('sales_team.group_sale_manager').id])]})
        log = self.env['meta.sync.log'].create(
            {'meta_leadgen_id': 'LG1', 'raw_payload': '{"pii":"secret"}'})
        rec = log.with_user(mgr).read(['status'])
        self.assertIn('status', rec[0])
        with self.assertRaises(AccessError):
            log.with_user(mgr).read(['raw_payload'])
