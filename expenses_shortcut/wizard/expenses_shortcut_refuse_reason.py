# Part of Expenses Shortcut by CoreSys Builders. License LGPL-3.0 or later.
#
# Refuse wizard working on expenses rather than expense reports. It uses its
# own model name because core already has a report-based refuse wizard, and
# reusing that name would replace core's model. The refusal itself is handled
# by core's hr.expense.sheet._do_refuse and this module's override of it.

from odoo import _, api, fields, models
from odoo.exceptions import UserError


class HrExpenseDirectRefuseWizard(models.TransientModel):
    """Wizard to specify the reason for refusing an expense."""

    _name = 'expenses.shortcut.refuse.wizard'
    _description = "Expense Refuse Reason Wizard"

    reason = fields.Char(string='Reason', required=True)
    expense_ids = fields.Many2many(comodel_name='hr.expense')

    @api.model
    def default_get(self, fields):
        res = super().default_get(fields)
        # active_ids carry no model, so only trust them when the caller is
        # hr.expense; otherwise unrelated ids could be read as expense ids.
        if 'expense_ids' in fields and self.env.context.get('active_model') == 'hr.expense':
            res['expense_ids'] = self.env.context.get('active_ids', [])
        return res

    def action_refuse(self):
        # The selection can be empty if record rules filtered it out; refuse
        # rather than report success. The message names no record so that it
        # does not reveal which records exist.
        if not self.expense_ids:
            raise UserError(_("There are no expenses to refuse."))
        # `required=True` does not reject a blank string, and the reason is the
        # whole content of the notification sent to the employee.
        if not (self.reason or '').strip():
            raise UserError(_("A reason is required to refuse an expense."))
        self.expense_ids._do_refuse(self.reason)
        return {'type': 'ir.actions.act_window_close'}
