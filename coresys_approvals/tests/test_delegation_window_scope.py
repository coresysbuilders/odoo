# CoreSys Builders — https://coresysbuilders.com
# Copyright (C) CoreSys Builders. License OPL-1.
from datetime import timedelta

from odoo import fields
from odoo.tests.common import TransactionCase


class TestDelegationWindowScope(TransactionCase):
    # DEL-01/02 window + scope activation, SEC-03 company isolation, mid-flight
    # inbox surfacing, stale-visibility reconciliation and idempotent notify.

    @classmethod
    def setUpClass(cls):
        super().setUpClass()
        cls.Category = cls.env['coresys.approval.category']
        cls.Level = cls.env['coresys.approval.level']
        cls.Request = cls.env['coresys.approval.request']
        cls.Delegation = cls.env['coresys.approval.delegation']
        cls.Activity = cls.env['mail.activity']
        cls.company = cls.env.company
        cls.partner_model_id = cls.env['ir.model']._get_id('res.partner')
        cls.engine_type = cls.env.ref('coresys_approvals.mail_act_approval_todo')

        cls.group_internal = cls.env.ref('base.group_user')
        cls.group_user = cls.env.ref('coresys_approvals.group_approval_user')
        cls.group_approver = cls.env.ref(
            'coresys_approvals.group_approval_approver')

        Users = cls.env['res.users'].with_context(no_reset_password=True)
        cls.requester = Users.create({
            'name': 'Scope Requester',
            'login': 'coresys_scope_requester',
            'group_ids': [(6, 0, [cls.group_internal.id, cls.group_user.id])],
        })
        cls.user_a = Users.create({
            'name': 'Scope Approver A',
            'login': 'coresys_scope_a',
            'group_ids': [(6, 0, [cls.group_internal.id, cls.group_approver.id])],
        })
        cls.user_b = Users.create({
            'name': 'Scope Delegate B',
            'login': 'coresys_scope_b',
            'group_ids': [(6, 0, [cls.group_internal.id, cls.group_approver.id])],
        })

        cls.category = cls.Category.create({
            'name': 'Scope single chain',
            'model_id': cls.partner_model_id,
        })
        cls.Level.create({
            'name': 'Sole Level',
            'category_id': cls.category.id,
            'approver_user_ids': [(6, 0, [cls.user_a.id])],
        })
        # A second, unrelated category used only for the out-of-scope test.
        cls.other_category = cls.Category.create({
            'name': 'Scope other chain',
            'model_id': cls.partner_model_id,
        })

        cls.today = fields.Date.context_today(cls.env.user)

    # ---- helpers -----------------------------------------------------------
    def _vals(self, delegator, delegate, start_offset=-1, end_offset=30,
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

    def _submit(self, category=None, requester=None):
        request = self.Request.create({
            'category_id': (category or self.category).id,
            'requester_id': (requester or self.requester).id,
        })
        request.with_user(requester or self.requester).action_submit()
        return request

    def _active_line(self, request):
        return request.line_ids.filtered(
            lambda line: line.status == 'pending').sorted('sequence')[:1]

    def _awaiting(self, request):
        # Force a delegation-aware recompute of the stored inbox field: the
        # compute method reads delegations live, so invalidate+read is robust
        # even though a plain delegation write leaves no field dependency dirty.
        request.invalidate_recordset(['active_approver_user_ids'])
        return request.active_approver_user_ids

    def _engine_todos(self, request, user):
        return self.Activity.search([
            ('res_model', '=', 'coresys.approval.request'),
            ('res_id', '=', request.id),
            ('activity_type_id', '=', self.engine_type.id),
            ('user_id', '=', user.id),
        ])

    # ---- tests -------------------------------------------------------------
    def test_in_window_in_scope_delegate_effective(self):
        # An active, in-window, category-matching delegate is effective and can
        # act in the delegator's place.
        self.Delegation.create(self._vals(self.user_a, self.user_b))
        request = self._submit()
        line = self._active_line(request)
        self.assertIn(self.user_b, request._effective_approver_ids(line))
        request.with_user(self.user_b).action_approve()
        self.assertEqual(request.state, 'approved')

    def test_out_of_window_ignored(self):
        # A delegation whose window ends before today makes no one effective.
        self.Delegation.create(
            self._vals(self.user_a, self.user_b, start_offset=-30,
                       end_offset=-2))
        request = self._submit()
        line = self._active_line(request)
        self.assertNotIn(self.user_b, request._effective_approver_ids(line))

    def test_out_of_category_ignored(self):
        # A delegation scoped to a different category does not surface B here.
        self.Delegation.create(
            self._vals(self.user_a, self.user_b,
                       category_id=self.other_category.id))
        request = self._submit()
        line = self._active_line(request)
        self.assertNotIn(self.user_b, request._effective_approver_ids(line))

    def test_archived_delegation_ignored(self):
        # An archived (active=False) delegation is never effective.
        self.Delegation.create(
            self._vals(self.user_a, self.user_b, active=False))
        request = self._submit()
        line = self._active_line(request)
        self.assertNotIn(self.user_b, request._effective_approver_ids(line))

    def test_company_isolation(self):
        # A delegation booked in company A cannot surface its delegate on a
        # company-B request, even for a delegator who belongs to both (SEC-03).
        Company = self.env['res.company']
        company_a = Company.create({'name': 'Scope Company A'})
        company_b = Company.create({'name': 'Scope Company B'})
        Users = self.env['res.users'].with_context(no_reset_password=True)
        member = Users.create({
            'name': 'Scope Multi Approver',
            'login': 'coresys_scope_multi',
            'company_id': company_b.id,
            'company_ids': [(6, 0, [company_a.id, company_b.id])],
            'group_ids': [(6, 0, [self.group_internal.id, self.group_approver.id])],
        })
        delegate = Users.create({
            'name': 'Scope Multi Delegate',
            'login': 'coresys_scope_multi_delegate',
            'company_id': company_b.id,
            'company_ids': [(6, 0, [company_a.id, company_b.id])],
            'group_ids': [(6, 0, [self.group_internal.id, self.group_approver.id])],
        })
        requester_b = Users.create({
            'name': 'Scope Company B Requester',
            'login': 'coresys_scope_company_b_req',
            'company_id': company_b.id,
            'company_ids': [(6, 0, [company_b.id])],
            'group_ids': [(6, 0, [self.group_internal.id, self.group_user.id])],
        })
        category_b = self.Category.create({
            'name': 'Scope Company B chain',
            'model_id': self.partner_model_id,
            'company_id': company_b.id,
        })
        self.Level.create({
            'name': 'Company B level',
            'category_id': category_b.id,
            'approver_user_ids': [(6, 0, [member.id])],
        })
        request = self.Request.create({
            'category_id': category_b.id,
            'company_id': company_b.id,
            'requester_id': requester_b.id,
        })
        request.with_user(requester_b).action_submit()
        line = self._active_line(request)

        # Delegation booked in company A must NOT surface the delegate on the
        # company-B request.
        self.Delegation.create(self._vals(
            member, delegate, company_id=company_a.id))
        self.assertNotIn(delegate, request._effective_approver_ids(line),
                         'A company-A delegation must not leak into a '
                         'company-B request')
        # Same delegator/delegate booked in company B DOES surface — proving the
        # exclusion above is the company boundary, not a broken fixture.
        self.Delegation.create(self._vals(
            member, delegate, company_id=company_b.id))
        self.assertIn(delegate, request._effective_approver_ids(line))

    def test_midflight_inbox(self):
        # A delegation created AFTER submit surfaces the request in the
        # delegate's inbox once the model create hook reconciles (D-21).
        request = self._submit()
        self.assertNotIn(self.user_b, self._awaiting(request))
        self.Delegation.create(self._vals(self.user_a, self.user_b))
        self.assertIn(self.user_b, self._awaiting(request),
                      'A mid-flight delegation must surface the request for '
                      'the delegate')

    def test_stale_visibility_reconciled(self):
        # A surfaced delegate loses read visibility AND their engine To-Do once
        # the delegation is revoked (review HIGH #2).
        deleg = self.Delegation.create(self._vals(self.user_a, self.user_b))
        request = self._submit()
        # Surfaced: in the inbox, holds an engine To-Do, can read the request.
        self.assertIn(self.user_b, self._awaiting(request))
        self.assertTrue(self._engine_todos(request, self.user_b))
        visible = self.Request.with_user(self.user_b).search(
            [('id', '=', request.id)])
        self.assertIn(request, visible)

        # Revoke via write -> reconciling hook fires.
        deleg.active = False
        self.assertNotIn(self.user_b, self._awaiting(request))
        self.assertFalse(self._engine_todos(request, self.user_b),
                         'A revoked delegate must not keep an engine To-Do')
        visible = self.Request.with_user(self.user_b).search(
            [('id', '=', request.id)])
        self.assertNotIn(request, visible,
                         'A revoked delegate must lose read visibility')

    def test_stale_visibility_reconciled_by_cron(self):
        # A delegation that lapses purely by date fires no write event; the daily
        # _cron_sync_delegations sweep still drops the delegate's visibility and
        # To-Do (review HIGH #2).
        deleg = self.Delegation.create(self._vals(self.user_a, self.user_b))
        request = self._submit()
        self.assertIn(self.user_b, self._awaiting(request))
        self.assertTrue(self._engine_todos(request, self.user_b))

        # Expire the window directly in the DB so no ORM write hook runs.
        past = self.today - timedelta(days=1)
        self.env.cr.execute(
            "UPDATE coresys_approval_delegation SET date_end = %s WHERE id = %s",
            [past, deleg.id])
        deleg.invalidate_recordset(['date_end'])

        self.Delegation._cron_sync_delegations()
        self.assertNotIn(self.user_b, self._awaiting(request))
        self.assertFalse(self._engine_todos(request, self.user_b),
                         'The cron sweep must clear a date-lapsed delegate')

    def test_no_duplicate_notifications(self):
        # Re-running the notify path for an already-surfaced delegate never
        # creates a second engine To-Do (idempotent — review Codex-MEDIUM).
        self.Delegation.create(self._vals(self.user_a, self.user_b))
        request = self._submit()
        self.assertEqual(len(self._engine_todos(request, self.user_b)), 1)
        request._notify_active_level()
        request._notify_active_level()
        self.assertEqual(
            len(self._engine_todos(request, self.user_b)), 1,
            'A re-notify must not double-create the delegate To-Do')
