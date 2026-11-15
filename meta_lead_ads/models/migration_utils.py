# Meta Lead Ads Integration for Odoo — © 2026 CoreSys Builders. All rights reserved.
# Licensed under the Odoo Proprietary License v1.0 (OPL-1).
# Unauthorized copying, redistribution, or resale of this software, in whole or in
# part, via any medium, is strictly prohibited and constitutes a license violation.
# OPL-1: https://www.odoo.com/documentation/18.0/legal/licenses.html#odoo-apps

"""Plain SQL helpers for upgrade scripts.

Nothing here uses the ORM: pre-migrate scripts run before the new models are
set up. Scripts import this module by its full path, since the version
folders under migrations/ are not packages.
"""
from odoo.tools import SQL

# Batches bound memory and statement size, not commit cadence. Odoo commits
# once per module, so a failed upgrade still rolls back as a whole.
BATCH_SIZE = 2000


def iter_id_batches(cr, table, where=None, batch_size=None):
    """Yield ascending lists of ids from ``table`` matching ``where`` (an SQL).

    Paging is keyset on id: each query starts after the last id returned, so
    rows the caller updates, or leaves unresolved, never shift or repeat a
    page, and the loop ends even when those rows keep matching ``where``.
    Works the same for parent tables and for child tables filtered by a list
    of parent ids.

    ``table`` must be a literal table name from the calling script.
    """
    # Read at call time so tests can patch BATCH_SIZE.
    size = batch_size or BATCH_SIZE
    condition = where or SQL("TRUE")
    last_id = 0
    while True:
        cr.execute(SQL(
            "SELECT id FROM %s WHERE id > %s AND (%s) ORDER BY id LIMIT %s",
            SQL.identifier(table), last_id, condition, size))
        ids = [row[0] for row in cr.fetchall()]
        if not ids:
            return
        yield ids
        last_id = ids[-1]


def iter_child_batches(cr, table, parent_column, parent_ids, where=None,
                       batch_size=None):
    """Yield lists of ids from ``table`` whose ``parent_column`` is in
    ``parent_ids`` and that match ``where`` (an SQL).

    Paging is keyset on (parent, id), so rows come ordered by parent and,
    within one parent, by ascending id. Paging by id alone makes Postgres
    walk the primary key of a large table and filter almost every row;
    ordering by the parent lets it read through the index on that column.

    ``table`` and ``parent_column`` must be literal names from the calling
    script, and the column needs an index.
    """
    size = batch_size or BATCH_SIZE
    condition = where or SQL("TRUE")
    column = SQL.identifier(parent_column)
    parents = list(parent_ids)
    last_parent, last_id = 0, 0
    while True:
        # The plain >= on the parent gives the planner an index range; the
        # row comparison does the exact keyset step.
        cr.execute(SQL(
            "SELECT %s, id FROM %s WHERE %s = ANY(%s) AND %s >= %s "
            "AND (%s, id) > (%s, %s) AND (%s) ORDER BY %s, id LIMIT %s",
            column, SQL.identifier(table), column, parents, column,
            last_parent, column, last_parent, last_id, condition, column,
            size))
        rows = cr.fetchall()
        if not rows:
            return
        yield [row[1] for row in rows]
        last_parent, last_id = rows[-1]


def main_company_id(cr):
    """Id of base.main_company, else the lowest company id, else None."""
    cr.execute("SELECT res_id FROM ir_model_data "
               "WHERE module = 'base' AND name = 'main_company'")
    row = cr.fetchone()
    if row:
        return row[0]
    cr.execute("SELECT id FROM res_company ORDER BY id LIMIT 1")
    row = cr.fetchone()
    return row[0] if row else None
