# Meta Lead Ads Integration for Odoo — © 2026 CoreSys Builders. All rights reserved.
# Licensed under the Odoo Proprietary License v1.0 (OPL-1).
# Unauthorized copying, redistribution, or resale of this software, in whole or in
# part, via any medium, is strictly prohibited and constitutes a license violation.
# OPL-1: https://www.odoo.com/documentation/19.0/legal/licenses.html#odoo-apps

import json
import logging
from datetime import timedelta

from odoo import _, api, fields, models
from odoo.exceptions import UserError
from psycopg2 import errorcodes
from psycopg2.errors import LockNotAvailable   # row-lock fast-fail

from .exceptions import (MetaAuthError, MetaPermanentError,
                         MetaRateLimitError, MetaTransientError)

_logger = logging.getLogger(__name__)


class MetaSyncLog(models.Model):
    _name = 'meta.sync.log'
    _description = 'Meta Sync Log'
    _order = 'create_date desc'   # newest attempts first

    # Correlation id -- NOT unique here (unlike crm.lead.meta_leadgen_id).
    meta_leadgen_id = fields.Char(string='Meta Lead ID', index=True)
    # Created up-front as 'pending', resolved to a terminal status.
    status = fields.Selection(
        [('pending', 'Pending'), ('success', 'Success'),
         ('skipped_duplicate', 'Skipped (duplicate)'),
         ('skipped_idempotent', 'Skipped (idempotent)'),
         ('failed', 'Failed')],
        string='Status', default='pending', required=True)
    # Optional at the model level by design (no required=True) -- see action note.
    trigger = fields.Selection(
        [('webhook', 'Webhook'), ('cron', 'Cron'), ('manual', 'Manual')],
        string='Trigger')
    retry_count = fields.Integer(string='Retry Count', default=0)
    # Keep token-free: the ingest service must never write a token/secret here.
    error_message = fields.Text(string='Error Message')
    # Business-dedup write target -- the ingest service sets which key matched.
    match_key = fields.Selection(
        [('email', 'Email'), ('phone', 'Phone'), ('none', 'None')],
        string='Match Key')
    # ondelete='set null' so deleting a lead keeps its audit trail.
    lead_id = fields.Many2one('crm.lead', string='Lead', ondelete='set null')
    # The owning Meta page, stamped by the ingest service on the pre-fetch
    # failure row (no lead, no raw_payload) so the manual retry can re-fetch
    # from Graph by leadgen_id. Nullable (mirrors lead_id's ondelete='set
    # null'); Odoo auto-migrates the column on module update (no script).
    page_id = fields.Many2one('meta.page', string='Meta Page',
                              ondelete='set null')
    # Manual-retry attempt-correlation key (token-free, internal). Stamped by
    # _record from the meta_retry_origin_log_id context key set by _retry_one --
    # it points at the clicked failed row so _reconcile_after_ingest finds
    # exactly the fresh terminal row this attempt spawned (not by create_date).
    # Indexed: the reconcile searches it on every retry and this model grows
    # unbounded, so a direct-lookup index pays for itself. Auto-migrated.
    retry_origin_log_id = fields.Many2one(
        'meta.sync.log', string='Retry Origin Log',
        ondelete='set null', index=True)
    # Raw Meta payload -- contains lead PII. Field-level groups= strips the key
    # from a non-admin ORM read and the view. Never mirror into a compute/
    # related field, never sudo()-read into a non-admin context.
    raw_payload = fields.Text(string='Raw Payload',
                              groups='meta_lead_ads.group_meta_admin')

    @api.model
    def _record(self, leadgen_id, trigger, status, lead=None,
                match_key=None, raw=None, error=None, page=None):
        """Single audit-write API. Never pass a token/secret in `raw` or
        `error`.

        `raw` is the full Graph lead payload (incl. the complete field_data
        array) and is stored here losslessly (json.dumps) -- this is the
        authoritative copy; meta.lead.answer.value holds only a joined display
        string.

        `page` stamps `page_id` on the created row. Only the ingest service's
        pre-fetch failure path passes it: that row has no lead and no
        raw_payload, so the manual retry needs the page to re-fetch from Graph.
        The success / idempotent rows already carry lead_id, from which
        _resolve_page navigates, so they do not also need page=.

        `retry_origin_log_id` is stamped from the `meta_retry_origin_log_id`
        context key -- the manual-retry attempt-correlation handle set by
        meta.sync.log._retry_one. Non-manual callers (webhook/cron) never set
        the key, so their rows carry retry_origin_log_id = False (unaffected).
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
    # Retention sweep. Without this the table grows unbounded: every webhook
    # replay and every cron overlap re-query writes a fresh skipped_idempotent
    # row. We prune only the non-actionable terminal rows (success / skipped_*)
    # older than the retention window; 'failed' and 'pending' rows are kept
    # because they are actionable (manual retry / triage) and an admin may
    # still need them. A retention of 0 disables the sweep.
    # ------------------------------------------------------------------ #
    @api.model
    def _cron_vacuum_logs(self, batch=5000):
        """Delete non-actionable terminal audit rows older than the retention
        window (ir.config_parameter meta_lead_ads.sync_log_retention_days,
        default 14). Bounded per run by ``batch`` so a first sweep on a large
        backlog never holds one giant transaction; the daily cron drains the
        rest over subsequent runs."""
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
        """Retention window in days (default 14). Admin-tunable via
        ir.config_parameter key meta_lead_ads.sync_log_retention_days; 0
        disables pruning entirely."""
        raw = self.env['ir.config_parameter'].sudo().get_param(
            'meta_lead_ads.sync_log_retention_days', default='14')
        try:
            return max(0, int(raw))
        except (TypeError, ValueError):
            return 14

    # ------------------------------------------------------------------ #
    # Manual retry orchestration. action_retry is the sole public entry; every
    # hard sub-problem (idempotency, error classification, transport, business
    # dedup) is solved upstream in the ingest service ingest_leadgen -- the sole
    # crm.lead create surface, whose public signature stays unchanged: the
    # attempt-correlation key rides on with_context, read by _record. This
    # block only orchestrates and pins the concurrency contract.
    # ACL: write on this model is admin-only (ir.model.access.csv
    # group_meta_admin perm_write=1); group_meta_user / sale_manager are
    # read-only, so a CRM manager can see failed rows but cannot run
    # action_retry (ACL-rejected).
    # ------------------------------------------------------------------ #
    def action_retry(self):
        """Bulk manual retry. Filters status=='failed' (UserError on an empty/
        non-failed selection), then retries each row in its own savepoint
        (per-row isolation -- one row's unexpected failure does not abort the
        batch), tallying counts for a token-free summary notification (counts
        only -- never a page-id/token/raw JSON).

        retry_count is owned by _retry_one (the increment fires once,
        immediately after the lock). On the outer unexpected catch the per-row
        savepoint has already rolled that increment back together with the
        partial work (BaseCursor.savepoint calls clear() on exception), so it is
        re-applied here on the clean cursor -- a row that increments then raises
        an unexpected error past the lock is still counted exactly once.
        """
        failed = self.filtered(lambda r: r.status == 'failed')
        if not failed:
            raise UserError(_("Select at least one failed sync to retry."))
        succeeded = 0
        still_failed = 0
        for row in failed:
            try:
                # Per-row savepoint (mirrors meta_webhook_event._cron_drain): a
                # genuinely-unexpected raise on this row rolls back only its
                # partial work, leaving the siblings already retried intact.
                with self.env.cr.savepoint():
                    outcome = row._retry_one()
            except Exception:
                # Genuinely-unexpected (non-typed) error: isolate it, token-free.
                # The per-row savepoint cleared the ORM env and rolled back
                # _retry_one's post-lock increment along with the partial work,
                # so re-apply it here on the now-clean cursor. row.retry_count
                # reads fresh from the DB (cache was cleared by
                # savepoint.clear()), so the `+ 1` restores the exactly-once
                # count without double-count.
                _logger.exception(
                    "Meta sync retry: unexpected error on log id=%s "
                    "(no token/payload logged)", row.id)
                row.write({'status': 'failed',
                           'error_message': 'Unexpected retry error',
                           'retry_count': row.retry_count + 1})
                still_failed += 1
                continue
            # 'ok' -> succeeded; transient/permanent/page-fail/json-fail all
            # stay failed and count as still_failed (the row's error_message
            # carries the redacted distinction). The lock-loser ('failed', no
            # increment) also counts as still_failed.
            if outcome == 'ok':
                succeeded += 1
            else:
                still_failed += 1
        return self._retry_notification(succeeded, still_failed)

    def _lock_for_retry(self):
        """Acquire a fast-fail row lock on the clicked row. Issues the sole
        no-wait row lock in this file, wrapped in its own nested savepoint so a
        LockNotAvailable is rolled back cleanly -- the enclosing cursor is left
        usable so the lock-loser can still write its 'already being retried'
        error on a clean cursor (without the inner savepoint that post-lock-
        failure write would itself fail on a poisoned cursor). Returns True on
        lock acquired, False on lock-loss.

        NOWAIT (not SKIP LOCKED, the _cron_drain idiom) hard fast-fails on this
        specific clicked row -- two retries of the same failed row (form button
        + bulk, or two admins) cannot interleave.
        """
        self.ensure_one()
        try:
            with self.env.cr.savepoint():
                self.env.cr.execute(
                    "SELECT id FROM meta_sync_log WHERE id = %s "
                    "FOR UPDATE NOWAIT", (self.id,))
            return True
        except LockNotAvailable:
            # Rolled back to the inner savepoint -> cursor is clean.
            return False
        except Exception as e:   # pragma: no cover - psycopg2 variant fallback
            # Fallback for a psycopg2 build that surfaces lock-loss as a generic
            # OperationalError carrying the LOCK_NOT_AVAILABLE pgcode.
            if getattr(e, 'pgcode', None) == errorcodes.LOCK_NOT_AVAILABLE:
                return False
            raise

    def _retry_one(self):
        """Retry one failed row through the ingest service (typed contract
        mirrored from meta_webhook_event._process_one). Returns 'ok' /
        'transient' / 'failed'. Every error_message / _logger string is
        token-free -- counts + str(typed-exception) only, never page-id/token/
        raw JSON.
        """
        self.ensure_one()
        # 1. LOCK FIRST. The loser writes its error on a clean cursor (the
        #    helper contained the lock failure in its own savepoint) and does
        #    not increment -- the lock holder owns this row's increment.
        if not self._lock_for_retry():
            self.write({'error_message': 'Retry already in progress for this row'})
            return 'failed'
        # 2. INCREMENT ONCE -- the single owner of this attempt's increment. No
        #    path after this point increments again.
        self.retry_count += 1
        # 3. RESOLVE PAGE. Unresolvable -> token-free fail (no secret leak).
        page = self._resolve_page()
        if not page:
            self.write({'error_message': 'Cannot resolve Meta page for retry'})
            return 'failed'
        # 4. DECODE PAYLOAD (typed). A corrupt stored payload is a known
        #    failed-retry path before the ingest service, not the generic
        #    unexpected branch. raw=None => the re-fetch-from-Graph branch.
        if self.raw_payload:
            try:
                raw = json.loads(self.raw_payload)
            except (ValueError, TypeError):
                self.write(
                    {'error_message': 'Stored payload is not valid JSON'})
                return 'failed'
        else:
            raw = None
        # 5. CALL THE INGEST SERVICE with the correlation key on context (the
        #    public signature is unchanged; the key rides on with_context and is
        #    stamped onto the fresh row by _record). Mirror the typed split.
        try:
            ingest = self.with_context(meta_retry_origin_log_id=self.id).env[
                'meta.lead.ingest']
            lead = ingest.ingest_leadgen(
                page, self.meta_leadgen_id, trigger='manual', raw=raw)
        except (MetaTransientError, MetaRateLimitError) as e:
            # Recoverable: stay failed, error replaced (str is redacted
            # upstream).
            self.write({'error_message': str(e)})
            return 'transient'
        except (MetaPermanentError, MetaAuthError) as e:
            # Permanent/auth: stay failed, surfaced -- never marked success.
            self.write({'error_message': str(e)})
            return 'failed'
        # 6. Clean return: collapse to a single surviving row.
        self._reconcile_after_ingest(lead)
        return 'ok'

    def _reconcile_after_ingest(self, lead):
        """Collapse the ingest service's fresh terminal log row into the
        clicked row so exactly one row survives per leadgen_id, correlated by a
        hardened domain: the exact attempt key ``retry_origin_log_id ==
        self.id`` plus a matching ``meta_leadgen_id`` and a terminal status --
        not create_date, so a stale prior manual row, a malformed row, or a
        concurrent retry cannot collapse the wrong row.

        ``skipped_duplicate`` is intentionally excluded from the terminal set --
        the ingest service never emits it for an idempotent hit (it emits
        ``skipped_idempotent``); only success / skipped_idempotent are terminal
        fresh-row statuses. Does not increment retry_count (owned by
        _retry_one).
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
                # Defensive: the exact attempt key + terminal filter should
                # yield at most one. If not, take the first terminal one and
                # warn (token-free -- id only).
                _logger.warning(
                    "Meta sync retry: %s correlated fresh rows for log id=%s "
                    "(expected <=1; collapsing the first)", len(dups), self.id)
            dup = dups[0]
            # Copy the outcome and the provenance (raw_payload + page_id) onto
            # the surviving row before unlinking the duplicate (audit-trail
            # completeness). trigger is overwritten to 'manual': the surviving
            # row reflects the latest, admin-initiated attempt even if it was
            # originally webhook/cron.
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
            # Defensive: the ingest service returned a lead but emitted no
            # correlatable fresh row (a contract edge). Set lead_id from the
            # returned lead (not just status), trigger='manual', and warn
            # token-free.
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
        """Resolve the owning meta.page for a re-fetch. The stamped ``page_id``
        column wins; otherwise navigate the linked lead: ``meta_page_id_ref``
        -> ``meta_form_id_ref.page_id`` -> a deterministic ``meta.page.search``
        on the raw Char ``meta_page_id`` (globally unique via page_id_uniq, so
        no account scoping needed). Returns a (possibly empty) ``meta.page``
        recordset.

        Never place the raw external page id, the page name, or a token into any
        error/log/notification string.
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
        """A token-free display_notification reporting counts only (never a
        page-id/token/raw JSON). Both transient and permanent outcomes collapse
        into ``still_failed`` here: the admin sees 'still failed' and the row's
        error_message carries the redacted distinction.
        """
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
