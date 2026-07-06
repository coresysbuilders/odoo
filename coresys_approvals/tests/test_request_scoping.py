# CoreSys Builders — https://coresysbuilders.com
# Copyright (C) CoreSys Builders. License OPL-1.
from odoo.exceptions import AccessError, ValidationError
from odoo.tests.common import TransactionCase


class TestApprovalRequestScoping(TransactionCase):

    @classmethod
    def setUpClass(cls):
        super().setUpClass()
        cls.Category = cls.env['coresys.approval.category']
        cls.Level = cls.env['coresys.approval.level']
        cls.Request = cls.env['coresys.approval.request']
        cls.partner_model_id = cls.env['ir.model']._get_id('res.partner')

        cls.group_internal = cls.env.ref('base.group_user')
        cls.group_user = cls.env.ref('coresys_approvals.group_approval_user')
        cls.group_approver = cls.env.ref('coresys_approvals.group_approval_approver')
        cls.group_manager = cls.env.ref('coresys_approvals.group_approval_manager')

        Users = cls.env['res.users'].with_context(no_reset_password=True)
        cls.requester = Users.create({
            'name': 'Scope Requester',
            'login': 'coresys_scope_requester',
            'group_ids': [(6, 0, [cls.group_internal.id, cls.group_user.id])],
        })
        cls.stranger = Users.create({
            'name': 'Scope Stranger',
            'login': 'coresys_scope_stranger',
            'group_ids': [(6, 0, [cls.group_internal.id, cls.group_user.id])],
        })
        cls.approver = Users.create({
            'name': 'Scope Approver',
            'login': 'coresys_scope_approver',
            'group_ids': [(6, 0, [cls.group_internal.id, cls.group_approver.id])],
        })
        cls.manager = Users.create({
            'name': 'Scope Manager',
            'login': 'coresys_scope_manager',
            'group_ids': [(6, 0, [cls.group_internal.id, cls.group_manager.id])],
        })

        cls.category = cls.Category.create({
            'name': 'Scoping chain',
            'model_id': cls.partner_model_id,
        })
        cls.Level.create({
            'name': 'Sole Level',
            'category_id': cls.category.id,
            'approver_user_ids': [(6, 0, [cls.approver.id])],
        })

    def _draft(self, requester=None):
        return self.Request.create({
            'category_id': self.category.id,
            'requester_id': (requester or self.requester).id,
        })

    # --- H1: action_submit authorization -------------------------------------

    def test_stranger_cannot_submit_others_draft(self):
        # A plain Approval User who is neither the requester nor a manager cannot
        # submit someone else's draft.
        request = self._draft()
        with self.assertRaises(AccessError):
            request.with_user(self.stranger).action_submit()

    def test_requester_can_submit_own_draft(self):
        # The requester submits their own draft end-to-end.
        request = self._draft()
        request.with_user(self.requester).action_submit()
        self.assertEqual(request.state, 'to_approve')

    def test_manager_can_submit_others_draft(self):
        # A manager may submit a request on behalf of the requester.
        request = self._draft()
        request.with_user(self.manager).action_submit()
        self.assertEqual(request.state, 'to_approve')

    # --- H1: record-rule read/search scoping ---------------------------------

    def test_stranger_cannot_see_others_request(self):
        # A request created by one plain user is invisible to another plain user
        # via the ownership record rule, but a manager retains full visibility.
        request = self.Request.with_user(self.requester).create({
            'category_id': self.category.id,
        })
        found = self.Request.with_user(self.stranger).search(
            [('id', '=', request.id)])
        self.assertFalse(found, 'A stranger must not see another user\'s request')
        with self.assertRaises(AccessError):
            request.with_user(self.stranger).read(['name'])

        seen = self.Request.with_user(self.manager).search(
            [('id', '=', request.id)])
        self.assertEqual(seen, request.with_user(self.manager),
                         'A manager must see every request')

    def test_requester_sees_own_request(self):
        # The requester sees their own request under the ownership rule.
        request = self.Request.with_user(self.requester).create({
            'category_id': self.category.id,
        })
        found = self.Request.with_user(self.requester).search(
            [('id', '=', request.id)])
        self.assertEqual(found, request.with_user(self.requester))

    # --- H1: record-rule create scoping --------------------------------------

    def test_non_manager_cannot_create_for_another_user(self):
        # A plain user cannot attribute a new request to someone else — the
        # ownership rule rejects the create.
        with self.assertRaises(AccessError):
            self.Request.with_user(self.requester).create({
                'category_id': self.category.id,
                'requester_id': self.stranger.id,
            })

    def test_non_manager_can_create_own_request(self):
        # A plain user creating their own request is allowed.
        request = self.Request.with_user(self.requester).create({
            'category_id': self.category.id,
        })
        self.assertEqual(request.requester_id, self.requester)

    # --- H2: company/category coupling constraint ----------------------------

    def test_company_must_match_category_company(self):
        # A company-scoped category pins its requests to that company: detaching
        # the request (company_id=False) or moving it to another company raises.
        Company = self.env['res.company']
        company_a = Company.create({'name': 'Coupling Company A'})
        company_b = Company.create({'name': 'Coupling Company B'})
        category = self.Category.create({
            'name': 'Company A coupled chain',
            'model_id': self.partner_model_id,
            'company_id': company_a.id,
        })
        self.Level.create({
            'name': 'Company A Level',
            'category_id': category.id,
            'approver_user_ids': [(6, 0, [self.approver.id])],
        })
        request = self.Request.create({'category_id': category.id})
        self.assertEqual(request.company_id, company_a,
                         'The request must inherit its category company')

        with self.assertRaises(ValidationError):
            request.write({'company_id': False})
        with self.assertRaises(ValidationError):
            request.write({'company_id': company_b.id})

    # --- M1: zero-level category cannot be submitted -------------------------

    def test_zero_level_category_blocks_submit(self):
        # A category with no configured levels cannot be submitted — it would
        # otherwise strand the request with no actionable line.
        category = self.Category.create({
            'name': 'No levels chain',
            'model_id': self.partner_model_id,
        })
        request = self.Request.create({'category_id': category.id})
        with self.assertRaises(ValidationError):
            request.action_submit()

    # --- L1: refusal reason cleared on resubmit ------------------------------

    def test_resubmit_clears_refusal_reason(self):
        # After refuse → reset → resubmit the stale refusal reason is cleared so
        # the prior decision's text never reappears on the fresh cycle.
        request = self._draft()
        request.with_user(self.requester).action_submit()
        request.refusal_reason = 'Budget exceeded'
        request.with_user(self.approver).action_refuse()
        self.assertEqual(request.state, 'refused')

        request.with_user(self.requester).action_reset()
        request.with_user(self.requester).action_submit()
        self.assertFalse(request.refusal_reason,
                         'Resubmitting must clear the previous refusal reason')
