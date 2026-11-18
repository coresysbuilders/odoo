# Meta Lead Ads Integration for Odoo — © 2026 CoreSys Builders. All rights reserved.
# Licensed under the Odoo Proprietary License v1.0 (OPL-1).
# Unauthorized copying, redistribution, or resale of this software, in whole or in
# part, via any medium, is strictly prohibited and constitutes a license violation.
# OPL-1: https://www.odoo.com/documentation/20.0/legal/licenses.html#odoo-apps

"""Give the old failures of archived Pages their company.

1. Earlier versions treated a webhook lead of an archived Page as coming
   from an unknown Page, so its failed sync row and its queue entry got no
   company.
2. Where the queue entry still names the Page, both now get the Page's
   company and the archived message, so the lead waits for the Page to be
   reactivated, like new ones do.
3. Rows whose queue entry is gone can't be traced here. A Retry from the
   Sync Log attaches them to their Page.

Only counts are logged, never a lead id, Page id, name or secret.
"""
import logging

from odoo.tools import SQL

from odoo.addons.meta_lead_ads.models.migration_utils import iter_id_batches

_logger = logging.getLogger(__name__)

VERSION = '19.0.1.10.0'

# Must equal the drain's message in models/meta_sync_log.py; a test checks
# it. A migration does not import a models module.
_PAGE_ARCHIVED_MESSAGE = 'This Page is archived in Odoo. Reactivate it, then Retry.'


def _repair_rows(cr):
    # The company is set by hand: a SQL update of page_id does not recompute
    # the stored company_id. A Page without a company has none to give, so
    # its rows are skipped, as its events are.
    rows = 0
    for ids in iter_id_batches(cr, 'meta_sync_log', SQL(
            "status = 'failed' AND trigger = 'webhook' AND page_id IS NULL "
            "AND meta_leadgen_id IS NOT NULL")):
        cr.execute(
            "UPDATE meta_sync_log l SET page_id = p.id, "
            "company_id = p.lead_company_id, error_message = %s "
            "FROM meta_webhook_event e "
            "JOIN meta_page p ON p.page_id = e.page_id "
            "WHERE l.id = ANY(%s) AND e.leadgen_id = l.meta_leadgen_id "
            "AND p.active IS NOT TRUE AND p.lead_company_id IS NOT NULL "
            "AND l.lead_id IS NULL",
            [_PAGE_ARCHIVED_MESSAGE, ids])
        rows += cr.rowcount
    return rows


def _repair_events(cr):
    # A Page without a company would leave the event matching on every run,
    # so it is skipped.
    events = 0
    for ids in iter_id_batches(cr, 'meta_webhook_event', SQL(
            "status = 'failed' AND page_company_id IS NULL")):
        cr.execute(
            "UPDATE meta_webhook_event e "
            "SET page_company_id = p.lead_company_id, "
            "company_id = COALESCE(e.company_id, p.lead_company_id), "
            "error_message = %s "
            "FROM meta_page p "
            "WHERE e.id = ANY(%s) AND p.page_id = e.page_id "
            "AND p.active IS NOT TRUE AND p.lead_company_id IS NOT NULL",
            [_PAGE_ARCHIVED_MESSAGE, ids])
        events += cr.rowcount
    return events


def migrate(cr, version):
    if not version:
        return None
    # Rows first: they find their Page through the event, which the event
    # update leaves in place.
    counts = {'rows': _repair_rows(cr), 'events': _repair_events(cr)}
    _logger.info(
        "meta_lead_ads: %(rows)s failed sync row(s) and %(events)s queue "
        "event(s) of archived Pages were given their company.",
        counts)
    return counts
