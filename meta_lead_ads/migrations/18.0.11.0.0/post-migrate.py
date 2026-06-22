# Meta Lead Ads Integration for Odoo — © 2026 CoreSys Builders. All rights reserved.
# Licensed under the Odoo Proprietary License v1.0 (OPL-1).
# Unauthorized copying, redistribution, or resale of this software, in whole or in
# part, via any medium, is strictly prohibited and constitutes a license violation.
# OPL-1: https://www.odoo.com/documentation/18.0/legal/licenses.html#odoo-apps

"""Move existing deployments from the former 90-day sync-log retention default to
the new 14-day default (feature: auto-clear sync logs after 14 days).

The ``meta_lead_ads.sync_log_retention_days`` config parameter is seeded under
``noupdate="1"`` so the XML value change does not reach existing databases on
``-u``. This one-time migration nudges only deployments still on the OLD default
(90) to 14, leaving any admin-customized value untouched.
"""
import logging

from odoo import SUPERUSER_ID, api

_logger = logging.getLogger(__name__)

_KEY = 'meta_lead_ads.sync_log_retention_days'


def migrate(cr, version):
    env = api.Environment(cr, SUPERUSER_ID, {})
    icp = env['ir.config_parameter']
    current = icp.get_param(_KEY)
    # Only adopt the new default where the value is unset or still the former
    # 90-day default; respect any deliberate admin override.
    if current in (False, None, '', '90'):
        icp.set_param(_KEY, '14')
        _logger.info(
            "meta_lead_ads: sync-log retention moved to 14 days "
            "(was %r).", current)
    else:
        _logger.info(
            "meta_lead_ads: sync-log retention left at admin value %r "
            "(new default 14 not forced).", current)
