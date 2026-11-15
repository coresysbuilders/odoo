# Meta Lead Ads Integration for Odoo — © 2026 CoreSys Builders. All rights reserved.
# Licensed under the Odoo Proprietary License v1.0 (OPL-1).
# Unauthorized copying, redistribution, or resale of this software, in whole or in
# part, via any medium, is strictly prohibited and constitutes a license violation.
# OPL-1: https://www.odoo.com/documentation/18.0/legal/licenses.html#odoo-apps

"""Count the Meta apps a database already holds.

A database now holds one Meta app. Some databases were connected to more
than one before that rule, and the upgrade must not fail on them: nothing is
archived or changed here, the accounts stay as they are. The number of
different App IDs on active accounts is logged, as a warning when there is
more than one, so whoever runs the upgrade knows the accounts and the Meta
settings will show a warning until the accounts of the other apps are
archived.

Only the count is logged, never an App ID, account or secret.
"""
import logging

_logger = logging.getLogger(__name__)

VERSION = '18.0.13.11.0'


def migrate(cr, version):
    if not version:
        return None
    # Spaces around the App ID are ignored, as the model ignores them.
    cr.execute("""
        SELECT count(DISTINCT btrim(app_id))
          FROM meta_account
         WHERE active AND app_id IS NOT NULL AND btrim(app_id) != ''
    """)
    count = cr.fetchone()[0]
    if count > 1:
        _logger.warning(
            "meta_lead_ads: %s different App IDs on active Meta accounts. "
            "Only one app per database is supported; the accounts and Meta "
            "settings show a warning until the accounts of the other apps "
            "are archived.", count)
    else:
        _logger.info(
            "meta_lead_ads: %s App ID(s) on active Meta accounts", count)
    return {'active_app_ids': count}
