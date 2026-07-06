# CoreSys Builders — https://coresysbuilders.com
# Copyright (C) CoreSys Builders. License OPL-1.
from odoo.exceptions import AccessError
from odoo.tests.common import TransactionCase


class TestApprovalGroups(TransactionCase):

    def test_groups_exist(self):
        user = self.env.ref('coresys_approvals.group_approval_user')
        approver = self.env.ref('coresys_approvals.group_approval_approver')
        manager = self.env.ref('coresys_approvals.group_approval_manager')
        self.assertTrue(user, 'Approval User group must exist')
        self.assertTrue(approver, 'Approval Approver group must exist')
        self.assertTrue(manager, 'Approval Manager group must exist')

    def test_implied_ids_chain(self):
        user = self.env.ref('coresys_approvals.group_approval_user')
        approver = self.env.ref('coresys_approvals.group_approval_approver')
        manager = self.env.ref('coresys_approvals.group_approval_manager')
        # Manager implies Approver implies User (direct links).
        self.assertIn(approver, manager.implied_ids)
        self.assertIn(user, approver.implied_ids)
        # Transitive closure: a Manager also holds User rights.
        self.assertIn(user, manager.all_implied_ids)


class TestApprovalCategory(TransactionCase):

    @classmethod
    def setUpClass(cls):
        super().setUpClass()
        cls.Category = cls.env['coresys.approval.category']
        cls.partner_model_id = cls.env['ir.model']._get_id('res.partner')

    def test_category_name_roundtrip(self):
        category = self.Category.create({
            'name': 'Purchase Orders',
            'model_id': self.partner_model_id,
        })
        self.assertEqual(category.name, 'Purchase Orders')

    def test_category_config_fields(self):
        category = self.Category.create({
            'name': 'High-value partners',
            'model_id': self.partner_model_id,
        })
        self.assertEqual(category.model_name, 'res.partner')
        self.assertFalse(category.allow_self_approval, 'Self-approval must default to off')
        self.assertFalse(category.company_id, 'Category must be org-wide (no company) by default')


class TestApprovalCategorySecurity(TransactionCase):

    @classmethod
    def setUpClass(cls):
        super().setUpClass()
        cls.Category = cls.env['coresys.approval.category']
        cls.partner_model_id = cls.env['ir.model']._get_id('res.partner')

        cls.group_user = cls.env.ref('coresys_approvals.group_approval_user')
        cls.group_manager = cls.env.ref('coresys_approvals.group_approval_manager')
        cls.group_internal = cls.env.ref('base.group_user')

        Users = cls.env['res.users'].with_context(no_reset_password=True)
        cls.user_reader = Users.create({
            'name': 'Approval Reader',
            'login': 'coresys_reader',
            'group_ids': [(6, 0, [cls.group_internal.id, cls.group_user.id])],
        })
        cls.user_manager = Users.create({
            'name': 'Approval Manager',
            'login': 'coresys_manager',
            'group_ids': [(6, 0, [cls.group_internal.id, cls.group_manager.id])],
        })
        cls.user_plain = Users.create({
            'name': 'Plain Internal',
            'login': 'coresys_plain',
            'group_ids': [(6, 0, [cls.group_internal.id])],
        })

    def test_read_only_user_cannot_write(self):
        # A group_approval_user-only user has read-only access: create, write
        # and unlink must all be denied.
        with self.assertRaises(AccessError):
            self.Category.with_user(self.user_reader).create({
                'name': 'Blocked create',
                'model_id': self.partner_model_id,
            })

        category = self.Category.create({
            'name': 'Existing',
            'model_id': self.partner_model_id,
        })
        with self.assertRaises(AccessError):
            category.with_user(self.user_reader).write({'name': 'Renamed'})
        with self.assertRaises(AccessError):
            category.with_user(self.user_reader).unlink()

    def test_manager_can_create(self):
        category = self.Category.with_user(self.user_manager).create({
            'name': 'Manager owned',
            'model_id': self.partner_model_id,
        })
        self.assertTrue(category.id)

    def test_no_group_user_has_no_read(self):
        category = self.Category.create({
            'name': 'Hidden from plain user',
            'model_id': self.partner_model_id,
        })
        with self.assertRaises(AccessError):
            category.with_user(self.user_plain).read(['name'])

    def test_manager_can_read_metadata(self):
        # Model targeting + the domain widget require read on ir.model and
        # ir.model.fields for a non-Settings Approval Manager.
        self.assertTrue(
            self.env['ir.model'].with_user(self.user_manager).search([], limit=1)
        )
        self.assertTrue(
            self.env['ir.model.fields'].with_user(self.user_manager).search([], limit=1)
        )
        # ir.model.fields.selection read is granted too (used by the domain
        # widget for selection fields); assert it does not raise.
        self.env['ir.model.fields.selection'].with_user(self.user_manager).search([], limit=1)


class TestApprovalCategoryCompanyScope(TransactionCase):

    @classmethod
    def setUpClass(cls):
        super().setUpClass()
        cls.Category = cls.env['coresys.approval.category']
        cls.partner_model_id = cls.env['ir.model']._get_id('res.partner')

        Company = cls.env['res.company']
        cls.company_a = Company.create({'name': 'CoreSys Company A'})
        cls.company_b = Company.create({'name': 'CoreSys Company B'})

        cls.group_internal = cls.env.ref('base.group_user')
        cls.group_manager = cls.env.ref('coresys_approvals.group_approval_manager')

        # A NON-admin manager whose allowed companies is Company B only.
        cls.user_b = cls.env['res.users'].with_context(no_reset_password=True).create({
            'name': 'Company B Manager',
            'login': 'coresys_company_b',
            'company_id': cls.company_b.id,
            'company_ids': [(6, 0, [cls.company_b.id])],
            'group_ids': [(6, 0, [cls.group_internal.id, cls.group_manager.id])],
        })

        cls.category_a = cls.Category.create({
            'name': 'Company A scoped',
            'model_id': cls.partner_model_id,
            'company_id': cls.company_a.id,
        })
        cls.category_global = cls.Category.create({
            'name': 'Org-wide',
            'model_id': cls.partner_model_id,
            'company_id': False,
        })

    def test_company_b_user_cannot_see_company_a_config(self):
        visible = self.Category.with_user(self.user_b).with_context(
            allowed_company_ids=[self.company_b.id]
        ).search([])
        self.assertNotIn(self.category_a, visible,
                         'Company-A config must not leak to a Company-B user')
        self.assertIn(self.category_global, visible,
                      'Org-wide config must be visible to every company')
