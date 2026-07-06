# CoreSys Builders — https://coresysbuilders.com
# Copyright (C) CoreSys Builders. License OPL-1.
from odoo.tests.common import TransactionCase


class TestApprovalResolverScoping(TransactionCase):

    @classmethod
    def setUpClass(cls):
        super().setUpClass()
        cls.Category = cls.env['coresys.approval.category']
        cls.Level = cls.env['coresys.approval.level']
        cls.Request = cls.env['coresys.approval.request']
        cls.partner_model_id = cls.env['ir.model']._get_id('res.partner')

        cls.group_internal = cls.env.ref('base.group_user')
        cls.group_portal = cls.env.ref('base.group_portal')
        cls.group_user = cls.env.ref('coresys_approvals.group_approval_user')
        cls.group_approver = cls.env.ref(
            'coresys_approvals.group_approval_approver')

        Users = cls.env['res.users'].with_context(no_reset_password=True)
        cls.requester = Users.create({
            'name': 'Resolver Requester',
            'login': 'coresys_resolver_requester',
            'groups_id': [(6, 0, [cls.group_internal.id, cls.group_user.id])],
        })
        cls.active_internal = Users.create({
            'name': 'Resolver Active Approver',
            'login': 'coresys_resolver_active',
            'groups_id': [(6, 0, [cls.group_internal.id, cls.group_approver.id])],
        })
        cls.inactive_user = Users.create({
            'name': 'Resolver Inactive Approver',
            'login': 'coresys_resolver_inactive',
            'active': False,
            'groups_id': [(6, 0, [cls.group_internal.id, cls.group_approver.id])],
        })
        cls.portal_user = Users.create({
            'name': 'Resolver Portal Approver',
            'login': 'coresys_resolver_portal',
            'groups_id': [(6, 0, [cls.group_portal.id])],
        })

        # Clean single-approver chain for the plain-requester submit path.
        cls.category = cls.Category.create({
            'name': 'Resolver clean chain',
            'model_id': cls.partner_model_id,
        })
        cls.Level.create({
            'name': 'Sole Level',
            'category_id': cls.category.id,
            'approver_user_ids': [(6, 0, [cls.active_internal.id])],
        })

        # Mixed level: one valid approver plus an inactive and a portal user.
        cls.mixed_category = cls.Category.create({
            'name': 'Resolver mixed chain',
            'model_id': cls.partner_model_id,
        })
        cls.mixed_level = cls.Level.create({
            'name': 'Mixed Level',
            'category_id': cls.mixed_category.id,
            'approver_user_ids': [(6, 0, [
                cls.active_internal.id,
                cls.inactive_user.id,
                cls.portal_user.id,
            ])],
        })

    def _active_line(self, request):
        return request.line_ids.filtered(
            lambda line: line.status == 'pending').sorted('sequence')[:1]

    def test_plain_requester_can_submit(self):
        # A plain requester (group_approval_user only) submits their own draft
        # without an AccessError while the resolver reads approvers' company_ids;
        # the request reaches to_approve and freezes the active internal approver.
        request = self.Request.create({
            'category_id': self.category.id,
            'requester_id': self.requester.id,
        })
        request.with_user(self.requester).action_submit()
        self.assertEqual(request.state, 'to_approve')
        self.assertEqual(self._active_line(request).approver_user_ids,
                         self.active_internal,
                         'Only the active internal approver must be frozen')

    def test_inactive_and_portal_approvers_excluded(self):
        # A level naming an active internal user, an inactive user and a portal
        # (share) user snapshots ONLY the active internal user.
        request = self.Request.create({
            'category_id': self.mixed_category.id,
            'requester_id': self.requester.id,
        })
        request.with_user(self.requester).action_submit()
        self.assertEqual(self._active_line(request).approver_user_ids,
                         self.active_internal,
                         'Inactive and portal/share approvers must be excluded '
                         'from the frozen approver set')
