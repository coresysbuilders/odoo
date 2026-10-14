# Part of Expenses Shortcut by CoreSys Builders. License LGPL-3.0 or later.
"""Install and uninstall hooks.

``pre_init_hook`` records the original values of the core menus and actions the
module hides, before the data files change them. ``post_init_hook`` only reads
and logs: it checks that the core API the module relies on is present and
reports the result of the initial classification. ``uninstall_hook`` writes the
recorded values back, because Odoo does not revert changes made to records
owned by another module.

The install hooks do not run on module upgrade; upgrades go through
``migrations/``.
"""

import json
import logging

_logger = logging.getLogger(__name__)

# Private hr.expense.sheet methods the module relies on, directly or through
# core. Private API can change within the 18.0 branch without a version bump,
# so their presence is checked at install time.
REQUIRED_SHEET_API = (
    '_do_submit',
    '_do_approve',
    '_do_refuse',
    # Reached indirectly through core's approve and reset actions.
    '_do_create_moves',
    '_do_reverse_moves',
    '_do_reset_approval',
    '_check_can_approve',
    '_check_can_refuse',
    '_check_can_reset_approval',
    'action_reset_expense_sheets',
    'action_approve_expense_sheets',
    # Overridden to suppress the report's approval activity.
    'activity_update',
    '_get_responsible_for_approval',
    'action_sheet_move_post',
    'action_register_payment',
)

# Above this many candidates the approver check reports the pre-filter count
# instead of calling core's resolver once per employee.
APPROVER_CONFIRMATION_CAP = 500

# The core records hidden by data/hr_expense_concealment.xml. `stock_default` is
# the Odoo 18 default, used on uninstall only when no captured value exists.
CONCEALMENT_TARGETS = {
    # "Expense Reports" menus in the Expenses app.
    'hr_expense.menu_hr_expense_sheet_my_reports': {
        'model': 'ir.ui.menu',
        'conceal': {'active': False},
        'stock_default': {'active': True},
    },
    'hr_expense.menu_hr_expense_report': {
        'model': 'ir.ui.menu',
        'conceal': {'active': False},
        'stock_default': {'active': True},
    },
    # Accounting > Vendors > Employee Expenses.
    'hr_expense.menu_hr_expense_account_employee_expenses': {
        'model': 'ir.ui.menu',
        'conceal': {'active': False},
        'stock_default': {'active': True},
    },
    # Report actions with a URL path. The path is cleared to NULL rather than
    # '' because the column has a unique index.
    'hr_expense.action_hr_expense_sheet_my_all': {
        'model': 'ir.actions.act_window',
        'conceal': {'path': False},
        'stock_default': {'path': 'my-expense-reports'},
    },
    'hr_expense.action_hr_expense_sheet_all': {
        'model': 'ir.actions.act_window',
        'conceal': {'path': False},
        'stock_default': {'path': 'expense-reports'},
    },
}

# Created with set_param, so it has no xmlid and uninstall_hook deletes it.
CONCEALMENT_BASELINE_PARAM = 'expenses_shortcut.concealment_baseline'


