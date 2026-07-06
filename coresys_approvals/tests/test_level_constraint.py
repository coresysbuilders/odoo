# CoreSys Builders — https://coresysbuilders.com
# Copyright (C) CoreSys Builders. License OPL-1.
from odoo.exceptions import AccessError, ValidationError
from odoo.tests.common import TransactionCase


class TestApprovalLevelConstraint(TransactionCase):

    @classmethod
    def setUpClass(cls):
        super().setUpClass()
        cls.Category = cls.env['coresys.approval.category']
        cls.Level = cls.env['coresys.approval.level']
        cls.partner_model_id = cls.env['ir.model']._get_id('res.partner')

        cls.group_approver = cls.env.ref('coresys_approvals.group_approval_approver')
        cls.approver_user = cls.env['res.users'].with_context(no_reset_password=True).create({
            'name': 'Finance Approver',
            'login': 'coresys_level_approver',
        })

        cls.category = cls.Category.create({
            'name': 'Purchase Orders',
            'model_id': cls.partner_model_id,
        })

    def test_level_without_approver_is_rejected(self):
        # A level with neither an approver group nor specific approvers is
        # rejected at save; the error names the offending level.
        with self.assertRaises(ValidationError) as caught:
            self.Level.create({
                'name': 'Finance',
                'category_id': self.category.id,
            })
        self.assertIn('Finance', str(caught.exception),
                      'The validation error must name the offending level')

    def test_level_with_group_only_saves(self):
        level = self.Level.create({
            'name': 'Manager',
            'category_id': self.category.id,
            'approver_group_id': self.group_approver.id,
        })
        self.assertTrue(level.id)

    def test_level_with_users_only_saves(self):
        level = self.Level.create({
            'name': 'Finance',
            'category_id': self.category.id,
            'approver_user_ids': [(6, 0, [self.approver_user.id])],
        })
        self.assertTrue(level.id)

    def test_level_with_group_and_users_saves(self):
        # Group and specific approvers form a union (D-06).
        level = self.Level.create({
            'name': 'Directors',
            'category_id': self.category.id,
            'approver_group_id': self.group_approver.id,
            'approver_user_ids': [(6, 0, [self.approver_user.id])],
        })
        self.assertTrue(level.id)

    def test_levels_ordered_by_sequence(self):
        category = self.Category.create({
            'name': 'Ordered chain',
            'model_id': self.partner_model_id,
            'level_ids': [
                (0, 0, {
                    'name': 'Third',
                    'sequence': 30,
                    'approver_group_id': self.group_approver.id,
                }),
                (0, 0, {
                    'name': 'First',
                    'sequence': 10,
                    'approver_group_id': self.group_approver.id,
                }),
                (0, 0, {
                    'name': 'Second',
                    'sequence': 20,
                    'approver_group_id': self.group_approver.id,
                }),
            ],
        })
        # Read back via search so the model _order ('sequence, id') is applied
        # from the database: a one2many accessed in the same transaction it was
        # created in can return cache/insertion order, not the persisted order.
        ordered = self.Level.search([('category_id', '=', category.id)])
        self.assertEqual(
            ordered.mapped('name'),
            ['First', 'Second', 'Third'],
            'Levels must be returned ordered by ascending sequence',
        )


