# Meta Lead Ads Integration for Odoo — © 2026 CoreSys Builders. All rights reserved.
# Licensed under the Odoo Proprietary License v1.0 (OPL-1).
# Unauthorized copying, redistribution, or resale of this software, in whole or in
# part, via any medium, is strictly prohibited and constitutes a license violation.
# OPL-1: https://www.odoo.com/documentation/18.0/legal/licenses.html#odoo-apps

import json
import logging
from datetime import timedelta

from odoo import _, api, fields, models
from odoo.exceptions import UserError
from psycopg2 import errorcodes
from psycopg2.errors import LockNotAvailable

from .exceptions import (MetaAuthError, MetaPermanentError,
                         MetaRateLimitError, MetaTransientError)

_logger = logging.getLogger(__name__)


class MetaSyncLog(models.Model):
    _name = 'meta.sync.log'
    _description = 'Meta Sync Log'
    _order = 'create_date desc'

    # Not unique here: one lead can have several attempts logged.
    meta_leadgen_id = fields.Char(string='Meta Lead ID', index=True)
    status = fields.Selection(
        [('pending', 'Pending'), ('success', 'Success'),
         ('skipped_duplicate', 'Skipped (duplicate)'),
         ('skipped_idempotent', 'Skipped (idempotent)'),
         ('failed', 'Failed')],
        string='Status', default='pending', required=True)
    trigger = fields.Selection(
        [('webhook', 'Webhook'), ('cron', 'Cron'), ('manual', 'Manual')],
        string='Trigger')
    retry_count = fields.Integer(string='Retry Count', default=0)
    # Shown to non-admins, so never put a token or secret in here.
    error_message = fields.Text(string='Error Message')
    # Which key the business dedup matched on.
    match_key = fields.Selection(
        [('email', 'Email'), ('phone', 'Phone'), ('none', 'None')],
        string='Match Key')
    # set null so deleting a lead keeps its log history.
    lead_id = fields.Many2one('crm.lead', string='Lead', ondelete='set null')
    # Set on failures that happened before the lead was fetched (no lead, no
    # payload), so a manual retry knows which page to re-fetch from.
    page_id = fields.Many2one('meta.page', string='Meta Page',
                              ondelete='set null')
    # On rows written during a manual retry, points at the failed row that was
    # retried. _reconcile_after_ingest uses it to find the new row. Indexed
    # because this table gets large.
    retry_origin_log_id = fields.Many2one(
        'meta.sync.log', string='Retry Origin Log',
        ondelete='set null', index=True)
    # Full Meta payload, contains lead PII: admin-only. Don't expose it through
    # a computed/related field or a sudo() read for non-admins.
    raw_payload = fields.Text(string='Raw Payload',
                              groups='meta_lead_ads.group_meta_admin')

    @api.model
    def _record(self, leadgen_id, trigger, status, lead=None,
                match_key=None, raw=None, error=None, page=None):
        """Write one sync-log row. Never pass a token or secret in `raw` or
        `error`.

        `raw` is the full Graph lead payload and is stored as JSON; it is the
        complete copy (meta.lead.answer only keeps display strings).

        `page` is only passed for failures before the lead was fetched, so a
        retry can find the page. Other rows reach the page through lead_id.

        retry_origin_log_id comes from the meta_retry_origin_log_id context
        key, which only _retry_one sets.
        """
        return self.create({
            'meta_leadgen_id': leadgen_id,
            'trigger': trigger,
            'status': status,
            'lead_id': lead.id if lead else False,
            'match_key': match_key,
            'raw_payload': json.dumps(raw) if raw else False,
            'error_message': error,
            'page_id': page.id if page else False,
            'retry_origin_log_id': self.env.context.get(
                'meta_retry_origin_log_id') or False,
        })

    # ------------------------------------------------------------------ #
    # Retention. Every webhook replay and cron overlap adds a
    # skipped_idempotent row, so old rows have to be pruned. Failed and
    # pending rows are kept since someone may still need to act on them.
    # ------------------------------------------------------------------ #
    @api.model
    def _cron_vacuum_logs(self, batch=5000):
        """Delete success/skipped rows older than the retention period.

        At most ``batch`` rows per run, so the first sweep on a big table
        doesn't run as one huge transaction; the daily cron catches up.
        """
        days = self._sync_log_retention_days()
        if days <= 0:
            return
        cutoff = fields.Datetime.to_string(
            fields.Datetime.now() - timedelta(days=days))
        stale = self.search([
            ('create_date', '<', cutoff),
            ('status', 'in', ('success', 'skipped_idempotent',
                              'skipped_duplicate')),
        ], limit=batch)
        if stale:
            stale.unlink()

    @api.model
    def _sync_log_retention_days(self):
        """Retention in days from meta_lead_ads.sync_log_retention_days
        (default 14). 0 turns pruning off."""
        raw = self.env['ir.config_parameter'].sudo().get_param(
            'meta_lead_ads.sync_log_retention_days', default='14')
        try:
            return max(0, int(raw))
        except (TypeError, ValueError):
            return 14

    # ------------------------------------------------------------------ #
    # Manual retry. The real work (idempotency, error handling, dedup) is in
    # ingest_leadgen; this only drives it and handles locking.
    # Write access on this model is admin-only, so CRM managers can see failed
    # rows but can't retry them.
    # ------------------------------------------------------------------ #
    def action_retry(self):
        """Retry the selected failed rows and return a summary notification.

        Each row runs in its own savepoint so one unexpected error doesn't
        stop the rest. If a row raises unexpectedly, the savepoint rollback
        also undoes its retry_count bump, so it is applied again here.
        """
        failed = self.filtered(lambda r: r.status == 'failed')
        if not failed:
            raise UserError(_("Select at least one failed sync to retry."))
        succeeded = 0
        still_failed = 0
        for row in failed:
            try:
                with self.env.cr.savepoint():
                    outcome = row._retry_one()
            except Exception:
                # The savepoint cleared the cache, so retry_count is re-read
                # from the DB and the +1 counts this attempt once.
                _logger.exception(
                    "Meta sync retry: unexpected error on log id=%s "
                    "(no token/payload logged)", row.id)
                row.write({'status': 'failed',
                           'error_message': 'Unexpected retry error',
                           'retry_count': row.retry_count + 1})
                still_failed += 1
                continue
            # Anything but 'ok' counts as still failed; the row's
            # error_message says why.
            if outcome == 'ok':
                succeeded += 1
            else:
                still_failed += 1
        return self._retry_notification(succeeded, still_failed)

    def _lock_for_retry(self):
        """Lock this row with NOWAIT. Return True if locked, False if another
        retry already holds it.

        The lock runs in its own savepoint so a failed attempt leaves the
        cursor usable for writing the error message. NOWAIT stops two
        retries of the same row (two admins, or form button plus bulk action)
        from overlapping.
        """
        self.ensure_one()
        try:
            with self.env.cr.savepoint():
                self.env.cr.execute(
                    "SELECT id FROM meta_sync_log WHERE id = %s "
                    "FOR UPDATE NOWAIT", (self.id,))
            return True
        except LockNotAvailable:
            return False
        except Exception as e:   # pragma: no cover - psycopg2 variant fallback
            # Some psycopg2 builds raise a plain OperationalError with the
            # LOCK_NOT_AVAILABLE pgcode instead.
            if getattr(e, 'pgcode', None) == errorcodes.LOCK_NOT_AVAILABLE:
                return False
            raise

    def _retry_one(self):
        """Retry one failed row through ingest_leadgen.

        Returns 'ok', 'transient' or 'failed'. Error messages carry no page
        id, token or payload.
        """
        self.ensure_one()
        # Losing the lock means another retry is running; it does the count.
        if not self._lock_for_retry():
            self.write({'error_message': 'Retry already in progress for this row'})
            return 'failed'
        # Count the attempt here and nowhere else.
        self.retry_count += 1
        page = self._resolve_page()
        if not page:
            self.write({'error_message': 'Cannot resolve Meta page for retry'})
            return 'failed'
        # No stored payload: pass raw=None and ingest re-fetches from Graph.
        if self.raw_payload:
            try:
                raw = json.loads(self.raw_payload)
            except (ValueError, TypeError):
                self.write(
                    {'error_message': 'Stored payload is not valid JSON'})
                return 'failed'
        else:
            raw = None
        # The context key lets _record tag the new log row with this row's id.
        try:
            ingest = self.with_context(meta_retry_origin_log_id=self.id).env[
                'meta.lead.ingest']
            lead = ingest.ingest_leadgen(
                page, self.meta_leadgen_id, trigger='manual', raw=raw)
        except (MetaTransientError, MetaRateLimitError) as e:
            # The exception text is already redacted.
            self.write({'error_message': str(e)})
            return 'transient'
        except (MetaPermanentError, MetaAuthError) as e:
            self.write({'error_message': str(e)})
            return 'failed'
        self._reconcile_after_ingest(lead)
        return 'ok'

    def _reconcile_after_ingest(self, lead):
        """Merge the log row the retry just created into this one, so the
        lead keeps a single row.

        The new row is found by retry_origin_log_id, leadgen id and a success
        or skipped_idempotent status, not by create_date, so an older manual
        row or a parallel retry can't be merged by mistake. Ingest never
        writes skipped_duplicate for a retry, so it isn't matched.
        """
        self.ensure_one()
        dups = self.search([
            ('retry_origin_log_id', '=', self.id),
            ('id', '!=', self.id),
            ('meta_leadgen_id', '=', self.meta_leadgen_id),
            ('status', 'in', ('success', 'skipped_idempotent')),
        ])
        if dups:
            if len(dups) > 1:
                # Shouldn't happen; use the first and log it.
                _logger.warning(
                    "Meta sync retry: %s correlated fresh rows for log id=%s "
                    "(expected <=1; collapsing the first)", len(dups), self.id)
            dup = dups[0]
            # Copy the result, payload and page across, then drop the new row.
            # trigger becomes 'manual' since the last attempt was a retry.
            self.write({
                'status': dup.status,
                'lead_id': dup.lead_id.id,
                'match_key': dup.match_key,
                'error_message': False,
                'trigger': 'manual',
                'raw_payload': dup.raw_payload,
                'page_id': dup.page_id.id,
            })
            dup.unlink()
        else:
            # Ingest returned a lead but wrote no matching row. Mark this row
            # done from the returned lead and log a warning.
            self.write({
                'status': 'success',
                'lead_id': lead.id if lead else False,
                'trigger': 'manual',
                'error_message': False,
            })
            _logger.warning(
                "Meta sync retry: ingestion returned a lead with no correlatable "
                "log row for log id=%s (no token/payload logged)", self.id)

    def _resolve_page(self):
        """Find the meta.page to re-fetch from; may return an empty recordset.

        Tries page_id on this row, then the lead's page, the lead's form's
        page, and finally a search on the lead's raw meta_page_id (unique
        across accounts). Keep the page id and name out of any message.
        """
        self.ensure_one()
        if self.page_id:
            return self.page_id
        lead = self.lead_id
        if lead:
            if lead.meta_page_id_ref:
                return lead.meta_page_id_ref
            if lead.meta_form_id_ref and lead.meta_form_id_ref.page_id:
                return lead.meta_form_id_ref.page_id
            if lead.meta_page_id:
                return self.env['meta.page'].search(
                    [('page_id', '=', lead.meta_page_id)], limit=1)
        return self.env['meta.page']

    def _retry_notification(self, succeeded, still_failed):
        """Notification with the retry counts only."""
        total = succeeded + still_failed
        return {
            'type': 'ir.actions.client',
            'tag': 'display_notification',
            'params': {
                'message': _(
                    "%(n)s retried: %(ok)s succeeded, %(bad)s still failed",
                    n=total, ok=succeeded, bad=still_failed),
                'type': 'success' if not still_failed else 'warning',
                'sticky': False,
            },
        }
