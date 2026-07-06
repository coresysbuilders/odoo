# CoreSys Builders — https://coresysbuilders.com
# Copyright (C) CoreSys Builders. License OPL-1.
from odoo.exceptions import UserError
from odoo.tests.common import TransactionCase


class TestApprovalRequestUnlink(TransactionCase):

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
        cls.group_manager = cls.env.ref(
            'coresys_approvals.group_approval_manager')

        Users = cls.env['res.users'].with_context(no_reset_password=True)
        cls.requester = Users.create({
            'name': 'Unlink Requester',
            'login': 'coresys_unlink_requester',
            'group_ids': [(6, 0, [cls.group_internal.id, cls.group_user.id])],
        })
        cls.approver = Users.create({
            'name': 'Unlink Approver',
            'login': 'coresys_unlink_approver',
            'group_ids': [(6, 0, [cls.group_internal.id, cls.group_approver.id])],
        })
        cls.manager = Users.create({
            'name': 'Unlink Manager',
            'login': 'coresys_unlink_manager',
            'group_ids': [(6, 0, [cls.group_internal.id, cls.group_manager.id])],
        })

        cls.category = cls.Category.create({
            'name': 'Unlink single-level chain',
            'model_id': cls.partner_model_id,
        })
        cls.Level.create({
            'name': 'Sole Level',
            'category_id': cls.category.id,
            'approver_user_ids': [(6, 0, [cls.approver.id])],
        })

    def _draft(self):
        return self.Request.create({
            'category_id': self.category.id,
            'requester_id': self.requester.id,
        })

    def test_draft_request_deletable(self):
        # A draft with no decision history can be deleted by a manager.
        request = self._draft()
        request.with_user(self.manager).unlink()
        self.assertFalse(request.exists(),
                         'A draft request must be deletable')

    def test_submitted_request_not_deletable(self):
        # A submitted request cannot be deleted — a clean UserError is raised.
        request = self._draft()
        request.with_user(self.requester).action_submit()
        with self.assertRaises(UserError):
            request.with_user(self.manager).unlink()
        self.assertTrue(request.exists(),
                        'A submitted request must survive the delete attempt')

    def test_approved_request_not_deletable(self):
        # A decided (approved) request cannot be deleted.
        request = self._draft()
        request.with_user(self.requester).action_submit()
        request.with_user(self.approver).action_approve()
        self.assertEqual(request.state, 'approved')
        with self.assertRaises(UserError):
            request.with_user(self.manager).unlink()
        self.assertTrue(request.exists(),
                        'An approved request must survive the delete attempt')

    def test_cancelled_request_with_audit_not_deletable(self):
        # action_cancel logs a 'cancel' decision, so a cancelled request always
        # carries audit history — deleting it must raise a clean UserError, not
        # slip past the state check and hit the response FK as a raw DB error.
        request = self._draft()
        request.with_user(self.requester).action_cancel()
        self.assertEqual(request.state, 'cancelled')
        self.assertTrue(request.response_ids,
                        'A cancelled request retains its cancel decision record')
        with self.assertRaises(UserError):
            request.with_user(self.manager).unlink()
        self.assertTrue(request.exists(),
                        'A cancelled request with history must survive deletion')

    def test_reset_to_draft_with_audit_not_deletable(self):
        # submit -> refuse -> reset returns the request to draft but keeps its
        # earlier audit rows; the draft state must not make it deletable.
        request = self._draft()
        request.with_user(self.requester).action_submit()
        request.refusal_reason = 'Rejected for cause'
        request.with_user(self.approver).action_refuse()
        request.with_user(self.requester).action_reset()
        self.assertEqual(request.state, 'draft')
        self.assertTrue(request.response_ids,
                        'A reset request retains its submit/refuse/reset history')
        with self.assertRaises(UserError):
            request.with_user(self.manager).unlink()
        self.assertTrue(request.exists(),
                        'A reset draft with history must survive deletion')