def capture_concealment_baseline(env):
    """Record the values of the hidden core records before they are changed.

    Called from ``pre_init_hook`` on install and from the pre-migration script
    on upgrade. An existing capture is never overwritten, otherwise the hidden
    values would become the restore target. Failures are logged as warnings
    and never block the install.
    """
    try:
        params = env['ir.config_parameter'].sudo()
        if params.get_param(CONCEALMENT_BASELINE_PARAM):
            _logger.info(
                "expenses_shortcut: %s already exists; the pre-install baseline is "
                "captured once and never overwritten",
                CONCEALMENT_BASELINE_PARAM,
            )
            return

        baseline = {}
        for xmlid, target in CONCEALMENT_TARGETS.items():
            record = env.ref(xmlid, raise_if_not_found=False)
            if not record:
                # Uninstall falls back to `stock_default` for this record.
                _logger.info(
                    "expenses_shortcut: %s does not resolve on this database; it is "
                    "left out of the concealment baseline",
                    xmlid,
                )
                continue
            baseline[xmlid] = {
                field: record[field] for field in target['conceal']
            }

        params.set_param(
            CONCEALMENT_BASELINE_PARAM,
            json.dumps(baseline, sort_keys=True),
        )
        _logger.info(
            "expenses_shortcut: captured the pre-install state of %s of %s core "
            "records into %s",
            len(baseline), len(CONCEALMENT_TARGETS), CONCEALMENT_BASELINE_PARAM,
        )
    except Exception:  # noqa: BLE001
        _logger.warning(
            "expenses_shortcut: could not capture the pre-install state of the "
            "concealed core records. Uninstall will restore documented Odoo 18 "
            "defaults instead, and will name every record it defaults.",
            exc_info=True,
        )


def pre_init_hook(env):
    """Capture the original values before the module's data files run."""
    capture_concealment_baseline(env)


def _log_approver_coverage(env, sheet_model):
    """Log how many active employees have no expense approver.

    Expenses of such employees are approved automatically on submit, which on
    Odoo 18 also creates the draft vendor bill. This is an install-time report
    only; nothing is written (``.new()`` records are never saved).
    """
    employees = env['hr.employee'].sudo().search([('active', '=', True)])
    if not employees:
        _logger.info("expenses_shortcut: no active employees to check for approver coverage")
        return

    # Prefetch each relation in one query.
    employees.mapped('expense_manager_id')
    employees.mapped('parent_id.user_id')
    employees.mapped('department_id.manager_id.user_id')
    # The same three sources core's `_get_expense_managers` uses, in the same
    # order. Employees with any of them set certainly have an approver.
    candidates = employees.filtered(lambda employee: not (
        employee.expense_manager_id
        or employee.parent_id.user_id
        or employee.department_id.manager_id.user_id
    ))
    total = len(employees)

    if len(candidates) > APPROVER_CONFIRMATION_CAP:
        _logger.warning(
            "expenses_shortcut: %s of %s active employees appear to have no expense "
            "approver -- UNCONFIRMED, the pre-filter only: %s candidates exceeds the "
            "%s-candidate cap so core's own resolver was not run. Expenses of "
            "employees without an approver are approved automatically on submit.",
            len(candidates), total, len(candidates), APPROVER_CONFIRMATION_CAP,
        )
        return

    # Confirm with core's resolver, through an unsaved report for the employee.
    unconfirmed = False
    without_approver = 0
    try:
        for employee in candidates:
            draft_sheet = sheet_model.new({'employee_id': employee.id})
            if not draft_sheet.sudo()._get_responsible_for_approval():
                without_approver += 1
    except Exception:  # noqa: BLE001 -- a diagnostic must never fail an install
        # Fall back to the pre-filter count, reported as unconfirmed.
        unconfirmed = True
        without_approver = len(candidates)
        _logger.info(
            "expenses_shortcut: could not confirm approver coverage through "
            "hr.expense.sheet._get_responsible_for_approval; falling back to the "
            "three-leg pre-filter",
        )

    qualifier = "unconfirmed pre-filter" if unconfirmed else "resolver-confirmed"
    if without_approver:
        _logger.warning(
            "expenses_shortcut: %s of %s active employees have no expense approver (%s). "
            "Their expenses are approved automatically on submit, and on Odoo 18 approval "
            "also creates the draft vendor bill. Set an expense approver (or a manager) "
            "for these employees before going live. This is an install-time snapshot only.",
            without_approver, total, qualifier,
        )
    else:
        _logger.info(
            "expenses_shortcut: every one of %s active employees has an expense approver "
            "(%s). This is an install-time snapshot only.",
            total, qualifier,
        )


