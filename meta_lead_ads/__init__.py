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
from .models.meta_field_mapping import _like_prefix

_logger = logging.getLogger(__name__)


def post_init_hook(env):
    """Record the install time and remind the admin to check the scheduler.

    Automatic lead sync needs Odoo's cron worker, so the Meta Account form
    shows a 'Scheduler' status worth checking right after install."""
    env['ir.config_parameter'].set_param(
        'meta_lead_ads.installed_at', fields.Datetime.to_string(fields.Datetime.now()))
    _logger.info(
        "Meta Lead Ads installed. Open CRM > Configuration > Meta Accounts and "
        "check the Scheduler status to confirm cron is running (it powers "
        "automatic lead sync).")


def uninstall_hook(env):
    """Remove the settings the module writes at runtime.

    Odoo's uninstall deletes the settings from data files, which have an
    external id, but not the run stamps written through set_param (last
    drain, backfill and webhook run, install time). Every key under the
    module's own prefix is removed here, the data-file ones too since they
    go anyway, and only their number is logged."""
    params = env['ir.config_parameter'].sudo().search(
        [('key', '=like', _like_prefix('meta_lead_ads.'))])
    count = len(params)
    params.unlink()
    _logger.info("Meta Lead Ads uninstalled: removed %d setting(s).",
                 count)
