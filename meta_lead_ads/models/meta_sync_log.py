# Meta Lead Ads Integration for Odoo — © 2026 CoreSys Builders. All rights reserved.
# Licensed under the Odoo Proprietary License v1.0 (OPL-1).
# Unauthorized copying, redistribution, or resale of this software, in whole or in
# part, via any medium, is strictly prohibited and constitutes a license violation.
# OPL-1: https://www.odoo.com/documentation/19.0/legal/licenses.html#odoo-apps

import json
import logging
import time
from datetime import timedelta

from odoo import _, api, fields, models
from odoo.exceptions import AccessError, UserError, ValidationError
from psycopg2 import Error as PsycopgError
from psycopg2 import errorcodes
from psycopg2.errors import (DeadlockDetected, LockNotAvailable,
                             SerializationFailure)

# Read at call time so tests can patch the retry limits.
from . import const
from .exceptions import (MetaAuthError, MetaLeadRemovedError,
                         MetaPermanentError, MetaRateLimitError,
                         MetaTransientError)
from .meta_lead_ingest import _quiet_lost_races

_logger = logging.getLogger(__name__)
# Module-level so tests can patch the clock of the retry time budget.
_monotonic = time.monotonic

# Shown on the sync row and queue entry of a lead deleted in Odoo.
_REMOVED_MESSAGE = 'Removed in Odoo; skipped'

# Shown on the sync row and queue entry of a lead whose Page is archived.
# No Page id or name: sales managers read it.
_PAGE_ARCHIVED_MESSAGE = 'This Page is archived in Odoo. Reactivate it, then Retry.'
# Replaces it once the Page is active again and the lead is back in line.
_PAGE_REQUEUED_MESSAGE = 'Queued again after its Page was reactivated.'

# Most sync or queue rows one erase handles, so a large selection can't
# turn into one long transaction. Read at call time so tests can patch it.
_ERASE_ROW_LIMIT = 200

# At most one open failed row per Meta lead, and one per form for failures
# that concern a whole form rather than a single lead.
_FAILED_LEADGEN_UNIQ = 'meta_sync_log_failed_leadgen_uniq'
_FAILED_FORM_UNIQ = 'meta_sync_log_failed_form_uniq'

# Ways an insert of a failure row can lose to another transaction recording
# the same failure.
_LOST_FAILURE_INSERT_CODES = (
    errorcodes.UNIQUE_VIOLATION,
    errorcodes.SERIALIZATION_FAILURE,
    errorcodes.DEADLOCK_DETECTED,
    errorcodes.LOCK_NOT_AVAILABLE,
)

# A NOWAIT lock that fails with one of these means another transaction holds
# the row, or changed it after ours started. Either way it is busy.
_BUSY_CODES = (
    errorcodes.LOCK_NOT_AVAILABLE,
    errorcodes.SERIALIZATION_FAILURE,
)

# Busy, plus a deadlock: another transaction is in the way and trying again
# later will work.
_CONTENTION_CODES = _BUSY_CODES + (errorcodes.DEADLOCK_DETECTED,)

# Advisory lock key the log cleanup holds for its whole transaction, so a
# second run started meanwhile leaves instead of failing on the rows the
# first one has locked or deleted.
VACUUM_LOCK_KEY = 'meta_lead_ads.vacuum_logs'