def post_init_hook(env):
    """Discard install-time tracking, check the core API, and log statistics."""
    sheet_model = env['hr.expense.sheet']
    missing = [name for name in REQUIRED_SHEET_API if not hasattr(sheet_model, name)]
    if missing:
        # Logged rather than raised, so the drift is visible without blocking
        # the install.
        _logger.error(
            "expenses_shortcut: hr.expense.sheet is missing %s -- core API changed",
            missing,
        )

    expenses = env['hr.expense'].search([])

    # The new stored computed fields are tracked. Discard tracking before the
    # flush computes them, so existing expenses do not each get a chatter
    # message on install.
    expenses._track_discard()

    env.flush_all()

    # No explicit recompute is needed: Odoo queues every existing row for
    # computation when a new stored computed column is added.
    histogram = env['hr.expense']._read_group([], ['expense_state'], ['__count'])
    _logger.info(
        "expenses_shortcut: classified %s expenses: %s",
        len(expenses),
        {state: count for state, count in histogram},
    )

    _log_approver_coverage(env, sheet_model)


def uninstall_hook(env):
    """Restore the hidden core records to the values they had before install.

    The captured values are used, so any prior customisation is preserved; the
    Odoo 18 defaults apply only when no capture exists. Each write is guarded
    separately and failures are logged, so the uninstall itself never fails.
    """
    params = env['ir.config_parameter'].sudo()

    baseline = {}
    try:
        payload = params.get_param(CONCEALMENT_BASELINE_PARAM)
        if payload:
            baseline = json.loads(payload)
        else:
            _logger.warning(
                "expenses_shortcut: no pre-install baseline found under %s. Every "
                "concealed record below is restored to a documented Odoo 18 default "
                "instead of to the value this database actually held.",
                CONCEALMENT_BASELINE_PARAM,
            )
    except Exception:  # noqa: BLE001 -- an unreadable payload is not a failure
        _logger.warning(
            "expenses_shortcut: the pre-install baseline under %s could not be read; "
            "falling back to documented Odoo 18 defaults",
            CONCEALMENT_BASELINE_PARAM, exc_info=True,
        )

    unrestored = []
    for xmlid, target in CONCEALMENT_TARGETS.items():
        captured = baseline.get(xmlid) if isinstance(baseline, dict) else None
        if captured is None:
            values = dict(target['stock_default'])
            _logger.warning(
                "expenses_shortcut: the pre-install value of %s was not available, so "
                "the documented Odoo 18 default %s is being written instead",
                xmlid, values,
            )
        else:
            # Only write the fields the module actually changes.
            values = {
                field: captured[field]
                for field in target['conceal']
                if field in captured
            }
            if not values:
                values = dict(target['stock_default'])
                _logger.warning(
                    "expenses_shortcut: the captured baseline for %s covers none of "
                    "the concealed fields, so the documented Odoo 18 default %s is "
                    "being written instead",
                    xmlid, values,
                )

        record = env.ref(xmlid, raise_if_not_found=False)
        if not record:
            # hr_expense may already have been uninstalled.
            _logger.info("expenses_shortcut: %s is gone; nothing to restore", xmlid)
            continue

        try:
            record.sudo().write(values)
        except Exception:  # noqa: BLE001
            unrestored.append(xmlid)
            _logger.warning(
                "expenses_shortcut: could not restore %s to %s", xmlid, values,
                exc_info=True,
            )

    try:
        params.search([('key', '=', CONCEALMENT_BASELINE_PARAM)]).unlink()
    except Exception:  # noqa: BLE001
        _logger.warning(
            "expenses_shortcut: could not delete %s. It is inert, but it is residue: "
            "delete the row by hand.",
            CONCEALMENT_BASELINE_PARAM, exc_info=True,
        )

    if unrestored:
        _logger.warning(
            "expenses_shortcut: %s core record(s) were NOT restored on uninstall: %s. "
            "Repair by re-activating the named records by hand, or by reinstalling "
            "and uninstalling again once whatever blocked the write is cleared.",
            len(unrestored), unrestored,
        )
