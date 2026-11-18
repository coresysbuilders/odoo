# Meta Lead Ads Integration for Odoo — © 2026 CoreSys Builders. All rights reserved.
# Licensed under the Odoo Proprietary License v1.0 (OPL-1).
# Unauthorized copying, redistribution, or resale of this software, in whole or in
# part, via any medium, is strictly prohibited and constitutes a license violation.
# OPL-1: https://www.odoo.com/documentation/20.0/legal/licenses.html#odoo-apps

"""Bring existing data in line with this version.

1. Mappings that write a date or datetime and have no date format get
   DD/MM/YYYY, the same default a new date mapping gets. A format already
   set is kept.
2. The stored phone dedup key of every lead with a phone is computed again.
   It now reads a national number in the lead's country, else its company's,
   and the ORM doesn't recompute a stored field when only its code changes.
3. Removed-lead records written before they kept their Page, form and
   company get whatever the sync log or their Page can still tell. Values
   already there are never replaced.
4. Failed sync rows and failed webhook events of leads recorded as removed
   are settled as skipped, the way the drain and Retry now settle them.
"""
import logging

from odoo.tools import SQL

from odoo.addons.meta_lead_ads.models.migration_utils import iter_id_batches
from odoo.addons.meta_lead_ads.models.phone_key import phone_dedup_key

_logger = logging.getLogger(__name__)

# Same text as meta_sync_log._REMOVED_MESSAGE. Not imported: that module
# pulls in the ORM models.
_REMOVED_MESSAGE = 'Removed in Odoo; skipped'


def _fill_date_formats(cr):
    # The ORM usually fills the new column on its own when it creates it.
    # This covers rows it left empty. A small config table, so one statement
    # is enough.
    cr.execute("""
        UPDATE meta_field_mapping m SET date_format = 'dmy'
          FROM ir_model_fields f
         WHERE f.id = m.crm_field_id
           AND f.ttype IN ('date', 'datetime')
           AND m.date_format IS NULL
    """)
    return cr.rowcount


def _recompute_phone_keys(cr):
    visited = changed = 0
    for ids in iter_id_batches(cr, 'crm_lead', SQL("phone IS NOT NULL")):
        # res_company has no country column of its own; it lives on the
        # company's partner.
        cr.execute(SQL("""
            SELECT l.id, l.phone, l.meta_phone_normalized,
                   lc.code, lc.phone_code, pc.code, pc.phone_code
              FROM crm_lead l
              LEFT JOIN res_country lc ON lc.id = l.country_id
              LEFT JOIN res_company co ON co.id = l.company_id
              LEFT JOIN res_partner p ON p.id = co.partner_id
              LEFT JOIN res_country pc ON pc.id = p.country_id
             WHERE l.id = ANY(%s)
        """, ids))
        rows = cr.fetchall()
        visited += len(rows)
        updates = []
        for (lead_id, phone, current, lead_code, lead_pc,
             company_code, company_pc) in rows:
            # The lead's own country wins over its company's.
            if lead_code:
                code, phone_code = lead_code, lead_pc
            else:
                code, phone_code = company_code, company_pc
            key = phone_dedup_key(phone, code, phone_code) or None
            if key != current:
                updates.append((lead_id, key))
        if not updates:
            continue
        values = SQL(", ").join(
            SQL("(%s::int4, %s::varchar)", lead_id, key)
            for lead_id, key in updates)
        cr.execute(SQL("""
            UPDATE crm_lead l SET meta_phone_normalized = v.key
              FROM (VALUES %s) AS v(id, key)
             WHERE l.id = v.id
               AND l.meta_phone_normalized IS DISTINCT FROM v.key
        """, values))
        changed += cr.rowcount
    return visited, changed


def _link_removed_records(cr):
    linked = 0
    # Keyset paging ends even though records with no trace keep matching.
    for ids in iter_id_batches(cr, 'meta_leadgen_tombstone', SQL(
            "page_id IS NULL OR form_id IS NULL OR company_id IS NULL")):
        # The newest sync row with a Page says where the lead came from. The
        # company falls back to where that Page sends its leads.
        cr.execute(SQL("""
            UPDATE meta_leadgen_tombstone tomb
               SET page_id = x.page_id,
                   form_id = x.form_id,
                   company_id = x.company_id
              FROM (
                  SELECT t.id,
                         COALESCE(t.page_id, s.page_id) AS page_id,
                         COALESCE(t.form_id, s.form_id) AS form_id,
                         COALESCE(t.company_id, s.company_id,
                                  pg.lead_company_id, pg.company_id,
                                  acc.company_id) AS company_id
                    FROM meta_leadgen_tombstone t
                    LEFT JOIN (
                        SELECT DISTINCT ON (meta_leadgen_id)
                               meta_leadgen_id, page_id, form_id, company_id
                          FROM meta_sync_log
                         WHERE meta_leadgen_id IN (
                                   SELECT leadgen_id
                                     FROM meta_leadgen_tombstone
                                    WHERE id = ANY(%s))
                           AND page_id IS NOT NULL
                         ORDER BY meta_leadgen_id, id DESC
                    ) s ON s.meta_leadgen_id = t.leadgen_id
                    LEFT JOIN meta_page pg
                           ON pg.id = COALESCE(t.page_id, s.page_id)
                    LEFT JOIN meta_account acc ON acc.id = pg.account_id
                   WHERE t.id = ANY(%s)
              ) x
             WHERE x.id = tomb.id
               AND (tomb.page_id IS DISTINCT FROM x.page_id
                    OR tomb.form_id IS DISTINCT FROM x.form_id
                    OR tomb.company_id IS DISTINCT FROM x.company_id)
        """, ids, ids))
        linked += cr.rowcount
    return linked


def _settle_removed(cr):
    rows = events = 0
    removed = SQL("meta_leadgen_id IN (SELECT leadgen_id "
                  "FROM meta_leadgen_tombstone)")
    for ids in iter_id_batches(cr, 'meta_sync_log', SQL(
            "status = 'failed' AND %s", removed)):
        cr.execute(SQL("""
            UPDATE meta_sync_log
               SET status = 'skipped_removed', error_message = %s
             WHERE id = ANY(%s) AND status = 'failed'
        """, _REMOVED_MESSAGE, ids))
        rows += cr.rowcount
    # Pending events are left to the drain, which settles them the same way.
    for ids in iter_id_batches(cr, 'meta_webhook_event', SQL(
            "status = 'failed' AND leadgen_id IN (SELECT leadgen_id "
            "FROM meta_leadgen_tombstone)")):
        cr.execute(SQL("""
            UPDATE meta_webhook_event
               SET status = 'done', lead_id = NULL, next_attempt_at = NULL,
                   error_message = %s
             WHERE id = ANY(%s) AND status = 'failed'
        """, _REMOVED_MESSAGE, ids))
        events += cr.rowcount
    return rows, events


def migrate(cr, version):
    if not version:
        return None
    counts = {'mappings': _fill_date_formats(cr)}
    counts['leads'], counts['keys'] = _recompute_phone_keys(cr)
    counts['removed_linked'] = _link_removed_records(cr)
    counts['rows_settled'], counts['events_settled'] = _settle_removed(cr)
    _logger.info(
        "meta_lead_ads: date format set on %(mappings)s mapping(s); phone "
        "key read again for %(leads)s lead(s), %(keys)s changed; links "
        "filled on %(removed_linked)s removed-lead record(s); "
        "%(rows_settled)s failed sync row(s) and %(events_settled)s failed "
        "webhook event(s) of removed leads settled.", counts)
    return counts
