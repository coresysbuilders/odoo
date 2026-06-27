# Meta Lead Ads Integration for Odoo — © 2026 CoreSys Builders. All rights reserved.
# Licensed under the Odoo Proprietary License v1.0 (OPL-1).
# Unauthorized copying, redistribution, or resale of this software, in whole or in
# part, via any medium, is strictly prohibited and constitutes a license violation.
# OPL-1: https://www.odoo.com/documentation/19.0/legal/licenses.html#odoo-apps

from odoo.tests.common import TransactionCase, tagged
from odoo.exceptions import AccessError


@tagged('post_install', '-at_install')
class TestSyncLogSecurity(TransactionCase):
    """meta.sync.log ACL posture + raw_payload PII gate.

    raw_payload is admin-only (information-disclosure mitigation); the log row
    itself is operationally visible to internal/CRM users.
    """

    def setUp(self):
        super().setUp()
        self.user_group = self.env.ref('meta_lead_ads.group_meta_user')
        self.admin_group = self.env.ref('meta_lead_ads.group_meta_admin')
        # base.group_user makes these realistic internal (non-share) users;
        # without it Odoo sets share=True (portal semantics) and ACL tests can
        # pass for the wrong reasons. group_meta_admin implies group_meta_user
        # via implied_ids, so the admin auto-gains it -- no need to add it.
        base_internal = self.env.ref('base.group_user')
        self.meta_user = self.env['res.users'].create({
            'name': 'Meta U', 'login': 'meta_u',
            'group_ids': [(6, 0, [base_internal.id, self.user_group.id])]})
        self.meta_admin = self.env['res.users'].create({
            'name': 'Meta A', 'login': 'meta_a',
            'group_ids': [(6, 0, [base_internal.id, self.admin_group.id])]})

    # ---- raw_payload PII gate (field-level groups=) -----------------------

    def test_non_admin_cannot_read_raw_payload(self):
        log = self.env['meta.sync.log'].create(
            {'meta_leadgen_id': 'LG1', 'raw_payload': '{"pii":"secret"}'})
        # operational field stays readable for a Meta User.
        rec = log.with_user(self.meta_user).read(['status'])
        self.assertIn('status', rec[0])
        # Odoo 18 RAISES AccessError when an admin-only field is requested
        # EXPLICITLY in read() (it only silently drops the key when the field is
        # not named, e.g. an implicit read-all or a group-gated view). Requesting
        # it explicitly is the strongest assertion that the PII gate holds.
        with self.assertRaises(AccessError):
            log.with_user(self.meta_user).read(['raw_payload'])

    def test_admin_can_read_raw_payload(self):
        log = self.env['meta.sync.log'].create(
            {'meta_leadgen_id': 'LG1', 'raw_payload': '{"pii":"secret"}'})
        rec = log.with_user(self.meta_admin).read(['raw_payload'])
        self.assertEqual(rec[0]['raw_payload'], '{"pii":"secret"}')

    def test_raw_payload_groups_attr(self):
        # Lock the gate at the field level: it is exactly the admin group, so it
        # cannot be silently widened by a later edit.
        self.assertEqual(
            self.env['meta.sync.log']._fields['raw_payload'].groups,
            'meta_lead_ads.group_meta_admin')

    # ---- ACL posture (operational, not secret) ----------------------------

    def test_acl_user_readonly(self):
        # A Meta User can read/search the log but cannot create rows.
        self.env['meta.sync.log'].with_user(self.meta_user).search([])
        with self.assertRaises(AccessError):
            self.env['meta.sync.log'].with_user(self.meta_user).create(
                {'meta_leadgen_id': 'X'})

    def test_acl_admin_crud(self):
        # A Meta Admin has full create/write/unlink on the log.
        log = self.env['meta.sync.log'].with_user(self.meta_admin).create(
            {'meta_leadgen_id': 'LGA'})
        log.with_user(self.meta_admin).write({'status': 'success'})
        log.with_user(self.meta_admin).unlink()

    # ---- operational visibility vs PII gate reconciliation ----------------

    def test_crm_manager_reads_log_not_payload(self):
        # A CRM manager can READ a sync-log row (operational visibility) but
        # raw_payload stays absent (PII admin-only).
        mgr = self.env['res.users'].create({
            'name': 'CRM Mgr', 'login': 'crm_mgr',
            'group_ids': [(6, 0, [self.env.ref('base.group_user').id,
                                  self.env.ref('sales_team.group_sale_manager').id])]})
        log = self.env['meta.sync.log'].create(
            {'meta_leadgen_id': 'LG1', 'raw_payload': '{"pii":"secret"}'})
        rec = log.with_user(mgr).read(['status'])
        self.assertIn('status', rec[0])           # managers see the operational row
        # PII payload stays admin-only -> explicit read denied (Odoo 18 raises).
        with self.assertRaises(AccessError):
            log.with_user(mgr).read(['raw_payload'])
