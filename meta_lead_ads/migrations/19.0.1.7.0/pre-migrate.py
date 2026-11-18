# Meta Lead Ads Integration for Odoo — © 2026 CoreSys Builders. All rights reserved.
# Licensed under the Odoo Proprietary License v1.0 (OPL-1).
# Unauthorized copying, redistribution, or resale of this software, in whole or in
# part, via any medium, is strictly prohibited and constitutes a license violation.
# OPL-1: https://www.odoo.com/documentation/20.0/legal/licenses.html#odoo-apps

"""Empty the Meta name cache before it becomes per company.

Cached campaign, ad set, ad and form names now belong to the company whose
Page fetched them. Old rows have no company and there is no safe way to tell
whose they were, since one row served every company. They are deleted here,
in id batches, before the schema update adds the required company column.
Names are fetched from Meta again the next time a lead needs them.

Names already stamped on leads are not touched: crm_lead is not read or
written here.
"""
import logging

from odoo.tools import SQL
from odoo.tools.sql import column_exists, table_exists

from odoo.addons.meta_lead_ads.models.migration_utils import iter_id_batches

_logger = logging.getLogger(__name__)


def migrate(cr, version):
    if not version:
        return None
    if not table_exists(cr, 'meta_name_cache'):
        return {'name_cache_dropped': 0}
    # On a real upgrade the column doesn't exist yet, so every row is
    # without a company. If it does exist (the script run again), only the
    # blank rows go.
    if column_exists(cr, 'meta_name_cache', 'company_id'):
        where = SQL("company_id IS NULL")
    else:
        where = None
    count = 0
    for ids in iter_id_batches(cr, 'meta_name_cache', where):
        cr.execute(SQL("DELETE FROM meta_name_cache WHERE id = ANY(%s)", ids))
        count += cr.rowcount
    _logger.info(
        "meta_lead_ads: dropped %s cached Meta names without a company", count)
    return {'name_cache_dropped': count}
