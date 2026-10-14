# Part of Expenses Shortcut by CoreSys Builders. License LGPL-3.0 or later.
#
# Post wizard modelled on Odoo 19's expense posting dialog. It only collects
# the journal and accounting date; all posting logic lives in
# hr.expense._do_post, which posts the draft entry created at approval rather
# than creating a new one. The model name is prefixed so it never clashes with
# a core model of the same purpose.

from odoo import _, api, fields, models
from odoo.exceptions import UserError


class HrExpenseDirectPostWizard(models.TransientModel):
    """Wizard to choose the journal and the accounting date at post time."""

    _name = 'expenses.shortcut.post.wizard'
    _description = "Expense Posting Wizard"

    def _default_company(self):
        # Use the company of the selected expenses when the caller provides it,
        # rather than the user's active company, so the default journal and the
        # company check match the expenses being posted.
        return self.env['res.company'].browse(self.env.context.get('default_company_id')) or self.env.company

    def _default_journal_id(self):
        """The journal determines the company of the accounting entries generated from the expense."""
        company = self._default_company()
        company_journal_id = company.expense_journal_id
        if company_journal_id:
            return company_journal_id.id
        closest_parent_company_journal = company.parent_ids[::-1].expense_journal_id[:1]
        if closest_parent_company_journal:
            return closest_parent_company_journal.id

        journal = self.env['account.journal'].search([
            *self.env['account.journal']._check_company_domain(company.id),
            ('type', '=', 'purchase'),
        ], limit=1)
        return journal.id

    expense_ids = fields.Many2many(comodel_name='hr.expense')
    # Not displayed; used by check_company on the journal.
    company_id = fields.Many2one(
        comodel_name='res.company',
        string="Company",
        readonly=True,
        default=lambda self: self._default_company(),
    )
    # Not required at form level; _do_post raises a clear error if it is empty,
    # which also covers callers that never open this form.
    employee_journal_id = fields.Many2one(
        comodel_name='account.journal',
        string="Journal",
        default=lambda self: self._default_journal_id(),
        check_company=True,
        domain="[('type', '=', 'purchase')]",
        help="The journal used when the expense is paid by employee.",
    )
    accounting_date = fields.Date(
        string="Accounting date",
        default=fields.Date.context_today,
        help="Specify the bill date of the related vendor bill.",
    )

    @api.model
    def default_get(self, fields):
        res = super().default_get(fields)
        # active_ids carry no model, so only trust them when the caller is
        # hr.expense; otherwise unrelated ids could be read as expense ids.
        if 'expense_ids' in fields and self.env.context.get('active_model') == 'hr.expense':
            res['expense_ids'] = self.env.context.get('active_ids', [])
        return res

    def action_post_entry(self):
        # The selection can be empty if record rules filtered it out; refuse
        # rather than report success. _do_post performs the full checks.
        if not self.expense_ids:
            raise UserError(_("You can only generate an accounting entry for approved expense(s)."))
        self.expense_ids._do_post(self.employee_journal_id, self.accounting_date)
        # Close instead of opening the journal entry: on Odoo 18 the entry form
        # links back to the hidden expense report.
        return {'type': 'ir.actions.act_window_close'}
