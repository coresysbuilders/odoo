# Meta Lead Ads Integration for Odoo — © 2026 CoreSys Builders. All rights reserved.
# Licensed under the Odoo Proprietary License v1.0 (OPL-1).
# Unauthorized copying, redistribution, or resale of this software, in whole or in
# part, via any medium, is strictly prohibited and constitutes a license violation.
# OPL-1: https://www.odoo.com/documentation/19.0/legal/licenses.html#odoo-apps

import logging

from odoo import fields

from . import models
from . import wizard
from . import controllers

_logger = logging.getLogger(__name__)


def post_init_hook(env):
    """Stamp the install time and remind the admin to confirm the scheduler.
    Automatic lead sync only works if Odoo's cron worker is running, so the
    Meta Account form shows a live 'Scheduler' status the admin should check
    right after installing."""
    env['ir.config_parameter'].set_param(
        'meta_lead_ads.installed_at', fields.Datetime.to_string(fields.Datetime.now()))
    _logger.info(
        "Meta Lead Ads installed. Open CRM > Configuration > Meta Accounts and "
        "check the Scheduler status to confirm cron is running (it powers "
        "automatic lead sync).")
