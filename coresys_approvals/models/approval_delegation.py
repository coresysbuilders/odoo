# CoreSys Builders — https://coresysbuilders.com
# Copyright (C) CoreSys Builders. License OPL-1.
from odoo import _, api, fields, models
from odoo.exceptions import ValidationError


class ApprovalDelegation(models.Model):
    _name = 'coresys.approval.delegation'
    _description = 'Approval Delegation'
    _order = 'date_end desc, id desc'

    delegator_id = fields.Many2one(
        'res.users',
        string='Delegator',
        required=True,
        default=lambda self: self.env.user,
        ondelete='cascade',
        help="The approver handing over their approval authority.",
    )
    delegate_id = fields.Many2one(
        'res.users',
        string='Delegate',
        required=True,
        ondelete='cascade',
        help="The user who may act with the delegator's authority while this "
             "delegation is active.",
    )
    date_start = fields.Date(
        string='From',
        required=True,
        default=fields.Date.context_today,
        help="First day the delegation is effective.",
    )
    date_end = fields.Date(
        string='Until',
        required=True,
        help="Last day the delegation is effective.",
    )
    category_id = fields.Many2one(
        'coresys.approval.category',
        string='Limited to Category',
        ondelete='cascade',
        help="Leave empty to delegate for every approval category.",
    )
    company_id = fields.Many2one(
        'res.company',
        string='Company',
        index=True,
        required=True,
        default=lambda self: self.env.company,
        help="The company this delegation applies within. Authority never "
             "crosses companies.",
    )
    active = fields.Boolean(default=True)

    @api.constrains('delegator_id', 'delegate_id', 'date_start', 'date_end',
                    'active', 'company_id')
    def _check_no_cycle(self):
        # Every listed field is required and always in create vals, so this
        # check always fires (Pitfall 4). Self-delegation and a reciprocal
        # active A<->B pair (overlapping window, same company) are the only
        # loops single-hop delegation (D-20) can form — an A->B->A chain is not
        # representable without re-delegation, so no transitive walk is needed.
        for deleg in self:
            if deleg.delegator_id == deleg.delegate_id:
                raise ValidationError(_(
                    "You cannot delegate approval authority to yourself."))
            if not deleg.active:
                continue
            # sudo so a reciprocal created by the other party is visible even
            # when record rules would otherwise hide it. This create-time check
            # has a narrow concurrency window — two reciprocal delegations
            # committed in parallel transactions can each miss the other. That
            # is an accepted low-risk MVP tradeoff: single-hop expansion (D-20)
            # means even a slipped A<->B pair cannot form a transitive chain,
            # and each side still acts only within their delegator's frozen
            # authority. The interval-overlap predicate (S1 <= E2 AND S2 <= E1)
            # is retained (review Codex-MEDIUM; refreshes threat T-03-01).
            reciprocal = deleg.sudo().search([
                ('id', '!=', deleg.id),
                ('active', '=', True),
                ('delegator_id', '=', deleg.delegate_id.id),
                ('delegate_id', '=', deleg.delegator_id.id),
                ('company_id', '=', deleg.company_id.id),
                ('date_start', '<=', deleg.date_end),
                ('date_end', '>=', deleg.date_start),
            ], limit=1)
            if reciprocal:
                raise ValidationError(_(
                    "A reciprocal delegation between %(a)s and %(b)s is already "
                    "active for an overlapping period; this would create an "
                    "approval loop.",
                    a=deleg.delegator_id.display_name,
                    b=deleg.delegate_id.display_name))

    @api.constrains('date_start', 'date_end')
    def _check_window(self):
        for deleg in self:
            if deleg.date_end < deleg.date_start:
                raise ValidationError(_(
                    "The delegation end date cannot be before its start date."))

    @api.constrains('delegate_id', 'active')
    def _check_delegate_valid(self):
        # Reject an invalid delegate at save, not only at resolution, so a
        # dormant record cannot activate later on a group change (Codex-MEDIUM;
        # D-26). The delegate must be an active, internal (non-share) Approval
        # Approver — Approver implies User and keeps the Approve/Refuse button
        # visible (OQ1 locked floor).
        for deleg in self:
            if not deleg.active:
                continue
            delegate = deleg.delegate_id.sudo()
            if not (delegate.active and not delegate.share and delegate.has_group(
                    'coresys_approvals.group_approval_approver')):
                raise ValidationError(_(
                    "%(user)s cannot be a delegate: the delegate must be an "
                    "active internal user in the Approval Approver group.",
                    user=deleg.delegate_id.display_name))

    @api.constrains('delegator_id', 'delegate_id', 'company_id', 'category_id')
    def _check_company_membership(self):
        # company_id is required and never blank; both parties must belong to it
        # and a scoped category must not belong to a different company, so a
        # delegation can never span companies (review HIGH #1; SEC-03/D-27).
        for deleg in self:
            company = deleg.company_id
            if company not in deleg.delegator_id.sudo().company_ids:
                raise ValidationError(_(
                    "The delegator is not a member of the selected company."))
            if company not in deleg.delegate_id.sudo().company_ids:
                raise ValidationError(_(
                    "The delegate is not a member of the selected company."))
            if deleg.category_id and deleg.category_id.company_id \
                    and deleg.category_id.company_id != company:
                raise ValidationError(_(
                    "The selected category belongs to another company."))

    @api.constrains('delegator_id')
    def _check_delegator_is_self(self):
        # Record-rule-independent defense: a non-manager, non-sudo user cannot
        # mint authority for another user via a sudo/misconfig path. A manager
        # may set any delegator (review Gemini-HIGH).
        if self.env.su or self.env.user.has_group(
                'coresys_approvals.group_approval_manager'):
            return
        for deleg in self:
            if deleg.delegator_id != self.env.user:
                raise ValidationError(_(
                    "You can only create delegations where you are the "
                    "delegator."))

    @api.model
    def _effective_delegations(self, delegator_ids, company, category,
                               delegate_ids=None):
        # THE single source of the delegation predicate set. Consumed by BOTH
        # _active_delegates_for (engine eligibility) and the Plan-02
        # _delegator_for (audit attribution) — always called with the same
        # filtered eligible_delegators.ids — so eligibility and attribution can
        # never diverge. Returns delegation RECORDS.
        if not delegator_ids or not company:
            return self.browse()
        today = fields.Date.context_today(self)
        domain = [
            ('active', '=', True),
            ('delegator_id', 'in', list(delegator_ids)),
            ('date_start', '<=', today),
            ('date_end', '>=', today),
            '|', ('category_id', '=', False),
            ('category_id', '=', category.id if category else False),
            # EXACT company match only — there is deliberately no blank-company
            # branch: company_id is required and admitting a False value would
            # leak a delegation across all companies (review HIGH #1).
            ('company_id', '=', company.id),
        ]
        if delegate_ids:
            domain.append(('delegate_id', 'in', list(delegate_ids)))
        # sudo the whole read so res.users AND delegation record rules cannot
        # hide a valid delegate and wrongly narrow authority (same reasoning as
        # _resolve_level). Then re-apply delegate validity: active, internal
        # (non-share), member of the company, and in the Approval Approver group
        # (OQ1 locked floor — Approver implies User and keeps the Approve/Refuse
        # button visible).
        dels = self.sudo().search(domain)
        return dels.filtered(
            lambda d: d.delegate_id.active
            and not d.delegate_id.share
            and company in d.delegate_id.company_ids
            and d.delegate_id.has_group('coresys_approvals.group_approval_approver'))

    @api.model
    def _active_delegates_for(self, delegator_ids, company, category):
        # Thin wrapper — Plan 02 and the tests call this exact signature.
        return self._effective_delegations(
            delegator_ids, company, category).mapped('delegate_id')

    def _impacted_pending_requests(self):
        # Pending requests whose stored effective set intersects EITHER the old
        # delegators (to surface a new delegate) OR the delegates (so a revoked
        # delegate's currently-surfaced request is recomputed and their
        # visibility drops) — review HIGH #2.
        parties = (self.mapped('delegator_id') | self.mapped('delegate_id'))
        if not parties:
            return self.env['coresys.approval.request']
        return self.env['coresys.approval.request'].sudo().search([
            ('state', '=', 'to_approve'),
            ('active_approver_user_ids', 'in', parties.ids),
        ])

    def _sync_impacted_requests(self, extra_requests=None):
        requests = self._impacted_pending_requests()
        if extra_requests:
            requests |= extra_requests
        # Re-derive the stored effective set, then reconcile activities:
        # _reconcile_active_activities (delivered by Plan 02, wave 2) unlinks
        # stale mail_act_approval_todo To-Dos for users no longer in
        # active_approver_user_ids and re-notifies newly-effective ones
        # (idempotent). The delegation-aware _compute_active_approvers is also
        # folded in Plan 02; authority is always evaluated live regardless.
        requests._compute_active_approvers()
        requests._reconcile_active_activities()

    @api.model_create_multi
    def create(self, vals_list):
        records = super().create(vals_list)
        records._sync_impacted_requests()
        return records

    def write(self, vals):
        # Capture the old delegator/delegate surfacing BEFORE the mutation so a
        # revoked/rescoped delegate's request is reconciled too (review HIGH #2).
        before = self._impacted_pending_requests()
        res = super().write(vals)
        self._sync_impacted_requests(extra_requests=before)
        return res

    def unlink(self):
        before = self._impacted_pending_requests()
        res = super().unlink()
        before._compute_active_approvers()
        before._reconcile_active_activities()
        return res

    @api.model
    def _cron_sync_delegations(self):
        # Daily reconciliation sweep: a delegation that lapses by date fires no
        # write event, so this bounded pass clears an expired delegate's
        # read-visibility and lingering To-Do within a day (review HIGH #2).
        # This is a security reconciliation, NOT the deferred v2 SLA/escalation
        # cron.
        requests = self.env['coresys.approval.request'].sudo().search([
            ('state', '=', 'to_approve'),
        ])
        requests._compute_active_approvers()
        requests._reconcile_active_activities()
