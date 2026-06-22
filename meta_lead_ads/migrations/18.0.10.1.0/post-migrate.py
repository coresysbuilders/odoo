# Meta Lead Ads Integration for Odoo — © 2026 CoreSys Builders. All rights reserved.
# Licensed under the Odoo Proprietary License v1.0 (OPL-1).
# Unauthorized copying, redistribution, or resale of this software, in whole or in
# part, via any medium, is strictly prohibited and constitutes a license violation.
# OPL-1: https://www.odoo.com/documentation/18.0/legal/licenses.html#odoo-apps

"""Backfill ``meta_name_is_generated`` for Meta leads created before the title
provenance flag existed.

Before this version every Meta lead title was the byte-exact DEFAULT render
("Meta Lead • <form> • <date>"), so a Meta lead whose name STILL matches that
default skeleton is provably a generated title and is flagged True (eligible for
the retroactive rename). A name that no longer matches was manually edited and
stays False, so the retro rename never clobbers it. This one-time skeleton match
is accurate precisely because the configurable template did not exist before
this version — every pre-existing generated title used the default shape.
"""
import logging
import re

from odoo import SUPERUSER_ID, api
from odoo.addons.meta_lead_ads.models.const import DEFAULT_LEAD_NAME

_logger = logging.getLogger(__name__)

_TOKEN_RE = re.compile(r'\{(\w+)\}')


def _default_skeleton_re():
    parts, pos = [], 0
    for m in _TOKEN_RE.finditer(DEFAULT_LEAD_NAME):
        parts.append(re.escape(DEFAULT_LEAD_NAME[pos:m.start()]))
        parts.append(r'.*?')
        pos = m.end()
    parts.append(re.escape(DEFAULT_LEAD_NAME[pos:]))
    return re.compile(r'^%s$' % ''.join(parts))


def migrate(cr, version):
    env = api.Environment(cr, SUPERUSER_ID, {})
    skeleton = _default_skeleton_re()
    leads = env['crm.lead'].search([('meta_leadgen_id', '!=', False),
                                    ('meta_name_is_generated', '=', False)])
    to_flag = leads.filtered(lambda lead: skeleton.match(lead.name or ''))
    if to_flag:
        # write() does not clear the flag here (no 'name' key in vals).
        to_flag.write({'meta_name_is_generated': True})
    _logger.info(
        "meta_lead_ads: backfilled meta_name_is_generated on %s of %s "
        "pre-existing Meta lead(s).", len(to_flag), len(leads))
