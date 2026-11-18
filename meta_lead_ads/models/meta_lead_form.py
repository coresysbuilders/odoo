# Meta Lead Ads Integration for Odoo — © 2026 CoreSys Builders. All rights reserved.
# Licensed under the Odoo Proprietary License v1.0 (OPL-1).
# Unauthorized copying, redistribution, or resale of this software, in whole or in
# part, via any medium, is strictly prohibited and constitutes a license violation.
# OPL-1: https://www.odoo.com/documentation/20.0/legal/licenses.html#odoo-apps

# Lead forms and the cron backfill. The backfill pulls each form's leads from
# Graph and hands them to ingest_leadgen(trigger='cron'); it never creates a
# crm.lead itself. Re-reading a lead is harmless because the UNIQUE constraint
# on meta_leadgen_id makes ingestion idempotent.
import json
import logging
import re
import time
from datetime import datetime, timedelta, timezone

from odoo import _, api, fields, models
from odoo.exceptions import AccessError, ValidationError
from odoo.tools.misc import split_every

# Read at call time so tests can patch the backfill limits.
from . import const
from .exceptions import (
    MetaPermanentError, MetaAuthError, MetaLeadRemovedError,
    MetaRateLimitError, MetaTransientError)
from .meta_lead_ingest import parse_meta_time
from .meta_sync_log import _is_db_contention

_logger = logging.getLogger(__name__)
# Module-level so tests can patch the clock of the run's time budget.
_monotonic = time.monotonic

# Meta form ids are bare object ids. Anything else would be refused by the
# Graph client's path guard and break every backfill for the form.
_FORM_ID_RE = re.compile(r'^[A-Za-z0-9_-]+$')

# Listed lead ids checked against crm.lead in one query. One Graph page, so
# the IN list stays small and ingest can start after the first page.
_KNOWN_ID_CHUNK = 100

# End of the row text a reconcile cut-off leaves on its form. The backfill
# clears its own form rows when it syncs cleanly; this one waits for a
# finished reconcile pass.
_RECONCILE_STOP_TEXT = 'reconcile stopped for this form'

# Meta errors a listing page can raise once earlier pages were handed over.
_LISTING_ERRORS = (MetaRateLimitError, MetaTransientError, MetaAuthError,
                   MetaPermanentError)


def _meta_error_summary(exc, scope):
    """Fixed text for a failure row from a Meta error.

    ``scope`` is 'lead' or 'listing'. Only Meta's numeric code and subcode
    are kept: its message text can echo request details, and CRM managers
    read these rows.
    """
    if isinstance(exc, MetaAuthError):
        text = ('Meta rejected the access token while reading this lead'
                if scope == 'lead' else
                'Meta rejected the access token while listing this form')
    else:
        text = ('Meta refused to return this lead' if scope == 'lead'
                else 'Meta refused the lead listing for this form')
    code = getattr(exc, 'code', None)
    subcode = getattr(exc, 'subcode', None)
    # bool is an int too, but never a real Meta code.
    if isinstance(code, int) and not isinstance(code, bool):
        if isinstance(subcode, int) and not isinstance(subcode, bool):
            text += ' (Meta error code %d, subcode %d)' % (code, subcode)
        else:
            text += ' (Meta error code %d)' % code
    return text


# Stops after which a form did not really get its turn: Meta or the network
# cut it off, so it keeps its place in line.
_NO_TURN_STOPS = ('transient', 'rate_limit', 'auth', 'listing_permanent')


def _turn_done(outcome, whole_budget):
    """True when the form had its turn this run and can go to the back."""
    # Stopped by Meta or the network: it didn't get its turn, so it keeps
    # its place. Cut by the budget: same, unless it had the whole budget to
    # itself and would otherwise hold the head forever. A form that only got
    # what a failing form ahead of it left over keeps its place, so it gets
    # the whole budget on a later run.
    if outcome['stop'] in _NO_TURN_STOPS:
        return False
    if whole_budget:
        return True
    return not (outcome['deadline_hit'] or outcome['stop'] == 'budget')


