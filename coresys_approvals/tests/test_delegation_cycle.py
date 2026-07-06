# CoreSys Builders — https://coresysbuilders.com
# Copyright (C) CoreSys Builders. License OPL-1.
from datetime import timedelta

from odoo import fields
from odoo.exceptions import AccessError, ValidationError
from odoo.tests.common import TransactionCase


class TestDelegationCycle(TransactionCase):
    # DEL-02 cycle detection plus the four save-time constraint rejections
    # (window, invalid delegate, cross-company participant, non-self delegator).

    @classmethod
    def setUpClass(cls):
        super().setUpClass()
        cls.Delegation = cls.env['coresys.approval.delegation']
        cls.company = cls.env.company

        cls.group_internal = cls.env.ref('base.group_user')
        cls.group_portal = cls.env.ref('base.group_portal')
        cls.group_user = cls.env.ref('coresys_approvals.group_approval_user')
        cls.group_approver = cls.env.ref(
            'coresys_approvals.group_approval_approver')
        cls.group_manager = cls.env.ref(
            'coresys_approvals.group_approval_manager')

        Users = cls.env['res.users'].with_context(no_reset_password=True)
        cls.user_a = Users.create({
            'name': 'Cycle Approver A',
            'login': 'coresys_cycle_a',
            'group_ids': [(6, 0, [cls.group_internal.id, cls.group_approver.id])],
        })
        cls.user_b = Users.create({
            'name': 'Cycle Approver B',
            'login': 'coresys_cycle_b',
            'group_ids': [(6, 0, [cls.group_internal.id, cls.group_approver.id])],
        })
        cls.portal = Users.create({
            'name': 'Cycle Portal',
            'login': 'coresys_cycle_portal',
            'group_ids': [(6, 0, [cls.group_portal.id])],
        })
        cls.inactive = Users.create({
            'name': 'Cycle Inactive Approver',
            'login': 'coresys_cycle_inactive',
            'active': False,
            'group_ids': [(6, 0, [cls.group_internal.id, cls.group_approver.id])],
        })
        cls.user_only = Users.create({
            'name': 'Cycle User Only',
            'login': 'coresys_cycle_useronly',
            'group_ids': [(6, 0, [cls.group_internal.id, cls.group_user.id])],
        })
        cls.manager = Users.create({
            'name': 'Cycle Manager',
            'login': 'coresys_cycle_manager',
            'group_ids': [(6, 0, [cls.group_internal.id, cls.group_manager.id])],
        })

        cls.today = fields.Date.context_today(cls.env.user)

    def _vals(self, delegator, delegate, start_offset=0, end_offset=30,
              **extra):
        vals = {
            'delegator_id': delegator.id,
            'delegate_id': delegate.id,
            'date_start': self.today + timedelta(days=start_offset),
            'date_end': self.today + timedelta(days=end_offset),
            'company_id': self.company.id,
        }
        vals.update(extra)
        return vals

    def test_self_delegation_rejected(self):
        # A delegator cannot hand authority to themselves (_check_no_cycle).
        with self.assertRaises(ValidationError):
            self.Delegation.create(self._vals(self.user_a, self.user_a))

    def test_reciprocal_pair_rejected(self):
        # An active A->B and an overlapping active B->A in the same company form
        # the only representable loop and the second save is rejected.
        self.Delegation.create(self._vals(self.user_a, self.user_b))
        with self.assertRaises(ValidationError):
            self.Delegation.create(self._vals(self.user_b, self.user_a))

    def test_non_overlapping_reciprocal_allowed(self):
        # A B->A whose window does not overlap the A->B window saves cleanly —
        # the cycle guard is interval-scoped, not a blanket ban on the pair.
        self.Delegation.create(
            self._vals(self.user_a, self.user_b, start_offset=0, end_offset=10))
        reverse = self.Delegation.create(
            self._vals(self.user_b, self.user_a, start_offset=20, end_offset=30))
        self.assertTrue(reverse.id,
                        'A non-overlapping reciprocal delegation must be allowed')

    def test_end_before_start_rejected(self):
        # date_end < date_start is rejected by _check_window.
        with self.assertRaises(ValidationError):
            self.Delegation.create(
                self._vals(self.user_a, self.user_b, start_offset=0,
                           end_offset=-5))

    def test_invalid_delegate_rejected(self):
        # A portal (share), an inactive, and a User-only (non-Approver) delegate
        # are each rejected at save, not merely ignored at resolution.
        with self.assertRaises(ValidationError):
            self.Delegation.create(self._vals(self.user_a, self.portal))
        with self.assertRaises(ValidationError):
            self.Delegation.create(self._vals(self.user_a, self.inactive))
        with self.assertRaises(ValidationError):
            self.Delegation.create(self._vals(self.user_a, self.user_only))

    def test_out_of_company_participant_rejected(self):
        # A delegate or delegator that is not a member of company_id is rejected
        # by _check_company_membership (review HIGH #1 / SEC-03).
        Company = self.env['res.company']
        company_b = Company.create({'name': 'Cycle Company B'})
        Users = self.env['res.users'].with_context(no_reset_password=True)
        outsider = Users.create({
            'name': 'Cycle Company B Approver',
            'login': 'coresys_cycle_company_b',
            'company_id': company_b.id,
            'company_ids': [(6, 0, [company_b.id])],
            'group_ids': [(6, 0, [self.group_internal.id, self.group_approver.id])],
        })
        # Delegate outside company_id (which defaults to the main company).
        with self.assertRaises(ValidationError):
            self.Delegation.create(self._vals(self.user_a, outsider))
        # Delegator outside company_id.
        with self.assertRaises(ValidationError):
            self.Delegation.create(self._vals(outsider, self.user_a))

    def test_non_manager_delegator_must_be_self(self):
        # A non-manager Approver may only mint a delegation where they are the
        # delegator; a manager may set any delegator (review Gemini-HIGH). On 19.0
        # the "delegator must be self" record rule pre-empts the Python constraint,
        # so the rejection surfaces as AccessError; Odoo's assertRaises wraps the
        # AccessError case in a savepoint so the manager create below still runs.
        with self.assertRaises(AccessError):
            self.Delegation.with_user(self.user_a).create(
                self._vals(self.user_b, self.user_a))
        # A manager sets an arbitrary delegator without error.
        managed = self.Delegation.with_user(self.manager).create(
            self._vals(self.user_a, self.user_b))
        self.assertTrue(managed.id,
                        'A manager may create a delegation for any delegator')
