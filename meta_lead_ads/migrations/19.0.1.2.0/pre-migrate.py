# Meta Lead Ads Integration for Odoo — © 2026 CoreSys Builders. All rights reserved.
# Licensed under the Odoo Proprietary License v1.0 (OPL-1).
# Unauthorized copying, redistribution, or resale of this software, in whole or in
# part, via any medium, is strictly prohibited and constitutes a license violation.
# OPL-1: https://www.odoo.com/documentation/20.0/legal/licenses.html#odoo-apps

"""Give existing Meta records a company before the new columns are added by the ORM.

The columns are created here on purpose. When Odoo finds a new stored computed
or related field without a column, it queues every existing row for an ORM
recompute, which on a large sync log means loading the whole history at once.
Created and filled here in id batches, the columns already exist when the
schema update runs, so Odoo only adds the keys, indexes and NOT NULL.

Sync-log rows without a Page also get one where it can be found, so they
keep an owner after their lead is deleted.

Accounts go to the main company; admins with several companies re-point them
in Settings. Existing leads keep their company: crm_lead is only read here.
"""
import logging

from odoo.tools import SQL
from odoo.tools.sql import column_exists, create_column

from odoo.addons.meta_lead_ads.models.migration_utils import (
    iter_id_batches, main_company_id)

_logger = logging.getLogger(__name__)

_NEW_COLUMNS = (
    ('meta_account', 'company_id'),
    ('meta_page', 'company_id'),
    ('meta_page', 'lead_company_id'),
    ('meta_lead_form', 'company_id'),
    ('meta_sync_log', 'company_id'),
    ('meta_webhook_event', 'page_company_id'),
    ('meta_webhook_event', 'company_id'),
)


def _blank(column):
    return SQL("%s IS NULL", SQL.identifier(column))


def _tally(cr, counts, table, column, ids, changed, quarantine=False):
    """Add one batch to ``counts``. ``changed`` is the UPDATE's rowcount."""
    counts['batches'] += 1
    counts['visited'] += len(ids)
    counts['changed'] += changed
    cr.execute(SQL(
        "SELECT count(*) FROM %s WHERE id = ANY(%s) AND %s",
        SQL.identifier(table), ids, _blank(column)))
    counts['unresolved'] += cr.fetchone()[0]
    if quarantine:
        cr.execute(SQL(
            "SELECT count(*) FROM %s WHERE id = ANY(%s) AND %s "
            "AND raw_payload IS NOT NULL",
            SQL.identifier(table), ids, _blank(column)))
        counts['quarantined'] += cr.fetchone()[0]


def _new_counts(quarantine=False):
    counts = {'visited': 0, 'changed': 0, 'unresolved': 0, 'batches': 0}
    if quarantine:
        counts['quarantined'] = 0
    return counts


def _log_counts(key, counts):
    if 'quarantined' in counts:
        _logger.info(
            "meta_lead_ads: %s: visited %s, changed %s, unresolved %s in %s "
            "batch(es), quarantined %s", key, counts['visited'],
            counts['changed'], counts['unresolved'], counts['batches'],
            counts['quarantined'])
    else:
        _logger.info(
            "meta_lead_ads: %s: visited %s, changed %s, unresolved %s in %s "
            "batch(es)", key, counts['visited'], counts['changed'],
            counts['unresolved'], counts['batches'])


def _warn_if_multi_company(cr, main, assigned):
    cr.execute("SELECT count(*) FROM res_company")
    companies = cr.fetchone()[0]
    if companies <= 1:
        return
    cr.execute("""
        SELECT company_id, count(*) FROM crm_lead
         WHERE meta_leadgen_id IS NOT NULL
         GROUP BY company_id ORDER BY company_id NULLS LAST
    """)
    per_company = ', '.join(
        '%s: %s' % (company_id or 'none', count)
        for company_id, count in cr.fetchall()) or 'none'
    # Ids and counts only, never names.
    _logger.warning(
        "meta_lead_ads: %s companies in this database; %s Meta account(s) "
        "were set to the main company (id %s). Existing Meta leads per "
        "company id: %s. Check each Meta account's company in Settings "
        "before new leads arrive.", companies, assigned, main, per_company)


