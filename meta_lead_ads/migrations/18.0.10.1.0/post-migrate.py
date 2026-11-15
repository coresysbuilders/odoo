# Meta Lead Ads Integration for Odoo — © 2026 CoreSys Builders. All rights reserved.
# Licensed under the Odoo Proprietary License v1.0 (OPL-1).
# Unauthorized copying, redistribution, or resale of this software, in whole or in
# part, via any medium, is strictly prohibited and constitutes a license violation.
# OPL-1: https://www.odoo.com/documentation/18.0/legal/licenses.html#odoo-apps

"""Set ``meta_name_is_generated`` on Meta leads created before the flag existed.

Before this version the title template was not configurable, so every
generated title had the default shape. A lead whose name still matches it is
flagged True and can be renamed later; anything else was edited by hand and
stays False.
"""
import logging
import re

from odoo.tools import SQL

from odoo.addons.meta_lead_ads.models.const import DEFAULT_LEAD_NAME
from odoo.addons.meta_lead_ads.models.migration_utils import iter_id_batches

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
    skeleton = _default_skeleton_re()
    visited = flagged = 0
    # Archived leads are left out, as the original ORM search did.
    for ids in iter_id_batches(cr, 'crm_lead', SQL(
            "meta_leadgen_id IS NOT NULL AND active IS TRUE "
            "AND meta_name_is_generated IS NOT TRUE")):
        visited += len(ids)
        cr.execute("SELECT id, name FROM crm_lead WHERE id = ANY(%s)", (ids,))
        to_flag = [lead_id for lead_id, name in cr.fetchall()
                   if skeleton.match(name or '')]
        if to_flag:
            # Plain SQL skips the crm.lead write override, which only
            # matters when the name is written.
            cr.execute("""
                UPDATE crm_lead SET meta_name_is_generated = TRUE
                 WHERE id = ANY(%s) AND meta_name_is_generated IS NOT TRUE
            """, (to_flag,))
            flagged += cr.rowcount
    _logger.info(
        "meta_lead_ads: backfilled meta_name_is_generated on %s of %s "
        "pre-existing Meta lead(s).", flagged, visited)
