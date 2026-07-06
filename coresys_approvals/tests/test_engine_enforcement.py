# CoreSys Builders — https://coresysbuilders.com
# Copyright (C) CoreSys Builders. License OPL-1.
from odoo.exceptions import AccessError, UserError
from odoo.tests.common import TransactionCase


class TestApprovalEngineEnforcement(TransactionCase):

    @classmethod
    def setUpClass(cls):
        super().setUpClass()
        cls.Category = cls.env['coresys.approval.category']
        cls.Level = cls.env['coresys.approval.level']
        cls.Request = cls.env['coresys.approval.request']
        cls.Line = cls.env['coresys.approval.request.line']
        cls.partner_model_id = cls.env['ir.model']._get_id('res.partner')

        cls.group_internal = cls.env.ref('base.group_user')
        cls.group_user = cls.env.ref('coresys_approvals.group_approval_user')
        cls.group_approver = cls.env.ref('coresys_approvals.group_approval_approver')
        cls.group_manager = cls.env.ref('coresys_approvals.group_approval_manager')

        Users = cls.env['res.users'].with_context(no_reset_password=True)
        cls.requester = Users.create({
            'name': 'Chain Requester',
            'login': 'coresys_eng_requester',
            'group_ids': [(6, 0, [cls.group_internal.id, cls.group_user.id])],
        })
        cls.other = Users.create({
            'name': 'Chain Other User',
            'login': 'coresys_eng_other',
            'group_ids': [(6, 0, [cls.group_internal.id, cls.group_user.id])],
        })
        cls.approver1 = Users.create({
            'name': 'Level One Approver',
            'login': 'coresys_eng_approver1',
            'group_ids': [(6, 0, [cls.group_internal.id, cls.group_approver.id])],
        })
        cls.approver2 = Users.create({
            'name': 'Level Two Approver',
            'login': 'coresys_eng_approver2',
            'group_ids': [(6, 0, [cls.group_internal.id, cls.group_approver.id])],
        })
        cls.plain = Users.create({
            'name': 'Uninvolved User',
            'login': 'coresys_eng_plain',
            'group_ids': [(6, 0, [cls.group_internal.id, cls.group_user.id])],
        })
        cls.manager = Users.create({
            'name': 'Approval Manager',
            'login': 'coresys_eng_manager',
            'group_ids': [(6, 0, [cls.group_internal.id, cls.group_manager.id])],
        })

        cls.category = cls.Category.create({
            'name': 'Two-level chain',
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

        cls.category_single = cls.Category.create({
            'name': 'Single-level chain',
            'model_id': cls.partner_model_id,
        })
        cls.Level.create({
            'name': 'Sole Level',
            'sequence': 10,
            'category_id': cls.category_single.id,
            'approver_user_ids': [(6, 0, [cls.approver1.id])],
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

    def test_plain_user_cannot_rpc_approve(self):
        # An uninvolved user's direct RPC state write is rejected at write().
        request = self._submitted()
        with self.assertRaises(AccessError):
            request.with_user(self.plain).write({'state': 'approved'})

    def test_active_approver_direct_state_write_blocked(self):
        # Even the eligible active approver cannot reach 'approved' by a direct
        # write — action_approve is the sole mutation path (audit/chatter/reason).
        request = self._submitted()
        with self.assertRaises(AccessError):
            request.with_user(self.approver1).write({'state': 'approved'})

    def test_active_approver_direct_line_write_blocked(self):
        # The active approver cannot write the line status directly; decision
        # fields are engine-only, mutated by action_approve under sudo.
        request = self._submitted()
        active = request._active_line()
        with self.assertRaises(AccessError):
            active.with_user(self.approver1).write({'status': 'approved'})

    def test_non_approver_blocked(self):
        # A level-2 approver cannot decide while level 1 is the active level.
        request = self._submitted()
        with self.assertRaises(AccessError):
            request.with_user(self.approver2).action_approve()

    def test_out_of_sequence_blocked(self):
        # Writing a not-yet-active level's line status is rejected at write().
        request = self._submitted()
        line2 = self._line(request, 20)
        with self.assertRaises(AccessError):
            line2.with_user(self.approver2).write({'status': 'approved'})

    def test_frozen_snapshot_fields_immutable(self):
        # A non-superuser cannot reassign a frozen line's approvers or resequence
        # it, closing the self-reassignment bypass.
        request = self._submitted()
        active = request._active_line()
        with self.assertRaises(AccessError):
            active.with_user(self.approver1).write(
                {'approver_user_ids': [(6, 0, [self.approver1.id])]})
        with self.assertRaises(AccessError):
            active.with_user(self.approver1).write({'sequence': 99})

    def test_manager_cannot_forge_line(self):
        # Even a manager cannot create a snapshot line by direct RPC.
        request = self._submitted()
        with self.assertRaises(AccessError):
            self.Line.with_user(self.manager).create({
                'request_id': request.id,
                'sequence': 1,
                'status': 'pending',
                'approver_user_ids': [(6, 0, [self.manager.id])],
            })

    def test_manager_cannot_delete_line(self):
        # Even a manager cannot unlink a snapshot line by direct RPC.
        request = self._submitted()
        line = self._line(request, 10)
        with self.assertRaises(AccessError):
            line.with_user(self.manager).unlink()

    def test_terminal_request_line_write_blocked(self):
        # After refusal, a leftover pending line cannot be mutated by its approver.
        request = self._submitted()
        request.refusal_reason = 'Budget exceeded'
        request.with_user(self.approver1).action_refuse()
        self.assertEqual(request.state, 'refused')
        line2 = self._line(request, 20)
        with self.assertRaises(AccessError):
            line2.with_user(self.approver2).write({'status': 'approved'})

    def test_submitted_request_fields_immutable(self):
        # Post-submit, lifecycle fields are locked to non-superusers; a draft
        # edit of the same fields still succeeds (the guard is state-aware).
        request = self._submitted()
        with self.assertRaises(AccessError):
            request.with_user(self.requester).write({'company_id': False})
        with self.assertRaises(AccessError):
            request.with_user(self.requester).write({'requester_id': self.other.id})

        draft = self.Request.create({
            'category_id': self.category.id,
            'requester_id': self.requester.id,
        })
        draft.with_user(self.requester).write({'requester_id': self.other.id})
        self.assertEqual(draft.requester_id, self.other)

    def test_active_approver_approves_and_advances(self):
        # Approving level 1 approves its line, keeps the request open, and makes
        # level 2 the active line.
        request = self._submitted()
        request.with_user(self.approver1).action_approve()
        self.assertEqual(self._line(request, 10).status, 'approved')
        self.assertEqual(request.state, 'to_approve')
        self.assertEqual(request._active_line(), self._line(request, 20))

    def test_final_level_approves(self):
        # Approving the last pending level approves the request.
        request = self._submitted(category=self.category_single)
        request.with_user(self.approver1).action_approve()
        self.assertEqual(request.state, 'approved')

    def test_refuse_requires_reason(self):
        # Refusing without a reason raises; with a reason it terminally refuses
        # the whole request while leaving other pending lines untouched.
        request = self._submitted()
        with self.assertRaises(UserError):
            request.with_user(self.approver1).action_refuse()

        request.refusal_reason = 'Budget exceeded'
        request.with_user(self.approver1).action_refuse()
        self.assertEqual(request.state, 'refused')
        self.assertEqual(self._line(request, 10).status, 'refused')
        self.assertEqual(self._line(request, 20).status, 'pending')

    def test_reset_resubmit_resnapshots(self):
        # A refused request resets to draft (by its non-manager requester),
        # clearing its lines; resubmitting re-resolves from current config and
        # restarts at the lowest sequence.
        request = self._submitted()
        request.refusal_reason = 'Send it back'
        request.with_user(self.approver1).action_refuse()

        request.with_user(self.requester).action_reset()
        self.assertEqual(request.state, 'draft')
        self.assertFalse(request.line_ids)

        self.Level.create({
            'name': 'New First Level',
            'sequence': 5,
            'category_id': self.category.id,
            'approver_user_ids': [(6, 0, [self.approver1.id])],
        })
        request.with_user(self.requester).action_submit()
        self.assertEqual(request.state, 'to_approve')
        self.assertEqual(len(request.line_ids), 3)
        active = request._active_line()
        self.assertEqual(active.name, 'New First Level')
        self.assertEqual(active.sequence, 5)

    def test_reset_denied_for_stranger(self):
        # A non-requester, non-manager cannot reset a refused request; the inline
        # authorization runs before the destructive unlink, so lines survive.
        request = self._submitted()
        request.refusal_reason = 'Send it back'
        request.with_user(self.approver1).action_refuse()
        with self.assertRaises(AccessError):
            request.with_user(self.plain).action_reset()
        self.assertTrue(request.line_ids)

    def test_cancel_from_to_approve(self):
        # The requester can cancel an in-progress request.
        request = self._submitted()
        request.with_user(self.requester).action_cancel()
        self.assertEqual(request.state, 'cancelled')
