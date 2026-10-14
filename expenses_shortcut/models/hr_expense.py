# Part of Expenses Shortcut by CoreSys Builders. License LGPL-3.0 or later.
#
# Odoo 19 style expense lifecycle on top of Odoo 18.
#
# Each expense is driven through its own hidden hr.expense.sheet, and the
# lifecycle users see is exposed through a module-owned `expense_state` field.
# Core `hr.expense.state` is left untouched: several core record rules, the
# expense dashboard and sale_expense all match on its six values, and changing
# that vocabulary would break them without raising anything.

import logging
from ast import literal_eval

from odoo import _, api, fields, models
from odoo.exceptions import AccessError, UserError, ValidationError
from odoo.tools.misc import formatLang

_logger = logging.getLogger(__name__)

# Same values and spelling as Odoo 19's expense approval state.
APPROVAL_STATE = [
    ('submitted', "Submitted"),
    ('approved', "Approved"),
    ('refused', "Refused"),
]

# Maps the hidden sheet's approval_state to the values above. A sheet with no
# approval decision yet holds False, which .get() maps to False.
_SHEET_TO_19 = {'submit': 'submitted', 'approve': 'approved', 'cancel': 'refused'}

# Every stored column on hr.expense that this module computes from the hidden
# sheet. They are readonly in the UI, but `readonly` does not stop a write over
# RPC, so create() and write() below refuse these keys explicitly.
#
# Non-stored computes (can_approve, can_reset, cannot_approve_reason,
# direct_outstanding_notice) are intentionally not listed: they have no column
# to forge, and listing them would make an ordinary form save that echoes a
# read-only value back raise for no reason. A new stored projection added to
# this model should be added here.
_DIRECT_PROJECTIONS = frozenset({
    'expense_state',
    'approval_state',
    'approval_date',
    'payment_state',
    'account_move_id',
    'direct_is_auto_sheet',
    'amount_residual',
})


