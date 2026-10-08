# Meta Lead Ads Integration for Odoo — © 2026 CoreSys Builders. All rights reserved.
# Licensed under the Odoo Proprietary License v1.0 (OPL-1).
# Unauthorized copying, redistribution, or resale of this software, in whole or in
# part, via any medium, is strictly prohibited and constitutes a license violation.
# OPL-1: https://www.odoo.com/documentation/18.0/legal/licenses.html#odoo-apps

"""Lower the sync-log retention default from 90 to 14 days on existing databases.

The parameter is seeded with noupdate="1", so changing the XML value does not
reach databases that are only upgraded. Values an admin changed are kept.
"""
import logging

from odoo import SUPERUSER_ID, api

_logger = logging.getLogger(__name__)

_KEY = 'meta_lead_ads.sync_log_retention_days'


def migrate(cr, version):
    env = api.Environment(cr, SUPERUSER_ID, {})
    icp = env['ir.config_parameter']
    current = icp.get_param(_KEY)
    # Only touch it if unset or still on the old default.
    if current in (False, None, '', '90'):
        icp.set_param(_KEY, '14')
        _logger.info(
            "meta_lead_ads: sync-log retention moved to 14 days "
            "(was %r).", current)
    else:
        _logger.info(
            "meta_lead_ads: sync-log retention left at admin value %r "
            "(new default 14 not forced).", current)
