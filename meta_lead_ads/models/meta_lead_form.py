# Meta Lead Ads Integration for Odoo — © 2026 CoreSys Builders. All rights reserved.
# Licensed under the Odoo Proprietary License v1.0 (OPL-1).
# Unauthorized copying, redistribution, or resale of this software, in whole or in
# part, via any medium, is strictly prohibited and constitutes a license violation.
# OPL-1: https://www.odoo.com/documentation/18.0/legal/licenses.html#odoo-apps

# Lead forms and the cron backfill. The backfill pulls each form's leads from
# Graph and hands them to ingest_leadgen(trigger='cron'); it never creates a
# crm.lead itself. Re-reading a lead is harmless because the UNIQUE constraint
# on meta_leadgen_id makes ingestion idempotent.
import json
import logging
import re
from datetime import datetime, timedelta, timezone

from odoo import _, api, fields, models
from odoo.exceptions import ValidationError

from .exceptions import (
    MetaPermanentError, MetaAuthError, MetaRateLimitError, MetaTransientError)

_logger = logging.getLogger(__name__)

# Meta form ids are bare object ids. Anything else would be refused by the
# Graph client's path guard and break every backfill for the form.
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
    # Backfill cursor, naive UTC. Only the sweep moves it, and only forward.
    last_synced_time = fields.Datetime(
        string='Last Synced', readonly=True,
        help='Backfill cursor: created_time of the last lead ingested by the '
             'cron sweep.')

    _sql_constraints = [
        ('form_id_uniq', 'unique(form_id)',
         'A Meta Lead Form with this ID already exists.'),
    ]

    @api.constrains('form_id')
    def _check_form_id_format(self):
        """Reject a Meta Form ID the Graph client would refuse to call."""
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
        # No context={'create': False}: Odoo already hides "New" for Meta Users
        # (no create ACL on meta.field.mapping), and admins still need it.
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
    # Cron backfill
    # ------------------------------------------------------------------ #
    @api.model
    def _cron_backfill(self):
        """Cron entry point: backfill every active, sync-enabled form.

        Each form runs in its own savepoint so one form's error doesn't undo
        the others. No manual commit; the cron runner commits on return.
        """
        # Heartbeat for the scheduler health check on the account form.
        self.env['meta.account']._ping_scheduler_heartbeat()
        forms = self.search([('active', '=', True),
                             ('sync_enabled', '=', True)])
        for form in forms:
            try:
                with self.env.cr.savepoint():
                    form._backfill_one_form()
            except (MetaPermanentError, MetaAuthError,
                    MetaRateLimitError, MetaTransientError) as e:
                # Meta exception messages are already token-free.
                self.env['meta.sync.log']._record(
                    False, 'cron', 'failed', error=str(e))
            except Exception:
                _logger.exception(
                    "Meta backfill: unexpected error on form id=%s "
                    "(no token/payload logged)", form.id)
                self.env['meta.sync.log']._record(
                    False, 'cron', 'failed', error='Unexpected backfill error')

    def _backfill_one_form(self):
        """Fetch this form's new leads, oldest first, and ingest each one.

        The cursor moves only after a lead ingests cleanly. A rate-limit or
        transient error stops the loop without logging a failure, so the cursor
        progress made so far is kept and the next sweep resumes from there.
        Permanent and auth errors propagate: _cron_backfill logs a failed row
        and the form's savepoint rolls back, cursor included.
        """
        self.ensure_one()
        now = datetime.now(timezone.utc)
        if self.last_synced_time:
            # Re-read a small overlap (default 60s) to catch same-second leads
            # that Meta only exposed after the last sweep.
            since_dt = (self.last_synced_time.replace(tzinfo=timezone.utc)
                        - timedelta(seconds=self._backfill_overlap_seconds()))
        else:
            since_dt = now - timedelta(days=self._backfill_lookback_days())
        since_epoch = int(since_dt.timestamp())

        params = {
            # Same field list as meta_graph_client.fetch_lead; keep them in
            # sync. Without the attribution fields a lead first seen by the
            # backfill would never get campaign data, because idempotency then
            # skips the later webhook.
            'fields': 'id,created_time,field_data,ad_id,ad_name,adset_id,'
                      'adset_name,campaign_id,campaign_name,form_id,platform',
            # The filter field is 'time_created', not created_time. It only
            # saves bandwidth; the cursor is what guarantees correctness. Meta
            # wants the filter as a JSON string.
            'filtering': json.dumps([{'field': 'time_created',
                                      'operator': 'GREATER_THAN',
                                      'value': since_epoch}]),
            'limit': 100,
        }
        # No sudo() needed: the cron runs as a user who can read the
        # admin-only token fields.
        token = self.page_id.access_token
        app_secret = self.page_id.account_id.app_secret
        # Load every page before sorting, so leads from the same second split
        # across pages still come out in order.
        leads = list(self.env['meta.graph.client']._iter_paged(
            token, '%s/leads' % self.form_id, params=params,
            app_secret=app_secret))
        # ISO 8601 strings sort chronologically.
        leads.sort(key=lambda l: l.get('created_time') or '')

        for lead in leads:
            leadgen_id = lead.get('id')
            if not leadgen_id:
                continue
            # Skip a lead with an unparseable created_time before touching
            # ingest or the cursor.
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
            # ingest_leadgen raises on failure and returns normally on success
            # or a duplicate, so reaching the cursor update means it's done.
            try:
                self.env['meta.lead.ingest'].ingest_leadgen(
                    self.page_id, leadgen_id, trigger='cron', raw=lead)
            except (MetaRateLimitError, MetaTransientError):
                # break, not continue: the cursor must not jump past this lead.
                # It is retried next sweep.
                _logger.info(
                    "Meta backfill: transient interruption mid-form on form "
                    "id=%s; preserving cursor and resuming next sweep "
                    "(no token/payload logged)", self.id)
                break
            # Only move forward. The cursor starts as False, which can't be
            # compared with a datetime.
            current = self.last_synced_time
            if (not current) or (parsed > current):
                self.last_synced_time = parsed

    def _backfill_lookback_days(self):
        """Days to look back on a form's first sync (default 30).

        Set with ir.config_parameter meta_lead_ads.backfill_lookback_days.
        """
        raw = self.env['ir.config_parameter'].sudo().get_param(
            'meta_lead_ads.backfill_lookback_days', default='30')
        try:
            return max(1, int(raw))
        except (TypeError, ValueError):
            return 30

    def _backfill_overlap_seconds(self):
        """Seconds before the cursor to re-read each sweep (default 60).

        Set with ir.config_parameter meta_lead_ads.backfill_overlap_seconds.
        """
        raw = self.env['ir.config_parameter'].sudo().get_param(
            'meta_lead_ads.backfill_overlap_seconds', default='60')
        try:
            return max(0, int(raw))
        except (TypeError, ValueError):
            return 60
