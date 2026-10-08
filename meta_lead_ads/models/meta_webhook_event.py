# Meta Lead Ads Integration for Odoo — © 2026 CoreSys Builders. All rights reserved.
# Licensed under the Odoo Proprietary License v1.0 (OPL-1).
# Unauthorized copying, redistribution, or resale of this software, in whole or in
# part, via any medium, is strictly prohibited and constitutes a license violation.
# OPL-1: https://www.odoo.com/documentation/19.0/legal/licenses.html#odoo-apps

# Webhook queue. The controller checks the HMAC on the raw bytes, then
# _ingest_payload stores one pending row per leadgen change so the request can
# return quickly. A cron drains the rows through ingest_leadgen, which is where
# the crm.lead actually gets created.
import json
import logging

from psycopg2 import IntegrityError

from odoo import api, fields, models

from .exceptions import (
    MetaTransientError, MetaPermanentError, MetaAuthError, MetaRateLimitError)

_logger = logging.getLogger(__name__)
MAX_ATTEMPTS = 5          # give up on transient errors after this many tries


class MetaWebhookEvent(models.Model):
    _name = 'meta.webhook.event'
    _description = 'Meta Webhook Event Queue'
    _order = 'create_date asc'                 # oldest first

    leadgen_id = fields.Char(required=True, index=True)
    page_id = fields.Char(index=True)          # Meta page id (entry[].id)
    form_id = fields.Char()
    ad_id = fields.Char()
    # Contains PII, so admin-only, same as meta.sync.log.raw_payload.
    raw_payload = fields.Text(groups='meta_lead_ads.group_meta_admin')
    status = fields.Selection(
        [('pending', 'Pending'), ('done', 'Done'), ('failed', 'Failed')],
        default='pending', required=True, index=True)
    attempts = fields.Integer(default=0)
    error_message = fields.Text()              # never include a token
    lead_id = fields.Many2one('crm.lead', ondelete='set null')

    # One queue row per leadgen_id. Meta lead ids are globally unique, so this
    # is not keyed per page. The UNIQUE on crm.lead.meta_leadgen_id is still
    # what prevents duplicate leads. Odoo 19 uses models.Constraint instead of
    # _sql_constraints.
    _leadgen_id_uniq = models.Constraint(
        'unique(leadgen_id)',
        'A webhook event for this lead is already queued.',
    )

    @api.model
    def _ingest_payload(self, data, raw=None):
        """Queue a pending row for each leadgen change in a verified payload,
        skipping lead ids already queued. Does not create leads.
        """
        if (data or {}).get('object') != 'page':
            return
        for entry in data.get('entry', []):
            page_id_fallback = entry.get('id')     # entry id is the page id
            for change in entry.get('changes', []):
                if change.get('field') != 'leadgen':
                    continue
                v = change.get('value') or {}
                leadgen_id = v.get('leadgen_id')
                if not leadgen_id:
                    continue
                # Already queued, whatever its status.
                if self.search_count([('leadgen_id', '=', leadgen_id)]):
                    continue
                try:
                    with self.env.cr.savepoint():
                        self.create({
                            'leadgen_id': leadgen_id,
                            'page_id': v.get('page_id') or page_id_fallback,
                            'form_id': v.get('form_id'),
                            'ad_id': v.get('ad_id'),
                            # errors='replace': odd bytes shouldn't lose the
                            # event. The copy doesn't need to be exact; the
                            # drain re-fetches the lead from Graph.
                            'raw_payload': raw.decode('utf-8', errors='replace')
                                           if raw else False,
                        })
                # Someone queued the same lead between the check and the
                # insert. Only this error is ignored; anything else should
                # raise rather than silently drop a lead.
                except IntegrityError:
                    continue

    @api.model
    def _cron_drain(self, limit=50):
        """Cron: process pending events.

        Rows are locked with SKIP LOCKED so overlapping runs don't take the
        same event. Each event runs in its own savepoint, so one that fails
        doesn't roll back the others.
        """
        # Record that the scheduler ran, so the account form can show a
        # stalled cron instead of sync quietly stopping.
        self.env['meta.account']._ping_scheduler_heartbeat()
        self.env.cr.execute(
            "SELECT id FROM meta_webhook_event "
            "WHERE status = 'pending' "
            "ORDER BY create_date ASC LIMIT %s "
            "FOR UPDATE SKIP LOCKED", (limit,))
        ids = [r[0] for r in self.env.cr.fetchall()]
        for event in self.browse(ids):
            try:
                with self.env.cr.savepoint():
                    event._process_one()
            except Exception:
                # Expected Meta errors are handled in _process_one. Anything
                # else fails just this event and the loop carries on.
                _logger.exception(
                    "meta_webhook drain: unexpected error on event id=%s "
                    "(no payload/secret logged)", event.id)
                event.write({'status': 'failed',
                             'error_message': 'Unexpected drain error'})
                self.env['meta.sync.log']._record(
                    event.leadgen_id, 'webhook', 'failed',
                    error='Unexpected drain error')

    def _process_one(self):
        """Send one event through ingest_leadgen.

        Transient errors leave it pending until MAX_ATTEMPTS, then mark it
        failed. Permanent and auth errors fail it straight away. Exception
        text is already redacted, so it is safe to store.
        """
        self.ensure_one()
        page = self.env['meta.page'].search(
            [('page_id', '=', self.page_id)], limit=1)
        if not page:
            # Leave the page id out: sales managers can read sync-log rows.
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
            # Write attempts explicitly in each branch so the count isn't lost
            # to cache/flush ordering.
            new_attempts = self.attempts + 1
            if new_attempts >= MAX_ATTEMPTS:
                self.write({'attempts': new_attempts,
                            'status': 'failed', 'error_message': str(e)})
                self.env['meta.sync.log']._record(
                    self.leadgen_id, 'webhook', 'failed', error=str(e))
            else:
                # Still pending; the next run retries it.
                self.write({'attempts': new_attempts})
        except (MetaPermanentError, MetaAuthError) as e:
            self.write({'attempts': self.attempts + 1,
                        'status': 'failed', 'error_message': str(e)})
            self.env['meta.sync.log']._record(
                self.leadgen_id, 'webhook', 'failed', error=str(e))
