# Part of Expenses Shortcut by CoreSys Builders. License LGPL-3.0 or later.
#
# The expense report is kept as hidden plumbing: one report per expense, created
# by this module. This file holds no business logic of its own. It guards the
# hidden reports against misuse, silences the notifications they would otherwise
# send, and moves the refusal message onto the expense the user actually sees.
# Every override is gated on `direct_auto_sheet`, so reports created by hand keep
# their stock behaviour.

from odoo import _, api, fields, models
from odoo.exceptions import AccessError, UserError, ValidationError

# Fields that decide who approves a report. They are resolved by the workflow
# and may not be set by a non-superuser caller.
_DIRECT_SHEET_ROUTING = frozenset({'user_id'})

# The keys core's `_do_approve` writes on the report.
_CORE_APPROVE_KEYS = frozenset({'approval_state', 'user_id', 'approval_date'})


class HrExpenseSheet(models.Model):
    _inherit = 'hr.expense.sheet'

    # Marks reports created by this module for a single expense. copy=False keeps
    # a duplicated report from inheriting the module's behaviour.
    direct_auto_sheet = fields.Boolean(
        string="Auto-created for one expense",
        default=False,
        copy=False,
    )

    # The marker is listed too, so that marking an existing multi-line report
    # also triggers the check.
    @api.constrains('expense_line_ids', 'direct_auto_sheet')
    def _check_auto_sheet_single_line(self):
        for sheet in self.filtered('direct_auto_sheet'):
            if len(sheet.expense_line_ids) > 1:
                raise ValidationError(_(
                    "An automatically created expense report holds exactly one expense."
                ))

    # Approver routing guard.
    #
    # `user_id` decides who approves a report, and also grants record-rule access
    # to it. Without this guard an employee could create their own marked report
    # naming themselves as approver and have it auto-validated on submit. Non
    # superuser writes to `user_id` are therefore refused, on every report, with
    # one exception: the exact write core's `_do_approve` performs. The module's
    # own approver assignment runs under sudo.
    #
    # Matching `default_user_id` keys are also removed from the context on create.
    # Only those keys are removed, so other UI defaults are preserved.
    _DIRECT_SHEET_ROUTING = _DIRECT_SHEET_ROUTING

    @api.model_create_multi
    def create(self, vals_list):
        """Refuse a caller-chosen approver, and strip it from the context too."""
        if not self.env.su:
            for vals in vals_list:
                if self._DIRECT_SHEET_ROUTING.intersection(vals):
                    raise AccessError(_(
                        "The approver of an expense report is resolved by the "
                        "workflow and cannot be set by hand."
                    ))
            protected_defaults = {'default_%s' % name for name in self._DIRECT_SHEET_ROUTING}
            if protected_defaults.intersection(self.env.context):
                self = self.with_context({
                    key: value
                    for key, value in self.env.context.items()
                    if key not in protected_defaults
                })
        return super().create(vals_list)

    def _direct_is_core_approve_write(self, vals):
        """Return True only for the vals core's ``_do_approve`` writes.

        The keys must be a subset of core's, the state must be ``approve``, and
        ``user_id`` must be the value core itself computes for every sheet in
        ``self``, so the write cannot name an arbitrary approver.
        """
        if set(vals) - _CORE_APPROVE_KEYS:
            return False
        if vals.get('approval_state') != 'approve':
            return False
        # Same value as core: `sheet.user_id.id or self.env.user.id`.
        return all(
            vals.get('user_id') == (sheet.user_id.id or self.env.uid)
            for sheet in self
        )

    def write(self, vals):
        """Refuse a caller-chosen approver, except in core's own approve write."""
        if (
            not self.env.su
            and self._DIRECT_SHEET_ROUTING.intersection(vals)
            and not self._direct_is_core_approve_write(vals)
        ):
            raise AccessError(_(
                "The approver of an expense report is resolved by the "
                "workflow and cannot be set by hand."
            ))
        return super().write(vals)

    # `action_reset_expense_sheets` is public and runs its reversal under sudo,
    # so without this check an employee could reset their own posted expense and
    # have core reverse the journal entry. Hooking the check rather than the
    # action keeps core's own draft/submitted filtering intact. The sudo() is a
    # read-only lookup: employees and approvers may not be able to read the moves.
    def _check_can_reset_approval(self):
        """Refuse to reset a module-created report linked to a posted entry."""
        res = super()._check_can_reset_approval()
        marked = self.filtered('direct_auto_sheet')
        if marked and marked.sudo().account_move_ids.filtered(
            lambda move: move.state == 'posted'
        ):
            raise UserError(_(
                "You cannot reset to draft an expense linked to a posted journal entry."
            ))
        return res

    # Notification suppression for hidden reports.
    #
    # These are model overrides rather than context flags because core calls
    # several of these paths under sudo or from places no context reaches.

    # Called on a single record. Tracking values are still logged on the report;
    # only the outbound notification is suppressed.
    def _track_subtype(self, init_values):
        if self.direct_auto_sheet:
            return False
        return super()._track_subtype(init_values)

    # Core applies the follower tuples to every record in `self`, so marked
    # reports are split out here, before super() sees them.
    def _message_auto_subscribe(self, updated_values, followers_existing_policy='skip'):
        unmuted = self - self.filtered('direct_auto_sheet')
        if not unmuted:
            return True
        return super(HrExpenseSheet, unmuted)._message_auto_subscribe(
            updated_values, followers_existing_policy=followers_existing_policy
        )

    # Second line of defence for direct callers. Prevents both the employee
    # follower and the "You have been assigned to" email for hidden reports.
    def _message_auto_subscribe_followers(self, updated_values, subtype_ids):
        muted = self.filtered('direct_auto_sheet')
        unmuted = self - muted
        if not unmuted:
            return []
        return super(HrExpenseSheet, unmuted)._message_auto_subscribe_followers(
            updated_values, subtype_ids
        )

    # Removes core's report approval activity from hidden reports. The expense
    # carries its own approval activity, synchronised by the hooks below.
    def activity_update(self):
        muted = self.filtered('direct_auto_sheet')
        if muted:
            muted.activity_unlink(['hr_expense.mail_act_expense_approval'])
        return super(HrExpenseSheet, self - muted).activity_update()

    # Activity synchronisation.
    #
    # Approvals, refusals and resets can be made directly on the report (for
    # example from core's duplicate-approval dialog), bypassing the expense
    # actions. Hooking core's decision methods keeps the expense's approval
    # activity in step on every path. super() runs first so the expense state is
    # already up to date when activities are refreshed.

    def _do_approve(self):
        res = super()._do_approve()
        self.filtered('direct_auto_sheet').expense_line_ids.update_activities_and_mails()
        return res

    def _do_reset_approval(self):
        res = super()._do_reset_approval()
        self.filtered('direct_auto_sheet').expense_line_ids.update_activities_and_mails()
        return res

    # Every refusal path goes through `_do_refuse`, so the refusal reason is
    # posted on the expense here rather than in a wizard.
    def _do_refuse(self, reason):
        res = super()._do_refuse(reason)
        self.filtered('direct_auto_sheet').expense_line_ids.update_activities_and_mails()

        # A comment rather than a note, so that the message is delivered.
        comment_subtype_id = self.env['ir.model.data']._xmlid_to_res_id('mail.mt_comment')

        for sheet in self.filtered('direct_auto_sheet'):
            # The expense may have been deleted while its report survived.
            expense = sheet.expense_line_ids
            if not expense:
                continue

            # The body is rendered once, in the posting context's language, so
            # use the employee's language rather than the refuser's.
            post_lang = expense.employee_id.user_id.partner_id.lang or self.env.lang
            expense.with_context(lang=post_lang).message_post_with_source(
                # Module template; core's template names the expense report.
                'expenses_shortcut.expense_refuse_reason_template',
                # Keys must match the template's t-out names.
                render_values={'reason': reason, 'name': expense.name},
                subtype_id=comment_subtype_id,
                # The employee is not necessarily a follower of the expense.
                partner_ids=expense.employee_id.user_id.partner_id.ids,
            )
        return res
