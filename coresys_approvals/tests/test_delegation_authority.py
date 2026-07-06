# CoreSys Builders — https://coresysbuilders.com
# Copyright (C) CoreSys Builders. License OPL-1.
from datetime import timedelta

from odoo import fields
from odoo.exceptions import AccessError, ValidationError
from odoo.tests.common import TransactionCase


class TestDelegationAuthority(TransactionCase):
    # DEL-03 delegated authority: no widening, self-approval stays blocked to a
    # requester-delegate and a requester-delegator (post-submit toggle), audit
    # attribution to the eligible lowest-id delegator, immutability, the
    # OR-combination read-only rule and the Approver floor.

    @classmethod
    def setUpClass(cls):
        super().setUpClass()
        cls.Category = cls.env['coresys.approval.category']
        cls.Level = cls.env['coresys.approval.level']
        cls.Request = cls.env['coresys.approval.request']
        cls.Response = cls.env['coresys.approval.response']
        cls.Delegation = cls.env['coresys.approval.delegation']
        cls.company = cls.env.company
        cls.partner_model_id = cls.env['ir.model']._get_id('res.partner')

        cls.group_internal = cls.env.ref('base.group_user')
        cls.group_user = cls.env.ref('coresys_approvals.group_approval_user')
        cls.group_approver = cls.env.ref(
            'coresys_approvals.group_approval_approver')

        Users = cls.env['res.users'].with_context(no_reset_password=True)
        # R is created first so R.id < C.id (BLOCKER 1 ordering). R is both a
        # requester and an Approver so it can be a frozen requester-approver.
        cls.user_r = Users.create({
            'name': 'Auth Requester R',
            'login': 'coresys_auth_r',
            'group_ids': [(6, 0, [cls.group_internal.id, cls.group_approver.id,
                                  cls.group_user.id])],
        })
        cls.user_a = Users.create({
            'name': 'Auth Approver A',
            'login': 'coresys_auth_a',
            'group_ids': [(6, 0, [cls.group_internal.id, cls.group_approver.id])],
        })
        cls.user_c = Users.create({
            'name': 'Auth Approver C',
            'login': 'coresys_auth_c',
            'group_ids': [(6, 0, [cls.group_internal.id, cls.group_approver.id])],
        })
        cls.user_b = Users.create({
            'name': 'Auth Delegate B',
            'login': 'coresys_auth_b',
            'group_ids': [(6, 0, [cls.group_internal.id, cls.group_approver.id])],
        })
        # A1 before A2 so A1.id < A2.id (OQ2 ordering).
        cls.user_a1 = Users.create({
            'name': 'Auth Approver A1',
            'login': 'coresys_auth_a1',
            'group_ids': [(6, 0, [cls.group_internal.id, cls.group_approver.id])],
        })
        cls.user_a2 = Users.create({
            'name': 'Auth Approver A2',
            'login': 'coresys_auth_a2',
            'group_ids': [(6, 0, [cls.group_internal.id, cls.group_approver.id])],
        })
        # U is in BOTH the user and approver groups (OR-combination / direct
        # approver cases).
        cls.user_u = Users.create({
            'name': 'Auth Dual User U',
            'login': 'coresys_auth_u',
            'group_ids': [(6, 0, [cls.group_internal.id, cls.group_user.id,
                                  cls.group_approver.id])],
        })
        cls.plain_requester = Users.create({
            'name': 'Auth Plain Requester',
            'login': 'coresys_auth_plain',
            'group_ids': [(6, 0, [cls.group_internal.id, cls.group_user.id])],
        })
        cls.user_only = Users.create({
            'name': 'Auth User Only',
            'login': 'coresys_auth_useronly',
            'group_ids': [(6, 0, [cls.group_internal.id, cls.group_user.id])],
        })

        cls.today = fields.Date.context_today(cls.env.user)

    # ---- helpers -----------------------------------------------------------
    def _deleg(self, delegator, delegate, **extra):
        vals = {
            'delegator_id': delegator.id,
            'delegate_id': delegate.id,
            'date_start': self.today - timedelta(days=1),
            'date_end': self.today + timedelta(days=30),
            'company_id': self.company.id,
        }
        vals.update(extra)
        return self.Delegation.create(vals)

    def _category(self, name, allow_self_approval=False):
        return self.Category.create({
            'name': name,
            'model_id': self.partner_model_id,
            'allow_self_approval': allow_self_approval,
        })

    def _level(self, category, name, approvers, sequence=10):
        return self.Level.create({
            'name': name,
            'sequence': sequence,
            'category_id': category.id,
            'approver_user_ids': [(6, 0, [u.id for u in approvers])],
        })

    def _submit(self, category, requester):
        request = self.Request.create({
            'category_id': category.id,
            'requester_id': requester.id,
        })
        request.with_user(requester).action_submit()
        return request

    def _active_line(self, request):
        return request.line_ids.filtered(
            lambda line: line.status == 'pending').sorted('sequence')[:1]

    def _line(self, request, sequence):
        return request.line_ids.filtered(
            lambda line: line.sequence == sequence)[:1]

    def _approve_response(self, request):
        return request.response_ids.filtered(
            lambda r: r.action == 'approve')[:1]

    # ---- tests -------------------------------------------------------------
    def test_no_widening(self):
        # A delegate of the L1 approver may act on L1 but NOT on L2, whose
        # frozen approver never delegated to them (SC-2, D-23).
        category = self._category('Auth two level')
        self._level(category, 'Level One', [self.user_a], sequence=10)
        self._level(category, 'Level Two', [self.user_c], sequence=20)
        self._deleg(self.user_a, self.user_b)
        request = self._submit(category, self.plain_requester)

        request.with_user(self.user_b).action_approve()
        line2 = self._active_line(request)
        self.assertEqual(line2.name, 'Level Two')
        self.assertNotIn(self.user_b, request._effective_approver_ids(line2))
        with self.assertRaises(AccessError):
            request.with_user(self.user_b).action_approve()

    def test_self_approval_still_blocked(self):
        # With self-approval off, a delegation A->R (R the requester) never lets
        # R approve their own request (D-24).
        category = self._category('Auth self block')
        self._level(category, 'Sole Level', [self.user_a])
        self._deleg(self.user_a, self.user_r)
        request = self._submit(category, self.user_r)
        line = self._active_line(request)
        self.assertNotIn(self.user_r, request._effective_approver_ids(line))
        with self.assertRaises(AccessError):
            request.with_user(self.user_r).action_approve()

    def test_self_approval_requester_delegator(self):
        # HIGH #3 / BLOCKER 2: R is frozen as an approver because self-approval
        # was ON at submit; toggling it OFF mid-flight must strip R's authority
        # so R's delegate B can no longer act and is never attributed to R.
        # This is the real, user-triggerable path the delegator-first filtering
        # defends — not a test-only backdoor.
        category = self._category('Auth requester delegator',
                                  allow_self_approval=True)
        self._level(category, 'Sole Level', [self.user_r])
        self._deleg(self.user_r, self.user_b)
        request = self._submit(category, self.user_r)
        line = self._active_line(request)
        # CONTROL: with self-approval ON, B is effective via R's delegation.
        self.assertIn(self.user_b, request._effective_approver_ids(line))

        # Mid-flight toggle: the only real path to a disqualified frozen
        # requester-approver.
        request.category_id.write({'allow_self_approval': False})
        line = self._active_line(request)
        self.assertNotIn(self.user_b, request._effective_approver_ids(line))
        self.assertFalse(request._delegator_for(line, self.user_b),
                         'B must never be attributable to the disqualified '
                         'requester R')
        with self.assertRaises(AccessError):
            request.with_user(self.user_b).action_approve()

    def test_attribution_excludes_disqualified_delegator(self):
        # BLOCKER 1: R (lower id) and C (higher id) both freeze into the line
        # with self-approval ON; after toggling it OFF, R is disqualified but C
        # stays eligible. B, delegate of both, reaches the decision only via C,
        # so delegated_for must be C — never the lower-id but disqualified R.
        category = self._category('Auth disqualified delegator',
                                  allow_self_approval=True)
        self._level(category, 'Sole Level', [self.user_r, self.user_c])
        self._deleg(self.user_r, self.user_b)
        self._deleg(self.user_c, self.user_b)
        request = self._submit(category, self.user_r)
        self.assertLess(self.user_r.id, self.user_c.id,
                        'Fixture must keep R.id < C.id to expose a raw-set bug')

        request.category_id.write({'allow_self_approval': False})
        request.with_user(self.user_b).action_approve()
        response = self._approve_response(request)
        self.assertEqual(response.user_id, self.user_b)
        self.assertEqual(response.delegated_for, self.user_c,
                         'Attribution must resolve from the eligible set, never '
                         'the disqualified lower-id delegator')

    def test_audit_attribution(self):
        # A delegated decision records the delegate as actor and the delegator
        # as the on-behalf-of party (DEL-03).
        category = self._category('Auth audit')
        self._level(category, 'Sole Level', [self.user_a])
        self._deleg(self.user_a, self.user_b)
        request = self._submit(category, self.plain_requester)
        request.with_user(self.user_b).action_approve()
        response = self._approve_response(request)
        self.assertEqual(response.user_id, self.user_b)
        self.assertEqual(response.delegated_for, self.user_a)
        self.assertEqual(self._line(request, 10).decided_by, self.user_b)

    def test_multi_delegator_deterministic_attribution(self):
        # OQ2: B is delegate of two still-eligible frozen approvers A1 and A2.
        # Attribution records the lowest delegator id regardless of the order the
        # delegation rows were created.
        category = self._category('Auth multi delegator')
        self._level(category, 'Sole Level', [self.user_a1, self.user_a2])
        # Create A2's delegation FIRST so a row-id ordering bug would pick A2.
        self._deleg(self.user_a2, self.user_b)
        self._deleg(self.user_a1, self.user_b)
        request = self._submit(category, self.plain_requester)
        request.with_user(self.user_b).action_approve()
        response = self._approve_response(request)
        self.assertEqual(response.delegated_for, self.user_a1,
                         'Multi-cover attribution must record the lowest '
                         'delegator id, ordered on the delegator not the row')

    def test_direct_approver_no_delegated_for(self):
        # A user who is BOTH a frozen approver and a delegate of another approver
        # records no delegator when acting on their own frozen authority.
        category = self._category('Auth direct approver')
        self._level(category, 'Sole Level', [self.user_a, self.user_u])
        self._deleg(self.user_a, self.user_u)
        request = self._submit(category, self.plain_requester)
        request.with_user(self.user_u).action_approve()
        response = self._approve_response(request)
        self.assertEqual(response.user_id, self.user_u)
        self.assertFalse(response.delegated_for,
                         'A direct frozen approver records no delegated_for')

    def test_delegated_for_immutable(self):
        # delegated_for is create-only: the unconditional response guard blocks
        # rewriting it, and the record cannot be unlinked outside uninstall.
        category = self._category('Auth immutable')
        self._level(category, 'Sole Level', [self.user_a])
        self._deleg(self.user_a, self.user_b)
        request = self._submit(category, self.plain_requester)
        request.with_user(self.user_b).action_approve()
        response = self._approve_response(request)
        self.assertEqual(response.delegated_for, self.user_a)
        with self.assertRaises(AccessError):
            response.write({'delegated_for': self.user_c.id})
        with self.assertRaises(AccessError):
            response.unlink()

    def test_delegate_read_no_write_or_combination(self):
        # A user in both the user and approver groups who is only the DELEGATE of
        # a delegation owned by another approver cannot write it — the read-only
        # delegate rule must not OR-combine into write authority.
        deleg = self._deleg(self.user_a, self.user_u)
        readable = self.Delegation.with_user(self.user_u).search(
            [('id', '=', deleg.id)])
        self.assertIn(deleg, readable,
                      'The delegate must be able to READ the delegation')
        with self.assertRaises(AccessError):
            deleg.with_user(self.user_u).write(
                {'date_end': self.today + timedelta(days=60)})

    def test_user_only_delegate_not_effective(self):
        # OQ1 floor: a User-only (non-Approver) internal delegate is barred. The
        # save-time constraint enforces the floor, and the resolver never returns
        # such a user either.
        with self.assertRaises(ValidationError):
            self._deleg(self.user_a, self.user_only)
        # Resolution-side floor: a user-only account is never an active delegate.
        delegates = self.Delegation._active_delegates_for(
            self.user_a.ids, self.company, self.env['coresys.approval.category'])
        self.assertNotIn(self.user_only, delegates)
