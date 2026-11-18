# Meta Lead Ads Integration for Odoo — © 2026 CoreSys Builders. All rights reserved.
# Licensed under the Odoo Proprietary License v1.0 (OPL-1).
# Unauthorized copying, redistribution, or resale of this software, in whole or in
# part, via any medium, is strictly prohibited and constitutes a license violation.
# OPL-1: https://www.odoo.com/documentation/20.0/legal/licenses.html#odoo-apps

"""Give older failures a time.

1. Failed sync rows written before this release get their creation time as
   their failure time, so the webhook health can compare them with the last
   success.
2. The webhook success stamp is not seeded here. The upgrade step before
   this one already started it from the webhook rows the drain wrote, and a
   finished queue event proves nothing, because the backfill, a manual
   retry and the drain's healing also mark events done.
3. Without a stamp the dashboard counts only the last week's webhook
   failures and says no success is recorded yet, until the drain finishes
   its next event.

Only counts are logged, never a lead id, name or secret.
"""
import logging

from odoo.tools import SQL

from odoo.addons.meta_lead_ads.models.migration_utils import iter_id_batches

_logger = logging.getLogger(__name__)

VERSION = '19.0.1.9.1'


def _seed_failed_at(cr):
    # A row that already has a failure time was timed by this release and
    # is left alone.
    rows = 0
    for ids in iter_id_batches(cr, 'meta_sync_log',
                               SQL("status = 'failed' AND failed_at IS NULL")):
        cr.execute("UPDATE meta_sync_log SET failed_at = create_date "
                   "WHERE id = ANY(%s)", [ids])
        rows += cr.rowcount
    return rows


def migrate(cr, version):
    if not version:
        return None
    counts = {'rows': _seed_failed_at(cr)}
    _logger.info(
        "meta_lead_ads: %(rows)s failed sync row(s) got a failure time.",
        counts)
    return counts
