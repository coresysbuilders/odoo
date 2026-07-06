# CoreSys Builders — https://coresysbuilders.com
# Copyright (C) CoreSys Builders. License OPL-1.
from odoo.exceptions import ValidationError
from odoo.tests.common import TransactionCase


class TestApprovalRequestSubmit(TransactionCase):

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

        Users = cls.env['res.users'].with_context(no_reset_password=True)
        cls.approver_user = Users.create({
            'name': 'Finance Approver',
            'login': 'coresys_req_approver',
            'groups_id': [(6, 0, [cls.group_internal.id, cls.group_approver.id])],
        })
        cls.other_user = Users.create({
            'name': 'Other Approver',
            'login': 'coresys_req_other',
            'groups_id': [(6, 0, [cls.group_internal.id, cls.group_approver.id])],
        })

        cls.category = cls.Category.create({
            'name': 'Purchase Orders',
            'model_id': cls.partner_model_id,
        })
        cls.level = cls.Level.create({
            'name': 'Finance',
            'category_id': cls.category.id,
            'approver_user_ids': [(6, 0, [cls.approver_user.id])],
        })

        cls.partner = cls.env['res.partner'].create({'name': 'Referenced Partner'})

    def _make_request(self, category=None):
        return self.Request.create({
            'category_id': (category or self.category).id,
        })

    def test_snapshot_is_frozen(self):
        # Submit freezes the resolved approver set; later edits to the level's
        # approver source never alter an in-flight request's snapshot.
        request = self._make_request()
        request.action_submit()
        frozen = request.line_ids.approver_user_ids
        self.assertEqual(frozen, self.approver_user)

        self.level.approver_user_ids = [(6, 0, [self.other_user.id])]
        self.assertEqual(
            request.line_ids.approver_user_ids, self.approver_user,
            'Editing the level after submit must not change the snapshot',
        )

    def test_empty_level_blocks_submit(self):
        # A level whose only approver is an empty group blocks submission with a
        # ValidationError naming the offending level (D-11).
        empty_group = self.env['res.groups'].create({'name': 'Empty Approver Group'})
        category = self.Category.create({
            'name': 'Empty-level chain',
            'model_id': self.partner_model_id,
        })
        self.Level.create({
            'name': 'Nobody',
            'category_id': category.id,
            'approver_group_id': empty_group.id,
        })
        request = self._make_request(category=category)
        with self.assertRaises(ValidationError) as caught:
            request.action_submit()
        self.assertIn('Nobody', str(caught.exception),
                      'The empty-level error must name the offending level')

    def test_self_approval_excluded(self):
        # With self-approval off the requester is excluded from the eligible set;
        # if that empties the level, submission is blocked. With self-approval on
        # the same submit succeeds and the line contains the requester.
        Users = self.env['res.users'].with_context(no_reset_password=True)
        requester = Users.create({
            'name': 'Self Requester',
            'login': 'coresys_req_self',
            'groups_id': [(6, 0, [self.group_internal.id, self.group_approver.id])],
        })
        category = self.Category.create({
            'name': 'Self-approval chain',
            'model_id': self.partner_model_id,
            'allow_self_approval': False,
        })
        self.Level.create({
            'name': 'Self Only',
            'category_id': category.id,
            'approver_user_ids': [(6, 0, [requester.id])],
        })
        request = self.Request.create({
            'category_id': category.id,
            'requester_id': requester.id,
        })
        with self.assertRaises(ValidationError):
            request.with_user(requester).action_submit()

        category.allow_self_approval = True
        request.with_user(requester).action_submit()
        self.assertEqual(request.state, 'to_approve')
        self.assertEqual(request.line_ids.approver_user_ids, requester)

    def test_reference_type_check(self):
        # A reference to a record whose model differs from the category target
        # raises at save; a reference of the correct model saves (D-10).
        company = self.env['res.company'].create({'name': 'Wrong Model Co'})
        with self.assertRaises(ValidationError):
            self.Request.create({
                'category_id': self.category.id,
                'reference': 'res.company,%d' % company.id,
            })
        request = self.Request.create({
            'category_id': self.category.id,
            'reference': 'res.partner,%d' % self.partner.id,
        })
        self.assertTrue(request.id)

    def test_submit_sets_state_and_lines(self):
        # After submit the state is to_approve and one pending snapshot line
        # exists per category level, ordered by sequence.
        category = self.Category.create({
            'name': 'Two-level chain',
            'model_id': self.partner_model_id,
        })
        self.Level.create({
            'name': 'Second',
            'sequence': 20,
            'category_id': category.id,
            'approver_user_ids': [(6, 0, [self.other_user.id])],
        })
        self.Level.create({
            'name': 'First',
            'sequence': 10,
            'category_id': category.id,
            'approver_user_ids': [(6, 0, [self.approver_user.id])],
        })
        request = self._make_request(category=category)
        request.action_submit()
        self.assertEqual(request.state, 'to_approve')
        self.assertEqual(request.line_ids.mapped('name'), ['First', 'Second'])
        self.assertEqual(set(request.line_ids.mapped('status')), {'pending'})

    def test_plain_requester_can_submit(self):
        # A plain Approval User (no line create/write/unlink ACL) can submit their
        # own request end-to-end — snapshot lines are born through the engine's
        # narrow post-authorization sudo, not the user's absent line ACL.
        Users = self.env['res.users'].with_context(no_reset_password=True)
        requester = Users.create({
            'name': 'Plain Requester',
            'login': 'coresys_req_plain',
            'groups_id': [(6, 0, [self.group_internal.id, self.group_user.id])],
        })
        category = self.Category.create({
            'name': 'Plain requester chain',
            'model_id': self.partner_model_id,
        })
        self.Level.create({
            'name': 'Distinct Approver',
            'category_id': category.id,
            'approver_user_ids': [(6, 0, [self.approver_user.id])],
        })
        request = self.Request.with_user(requester).create({
            'category_id': category.id,
        })
        request.with_user(requester).action_submit()
        self.assertEqual(request.state, 'to_approve')
        self.assertTrue(request.line_ids)
        self.assertEqual(request.line_ids.approver_user_ids, self.approver_user)

    def test_cross_company_member_not_snapshotted(self):
        # A group member whose companies exclude the request's company is never
        # snapshotted into a company-scoped request's eligible set (finding #8).
        Company = self.env['res.company']
        company_a = Company.create({'name': 'Snapshot Company A'})
        company_b = Company.create({'name': 'Snapshot Company B'})

        Users = self.env['res.users'].with_context(no_reset_password=True)
        member_b = Users.create({
            'name': 'Company B Only Member',
            'login': 'coresys_req_company_b',
            'company_id': company_b.id,
            'company_ids': [(6, 0, [company_b.id])],
            'groups_id': [(6, 0, [self.group_internal.id, self.group_approver.id])],
        })
        approver_a = Users.create({
            'name': 'Company A Approver',
            'login': 'coresys_req_company_a',
            'company_id': company_a.id,
            'company_ids': [(6, 0, [company_a.id])],
            'groups_id': [(6, 0, [self.group_internal.id, self.group_approver.id])],
        })
        cross_group = self.env['res.groups'].create({
            'name': 'Cross Company Group',
            'users': [(6, 0, [member_b.id])],
        })

        category = self.Category.create({
            'name': 'Company A cross chain',
            'model_id': self.partner_model_id,
            'company_id': company_a.id,
        })
        self.Level.create({
            'name': 'Company A level',
            'category_id': category.id,
            'approver_group_id': cross_group.id,
            'approver_user_ids': [(6, 0, [approver_a.id])],
        })
        request = self.Request.create({
            'category_id': category.id,
            'company_id': company_a.id,
        })
        request.action_submit()
        snapshot = request.line_ids.approver_user_ids
        self.assertIn(approver_a, snapshot,
                      'The Company-A approver must be snapshotted')
        self.assertNotIn(member_b, snapshot,
                         'A Company-B-only member must not be snapshotted into '
                         'a Company-A request')
