# Meta Lead Ads Integration for Odoo — © 2026 CoreSys Builders. All rights reserved.
# Licensed under the Odoo Proprietary License v1.0 (OPL-1).
# Unauthorized copying, redistribution, or resale of this software, in whole or in
# part, via any medium, is strictly prohibited and constitutes a license violation.
# OPL-1: https://www.odoo.com/documentation/19.0/legal/licenses.html#odoo-apps

"""Fill ``crm.lead.meta_submitted_at`` for Meta leads ingested before it existed.

Sources, in order:

1. The raw Graph response on the successful sync log row (``created_time`` is
   ISO 8601). Old success rows are pruned, so this only covers recent leads.
2. The raw webhook envelope (``changes[].value.created_time`` is a Unix epoch).

Leads found in neither stay empty. create_date is not copied in: for
backfilled or retried leads it is not the submission time, and reports
would be quietly wrong.
"""
import json
import logging
from datetime import datetime, timezone

from odoo.addons.meta_lead_ads.models.meta_lead_ingest import parse_meta_time

_logger = logging.getLogger(__name__)


def _from_epoch(value):
    try:
        return (datetime.fromtimestamp(int(value), tz=timezone.utc)
                .replace(tzinfo=None))
    except (TypeError, ValueError, OverflowError, OSError):
        return None


def _from_envelope(payload, leadgen_id):
    """created_time of ``leadgen_id`` inside a webhook envelope, or None."""
    for entry in (payload or {}).get('entry') or []:
        for change in entry.get('changes') or []:
            value = change.get('value') or {}
            if str(value.get('leadgen_id')) == leadgen_id:
                return _from_epoch(value.get('created_time'))
    return None


def _loads(text):
    try:
        data = json.loads(text)
    except (TypeError, ValueError):
        return None
    return data if isinstance(data, dict) else None


def migrate(cr, version):
    if not version:
        return
    cr.execute("""
        SELECT id, meta_leadgen_id FROM crm_lead
         WHERE meta_leadgen_id IS NOT NULL AND meta_submitted_at IS NULL
    """)
    pending = dict(cr.fetchall())
    if not pending:
        return
    found = {}

    cr.execute("""
        SELECT lead_id, raw_payload FROM meta_sync_log
         WHERE lead_id = ANY(%s) AND status = 'success'
           AND raw_payload IS NOT NULL
         ORDER BY id
    """, (list(pending),))
    for lead_id, text in cr.fetchall():
        data = _loads(text)
        moment = parse_meta_time((data or {}).get('created_time'))
        if moment and lead_id not in found:
            found[lead_id] = moment

    missing = [lid for lid in pending if lid not in found]
    if missing:
        cr.execute("""
            SELECT lead_id, raw_payload FROM meta_webhook_event
             WHERE lead_id = ANY(%s) AND raw_payload IS NOT NULL
             ORDER BY id
        """, (missing,))
        for lead_id, text in cr.fetchall():
            moment = _from_envelope(_loads(text), pending[lead_id])
            if moment and lead_id not in found:
                found[lead_id] = moment

    for lead_id, moment in found.items():
        cr.execute("UPDATE crm_lead SET meta_submitted_at = %s WHERE id = %s",
                   (moment, lead_id))
    _logger.info(
        "meta_lead_ads: Meta submission time filled for %s of %s existing "
        "Meta lead(s); the rest have no stored payload and stay empty.",
        len(found), len(pending))