class HrExpense(models.Model):
    _inherit = 'hr.expense'

    # The Odoo 19 lifecycle, in Odoo 19's order. Being stored, the column is
    # computed for every existing row when the module is installed, so no
    # separate backfill is needed.
    expense_state = fields.Selection(
        selection=[
            ('draft', "Draft"),
            ('submitted', "Submitted"),
            ('approved', "Approved"),
            ('posted', "Posted"),
            ('in_payment', "In Payment"),
            ('paid', "Paid"),
            ('refused', "Refused"),
        ],
        string="Status",
        compute='_compute_expense_state',
        store=True,
        readonly=True,
        index=True,
        copy=False,
        default='draft',
        tracking=True,
    )

    # Approval status and date, projected read-only from the hidden sheet.
    # The only writer is core's sheet-side _do_approve.
    approval_state = fields.Selection(
        selection=APPROVAL_STATE,
        string="Approval Status",
        compute='_compute_approval',
        store=True,
        readonly=True,
        copy=False,
        tracking=True,
    )
    # Core's _do_approve writes a Date into the sheet's Datetime column, so
    # approval timestamps are at midnight. The value is mirrored as is.
    approval_date = fields.Datetime(
        string="Approval Date",
        compute='_compute_approval',
        store=True,
        readonly=True,
        copy=False,
        tracking=True,
    )

    # Mirrors the sheet's payment_state. Core declares that selection through a
    # lambda bound to the environment; a stored, groupable column needs a fixed
    # list, so account.move's PAYMENT_STATE_SELECTION is copied here. Not
    # tracked: the ledger already keeps its own history on account.move.
    payment_state = fields.Selection(
        selection=[
            ('not_paid', "Not Paid"),
            ('in_payment', "In Payment"),
            ('paid', "Paid"),
            ('partial', "Partially Paid"),
            ('reversed', "Reversed"),
            ('blocked', "Blocked"),
            ('invoicing_legacy', "Invoicing App Legacy"),
        ],
        string="Payment Status",
        compute='_compute_payment_state',
        store=True,
        readonly=True,
        copy=False,
    )

    # The expense's journal entry. Restricted to invoicing users so employees
    # cannot see accounting document ids. Because of that group, this field
    # must not be used in another field's depends, in a domain, or in a view
    # that non-accountants load. The column is empty until approval, hence the
    # partial index.
    account_move_id = fields.Many2one(
        comodel_name='account.move',
        string="Journal Entry",
        compute='_compute_account_move_id',
        store=True,
        readonly=True,
        copy=False,
        index='btree_not_null',
        groups='account.group_account_invoice',
    )

    # True when the expense sits on a single-line sheet created by this module.
    # This is the same condition _check_direct_sheet enforces. The residual and
    # the outstanding notice below are only meaningful when it holds: on a
    # legacy multi-line report a payment is reconciled against the whole move,
    # so there is no per-expense figure to show. A residual of 0.0 therefore
    # means "settled" when this flag is True and "not applicable" when it is
    # False. No groups: the employee-facing notice reads it in the view.
    direct_is_auto_sheet = fields.Boolean(
        compute='_compute_direct_is_auto_sheet',
        store=True,
        readonly=True,
        copy=False,
    )

    # Outstanding reimbursement on the expense itself, named and labelled as in
    # Odoo 19. Stored so the Pay button's `invisible` expression can read it.
    # The sheet residual is in company currency, so currency_field must point
    # to company_currency_id; the default currency_id would show the amount
    # under the wrong symbol on foreign-currency expenses. Intentionally not
    # restricted to accountants: this is the employee's own money.
    amount_residual = fields.Monetary(
        string="Amount Due",
        currency_field='company_currency_id',
        compute='_compute_amount_residual',
        store=True,
        readonly=True,
        copy=False,
    )

    # Message shown when the expense reads Paid but money is still owed.
    direct_outstanding_notice = fields.Char(
        compute='_compute_direct_outstanding_notice',
    )

    # Per-user permission fields, same names as Odoo 19. They must stay
    # non-stored and non-related so they are computed as the acting user:
    # a related or stored compute defaults to compute_sudo=True, every group
    # test would then pass, and users could approve their own expenses.
    can_approve = fields.Boolean(
        string="Can Approve",
        compute='_compute_can_approve',
    )
    cannot_approve_reason = fields.Char(
        compute='_compute_can_approve',
    )
    can_reset = fields.Boolean(
        string="Can Reset",
        compute='_compute_can_reset',
    )

    # Only stored sheet fields are listed. The sheet's state and payment_state
    # already depend on its moves, so payment changes reach this compute
    # without naming account.move here.
    @api.depends('sheet_id', 'sheet_id.state', 'sheet_id.payment_state', 'sheet_id.amount_residual')
    def _compute_expense_state(self):
        # Returns 'paid' on Community; Enterprise overrides it to 'in_payment'.
        in_payment_state = self.env['account.move']._get_invoice_in_payment_state()
        for expense in self:
            sheet = expense.sheet_id
            if not sheet or sheet.state == 'draft':
                # A legacy draft report maps to draft: the expense has not been
                # submitted yet, even though core state reads 'reported'.
                expense.expense_state = 'draft'
            elif sheet.state == 'cancel':
                expense.expense_state = 'refused'
            elif sheet.state == 'submit':
                expense.expense_state = 'submitted'
            elif sheet.state == 'approve':
                expense.expense_state = 'approved'
            elif sheet.state == 'post':
                expense.expense_state = 'posted'
            else:
                if sheet.payment_state == 'reversed':
                    # Core keeps a sheet in 'done' once its bill is reversed.
                    # Reporting that as paid would contradict the ledger, so a
                    # reversed bill is shown as refused.
                    expense.expense_state = 'refused'
                else:
                    partial = (
                        sheet.payment_state == 'partial'
                        and not sheet.company_currency_id.is_zero(sheet.amount_residual)
                    )
                    settling = sheet.payment_state == 'in_payment' or partial
                    expense.expense_state = in_payment_state if settling else 'paid'

    # One compute for both fields, so an approval produces a single chatter
    # tracking entry covering the status and the date.
    @api.depends('sheet_id.approval_state', 'sheet_id.approval_date')
    def _compute_approval(self):
        for expense in self:
            expense.approval_state = _SHEET_TO_19.get(expense.sheet_id.approval_state, False)
            expense.approval_date = expense.sheet_id.approval_date

    @api.depends('sheet_id', 'sheet_id.payment_state')
    def _compute_payment_state(self):
        for expense in self:
            expense.payment_state = expense.sheet_id.payment_state

    @api.depends('sheet_id', 'sheet_id.account_move_ids')
    def _compute_account_move_id(self):
        for expense in self:
            # sudo: employees cannot read account.move, and this stored compute
            # runs during their own submit. Only the id is stored, and the field
            # itself is restricted to invoicing users.
            expense.account_move_id = fields.first(expense.sheet_id.sudo().account_move_ids)

    @api.depends('sheet_id', 'sheet_id.direct_auto_sheet', 'sheet_id.expense_line_ids')
    def _compute_direct_is_auto_sheet(self):
        for expense in self:
            sheet = expense.sheet_id
            expense.direct_is_auto_sheet = bool(
                sheet and sheet.direct_auto_sheet and len(sheet.expense_line_ids) == 1
            )

    # Depends on the sheet residual rather than account_move_id.amount_residual:
    # account_move_id is restricted to accountants and must not appear in
    # another field's depends.
    @api.depends('sheet_id', 'sheet_id.amount_residual', 'direct_is_auto_sheet')
    def _compute_amount_residual(self):
        for expense in self:
            sheet = expense.sheet_id
            # Rounded so that `not amount_residual` in the view and
            # `is_zero(amount_residual)` in action_pay give the same answer.
            # The extra `and sheet` keeps the compute safe if the stored flag
            # ever disagrees with the actual sheet.
            expense.amount_residual = (
                sheet.company_currency_id.round(sheet.amount_residual)
                if expense.direct_is_auto_sheet and sheet
                else 0.0
            )

    # depends_context('lang') so a translated message computed for one user is
    # not served from cache to a user with another language.
    @api.depends_context('lang')
    @api.depends('amount_residual', 'expense_state', 'direct_is_auto_sheet')
    def _compute_direct_outstanding_notice(self):
        for expense in self:
            # The direct_is_auto_sheet test also covers rows whose stored
            # residual predates that flag and was never recomputed.
            if (
                not expense.direct_is_auto_sheet
                or expense.expense_state != 'paid'
                or expense.company_currency_id.is_zero(expense.amount_residual)
            ):
                expense.direct_outstanding_notice = False
                continue
            expense.direct_outstanding_notice = _(
                "This expense is shown as Paid, but %(amount)s of it has not been "
                "paid out yet.",
                amount=formatLang(
                    self.env,
                    expense.amount_residual,
                    currency_obj=expense.company_currency_id,
                ),
            )

    def _direct_subordinate_employee_ids(self):
        """Return ids of employees in ``self`` sitting strictly under the acting user.

        Uses sudo because ordinary users cannot read ``hr.employee``. The result
        is only compared against, never written.
        """
        is_team_approver = (
            self.env.user.has_group('hr_expense.group_hr_expense_team_approver')
            or self.env.su
        )
        if not is_team_approver:
            return set()
        return set(self.env['hr.employee'].sudo().search([
            ('id', 'in', self.employee_id.ids),
            ('id', 'child_of', self.env.user.employee_ids.ids),
            ('id', 'not in', self.env.user.employee_ids.ids),
        ]).ids)

    # depends_context('uid'): the answer is per user and must not be cached
    # across users.
    @api.depends_context('uid')
    @api.depends('employee_id', 'expense_state')
    def _compute_can_reset(self):
        user = self.env.user
        is_all_approver = user.has_groups(
            'hr_expense.group_hr_expense_user,hr_expense.group_hr_expense_manager'
        ) or self.env.su

        valid_company_ids = set(self.env.companies.ids)
        expenses_employee_ids_under_user_ones = self._direct_subordinate_employee_ids()

        for expense in self:
            expense.can_reset = (
                expense.company_id.id in valid_company_ids
                and (
                    is_all_approver
                    or expense.employee_id.id in expenses_employee_ids_under_user_ones
                    or expense.employee_id.expense_manager_id == user
                    or (
                        expense.expense_state in {'draft', 'submitted'}
                        and expense.employee_id.user_id == user
                    )
                )
            )

    @api.depends_context('uid')
    @api.depends('employee_id', 'sheet_id.user_id')
    def _compute_can_approve(self):
        cannot_reason_per_record_id = self._get_cannot_approve_reason()
        for expense in self:
            expense.cannot_approve_reason = cannot_reason_per_record_id[expense.id]
            expense.can_approve = not cannot_reason_per_record_id[expense.id]

    def _get_cannot_approve_reason(self):
        """Return ``{expense_id: reason_or_False}`` as evaluated for the acting user.

        Follows Odoo 19, with the hidden sheet's ``user_id`` standing in for
        Odoo 19's ``expense.manager_id``. As in core, expense administrators
        skip the self-approval test. Messages keep core's wording so the
        standard translations apply.
        """
        user = self.env.user
        is_team_approver = user.has_group('hr_expense.group_hr_expense_team_approver') or self.env.su
        is_approver = user.has_group('hr_expense.group_hr_expense_user') or self.env.su
        is_hr_admin = user.has_group('hr_expense.group_hr_expense_manager') or self.env.su

        valid_company_ids = set(self.env.companies.ids)
        expenses_employee_ids_under_user_ones = self._direct_subordinate_employee_ids()

        reasons_per_record_id = {}
        for expense in self:
            reason = False
            expense_employee = expense.employee_id
            is_expense_team_approver = (
                # Admins are team approvers, not necessarily direct parents.
                is_team_approver
                or expense_employee.id in expenses_employee_ids_under_user_ones
                or (expense_employee.expense_manager_id == user)
            )
            if expense.company_id.id not in valid_company_ids:
                reason = _(
                    "%(expense_name)s: Your are neither a Manager nor a HR Officer of this expense's company",
                    expense_name=expense.name,
                )
            elif not is_expense_team_approver:
                reason = _(
                    "%(expense_name)s: You are neither a Manager nor a HR Officer",
                    expense_name=expense.name,
                )
            elif not is_hr_admin:
                current_manager_ids = set(expense_employee.expense_manager_id.ids)
                current_manager_ids |= set(
                    expense_employee.sudo().department_id.manager_id.user_id.ids
                )
                current_manager_ids |= set(expense.sheet_id.user_id.ids)
                if expense_employee.id in expenses_employee_ids_under_user_ones:
                    current_manager_ids.add(user.id)

                if expense_employee.user_id == user:
                    reason = _(
                        "%(expense_name)s: It is your own expense",
                        expense_name=expense.name,
                    )
                elif user.id not in current_manager_ids and not is_approver:
                    reason = _(
                        "%(expense_name)s: It is not from your department",
                        expense_name=expense.name,
                    )
            reasons_per_record_id[expense.id] = reason
        return reasons_per_record_id

    # ------------------------------------------------------------------
    # Permission and state guards
    #
    # Authorisation decisions are never taken under sudo: can_approve and
    # can_reset treat env.su as "is administrator", so a single sudo() on that
    # path would disable the self-approval check. The few sudo() calls in this
    # file are read-only lookups (approver resolution, posted-entry checks on
    # account.move) that decide nothing about who may act.
    # ------------------------------------------------------------------

    def _check_can_approve(self):
        """Raise unless every expense in ``self`` may be approved by the acting user."""
        if not all(self.mapped('can_approve')):
            reasons_list = tuple(
                reason for reason in self._get_cannot_approve_reason().values() if reason
            )
            raise UserError(_(
                "You cannot approve:\n %(reasons)s",
                reasons="\n".join(reasons_list),
            ))

    def _check_can_refuse(self):
        """Raise unless every expense in ``self`` may be refused by the acting user.

        Refusing is an approver's action, so it uses the approval permission,
        as Odoo 19 does.
        """
        if not all(self.mapped('can_approve')):
            reasons_list = tuple(
                reason for reason in self._get_cannot_approve_reason().values() if reason
            )
            raise UserError(_(
                "You cannot refuse:\n %(reasons)s",
                reasons="\n".join(reasons_list),
            ))

    def _can_be_autovalidated(self):
        """Return True when no approval authority exists for this expense.

        That is the case when no approver resolves at all, or when the approver
        is the employee themselves. Must be evaluated after the approver has
        been written on the sheet at submit time. Reads are done with sudo
        because employees cannot read their submitted sheet or
        ``expense_manager_id``.
        """
        self.ensure_one()
        sheet = self.sheet_id.sudo()
        employee = self.employee_id.sudo()
        return (
            (not sheet.user_id and not employee.expense_manager_id)
            or sheet.user_id == employee.user_id
        )

    def _direct_self_established_approval(self):
        """Return True when the employee is their own approver without HR having set it.

        Employees may be able to edit their own manager or department (the
        ``hr.hr_employee_self_edit`` setting), which could route approval back
        to themselves. Only ``expense_manager_id`` cannot be set by the employee,
        so self-approval is trusted only when it comes from that field. Such
        expenses are not auto-approved and wait for a human approver instead.
        """
        self.ensure_one()
        sheet = self.sheet_id.sudo()
        employee = self.employee_id.sudo()
        if not sheet.user_id or sheet.user_id != employee.user_id:
            return False
        return employee.expense_manager_id != employee.user_id

    # The validators below check the whole recordset before anything is
    # written, so a batch with one invalid expense is refused as a whole and
    # every offending expense is named in the message.

    def _direct_reason_line(self, expense, reason):
        """Format one already-translated ``reason`` as a line naming ``expense``."""
        return _(
            "%(expense_name)s: %(reason)s",
            expense_name=expense.name,
            reason=reason,
        )

    def _check_can_submit(self):
        """Raise unless every expense in ``self`` may be submitted, naming each that may not.

        The category and amount checks run here, before the sheet is created,
        so users get these messages instead of core's report-worded ones.
        """
        user = self.env.user
        reasons_per_record = []
        for expense in self:
            reason = False
            if expense.expense_state != 'draft':
                reason = _("You can only submit a draft expense.")
            elif user.employee_id != expense.employee_id and not expense.can_approve:
                reason = _("You do not have the required permission to submit this expense.")
            elif not expense.product_id:
                reason = _("You can not submit an expense without a category.")
            elif expense.currency_id.is_zero(expense.total_amount_currency):
                reason = _("You can not submit an expense without an amount.")
            reasons_per_record.append((expense, reason))

        reasons_list = tuple(
            self._direct_reason_line(expense, reason)
            for expense, reason in reasons_per_record
            if reason
        )
        if reasons_list:
            raise UserError(_(
                "You cannot submit:\n %(reasons)s",
                reasons="\n".join(reasons_list),
            ))

    def _check_can_post(self):
        """Raise unless every expense in ``self`` may be posted, naming each that may not.

        Payment mode is checked before state: it is a permanent property of the
        expense, so it is the more useful message.
        """
        reasons_per_record = []
        for expense in self:
            reason = False
            if expense.payment_mode != 'own_account':
                reason = _(
                    "Expenses paid by the company are posted and settled together with their payment. "
                    "Open the related payment in Accounting."
                )
            elif expense.expense_state != 'approved':
                reason = _("You can only generate an accounting entry for approved expense(s).")
            reasons_per_record.append((expense, reason))

        reasons_list = tuple(
            self._direct_reason_line(expense, reason)
            for expense, reason in reasons_per_record
            if reason
        )
        if reasons_list:
            raise UserError(_(
                "You cannot post:\n %(reasons)s",
                reasons="\n".join(reasons_list),
            ))

    def _check_can_refuse_state(self):
        """Raise unless every expense in ``self`` is in a refusable state, naming each that is not.

        Called from ``action_refuse`` so the reason dialog does not open for a
        selection that will be refused, and again from ``_do_refuse``, which is
        the enforcing call.
        """
        reasons_per_record = []
        for expense in self:
            reason = False
            # The posted-entry test comes first: a posted expense would
            # otherwise always hit the more generic state message. sudo because
            # the acting user may not be able to read account.move.
            if expense.sheet_id.sudo().account_move_ids.filtered(lambda move: move.state == 'posted'):
                reason = _("You cannot cancel an expense linked to a posted journal entry")
            elif expense.expense_state not in ('submitted', 'approved'):
                reason = _("You can only refuse a submitted or approved expense.")
            reasons_per_record.append((expense, reason))

        reasons_list = tuple(
            self._direct_reason_line(expense, reason)
            for expense, reason in reasons_per_record
            if reason
        )
        if reasons_list:
            raise UserError(_(
                "You cannot refuse:\n %(reasons)s",
                reasons="\n".join(reasons_list),
            ))

    def _check_can_reset_approval(self):
        """Raise unless every expense may be reset, and none is linked to a posted entry.

        Odoo 18's sheet reset checks only who the user is, then reverses posted
        moves under sudo. This check must therefore run before delegating to
        core, otherwise a reset would silently write reversal entries.
        """
        if not all(self.mapped('can_reset')):
            raise UserError(_(
                "Only HR Officers, accountants, or the concerned employee can reset to draft."
            ))
        posted = self.sheet_id.sudo().account_move_ids.filtered(lambda move: move.state == 'posted')
        if posted:
            raise UserError(_(
                "You cannot reset to draft an expense linked to a posted journal entry."
            ))

    # Expense-side half of the one-expense-per-auto-sheet rule. Writing
    # `sheet_id` on an expense does not trigger the sheet's own constraint on
    # `expense_line_ids`, so the rule is checked from both sides.
    @api.constrains('sheet_id')
    def _check_auto_sheet_membership(self):
        for expense in self:
            sheet = expense.sheet_id
            if sheet and sheet.direct_auto_sheet and len(sheet.expense_line_ids) > 1:
                raise ValidationError(_(
                    "An automatically created expense report holds exactly one expense."
                ))

    def _check_direct_sheet(self):
        """Refuse expenses that belong to a report not created by this module.

        Acting on one line of a legacy multi-line report would also submit,
        approve, refuse or reset all its other lines. Such expenses are managed
        from the report instead.
        """
        for expense in self:
            sheet = expense.sheet_id
            if sheet and not (sheet.direct_auto_sheet and len(sheet.expense_line_ids) == 1):
                raise UserError(_(
                    "%(expense_name)s belongs to a report created outside this workflow. "
                    "Manage it from that report.",
                    expense_name=expense.name,
                ))

    def _check_direct_access(self):
        """Raise unless the acting user may read every expense in ``self``.

        Called first in every public action, before any workflow check, so a
        user who cannot see an expense gets a generic refusal rather than a
        message naming it. Most actions write through the hidden sheet, whose
        record rules differ from the expense's, so this explicit check is
        needed. It must not be run under sudo.
        """
        refused = _("You do not have access to one or more of the selected expenses.")
        if not self.has_access('read'):
            raise AccessError(refused)
        if self._filtered_access('read') != self:
            raise AccessError(refused)

    # Exposed on the model so both overrides read it through `self`.
    _DIRECT_PROJECTIONS = _DIRECT_PROJECTIONS

    def write(self, vals):
        """Refuse writes to the projected fields from non-superuser callers.

        Computes assign these fields through the ORM's protected path and never
        reach this method, so legitimate updates are not affected.
        """
        if not self.env.su:
            forged = self._DIRECT_PROJECTIONS.intersection(vals)
            if forged:
                raise AccessError(_(
                    "%(fields)s cannot be written: they are derived from the approval "
                    "and payment history of this expense.",
                    fields=', '.join(sorted(forged)),
                ))
        return super().write(vals)

    @api.model_create_multi
    def create(self, vals_list):
        """Refuse projected fields in ``vals`` and drop their ``default_*`` context keys.

        Context defaults are applied inside core's create, after this override
        has inspected ``vals_list``, so they are removed here. Only the
        protected keys are dropped; other defaults are kept.
        """
        if not self.env.su:
            for vals in vals_list:
                forged = self._DIRECT_PROJECTIONS.intersection(vals)
                if forged:
                    raise AccessError(_(
                        "%(fields)s cannot be written: they are derived from the approval "
                        "and payment history of this expense.",
                        fields=', '.join(sorted(forged)),
                    ))
            protected_defaults = {'default_%s' % name for name in self._DIRECT_PROJECTIONS}
            if protected_defaults.intersection(self.env.context):
                self = self.with_context({
                    key: value
                    for key, value in self.env.context.items()
                    if key not in protected_defaults
                })
        return super().create(vals_list)

    # ------------------------------------------------------------------
    # Hidden sheet creation
    # ------------------------------------------------------------------

    def _lock_the_sheetless_for_bridging(self):
        """Lock the rows of ``self`` and return those that already have a sheet.

        Prevents two concurrent submits from each creating a sheet for the same
        expense. NO KEY UPDATE is enough to serialise this method without
        blocking foreign-key checks, and ORDER BY id makes overlapping batches
        lock in the same order. If another transaction created the sheet first,
        PostgreSQL raises a serialisation error and Odoo retries the request,
        which then finds the existing sheet.
        """
        self.flush_recordset(['sheet_id'])
        self.env.cr.execute(
            'SELECT id FROM hr_expense WHERE id IN %s ORDER BY id FOR NO KEY UPDATE',
            (tuple(self.ids),),
        )
        self.invalidate_recordset(['sheet_id'])
        return self.filtered('sheet_id')

    def _ensure_sheet(self):
        """Return one hidden sheet per expense, creating only the missing ones.

        Sheet values and their validations come from core's
        ``_get_default_expense_sheet_values``. No sudo is needed: employees may
        create their own draft sheets.
        """
        self._check_direct_sheet()
        sheets = self.env['hr.expense.sheet']
        expenses_needing_a_sheet = self.env['hr.expense']
        for expense in self:
            if expense.sheet_id:
                sheets |= expense.sheet_id
                continue
            expenses_needing_a_sheet |= expense

        if expenses_needing_a_sheet:
            already_bridged = expenses_needing_a_sheet._lock_the_sheetless_for_bridging()
            if already_bridged:
                sheets |= already_bridged.sheet_id
                expenses_needing_a_sheet -= already_bridged

        vals_list = []
        for expense in expenses_needing_a_sheet:
            # Called per expense: on several records core would group them
            # into shared sheets by payment mode.
            vals = expense._get_default_expense_sheet_values()[0]
            vals['direct_auto_sheet'] = True
            vals_list.append(vals)

        if vals_list:
            sheets |= self.env['hr.expense.sheet'].create(vals_list)
        return sheets

    # ------------------------------------------------------------------
    # Workflow actions
    #
    # Each action validates in Odoo 19's terms, then delegates to the
    # corresponding sheet method. No journal entry is ever created here; core
    # creates the draft entry at approval, and posting edits and posts it.
    # ------------------------------------------------------------------

    def action_submit(self):
        """Submit draft expenses, creating their hidden sheets as needed.

        Expenses with no approver, or whose approver is the employee, are
        approved automatically, as in Odoo 19. Returns nothing: the user is not
        navigated to a report.
        """
        self._check_direct_access()
        self._check_direct_sheet()
        self._check_can_submit()

        sheets = self._ensure_sheet()
        for sheet in sheets:
            if not sheet.user_id:
                sheet_sudo = sheet.sudo()
                sheet_sudo.user_id = fields.first(sheet_sudo._get_responsible_for_approval())

        # Evaluated after the approver has been resolved above, so only
        # expenses that really have no other approver are auto-approved.
        autovalidated = self.filtered(lambda expense: expense._can_be_autovalidated())

        # Self-approval the employee could have arranged themselves is not
        # auto-approved; the expense stays submitted and the reason is logged.
        self_routed = autovalidated.filtered(
            lambda expense: expense._direct_self_established_approval()
        )
        if self_routed:
            withheld_author_id = self.env['ir.model.data']._xmlid_to_res_id('base.partner_root')
            for expense in self_routed:
                resolved = expense.sheet_id.sudo().user_id
                _logger.warning(
                    "expenses_shortcut: auto-approval withheld: expense %s resolves to "
                    "its own submitter %s as approver, through a routing field an ordinary "
                    "employee can write; no HR officer set expense_manager_id. Submitted "
                    "for human approval instead.",
                    expense.id,
                    resolved.login,
                )
                expense._message_log(
                    body=_(
                        "Automatic approval was withheld: the approver on file for this "
                        "expense is %(approver)s, who submitted it, and no HR officer "
                        "designated them as their own Expense Manager. It is waiting for a "
                        "human approver instead.",
                        approver=resolved.name,
                    ),
                    author_id=withheld_author_id,
                )
            autovalidated -= self_routed

        autovalidated_sheets = autovalidated.sheet_id

        # Keep the hidden sheets from adding followers or tracking messages.
        muted_sheets = sheets.with_context(
            mail_create_nosubscribe=True,
            mail_auto_subscribe_no_notify=True,
            mail_notrack=True,
        )
        muted_autovalidated = muted_sheets & autovalidated_sheets

        (muted_sheets - autovalidated_sheets)._do_submit()

        if muted_autovalidated:
            # Submitted first so the history reads submit then approve. Odoo 18's
            # sheet _do_approve has no equivalent of Odoo 19's check=False, so
            # sudo is used; the employee has no approval rights of their own and
            # there is no approver to bypass. Approval also creates the draft
            # vendor bill. No try/except: any failure must roll back the
            # whole submit.
            muted_autovalidated._do_submit()
            muted_autovalidated.sudo()._do_approve()

            # Logged as OdooBot, and with _message_log because the employee no
            # longer has write access to the expense at this point.
            odoobot_partner_id = self.env['ir.model.data']._xmlid_to_res_id('base.partner_root')

            for expense in autovalidated:
                expense._message_log(
                    body=_("Automatically approved — no approver is configured for this employee."),
                    author_id=odoobot_partner_id,
                )

        self.update_activities_and_mails()

    def action_approve(self):
        """Approve submitted expenses through their hidden sheets.

        Core may return the duplicate-expense confirmation dialog instead of
        approving; that action is returned to the client as is. Approval also
        creates the draft journal entry.
        """
        self._check_direct_access()
        self._check_direct_sheet()
        self._check_can_approve()
        for expense in self:
            if expense.expense_state != 'submitted':
                raise UserError(_("You can only approve a submitted expense."))

        result = self.sheet_id.action_approve_expense_sheets()
        # A truthy result is the duplicate dialog: nothing was approved yet,
        # so the approval activity must stay open.
        if not result:
            self.update_activities_and_mails()
        return result

    def action_refuse(self):
        """Open the refuse-reason dialog for the selected expenses.

        Uses this module's own wizard: core's sheet wizard reads ``active_ids``
        as sheet ids and would refuse the wrong records.
        """
        self._check_direct_access()
        self._check_direct_sheet()
        self._check_can_refuse()
        self._check_can_refuse_state()
        action_window = self.env['ir.actions.act_window']
        action = action_window._for_xml_id('expenses_shortcut.expenses_shortcut_refuse_wizard_action')
        action['context'] = {
            **literal_eval(action.get('context') or '{}'),
            'active_model': 'hr.expense',
            'active_ids': self.ids,
        }
        return action

    def _do_refuse(self, reason):
        """Refuse these expenses through their hidden sheets, recording ``reason``.

        Called by the refuse wizard. The refusal message is posted by the sheet
        override, which every refusal path goes through.
        """
        self._check_direct_sheet()
        self._check_can_refuse()
        self._check_can_refuse_state()
        self.sheet_id._do_refuse(reason)
        self.update_activities_and_mails()

    def action_reset(self):
        """Reset expenses to draft, refusing if a journal entry is already posted.

        The hidden sheet is kept and reused on the next submit.
        """
        self._check_direct_access()
        self._check_direct_sheet()
        self._check_can_reset_approval()
        self.sheet_id.action_reset_expense_sheets()
        self.update_activities_and_mails()

    def update_activities_and_mails(self):
        """Keep each expense's approval activity in line with its state.

        The activity is scheduled for the resolved approver while the expense
        is submitted, marked done once it is approved, and removed when it goes
        back to draft or is refused. The method is idempotent: an existing
        activity is reassigned rather than duplicated, since
        ``activity_schedule`` does not deduplicate. It is also called from the
        sheet overrides, so it may run twice for the same change.

        Unlike Odoo 19 there is no fallback to the current user when no
        approver resolves; a warning is logged instead, so the submitter never
        receives their own approval request. Only the activity operations run
        under sudo.
        """
        self._check_direct_access()
        expenses_activity_done = self.env['hr.expense']
        expenses_activity_unlink = self.env['hr.expense']
        for expense in self:
            if expense.expense_state == 'submitted':
                # Resolved per expense: the sheet method is not multi-record safe.
                approver = fields.first(
                    expense.sheet_id.sudo()._get_responsible_for_approval()
                )
                if not approver:
                    _logger.warning(
                        "expenses_shortcut: expense %r (id %s, employee id %s) is "
                        "submitted but no approver resolves; no approval activity was "
                        "scheduled. Expenses without an approver are normally approved "
                        "automatically at submit; check the employee's expense approver.",
                        expense.name, expense.id, expense.employee_id.id,
                    )
                    continue

                # activity_search matches only automated activities, the same
                # ones activity_feedback and activity_unlink act on below.
                existing = expense.sudo().activity_search(
                    ['expenses_shortcut.mail_act_expense_approval']
                )
                if existing:
                    # Only the assignee is updated, so the deadline does not move.
                    existing.user_id = approver
                else:
                    expense.sudo().activity_schedule(
                        'expenses_shortcut.mail_act_expense_approval',
                        user_id=approver.id,
                    )
            elif expense.expense_state == 'approved':
                expenses_activity_done |= expense
            elif expense.expense_state in {'draft', 'refused'}:
                expenses_activity_unlink |= expense

        if expenses_activity_done:
            expenses_activity_done.sudo().activity_feedback(
                ['expenses_shortcut.mail_act_expense_approval']
            )
        if expenses_activity_unlink:
            expenses_activity_unlink.sudo().activity_unlink(
                ['expenses_shortcut.mail_act_expense_approval']
            )

    @api.model
    def _cron_send_submitted_expenses_mail(self):
        """Weekly reminder to approvers about expenses waiting for them."""
        expenses_submitted_to_review = self.search([('expense_state', '=', 'submitted')])
        if expenses_submitted_to_review:
            expenses_submitted_to_review._send_submitted_expenses_mail()

    def _send_submitted_expenses_mail(self):
        """Send one reminder per company and approver.

        Sender, footer and recipient address are company specific, so an
        approver with pending expenses in two companies gets two mails. Each
        mail is rendered in the approver's language. Missing sender or
        recipient addresses are logged and skipped without affecting the
        other mails.
        """
        new_mails = []
        for company, expenses_submitted_per_company in self.grouped('company_id').items():
            # parent_ids includes the company itself, so it is removed to fall
            # back to the nearest real parent.
            parent_company_mails = (company.parent_ids - company)[::-1].mapped('email_formatted')
            mail_from = (
                self.env.user.email
                or company.email_formatted
                or (parent_company_mails and parent_company_mails[0])
            )
            if not mail_from:
                _logger.warning(
                    "expenses_shortcut: cannot send the submitted-expenses digest for "
                    "company %r (id %s): no sender email resolves from the acting user, "
                    "the company, or any parent company.",
                    company.name, company.id,
                )
                continue

            for manager, dummy_expenses in expenses_submitted_per_company.grouped(
                lambda expense: expense.sheet_id.sudo().user_id
            ).items():
                # Should not happen: expenses without an approver are
                # auto-approved at submit.
                if not manager:
                    _logger.warning(
                        "expenses_shortcut: a submitted expense in company %r (id %s) "
                        "has no approver on its sheet; it should have been approved "
                        "automatically at submit. No digest mail was built for it.",
                        company.name, company.id,
                    )
                    continue

                # with_company: a user may have one employee per company.
                email_to = manager.with_company(company).employee_id.work_email or manager.email
                if not email_to:
                    _logger.warning(
                        "expenses_shortcut: approver %r (id %s) has no work_email and no "
                        "user email in company %r (id %s); their submitted-expenses digest "
                        "was skipped. Other approvers still receive theirs.",
                        manager.name, manager.id, company.name, company.id,
                    )
                    continue

                manager_langs = tuple(lang for lang in manager.partner_id.mapped('lang') if lang)
                mail_lang = (manager_langs and manager_langs[0]) or self.env.lang or 'en_US'

                # The body contains no amounts or employee data. The link is
                # the same for everyone: the target action only lists the
                # expenses assigned to the viewing user.
                body = self.env['ir.qweb']._render(
                    template='expenses_shortcut.expense_submitted_digest_template',
                    values={
                        'manager_name': manager.name,
                        'url': '/odoo/direct-expenses-to-process',
                        'company': company,
                    },
                    lang=mail_lang,
                )
                new_mails.append({
                    'author_id': self.env.user.partner_id.id,
                    'auto_delete': True,
                    'body_html': body,
                    'email_from': mail_from,
                    'email_to': email_to,
                    # A bare _() would translate in the cron user's language;
                    # env._ on an environment carrying mail_lang uses the
                    # recipient's.
                    'subject': self.with_context(lang=mail_lang).env._(
                        "New expenses waiting for your approval"
                    ),
                })

        if new_mails:
            self.env['mail.mail'].sudo().create(new_mails).send()

    # ------------------------------------------------------------------
    # Accounting actions
    # ------------------------------------------------------------------

    def action_post(self):
        """Open the post dialog for approved, employee-paid expenses."""
        self._check_direct_access()
        self._check_direct_sheet()
        self._check_can_post()
        if len(self.company_id) > 1:
            raise UserError(_(
                "You can't post simultaneously employee-paid expenses belonging to different companies"
            ))
        action_window = self.env['ir.actions.act_window']
        action = action_window._for_xml_id('expenses_shortcut.expenses_shortcut_post_wizard_action')
        # default_company_id makes the dialog's journal default and domain
        # follow the expense's company rather than the user's current one.
        action['context'] = {
            **literal_eval(action.get('context') or '{}'),
            'active_model': 'hr.expense',
            'active_ids': self.ids,
            'default_company_id': self.company_id.id,
        }
        return action

    def _do_post(self, journal, date):
        """Set the chosen journal and date, then post the entry created at approval.

        Every expense is validated before anything is written, so an invalid
        expense in the batch leaves all of them unchanged. When ``date`` is
        empty, the dates set at approval are kept. The checks are repeated here
        because the wizard, RPC callers and batch actions all call this method
        directly.
        """
        self._check_direct_sheet()
        self._check_can_post()
        if len(self.company_id) > 1:
            raise UserError(_(
                "You can't post simultaneously employee-paid expenses belonging to different companies"
            ))
        # Without a journal, posting would fail on a database constraint.
        if not journal:
            raise UserError(_(
                "Please specify an expense journal in order to generate accounting entries."
            ))
        # The dialog's journal domain only applies in the UI, so the journal is
        # validated again here.
        company = self.company_id
        if (
            journal.type != 'purchase'
            or not journal.active
            or journal.company_id not in (company | company.parent_ids)
        ):
            raise UserError(_(
                "Select an active purchase journal that belongs to the expense's company."
            ))
        # Model-level and record-level access, checked up front so the user gets
        # a message instead of an AccessError traceback.
        if not self.env['account.move'].has_access('write'):
            raise UserError(_("You don't have the rights to create accounting entries."))
        sheets = self.sheet_id
        draft_moves = sheets.account_move_ids.filtered(lambda move: move.state == 'draft')
        if (
            sheets._filtered_access('write') != sheets
            or draft_moves._filtered_access('write') != draft_moves
        ):
            raise UserError(_("You don't have the rights to create accounting entries."))
        # The sheet's journal_id is computed without an inverse, so the
        # writable employee_journal_id is set instead. The draft move has no
        # sequence number yet, so its journal can still be changed. The date
        # goes into invoice_date, letting core derive the accounting date and
        # apply lock dates.
        sheet_vals = {'employee_journal_id': journal.id}
        draft_moves_vals = {'journal_id': journal.id}
        if date:
            sheet_vals['accounting_date'] = date
            draft_moves_vals['invoice_date'] = date
        sheets.write(sheet_vals)
        if draft_moves:
            draft_moves.write(draft_moves_vals)
        # Core only creates moves for sheets that have none, so no second
        # entry is created here.
        sheets.action_sheet_move_post()
        # Remember the journal as the company default if none is set. This
        # writes a core company setting and is not undone on uninstall.
        if not company.expense_journal_id:
            company.sudo().expense_journal_id = journal

    def action_pay(self):
        """Open core's payment register for a posted, employee-paid expense.

        Single-record by design: partial payments across a mixed selection have
        no single correct meaning. The checks read the moves and the residual
        directly rather than ``expense_state``, which on Community shows a
        partly paid expense as paid.
        """
        self.ensure_one()
        self._check_direct_access()
        self._check_direct_sheet()
        for expense in self:
            if expense.payment_mode != 'own_account':
                raise UserError(_(
                    "Expenses paid by the company are posted and settled together with their payment. "
                    "Open the related payment in Accounting."
                ))
            # sudo: the accountant may not pass every account.move record rule,
            # which would hide the moves and give the wrong message.
            moves = expense.sheet_id.sudo().account_move_ids
            if not moves or any(move.state != 'posted' for move in moves):
                raise UserError(_("You can only register payment for posted journal entries."))
            # Checked here so the user is told before the register dialog opens.
            if expense.company_currency_id.is_zero(expense.amount_residual):
                raise UserError(_(
                    "You can't register a payment because there is nothing left to pay "
                    "on the selected journal items."
                ))
        return self.sheet_id.with_context(dont_redirect_to_payments=True).action_register_payment()

    def action_open_account_move(self):
        """Open this expense's journal entry, or its payment when the company paid.

        Raises instead of opening an empty form when there is nothing to show
        yet. Reading ``account_move_id`` already requires invoicing rights.
        """
        self.ensure_one()
        self._check_direct_access()
        self._check_direct_sheet()

        move = self.account_move_id
        no_target = _("There is no journal entry to open for this expense yet.")
        if not move:
            raise UserError(no_target)

        if self.payment_mode == 'own_account':
            res_model, record = 'account.move', move
        else:
            record = move.origin_payment_id
            if not record:
                raise UserError(no_target)
            res_model = 'account.payment'

        return {
            'type': 'ir.actions.act_window',
            'res_model': res_model,
            'name': record.name,
            'view_mode': 'form',
            'res_id': record.id,
            'views': [(False, 'form')],
        }

    # ------------------------------------------------------------------
    # Core entry points that would open the hidden report
    #
    # Hiding their buttons is not enough, since RPC callers and other modules
    # can still call them. They keep working but never return a window on
    # hr.expense.sheet. Core's get_expenses_to_submit ends by calling
    # action_submit_expenses, so it is covered as well.
    # ------------------------------------------------------------------

    def action_submit_expenses(self):
        """Submit through ``action_submit`` instead of opening the created report."""
        return self.action_submit()

    def action_view_sheet(self):
        """Open the expense's journal entry or payment instead of its hidden report."""
        self.ensure_one()
        return self.action_open_account_move()
