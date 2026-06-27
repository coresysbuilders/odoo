# Meta Lead Ads Integration for Odoo — © 2026 CoreSys Builders. All rights reserved.
# Licensed under the Odoo Proprietary License v1.0 (OPL-1).
# Unauthorized copying, redistribution, or resale of this software, in whole or in
# part, via any medium, is strictly prohibited and constitutes a license violation.
# OPL-1: https://www.odoo.com/documentation/19.0/legal/licenses.html#odoo-apps

# The per-form cron backfill sweep. _cron_backfill iterates only active +
# sync_enabled forms inside a per-form savepoint (sibling isolation only -- not
# a per-lead durability mechanism; gap-safety rests on DB-UNIQUE idempotency)
# and delegates to _backfill_one_form, a thin caller over the ingest service
# ingest_leadgen(trigger='cron') -- the sole lead-create path. This model
# creates zero leads itself.
#
# _backfill_one_form also catches a transient/rate-limit error per-lead and
# `break`s the page loop -- so the cursor advances already made this sweep
# (leads 1..N-1) survive and commit on the clean return, instead of the whole
# per-form savepoint rolling back on a single busy-form interruption. The
# per-form savepoint remains sibling isolation only; the per-lead transient
# break is the cursor-protection mechanism (it mirrors the per-form sibling
# boundary, not the per-event meta.webhook.event._cron_drain queue). A
# permanent/auth error is not caught per-lead -- it propagates and fails the
# form (whole-sweep rollback, surfaced 'failed' row via _cron_backfill's typed
# handler).
import json
import logging
import re
from datetime import datetime, timedelta, timezone

from odoo import _, api, fields, models
from odoo.exceptions import ValidationError

from .exceptions import (
    MetaPermanentError, MetaAuthError, MetaRateLimitError, MetaTransientError)

_logger = logging.getLogger(__name__)

# A Meta lead-form id is a bare object id (digits, occasionally with '-'/'_').
# Anything else (a slash, '://', '?', '#', '%', '..', whitespace, ...) would be
# rejected by the Graph client's bare-path guard and so fail EVERY backfill
# sweep for this form. Validate at write time so a bad value can never be
# stored in the first place.
_FORM_ID_RE = re.compile(r'^[A-Za-z0-9_-]+$')