def migrate(cr, version):
    if not version:
        return None
    for table, column in _NEW_COLUMNS:
        if not column_exists(cr, table, column):
            create_column(cr, table, column, 'int4')
    result = {}

    # Accounts: the main company.
    main = main_company_id(cr)
    key = 'meta_account.company_id'
    counts = result[key] = _new_counts()
    for ids in iter_id_batches(cr, 'meta_account', _blank('company_id')):
        cr.execute(SQL("""
            UPDATE meta_account SET company_id = %s
             WHERE id = ANY(%s) AND company_id IS DISTINCT FROM %s
        """, main, ids, main))
        _tally(cr, counts, 'meta_account', 'company_id', ids, cr.rowcount)
    _log_counts(key, counts)
    _warn_if_multi_company(cr, main, counts['changed'])

    # Pages: the override if set, else the account's company.
    key = 'meta_page.lead_company_id'
    counts = result[key] = _new_counts()
    for ids in iter_id_batches(cr, 'meta_page', _blank('lead_company_id')):
        cr.execute(SQL("""
            UPDATE meta_page p
               SET lead_company_id = COALESCE(p.company_id, a.company_id)
              FROM meta_account a
             WHERE a.id = p.account_id AND p.id = ANY(%s)
               AND p.lead_company_id IS DISTINCT FROM
                   COALESCE(p.company_id, a.company_id)
        """, ids))
        _tally(cr, counts, 'meta_page', 'lead_company_id', ids, cr.rowcount)
    _log_counts(key, counts)

    # Forms: their Page's lead company.
    key = 'meta_lead_form.company_id'
    counts = result[key] = _new_counts()
    for ids in iter_id_batches(cr, 'meta_lead_form', _blank('company_id')):
        cr.execute(SQL("""
            UPDATE meta_lead_form f SET company_id = p.lead_company_id
              FROM meta_page p
             WHERE p.id = f.page_id AND f.id = ANY(%s)
               AND f.company_id IS DISTINCT FROM p.lead_company_id
        """, ids))
        _tally(cr, counts, 'meta_lead_form', 'company_id', ids, cr.rowcount)
    _log_counts(key, counts)

    # Sync logs without a Page. Same order as the Retry path's _resolve_page
    # (lead Page, lead form's Page, lead's raw Meta page id), so migration and
    # runtime agree on a row's Page; the webhook event comes last. Every join
    # is on a primary key or an index that already exists before the schema
    # update. Archived Pages match too: this only records ownership.
    key = 'meta_sync_log.page_id'
    counts = result[key] = _new_counts()
    for ids in iter_id_batches(cr, 'meta_sync_log', _blank('page_id')):
        cr.execute(SQL("""
            UPDATE meta_sync_log s SET page_id = src.page_id
              FROM (SELECT s2.id,
                           COALESCE(l.meta_page_id_ref, f.page_id,
                                    p_raw.id, p_evt.id) AS page_id
                      FROM meta_sync_log s2
                      LEFT JOIN crm_lead l ON l.id = s2.lead_id
                      LEFT JOIN meta_lead_form f ON f.id = l.meta_form_id_ref
                      LEFT JOIN meta_page p_raw ON p_raw.page_id = l.meta_page_id
                      LEFT JOIN meta_webhook_event e
                             ON e.leadgen_id = s2.meta_leadgen_id
                      LEFT JOIN meta_page p_evt ON p_evt.page_id = e.page_id
                     WHERE s2.id = ANY(%s)) src
             WHERE s.id = src.id AND s.page_id IS DISTINCT FROM src.page_id
        """, ids))
        _tally(cr, counts, 'meta_sync_log', 'page_id', ids, cr.rowcount)
    _log_counts(key, counts)

    # Sync logs: the lead's company, else the Page's. Rows left blank that
    # still hold a payload are hidden from everyone but privileged code.
    key = 'meta_sync_log.company_id'
    counts = result[key] = _new_counts(quarantine=True)
    for ids in iter_id_batches(cr, 'meta_sync_log', _blank('company_id')):
        cr.execute(SQL("""
            UPDATE meta_sync_log s SET company_id = src.company_id
              FROM (SELECT s2.id,
                           COALESCE(l.company_id, p.lead_company_id)
                               AS company_id
                      FROM meta_sync_log s2
                      LEFT JOIN crm_lead l ON l.id = s2.lead_id
                      LEFT JOIN meta_page p ON p.id = s2.page_id
                     WHERE s2.id = ANY(%s)) src
             WHERE s.id = src.id
               AND s.company_id IS DISTINCT FROM src.company_id
        """, ids))
        _tally(cr, counts, 'meta_sync_log', 'company_id', ids, cr.rowcount,
               quarantine=True)
    _log_counts(key, counts)

    # Webhook events: the company of the Page they were sent for. page_id
    # here is the Meta page id, not a meta_page row id.
    key = 'meta_webhook_event.page_company_id'
    counts = result[key] = _new_counts()
    for ids in iter_id_batches(cr, 'meta_webhook_event',
                               _blank('page_company_id')):
        cr.execute(SQL("""
            UPDATE meta_webhook_event e SET page_company_id = p.lead_company_id
              FROM meta_page p
             WHERE p.page_id = e.page_id AND e.id = ANY(%s)
               AND e.page_company_id IS DISTINCT FROM p.lead_company_id
        """, ids))
        _tally(cr, counts, 'meta_webhook_event', 'page_company_id', ids,
               cr.rowcount)
    _log_counts(key, counts)

    # Webhook events: the lead's company, else the Page's.
    key = 'meta_webhook_event.company_id'
    counts = result[key] = _new_counts()
    for ids in iter_id_batches(cr, 'meta_webhook_event', _blank('company_id')):
        cr.execute(SQL("""
            UPDATE meta_webhook_event e SET company_id = src.company_id
              FROM (SELECT e2.id,
                           COALESCE(l.company_id, e2.page_company_id)
                               AS company_id
                      FROM meta_webhook_event e2
                      LEFT JOIN crm_lead l ON l.id = e2.lead_id
                     WHERE e2.id = ANY(%s)) src
             WHERE e.id = src.id
               AND e.company_id IS DISTINCT FROM src.company_id
        """, ids))
        _tally(cr, counts, 'meta_webhook_event', 'company_id', ids,
               cr.rowcount)
    _log_counts(key, counts)
    return result
