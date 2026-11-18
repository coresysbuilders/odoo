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
import math
import time
from datetime import timedelta

from psycopg2 import IntegrityError, errorcodes
from psycopg2.errors import LockNotAvailable, SerializationFailure

from odoo import _, api, fields, models
from odoo.exceptions import AccessError, UserError, ValidationError

# The retry limits are read through const at call time so tests can patch
# them.
from . import const
from . import meta_sync_log
from .const import (RATE_LIMIT_FALLBACK_MINUTES, RATE_LIMIT_MAX_MINUTES,
                    RATE_LIMIT_PAGE_CODES)
from .exceptions import (
    MetaTransientError, MetaPermanentError, MetaAuthError, MetaRateLimitError,
    MetaIngestRaceError, MetaLeadRemovedError)
from .meta_sync_log import (_PAGE_ARCHIVED_MESSAGE, _PAGE_REQUEUED_MESSAGE,
                            _REMOVED_MESSAGE)

_logger = logging.getLogger(__name__)
# Module-level so tests can patch the clock of the retry time budget.
_monotonic = time.monotonic
MAX_ATTEMPTS = 5          # give up on transient errors after this many tries
# Set when Meta throttles lead reads for the whole app.
APP_PAUSE_PARAM = 'meta_lead_ads.lead_fetch_paused_until'


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
    # Why raw_payload is empty when it was once set; same as on the sync log.
    payload_cleared = fields.Selection(
        [('erased', 'Erased'), ('expired', 'Payload expired')],
        string='Payload', readonly=True, copy=False)
    status = fields.Selection(
        [('pending', 'Pending'), ('done', 'Done'), ('failed', 'Failed')],
        default='pending', required=True, index=True)
    attempts = fields.Integer(default=0)
    error_message = fields.Text()              # never include a token
    # Set when Meta asks us to slow down; the drain skips the event until
    # then. No default, so upgrading does not rewrite the table.
    next_attempt_at = fields.Datetime(string='Next Attempt', copy=False,
                                      index=True)
    # Set when reactivating an archived Page puts this lead back in line.
    # That is an admin's action, not a new delivery, so the drain keeps the
    # row's failure time and leaves the webhook stamp alone. No default, so
    # upgrading does not rewrite the table.
    requeued = fields.Boolean(string='Re-queued', copy=False, readonly=True)
    # When it was put back. The drain takes fresh deliveries first, and the
    # backlog count ages a re-queued lead from here rather than from when
    # Meta first sent it. No default, for the same reason as above.
    requeued_at = fields.Datetime(string='Re-queued On', copy=False,
                                  readonly=True)
    # Indexed: company_id below depends on it, so a company change on any
    # crm.lead looks events up by lead.
    lead_id = fields.Many2one('crm.lead', ondelete='set null',
                              index='btree_not_null')
    # Company of the Page this event was routed to, refreshed on each drain
    # attempt.
    page_company_id = fields.Many2one('res.company', string='Page Company',
                                      index=True)
    # The lead's company once linked, else the Page's. Blank only for events
    # from an unknown Page, which hold Meta ids and nothing else.
    company_id = fields.Many2one(
        'res.company', string='Company', compute='_compute_company_id',
        store=True, index=True)

    # One queue row per leadgen_id. Meta lead ids are globally unique, so this
    # is not keyed per page. The UNIQUE on crm.lead.meta_leadgen_id is still
    # what prevents duplicate leads. Odoo 19 uses models.Constraint instead of
    # _sql_constraints.
    _leadgen_id_uniq = models.Constraint(
        'unique(leadgen_id)',
        'A webhook event for this lead is already queued.',
    )

    # The ORM does not enforce readonly on write, so without a guard an RPC
    # call could move an event into another company's queue. Only the
    # controller and the drain set these, both as sudo.
    _derived_company_fields = ('company_id', 'page_company_id')
    # Only the erase code may set these, or a user could make an event look
    # erased while it still holds data.
    _system_set_fields = ('payload_cleared',)
    # Only the re-queue sets these, or a user could hide a real delivery from
    # the webhook health or a stuck lead from the backlog count.
    _requeue_fields = ('requeued', 'requeued_at')

    def init(self):
        # Serves the daily cleanup of processed and failed rows in
        # meta.sync.log._cron_vacuum_logs. Partial, so pending rows, which
        # are never cleaned up, stay out of it. Plain SQL rather than
        # models.Index so the Odoo 18 and 19 code stay the same.
        self.env.cr.execute("""
            CREATE INDEX IF NOT EXISTS meta_webhook_event_retention_idx
                ON meta_webhook_event (status, write_date)
             WHERE status IN ('done', 'failed')
        """)

    @api.depends('lead_id.company_id', 'page_company_id')
    def _compute_company_id(self):
        for event in self:
            event.company_id = event.lead_id.company_id or event.page_company_id

    @api.model_create_multi
    def create(self, vals_list):
        self._check_derived_company_vals(vals_list)
        records = super().create(vals_list)
        if not self.env.su:
            records._check_link_access()
        return records

    def write(self, vals):
        self._check_derived_company_vals([vals])
        if not self.env.su and 'leadgen_id' in vals:
            # A retry fetches this lead and updates its failure row, which
            # may belong to another company, so it stays what Meta sent.
            raise AccessError(_(
                "The Meta lead ID of a queued event can't be changed."))
        res = super().write(vals)
        if not self.env.su and ('lead_id' in vals or 'page_id' in vals):
            self._check_link_access()
        return res

    def _check_link_access(self):
        # The lead sets the company at once and the Meta page id sets it on
        # the next drain, which also ingests through that Page. Both must be
        # something the writer can already see.
        Lead = self.env['crm.lead']
        Page = self.env['meta.page'].with_context(active_test=False)
        for rec in self.sudo():
            if rec.lead_id and not Lead.browse(rec.lead_id.id).has_access('read'):
                raise ValidationError(_(
                    "You can only link a lead or Page you can access."))
            if rec.page_id and not Page.search_count(
                    [('page_id', '=', rec.page_id)], limit=1):
                raise ValidationError(_(
                    "You can only link a lead or Page you can access."))

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
            if any(name in vals for name in self._requeue_fields):
                raise AccessError(_(
                    "The re-queue marker on this record is set automatically "
                    "and can't be changed directly."))

    @api.model
    def _ingest_payload(self, data, raw=None):
        """Queue a pending row for each leadgen change in a verified payload,
        skipping lead ids already queued. Does not create leads.
        """
        if (data or {}).get('object') != 'page':
            return
        # An archived Page still gives the event its company.
        Page = self.env['meta.page'].with_context(active_test=False)
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
                page_id = v.get('page_id') or page_id_fallback
                # One indexed lookup, no Graph call: this runs while Meta
                # waits for the response.
                page_company = Page.search(
                    [('page_id', '=', page_id)], limit=1).lead_company_id
                try:
                    with self.env.cr.savepoint():
                        self.create({
                            'leadgen_id': leadgen_id,
                            'page_id': page_id,
                            'page_company_id': page_company.id or False,
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
        # Record that the drain ran, so the account form can show a stalled
        # cron instead of sync quietly stopping. Stamped before the pause
        # check: a paused run still shows the scheduler works.
        self.env['meta.account']._stamp_cron_run(
            const.DRAIN_RUN_PARAM,
            min_gap=timedelta(minutes=const.DRAIN_STAMP_MINUTES))
        # Closes failure rows that another transaction wrote after the lead
        # had already been created. It makes no Graph call, so it runs even
        # while Meta is throttling.
        try:
            with self.env.cr.savepoint():
                self.env['meta.sync.log']._heal_resolved_failures()
        except Exception as e:
            _logger.warning("meta_webhook drain: could not close resolved "
                            "failures (%s)", type(e).__name__)
        # Puts back leads that still say their Page is archived although it
        # is active again: one that was busy when the Page was reactivated,
        # or one a Sync Log Retry refused meanwhile. Only that message
        # matches, so a lead that failed for a real reason is never re-queued
        # over and over. No Graph call either, and this run takes the leads,
        # so no extra run is asked for.
        try:
            with self.env.cr.savepoint():
                stale = self.env['meta.sync.log'].sudo().search([
                    ('status', '=', 'failed'), ('trigger', '=', 'webhook'),
                    ('meta_leadgen_id', '!=', False),
                    ('error_message', '=', _PAGE_ARCHIVED_MESSAGE),
                    ('payload_cleared', '!=', 'erased'),
                    ('page_id.active', '=', True)], order='id', limit=200)
                pages = stale.page_id.filtered('active')
                if pages:
                    self._requeue_archived_holds(pages, held_only=True,
                                                 trigger=False)
        except Exception as e:
            _logger.warning("meta_webhook drain: could not put back leads of "
                            "reactivated Pages (%s)", type(e).__name__)
        # now comes from Python because SQL now() is the transaction start.
        now = fields.Datetime.now()
        app_paused = self._app_lead_fetch_paused_until()
        if app_paused and app_paused > now:
            _logger.info("meta_webhook drain: Meta lead reads paused until "
                         "%s UTC; nothing fetched this run",
                         fields.Datetime.to_string(app_paused))
            return
        # The Page check also holds back events queued after the throttle,
        # which have no wait of their own. Leads put back after a Page was
        # reactivated go after fresh deliveries, so a large re-queue can't
        # hold up real-time leads of other Pages; among themselves they keep
        # their order.
        self.env.cr.execute(
            "SELECT e.id FROM meta_webhook_event e "
            "WHERE e.status = 'pending' "
            "AND (e.next_attempt_at IS NULL OR e.next_attempt_at <= %s) "
            "AND NOT EXISTS (SELECT 1 FROM meta_page p "
            "WHERE p.page_id = e.page_id "
            "AND p.lead_fetch_paused_until > %s) "
            "ORDER BY (e.requeued_at IS NOT NULL), e.create_date ASC "
            "LIMIT %s "
            "FOR UPDATE OF e SKIP LOCKED", (now, now, limit))
        ids = [r[0] for r in self.env.cr.fetchall()]
        # Name reads are throttled separately from lead reads, so after a
        # refused name lookup the rest of the run skips them for that Page
        # and its leads arrive flagged instead.
        lookup_states = {}
        # Meta page ids throttled during this run.
        paused_pages = set()
        # Whether any event was finished this run, for the webhook stamp.
        processed = False
        Page = self.env['meta.page'].with_context(active_test=False)
        # A run killed at the cron time limit rolls back every event it
        # finished, and the next run would pick the same ones again.
        deadline = _monotonic() + const.DRAIN_TIME_BUDGET
        for event in self.browse(ids):
            if _monotonic() >= deadline:
                _logger.info("meta_webhook drain: time budget used; the "
                             "remaining events wait for the next run")
                break
            if event.page_id in paused_pages:
                continue
            scope = None
            try:
                with self.env.cr.savepoint():
                    scope = event._process_one(lookup_states=lookup_states,
                                               deadline=deadline)
            except Exception:
                # Expected Meta errors are handled in _process_one. Anything
                # else fails just this event and the loop carries on.
                _logger.exception(
                    "meta_webhook drain: unexpected error on event id=%s "
                    "(no payload/secret logged)", event.id)
                # The Page gives the row its company, so only that company
                # sees it and its admins can retry it. Archived Pages count.
                page = Page.search(
                    [('page_id', '=', event.page_id)], limit=1)
                if self.env['meta.leadgen.tombstone']._removed(
                        [event.leadgen_id]):
                    # Deleted in Odoo, so there is nothing to fail: whatever
                    # went wrong, the lead must not come back.
                    event._settle_removed()
                    self.env['meta.sync.log']._record_removed_skip(
                        [event.leadgen_id], 'webhook', page=page)
                    # A re-queued lead was not delivered by Meta, so it is
                    # no evidence that the webhook works.
                    if not event.requeued:
                        processed = True
                    continue
                message = (_PAGE_ARCHIVED_MESSAGE if page and not page.active
                           else 'Unexpected drain error')
                event.write({'status': 'failed', 'error_message': message})
                self.env['meta.sync.log']._record_failure(
                    event.leadgen_id, 'webhook', message, page=page,
                    failed_at=event._requeue_failed_at())
            # Same for a re-queued lead that went through.
            if event.status == 'done' and not event.requeued:
                processed = True
            if scope == 'page':
                # Other Pages have their own limit and keep flowing.
                paused_pages.add(event.page_id)
            elif scope == 'app':
                # Further calls in this run would only hit the same throttle
                # and push Meta's block out further.
                _logger.info(
                    "meta_webhook drain: rate limited on event id=%s; "
                    "ending this run (no token/payload logged)", event.id)
                break
        # Once per run and only for a finished event, stamped with the run's
        # start so that a failure written later in this run still reads as
        # newer.
        if processed:
            Account = self.env['meta.account']
            Account._stamp_cron_run(const.WEBHOOK_OK_PARAM, now)

    # ------------------------------------------------------------------ #
    # Manual retry from the queue. Failed events go through the sync-log
    # retry engine, so both Retry buttons leave the queue and the log in
    # the same state. Write access here is admin-only, like the sync log.
    # ------------------------------------------------------------------ #
    def action_retry(self):
        """Retry the selected failed events, and end the rate-limit wait of
        waiting ones whose Page is active. Returns a summary notification.
        """
        # The view only hides the button; RPC reaches this method directly.
        is_admin = self.env.user.has_group('meta_lead_ads.group_meta_admin')
        if not self.env.su and not is_admin:
            raise AccessError(_("Meta Admin access required."))
        self.check_access('read')
        # Same as the sync-log Retry: the erased data must not be fetched
        # from Meta again.
        if self.filtered(lambda e: e.payload_cleared == 'erased'):
            raise UserError(_("The personal data of this Meta lead was "
                              "erased, so it can't be retried."))
        failed = self.filtered(lambda e: e.status == 'failed')
        waiting = self.filtered(
            lambda e: e.status == 'pending' and e.next_attempt_at)
        if not failed and not waiting:
            raise UserError(_(
                "Select at least one failed or waiting webhook event to "
                "retry."))
        # Every Page is checked before anything changes, so a refusal
        # leaves the whole selection as it was.
        pages = {}
        for event in failed | waiting:
            page = event._retry_page()
            if not page:
                raise UserError(self._page_out_of_reach_message())
            # Such a Page gives the failure row no company, and the caller's
            # own writes on that row would then be refused halfway through
            # the retry. The Page rule hides these Pages already; this keeps
            # it that way if the rule is ever widened.
            if not self.env.su and not page.sudo().lead_company_id:
                raise UserError(_(
                    "Sync rows that belong to no company can't be retried "
                    "here."))
            pages[event.id] = page
        Log = self.env['meta.sync.log']
        counts = {'ok': 0, 'failed': 0, 'scheduled': 0, 'busy': 0,
                  'held': 0, 'left': 0, 'removed': 0, 'archived': 0}
        for event in waiting:
            page = pages[event.id]
            if not page.active:
                # The drain would hold the lead without calling Meta, so
                # lifting Meta's pauses here would gain nothing and would let
                # every other Page call Meta while it still asks to wait.
                # Nothing changes and no drain is triggered.
                counts['archived'] += 1
                continue
            counts[event._end_wait(page)] += 1
        # Shared by the whole click, as in the sync-log retry: once Meta
        # asks to wait, the remaining events don't call it again.
        held = {}
        sent = 0
        deadline = _monotonic() + const.RETRY_TIME_BUDGET
        for event in failed:
            if sent >= const.RETRY_BATCH_LIMIT or _monotonic() >= deadline:
                counts['left'] += 1
                continue
            ev = event.sudo()
            try:
                with self.env.cr.savepoint():
                    # Same lock order as every other recovery path: this
                    # event first, then its sync-log row inside the engine.
                    if self._lock_leadgen_event(ev.leadgen_id) is None:
                        outcome = 'busy'
                    else:
                        # Something may have finished it before the lock.
                        ev.invalidate_recordset()
                        outcome = None
                        if ev.status == 'done':
                            outcome = 'ok'
                        elif ev.status != 'failed':
                            outcome = 'busy'
                        else:
                            # The Page may have been moved since the check
                            # above.
                            page = event._retry_page()
                            if not page:
                                ev.write({'error_message':
                                          self._page_out_of_reach_message()})
                                outcome = 'failed'
                            elif self.env['meta.leadgen.tombstone']._removed(
                                    [ev.leadgen_id]):
                                # Deleted in Odoo. Settled before a failure
                                # row is written, so it never gets a second
                                # skip row.
                                ev._settle_removed()
                                Log._record_removed_skip(
                                    [ev.leadgen_id], 'webhook', page=page)
                                outcome = 'removed'
                            elif page.active and Log._lead_reads_held(
                                    page, held):
                                # Checked before the failure row is
                                # touched, so a held event changes nothing.
                                # An archived Page goes on to the engine,
                                # which says so without calling Meta, as
                                # the Sync Log Retry does.
                                outcome = 'held'
                        if outcome is None:
                            # Same as the drain: the event follows its
                            # Page's company. The value comes from the Page.
                            ev.write({'page_company_id':
                                      page.sudo().lead_company_id.id})
                            # A click is not a new failure, so a row it finds
                            # keeps its time. A row made here is dated now.
                            row = Log._record_failure(
                                ev.leadgen_id, 'webhook',
                                ev.error_message or 'Webhook event failed',
                                page=page, failed_at=ev.create_date)
                            if row and not row.with_env(
                                    self.env).has_access('write'):
                                # The lead's failure row sits in a company
                                # the caller can't change.
                                ev.write({'error_message':
                                          self._page_out_of_reach_message()})
                                outcome = 'failed'
                            else:
                                # Run as the caller, so ingest checks the
                                # caller's Meta Admin rights and Page
                                # access.
                                outcome = (row.with_env(self.env)._retry_one(
                                    held=held) if row else 'busy')
            except Exception as e:
                # The text of an unexpected error can carry Meta's reply or
                # lead data, so only its class is logged.
                _logger.error(
                    "meta_webhook retry: unexpected error on event id=%s (%s) "
                    "(no token/payload logged)", event.id, type(e).__name__)
                outcome = event._fail_retry(pages[event.id])
            # A removed lead made no Graph call, so it isn't counted as sent.
            if outcome in ('ok', 'transient', 'failed'):
                sent += 1
            if outcome not in ('ok', 'busy', 'held', 'removed', 'archived'):
                outcome = 'failed'
            counts[outcome] += 1
        if counts['scheduled']:
            # Meta's estimate is honoured by the drain, so ending a wait
            # early is the admin's call. If Meta still refuses, the drain
            # records a new wait from Meta's new estimate.
            self.sudo().env.ref(
                'meta_lead_ads.cron_meta_webhook_drain')._trigger()
        return self.env['meta.sync.log']._retry_notification(
            counts['ok'],
            counts['failed'] + counts['busy'] + counts['held']
            + counts['archived'],
            scheduled=counts['scheduled'], busy=counts['busy'],
            held=counts['held'], left=counts['left'],
            removed=counts['removed'], archived=counts['archived'])

    @api.model
    def _page_out_of_reach_message(self):
        # No Page id in here: it would tell the caller about a Page in a
        # company they can't see.
        return _("This event's Page is not connected, or sends leads to a "
                 "company you can't access.")

    def _retry_page(self):
        """The connected Page of this event, searched as the caller so the
        company rules apply. Empty when unknown or out of reach. Archived
        Pages count, so Retry can say to reactivate them."""
        self.ensure_one()
        page_id = self.sudo().page_id
        if not page_id:
            return self.env['meta.page']
        Page = self.env['meta.page'].with_context(active_test=False)
        return Page.search([('page_id', '=', page_id)], limit=1)

    def _end_wait(self, page):
        """Let the drain take a waiting event on its next run.

        Clears the event's wait and its Page's pause, and the app-wide
        pause only while one is running: a throttle that covered just this
        Page leaves the other Pages' setting alone. Makes no Graph call.
        Returns 'scheduled', 'ok' when the event was finished meanwhile, or
        'busy'.
        """
        self.ensure_one()
        cr = self.env.cr
        try:
            with cr.savepoint():
                if self._lock_leadgen_event(self.leadgen_id) is None:
                    return 'busy'
                event = self.sudo()
                event.invalidate_recordset()
                if event.status == 'done':
                    return 'ok'
                if event.status != 'pending' or not event.next_attempt_at:
                    return 'busy'
                # The drain keeps the row of a Page it has just paused until
                # its run ends, so don't wait for it.
                cr.execute("SELECT id FROM meta_page WHERE id = %s "
                           "FOR UPDATE NOWAIT", (page.id,),
                           log_exceptions=False)
                # Same for the app pause setting. Nothing is written yet, so
                # a busy one leaves the event as it was.
                app_paused = self._app_lead_fetch_paused_until()
                if (app_paused and app_paused > fields.Datetime.now()
                        and not self._clear_app_lead_fetch_pause()):
                    return 'busy'
                event.write({'next_attempt_at': False})
                page.sudo()._clear_lead_fetch_pause()
        except (LockNotAvailable, SerializationFailure):
            return 'busy'
        return 'scheduled'

    def _fail_retry(self, page):
        """Leave a durable trace of a retry that raised unexpectedly.

        The savepoint rollback released the event lock, so it is taken
        again; a busy event is left to whoever holds it. A lead deleted in
        Odoo is settled as removed instead: the rollback may have undone
        that outcome after the retry had already reached it.
        """
        self.ensure_one()
        if self._lock_leadgen_event(self.leadgen_id) is None:
            return 'busy'
        Tomb = self.env['meta.leadgen.tombstone']
        if Tomb._removed([self.sudo().leadgen_id]):
            self._settle_removed()
            self.env['meta.sync.log']._record_removed_skip(
                [self.sudo().leadgen_id], 'webhook', page=page)
            return 'removed'
        message = 'Unexpected retry error'
        self.sudo().write({'status': 'failed', 'error_message': message})
        self.env['meta.sync.log']._record_failure(
            self.sudo().leadgen_id, 'webhook', message, page=page,
            failed_at=self.sudo().create_date)
        return 'failed'

    def action_erase_personal_data(self):
        """Erase the stored Meta data of the selected events' Meta leads,
        in the queue and in the sync log."""
        Log = self.env['meta.sync.log']
        Log._check_erase_access()
        self.check_access('read')
        rows = self.sudo().filtered(
            lambda e: e.leadgen_id and e.payload_cleared != 'erased')
        if not rows:
            raise UserError(_("Select at least one row whose personal data "
                              "hasn't been erased yet."))
        limit = meta_sync_log._ERASE_ROW_LIMIT
        if len(rows) > limit:
            raise UserError(_("Select at most %(limit)s rows at a time.",
                              limit=limit))
        # The shared helper refuses a lead that still holds one of the ids.
        Log._erase_leadgen_copies(
            sorted(set(rows.mapped('leadgen_id'))), 'manual',
            details=rows._erase_details())
        return Log._erase_notification(len(rows))

    def _erase_details(self):
        """Page, form and company per Meta lead id, for the removed-lead
        record the erase writes. Events only know Meta's ids, so the Page
        and form are looked up by those."""
        events = self.sudo()
        Page = self.env['meta.page'].sudo().with_context(active_test=False)
        Form = self.env['meta.lead.form'].sudo().with_context(
            active_test=False)
        pages = {p.page_id: p for p in Page.search(
            [('page_id', 'in', [e.page_id for e in events if e.page_id])])}
        forms = Form.search(
            [('form_id', 'in', [e.form_id for e in events if e.form_id])])
        details = {}
        for event in events.sorted('id', reverse=True):
            page = pages.get(event.page_id, Page)
            form = forms.filtered(
                lambda f: f.form_id == event.form_id
                and (not page or f.page_id == page))[:1]
            found = details.setdefault(event.leadgen_id, {})
            for key, value in (
                    ('page_id', page.id),
                    ('form_id', form.id),
                    ('company_id', (event.company_id
                                    or page.lead_company_id).id)):
                if value and not found.get(key):
                    found[key] = value
        return details

    def _settle_removed(self):
        """Finish these events without a lead: it was deleted in Odoo."""
        self.sudo().filtered(lambda e: e.status != 'done').write({
            'status': 'done', 'lead_id': False, 'next_attempt_at': False,
            'error_message': _REMOVED_MESSAGE})

    def _requeue_failed_at(self):
        # A re-queued lead failing again is the same failure, so its row
        # keeps its time and trigger, as after a Retry.
        self.ensure_one()
        return self.create_date if self.requeued else None

    @api.model
    def _requeue_archived_holds(self, pages, held_only=False, trigger=True):
        """Put the leads held while ``pages`` were archived back in line.

        Each held lead's failed queue event is reset to pending, or made
        again if the cleanup removed it. An event that is still pending is
        already in line and keeps any wait Meta asked for. Either way the
        lead's failed rows then say it is queued again; they stay failed,
        with their time and trigger, until the drain fetches the lead.
        There is no Graph call here: the drain fetches each lead through
        the single ingest path. The event is locked first and its rows
        second, as on every recovery path, and a lead or row busy elsewhere
        is skipped; the drain picks up such a lead on a later run.
        Erased leads are never fetched again.

        ``held_only`` limits this to rows that still say their Page is
        archived. ``trigger`` asks the drain for a run when anything was
        put back. Returns how many leads were put back.
        """
        Event = self.sudo()
        Log = self.env['meta.sync.log'].sudo()
        domain = [
            ('status', '=', 'failed'), ('trigger', '=', 'webhook'),
            ('meta_leadgen_id', '!=', False), ('page_id', 'in', pages.ids),
            ('payload_cleared', '!=', 'erased')]
        if held_only:
            domain.append(('error_message', '=', _PAGE_ARCHIVED_MESSAGE))
        rows_by_lead = {}
        for row in Log.search(domain, order='id'):
            rows_by_lead.setdefault(row.meta_leadgen_id, Log.browse())
            rows_by_lead[row.meta_leadgen_id] |= row
        count = 0
        now = fields.Datetime.now()
        for leadgen_id, lead_rows in rows_by_lead.items():
            event = Event._lock_leadgen_event(leadgen_id)
            if event is None:
                continue
            row = lead_rows[0]
            company = row.page_id.lead_company_id
            if event:
                # Something may have finished or erased it before the lock.
                event.invalidate_recordset()
                if event.status == 'done' or event.payload_cleared == 'erased':
                    continue
                if event.status == 'failed':
                    event.write({
                        'status': 'pending', 'attempts': 0,
                        'next_attempt_at': False, 'error_message': False,
                        'page_company_id': company.id, 'requeued': True,
                        'requeued_at': now})
                    count += 1
            else:
                try:
                    with self.env.cr.savepoint():
                        Event.create({
                            'leadgen_id': leadgen_id,
                            'page_id': row.page_id.page_id,
                            'form_id': row.form_id.form_id or False,
                            'page_company_id': company.id,
                            'requeued': True, 'requeued_at': now})
                # Queued by a webhook in the meantime; the drain takes it.
                # A row that still says the Page is archived is updated on
                # a later drain run.
                except IntegrityError:
                    continue
                count += 1
            for lead_row in lead_rows:
                if lead_row._lock_for_retry():
                    lead_row.write({'error_message': _PAGE_REQUEUED_MESSAGE})
        if count and trigger:
            Event.env.ref('meta_lead_ads.cron_meta_webhook_drain')._trigger()
        return count

    @api.model
    def _lock_leadgen_event(self, leadgen_id):
        """Lock the queue row of a Meta lead with NOWAIT.

        Every recovery path takes this lock first and the lead's sync-log
        rows second, so two of them can never wait on each other in a
        circle. NOWAIT keeps an admin click or a background reconcile from
        waiting on the drain or on another retry. Locking a row this
        transaction already holds succeeds.

        Returns the event (empty when the lead has none), or None when
        another transaction holds it.
        """
        cr = self.env.cr
        try:
            with cr.savepoint():
                cr.execute("SELECT id FROM meta_webhook_event "
                           "WHERE leadgen_id = %s FOR UPDATE NOWAIT",
                           (leadgen_id,), log_exceptions=False)
                ids = [r[0] for r in cr.fetchall()]
        except (LockNotAvailable, SerializationFailure):
            return None
        except Exception as e:
            # Some psycopg2 builds raise a plain OperationalError with the
            # pgcode instead.
            if getattr(e, 'pgcode', None) in (
                    errorcodes.LOCK_NOT_AVAILABLE,
                    errorcodes.SERIALIZATION_FAILURE):
                return None
            raise
        return self.browse(ids)

    @api.model
    def _rate_limit_is_page_scoped(self, exc):
        """True when Meta's throttle covers one Page rather than the app."""
        return getattr(exc, 'code', None) in RATE_LIMIT_PAGE_CODES

    @api.model
    def _app_lead_fetch_paused_until(self):
        """Return the app-wide lead read pause, or None."""
        value = self.env['ir.config_parameter'].sudo().get_param(
            APP_PAUSE_PARAM)
        if not value:
            return None
        try:
            return fields.Datetime.to_datetime(value)
        except (TypeError, ValueError):
            # A hand-edited value must not stop the queue for good.
            return None

    @api.model
    def _pause_app_lead_fetch(self, until):
        """Hold back lead reads for every Page until ``until``.

        Only ever moves the pause later. Errors are logged and swallowed:
        the event's own wait still holds it back.
        """
        try:
            with self.env.cr.savepoint():
                current = self._app_lead_fetch_paused_until()
                if current and current >= until:
                    return
                self.env['ir.config_parameter'].sudo().set_param(
                    APP_PAUSE_PARAM, fields.Datetime.to_string(until))
        except Exception as e:
            _logger.info("meta_webhook drain: app lead read pause not "
                         "saved (%s)", type(e).__name__)

    @api.model
    def _clear_app_lead_fetch_pause(self):
        """End the app-wide lead read pause now.

        The drain keeps the setting's row locked until its run ends, and an
        admin click must not wait for that, so the row is locked with
        NOWAIT first. Returns False, changing nothing, when it is busy.
        """
        cr = self.env.cr
        try:
            with cr.savepoint():
                cr.execute("SELECT id FROM ir_config_parameter "
                           "WHERE key = %s FOR UPDATE NOWAIT",
                           (APP_PAUSE_PARAM,), log_exceptions=False)
                if cr.fetchone():
                    self.env['ir.config_parameter'].sudo().set_param(
                        APP_PAUSE_PARAM, False)
        except (LockNotAvailable, SerializationFailure):
            return False
        except Exception as e:
            # Some psycopg2 builds raise a plain OperationalError with the
            # pgcode instead.
            if getattr(e, 'pgcode', None) in (
                    errorcodes.LOCK_NOT_AVAILABLE,
                    errorcodes.SERIALIZATION_FAILURE):
                return False
            raise
        return True

    @api.model
    def _rate_limit_until(self, exc, now):
        """Return the earliest time to call Meta again after a throttle.

        Uses the error's estimate in minutes, rounded up, or the fallback
        when the estimate is missing or not a positive number. Calling
        sooner than Meta asks only extends the block, but a garbled header
        must not hold leads back for good, so the wait is capped at
        RATE_LIMIT_MAX_MINUTES.
        """
        hint = getattr(exc, 'retry_after_min', None)
        minutes = None
        if isinstance(hint, (int, float)) and not isinstance(hint, bool):
            try:
                value = float(hint)
            except OverflowError:
                # An int too big for a float is still a huge wait.
                value = math.inf
            if not math.isnan(value) and value > 0:
                minutes = value
        if minutes is None:
            minutes = RATE_LIMIT_FALLBACK_MINUTES
        elif minutes > RATE_LIMIT_MAX_MINUTES:
            # The value itself can be enormous, so it is not logged.
            _logger.warning(
                "meta_webhook drain: Meta asked to wait more than %s "
                "minutes; waiting %s", RATE_LIMIT_MAX_MINUTES,
                RATE_LIMIT_MAX_MINUTES)
            minutes = RATE_LIMIT_MAX_MINUTES
        return now + timedelta(minutes=math.ceil(minutes))

    def _process_one(self, lookup_states=None, deadline=None):
        """Send one event through ingest_leadgen.

        A rate limit leaves it pending without counting an attempt, sets
        its next try, and pauses lead reads for its Page or, when Meta's
        limit covers the whole app, for every Page.
        Transient errors leave it pending until MAX_ATTEMPTS, then mark it
        failed. Permanent and auth errors fail it straight away. A failed
        sync-log row is written only when the event fails for good.
        Exception text is already redacted, so it is safe to store.
        ``lookup_states`` maps a Page to the name lookup state shared by the
        whole drain run; without it each event starts fresh. ``deadline``
        (a time.monotonic value) stops name lookups once the run is out of
        time; the lead is then flagged and the retry job fills the names.
        Returns 'page' or 'app' for the reach of a rate limit, else None.
        """
        self.ensure_one()
        page = self.env['meta.page'].with_context(active_test=False).search(
            [('page_id', '=', self.page_id)], limit=1)
        if not page:
            # Leave the page id out: sales managers can read sync-log rows.
            self.write({'status': 'failed',
                        'error_message': 'Unknown page; cannot process event'})
            self.env['meta.sync.log']._record_failure(
                self.leadgen_id, 'webhook',
                'Unknown page; cannot process event')
            return
        # The Page may have been re-routed since the event was queued. The
        # drain runs as superuser; sudo just keeps this derived field
        # writable if that ever changes. The value comes from the Page.
        self.sudo().write({'page_company_id': page.lead_company_id.id})
        if not page.active:
            # The drain takes no new leads from an archived Page, so the lead
            # waits on a failed row that keeps its Page and company. Ingest
            # does not check the Page's state itself, which is why Ingest by
            # ID and Restore still work for an admin.
            if self.env['meta.leadgen.tombstone']._removed([self.leadgen_id]):
                self._settle_removed()
                self.env['meta.sync.log']._record_removed_skip(
                    [self.leadgen_id], 'webhook', page=page)
                return
            # No Graph call was made, so no attempt is counted.
            self.write({'status': 'failed',
                        'error_message': _PAGE_ARCHIVED_MESSAGE})
            self.env['meta.sync.log']._record_failure(
                self.leadgen_id, 'webhook', _PAGE_ARCHIVED_MESSAGE, page=page,
                failed_at=self._requeue_failed_at())
            return
        state = (lookup_states.setdefault(page.id, {})
                 if lookup_states is not None else None)
        if deadline is not None:
            if state is None:
                state = {}
            state['deadline'] = deadline
        try:
            lead = self.env['meta.lead.ingest'].ingest_leadgen(
                page, self.leadgen_id, trigger='webhook',
                raw=json.loads(self.raw_payload) if self.raw_payload else None,
                lookup_state=state)
            # Only mark the event done when it points at a real lead; an
            # empty result goes back to pending like any transient error.
            if len(lead) != 1:
                raise MetaIngestRaceError(
                    "No lead returned for %s; retry" % self.leadgen_id)
            self.write({'status': 'done', 'lead_id': lead.id,
                        'next_attempt_at': False})
            # The drain already holds this event row.
            self.env['meta.sync.log']._reconcile_leadgen(
                self.leadgen_id, lead, event_locked=True)
        except MetaLeadRemovedError:
            # Deleted in Odoo, so the event is finished, not failed.
            self._settle_removed()
            self.env['meta.sync.log']._record_removed_skip(
                [self.leadgen_id], 'webhook', page=page)
        except MetaRateLimitError as e:
            # Not counted: a long throttle would otherwise use up every
            # attempt while waiting, and the event would fail for nothing.
            until = self._rate_limit_until(e, fields.Datetime.now())
            self.write({
                'next_attempt_at': until,
                'error_message': "Meta rate limit reached; next try after "
                                 "%s UTC" % fields.Datetime.to_string(until),
            })
            if self._rate_limit_is_page_scoped(e):
                page._pause_lead_fetch(until)
                return 'page'
            self._pause_app_lead_fetch(until)
            return 'app'
        except MetaTransientError as e:
            # Write attempts explicitly in each branch so the count isn't lost
            # to cache/flush ordering.
            new_attempts = self.attempts + 1
            if new_attempts >= MAX_ATTEMPTS:
                self.write({'attempts': new_attempts,
                            'status': 'failed', 'error_message': str(e)})
                self.env['meta.sync.log']._record_failure(
                    self.leadgen_id, 'webhook', str(e), page=page,
                    failed_at=self._requeue_failed_at())
            else:
                # Still pending; the next run retries it.
                self.write({'attempts': new_attempts})
        except (MetaPermanentError, MetaAuthError) as e:
            self.write({'attempts': self.attempts + 1,
                        'status': 'failed', 'error_message': str(e)})
            self.env['meta.sync.log']._record_failure(
                self.leadgen_id, 'webhook', str(e), page=page,
                failed_at=self._requeue_failed_at())