class MetaLeadForm(models.Model):
    _name = 'meta.lead.form'
    _description = 'Meta Lead Form'

    name = fields.Char(required=True)
    form_id = fields.Char(string='Meta Form ID', required=True, index=True)
    active = fields.Boolean(default=True)
    sync_enabled = fields.Boolean(string='Sync Enabled', default=True,
                                  help='When off, the backfill cron skips this form.')
    page_id = fields.Many2one('meta.page', string='Page',
                              required=True, ondelete='cascade')
    mapping_ids = fields.One2many('meta.field.mapping', 'form_id', string='Field Mappings')
    mapping_count = fields.Integer(compute='_compute_mapping_count', store=True)
    # Backfill cursor: created_time of the last lead ingested by the cron
    # sweep, stored naive-UTC. readonly -- only the sweep advances it, and only
    # forward (falsey-safe strict-greater idiom). A re-sweep re-reads the
    # (T - overlap) window; DB-UNIQUE idempotency makes those re-reads free.
    last_synced_time = fields.Datetime(
        string='Last Synced', readonly=True,
        help='Backfill cursor: created_time of the last lead ingested by the '
             'cron sweep.')

    # Odoo 19: models.Constraint replaces the removed _sql_constraints list.
    _form_id_uniq = models.Constraint(
        'unique(form_id)',
        'A Meta Lead Form with this ID already exists.',
    )

    @api.constrains('form_id')
    def _check_form_id_format(self):
        """Reject a malformed Meta Form ID at write time (security L-03).

        A stored id containing a slash / scheme / query / fragment / traversal /
        percent-encoding / whitespace would be refused by the Graph client's
        bare-path guard and so break every backfill sweep for this form. Block
        it here so it can never be persisted."""
        for rec in self:
            if rec.form_id and not _FORM_ID_RE.match(rec.form_id):
                raise ValidationError(_(
                    "Invalid Meta Form ID %r: only letters, digits, '-' and "
                    "'_' are allowed. A malformed id would fail every backfill "
                    "sweep for this form.") % rec.form_id)

    @api.depends('mapping_ids')
    def _compute_mapping_count(self):
        for rec in self:
            rec.mapping_count = len(rec.mapping_ids)

    def action_open_mappings(self):
        # The child list's "New" button is auto-hidden for users without
        # create ACL. Meta Users have perm_create=0 on meta.field.mapping, so
        # they cannot create here; Meta Admins (full CRUD) can still create via
        # drill-down. No context={'create': False} override -- that would
        # wrongly block legitimate admin creation.
        self.ensure_one()
        return {
            'type': 'ir.actions.act_window',
            'name': 'Field Mappings',
            'res_model': 'meta.field.mapping',
            'view_mode': 'list,form',
            'domain': [('form_id', '=', self.id)],
            'context': {'default_form_id': self.id},
        }

    # ------------------------------------------------------------------ #
    # The cron backfill sweep.
    # ------------------------------------------------------------------ #
    @api.model
    def _cron_backfill(self):
        """ir.cron entry point. Sweep only active + sync_enabled forms, each
        inside its own savepoint so one form's Graph error never aborts the
        siblings. The savepoint is sibling isolation only -- not a per-lead
        durability mechanism: gap-safety rests on DB-UNIQUE idempotency (a
        crashed form's savepoint rolls back and the next sweep re-ingests
        harmlessly via the ingest service). No manual commit anywhere -- the
        cron runner commits on clean return; per-lead commit is forbidden.

        This method's per-form savepoint is sibling isolation (one form's error
        never aborts the others) -- it is not the per-event durability of
        meta.webhook.event._cron_drain. Per-lead cursor durability inside a form
        is provided by _backfill_one_form's transient catch+break, not by this
        savepoint. The cron creates zero leads -- _backfill_one_form is a thin
        caller over the ingest service ingest_leadgen.
        """
        # Heartbeat: prove the scheduler is alive so the account form can flag a
        # stalled cron worker (otherwise lead sync fails silently).
        self.env['meta.account']._ping_scheduler_heartbeat()
        forms = self.search([('active', '=', True),
                             ('sync_enabled', '=', True)])
        for form in forms:
            try:
                # The savepoint wraps the per-form worker (sibling isolation):
                # a Graph/processing failure on THIS form rolls back only its
                # partial work, leaving already-swept siblings intact.
                with self.env.cr.savepoint():
                    form._backfill_one_form()
            except (MetaPermanentError, MetaAuthError,
                    MetaRateLimitError, MetaTransientError) as e:
                # Typed Meta failures are already token-free (str redacted
                # upstream). Record a token-free cron/failed audit row and keep
                # sweeping the remaining forms.
                self.env['meta.sync.log']._record(
                    False, 'cron', 'failed', error=str(e))
            except Exception:
                # Genuinely-unexpected (non-typed/programming) error: isolate it,
                # log token-free (NO token/payload), and continue the sweep.
                _logger.exception(
                    "Meta backfill: unexpected error on form id=%s "
                    "(no token/payload logged)", form.id)
                self.env['meta.sync.log']._record(
                    False, 'cron', 'failed', error='Unexpected backfill error')

    def _backfill_one_form(self):
        """Worker: page the Graph leads edge to exhaustion via _iter_paged,
        materialize the full result with list(...), sort oldest-first, and feed
        every leadgen_id to the ingest service ingest_leadgen (trigger='cron')
        -- the sole lead-create path (this worker creates no lead itself).
        Advance last_synced_time only after a successful (non-raising) ingest,
        via the falsey-safe strict-greater idiom (no TypeError on a never-synced
        form; never moves backward).

        Per-lead transient durability: a MetaRateLimitError / MetaTransientError
        raised by ingest_leadgen mid-form is caught per-lead and `break`s the
        loop (not continue) -- so the cursor advances already made for leads
        1..N-1 stand and commit on the clean return, surviving the enclosing
        per-form savepoint. The next sweep re-reads the overlap window from that
        preserved cursor and the interrupted lead is retried (DB-UNIQUE
        idempotency makes the re-reads free). This transient break is the
        per-form sibling-level cursor protection; it is not the per-event
        _cron_drain queue mirror.

        The transient break is intentionally silent at the sync-log level -- it
        records no meta.sync.log 'failed' row, because a mid-form transient is a
        benign pause-and-resume, not a surfaced failure (it would only add
        transient noise; cf. the webhook queue keeping transient events pending,
        not failed). The surfaced failure path is the permanent/auth one: those
        are not caught here, so they propagate to _cron_backfill's typed handler
        which does _record(... 'failed' ...) and rolls back the whole per-form
        savepoint for this sweep (no infinite retry; the cursor does not advance
        on a permanent failure).
        """
        self.ensure_one()
        now = datetime.now(timezone.utc)
        if self.last_synced_time:
            # Overlap re-query window (default 60s): re-read the last `overlap`
            # seconds so same-second siblings -- including ones that only became
            # visible in a later sweep under Meta's eventual consistency -- are
            # re-queried. DB-UNIQUE idempotency makes the re-reads free. The
            # naive-UTC cursor is reinterpreted as UTC to compute the epoch
            # floor.
            since_dt = (self.last_synced_time.replace(tzinfo=timezone.utc)
                        - timedelta(seconds=self._backfill_overlap_seconds()))
        else:
            # Never-synced: bounded lookback window (default 30 days).
            since_dt = now - timedelta(days=self._backfill_lookback_days())
        since_epoch = int(since_dt.timestamp())

        params = {
            # Request the full attribution set so a cron-backfilled lead
            # carries the same campaign/ad-set/ad attribution a webhook lead
            # gets via fetch_lead. Omitting these made backfill-first leads
            # permanently lose campaign/ad-set data -- idempotency then skips
            # the later webhook, so the gap never healed. Keep this list in
            # lock-step with meta_graph_client.fetch_lead's fields.
            'fields': 'id,created_time,field_data,ad_id,ad_name,adset_id,'
                      'adset_name,campaign_id,campaign_name,form_id,platform',
            # Filter on 'time_created' (not created_time); a best-effort
            # bandwidth hint only -- correctness rests on the cursor + DB-UNIQUE
            # idempotency. Meta expects the nested filtering structure as a JSON
            # string; requests will not encode a Python list-of-dicts into the
            # Graph query shape for us.
            'filtering': json.dumps([{'field': 'time_created',
                                      'operator': 'GREATER_THAN',
                                      'value': since_epoch}]),
            'limit': 100,
        }
        # Admin-grouped secrets read with plain attribute access (no .sudo()) --
        # the ir.cron runs as the privileged cron user, matching the
        # meta_webhook_event._process_one precedent (deliberate mirror).
        token = self.page_id.access_token
        app_secret = self.page_id.account_id.app_secret
        # Materialize the full paged result with list(...) before sorting:
        # _iter_paged exhausts all pages, so buffering every page first is what
        # makes a same-second bucket split across pages safe.
        leads = list(self.env['meta.graph.client']._iter_paged(
            token, '%s/leads' % self.form_id, params=params,
            app_secret=app_secret))
        # Oldest-first: ISO8601 sorts lexicographically.
        leads.sort(key=lambda l: l.get('created_time') or '')

        for lead in leads:
            leadgen_id = lead.get('id')
            if not leadgen_id:
                continue
            # Parse created_time first: a malformed/missing value is skipped
            # token-free before any ingest/cursor change, with a meta.sync.log
            # failed/cron audit row.
            raw_ct = lead.get('created_time')
            try:
                parsed = (datetime.fromisoformat(raw_ct)
                          .astimezone(timezone.utc).replace(tzinfo=None))
            except (TypeError, ValueError):
                _logger.warning(
                    "Meta backfill: skipping lead with malformed/missing "
                    "created_time on form id=%s (no token/payload logged)",
                    self.id)
                self.env['meta.sync.log']._record(
                    False, 'cron', 'failed',
                    error='malformed/missing created_time on backfill '
                          '(no token/payload logged)')
                continue
            # Sole create path. page arg = the meta.page record. Success
            # contract: ingest_leadgen raises on failure and returns cleanly on
            # success / idempotent no-op -- so a clean return == confirmed
            # processing. The advance below sits after this call, never before,
            # never in an except branch (state-driven, not exception-driven).
            try:
                self.env['meta.lead.ingest'].ingest_leadgen(
                    self.page_id, leadgen_id, trigger='cron', raw=lead)
            except (MetaRateLimitError, MetaTransientError):
                # A transient/rate-limit interruption is contained per-lead.
                # `break` (not continue) stops the loop so the cursor advances
                # already made for leads 1..N-1 stand and commit on the clean
                # return -- they survive the enclosing per-form savepoint, and
                # the next sweep re-reads from the preserved cursor (this lead
                # is retried, never stranded). Intentionally silent: no
                # _record('failed') row -- a transient pause-and-resume is not a
                # surfaced failure (see docstring). The token-free log
                # references only self.id -- no leadgen-id/page-id/token.
                _logger.info(
                    "Meta backfill: transient interruption mid-form on form "
                    "id=%s; preserving cursor and resuming next sweep "
                    "(no token/payload logged)", self.id)
                break
            # MetaPermanentError / MetaAuthError are deliberately not caught
            # here -- they propagate out of the worker and fail the form via
            # _cron_backfill's typed handler (whole-sweep rollback + surfaced
            # 'failed' row). No infinite retry of a dead-token form.
            # Falsey-safe strict-greater advance: treat a False/None cursor as
            # "always less" so the first advance never evaluates `parsed >
            # False` (TypeError in Py3); the strict `>` is the regression guard
            # so an older out-of-order re-read (still ingested, idempotency-safe)
            # never lowers the cursor.
            current = self.last_synced_time
            if (not current) or (parsed > current):
                self.last_synced_time = parsed

    def _backfill_lookback_days(self):
        """Never-synced lookback window in days (default 30). Admin-tunable via
        ir.config_parameter key meta_lead_ads.backfill_lookback_days."""
        raw = self.env['ir.config_parameter'].sudo().get_param(
            'meta_lead_ads.backfill_lookback_days', default='30')
        try:
            return max(1, int(raw))
        except (TypeError, ValueError):
            return 30

    def _backfill_overlap_seconds(self):
        """Same-second overlap re-query window in seconds (default 60). A
        bounded eventual-consistency hedge -- a 15-minute cron re-reads only the
        last 60s of the prior window, and DB-UNIQUE idempotency makes those
        re-reads free. Admin-tunable via ir.config_parameter key
        meta_lead_ads.backfill_overlap_seconds."""
        raw = self.env['ir.config_parameter'].sudo().get_param(
            'meta_lead_ads.backfill_overlap_seconds', default='60')
        try:
            return max(0, int(raw))
        except (TypeError, ValueError):
            return 60