class TestApprovalLevelSecurity(TransactionCase):

    @classmethod
    def setUpClass(cls):
        super().setUpClass()
        cls.Category = cls.env['coresys.approval.category']
        cls.Level = cls.env['coresys.approval.level']
        cls.partner_model_id = cls.env['ir.model']._get_id('res.partner')
        cls.group_approver = cls.env.ref('coresys_approvals.group_approval_approver')

        cls.group_internal = cls.env.ref('base.group_user')
        cls.group_user = cls.env.ref('coresys_approvals.group_approval_user')
        cls.group_manager = cls.env.ref('coresys_approvals.group_approval_manager')

        Users = cls.env['res.users'].with_context(no_reset_password=True)
        cls.user_reader = Users.create({
            'name': 'Level Reader',
            'login': 'coresys_level_reader',
            'group_ids': [(6, 0, [cls.group_internal.id, cls.group_user.id])],
        })
        cls.user_manager = Users.create({
            'name': 'Level Manager',
            'login': 'coresys_level_manager',
            'group_ids': [(6, 0, [cls.group_internal.id, cls.group_manager.id])],
        })
        cls.user_plain = Users.create({
            'name': 'Level Plain',
            'login': 'coresys_level_plain',
            'group_ids': [(6, 0, [cls.group_internal.id])],
        })

        cls.category = cls.Category.create({
            'name': 'Secured chain',
            'model_id': cls.partner_model_id,
        })
        cls.level = cls.Level.create({
            'name': 'Manager',
            'category_id': cls.category.id,
            'approver_group_id': cls.group_approver.id,
        })

    def test_read_only_user_cannot_write_levels(self):
        # A group_approval_user-only user has read-only access: create, write
        # and unlink on levels must all be denied.
        with self.assertRaises(AccessError):
            self.Level.with_user(self.user_reader).create({
                'name': 'Blocked',
                'category_id': self.category.id,
                'approver_group_id': self.group_approver.id,
            })
        with self.assertRaises(AccessError):
            self.level.with_user(self.user_reader).write({'name': 'Renamed'})
        with self.assertRaises(AccessError):
            self.level.with_user(self.user_reader).unlink()

    def test_manager_can_create_level(self):
        level = self.Level.with_user(self.user_manager).create({
            'name': 'Finance',
            'category_id': self.category.id,
            'approver_group_id': self.group_approver.id,
        })
        self.assertTrue(level.id)

    def test_no_group_user_has_no_read(self):
        with self.assertRaises(AccessError):
            self.level.with_user(self.user_plain).read(['name'])


class TestApprovalLevelCompanyScope(TransactionCase):

    @classmethod
    def setUpClass(cls):
        super().setUpClass()
        cls.Category = cls.env['coresys.approval.category']
        cls.Level = cls.env['coresys.approval.level']
        cls.partner_model_id = cls.env['ir.model']._get_id('res.partner')
        cls.group_approver = cls.env.ref('coresys_approvals.group_approval_approver')

        Company = cls.env['res.company']
        cls.company_a = Company.create({'name': 'CoreSys Company A'})
        cls.company_b = Company.create({'name': 'CoreSys Company B'})

        cls.group_internal = cls.env.ref('base.group_user')
        cls.group_manager = cls.env.ref('coresys_approvals.group_approval_manager')

        # A NON-admin manager whose allowed companies is Company B only.
        cls.user_b = cls.env['res.users'].with_context(no_reset_password=True).create({
            'name': 'Company B Manager',
            'login': 'coresys_level_company_b',
            'company_id': cls.company_b.id,
            'company_ids': [(6, 0, [cls.company_b.id])],
            'group_ids': [(6, 0, [cls.group_internal.id, cls.group_manager.id])],
        })

        cls.category_a = cls.Category.create({
            'name': 'Company A scoped',
            'model_id': cls.partner_model_id,
            'company_id': cls.company_a.id,
        })
        cls.level_a = cls.Level.create({
            'name': 'Company A level',
            'category_id': cls.category_a.id,
            'approver_group_id': cls.group_approver.id,
        })
        cls.category_global = cls.Category.create({
            'name': 'Org-wide',
            'model_id': cls.partner_model_id,
            'company_id': False,
        })
        cls.level_global = cls.Level.create({
            'name': 'Org-wide level',
            'category_id': cls.category_global.id,
            'approver_group_id': cls.group_approver.id,
        })

    def test_level_company_id_follows_category(self):
        self.assertEqual(self.level_a.company_id, self.company_a,
                         "Level company_id must follow its category")

    def test_company_b_user_cannot_see_company_a_level(self):
        visible = self.Level.with_user(self.user_b).with_context(
            allowed_company_ids=[self.company_b.id]
        ).search([])
        self.assertNotIn(self.level_a, visible,
                         'Company-A level must not leak to a Company-B user')
        self.assertIn(self.level_global, visible,
                      'Org-wide level must be visible to every company')

    def test_category_company_change_recomputes_level(self):
        category = self.Category.create({
            'name': 'Reassigned chain',
            'model_id': self.partner_model_id,
            'company_id': self.company_a.id,
        })
        level = self.Level.create({
            'name': 'Step',
            'category_id': category.id,
            'approver_group_id': self.group_approver.id,
        })
        self.assertEqual(level.company_id, self.company_a)
        category.company_id = self.company_b
        self.assertEqual(level.company_id, self.company_b,
                         "Level company_id must recompute when the category "
                         "company changes")
