# CoreSys Builders — https://coresysbuilders.com
# Copyright (C) CoreSys Builders. License OPL-1.
import logging

from odoo import _, api, fields, models
from odoo.exceptions import AccessError, UserError, ValidationError

from .approval_mixin import _approval_bypass_context

_logger = logging.getLogger(__name__)

# Lifecycle-critical fields. A non-superuser may never write state (every legal
# transition is an action method's narrow-sudo write), and may edit the other
# four only while the request is still a draft.
PROTECTED = {'state', 'category_id', 'requester_id', 'company_id', 'reference'}


class ApprovalRequest(models.Model):
    _name = 'coresys.approval.request'
    _description = 'Approval Request'
    _inherit = ['mail.thread', 'mail.activity.mixin']
    _order = 'id desc'

    name = fields.Char(
        string='Reference',
        default='New',
        copy=False,
        readonly=True,
    )
    category_id = fields.Many2one(
        'coresys.approval.category',
        string='Approval Category',
        required=True,
        ondelete='restrict',
        help="The approval configuration whose levels this request follows.",
    )
    reference = fields.Reference(
        selection='_selection_reference_models',
        string='Document',
        help="Optional record this request is about. When set, it must be of "
             "the category's target document type.",
    )
    requester_id = fields.Many2one(
        'res.users',
        string='Requester',
        required=True,
        default=lambda self: self.env.user,
    )
    state = fields.Selection(
        [
            ('draft', 'Draft'),
            ('to_approve', 'To Approve'),
            ('approved', 'Approved'),
            ('refused', 'Refused'),
            ('cancelled', 'Cancelled'),
        ],
        default='draft',
        required=True,
        tracking=True,
    )
    company_id = fields.Many2one(
        'res.company',
        string='Company',
        index=True,
        compute='_compute_company_id',
        store=True,
        readonly=False,
        help="Restrict this request to one company. Defaults from the category, "
             "otherwise the active company.",
    )
    line_ids = fields.One2many(
        'coresys.approval.request.line',
        'request_id',
        string='Approval Levels',
        copy=False,
    )
    refusal_reason = fields.Text(
        string='Refusal Reason',
        copy=False,
        help="Required when refusing. Recorded on the decided level and posted "
             "to the chatter.",
    )
    response_ids = fields.One2many(
        'coresys.approval.response',
        'request_id',
        string='Decision History',
        help="The immutable, engine-written record of every decision on this "
             "request.",
    )
    active_approver_user_ids = fields.Many2many(
        'res.users',
        string='Awaiting',
        compute='_compute_active_approvers',
        store=True,
        compute_sudo=True,
        help="The eligible approvers of the level this request is currently "
             "awaiting. Drives the 'To Approve' inbox; empty unless routing.",
    )

    @api.model
    def _selection_reference_models(self):
        models = self.env['ir.model'].sudo().search([])
        return [(model.model, model.name) for model in models]

    @api.depends('category_id')
    def _compute_company_id(self):
        for req in self:
            req.company_id = (
                req.category_id.company_id
                or req.company_id
                or self.env.company
            )

    @api.model_create_multi
    def create(self, vals_list):
        records = super().create(vals_list)
        for record in records:
            if not record.name or record.name == 'New':
                record.name = 'Approval / %s / %s' % (
                    record.category_id.name or _('Request'), record.id)
        return records

    def unlink(self):
        # A request may be deleted only while it has no decision history. Gate on
        # response_ids, not state: a cancelled request always carries its 'cancel'
        # audit row, and a submit->refuse->reset request is back in draft yet keeps
        # its earlier rows — both would slip past a state check and hit
        # coresys.approval.response's ondelete='restrict' FK as a raw DB error.
        # This raises the clean UserError first; an untouched draft stays deletable.
        for req in self:
            if req.response_ids:
                raise UserError(_(
                    "This request has decision history that must be retained "
                    "and it cannot be deleted."))
        return super().unlink()

    @api.constrains('reference', 'category_id')
    def _check_reference_matches_model(self):
        for req in self:
            if not req.reference:
                continue
            if req.reference._name != req.category_id.model_name:
                raise ValidationError(
                    'The document on request "%(req)s" must be a %(model)s '
                    'record to match its approval category.' % {
                        'req': req.display_name,
                        'model': req.category_id.model_name,
                    }
                )

    @api.constrains('company_id', 'category_id')
    def _check_company_matches_category(self):
        for req in self:
            cat_company = req.category_id.company_id
            if cat_company and req.company_id != cat_company:
                raise ValidationError(_(
                    "This request's company must match its category's company "
                    "(%s).", cat_company.display_name))

    def write(self, vals):
        if not self.env.su and (PROTECTED & vals.keys()
                                or 'refusal_reason' in vals):
            for req in self:
                req._assert_write_allowed(vals)
        return super().write(vals)

    def _assert_write_allowed(self, vals):
        # Reject stance: this guard never authorizes a transition. Every legal
        # state change is performed by an action method under a narrow sudo, so a
        # direct state write — even by the active approver — is always rejected
        # and can never skip the audit/chatter/activity/reason side effects.
        self.ensure_one()
        if 'state' in vals:
            raise AccessError(_(
                "Approval state changes must go through the Submit, Approve, "
                "Refuse, Reset or Cancel actions."))
        # Once decided, the refusal reason is locked: it may be set while the
        # request is still draft/to_approve (before action_refuse), but a
        # non-sudo rewrite after a terminal decision would tamper with the audit
        # trail. The action methods write it under sudo, so they are unaffected.
        if 'refusal_reason' in vals and self.state in (
                'approved', 'refused', 'cancelled'):
            raise AccessError(_(
                "The refusal reason is locked once the request is decided."))
        if self.state != 'draft' and (
                {'category_id', 'requester_id', 'company_id', 'reference'}
                & vals.keys()):
            raise AccessError(_(
                "This request is submitted; its category, requester, company "
                "and document are locked."))

    def _apply_decision(self, line, line_vals, new_state=False):
        # The caller has already authorized env.user against `line` (the captured
        # active line). Mutate the line status and, if given, the request state
        # atomically under a narrow post-authorization sudo. new_state is decided
        # from the captured line before any mutation, so the transition ordering
        # cannot drift and the guard is never re-derived mid-write.
        self.ensure_one()
        line.sudo().write(line_vals)
        if new_state:
            self.sudo().write({'state': new_state})

    def _log_response(self, action, line=False, reason=False, delegated_for=False):
        # Create the immutable decision record via sudo — the only sudo added
        # here — so ACL perm_create=0 cannot be forged by a user yet the engine
        # can still write history as the acting user runs the decision.
        # user_id stays the acting user (the delegate when delegated); delegated_for
        # records the frozen approver whose authority was exercised, or False.
        self.ensure_one()
        self.env['coresys.approval.response'].sudo().create({
            'request_id': self.id,
            'line_id': line.id if line else False,
            'user_id': self.env.user.id,
            'decided_on': fields.Datetime.now(),
            'level_name': line.name if line else False,
            'action': action,
            'reason': reason,
            'company_id': self.company_id.id,
            'delegated_for': delegated_for.id if delegated_for else False,
        })
        return True

    def action_approve(self):
        self.ensure_one()
        if self.state != 'to_approve':
            raise UserError(_("Only a request awaiting approval can be approved."))
        line = self._active_line()
        if not line:
            raise UserError(_("There is no pending level to approve."))
        if self.env.user not in self._effective_approver_ids(line):
            raise AccessError(_(
                "Only an eligible approver of the current level may decide."))
        # Resolve which frozen approver's authority is being exercised (empty
        # when the actor is themselves a frozen approver) for the audit record.
        delegator = self._delegator_for(line, self.env.user)
        # Lock the active line and re-check after acquiring it: a concurrent
        # approver may have decided this level between our read and write.
        self._lock_active_line(line)
        if self.state != 'to_approve' or line.status != 'pending':
            raise UserError(_(
                "This level was just decided by another approver; refresh and "
                "retry."))
        is_final = not (self._pending_lines() - line)
        self._apply_decision(
            line,
            {
                'status': 'approved',
                'decided_by': self.env.user.id,
                'decided_on': fields.Datetime.now(),
            },
            new_state=('approved' if is_final else False),
        )
        self._log_response(action='approve', line=line, delegated_for=delegator)
        # sudo the chatter post: it keeps the acting user as author (sudo does
        # not change env.user) but skips message_post's raise when the author
        # has no email, so a decision never rolls back over mail configuration.
        self.sudo().message_post(body=_("Approved by %s", self.env.user.name))
        # Clear this level's To-Dos, then notify the next level if one remains.
        self._close_active_activities()
        if not is_final:
            self._notify_active_level()
        else:
            # Final approval: auto-run the referenced target's gated action as
            # the requester, past the mixin's re-gate only (D-33/D-35).
            self._run_reference_gated_action()
        return True

    def _run_reference_gated_action(self):
        # Generic, duck-typed auto-run: the core engine stays base+mail and never
        # imports a target module (D-38). A savepoint keeps the approval intact if
        # the downstream action fails for an unrelated reason (D-36): the approval
        # is a recorded fact and must survive a confirmation error.
        self.ensure_one()
        record = self.reference
        if not record or not hasattr(record, '_run_approval_gated_action'):
            return False
        try:
            with self.env.cr.savepoint():
                # with_user gives the requester their own rights (not broad sudo);
                # the token context disables ONLY the mixin re-gate.
                record.with_user(self.requester_id.id).with_context(
                    **_approval_bypass_context()
                )._run_approval_gated_action()
        except Exception as exc:
            # D-36: the approval is a recorded fact and must survive ANY downstream
            # failure — not only the UserError family. The savepoint above already
            # isolated the rollback to the auto-run, so the approval stays committed.
            # Log at exception level so a genuine bug (e.g. a programming error) stays
            # visible in the server log rather than being silently swallowed, while the
            # requester still gets a chatter note to retry the action manually.
            _logger.exception(
                "Approval %s: auto-run of the gated action failed; approval kept, "
                "manual retry required.", self.id)
            self.sudo().message_post(body=_(
                "Approved, but the automatic action failed and must be retried "
                "manually: %s", exc))
        return True

    def action_refuse(self):
        self.ensure_one()
        if self.state != 'to_approve':
            raise UserError(_("Only a request awaiting approval can be refused."))
        line = self._active_line()
        if not line:
            raise UserError(_("There is no pending level to refuse."))
        if self.env.user not in self._effective_approver_ids(line):
            raise AccessError(_(
                "Only an eligible approver of the current level may decide."))
        if not self.refusal_reason:
            raise UserError(_("A reason is required to refuse."))
        # Resolve which frozen approver's authority is being exercised (empty
        # when the actor is themselves a frozen approver) for the audit record.
        delegator = self._delegator_for(line, self.env.user)
        # Lock the active line and re-check after acquiring it: a concurrent
        # approver may have decided this level between our read and write.
        self._lock_active_line(line)
        if self.state != 'to_approve' or line.status != 'pending':
            raise UserError(_(
                "This level was just decided by another approver; refresh and "
                "retry."))
        self._apply_decision(
            line,
            {
                'status': 'refused',
                'decided_by': self.env.user.id,
                'decided_on': fields.Datetime.now(),
                'reason': self.refusal_reason,
            },
            new_state='refused',
        )
        self._log_response(action='refuse', line=line, reason=self.refusal_reason,
                           delegated_for=delegator)
        self.sudo().message_post(
            body=_("Refused by %s: %s", self.env.user.name, self.refusal_reason))
        # Terminal: the request left to_approve, so drop every pending To-Do.
        self._close_active_activities()
        return True

    def action_reset(self):
        self.ensure_one()
        if self.state != 'refused':
            raise UserError(_("Only a refused request can be reset to draft."))
        if not (self.env.user == self.requester_id
                or self.env.user.has_group(
                    'coresys_approvals.group_approval_manager')):
            raise AccessError(_(
                "Only the requester or a manager can reset this request."))
        # Authorization above runs before this destructive op; the requester
        # legitimately lacks line-unlink ACL, so clear under a narrow sudo.
        self.line_ids.sudo().unlink()
        self.sudo().write({'state': 'draft'})
        self._log_response(action='reset')
        self.sudo().message_post(body=_("Reset to draft by %s", self.env.user.name))
        # Back to draft: clear any lingering To-Dos from the prior run.
        self._close_active_activities()
        return True

    def action_cancel(self):
        self.ensure_one()
        if self.state not in ('draft', 'to_approve'):
            raise UserError(_(
                "Only a draft or in-progress request can be cancelled."))
        if not (self.env.user == self.requester_id
                or self.env.user.has_group(
                    'coresys_approvals.group_approval_manager')):
            raise AccessError(_(
                "Only the requester or a manager can cancel this request."))
        self.sudo().write({'state': 'cancelled'})
        self._log_response(action='cancel')
        self.sudo().message_post(body=_("Cancelled by %s", self.env.user.name))
        # Cancelled: the request left the routing flow, drop pending To-Dos.
        self._close_active_activities()
        return True

    @api.depends('state', 'line_ids.status', 'line_ids.sequence',
                 'line_ids.approver_user_ids')
    def _compute_active_approvers(self):
        # Depend on line status AND sequence so the field recomputes after every
        # decision and any line reordering (Pitfall 5). Empty unless routing, so
        # a decided request never lists a past approver as awaiting.
        for req in self:
            if req.state == 'to_approve':
                req.active_approver_user_ids = req._effective_approver_ids(req._active_line())
            else:
                req.active_approver_user_ids = self.env['res.users']

    def _notify_active_level(self):
        # One dedicated engine To-Do per eligible approver of the active level.
        # The frozen approver set is already company-scoped by the resolver
        # (finding #8), so no cross-company user is ever in the loop — activities
        # never leak a request's existence to another company. Idempotent: skip
        # any approver who already holds an open engine To-Do on this record so a
        # re-notify (level advance recompute) never double-creates.
        self.ensure_one()
        engine_type = self.env.ref('coresys_approvals.mail_act_approval_todo')
        already_notified = self.activity_ids.filtered(
            lambda act: act.activity_type_id == engine_type).mapped('user_id')
        line = self._active_line()
        for user in self._effective_approver_ids(line):
            if user in already_notified:
                continue
            # quick_update creates the actionable To-Do without firing the
            # assignment notification, so submission never hard-depends on a
            # configured sender email (a fresh install may have none) yet the
            # approver still gets the activity and is subscribed for chatter.
            self.with_context(
                mail_activity_quick_update=True,
            ).activity_schedule(
                'coresys_approvals.mail_act_approval_todo',
                user_id=user.id,
                summary=_("Approval needed: %s", self.display_name))

    def _close_active_activities(self):
        # Clear this record's pending engine To-Dos so decided or superseded
        # levels don't linger in anyone's activity list. Only the engine's
        # dedicated type is removed — a user's own manual To-Dos survive.
        self.ensure_one()
        self.activity_unlink(['coresys_approvals.mail_act_approval_todo'])

    def _lock_active_line(self, line):
        # Serialize concurrent decisions on the same active level: a plain
        # SELECT ... FOR UPDATE makes a second transaction block until the first
        # commits, then re-read the now-decided row. Invalidate the cached line
        # status and request state so the post-lock re-check sees fresh values.
        self.ensure_one()
        self.env.cr.execute(
            "SELECT id FROM coresys_approval_request_line WHERE id = %s "
            "FOR UPDATE", [line.id])
        line.invalidate_recordset(['status'])
        self.invalidate_recordset(['state'])

    def _active_line(self):
        self.ensure_one()
        return self.line_ids.filtered(
            lambda line: line.status == 'pending').sorted('sequence')[:1]

    def _pending_lines(self):
        self.ensure_one()
        return self.line_ids.filtered(lambda line: line.status == 'pending')

    def _eligible_delegators(self, line):
        # THE single filtered set of frozen approvers still holding usable
        # authority — the sole source shared by BOTH the eligibility union
        # (_effective_approver_ids) and the audit attribution (_delegator_for),
        # so the two paths can never diverge (BLOCKER 1). At SUBMIT the untouched
        # _resolve_level (lines ~401-402) already drops the requester from the
        # frozen set, so the requester only appears in line.approver_user_ids when
        # allow_self_approval was TRUE at submit time. This subtraction is the
        # defense for the POST-SUBMIT case: a category whose allow_self_approval
        # is toggled OFF while the request is pending must not let a frozen
        # requester-approver keep (or delegate out) approval authority (BLOCKER 2).
        self.ensure_one()
        delegators = line.approver_user_ids
        if not self.category_id.allow_self_approval:
            delegators = delegators - self.requester_id
        return delegators

    def _effective_approver_ids(self, line):
        # Decision-time authority for `line`: the still-eligible FROZEN approvers
        # plus the active, in-window, in-scope, company-scoped, valid delegates of
        # those approvers. Delegates are resolved ONLY from _eligible_delegators,
        # so a delegation handed out by a self-approval-disqualified requester
        # cannot re-admit the requester's delegate (D-24), and the per-line union
        # from that line's own frozen set means delegation can never widen
        # authority beyond what the delegator already held (D-23).
        self.ensure_one()
        if not line:
            return self.env['res.users']
        eligible = self._eligible_delegators(line)
        Delegation = self.env['coresys.approval.delegation']
        delegates = Delegation._active_delegates_for(eligible.ids, self.company_id, self.category_id)
        effective = self._eligible_delegators(line) | delegates
        # Final defensive mirror of _resolve_level: never surface the requester
        # when self-approval is off, even via a delegate chain.
        if not self.category_id.allow_self_approval:
            effective -= self.requester_id
        return effective

    def _delegator_for(self, line, user):
        # Attribution: which frozen approver's authority `user` exercised. Empty
        # when the user is themselves a frozen approver (no delegated_for). The
        # delegator set is _eligible_delegators(line).ids — the SAME filtered set
        # as the eligibility path, NOT the raw line.approver_user_ids — so a
        # disqualified frozen delegator (e.g. the requester after a mid-flight
        # self-approval-off toggle) can never be recorded as the delegator
        # (BLOCKER 1). Resolved through the shared _effective_delegations
        # predicate helper so eligibility and attribution use the identical
        # predicate set.
        self.ensure_one()
        if user in line.approver_user_ids:
            return self.env['res.users']
        Delegation = self.env['coresys.approval.delegation']
        dels = Delegation._effective_delegations(self._eligible_delegators(line).ids, self.company_id, self.category_id, delegate_ids=user.ids)
        # Order by (delegator_id, id): when one delegate covers several frozen
        # approvers on a line, record the LOWEST original-approver user id (OQ2;
        # D-25 wording is singular). Ordering on the delegator — not the row id —
        # keeps attribution deterministic regardless of delegation creation order.
        return dels.sorted(lambda d: (d.delegator_id.id, d.id))[:1].delegator_id

    def _reconcile_active_activities(self):
        # Request-side reconcile called by the delegation model's create/write/
        # unlink hook and the daily cron (Plan 01). The caller has already
        # recomputed active_approver_user_ids, which drops a revoked/expired
        # delegate's READ visibility via the ownership ir.rule leaf; this method
        # additionally clears the lingering engine To-Do so that user has neither
        # read access nor an actionable activity (review HIGH #2). Mirrors the
        # _close_active_activities engine-To-Do idiom, then re-notifies (idempotent).
        engine_type = self.env.ref('coresys_approvals.mail_act_approval_todo')
        for req in self:
            stale = req.activity_ids.filtered(
                lambda act: act.activity_type_id == engine_type
                and act.user_id not in req.active_approver_user_ids)
            if stale:
                stale.sudo().unlink()
            if req.state == 'to_approve':
                req._notify_active_level()
        return True

    def _group_members(self, group):
        # Single 19.0 port-isolation point: res.groups.users is renamed to
        # user_ids in Odoo 19. sudo() only this membership read so res.users
        # record rules cannot hide members and wrongly empty a level.
        if not group:
            return self.env['res.users']
        return group.sudo().user_ids

    def _resolve_level(self, level):
        self.ensure_one()
        # Gather and vet approvers entirely under sudo: action_submit runs as the
        # plain requester, whose res.users record rules may hide other users'
        # active/share/company_ids and spuriously empty a level or raise. sudo the
        # named-user read and the union so every subsequent attribute is readable.
        members = self._group_members(level.approver_group_id)
        approvers = (members | level.sudo().approver_user_ids).sudo()
        # Drop inactive and portal/share accounts before eligibility: neither can
        # legitimately act on an internal approval, so they are never snapshotted.
        approvers = approvers.filtered(
            lambda user: user.active and not user.share)
        if not self.category_id.allow_self_approval:
            approvers -= self.requester_id
        if self.company_id:
            approvers = approvers.filtered(
                lambda user: self.company_id in user.company_ids)
        return approvers

    def action_submit(self):
        self.ensure_one()
        # Authorize as the acting user before any sudo: only the requester or a
        # manager may submit, and only a draft is submittable.
        if self.env.user != self.requester_id and not self.env.user.has_group(
                'coresys_approvals.group_approval_manager'):
            raise AccessError(_(
                "Only the requester or a manager can submit this request."))
        if self.state != 'draft':
            raise UserError(_("Only a draft request can be submitted."))
        if not self.category_id.level_ids:
            raise ValidationError(_(
                "This request's category has no approval levels configured, so "
                "it cannot be submitted."))
        # Re-snapshot from current config (D-16); line lifecycle is engine-only.
        self.line_ids.sudo().unlink()
        lines = []
        for level in self.category_id.level_ids.sorted('sequence'):
            approvers = self._resolve_level(level)
            if not approvers:
                raise ValidationError(
                    'Level "%(level)s" has no eligible approver, so this '
                    'request cannot be submitted.' % {'level': level.name}
                )
            lines.append((0, 0, {
                'name': level.name,
                'sequence': level.sequence,
                'approver_user_ids': [(6, 0, approvers.ids)],
                'status': 'pending',
            }))
        # Snapshot lines and the state transition are engine mutations, created
        # under a narrow post-authorization sudo (authorization happened above).
        self.sudo().write({
            'line_ids': lines,
            'state': 'to_approve',
            'refusal_reason': False,
        })
        # Persist the routing field now. active_approver_user_ids is a stored,
        # compute_sudo m2m that rule_approval_request_own reads to grant an
        # approver visibility of the request they must decide. The sudo write
        # above only marks it for recomputation; the record rule is evaluated as
        # a SQL subquery against the stored relation table, and rule-domain
        # fields are not part of flush_search — so without this flush the very
        # next approver to fetch the request would be denied read against a
        # still-empty relation. Flushing here makes routing durable before the
        # method returns and control passes to an approver.
        self.flush_recordset(['active_approver_user_ids'])
        self._log_response(action='submit')
        # Routing starts at level 1 — notify its eligible approvers.
        self._notify_active_level()
        return True
