# CoreSys Builders — https://coresysbuilders.com
# Copyright (C) CoreSys Builders. License OPL-1.
from odoo.tests.common import TransactionCase


class TestApprovalActivityType(TransactionCase):

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
        cls.engine_type = cls.env.ref(
            'coresys_approvals.mail_act_approval_todo')
        cls.generic_type = cls.env.ref('mail.mail_activity_data_todo')

        Users = cls.env['res.users'].with_context(no_reset_password=True)
        cls.requester = Users.create({
            'name': 'Activity Type Requester',
            'login': 'coresys_acttype_requester',
            'group_ids': [(6, 0, [cls.group_internal.id, cls.group_user.id])],
        })
        cls.approver1 = Users.create({
            'name': 'Activity Type Approver',
            'login': 'coresys_acttype_approver1',
            'group_ids': [(6, 0, [cls.group_internal.id, cls.group_approver.id])],
        })

        cls.category = cls.Category.create({
            'name': 'Activity Type single-level chain',
            'model_id': cls.partner_model_id,
        })
        cls.Level.create({
            'name': 'Sole Level',
            'category_id': cls.category.id,
            'approver_user_ids': [(6, 0, [cls.approver1.id])],
        })

    def _submitted(self):
        request = self.Request.create({
            'category_id': self.category.id,
            'requester_id': self.requester.id,
        })
        request.with_user(self.requester).action_submit()
        return request

    def _engine_todos(self, request, user):
        return self.Activity.search([
            ('res_model', '=', 'coresys.approval.request'),
            ('res_id', '=', request.id),
            ('user_id', '=', user.id),
            ('activity_type_id', '=', self.engine_type.id),
        ])

    def _generic_todos(self, request, user):
        return self.Activity.search([
            ('res_model', '=', 'coresys.approval.request'),
            ('res_id', '=', request.id),
            ('user_id', '=', user.id),
            ('activity_type_id', '=', self.generic_type.id),
        ])

    def test_manual_todo_survives_decision(self):
        # A user's own generic To-Do on the request is not wiped by the engine
        # when a level is decided; only the engine's dedicated type is cleared.
        request = self._submitted()
        request.activity_schedule(
            'mail.mail_activity_data_todo',
            user_id=self.approver1.id,
            summary='Follow up manually')
        self.assertTrue(self._generic_todos(request, self.approver1))

        request.with_user(self.approver1).action_approve()
        self.assertTrue(self._generic_todos(request, self.approver1),
                        'A manually-added generic To-Do must survive a decision')
        self.assertFalse(self._engine_todos(request, self.approver1),
                         'The engine To-Do must be cleared after the decision')

    def test_notify_is_idempotent(self):
        # Re-notifying the active level does not create a second engine To-Do for
        # an approver who already has an open one.
        request = self._submitted()
        self.assertEqual(len(self._engine_todos(request, self.approver1)), 1)
        request._notify_active_level()
        self.assertEqual(len(self._engine_todos(request, self.approver1)), 1,
                         'Re-notify must not double-create the engine To-Do')