class MetaLeadForm(models.Model):
    _name = 'meta.lead.form'
    _description = 'Meta Lead Form'

    # The ORM does not enforce readonly on write, and writing the related
    # company would push it onto the Page and move the Page as well.
    _derived_company_fields = ('company_id',)
    # The backfill position. Only the scheduled sync sets it; a crafted RPC
    # write could make the sweep skip leads or stop reading a form.
    _sweep_state_fields = ('last_synced_time', 'backfill_slicing',
                           'backfill_slice_hours',
                           'backfill_incomplete_count', 'last_swept_time',
                           'last_reconciled_time', 'reconcile_resume_time',
                           'reconcile_slice_hours', 'reconcile_pass_end')

    name = fields.Char(required=True)
    form_id = fields.Char(string='Meta Form ID', required=True, index=True)
    active = fields.Boolean(default=True)
    sync_enabled = fields.Boolean(string='Sync Enabled', default=True,
                                  help='When off, the backfill cron skips this form.')
    page_id = fields.Many2one('meta.page', string='Page',
                              required=True, ondelete='cascade')
    company_id = fields.Many2one(related='page_id.lead_company_id', store=True,
                                 index=True, string='Company')
    mapping_ids = fields.One2many('meta.field.mapping', 'form_id', string='Field Mappings')
    mapping_count = fields.Integer(compute='_compute_mapping_count', store=True)
    # Mappings the module switched off because their field may no longer be
    # filled from a form. Rows an admin archived by hand carry no reason.
    disabled_mapping_count = fields.Integer(
        compute='_compute_disabled_mappings')
    disabled_mapping_notice = fields.Char(
        compute='_compute_disabled_mappings')
    # Backfill cursor, naive UTC. Only the sweep moves it, and only forward.
    last_synced_time = fields.Datetime(
        string='Last Synced', readonly=True, copy=False,
        help='Backfill cursor: created_time of the last lead the cron sweep '
             'settled, or the end of the last time slice read while catching '
             'up.')
    # No defaults on these two, so an upgrade doesn't rewrite the table. An
    # empty column reads as False / 0.
    backfill_slicing = fields.Boolean(
        string='Backfill Catching Up', copy=False, readonly=True,
        help='Set after an incomplete lead listing. The backfill then reads '
             'one day of leads per run, or less when a day does not list in '
             'one run, until it has caught up, so a long '
             'backlog takes days rather than minutes. New leads still arrive '
             'through the webhook meanwhile.')
    backfill_slice_hours = fields.Integer(
        string='Catch-up Read Length (hours)', copy=False, readonly=True,
        help='How many hours of leads the backfill reads per run while '
             'catching up. It shrinks when even that does not list in one '
             'run and grows back after clean reads. Empty means one day.')
    backfill_incomplete_count = fields.Integer(
        string='Incomplete Listings in a Row', copy=False, readonly=True)
    # No default: an empty stamp means never swept and sorts first.
    last_swept_time = fields.Datetime(
        string='Last Backfill Turn', copy=False, readonly=True,
        help='When the 15-minute backfill last finished a full turn on this '
             'form. The backfill starts with the forms that waited longest.')
    # No default either: an empty stamp means never reconciled and sorts
    # first, and an upgrade doesn't rewrite the table.
    last_reconciled_time = fields.Datetime(
        string='Last Reconciled', copy=False, readonly=True,
        help='When the daily wide sweep last finished a turn on this form. '
             'The sweep starts with the forms that waited longest.')
    # The wide sweep's own place in its window, apart from the backfill
    # cursor. Empty means a fresh pass over the whole window.
    reconcile_resume_time = fields.Datetime(
        string='Reconcile Resumes At', copy=False, readonly=True,
        help='Where the daily wide sweep continues when its last pass did not '
             'fit in one run. Empty means the next pass starts at the '
             'beginning of the window.')
    reconcile_slice_hours = fields.Integer(
        string='Reconcile Read Length (hours)', copy=False, readonly=True,
        help='How many hours of leads the wide sweep reads at once on this '
             'form. It shrinks after an incomplete read and grows back after '
             'clean ones. Empty means the whole window.')
    reconcile_pass_end = fields.Datetime(
        string='Last Reconcile Pass Ended', copy=False, readonly=True,
        help='When the daily wide sweep last read this form up to the '
             'present. If the next pass comes later than the window, it '
             'starts here instead, up to twice the window back.')

    # Odoo 19: models.Constraint replaces _sql_constraints.
    _form_id_uniq = models.Constraint(
        'unique(form_id)',
        'A Meta Lead Form with this ID already exists.',
    )

    @api.model_create_multi
    def create(self, vals_list):
        self._check_derived_company_vals(vals_list)
        self._check_sweep_state_vals(vals_list)
        records = super().create(vals_list)
        if not self.env.su:
            records._check_page_access()
        return records

    def write(self, vals):
        self._check_derived_company_vals([vals])
        self._check_sweep_state_vals([vals])
        res = super().write(vals)
        if not self.env.su and 'page_id' in vals:
            self._check_page_access()
        return res

    def unlink(self):
        # The database drops the mappings with their form and never calls
        # the mapping's unlink, so the fields they filled are released here.
        mapped = self._meta_mapped_crm_fields()
        res = super().unlink()
        self.env['meta.field.mapping']._meta_release_unused_fields(mapped)
        return res

    def _meta_mapped_crm_fields(self):
        # Also used by Page and account deletes, which drop their forms the
        # same way. Archived mappings count too: they kept the field owned.
        return self.env['meta.field.mapping'].sudo().with_context(
            active_test=False).search(
            [('form_id', 'in', self.ids)]).mapped('crm_field_id')

    def _check_page_access(self):
        # A form follows its Page's company, so attaching it to a Page the
        # writer can't see would move it into another company. Done here
        # rather than in @api.constrains, which always runs as sudo.
        Page = self.env['meta.page']
        for rec in self.sudo():
            if not Page.browse(rec.page_id.id).has_access('read'):
                raise ValidationError(_(
                    "You can only add forms to a Page you can access."))

    def _check_derived_company_vals(self, vals_list):
        if self.env.su:
            return
        for vals in vals_list:
            if any(name in vals for name in self._derived_company_fields):
                raise AccessError(_(
                    "The company on this record is set automatically and "
                    "can't be changed directly."))

    def _check_sweep_state_vals(self, vals_list):
        # readonly=True only hides the field in the web client. Reject rather
        # than drop the values, so a crafted RPC fails loudly.
        if self.env.su:
            return
        for vals in vals_list:
            if any(name in vals for name in self._sweep_state_fields):
                raise AccessError(_(
                    "The backfill position of a form is kept by the scheduled "
                    "sync and can't be set directly."))

    @api.constrains('form_id')
    def _check_form_id_format(self):
        """Reject a Meta Form ID the Graph client would refuse to call."""
        for rec in self:
            if rec.form_id and not _FORM_ID_RE.match(rec.form_id):
                raise ValidationError(_(
                    "Invalid Meta Form ID %r: only letters, digits, '-' and "
                    "'_' are allowed. A malformed id would fail every backfill "
                    "sweep for this form.") % rec.form_id)

    # The count is stored, and mapping_ids alone only reacts to rows moving
    # between forms, not to a row being archived. The recompute can also run
    # in an env with active_test off, which lets archived rows into
    # mapping_ids, so they are filtered here.
    @api.depends('mapping_ids', 'mapping_ids.active')
    def _compute_mapping_count(self):
        for rec in self:
            rec.mapping_count = len(rec.mapping_ids.filtered('active'))

    def _compute_disabled_mappings(self):
        # Searched as the caller, so company rules still apply.
        Mapping = self.env['meta.field.mapping'].with_context(
            active_test=False)
        for rec in self:
            count = rec.id and Mapping.search_count([
                ('form_id', '=', rec.id), ('active', '=', False),
                ('disabled_reason', '!=', False)])
            rec.disabled_mapping_count = count or 0
            if not count:
                rec.disabled_mapping_notice = False
            elif count == 1:
                rec.disabled_mapping_notice = _(
                    "One mapping on this form was turned off because its "
                    "field can no longer be filled from a form answer. The "
                    "answer is still saved on each lead, under Meta Answers. "
                    "To route leads to a salesperson or team, use the Page's "
                    "Lead Routing settings.")
            else:
                rec.disabled_mapping_notice = _(
                    "%(count)s mappings on this form were turned off because "
                    "their fields can no longer be filled from a form answer. "
                    "The answers are still saved on each lead, under Meta "
                    "Answers. To route leads to a salesperson or team, use "
                    "the Page's Lead Routing settings.", count=count)

    def action_open_disabled_mappings(self):
        self.ensure_one()
        return {
            'type': 'ir.actions.act_window',
            'name': _("Disabled Mappings"),
            'res_model': 'meta.field.mapping',
            'view_mode': 'list,form',
            'domain': [('form_id', '=', self.id)],
            'context': {'active_test': False,
                        'search_default_filter_disabled': 1,
                        'default_form_id': self.id},
        }

    def action_open_page_routing(self):
        self.ensure_one()
        # The banner button is admin-only, but RPC reaches this directly.
        if (not self.env.su
                and not self.env.user.has_group('meta_lead_ads.group_meta_admin')):
            raise AccessError(_("Meta Admin access required."))
        return {
            'type': 'ir.actions.act_window',
            'res_model': 'meta.page',
            'res_id': self.page_id.id,
            'view_mode': 'form',
            'target': 'current',
        }

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

        The work is done by _run_sweep. No manual commit; the cron runner
        commits on return.

        A form's cursor never passes leads the sweep has not read. When
        Meta's listing comes back incomplete the cursor holds and the form
        is read one day at a time until it has caught up; a form that stays
        incomplete for several sweeps gets one failure row, which clears on
        its next complete sweep. A listing cut by the run's time budget holds
        the cursor and goes first next run; only on the run's first form,
        which had the whole budget, is it read in slices as well.
        """
        # Its own stamp for the scheduler check, so a stopped backfill shows
        # even while the drain keeps running.
        self.env['meta.account']._stamp_cron_run(const.BACKFILL_RUN_PARAM)
        return self._run_sweep(reconcile=False)

    @api.model
    def _cron_reconcile(self):
        """Daily cron: read the last few days of every form again.

        The 15-minute backfill re-reads only a short overlap before its
        cursor, so a lead Meta makes visible after the cursor passed its time
        would never be read. This wider read only ingests such leads; it
        never moves the backfill cursor. A form whose window does not fit in
        one run continues where it stopped on the next run.
        """
        # No stamp here: the health check doesn't report this job, and the
        # backfill has its own key.
        return self._run_sweep(reconcile=True)

    @api.model
    def _run_sweep(self, reconcile=False):
        """Sweep the forms one by one until the run's time budget is used.

        Each form runs in its own savepoint so a failed listing doesn't undo
        the others. A single bad lead never gets this far: _ingest_listed
        records it and moves on.

        Forms take turns: the ones that waited longest since their last full
        turn go first, so a form cut by the budget starts the next run. A
        form that fails keeps its place, unless failures at the head of the
        line used up half the budget: the form whose failure crossed that
        point goes to the back, so one that hangs to the read timeout every
        run can't keep every form behind it from getting a whole run.

        Meta's lead read pauses (per Page and app-wide) are honoured the way
        the queue drain and Retry honour them, and a token Meta rejects stops
        only the forms of its own account for the rest of the run.
        """
        deadline = _monotonic() + const.BACKFILL_TIME_BUDGET
        # So the two jobs can be told apart in the server log.
        label = 'Meta reconcile' if reconcile else 'Meta backfill'
        Log = self.env['meta.sync.log']
        # Throttles met during this run, shared with _lead_reads_held.
        held = {}
        if Log._lead_reads_held(False, held):
            _logger.info("%s: Meta lead reads are paused; nothing listed "
                         "this run", label)
            return
        # Accounts whose token Meta rejected this run. Their other forms
        # would only fail the same way and add one row each.
        dead_accounts = set()
        # Each job keeps its own turn order, so neither one's cuts push the
        # other's forms back.
        if reconcile:
            order = 'last_reconciled_time asc nulls first, id'
        else:
            order = 'last_swept_time asc nulls first, id'
        # The forms of an archived Page stay active, but archiving the Page
        # holds its leads; the sweep picks them up from the cursor once the
        # Page is active again.
        forms = self.search([('active', '=', True),
                             ('sync_enabled', '=', True),
                             ('page_id.active', '=', True)], order=order)
        # True until a form gets its turn. A form whose listing fails, or
        # that Meta stops on one of its leads, is never stamped and comes
        # first again next run, so it must not use up this turn, or a long
        # form behind it would never be sliced.
        first = True
        # Name reads are throttled separately from lead reads, so after a
        # refused name lookup the rest of the run skips them for that Page
        # and its leads arrive flagged instead. The deadline rides along so
        # no name lookup starts once the budget is used.
        lookup_states = {}
        complete_forms = self.browse()
        half = const.BACKFILL_TIME_BUDGET / 2
        stamp_field = 'last_reconciled_time' if reconcile else 'last_swept_time'

        def _stamp(form):
            # sudo: the stamp is sweep state, guarded like the cursor. Each
            # job stamps only its own field.
            form.sudo().write({stamp_field: fields.Datetime.now()})

        def _failed_turn(form, left_before):
            # Only while no form has had its turn yet: this failure is the
            # one that left the rest of the line without a whole budget.
            if first and left_before >= half > deadline - _monotonic():
                _stamp(form)

        for form in forms:
            if _monotonic() >= deadline:
                _logger.info("%s: time budget used; the rest waits for the "
                             "next run", label)
                break
            # Skipped forms are not stamped and keep their place in line.
            account_id = form.page_id.account_id.id
            if account_id in dead_accounts:
                continue
            if Log._lead_reads_held(form.page_id, held):
                continue
            state = lookup_states.setdefault(form.page_id.id,
                                             {'deadline': deadline})
            left_before = deadline - _monotonic()
            # A failed form before this one may have hung for most of the
            # budget; a cut then says nothing about this form's length.
            whole_budget = first and left_before >= half
            try:
                with self.env.cr.savepoint():
                    outcome = form._sweep_form(
                        reconcile=reconcile, lookup_state=state,
                        deadline=deadline, whole_budget=whole_budget)
            except MetaRateLimitError as e:
                # Recorded here, after the form's savepoint rolled back, so
                # the pause stays.
                scope = self._note_lead_read_throttle(e, form.page_id, held)
                _logger.info(
                    "%s: Meta throttled the listing of form id=%s; lead reads "
                    "paused for the %s", label, form.id,
                    'app' if scope == 'app' else 'Page')
                if scope == 'app':
                    break
                _failed_turn(form, left_before)
                continue
            except MetaTransientError:
                # Not a lasting failure: the next sweep reads the listing
                # again from the same cursor.
                _logger.info(
                    "%s: listing interrupted on form id=%s; resuming next "
                    "sweep (no token/payload logged)", label, form.id)
                _failed_turn(form, left_before)
                continue
            except MetaAuthError as e:
                dead_accounts.add(account_id)
                # One row, on the form where the token failed.
                Log._record_failure(
                    False, 'cron', _meta_error_summary(e, 'listing'),
                    page=form.page_id, form=form)
                _failed_turn(form, left_before)
                continue
            except MetaPermanentError as e:
                Log._record_failure(
                    False, 'cron', _meta_error_summary(e, 'listing'),
                    page=form.page_id, form=form)
                _failed_turn(form, left_before)
                continue
            except Exception as e:
                _logger.warning(
                    "%s: unexpected error on form id=%s (%s) (no "
                    "token/payload logged)", label, form.id, type(e).__name__)
                Log._record_failure(
                    False, 'cron', 'Unexpected backfill error',
                    page=form.page_id, form=form)
                _failed_turn(form, left_before)
                continue
            # Same turn rule for both jobs: a form that had the whole budget
            # goes to the back even when cut, any other form cut by the
            # budget keeps its place.
            if _turn_done(outcome, whole_budget):
                _stamp(form)
            elif outcome['stop'] in _NO_TURN_STOPS:
                _failed_turn(form, left_before)
            if whole_budget and (outcome['deadline_hit']
                                 or outcome['stop'] == 'budget'):
                if reconcile:
                    _logger.warning(
                        "Meta reconcile: form id=%s did not fit in one run's "
                        "time budget; it resumes from where it stopped on its "
                        "next turn (no token/payload logged)", form.id)
                else:
                    _logger.info(
                        "Meta backfill: form id=%s used the run's time "
                        "budget; it continues next round (no token/payload "
                        "logged)", form.id)
            # A form stopped on one of its leads keeps its place like one
            # whose listing raised, so it must not use up the turn either.
            if outcome['stop'] not in _NO_TURN_STOPS:
                first = False
            # Written here, after the form's savepoint, like the rows above.
            if outcome['stop'] == 'cutoff':
                Log._record_failure(
                    False, 'cron',
                    '%d leads in a row failed; %s' % (
                        const.BACKFILL_FAILURE_STREAK,
                        _RECONCILE_STOP_TEXT if reconcile
                        else 'backfill stopped for this form'),
                    page=form.page_id, form=form)
            elif outcome['stop'] == 'auth':
                dead_accounts.add(account_id)
                # A listing page that failed has no lead id, so the row
                # lands on the form like a listing that failed up front.
                scope = 'listing' if outcome.get('listing_error') else 'lead'
                Log._record_failure(
                    outcome['leadgen_id'], 'cron',
                    _meta_error_summary(outcome['error'], scope),
                    page=form.page_id, form=form)
            elif outcome['stop'] == 'listing_permanent':
                Log._record_failure(
                    False, 'cron',
                    _meta_error_summary(outcome['error'], 'listing'),
                    page=form.page_id, form=form)
            # Keyed on a truncated listing only: a cut by our own budget says
            # nothing about Meta's listing.
            if not reconcile and outcome['truncated']:
                if not outcome.get('budget_promoted'):
                    _logger.warning(
                        "Meta backfill: lead listing for form id=%s was "
                        "incomplete; cursor held (no token/payload logged)",
                        form.id)
                # One open lead-less row per form, so a cut-off row written
                # just above keeps its slot: that one points at the thing to
                # fix first, and the stuck row comes back next sweep.
                if form.backfill_incomplete_count >= \
                        const.BACKFILL_STUCK_SWEEPS and \
                        outcome['stop'] != 'cutoff':
                    Log._record_failure(
                        False, 'cron',
                        'Lead listing incomplete; backfill cursor held',
                        page=form.page_id, form=form)
            if outcome['complete']:
                complete_forms |= form
            if outcome['stop'] == 'rate_limit':
                scope = self._note_lead_read_throttle(
                    outcome['error'], form.page_id, held)
                if scope == 'app':
                    _logger.info("%s: Meta throttled lead reads for the "
                                 "app; the rest waits for the pause", label)
                    break
        if complete_forms:
            self._clear_form_failures(complete_forms, reconcile=reconcile)

    @api.model
    def _note_lead_read_throttle(self, exc, page, held):
        """Pause lead reads for ``page`` or the whole app after a throttle.

        Same wait and scope as the queue drain and Retry use. Returns 'page'
        or 'app'. The ``held`` memo keeps the rest of the run off that Page
        even when the pause could not be saved because the Page row was
        busy.
        """
        Event = self.env['meta.webhook.event']
        until = Event._rate_limit_until(exc, fields.Datetime.now())
        if Event._rate_limit_is_page_scoped(exc):
            page.sudo()._pause_lead_fetch(until)
            held.setdefault('pages', set()).add(page.id)
            return 'page'
        Event._pause_app_lead_fetch(until)
        held['app'] = True
        return 'app'

    def _clear_form_failures(self, forms, reconcile=False):
        """Drop the listing-failure rows of forms that just synced cleanly.

        A form whose whole listing was read and every listed lead processed
        has nothing left to recover from an earlier listing failure. Rows
        with a lead id stay until that lead is retried, and older rows that
        only know their Page stay too: a clean sweep can't prove which lead
        they were about.

        The reconcile keeps its own place, so a clean backfill says nothing
        about where it stopped: a reconcile cut-off row is left to a
        finished reconcile pass, which clears only that row.
        """
        domain = [('status', '=', 'failed'),
                  ('meta_leadgen_id', '=', False),
                  ('form_id', 'in', forms.ids)]
        mark = '%' + _RECONCILE_STOP_TEXT
        if reconcile:
            domain.append(('error_message', '=like', mark))
        else:
            domain += ['|', ('error_message', '=', False),
                       ('error_message', 'not like', _RECONCILE_STOP_TEXT)]
        try:
            with self.env.cr.savepoint():
                self.env['meta.sync.log'].sudo().search(domain).unlink()
        except Exception as e:
            # Leftover rows are harmless; the sweep itself must not fail.
            _logger.warning(
                "Meta backfill: could not clear old form failures (%s)",
                type(e).__name__)

    def _sweep_form(self, reconcile=False, lookup_state=None, deadline=None,
                    whole_budget=False):
        """List this form's leads and ingest them, oldest first.

        Only a failure of the listing itself raises. A problem with one lead
        is recorded on that lead, or stops the form for this run, and comes
        back in the outcome dict.

        An incomplete listing (Meta's paging gave out) holds the cursor and
        switches the form to reading one day at a time until it has caught
        up. A listing cut by our own time budget only holds the cursor,
        unless ``whole_budget`` says this form had the whole run to itself:
        then the listing is simply too long for one run and is sliced too.

        The reconcile sweep reads its own window and never touches the
        cursor; see _reconcile_form.
        """
        self.ensure_one()
        if lookup_state is None:
            lookup_state = {}
        if reconcile:
            return self._reconcile_form(lookup_state, deadline, whole_budget)
        now = datetime.now(timezone.utc)
        # Read before the listing: the lead savepoints below may roll back
        # and the cursor rules must see the state the sweep started from.
        slicing = bool(self.backfill_slicing)
        since_dt, until_dt = self._sweep_window(now)
        leads, truncated, deadline_hit = self._list_leads(
            since_dt, until_dt, deadline=deadline)
        promoted = deadline_hit and whole_budget
        if promoted:
            # This form had the whole budget, so holding the cursor without
            # slicing would repeat the same over-long read every run.
            _logger.warning(
                "Meta backfill: lead listing for form id=%s did not fit in one "
                "run's time budget; reading it in slices (no token/payload "
                "logged)", self.id)
            truncated, deadline_hit = True, False
        if until_dt:
            bound = until_dt.astimezone(timezone.utc).replace(tzinfo=None)
            for lead in leads:
                at = parse_meta_time(lead.get('created_time'))
                if at is not None and at > bound:
                    # Meta ignored the upper bound. The leads are still
                    # ingested; _advance_cursor clips the cursor anyway.
                    _logger.warning(
                        "Meta backfill: lead listing for form id=%s returned "
                        "leads past the slice end; cursor clipped (no "
                        "token/payload logged)", self.id)
                    break
        outcome = self._ingest_listed(leads, lookup_state, deadline)
        outcome['truncated'] = truncated
        outcome['deadline_hit'] = deadline_hit
        outcome['slicing'] = slicing
        # Treated as truncated so the form slices, but it was our budget that
        # ran out, not Meta's listing, and the warning above already says so.
        outcome['budget_promoted'] = promoted
        outcome['complete'] = (not truncated and not deadline_hit
                               and outcome['stop'] is None
                               and not outcome['id_less'])
        self._advance_cursor(outcome, since_dt, until_dt)
        return outcome

    def _reconcile_form(self, lookup_state, deadline=None, whole_budget=False):
        """Read the last reconcile_window_days of this form and ingest what
        is new.

        Leads already stored are skipped before any fetch, and the listing
        is ingested as it streams in, so a read cut short keeps what it got.
        Never reads or writes the backfill cursor or its catch-up state.

        Normally the whole window is one read with no upper bound. A pass
        that does not fit in one run keeps its place and read length on the
        form: a read Meta cut short, or one our budget cut while the form
        still had real time, is retried at half the length (down to
        RECONCILE_MIN_SLICE_HOURS); a whole read moves the place to its end
        and doubles the length back, up to the whole window. The next run
        continues from the stored place.

        A new pass starts at the beginning of the window, or where the last
        pass ended if that is earlier, so forms whose turns come further
        apart than the window still have every day read. Neither start goes
        back more than twice the window.

        The narrowed reads rely on Meta honouring the LESS_THAN bound, like
        the backfill's catch-up slices. If it did not, the later leads would
        still be ingested and the place would still only move to the end of
        the read.
        """
        self.ensure_one()
        min_h = const.RECONCILE_MIN_SLICE_HOURS
        # To the second, so the stored place and the next run's floor agree.
        now = datetime.now(timezone.utc).replace(microsecond=0)
        days = self._reconcile_window_days()
        full = days * 24
        window_start = now - timedelta(days=days)
        # A form that still had at least half the budget when its turn began
        # had a real chance to finish, so a cut means its read is too long,
        # wherever it stood in the queue. Judging by queue position alone
        # made a form stamped as first re-read the same window next turn.
        had_budget = whole_budget or (
            deadline is not None
            and deadline - _monotonic() >= const.BACKFILL_TIME_BUDGET / 2)
        stored_pos = self.reconcile_resume_time
        stored_hours = self.reconcile_slice_hours
        # Never further back than twice the window, so a form left alone
        # for long can't turn its next pass into a read of months.
        floor = now - timedelta(days=2 * days)
        pos = stored_pos.replace(tzinfo=timezone.utc) if stored_pos else None
        if pos is None or pos > now:
            pos = window_start
            # A form whose turns come further apart than the window starts
            # where its last pass ended, so the days in between are read.
            last_end = self.reconcile_pass_end
            last_end = last_end.replace(tzinfo=timezone.utc) if last_end \
                else None
            if last_end and last_end < window_start:
                pos = max(last_end, floor)
                if last_end < floor:
                    _logger.warning(
                        "Meta reconcile: form id=%s was last reconciled more "
                        "than twice its window ago; late leads from before "
                        "%s are not read again", self.id,
                        floor.strftime('%Y-%m-%d %H:%M'))
        elif pos < floor:
            _logger.warning(
                "Meta reconcile: form id=%s had a stale reconcile position; "
                "continuing from %s, late leads from before it are not read "
                "again", self.id, floor.strftime('%Y-%m-%d %H:%M'))
            pos = floor
        hours = max(min_h, min(full, stored_hours)) if stored_hours else full
        overlap = timedelta(seconds=self._backfill_overlap_seconds())
        outcome = {'settled_until': None, 'stop': None, 'error': None,
                   'leadgen_id': False, 'quarantined': [], 'id_less': False,
                   'listing_error': False, 'streak': 0, 'truncated': False,
                   'deadline_hit': False, 'complete': False,
                   'slicing': False}
        streak = 0
        reads = 0
        while True:
            if reads and deadline is not None and _monotonic() >= deadline:
                outcome['stop'] = 'budget'
                break
            reads += 1
            end = pos + timedelta(hours=hours)
            until_dt = end if end < now else None
            leads, status = self._iter_leads(pos - overlap, until_dt,
                                             deadline=deadline)
            part = self._ingest_listed(
                self._drop_known(leads), lookup_state, deadline,
                ordered=False, streak=streak)
            streak = part['streak']
            outcome['quarantined'].extend(part['quarantined'])
            outcome['id_less'] = outcome['id_less'] or part['id_less']
            truncated = bool(status.get('truncated'))
            deadline_hit = bool(status.get('deadline_hit'))
            outcome['truncated'] = outcome['truncated'] or truncated
            outcome['deadline_hit'] = outcome['deadline_hit'] or deadline_hit
            # A read that did not finish would be repeated the same way
            # every day; a shorter one eventually fits. The place never
            # moves on a cut: Meta promises no order inside a listing, so
            # only a whole read proves its window was covered.
            cut = truncated or (deadline_hit and had_budget)
            if part['stop']:
                for key in ('stop', 'error', 'leadgen_id', 'listing_error'):
                    outcome[key] = part[key]
                if cut:
                    hours = max(min_h, hours // 2)
                break
            if cut:
                if hours <= min_h:
                    _logger.warning(
                        "Meta reconcile: form id=%s could not list %s hour(s) "
                        "of leads in one run; it resumes from the same point "
                        "next run (no token/payload logged)", self.id, hours)
                    break
                hours = max(min_h, hours // 2)
                _logger.info(
                    "Meta reconcile: form id=%s listing incomplete; reading "
                    "%s hour(s) at a time", self.id, hours)
                continue
            if deadline_hit:
                # Cut with little budget left: keep the length, the form
                # keeps its place in line and gets the whole budget soon.
                break
            hours = min(full, hours * 2)
            if until_dt is None:
                # Reached now: this pass is done.
                pos = None
                break
            pos = end
        outcome['streak'] = streak
        vals = {}
        if pos is None:
            # Where the next pass picks up if its turn comes late.
            vals['reconcile_pass_end'] = now.replace(tzinfo=None)
            # Read through to the present without a stop, so an earlier
            # reconcile cut-off on this form is behind it.
            outcome['complete'] = outcome['stop'] is None
        new_pos = pos.replace(tzinfo=None) if pos else False
        if new_pos != (stored_pos or False):
            vals['reconcile_resume_time'] = new_pos
        # A form that never needed narrowing keeps an empty length.
        new_hours = hours if hours != full else False
        if new_hours != (stored_hours or False):
            vals['reconcile_slice_hours'] = new_hours
        if vals:
            # sudo: sweep state, guarded against non-superuser writes.
            self.sudo().write(vals)
        return outcome

    def _sweep_window(self, now):
        """Return the (since, until) bounds of the listing; until may be None.

        While a form is catching up, the read ends one slice after the
        cursor (or after the lookback start on a first sync). A slice that
        would end in the future is read without an upper bound, and a clean
        read of it ends the catch-up.
        """
        if self.last_synced_time:
            base = self.last_synced_time.replace(tzinfo=timezone.utc)
            # Re-read a small overlap (default 60s) to catch same-second leads
            # that Meta only exposed after the last sweep.
            since_dt = base - timedelta(
                seconds=self._backfill_overlap_seconds())
        else:
            base = now - timedelta(days=self._backfill_lookback_days())
            since_dt = base
        until_dt = None
        if self.backfill_slicing:
            slice_end = base + timedelta(hours=self._backfill_slice_length())
            if slice_end < now:
                until_dt = slice_end
        return since_dt, until_dt

    def _backfill_slice_length(self):
        """Hours read per catch-up slice, from the stored length or a day."""
        hours = self.backfill_slice_hours or const.BACKFILL_SLICE_HOURS
        return max(const.BACKFILL_MIN_SLICE_HOURS,
                   min(const.BACKFILL_SLICE_HOURS, hours))

    def _list_leads(self, since_dt, until_dt, deadline=None):
        """Read every listing page in the window.

        Returns (leads, truncated, deadline_hit). A listing cut short by our
        own time budget is reported apart from one Meta cut short.
        """
        # Load every page before sorting, so leads from the same second split
        # across pages still come out in order.
        leads, listing = self._iter_leads(since_dt, until_dt,
                                          deadline=deadline)
        leads = list(leads)
        return (leads, bool(listing.get('truncated')),
                bool(listing.get('deadline_hit')))

    def _iter_leads(self, since_dt, until_dt, deadline=None):
        """Return (iterator, status) over the listing pages in the window.

        Pages are requested as the iterator is consumed, and ``status`` gets
        'truncated' / 'deadline_hit' as the paging goes, so read it only
        once the iterator is done. A Meta error from a later page is raised
        by the iterator itself.
        """
        filters = [{'field': 'time_created', 'operator': 'GREATER_THAN',
                    'value': int(since_dt.timestamp())}]
        if until_dt:
            filters.append({'field': 'time_created', 'operator': 'LESS_THAN',
                            'value': int(until_dt.timestamp())})
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
            'filtering': json.dumps(filters),
            'limit': 100,
        }
        # No sudo() needed: the cron runs as a user who can read the
        # admin-only token fields.
        token = self.page_id.access_token
        app_secret = self.page_id.account_id.app_secret
        listing = {}
        leads = self.env['meta.graph.client']._iter_paged(
            token, '%s/leads' % self.form_id, params=params,
            app_secret=app_secret, status=listing, deadline=deadline,
            # So a Page-level throttle reads the Page's own usage estimate.
            buc_id=self.page_id.page_id)
        return leads, listing

    def _drop_known(self, items):
        """Yield the listed leads that are not on a crm.lead yet.

        Only saves the fetch of a lead already stored. Whether a lead gets
        created is still decided by ingest_leadgen and the unique leadgen id,
        and a lead with an open failure row but no crm.lead passes through,
        so its row closes when it ingests. A lead whose crm.lead was deleted
        or merged away is dropped too, so the clean-up sticks. Items without
        an id are kept.
        """
        Lead = self.env['crm.lead'].sudo().with_context(active_test=False)
        Tombstone = self.env['meta.leadgen.tombstone']
        failed = []

        def _source():
            # Hand over what a failing page left in the batch before
            # raising, so the leads already read are still ingested.
            try:
                yield from items
            except _LISTING_ERRORS as e:
                failed.append(e)

        for batch in split_every(_KNOWN_ID_CHUNK, _source(), list):
            ids = [item.get('id') for item in batch if item.get('id')]
            known = set()
            if ids:
                found = Lead.search([('meta_leadgen_id', 'in', ids)])
                known = set(found.mapped('meta_leadgen_id'))
                known |= Tombstone._removed(ids)
            for item in batch:
                if item.get('id') not in known:
                    yield item
        if failed:
            raise failed[0]

    def _ingest_listed(self, leads, lookup_state, deadline=None, ordered=True,
                       streak=0):
        """Ingest the listed leads oldest first, one savepoint per lead.

        A lead Meta refuses, or one that hits an unexpected error, gets its
        own retryable failed row and the loop goes on. Rate-limit, transient
        and auth errors, and database contention, stop the form for this run
        without a row, so the cursor can't pass that lead. After
        BACKFILL_FAILURE_STREAK failures in a row the form stops too; a lead
        that already had an open failure row from an earlier read is not
        counted, so the same bad leads can't hold the cursor for good.

        Returns a dict; 'settled_until' is the newest created_time the cursor
        may move to. The cursor itself is written by _advance_cursor.

        With ``ordered`` False the leads are taken from an iterator as it
        goes, in listing order, and 'settled_until' stays None. A Meta error
        raised by the iterator (a listing page, not a lead) ends the read as
        a stop with 'listing_error' set; the leads ingested before it stay.
        ``streak`` carries the failure streak over from an earlier read of
        the same form, and the outcome returns it.
        """
        outcome = {'settled_until': None, 'stop': None, 'error': None,
                   'leadgen_id': False, 'quarantined': [], 'id_less': False,
                   'listing_error': False, 'streak': streak}
        Ingest = self.env['meta.lead.ingest']
        Log = self.env['meta.sync.log']
        removed = set()
        if ordered:
            timed = [(parse_meta_time(lead.get('created_time')), lead)
                     for lead in leads]
            # Leads without a usable time go first; they never move the
            # cursor.
            timed.sort(key=lambda p: (p[0] is not None,
                                      p[0] or datetime.min))
            pairs = iter(timed)
            # The streamed reads come through _drop_known, which already
            # leaves these out.
            removed = self.env['meta.leadgen.tombstone']._removed(
                [lead.get('id') for _at, lead in timed])
            if removed:
                # One skip row per lead, however often a sweep reads it. The
                # log vacuum keeps that row while the lead is still removed.
                Log._record_removed_skip(sorted(removed), 'cron',
                                         page=self.page_id)
        else:
            pairs = ((None, lead) for lead in leads)
        settled = None
        before_streak = None
        while True:
            try:
                at, lead = next(pairs)
            except StopIteration:
                break
            except _LISTING_ERRORS as e:
                if isinstance(e, MetaRateLimitError):
                    outcome['stop'] = 'rate_limit'
                elif isinstance(e, MetaAuthError):
                    outcome['stop'] = 'auth'
                elif isinstance(e, MetaTransientError):
                    outcome['stop'] = 'transient'
                else:
                    outcome['stop'] = 'listing_permanent'
                outcome['error'] = e
                outcome['leadgen_id'] = False
                outcome['listing_error'] = True
                _logger.info(
                    "Meta backfill: listing of form id=%s failed part way "
                    "(%s); leads read before it are kept (no token/payload "
                    "logged)", self.id, type(e).__name__)
                break
            leadgen_id = lead.get('id')
            if not leadgen_id:
                # Nothing to retry without an id, so it doesn't hold the
                # cursor, but the form isn't complete either.
                outcome['id_less'] = True
                continue
            if deadline is not None and _monotonic() >= deadline:
                outcome['stop'] = 'budget'
                break
            if leadgen_id in removed:
                # Ingested before and since deleted or merged away in Odoo.
                # Nothing to do, so the cursor may pass it.
                if at is not None and (settled is None or at > settled):
                    settled = at
                continue
            try:
                with self.env.cr.savepoint():
                    ingested = Ingest.ingest_leadgen(
                        self.page_id, leadgen_id, trigger='cron', raw=lead,
                        lookup_state=lookup_state)
                    # A lead the sweep finally fetched must not leave its old
                    # failure row or queue entry looking open. If the event
                    # is busy, the drain's healing step closes it instead.
                    Log._reconcile_leadgen(leadgen_id, ingested)
            except MetaLeadRemovedError:
                # Deleted in Odoo after the list was filtered. Not a failure,
                # so the streak is left as it was and the cursor may pass.
                Log._record_removed_skip([leadgen_id], 'cron',
                                         page=self.page_id)
            except (MetaRateLimitError, MetaTransientError,
                    MetaAuthError) as e:
                if isinstance(e, MetaRateLimitError):
                    outcome['stop'] = 'rate_limit'
                elif isinstance(e, MetaAuthError):
                    outcome['stop'] = 'auth'
                else:
                    outcome['stop'] = 'transient'
                outcome['error'] = e
                outcome['leadgen_id'] = leadgen_id
                _logger.info(
                    "Meta backfill: form id=%s stopped on a lead (%s); "
                    "resuming next sweep (no token/payload logged)",
                    self.id, type(e).__name__)
                break
            except Exception as e:
                if _is_db_contention(e):
                    # Another transaction is busy with this lead. Nothing is
                    # wrong with it, so no row; the next sweep tries again.
                    outcome['stop'] = 'transient'
                    outcome['error'] = e
                    outcome['leadgen_id'] = leadgen_id
                    _logger.info(
                        "Meta backfill: form id=%s stopped on a busy lead "
                        "(%s); resuming next sweep", self.id,
                        type(e).__name__)
                    break
                if isinstance(e, MetaPermanentError):
                    message = _meta_error_summary(e, 'lead')
                else:
                    message = 'Unexpected backfill error'
                    _logger.warning(
                        "Meta backfill: unexpected error on a lead of form "
                        "id=%s (%s) (no token/payload logged)",
                        self.id, type(e).__name__)
                # Looked up before the row below is written: a lead that
                # already had an open row failed on an earlier read too.
                repeat = bool(Log.sudo().search_count(
                    [('meta_leadgen_id', '=', leadgen_id),
                     ('status', '=', 'failed')], limit=1))
                # The lead's savepoint is already rolled back, so the row
                # stays.
                Log._record_failure(leadgen_id, 'cron', message,
                                    page=self.page_id, form=self)
                outcome['quarantined'].append(leadgen_id)
                # A repeat is not counted: its row is its way back, and
                # counting it again would stop the form on the same leads
                # every sweep, with nothing an admin could do to move the
                # cursor on.
                if not repeat:
                    if not streak:
                        before_streak = settled
                    streak += 1
                if streak >= const.BACKFILL_FAILURE_STREAK:
                    # Probably not the leads but something systemic. Stop
                    # before the first failure of the run of failures, so
                    # the next sweep reads them again.
                    outcome['stop'] = 'cutoff'
                    settled = before_streak
                    break
            else:
                streak = 0
            # A failed lead has its own row to retry from, so the cursor
            # may pass it.
            if at is not None and (settled is None or at > settled):
                settled = at
        outcome['settled_until'] = settled
        outcome['streak'] = streak
        return outcome

    def _advance_cursor(self, outcome, since_dt, until_dt):
        """Move the cursor forward to what the sweep settled, never back.

        Also keeps the catch-up state: an incomplete listing holds the cursor
        and starts slicing; a sliced read moves the cursor at most to the
        slice end. A slice that is itself incomplete halves the next one,
        down to BACKFILL_MIN_SLICE_HOURS, and clean slices double it back.
        """
        current = self.last_synced_time
        count = self.backfill_incomplete_count or 0
        slicing = outcome.get('slicing', False)
        hours = self._backfill_slice_length()
        new_hours = hours
        vals = {}
        new = None
        if outcome['truncated']:
            # Meta's listing gave out, so older pages may be unread.
            vals['backfill_slicing'] = True
            vals['backfill_incomplete_count'] = count + 1
            if slicing:
                # Reading the same slice again would fail the same way.
                if hours <= const.BACKFILL_MIN_SLICE_HOURS:
                    _logger.warning(
                        "Meta backfill: form id=%s could not list %s hour(s) "
                        "of leads in one run; it reads the same slice again "
                        "next run (no token/payload logged)", self.id, hours)
                new_hours = max(const.BACKFILL_MIN_SLICE_HOURS, hours // 2)
        elif outcome['deadline_hit']:
            # We stopped reading the listing ourselves, so the leads in hand
            # may be the newest ones with older pages unread. That says
            # nothing about Meta's listing, so nothing is counted.
            return
        else:
            # The listing came back whole. The count tracks listing
            # completeness only, so a stop on one lead does not keep it up.
            if count:
                vals['backfill_incomplete_count'] = 0
            settled = outcome['settled_until']
            slice_end = (until_dt.astimezone(timezone.utc).replace(tzinfo=None)
                         if until_dt else None)
            if not slicing:
                new = settled
            elif slice_end is None:
                # Caught up: this read had no upper bound.
                new = settled
                if outcome['stop'] is None:
                    vals['backfill_slicing'] = False
                    new_hours = const.BACKFILL_SLICE_HOURS
            elif outcome['stop'] is None:
                # Meta's time filters are strict, so a lead exactly at the
                # slice end is read by the next slice through the overlap.
                # An empty slice still moves on.
                new = slice_end
                new_hours = min(const.BACKFILL_SLICE_HOURS, hours * 2)
            elif settled:
                new = settled
            if new and slice_end:
                # Never past the slice end, even if Meta ignored the upper
                # bound and returned later leads.
                new = min(new, slice_end)
        # The cursor starts as False, which can't be compared with a datetime.
        if new and (not current or new > current):
            vals['last_synced_time'] = new
        if 'backfill_slicing' in vals and \
                vals['backfill_slicing'] == bool(self.backfill_slicing):
            del vals['backfill_slicing']
        # The default length is stored as empty, like a form that never
        # needed narrowing.
        stored_hours = (new_hours if new_hours != const.BACKFILL_SLICE_HOURS
                        else False)
        if stored_hours != (self.backfill_slice_hours or False):
            vals['backfill_slice_hours'] = stored_hours
        if vals:
            # sudo: the sweep state is guarded against non-superuser writes,
            # whichever user the sweep runs as.
            self.sudo().write(vals)

    def _backfill_lookback_days(self):
        """Days to look back on a form's first sync (default 30).

        Set with ir.config_parameter meta_lead_ads.backfill_lookback_days.
        Capped at ten years so a huge value can't overflow the date
        arithmetic.
        """
        raw = self.env['ir.config_parameter'].sudo().get_str(
            'meta_lead_ads.backfill_lookback_days', default='30')
        try:
            return max(1, min(3650, int(raw)))
        except (TypeError, ValueError):
            return 30

    def _reconcile_window_days(self):
        """Days the daily reconcile sweep reads again (default 7).

        Set with ir.config_parameter meta_lead_ads.reconcile_window_days.
        Kept between 1 and 90: a wider read only spends Meta quota on leads
        that are long stored.
        """
        raw = self.env['ir.config_parameter'].sudo().get_str(
            'meta_lead_ads.reconcile_window_days', default='7')
        try:
            return max(1, min(90, int(raw)))
        except (TypeError, ValueError):
            return 7

    def _backfill_overlap_seconds(self):
        """Seconds before the cursor to re-read each sweep (default 60).

        Set with ir.config_parameter meta_lead_ads.backfill_overlap_seconds.
        Never below 1: Meta's time filters are strict, so with 0 a lead in
        the cursor's own second would never be read again. Capped at one
        day so a huge value can't overflow the date arithmetic.
        """
        raw = self.env['ir.config_parameter'].sudo().get_str(
            'meta_lead_ads.backfill_overlap_seconds', default='60')
        try:
            return max(1, min(86400, int(raw)))
        except (TypeError, ValueError):
            return 60
