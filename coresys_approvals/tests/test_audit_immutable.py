# CoreSys Builders — https://coresysbuilders.com
# Copyright (C) CoreSys Builders. License OPL-1.
from odoo.addons.base.models.ir_model import MODULE_UNINSTALL_FLAG
from odoo.exceptions import AccessError
from odoo.tests.common import TransactionCase


class TestApprovalAuditImmutable(TransactionCase):

    @classmethod
    def setUpClass(cls):
        super().setUpClass()
        cls.Category = cls.env['coresys.approval.category']
        cls.Level = cls.env['coresys.approval.level']
        cls.Request = cls.env['coresys.approval.request']
        cls.Response = cls.env['coresys.approval.response']
        cls.partner_model_id = cls.env['ir.model']._get_id('res.partner')

        cls.group_internal = cls.env.ref('base.group_user')
        cls.group_user = cls.env.ref('coresys_approvals.group_approval_user')
        cls.group_approver = cls.env.ref('coresys_approvals.group_approval_approver')
        cls.group_manager = cls.env.ref('coresys_approvals.group_approval_manager')

        Users = cls.env['res.users'].with_context(no_reset_password=True)
        cls.requester = Users.create({
            'name': 'Audit Requester',
            'login': 'coresys_aud_requester',
            'group_ids': [(6, 0, [cls.group_internal.id, cls.group_user.id])],
        })
        cls.approver1 = Users.create({
            'name': 'Audit Level One Approver',
            'login': 'coresys_aud_approver1',
            'group_ids': [(6, 0, [cls.group_internal.id, cls.group_approver.id])],
        })
        cls.approver2 = Users.create({
            'name': 'Audit Level Two Approver',
            'login': 'coresys_aud_approver2',
            'group_ids': [(6, 0, [cls.group_internal.id, cls.group_approver.id])],
        })
        cls.manager = Users.create({
            'name': 'Audit Approval Manager',
            'login': 'coresys_aud_manager',
            'group_ids': [(6, 0, [cls.group_internal.id, cls.group_manager.id])],
        })

        cls.category = cls.Category.create({
            'name': 'Audit two-level chain',
            'model_id': cls.partner_model_id,
        })
        cls.Level.create({
            'name': 'Level One',
            'sequence': 10,
            'category_id': cls.category.id,
            'approver_user_ids': [(6, 0, [cls.approver1.id])],
        })
        cls.Level.create({
            'name': 'Level Two',
            'sequence': 20,
            'category_id': cls.category.id,
            'approver_user_ids': [(6, 0, [cls.approver2.id])],
        })

    def _submitted(self, category=None):
        request = self.Request.create({
            'category_id': (category or self.category).id,
            'requester_id': self.requester.id,
        })
        request.with_user(self.requester).action_submit()
        return request

    def _line(self, request, sequence):
        return request.line_ids.filtered(lambda line: line.sequence == sequence)

    def test_decision_writes_audit(self):
        # Every decision leaves an immutable audit row capturing who/level/action;
        # a refusal also freezes its reason.
        request = self._submitted()
        submit_row = request.response_ids.filtered(
            lambda r: r.action == 'submit')
        self.assertTrue(submit_row, 'Submitting must write an audit record')
        self.assertEqual(submit_row.user_id, self.requester)

        line1 = self._line(request, 10)
        request.with_user(self.approver1).action_approve()
        approve_row = request.response_ids.filtered(
            lambda r: r.action == 'approve')
        self.assertTrue(approve_row, 'Approving must write an audit record')
        self.assertEqual(approve_row.user_id, self.approver1)
        self.assertEqual(approve_row.level_name, line1.name)

        refused = self._submitted()
        refused.refusal_reason = 'Budget exceeded'
        refused.with_user(self.approver1).action_refuse()
        refuse_row = refused.response_ids.filtered(
            lambda r: r.action == 'refuse')
        self.assertTrue(refuse_row, 'Refusing must write an audit record')
        self.assertEqual(refuse_row.user_id, self.approver1)
        self.assertEqual(refuse_row.reason, 'Budget exceeded')

    def test_manager_cannot_mutate_audit(self):
        # No user, including a Manager, and not even sudo/admin, can edit history.
        request = self._submitted()
        response = request.response_ids[:1]
        self.assertTrue(response)
        with self.assertRaises(AccessError):
            response.with_user(self.manager).write({'reason': 'tampered'})
        with self.assertRaises(AccessError):
            response.sudo().write({'reason': 'tampered'})

    def test_audit_unlink_blocked(self):
        # Deletion raises for a normal delete even under sudo, but the module
        # uninstall flag still lets Odoo drop the rows so the module stays
        # uninstallable.
        request = self._submitted()
        response = request.response_ids[:1]
        self.assertTrue(response)
        with self.assertRaises(AccessError):
            response.unlink()
        with self.assertRaises(AccessError):
            response.sudo().unlink()
        response.with_context(**{MODULE_UNINSTALL_FLAG: True}).sudo().unlink()
        self.assertFalse(response.exists(),
                         'Uninstall-flag deletion must succeed')

    def test_audit_create_only_acl(self):
        # A Manager cannot forge an audit entry by direct RPC create; only the
        # engine sudo-creates.
        request = self._submitted()
        with self.assertRaises(AccessError):
            self.Response.with_user(self.manager).create({
                'request_id': request.id,
                'user_id': self.manager.id,
                'action': 'approve',
            })

    def test_audit_no_cross_company(self):
        # A Company-A audit row never leaks to a non-admin Company-B user.
        Company = self.env['res.company']
        company_a = Company.create({'name': 'Audit Company A'})
        company_b = Company.create({'name': 'Audit Company B'})

        Users = self.env['res.users'].with_context(no_reset_password=True)
        approver_a = Users.create({
            'name': 'Company A Approver',
            'login': 'coresys_aud_a_approver',
            'company_id': company_a.id,
            'company_ids': [(6, 0, [company_a.id])],
            'group_ids': [(6, 0, [self.group_internal.id, self.group_approver.id])],
        })
        requester_a = Users.create({
            'name': 'Company A Requester',
            'login': 'coresys_aud_a_requester',
            'company_id': company_a.id,
            'company_ids': [(6, 0, [company_a.id])],
            'group_ids': [(6, 0, [self.group_internal.id, self.group_user.id])],
        })
        user_b = Users.create({
            'name': 'Company B User',
            'login': 'coresys_aud_b_user',
            'company_id': company_b.id,
            'company_ids': [(6, 0, [company_b.id])],
            'group_ids': [(6, 0, [self.group_internal.id, self.group_manager.id])],
        })

        category_a = self.Category.create({
            'name': 'Company A chain',
            'model_id': self.partner_model_id,
            'company_id': company_a.id,
        })
        self.Level.create({
            'name': 'Company A Level',
            'sequence': 10,
            'category_id': category_a.id,
            'approver_user_ids': [(6, 0, [approver_a.id])],
        })
        request_a = self.Request.create({
            'category_id': category_a.id,
            'requester_id': requester_a.id,
        })
        request_a.with_user(requester_a).action_submit()
        audit_a = request_a.response_ids
        self.assertTrue(audit_a)
        self.assertEqual(audit_a.mapped('company_id'), company_a)

        visible = self.Response.with_user(user_b).with_context(
            allowed_company_ids=[company_b.id]).search([])
        self.assertFalse(audit_a & visible,
                         'Company-A audit rows must not leak to a Company-B user')

    def test_manager_cannot_delete_request_with_audit(self):
        # A Manager holds request perm_unlink, but the audit->request restrict FK
        # blocks deleting a request that already has decision history — so history
        # can never be cascade-deleted out from under the immutability override.
        request = self._submitted()
        request.with_user(self.approver1).action_approve()
        audit_ids = request.response_ids.ids
        self.assertTrue(audit_ids, 'The request must have audit rows to guard')

        with self.assertRaises(Exception):
            with self.cr.savepoint():
                request.with_user(self.manager).unlink()

        surviving = self.Response.sudo().browse(audit_ids).exists()
        self.assertEqual(set(surviving.ids), set(audit_ids),
                         'Audit rows must survive a blocked parent delete')
