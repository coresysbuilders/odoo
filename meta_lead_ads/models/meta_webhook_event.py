# Meta Lead Ads Integration for Odoo — © 2026 CoreSys Builders. All rights reserved.
# Licensed under the Odoo Proprietary License v1.0 (OPL-1).
# Unauthorized copying, redistribution, or resale of this software, in whole or in
# part, via any medium, is strictly prohibited and constitutes a license violation.
# OPL-1: https://www.odoo.com/documentation/18.0/legal/licenses.html#odoo-apps

# The durable webhook queue. The public controller verifies the HMAC over the
# raw bytes, then hands the parsed payload to _ingest_payload, which inserts one
# pending row per leadgen change and returns fast. An ir.cron drains pending
# rows out-of-band via _cron_drain -> _process_one, a thin wrapper over the
# ingest service ingest_leadgen (the sole crm.lead create path). This model
# creates zero crm.lead itself. The leadgen payload shape uses entry[].id as
# the page id.
import json
import logging

from psycopg2 import IntegrityError

from odoo import api, fields, models

from .exceptions import (
    MetaTransientError, MetaPermanentError, MetaAuthError, MetaRateLimitError)

_logger = logging.getLogger(__name__)
MAX_ATTEMPTS = 5          # transient-retry give-up cap


class MetaWebhookEvent(models.Model):
    _name = 'meta.webhook.event'
    _description = 'Meta Webhook Event Queue'
    _order = 'create_date asc'                 # FIFO drain

    leadgen_id = fields.Char(required=True, index=True)
    page_id = fields.Char(index=True)          # Meta page id (entry[].id)
    form_id = fields.Char()
    ad_id = fields.Char()
    # PII -> admin-grouped, identical idiom to meta.sync.log.raw_payload: the
    # key is stripped from a non-admin ORM read and the view; a non-admin
    # explicit read raises AccessError.
    raw_payload = fields.Text(groups='meta_lead_ads.group_meta_admin')
    status = fields.Selection(
        [('pending', 'Pending'), ('done', 'Done'), ('failed', 'Failed')],
        default='pending', required=True, index=True)
    attempts = fields.Integer(default=0)
    error_message = fields.Text()              # token-free
    lead_id = fields.Many2one('crm.lead', ondelete='set null')

    _sql_constraints = [
        # Belt-and-braces: at most one queue row per leadgen_id. Meta treats
        # leadgen_id as a globally unique lead id, and crm.lead.meta_leadgen_id
        # already carries a global DB-UNIQUE (the ultimate idempotency guard) --
        # so a global queue UNIQUE (not keyed per page) is correct and
        # consistent. Do not re-key dedup to (page_id, leadgen_id).
        ('leadgen_id_uniq', 'unique(leadgen_id)',
         'A webhook event for this lead is already queued.'),
    ]

    @api.model
    def _ingest_payload(self, data, raw=None):
        """Extract leadgen changes from a verified payload and insert pending
        rows (dedup on leadgen_id). Never creates a crm.lead.
        """
        if (data or {}).get('object') != 'page':
            return
        for entry in data.get('entry', []):
            page_id_fallback = entry.get('id')     # entry-level id IS the page id
            for change in entry.get('changes', []):
                if change.get('field') != 'leadgen':
                    continue
                v = change.get('value') or {}      # missing/None value -> {} -> skip
                leadgen_id = v.get('leadgen_id')
                if not leadgen_id:
                    continue
                # Dedup: skip if a row already exists (any status). Uses
                # search_count -- the count= kwarg on search was removed in
                # Odoo 18.
                if self.search_count([('leadgen_id', '=', leadgen_id)]):
                    continue
                try:
                    with self.env.cr.savepoint():
                        self.create({
                            'leadgen_id': leadgen_id,
                            # entry.id is the authoritative page fallback.
                            'page_id': v.get('page_id') or page_id_fallback,
                            'form_id': v.get('form_id'),
                            'ad_id': v.get('ad_id'),
                            # errors='replace' so an exotic-byte payload never
                            # discards the event -- the audit decode must not
                            # crash extraction. An exact-bytes copy is not
                            # required (the drain fetches from Graph by
                            # leadgen_id).
                            'raw_payload': raw.decode('utf-8', errors='replace')
                                           if raw else False,
                        })
                # Swallow only the unique-violation race (webhook+cron+Meta-
                # resend collide between search_count and INSERT). Any other
                # error (programming/permission/encoding bug) must propagate so
                # a lead is never silently dropped -- this swallow is
                # deliberately narrowed to IntegrityError only (no broad
                # catch-all).
                except IntegrityError:
                    continue

    @api.model
    def _cron_drain(self, limit=50):
        """ir.cron entry point. Lock pending rows with SKIP LOCKED so
        overlapping cron runs / workers never double-process, then call the
        ingest service. Retry only transient failures.

        Sibling isolation: each _process_one() call runs inside its own
        per-event savepoint with an outer unexpected-exception handler. One bad
        event (incl. a non-typed/programming error) must never roll back the
        status writes of siblings already processed in this same sweep -- the
        savepoint rollback is strictly per-event.
        """
        # Heartbeat: prove the scheduler is alive so the account form can flag a
        # stalled cron worker (otherwise lead sync fails silently).
        self.env['meta.account']._ping_scheduler_heartbeat()
        self.env.cr.execute(
            "SELECT id FROM meta_webhook_event "
            "WHERE status = 'pending' "
            "ORDER BY create_date ASC LIMIT %s "
            "FOR UPDATE SKIP LOCKED", (limit,))
        ids = [r[0] for r in self.env.cr.fetchall()]
        for event in self.browse(ids):
            try:
                # The savepoint wraps the _process_one() call itself: a failure
                # or unexpected raise on this event rolls back only this event's
                # partial work, leaving siblings 1..N-1 intact.
                with self.env.cr.savepoint():
                    event._process_one()
            except Exception:
                # Unexpected (non-typed) error: isolate it. Mark ONLY this event
                # failed (token-free) and keep draining its siblings. The typed
                # transient/permanent handling lives in _process_one; this catches
                # the genuinely-unexpected so the savepoint rollback is per-event.
                _logger.exception(
                    "meta_webhook drain: unexpected error on event id=%s "
                    "(no payload/secret logged)", event.id)
                event.write({'status': 'failed',
                             'error_message': 'Unexpected drain error'})
                self.env['meta.sync.log']._record(
                    event.leadgen_id, 'webhook', 'failed',
                    error='Unexpected drain error')

    def _process_one(self):
        """Drain one event through the ingest service. Transient -> stay
        pending + bump attempts until MAX_ATTEMPTS, then give up; permanent/
        auth -> fail fast. error_message + every sync.log row stay token-free:
        str(typed-exception) is already redacted upstream."""
        self.ensure_one()
        page = self.env['meta.page'].search(
            [('page_id', '=', self.page_id)], limit=1)
        if not page:
            # Error strings carry counts/redaction only -- never the Meta
            # page-id (it reaches sale-manager-visible sync.log rows).
            self.write({'status': 'failed',
                        'error_message': 'Unknown page; cannot process event'})
            self.env['meta.sync.log']._record(
                self.leadgen_id, 'webhook', 'failed',
                error='Unknown page; cannot process event')
            return
        try:
            lead = self.env['meta.lead.ingest'].ingest_leadgen(
                page, self.leadgen_id, trigger='webhook',
                raw=json.loads(self.raw_payload) if self.raw_payload else None)
            self.write({'status': 'done', 'lead_id': lead.id})
        except (MetaTransientError, MetaRateLimitError) as e:
            # Persist the bump as an explicit UPDATE folded into every write, so
            # the attempt count cannot be lost to ORM cache-flush ordering or a
            # per-event savepoint rollback.
            new_attempts = self.attempts + 1
            if new_attempts >= MAX_ATTEMPTS:
                self.write({'attempts': new_attempts,
                            'status': 'failed', 'error_message': str(e)})
                self.env['meta.sync.log']._record(
                    self.leadgen_id, 'webhook', 'failed', error=str(e))
            else:
                # Stay pending; next sweep retries.
                self.write({'attempts': new_attempts})
        except (MetaPermanentError, MetaAuthError) as e:
            self.write({'attempts': self.attempts + 1,
                        'status': 'failed', 'error_message': str(e)})
            self.env['meta.sync.log']._record(
                self.leadgen_id, 'webhook', 'failed', error=str(e))
