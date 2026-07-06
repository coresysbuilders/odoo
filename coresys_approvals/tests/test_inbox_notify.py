# CoreSys Builders — https://coresysbuilders.com
# Copyright (C) CoreSys Builders. License OPL-1.
from odoo.tests.common import TransactionCase


class TestApprovalInboxNotify(TransactionCase):

    @classmethod
    def setUpClass(cls):
        super().setUpClass()
        cls.Category = cls.env['coresys.approval.category']
        cls.Level = cls.env['coresys.approval.level']
        cls.Request = cls.env['coresys.approval.request']
        cls.Activity = cls.env['mail.activity']
        cls.partner_model_id = cls.env['ir.model']._get_id('res.partner')

        cls.group_internal = cls.env.ref('base.group_user')
        cls.group_user = cls.env.ref('coresys_approvals.group_approval_user')
        cls.group_approver = cls.env.ref(
            'coresys_approvals.group_approval_approver')

        Users = cls.env['res.users'].with_context(no_reset_password=True)
        cls.requester = Users.create({
            'name': 'Inbox Requester',
            'login': 'coresys_inbox_requester',
            'group_ids': [(6, 0, [cls.group_internal.id, cls.group_user.id])],
        })
        cls.approver1 = Users.create({
            'name': 'Inbox Level One Approver',
            'login': 'coresys_inbox_approver1',
            'group_ids': [(6, 0, [cls.group_internal.id, cls.group_approver.id])],
        })
        cls.approver2 = Users.create({
            'name': 'Inbox Level Two Approver',
            'login': 'coresys_inbox_approver2',
            'group_ids': [(6, 0, [cls.group_internal.id, cls.group_approver.id])],
        })

        cls.category = cls.Category.create({
            'name': 'Inbox two-level chain',
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

    def _submitted(self):
        request = self.Request.create({
            'category_id': self.category.id,
            'requester_id': self.requester.id,
        })
        request.with_user(self.requester).action_submit()
        return request

    def _todos(self, request, user=None):
        domain = [
            ('res_model', '=', 'coresys.approval.request'),
            ('res_id', '=', request.id),
        ]
        if user is not None:
            domain.append(('user_id', '=', user.id))
        return self.Activity.search(domain)

    def test_activity_scheduled_for_approver(self):
        # After submit, each eligible approver of level 1 has a To-Do activity.
        request = self._submitted()
        self.assertTrue(self._todos(request, self.approver1),
                        'The active-level approver must receive a To-Do')
        self.assertFalse(self._todos(request, self.approver2),
                         'A not-yet-active approver must not be notified')

    def test_activity_cleared_after_decision(self):
        # Approving level 1 clears its To-Dos and notifies level-2 approvers.
        request = self._submitted()
        request.with_user(self.approver1).action_approve()
        self.assertFalse(self._todos(request, self.approver1),
                         'A decided level must leave no stale To-Do')
        self.assertTrue(self._todos(request, self.approver2),
                        'Advancing to level 2 must notify its approver')

    def test_active_approver_recompute(self):
        # active_approver_user_ids tracks the active level and empties at the end.
        request = self._submitted()
        self.assertEqual(request.active_approver_user_ids, self.approver1)
        request.with_user(self.approver1).action_approve()
        self.assertEqual(request.active_approver_user_ids, self.approver2)
        request.with_user(self.approver2).action_approve()
        self.assertEqual(request.state, 'approved')
        self.assertFalse(request.active_approver_user_ids,
                         'A decided request awaits no one')

    def test_inbox_domain_lists_awaiting_user(self):
        # The inbox domain (list-form M2M `in`) lists only the awaiting approver.
        request = self._submitted()
        awaiting = self.Request.search([
            ('state', '=', 'to_approve'),
            ('active_approver_user_ids', 'in', [self.approver1.id]),
        ])
        self.assertIn(request, awaiting)
        not_awaiting = self.Request.search([
            ('state', '=', 'to_approve'),
            ('active_approver_user_ids', 'in', [self.approver2.id]),
        ])
        self.assertNotIn(request, not_awaiting,
                         'A non-active approver must not see the request yet')

    def test_no_cross_company_leak(self):
        # A Company-A request in to_approve is invisible to a Company-B approver.
        Company = self.env['res.company']
        company_a = Company.create({'name': 'Inbox Company A'})
        company_b = Company.create({'name': 'Inbox Company B'})

        Users = self.env['res.users'].with_context(no_reset_password=True)
        approver_a = Users.create({
            'name': 'Inbox Company A Approver',
            'login': 'coresys_inbox_company_a',
            'company_id': company_a.id,
            'company_ids': [(6, 0, [company_a.id])],
            'group_ids': [(6, 0, [self.group_internal.id, self.group_approver.id])],
        })
        approver_b = Users.create({
            'name': 'Inbox Company B Approver',
            'login': 'coresys_inbox_company_b',
            'company_id': company_b.id,
            'company_ids': [(6, 0, [company_b.id])],
            'group_ids': [(6, 0, [self.group_internal.id, self.group_approver.id])],
        })

        category_a = self.Category.create({
            'name': 'Inbox Company A chain',
            'model_id': self.partner_model_id,
            'company_id': company_a.id,
        })
        self.Level.create({
            'name': 'Company A level',
            'category_id': category_a.id,
            'approver_user_ids': [(6, 0, [approver_a.id])],
        })
        request = self.Request.create({
            'category_id': category_a.id,
            'company_id': company_a.id,
        })
        request.action_submit()

        visible = self.Request.with_user(approver_b).with_context(
            allowed_company_ids=[company_b.id]
        ).search([])
        self.assertNotIn(request, visible,
                         'A Company-A request must not leak to a Company-B user')

    def test_no_cross_company_activity(self):
        # A Company-B-only group member receives no To-Do for a Company-A request
        # (the resolver excluded them from the snapshot), while the Company-A
        # named approver does (finding #8 regression).
        Company = self.env['res.company']
        company_a = Company.create({'name': 'Activity Company A'})
        company_b = Company.create({'name': 'Activity Company B'})

        Users = self.env['res.users'].with_context(no_reset_password=True)
        member_b = Users.create({
            'name': 'Activity Company B Member',
            'login': 'coresys_activity_company_b',
            'company_id': company_b.id,
            'company_ids': [(6, 0, [company_b.id])],
            'group_ids': [(6, 0, [self.group_internal.id, self.group_approver.id])],
        })
        approver_a = Users.create({
            'name': 'Activity Company A Approver',
            'login': 'coresys_activity_company_a',
            'company_id': company_a.id,
            'company_ids': [(6, 0, [company_a.id])],
            'group_ids': [(6, 0, [self.group_internal.id, self.group_approver.id])],
        })
        cross_group = self.env['res.groups'].create({
            'name': 'Activity Cross Company Group',
            'user_ids': [(6, 0, [member_b.id])],
        })

        category_a = self.Category.create({
            'name': 'Activity Company A chain',
            'model_id': self.partner_model_id,
            'company_id': company_a.id,
        })
        self.Level.create({
            'name': 'Company A level',
            'category_id': category_a.id,
            'approver_group_id': cross_group.id,
            'approver_user_ids': [(6, 0, [approver_a.id])],
        })
        request = self.Request.create({
            'category_id': category_a.id,
            'company_id': company_a.id,
        })
        request.action_submit()

        self.assertFalse(self._todos(request, member_b),
                         'A Company-B-only member must not be sent a To-Do for '
                         'a Company-A request')
        self.assertTrue(self._todos(request, approver_a),
                        'The Company-A approver must receive a To-Do')