def _is_db_contention(exc):
    """True when ``exc`` means another transaction got in the way."""
    if isinstance(exc, (LockNotAvailable, SerializationFailure,
                        DeadlockDetected)):
        return True
    # Some psycopg2 builds raise a plain OperationalError that only carries
    # the code, which is why the lock checks elsewhere read pgcode too.
    return getattr(exc, 'pgcode', None) in _CONTENTION_CODES


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
         ('skipped_removed', 'Skipped (removed in Odoo)'),
         ('failed', 'Failed')],
        string='Status', default='pending', required=True)
    trigger = fields.Selection(
        [('webhook', 'Webhook'), ('cron', 'Cron'), ('manual', 'Manual')],
        string='Trigger')
    # Moves each time the row's own ingestion path records the failure, so
    # the webhook health can compare it with the success stamp. write_date
    # is no use there: the cleanup and retries also write failed rows.
    failed_at = fields.Datetime(string='Last Failed On', readonly=True,
                                copy=False)
    retry_count = fields.Integer(string='Retry Count', default=0)
    # Shown to non-admins, so never put a token or secret in here.
    error_message = fields.Text(string='Error Message')
    # Which key the business dedup matched on.
    match_key = fields.Selection(
        [('email', 'Email'), ('phone', 'Phone'), ('none', 'None')],
        string='Match Key')
    # Distinct open leads that matched, the chosen one included.
    match_candidates = fields.Integer(string='Match Candidates')
    # The count came from a capped search, so the list below is partial.
    match_candidates_truncated = fields.Boolean(string='More Than Listed')
    # Ids only, never names or contact details.
    match_other_ids = fields.Char(
        string='Other Matching Leads (at import)',
        help="Lead ids and how they matched, as they were when this "
             "submission was imported. Those leads may since have been "
             "merged or deleted.")
    # set null so deleting a lead keeps its log history. lead_id and page_id
    # are indexed because company_id below depends on them: every company
    # change on any crm.lead looks this table up by lead, and it gets large.
    lead_id = fields.Many2one('crm.lead', string='Lead', ondelete='set null',
                              index='btree_not_null')
    # Set on failures that happened before the lead was fetched (no lead, no
    # payload), so a manual retry knows which page to re-fetch from. Meta
    # users only, like form_id: CRM managers read sync logs but not Pages.
    page_id = fields.Many2one('meta.page', string='Meta Page',
                              ondelete='set null', index='btree_not_null',
                              groups='meta_lead_ads.group_meta_user')
    # Set on backfill failures that concern a whole form, so a retry knows
    # which form to sweep. Meta users only: CRM managers read sync logs but
    # not lead forms.
    form_id = fields.Many2one('meta.lead.form', string='Meta Lead Form',
                              ondelete='set null', index='btree_not_null',
                              groups='meta_lead_ads.group_meta_user')
    # Follows the lead, so a row never shows another company's lead. Falls
    # back to the Page when there is no lead, e.g. after it was deleted.
    company_id = fields.Many2one(
        'res.company', string='Company', compute='_compute_company_id',
        store=True, index=True)
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
    # Why raw_payload is empty when it was once set. Holds no personal data,
    # so everyone who reads the row sees it. No default, so upgrading does
    # not rewrite the table.
    payload_cleared = fields.Selection(
        [('erased', 'Erased'), ('expired', 'Payload expired')],
        string='Payload', readonly=True, copy=False)

    # The ORM does not enforce readonly on write, so without a guard an RPC
    # call could move a row into another company's view.
    _derived_company_fields = ('company_id',)
    # Only the cleanup and erase code may set these, or a user could make a
    # row look erased while it still holds data.
    _system_set_fields = ('payload_cleared',)

    @api.depends('lead_id.company_id', 'page_id.lead_company_id')
    def _compute_company_id(self):
        for log in self:
            log.company_id = (log.lead_id.company_id
                              or log.page_id.lead_company_id)

    @api.model_create_multi
    def create(self, vals_list):
        self._check_derived_company_vals(vals_list)
        records = super().create(vals_list)
        if not self.env.su:
            records._check_link_access()
        return records

    def write(self, vals):
        self._check_derived_company_vals([vals])
        res = super().write(vals)
        if not self.env.su and ('lead_id' in vals or 'page_id' in vals
                                or 'form_id' in vals):
            self._check_link_access()
        return res

    def _check_link_access(self):
        # The company follows the lead and the Page, so linking a row to one
        # the writer can't see would move it into another company's logs.
        # _resolve_page falls back to the form's Page, so a form the writer
        # can't see, or one from another Page, would let a retry fetch from
        # somewhere the row never came from.
        # Values are read as sudo, the check runs as the writer.
        Lead = self.env['crm.lead']
        Page = self.env['meta.page']
        Form = self.env['meta.lead.form']
        for rec in self.sudo():
            if ((rec.lead_id
                 and not Lead.browse(rec.lead_id.id).has_access('read'))
                    or (rec.page_id
                        and not Page.browse(rec.page_id.id).has_access('read'))
                    or (rec.form_id
                        and (not Form.browse(rec.form_id.id).has_access('read')
                             or (rec.page_id
                                 and rec.form_id.page_id != rec.page_id)))):
                raise ValidationError(_(
                    "You can only link a lead, Page or form you can access."))

    def _check_derived_company_vals(self, vals_list):
        if self.env.su:
            return
        for vals in vals_list:
            if any(name in vals for name in self._derived_company_fields):
                raise AccessError(_(
                    "The company on this record is set automatically and "
                    "can't be changed directly."))
            if any(name in vals for name in self._system_set_fields):
                raise AccessError(_(
                    "The payload marker on this record is set automatically "
                    "and can't be changed directly."))

    @api.model
    def _record(self, leadgen_id, trigger, status, lead=None,
                match_key=None, raw=None, error=None, page=None, form=None,
                candidates=None, truncated=False, other_ids=None):
        """Write one sync-log row. Never pass a token or secret in `raw` or
        `error`.

        `raw` is the full Graph lead payload and is stored as JSON; it is the
        complete copy (meta.lead.answer only keeps display strings).

        Ingest passes `page` on every row, so the row keeps a company after
        its lead is deleted. Other callers pass it on failures, so a retry
        can find the page.

        `form` is set on failures that concern a whole lead form.

        `candidates`, `truncated` and `other_ids` describe a match that
        found more than one open lead.

        retry_origin_log_id comes from the meta_retry_origin_log_id context
        key, which only _retry_one sets.

        `raw` is dropped, and the row marked erased, when the lead's
        personal data was erased.
        """
        # The sweeps keep re-reading recent leads and Meta may send one
        # again, so without this an erased lead's data would be copied
        # straight back into the log.
        erased = False
        if raw:
            erased = bool(lead and lead.sudo().meta_personal_data_erased_at)
            if not erased and leadgen_id:
                erased = bool(self.env['meta.leadgen.tombstone'].sudo()
                              .search_count([('leadgen_id', '=', leadgen_id),
                                             ('removal_reason', '=', 'erased')],
                                            limit=1))
            if erased:
                raw = None
        vals = {
            'meta_leadgen_id': leadgen_id,
            'trigger': trigger,
            'status': status,
            'lead_id': lead.id if lead else False,
            'match_key': match_key,
            'match_candidates': candidates or 0,
            'match_candidates_truncated': bool(truncated),
            'match_other_ids': other_ids or False,
            'raw_payload': json.dumps(raw) if raw else False,
            'error_message': error,
            'page_id': page.id if page else False,
            'retry_origin_log_id': self.env.context.get(
                'meta_retry_origin_log_id') or False,
        }
        if status == 'failed':
            vals['failed_at'] = fields.Datetime.now()
        # form_id is limited to Meta users, so only send it when there is one.
        if form:
            vals['form_id'] = form.id
        row = self.create(vals)
        if erased:
            # The marker is refused to non-superuser writers.
            row.sudo().write({'payload_cleared': 'erased'})
        return row

    def init(self):
        # The drain, the backfill and manual retries can record the same
        # failure at the same time, and a REPEATABLE READ transaction can't
        # see a row another one has just committed, so only the database can
        # keep it to one open row. Done here rather than with
        # models.UniqueIndex so the Odoo 18 and 19 code stay the same.
        # Older databases may already hold duplicates; the newest row of
        # each set is kept. Once an index exists its step is skipped.
        cr = self.env.cr
        cr.execute("SELECT indexname FROM pg_indexes "
                   "WHERE tablename = 'meta_sync_log' AND indexname IN %s",
                   ((_FAILED_LEADGEN_UNIQ, _FAILED_FORM_UNIQ),))
        existing = {row[0] for row in cr.fetchall()}
        if _FAILED_LEADGEN_UNIQ not in existing:
            cr.execute("""
                DELETE FROM meta_sync_log older
                 WHERE older.status = 'failed'
                   AND older.meta_leadgen_id IS NOT NULL
                   AND EXISTS (
                       SELECT 1 FROM meta_sync_log newer
                        WHERE newer.status = 'failed'
                          AND newer.meta_leadgen_id = older.meta_leadgen_id
                          AND newer.id > older.id)
            """)
            if cr.rowcount:
                _logger.info("meta.sync.log: removed %s older duplicate "
                             "failed rows per lead", cr.rowcount)
            cr.execute("""
                CREATE UNIQUE INDEX IF NOT EXISTS
                       meta_sync_log_failed_leadgen_uniq
                    ON meta_sync_log (meta_leadgen_id)
                 WHERE status = 'failed' AND meta_leadgen_id IS NOT NULL
            """)
        if _FAILED_FORM_UNIQ not in existing:
            cr.execute("""
                DELETE FROM meta_sync_log older
                 WHERE older.status = 'failed'
                   AND older.meta_leadgen_id IS NULL
                   AND older.form_id IS NOT NULL
                   AND EXISTS (
                       SELECT 1 FROM meta_sync_log newer
                        WHERE newer.status = 'failed'
                          AND newer.meta_leadgen_id IS NULL
                          AND newer.form_id = older.form_id
                          AND newer.id > older.id)
            """)
            if cr.rowcount:
                _logger.info("meta.sync.log: removed %s older duplicate "
                             "failed rows per form", cr.rowcount)
            cr.execute("""
                CREATE UNIQUE INDEX IF NOT EXISTS
                       meta_sync_log_failed_form_uniq
                    ON meta_sync_log (form_id)
                 WHERE status = 'failed' AND meta_leadgen_id IS NULL
                   AND form_id IS NOT NULL
            """)
        # Serves the daily expiry of failed-row payloads in
        # _cron_vacuum_logs. Partial, so it only holds failed rows that
        # still carry a payload and stays small.
        cr.execute("""
            CREATE INDEX IF NOT EXISTS meta_sync_log_failed_payload_idx
                ON meta_sync_log (create_date)
             WHERE status = 'failed' AND raw_payload IS NOT NULL
        """)

    @api.model
    def _record_failure(self, leadgen_id, trigger, error, page=None,
                        form=None, failed_at=None):
        """Keep one durable failed row per Meta lead, or per form for
        failures that concern a whole form.

        The caller writes it once a failure is final. `error` must carry no
        token, payload or Page id: CRM managers read it. Returns the row, or
        an empty recordset when another transaction recorded the same
        failure first.

        A reused row that is or becomes a webhook failure stays one, and
        its failure time only moves when the caller's trigger is the row's.

        A Retry passes `failed_at`, the time of the failure it retries. A
        reused row then keeps its trigger and failure time. A row the Retry
        has to make is dated now: the event may have failed after the last
        success, and an older date would hide that from the webhook health.
        """
        Log = self.sudo().with_context(meta_retry_origin_log_id=False)
        if form and not page:
            page = form.page_id
        if leadgen_id:
            domain = [('meta_leadgen_id', '=', leadgen_id)]
        elif form:
            domain = [('meta_leadgen_id', '=', False),
                      ('form_id', '=', form.id)]
        else:
            domain = None
        row = Log.browse()
        if domain:
            # The unique indexes make this at most one row.
            row = Log.search(domain + [('status', '=', 'failed')],
                             order='id desc', limit=1)
        if row and not self.env.su and row.company_id and (
                row.company_id not in self.env.user.company_ids):
            # A caller outside the row's company must not move it into
            # their own, or rewrite what another company sees.
            return row
        if row:
            # A retry or another failure writer holding the row will leave
            # its own, equally current, outcome, so don't wait for it.
            try:
                locked = row._lock_for_retry()
            except SerializationFailure:
                locked = False
            if not locked:
                return row
            # The latest caller knows the current Page and error, so both
            # replace the old values. A webhook failure keeps the row counted
            # as one until a webhook success passes it, so a backfill sweep
            # that fails the same lead again changes the error but neither
            # the trigger nor the failure time. Any other caller records its
            # own trigger and time.
            if failed_at:
                # A Retry is not a new failure, so the row keeps its trigger
                # and time; only the error and the Page move.
                vals = {'error_message': error}
            else:
                new_trigger = ('webhook' if 'webhook' in (row.trigger, trigger)
                               else trigger)
                vals = {'error_message': error, 'trigger': new_trigger}
                if trigger == new_trigger:
                    vals['failed_at'] = fields.Datetime.now()
            if page:
                vals['page_id'] = page.id
            if form:
                vals['form_id'] = form.id
            row.write(vals)
            return row

        cr = self.env.cr
        cr.execute("SHOW lock_timeout")
        previous_timeout = cr.fetchone()[0]
        try:
            with _quiet_lost_races(), cr.savepoint():
                # The insert can only wait on another writer of the same
                # lead. Five seconds keeps a stuck one from holding up the
                # cron.
                cr.execute("SELECT set_config('lock_timeout', '5s', true)")
                return Log._record(leadgen_id or False, trigger, 'failed',
                                   match_key='none', error=error,
                                   page=page, form=form)
        except PsycopgError as e:
            if getattr(e, 'pgcode', None) not in _LOST_FAILURE_INSERT_CODES:
                raise
            constraint = getattr(getattr(e, 'diag', None),
                                 'constraint_name', None)
            if (e.pgcode == errorcodes.UNIQUE_VIOLATION
                    and constraint not in (None, _FAILED_LEADGEN_UNIQ,
                                           _FAILED_FORM_UNIQ)):
                raise
            _logger.info("Meta sync log: %s failure already recorded by "
                         "another transaction (no token/payload logged)",
                         trigger)
            return Log.browse()
        finally:
            # SET LOCAL would otherwise last to the end of the transaction,
            # and the drain and backfill handle many leads in one.
            cr.execute("SELECT set_config('lock_timeout', %s, true)",
                       (previous_timeout,))

    @api.model
    def _record_removed_skip(self, leadgen_ids, trigger, page=None):
        """Note that these Meta leads were skipped because they were deleted
        in Odoo. Every path that meets a removed lead calls this.

        Per lead: an open failed row becomes skipped_removed, which also
        frees the one-open-failed-row index; else an existing
        skipped_removed row is kept; else one is written. Repeating a path
        therefore leaves one row. Two skips of the same lead in truly
        concurrent transactions may each write one; that is rare and
        harmless, so no lock is taken for it.

        Only ids recorded as removed are touched, so a caller's mistake
        can't turn a real failure into a skip. Callers pass the Page, so a
        new row gets its company and isn't shared with every company.
        """
        Log = self.sudo().with_context(meta_retry_origin_log_id=False)
        ids = sorted(self.env['meta.leadgen.tombstone']._removed(leadgen_ids))
        if not ids:
            return Log.browse()
        rows = Log.search([('meta_leadgen_id', 'in', ids),
                           ('status', 'in', ('failed', 'skipped_removed'))],
                          order='id desc')
        failed = {}
        skipped = set()
        for row in rows:
            if row.status == 'failed':
                failed.setdefault(row.meta_leadgen_id, row)
            else:
                skipped.add(row.meta_leadgen_id)
        done = Log.browse()
        for leadgen_id in ids:
            row = failed.get(leadgen_id)
            if row:
                # A retry holding the row meets the removed lead too and
                # settles it itself, so don't wait for it.
                if row._lock_for_retry():
                    row.write({'status': 'skipped_removed',
                               'error_message': _REMOVED_MESSAGE})
                done |= row
            elif leadgen_id not in skipped:
                done |= Log._record(leadgen_id, trigger, 'skipped_removed',
                                    error=_REMOVED_MESSAGE, page=page)
        return done

    # ------------------------------------------------------------------ #
    # Erase of rows that never became a lead. A lead's own data is erased
    # from the lead (crm.lead.action_meta_erase_personal_data), which also
    # clears these rows.
    # ------------------------------------------------------------------ #
    @api.model
    def _check_erase_access(self):
        # The erase runs as sudo across every company's rows, and RPC
        # reaches it directly.
        if (not self.env.su and not self.env.user.has_group(
                'meta_lead_ads.group_meta_admin')):
            raise AccessError(_("Meta Admin access required."))

    def action_erase_personal_data(self):
        """Erase the stored Meta data of the selected rows' Meta leads."""
        self._check_erase_access()
        self.check_access('read')
        rows = self.sudo().filtered(
            lambda r: r.meta_leadgen_id and r.payload_cleared != 'erased')
        if not rows:
            raise UserError(_("Select at least one row whose personal data "
                              "hasn't been erased yet."))
        if len(rows) > _ERASE_ROW_LIMIT:
            raise UserError(_("Select at most %(limit)s rows at a time.",
                              limit=_ERASE_ROW_LIMIT))
        # The shared helper refuses a lead that still holds one of the ids.
        self._erase_leadgen_copies(
            sorted(set(rows.mapped('meta_leadgen_id'))), 'manual',
            details=rows._erase_details())
        return self._erase_notification(len(rows))

    def _erase_details(self):
        """Page, form and company per Meta lead id, newest row first, for
        the removed-lead record the erase writes."""
        details = {}
        for row in self.sudo().sorted('id', reverse=True):
            found = details.setdefault(row.meta_leadgen_id, {})
            for key, value in (('page_id', row.page_id.id),
                               ('form_id', row.form_id.id),
                               ('company_id', row.company_id.id)):
                if value and not found.get(key):
                    found[key] = value
        return details

    def action_open_quarantined_rows(self):
        """List the rows no company can see that still hold a payload, so
        a Meta Admin can erase them.

        The record rule hides them from everyone and is not widened. The
        list is a snapshot holding only each row's id, Meta lead id, status
        and date, built through sudo.

        A list header button, so the client sends the selected rows, or
        none. They aren't used.
        """
        self._check_erase_access()
        # The 90-day payload expiry keeps this set small.
        found = self.sudo().search_read(
            [('company_id', '=', False), ('lead_id', '=', False),
             ('meta_leadgen_id', '!=', False), ('raw_payload', '!=', False)],
            ['meta_leadgen_id', 'status', 'create_date'],
            order='id desc', limit=1000)
        # As the caller, so the snapshot rows are theirs.
        Snapshot = self.env['meta.sync.log.quarantine']
        Snapshot.search([('create_uid', '=', self.env.uid)]).unlink()
        Snapshot.create([{
            'log_ref': row['id'],
            'leadgen_id': row['meta_leadgen_id'],
            'status': row['status'],
            'logged_at': row['create_date'],
        } for row in found])
        view = self.env.ref(
            'meta_lead_ads.meta_sync_log_quarantine_view_list')
        return {
            'type': 'ir.actions.act_window',
            'name': _("Quarantined Sync Rows"),
            'res_model': 'meta.sync.log.quarantine',
            'view_mode': 'list',
            'views': [(view.id, 'list')],
            'domain': [('create_uid', '=', self.env.uid)],
            'context': {'create': False},
            'target': 'current',
        }

    @api.model
    def _erase_notification(self, count):
        return {
            'type': 'ir.actions.client',
            'tag': 'display_notification',
            'params': {
                'message': _("Personal data erased from %(n)s row(s). The "
                             "rows stay as a trace.", n=count),
                'type': 'success',
                'sticky': False,
                'next': {'type': 'ir.actions.client', 'tag': 'soft_reload'},
            },
        }

    @api.model
    def _erase_leadgen_copies(self, leadgen_ids, trigger, details=None):
        """Erase every stored copy of these Meta leads' data from the sync
        log and the webhook queue, and keep the leads from coming back.

        Only for Meta leads that never became a lead. The caller has
        checked the user's rights on the rows it started from. Works as
        sudo across every company but keeps the caller's uid. ``details``
        maps a lead id to ``page_id``, ``form_id`` and ``company_id``.

        Rows are never deleted; they stay as traces marked erased. Running
        it again on the same ids changes nothing.
        """
        ids = sorted({lg for lg in leadgen_ids if lg})
        if not ids:
            return
        details = details or {}
        Log = self.sudo()
        Event = self.env['meta.webhook.event'].sudo()
        # 1. A lead that still holds one of these ids, or that a row points
        # at, keeps the data on the lead: erasing only the copies would
        # leave it there, and the removed-lead record would cut that lead
        # off from Meta for good. Same for an id merged into a live lead.
        linked = (Log.search([('meta_leadgen_id', 'in', ids),
                              ('lead_id', '!=', False)]).lead_id
                  | Event.search([('leadgen_id', 'in', ids),
                                  ('lead_id', '!=', False)]).lead_id)
        holder = self.env['crm.lead'].sudo().with_context(
            active_test=False).search(
                ['|', ('meta_leadgen_id', 'in', ids),
                 ('id', 'in', linked.ids)], order='id', limit=1)
        if not holder:
            holder = self.env['meta.leadgen.tombstone'].sudo().search([
                ('leadgen_id', 'in', ids), ('removal_reason', '=', 'merged'),
                ('merged_into_id', '!=', False)],
                order='id', limit=1).merged_into_id
        if holder:
            # A readable row can point at a lead of a company the caller
            # can't see, so the lead's id is only named to someone who can
            # open it. sudo kept the uid, so with_user drops only sudo.
            if holder.with_user(self.env.uid).has_access('read'):
                raise UserError(_(
                    "Lead #%(id)s holds this Meta lead. Erase its personal "
                    "data from the lead instead.", id=holder.id))
            raise UserError(_(
                "A lead holds this Meta lead. Erase its personal data from "
                "the lead instead."))
        # 2. The sweeps, the webhook, Retry and Restore all stop at this
        # record, and an older one is turned into an erased one.
        self.env['meta.leadgen.tombstone'].sudo()._mark_erased(
            ids, details=details)
        # 3. The open failed row becomes a removed-lead skip, or a skip row
        # is written. This may create a row, so it runs before the clear.
        by_page = {}
        for leadgen_id in ids:
            page_id = details.get(leadgen_id, {}).get('page_id') or False
            by_page.setdefault(page_id, []).append(leadgen_id)
        Page = self.env['meta.page'].sudo()
        for page_id, group in by_page.items():
            Log._record_removed_skip(
                group, trigger, page=Page.browse(page_id) if page_id else None)
        # 4. Every row of these ids, whatever its company, the row step 3
        # may have written included.
        logs = Log.search([('meta_leadgen_id', 'in', ids)])
        events = Event.search([('leadgen_id', 'in', ids)])
        vals = {'raw_payload': False, 'payload_cleared': 'erased'}
        logs.filtered(
            lambda r: r.raw_payload or r.payload_cleared != 'erased').write(vals)
        events.filtered(
            lambda r: r.raw_payload or r.payload_cleared != 'erased').write(vals)
        # 5. Queue rows still waiting are finished without a lead.
        events._settle_removed()
        _logger.info("Meta row erase: personal data erased for Meta lead(s) "
                     "%s by uid=%s", ids, self.env.uid)

    # ------------------------------------------------------------------ #
    # Retention. Every webhook replay and cron overlap adds a
    # skipped_idempotent row, and the webhook queue keeps one row per lead
    # with the lead's data in it, so both have to be cleaned up. Failed
    # sync rows are kept as traces, but their stored Meta data expires.
    # Pending queue rows are never touched: the drain still needs them.
    # ------------------------------------------------------------------ #
    @api.model
    def _cron_vacuum_logs(self, batch=5000):
        """Daily cleanup, in this order:

        1. removed-lead records past their own fixed retention;
        2. success and skipped sync rows older than the sync log retention;
           their counts are added to the daily totals first;
        3. processed (done) queue rows older than the processed-rows
           retention, then failed queue rows older than the failed-rows
           retention, both by last change;
        4. the stored payload of failed sync rows older than the failed-rows
           retention: the row stays, so it can still be retried by id, and
           is marked as expired.

        Each step handles at most ``batch`` rows per run, so the first
        sweep on a big table doesn't run as one huge transaction; the daily
        cron catches up.

        Only one cleanup runs at a time. A run that overlaps another one,
        such as a manual run during the scheduled one, does nothing.
        """
        if not self._meta_try_vacuum_lock():
            _logger.info("Meta log cleanup: another cleanup is already "
                         "running; this run does nothing.")
            return
        # Same daily job: the removed-lead records have their own, fixed
        # retention, which the settings below don't change.
        self.env['meta.leadgen.tombstone']._vacuum(batch=batch)
        now = fields.Datetime.now()

        def cutoff(key):
            return fields.Datetime.to_string(
                now - timedelta(days=self._retention_days(key)))

        cr = self.env.cr
        # A removed-lead skip is kept while the lead is still recorded as
        # removed. The sweeps keep reading such a lead, and a pruned row
        # would be written again as if the skip were new.
        self.flush_model(['create_date', 'status', 'meta_leadgen_id',
                          'company_id', 'trigger'])
        self.env['meta.leadgen.tombstone'].flush_model(['leadgen_id'])
        cr.execute("""
            SELECT l.id FROM meta_sync_log l
             WHERE l.create_date < %s
               AND (l.status IN ('success', 'skipped_idempotent',
                                 'skipped_duplicate')
                    OR (l.status = 'skipped_removed'
                        AND NOT EXISTS (
                            SELECT 1 FROM meta_leadgen_tombstone t
                             WHERE t.leadgen_id = l.meta_leadgen_id)))
             LIMIT %s
             FOR UPDATE OF l SKIP LOCKED
        """, (cutoff(const.SYNC_LOG_RETENTION_PARAM), batch))
        ids = [row[0] for row in cr.fetchall()]
        if ids:
            # The counts go into the daily totals first, in the same
            # transaction, so the dashboard totals don't change when the
            # rows go. The lock keeps an overlapping run from counting the
            # same rows again. Rows deleted before this table existed are
            # gone, and their counts can't be rebuilt.
            cr.execute("""
                INSERT INTO meta_sync_stats
                       (day, company_id, trigger, status, row_count)
                SELECT l.create_date::date, l.company_id, l.trigger,
                       l.status, count(*)
                  FROM meta_sync_log l
                 WHERE l.id = ANY(%s)
                 GROUP BY 1, 2, 3, 4
            """, (ids,))
            self.env['meta.sync.stats'].invalidate_model()
            self.browse(ids).unlink()

        # Queue rows by last change, so a failed row retried today isn't
        # removed straight away. No removed-lead check is needed here:
        # leadgen_id is unique on the queue, so if Meta sends a pruned lead
        # again it comes back as a new pending row, and the drain settles
        # it from the removed-lead record or the unique lead id.
        Event = self.env['meta.webhook.event'].sudo()
        Event.flush_model(['status', 'write_date'])
        cr.execute("""
            SELECT id FROM meta_webhook_event
             WHERE status = 'done' AND write_date < %s
             LIMIT %s
        """, (cutoff(const.QUEUE_DONE_RETENTION_PARAM), batch))
        done = Event.browse([row[0] for row in cr.fetchall()])
        if done:
            done.unlink()
        failed_cutoff = cutoff(const.FAILED_RETENTION_PARAM)
        cr.execute("""
            SELECT id FROM meta_webhook_event
             WHERE status = 'failed' AND write_date < %s
             LIMIT %s
        """, (failed_cutoff, batch))
        failed_events = Event.browse([row[0] for row in cr.fetchall()])
        if failed_events:
            failed_events.unlink()

        # Failed sync rows stay as traces, with ids, status and error text,
        # but the copy of the lead's answers goes. Rows erased on request
        # have no payload left and aren't picked up again.
        self.flush_model(['status', 'raw_payload', 'create_date'])
        cr.execute("""
            SELECT id FROM meta_sync_log
             WHERE status = 'failed' AND raw_payload IS NOT NULL
               AND create_date < %s
             LIMIT %s
        """, (failed_cutoff, batch))
        expired = self.sudo().browse([row[0] for row in cr.fetchall()])
        if expired:
            expired.write({'raw_payload': False, 'payload_cleared': 'expired'})

    @api.model
    def _meta_try_vacuum_lock(self):
        """Take the cleanup's lock until this transaction ends. False when
        another transaction holds it."""
        self.env.cr.execute(
            "SELECT pg_try_advisory_xact_lock(hashtext(%s)::bigint)",
            (VACUUM_LOCK_KEY,))
        return self.env.cr.fetchone()[0]

    @api.model
    def _retention_days(self, key):
        """Days to keep for retention parameter ``key``, from 1 to
        RETENTION_MAX_DAYS.

        A missing or empty value means the default. 0, a negative number, a
        number above the maximum or anything that isn't a whole number also
        falls back to the default, with a warning, so a bad value can never
        mean "keep forever" or stop the cleanup.
        """
        default = const.RETENTION_DEFAULTS[key]
        raw = self.env['ir.config_parameter'].sudo().get_param(key)
        if raw in (None, False, ''):
            return default
        try:
            value = int(raw)
        except (TypeError, ValueError):
            value = None
        if value is None or not 1 <= value <= const.RETENTION_MAX_DAYS:
            # Only the key and the stored value, which is a setting, never
            # lead data.
            _logger.warning(
                "meta_lead_ads: %s is %r, which is not a valid number of "
                "days; using %s", key, raw, default)
            return default
        return value

    @api.model
    def _sync_log_retention_days(self):
        """Days to keep success and skipped rows (default 14, minimum 1)."""
        return self._retention_days(const.SYNC_LOG_RETENTION_PARAM)

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
        # Checked here as well as by the write ACL: the company-less step
        # below writes as sudo, and RPC reaches this method directly.
        is_admin = self.env.user.has_group('meta_lead_ads.group_meta_admin')
        if not self.env.su and not is_admin:
            raise AccessError(_("Meta Admin access required."))
        self.check_access('read')
        # A retry would fetch the erased data from Meta again. Refused as a
        # whole, before anything changes or Meta is called.
        if self.filtered(lambda r: r.payload_cleared == 'erased'):
            raise UserError(_("The personal data of this Meta lead was "
                              "erased, so it can't be retried."))
        failed = self.filtered(lambda r: r.status == 'failed')
        if not failed:
            raise UserError(_("Select at least one failed sync to retry."))
        busy_rows = self.browse()
        # A row without a company is shared by every company, so it is only
        # handed to a caller who can read the Page it resolves to. Writing
        # that Page gives the row the Page's company before anything else
        # changes it.
        if not self.env.su:
            refused = _(
                "Sync rows that belong to no company can't be retried here.")
            Event = self.env['meta.webhook.event']
            for row in failed.filtered(lambda r: not r.company_id):
                page = row.sudo()._resolve_page()
                if not page or not page.with_env(self.env).has_access('read'):
                    raise UserError(refused)
                # Same locks, in the same order, as the retry itself takes
                # next, so this write never waits on a row someone else is
                # working on.
                leadgen_id = row.sudo().meta_leadgen_id
                if ((leadgen_id
                     and Event._lock_leadgen_event(leadgen_id) is None)
                        or not row._lock_for_retry()):
                    busy_rows |= row
                    continue
                row.sudo().write({'page_id': page.id})
                if not row.sudo().company_id:
                    raise UserError(refused)
            failed -= busy_rows
        # A row follows its lead, so its Page may have been moved to a
        # company the caller can't access since. Ingest would refuse it.
        if not self.env.su:
            for row in failed:
                page = row.sudo()._resolve_page()
                if page and not page.with_env(self.env).has_access('read'):
                    raise UserError(_(
                        "The Page of a selected sync row now sends leads to "
                        "a company you can't access, so it can't be retried "
                        "here."))
        succeeded = 0
        still_failed = len(busy_rows)
        scheduled = 0
        removed = 0
        busy = len(busy_rows)
        held_back = 0
        archived = 0
        left = 0
        # Shared by the whole click: once Meta asks to wait, the remaining
        # rows don't call it again for that Page, or for any Page.
        held = {}
        sent = 0
        deadline = _monotonic() + const.RETRY_TIME_BUDGET
        for row in failed:
            if sent >= const.RETRY_BATCH_LIMIT or _monotonic() >= deadline:
                left += 1
                continue
            try:
                with self.env.cr.savepoint():
                    outcome = row._retry_one(held=held)
            except Exception as e:
                # The text of an unexpected error can carry Meta's reply or
                # lead data, so only its class is logged. The savepoint
                # cleared the cache, so retry_count is re-read from the DB
                # and the +1 counts this attempt once.
                _logger.error(
                    "Meta sync retry: unexpected error on log id=%s (%s) "
                    "(no token/payload logged)", row.id, type(e).__name__)
                sent += 1
                # The rollback also released the locks. Take them again in
                # the usual order, and leave a row someone else has taken
                # meanwhile to them.
                leadgen_id = row.sudo().meta_leadgen_id
                if ((leadgen_id and self.env['meta.webhook.event']
                        ._lock_leadgen_event(leadgen_id) is None)
                        or not row._lock_for_retry()):
                    still_failed += 1
                    busy += 1
                    continue
                # The rollback undid any skip the retry had recorded, so a
                # deleted lead is settled again here rather than failed.
                if (leadgen_id and self.env['meta.leadgen.tombstone']
                        ._removed([leadgen_id])):
                    row._settle_removed(page=row.sudo()._resolve_page())
                    removed += 1
                    continue
                row.write({'status': 'failed',
                           'error_message': 'Unexpected retry error',
                           'retry_count': row.retry_count + 1})
                still_failed += 1
                continue
            # Anything but 'ok' or 'scheduled' counts as still failed; the
            # row's error_message says why.
            if outcome == 'ok':
                succeeded += 1
            elif outcome == 'scheduled':
                scheduled += 1
            elif outcome == 'removed':
                removed += 1
            else:
                still_failed += 1
                # Busy and held rows get no new error message, so the
                # notification says why they were skipped.
                if outcome == 'busy':
                    busy += 1
                elif outcome == 'held':
                    held_back += 1
                elif outcome == 'archived':
                    archived += 1
            if outcome in ('ok', 'transient', 'failed'):
                sent += 1
        if scheduled:
            self._trigger_backfill_once()
        return self._retry_notification(succeeded, still_failed,
                                        scheduled=scheduled, busy=busy,
                                        held=held_back, left=left,
                                        removed=removed, archived=archived)

    @api.model
    def _trigger_backfill_once(self):
        """Ask for one backfill sweep, unless one is already due.

        A sweep covers every form, and each extra one spends Meta quota
        that every form shares, so repeated clicks on a row that stays
        failed don't stack sweeps up.
        """
        cron = self.sudo().env.ref('meta_lead_ads.cron_meta_backfill')
        due = self.env['ir.cron.trigger'].sudo().search_count([
            ('cron_id', '=', cron.id),
            ('call_at', '<=', fields.Datetime.now())], limit=1)
        if not due:
            cron._trigger()

    def _lock_for_retry(self):
        """Lock this row with NOWAIT. Return True if locked, False if another
        retry already holds it.

        The lock runs in its own savepoint so a failed attempt leaves the
        cursor usable for writing the error message. NOWAIT stops two
        retries of the same row (two admins, or form button plus bulk action)
        from overlapping.

        At REPEATABLE READ a row changed by a transaction that committed
        after ours started can't be locked either. For a retry that is the
        same as busy.
        """
        self.ensure_one()
        try:
            with self.env.cr.savepoint():
                self.env.cr.execute(
                    "SELECT id FROM meta_sync_log WHERE id = %s "
                    "FOR UPDATE NOWAIT", (self.id,), log_exceptions=False)
            return True
        except (LockNotAvailable, SerializationFailure):
            return False
        except Exception as e:   # pragma: no cover - psycopg2 variant fallback
            # Some psycopg2 builds raise a plain OperationalError with the
            # pgcode instead.
            if getattr(e, 'pgcode', None) in _BUSY_CODES:
                return False
            raise

    def _retry_one(self, held=None):
        """Retry one failed row through ingest_leadgen.

        Both Retry buttons (sync log and queue) run through here, so the
        queue and the log always agree on what a retry did.

        Locks the lead's queue event first and this row second, both with
        NOWAIT, then re-reads the row: something may have finished it while
        we were getting there.

        ``held`` is shared by all rows of one click (see _lead_reads_held).
        A row whose Page or app Meta has asked to wait is left untouched.

        Returns 'ok', 'transient' or 'failed'; 'scheduled' for a row about
        a whole form or Page, whose backfill the caller then triggers;
        'held' when Meta has asked to wait; 'archived' when the lead's Page
        is archived in Odoo; 'removed' when the lead was deleted in Odoo,
        after which the row is skipped_removed; or 'busy' when another
        transaction holds the event or the row.
        Error messages carry no page id, token or payload.
        """
        self.ensure_one()
        if held is None:
            held = {}
        Event = self.env['meta.webhook.event']
        event = Event.browse()
        # Nothing is written on 'busy': the holder owns the row, and an
        # UPDATE here would wait for it.
        if self.meta_leadgen_id:
            event = Event._lock_leadgen_event(self.meta_leadgen_id)
            if event is None:
                return 'busy'
            event = event.sudo()
        if not self._lock_for_retry():
            return 'busy'
        self.invalidate_recordset()
        if self.status != 'failed':
            if self.status in ('success', 'skipped_idempotent'):
                return 'ok'
            if self.status == 'skipped_removed':
                return 'removed'
            return 'busy'
        page = self._resolve_page()
        # The webhook drain, the form sweep and both Retry paths hold the
        # leads of an archived Page, so nothing is counted, fetched or
        # scheduled here until it is active again. An admin can still bring
        # one in on purpose with Ingest by ID or Restore.
        if page and not page.active:
            # A lead deleted in Odoo still settles as removed.
            Tombstone = self.env['meta.leadgen.tombstone']
            if (self.meta_leadgen_id
                    and Tombstone._removed([self.meta_leadgen_id])):
                self._settle_removed(page=page, event=event)
                return 'removed'
            self.write({'error_message': _PAGE_ARCHIVED_MESSAGE})
            if event and event.status != 'done':
                event.write({'error_message': _PAGE_ARCHIVED_MESSAGE})
            return 'archived'
        # Calling Meta now would only extend its block. A form sweep would
        # hit the same throttle, so it waits too.
        if self._lead_reads_held(page, held):
            return 'held'
        # Count the attempt here and nowhere else.
        self.retry_count += 1
        if not self.meta_leadgen_id and (self.form_id or self.page_id):
            return self._schedule_backfill()
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
        except MetaRateLimitError as e:
            # Same wait and pause as the drain, so a click can't keep
            # hitting a throttled Page and the queued event waits too.
            until = Event._rate_limit_until(e, fields.Datetime.now())
            # Noted for the rest of the click as well, in case the pause
            # could not be saved.
            if Event._rate_limit_is_page_scoped(e):
                page.sudo()._pause_lead_fetch(until)
                held.setdefault('pages', set()).add(page.id)
            else:
                Event._pause_app_lead_fetch(until)
                held['app'] = True
            message = ("Meta rate limit reached; next try after %s UTC"
                       % fields.Datetime.to_string(until))
            self.write({'error_message': message})
            self._requeue_event(event, message, next_attempt_at=until)
            return 'transient'
        except MetaTransientError as e:
            # The exception text is already redacted.
            self.write({'error_message': str(e)})
            self._requeue_event(event, str(e))
            return 'transient'
        except MetaLeadRemovedError:
            # Deleted in Odoo: nothing went wrong, so the row and its queue
            # entry are finished rather than failed.
            self._settle_removed(page=page, event=event)
            return 'removed'
        except (MetaPermanentError, MetaAuthError) as e:
            self.write({'error_message': str(e)})
            if event and event.status != 'done':
                event.write({'error_message': str(e)})
            return 'failed'
        # Never count a retry as done without a lead to show for it.
        if len(lead) != 1:
            self.write({'error_message': 'No lead returned; retry later'})
            return 'transient'
        self._reconcile_after_ingest(lead)
        return 'ok'

    def _settle_removed(self, page=None, event=None):
        """Mark this row and its queue entry as skipped because the lead was
        deleted in Odoo. The caller holds the event lock, or none exists."""
        self.ensure_one()
        leadgen_id = self.sudo().meta_leadgen_id
        if event is None:
            event = self.env['meta.webhook.event'].sudo().search(
                [('leadgen_id', '=', leadgen_id)], limit=1)
        self._record_removed_skip([leadgen_id], self.sudo().trigger,
                                  page=page)
        event._settle_removed()

    @api.model
    def _lead_reads_held(self, page, held):
        """True while Meta has asked to pause lead reads for ``page`` or for
        the whole app.

        ``held`` is a dict shared by one Retry click. It remembers the
        throttles met during the click ('app', and 'pages' as a set of
        ids), so later rows stay back even if a pause was not saved. A
        Meta Admin ends a pause on the Page, in Settings or with Retry on a
        waiting queue event.
        """
        if held.get('app'):
            return True
        pages = held.setdefault('pages', set())
        if page and page.id in pages:
            return True
        now = fields.Datetime.now()
        app_until = self.env['meta.webhook.event']._app_lead_fetch_paused_until()
        if app_until and app_until > now:
            held['app'] = True
            return True
        if page:
            until = page.sudo().lead_fetch_paused_until
            if until and until > now:
                pages.add(page.id)
                return True
        return False

    @api.model
    def _requeue_event(self, event, message, next_attempt_at=False):
        """Hand the lead back to the drain after a retry that may work later.

        Attempts start again from zero, since an admin asked for this try.
        A done event already has its lead and is left alone.
        """
        if event and event.status != 'done':
            event.write({'status': 'pending', 'attempts': 0,
                         'next_attempt_at': next_attempt_at,
                         'error_message': message})

    def _schedule_backfill(self):
        """Check that the backfill would sweep this row's form (or, for an
        older row that only knows its Page, one of the Page's forms).

        There is no single lead to fetch, and a full sweep can take many
        Graph calls, so it runs in the cron rather than in the click.
        Refusing when the sweep would not run keeps the button from
        promising a recovery that can't happen. Returns 'scheduled' or
        'failed'.
        """
        self.ensure_one()
        if self.form_id:
            form = self.form_id
            if not (form.active and form.sync_enabled):
                self.write({'error_message': 'Backfill is off for this form; '
                                             'turn sync on to retry it'})
                return 'failed'
        else:
            # search() leaves archived forms out.
            runnable = self.env['meta.lead.form'].search_count([
                ('page_id', '=', self.page_id.id),
                ('sync_enabled', '=', True)])
            if not runnable:
                self.write({'error_message':
                            'This Page has no form with sync turned on'})
                return 'failed'
        cron = self.sudo().env.ref('meta_lead_ads.cron_meta_backfill',
                                   raise_if_not_found=False)
        if not cron:
            self.write({'error_message': 'Backfill job not found'})
            return 'failed'
        if not cron.active:
            self.write({'error_message': 'The backfill job is turned off; '
                                         'turn it on in Scheduled Actions'})
            return 'failed'
        if self.form_id:
            message = ('Backfill scheduled; this row clears when the form '
                       'syncs cleanly')
        else:
            # Nothing ties this row to a lead, so no sweep can tell when it
            # is resolved.
            message = ('Backfill scheduled; this older row has no Meta lead '
                       'id, so delete it once the Page leads are checked')
        self.write({'error_message': message})
        return 'scheduled'

    def _reconcile_after_ingest(self, lead):
        """Merge the log row the retry just created into this one, so the
        lead keeps a single row.

        The new row is found by retry_origin_log_id, leadgen id and a success
        or skipped_idempotent status, not by create_date, so an older manual
        row or a parallel retry can't be merged by mistake. Ingest never
        writes skipped_duplicate for a retry, so it isn't matched.
        """
        self.ensure_one()
        # A row is never marked successful without a lead.
        if not lead:
            return
        # The fresh row follows its lead and may sit in a company the caller
        # has not selected. The values copied below come from ingest, not
        # from the caller, so they are written as sudo too.
        dups = self.sudo().search([
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
            self.sudo().write({
                'status': dup.status,
                'lead_id': dup.lead_id.id,
                'match_key': dup.match_key,
                'match_candidates': dup.match_candidates,
                'match_candidates_truncated': dup.match_candidates_truncated,
                'match_other_ids': dup.match_other_ids,
                'error_message': False,
                'trigger': 'manual',
                'raw_payload': dup.raw_payload,
                'page_id': dup.page_id.id,
            })
            dup.unlink()
        else:
            # Ingest returned a lead but wrote no matching row. Mark this row
            # done from the returned lead and log a warning.
            self.sudo().write({
                'status': 'success',
                'lead_id': lead.id if lead else False,
                'trigger': 'manual',
                'error_message': False,
            })
            _logger.warning(
                "Meta sync retry: ingestion returned a lead with no correlatable "
                "log row for log id=%s (no token/payload logged)", self.id)
        # The retry holds the event lock by now. Read through sudo: the row
        # follows its lead and may now sit in a company the caller can't
        # read.
        self._reconcile_leadgen(self.sudo().meta_leadgen_id, lead, keep=self,
                                event_locked=True)

    @api.model
    def _reconcile_leadgen(self, leadgen_id, lead, keep=None,
                           event_locked=False):
        """Close what is still open for a Meta lead that has arrived.

        A lead that finally arrived keeps no failed sync-log row (other
        than ``keep``, the row being retried) and its queue entry shows
        done. Success and skipped rows stay as history.

        Locks the event first, then the failed rows, the same order as
        every other recovery path. Both with NOWAIT, so this never waits:
        when something is busy nothing is changed and False is returned,
        and the drain's healing step picks it up later. Pass
        ``event_locked`` when the caller already holds the event row.

        Rows can sit in another company of the admin, so this runs as sudo
        with values from ingest only.
        """
        if not leadgen_id or not lead:
            return False
        Event = self.env['meta.webhook.event'].sudo()
        if event_locked:
            event = Event.search([('leadgen_id', '=', leadgen_id)], limit=1)
        else:
            event = Event._lock_leadgen_event(leadgen_id)
            if event is None:
                return False
        self.flush_model(['meta_leadgen_id', 'status'])
        cr = self.env.cr
        try:
            with cr.savepoint():
                cr.execute(
                    "SELECT id FROM meta_sync_log "
                    "WHERE meta_leadgen_id = %s AND status = 'failed' "
                    "AND id != %s FOR UPDATE NOWAIT",
                    (leadgen_id, keep.id if keep else 0),
                    log_exceptions=False)
                stale_ids = [r[0] for r in cr.fetchall()]
        except (LockNotAvailable, SerializationFailure):
            return False
        except PsycopgError as e:
            if getattr(e, 'pgcode', None) in _BUSY_CODES:
                return False
            raise
        if stale_ids:
            self.sudo().browse(stale_ids).unlink()
        if event and event.status in ('pending', 'failed'):
            event.write({'status': 'done', 'lead_id': lead.id,
                         'next_attempt_at': False, 'error_message': False})
        return True

    @api.model
    def _heal_resolved_failures(self, limit=100):
        """Close failed rows and failed queue entries whose lead now exists.

        The locks can't order every interleaving: a failure row committed
        by one transaction after another one created the lead stays open
        until something looks again. The drain calls this at the start of
        each run. It is bounded because it runs every minute, and it makes
        no Graph call. Returns how many leads were reconciled.
        """
        self.env['crm.lead'].flush_model(['meta_leadgen_id'])
        self.flush_model(['meta_leadgen_id', 'status', 'lead_id'])
        self.env['meta.webhook.event'].flush_model(['leadgen_id', 'status'])
        cr = self.env.cr
        cr.execute("""
            SELECT lg, lead_id FROM (
                SELECT c.lg, COALESCE(
                    (SELECT l.id FROM crm_lead l
                      WHERE l.meta_leadgen_id = c.lg LIMIT 1),
                    (SELECT s.lead_id FROM meta_sync_log s
                      WHERE s.meta_leadgen_id = c.lg
                        AND s.status != 'failed'
                        AND s.lead_id IS NOT NULL
                      ORDER BY s.id DESC LIMIT 1)) AS lead_id
                  FROM (SELECT meta_leadgen_id AS lg FROM meta_sync_log
                         WHERE status = 'failed'
                           AND meta_leadgen_id IS NOT NULL
                        UNION
                        SELECT leadgen_id FROM meta_webhook_event
                         WHERE status = 'failed') c
            ) known
             WHERE lead_id IS NOT NULL
             ORDER BY lg
             LIMIT %s
        """, (limit,))
        pairs = cr.fetchall()
        Lead = self.env['crm.lead'].sudo()
        healed = 0
        for leadgen_id, lead_id in pairs:
            if self._reconcile_leadgen(leadgen_id, Lead.browse(lead_id)):
                healed += 1
        return healed

    def _resolve_page(self):
        """Find the meta.page to re-fetch from; may return an empty recordset.

        Tries page_id on this row, then the Page of the row's form, the
        lead's page, the lead's form's page, a search on the lead's raw
        meta_page_id (unique across accounts), and finally the Meta page id
        of the queued webhook event for this lead. Archived Pages count, so
        a held row still finds its Page. Keep the page id and name out of
        any message.
        """
        self.ensure_one()
        if self.page_id:
            return self.page_id
        # Only reached when the row has no Page; the link check keeps the
        # two consistent for non-superuser writers.
        if self.form_id.page_id:
            return self.form_id.page_id
        lead = self.lead_id
        if lead:
            if lead.meta_page_id_ref:
                return lead.meta_page_id_ref
            if lead.meta_form_id_ref and lead.meta_form_id_ref.page_id:
                return lead.meta_form_id_ref.page_id
            if lead.meta_page_id:
                return self.env['meta.page'].with_context(
                    active_test=False).search(
                        [('page_id', '=', lead.meta_page_id)], limit=1)
        if self.meta_leadgen_id:
            # A lead from a Page that wasn't connected yet. The event is read
            # as sudo only for its Meta page id; the Page itself is searched
            # as the caller, so company rules still apply.
            event = self.env['meta.webhook.event'].sudo().search(
                [('leadgen_id', '=', self.meta_leadgen_id)], limit=1)
            if event.page_id:
                return self.env['meta.page'].with_context(
                    active_test=False).search(
                        [('page_id', '=', event.page_id)], limit=1)
        return self.env['meta.page']

    def _retry_notification(self, succeeded, still_failed, scheduled=0,
                            busy=0, held=0, left=0, removed=0, archived=0):
        """Notification with the retry counts only. ``busy``, ``held`` and
        ``archived`` rows are also counted in ``still_failed``; ``left``
        rows, which the click did not get to, are not. ``removed`` rows,
        deleted in Odoo, count in the total but neither as succeeded nor as
        failed."""
        total = succeeded + still_failed + scheduled + removed
        if scheduled:
            message = _(
                "%(n)s retried: %(ok)s succeeded, %(sched)s scheduled to run "
                "shortly, %(bad)s still failed",
                n=total, ok=succeeded, sched=scheduled, bad=still_failed)
        else:
            message = _(
                "%(n)s retried: %(ok)s succeeded, %(bad)s still failed",
                n=total, ok=succeeded, bad=still_failed)
        notes = []
        if busy:
            notes.append(_(
                "%(busy)s of them were already being retried.", busy=busy))
        if held:
            notes.append(_(
                "%(held)s of them were held back because Meta asked to pause "
                "lead reads; resume lead reads on the Page or in Settings, "
                "or try again later.", held=held))
        if archived:
            notes.append(_(
                "%(archived)s of them belong to an archived Page; reactivate "
                "the Page, then retry.", archived=archived))
        if left:
            notes.append(_(
                "%(left)s more were not retried this time; retry them again.",
                left=left))
        if removed:
            notes.append(_(
                "%(removed)s of them were deleted in Odoo and were skipped; "
                "restore them from CRM > Configuration > Removed Meta Leads "
                "if they should come back.",
                removed=removed))
        if notes:
            message = "%s. %s" % (message, " ".join(notes))
        return {
            'type': 'ir.actions.client',
            'tag': 'display_notification',
            'params': {
                'message': message,
                'type': 'success' if not (still_failed or left) else 'warning',
                'sticky': False,
            },
        }
