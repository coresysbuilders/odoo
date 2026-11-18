# Meta Lead Ads Integration for Odoo — © 2026 CoreSys Builders. All rights reserved.
# Licensed under the Odoo Proprietary License v1.0 (OPL-1).
# Unauthorized copying, redistribution, or resale of this software, in whole or in
# part, via any medium, is strictly prohibited and constitutes a license violation.
# OPL-1: https://www.odoo.com/documentation/20.0/legal/licenses.html#odoo-apps

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

from odoo.tools import SQL

from odoo.addons.meta_lead_ads.models.meta_lead_ingest import parse_meta_time
from odoo.addons.meta_lead_ads.models.migration_utils import (
    iter_child_batches, iter_id_batches)

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
    visited = filled = 0
    # Keyset paging ends even though leads with no payload keep matching.
    for ids in iter_id_batches(cr, 'crm_lead', SQL(
            "meta_leadgen_id IS NOT NULL AND meta_submitted_at IS NULL")):
        cr.execute("SELECT id, meta_leadgen_id FROM crm_lead "
                   "WHERE id = ANY(%s)", (ids,))
        pending = dict(cr.fetchall())
        visited += len(pending)
        found = {}

        # A lead can have many success rows, so the payloads are paged too,
        # by (lead, id) so the lead_id indexes that the schema update creates
        # before post-migrate scripts run can be used. Each lead's rows come
        # in ascending id order, so its first usable payload by id still wins.
        for log_ids in iter_child_batches(
                cr, 'meta_sync_log', 'lead_id', list(pending), SQL(
                    "status = 'success' AND raw_payload IS NOT NULL")):
            cr.execute("""
                SELECT lead_id, raw_payload FROM meta_sync_log
                 WHERE id = ANY(%s)
                 ORDER BY lead_id, id
            """, (log_ids,))
            for lead_id, text in cr.fetchall():
                data = _loads(text)
                moment = parse_meta_time((data or {}).get('created_time'))
                if moment and lead_id not in found:
                    found[lead_id] = moment

        missing = [lid for lid in pending if lid not in found]
        if missing:
            for event_ids in iter_child_batches(
                    cr, 'meta_webhook_event', 'lead_id', missing,
                    SQL("raw_payload IS NOT NULL")):
                cr.execute("""
                    SELECT lead_id, raw_payload FROM meta_webhook_event
                     WHERE id = ANY(%s)
                     ORDER BY lead_id, id
                """, (event_ids,))
                for lead_id, text in cr.fetchall():
                    moment = _from_envelope(_loads(text), pending[lead_id])
                    if moment and lead_id not in found:
                        found[lead_id] = moment

        for lead_id, moment in found.items():
            cr.execute("""
                UPDATE crm_lead SET meta_submitted_at = %s
                 WHERE id = %s AND meta_submitted_at IS NULL
            """, (moment, lead_id))
            filled += cr.rowcount
    if not visited:
        return
    _logger.info(
        "meta_lead_ads: Meta submission time filled for %s of %s existing "
        "Meta lead(s); the rest have no stored payload and stay empty.",
        filled, visited)
