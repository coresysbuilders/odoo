# CoreSys Builders — https://coresysbuilders.com
# Copyright (C) CoreSys Builders. License OPL-1.
from odoo.exceptions import UserError
from odoo.tests.common import TransactionCase


class TestApprovalDecisionConcurrency(TransactionCase):

    @classmethod
    def setUpClass(cls):
        super().setUpClass()
        cls.Category = cls.env['coresys.approval.category']
        cls.Level = cls.env['coresys.approval.level']
        cls.Request = cls.env['coresys.approval.request']
        cls.partner_model_id = cls.env['ir.model']._get_id('res.partner')

        cls.group_internal = cls.env.ref('base.group_user')
        cls.group_user = cls.env.ref('coresys_approvals.group_approval_user')
        cls.group_approver = cls.env.ref(
            'coresys_approvals.group_approval_approver')

        Users = cls.env['res.users'].with_context(no_reset_password=True)
        cls.requester = Users.create({
            'name': 'Concurrency Requester',
            'login': 'coresys_conc_requester',
            'group_ids': [(6, 0, [cls.group_internal.id, cls.group_user.id])],
        })
        cls.approver_a = Users.create({
            'name': 'Concurrency Approver A',
            'login': 'coresys_conc_approver_a',
            'group_ids': [(6, 0, [cls.group_internal.id, cls.group_approver.id])],
        })
        cls.approver_b = Users.create({
            'name': 'Concurrency Approver B',
            'login': 'coresys_conc_approver_b',
            'group_ids': [(6, 0, [cls.group_internal.id, cls.group_approver.id])],
        })

        cls.category = cls.Category.create({
            'name': 'Concurrency single-level chain',
            'model_id': cls.partner_model_id,
        })
        cls.Level.create({
            'name': 'Sole Level',
            'category_id': cls.category.id,
            'approver_user_ids': [(6, 0, [cls.approver_a.id, cls.approver_b.id])],
        })

    def test_second_decision_on_decided_line_rejected(self):
        # Two named approvers share one active level. Once A decides, a second
        # decision by B is rejected with a clean UserError and writes no
        # duplicate audit row.
        #
        # True FOR UPDATE contention cannot be exercised in a single-cursor
        # TransactionCase — this asserts the observable no-duplicate / clean-error
        # contract; the lock itself protects the genuine two-transaction race.
        request = self.Request.create({
            'category_id': self.category.id,
            'requester_id': self.requester.id,
        })
        request.with_user(self.requester).action_submit()

        request.with_user(self.approver_a).action_approve()
        self.assertEqual(request.state, 'approved')
        self.assertEqual(
            len(request.response_ids.filtered(lambda r: r.action == 'approve')),
            1, 'A single decision must write exactly one approve audit row')

        with self.assertRaises(UserError):
            request.with_user(self.approver_b).action_approve()
        self.assertEqual(
            len(request.response_ids.filtered(lambda r: r.action == 'approve')),
            1, 'A stale second decision must write no duplicate audit row')
