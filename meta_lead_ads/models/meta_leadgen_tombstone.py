# Meta Lead Ads Integration for Odoo — © 2026 CoreSys Builders. All rights reserved.
# Licensed under the Odoo Proprietary License v1.0 (OPL-1).
# Unauthorized copying, redistribution, or resale of this software, in whole or in
# part, via any medium, is strictly prohibited and constitutes a license violation.
# OPL-1: https://www.odoo.com/documentation/20.0/legal/licenses.html#odoo-apps

# Meta lead ids whose crm.lead was deleted in Odoo, merges included. The
# sweeps re-read days of leads at a time and would otherwise bring such a
# lead back as new, undoing the clean-up. Webhook re-sends and Retry check
# it too. A record also says whether the lead was merged away, so Restore
# never undoes a merge. It holds no personal data, only Meta ids and links
# to Odoo records.
from datetime import timedelta

from psycopg2.errors import LockNotAvailable, SerializationFailure

from odoo import _, api, fields, models
from odoo.exceptions import AccessError, UserError
from odoo.tools import format_datetime

from .exceptions import (MetaGraphError, MetaIngestRaceError,
                         MetaLeadRemovedError, MetaRateLimitError)

# Kept well past the furthest the reconcile can reach back (twice its
# longest window), so a removed lead can't come back through a late pass.
_KEEP_DAYS = 200


class MetaLeadgenTombstone(models.Model):
    _name = 'meta.leadgen.tombstone'
    _description = 'Removed Meta Lead'

    leadgen_id = fields.Char(string='Meta Lead ID', required=True,
                             readonly=True)
    page_id = fields.Many2one('meta.page', string='Page', ondelete='set null',
                              readonly=True)
    form_id = fields.Many2one('meta.lead.form', string='Form',
                              ondelete='set null', readonly=True)
    company_id = fields.Many2one('res.company', string='Company',
                                 ondelete='set null', readonly=True,
                                 index=True)
    # Empty on records written before the reason was kept. 'erased' is a
    # Meta lead whose personal data was erased (the lead itself, a lead
    # merged into it, or rows that never became a lead), which must not be
    # fetched back from Meta. An erased record may keep its merge link.
    removal_reason = fields.Selection(
        [('deleted', 'Deleted'), ('merged', 'Merged'), ('erased', 'Erased')],
        string='Reason', readonly=True)
    # Odoo reads the name of a linked record with elevated rights, and a
    # lead's name is usually a person's name. So the link is for Settings
    # admins and code only, and the list and form show the plain id.
    merged_into_id = fields.Many2one('crm.lead', string='Merged Into Lead',
                                     ondelete='set null', readonly=True,
                                     groups='base.group_system')
    merged_into_ref = fields.Char(string='Merged Into',
                                  compute='_compute_merged_into_ref')
    expires_on = fields.Datetime(string='Kept Until',
                                 compute='_compute_expires_on')

    _leadgen_id_uniq = models.Constraint(
        'unique(leadgen_id)',
        'This Meta lead is already recorded as removed.',
    )

    @api.depends('create_date')
    def _compute_expires_on(self):
        for tomb in self:
            tomb.expires_on = (tomb.create_date + timedelta(days=_KEEP_DAYS)
                               if tomb.create_date else False)

    @api.depends('merged_into_id')
    def _compute_merged_into_ref(self):
        for tomb in self:
            head = tomb.sudo().merged_into_id
            tomb.merged_into_ref = '#%d' % head.id if head else False

    @api.model
    def _remember(self, leadgen_ids, details=None):
        """Record the given Meta lead ids as removed from the CRM.

        ``details`` maps a lead id to a dict with ``page_id``, ``form_id``,
        ``company_id``, ``reason`` and ``merged_into``; missing keys stay
        empty.
        """
        details = details or {}
        ids = sorted({lg for lg in leadgen_ids if lg})
        if not ids:
            return
        cols = {key: [details.get(lg, {}).get(key) or None for lg in ids]
                for key in ('page_id', 'form_id', 'company_id', 'reason',
                            'merged_into')}
        # Plain SQL so a second delete of the same id, in this transaction
        # or another, is a no-op instead of a unique violation. The lead a
        # tail was merged into may be gone by now (deleted with it, or
        # earlier), so only a live one is linked.
        self.env.cr.execute("""
            INSERT INTO meta_leadgen_tombstone
                   (leadgen_id, page_id, form_id, company_id, removal_reason,
                    merged_into_id, create_uid, write_uid, create_date,
                    write_date)
            SELECT t.lg, t.page, t.form, t.company, t.reason,
                   (SELECT id FROM crm_lead WHERE id = t.merged),
                   %s, %s, now() at time zone 'UTC', now() at time zone 'UTC'
              FROM unnest(%s::varchar[], %s::int4[], %s::int4[], %s::int4[],
                          %s::varchar[], %s::int4[])
                   AS t(lg, page, form, company, reason, merged)
            ON CONFLICT (leadgen_id) DO NOTHING
        """, (self.env.uid, self.env.uid, ids, cols['page_id'],
              cols['form_id'], cols['company_id'], cols['reason'],
              cols['merged_into']))
        self.invalidate_model()

    @api.model
    def _mark_erased(self, leadgen_ids, details=None):
        """Record the given Meta lead ids as erased, whatever was recorded
        for them before.

        ``details`` maps a lead id to a dict with ``page_id``, ``form_id``
        and ``company_id``; they only fill links an existing record lacks.
        """
        details = details or {}
        ids = sorted({lg for lg in leadgen_ids if lg})
        if not ids:
            return
        cols = {key: [details.get(lg, {}).get(key) or None for lg in ids]
                for key in ('page_id', 'form_id', 'company_id')}
        self.flush_model()
        # A row erase must also close a record written earlier with another
        # reason, or Restore would fetch the data back. The merge link stays
        # as it was.
        self.env.cr.execute("""
            INSERT INTO meta_leadgen_tombstone
                   (leadgen_id, page_id, form_id, company_id, removal_reason,
                    create_uid, write_uid, create_date, write_date)
            SELECT t.lg, t.page, t.form, t.company, 'erased', %s, %s,
                   now() at time zone 'UTC', now() at time zone 'UTC'
              FROM unnest(%s::varchar[], %s::int4[], %s::int4[], %s::int4[])
                   AS t(lg, page, form, company)
            ON CONFLICT (leadgen_id) DO UPDATE SET removal_reason = 'erased',
                   page_id = COALESCE(meta_leadgen_tombstone.page_id,
                                      EXCLUDED.page_id),
                   form_id = COALESCE(meta_leadgen_tombstone.form_id,
                                      EXCLUDED.form_id),
                   company_id = COALESCE(meta_leadgen_tombstone.company_id,
                                         EXCLUDED.company_id),
                   write_uid = EXCLUDED.write_uid,
                   write_date = EXCLUDED.write_date
        """, (self.env.uid, self.env.uid, ids, cols['page_id'],
              cols['form_id'], cols['company_id']))
        self.invalidate_model()

    @api.model
    def _follow_merges(self, new_heads):
        """Move records merged into a lead that is now merged away itself.

        ``new_heads`` maps the id of each lead being merged away to the id
        of the lead it is merged into.
        """
        if not new_heads:
            return
        old_ids, new_ids = zip(*sorted(new_heads.items()))
        # The new lead may be gone already. Those records are left alone and
        # lose the link with the delete, as when the head itself is deleted.
        self.env.cr.execute("""
            UPDATE meta_leadgen_tombstone AS t
               SET merged_into_id = m.new_head, write_uid = %s,
                   write_date = now() at time zone 'UTC'
              FROM unnest(%s::int4[], %s::int4[]) AS m(old_head, new_head)
              JOIN crm_lead AS l ON l.id = m.new_head
             WHERE t.merged_into_id = m.old_head
        """, (self.env.uid, list(old_ids), list(new_ids)))
        self.invalidate_model()

    # ------------------------------------------------------------------ #
    # Restore
    # ------------------------------------------------------------------ #
    def action_restore(self):
        """Drop this record and fetch the lead from Meta again, through the
        normal ingest path, then open it."""
        self.ensure_one()
        self._check_restore_access()
        self._check_not_merged()
        self._check_not_erased()
        page = self._restore_page()
        if not page:
            # Nothing has been written yet, which is what keeps the record.
            raise UserError(self._no_page_message())
        # Calling Meta during a pause only extends it.
        if self.env['meta.sync.log']._lead_reads_held(page, {}):
            raise UserError(_(
                "Meta asked to pause lead reads for now. Resume lead reads on "
                "the Page or in Settings, or try again later."))
        try:
            lead = self._do_restore(page)
        except MetaRateLimitError as e:
            return self._hold_lead_reads(page, e)
        return self.env['meta.lead.ingest']._lead_result_action(
            lead, message=_("The Meta lead was restored. You don't have "
                            "access to open it."))

    def _check_restore_access(self):
        # Restore writes as sudo below, and RPC reaches it directly.
        is_admin = self.env.user.has_group('meta_lead_ads.group_meta_admin')
        if not self.env.su and not is_admin:
            raise AccessError(_("Meta Admin access required."))
        self.check_access('read')

    def _check_not_merged(self):
        # Restoring a merged lead would bring back the duplicate the merge
        # removed. Only the id is shown, never the surviving lead's title.
        # Once that lead is deleted there is nothing left to duplicate, so
        # the record is restored like a deleted one.
        self.ensure_one()
        if self.removal_reason != 'merged':
            return
        head = self.sudo().merged_into_id
        if head:
            raise UserError(_(
                "This lead was merged into lead #%(id)s. Open that lead "
                "instead of restoring this one.", id=head.id))

    def _check_not_erased(self):
        # The personal data was erased on request. Restore fetches the lead
        # from Meta, which would bring it straight back.
        self.ensure_one()
        if self.removal_reason == 'erased':
            raise UserError(_(
                "The personal data of this Meta lead was erased, so it can't "
                "be fetched from Meta again."))

    def _refusal_message(self):
        """Why a removed lead can't be imported, for the current user.

        Who deleted it and when is only told to users who can read this
        record, so call it on the record in the caller's env, not sudo.
        """
        self.ensure_one()
        if not self.env.su and not self.has_access('read'):
            return _("This lead was deleted in Odoo and can't be imported "
                     "again.")
        tomb = self.sudo()
        return _(
            "This lead was deleted in Odoo on %(when)s by %(who)s. Restore it "
            "from Removed Meta Leads if it should come back.",
            when=format_datetime(self.env, tomb.create_date),
            who=tomb.create_uid.name or _("an unknown user"))

    def _restore_page(self):
        """The Page to fetch the lead from, or an empty recordset.

        The links are read as sudo, so a Page the caller can't read is
        skipped. When only such Pages are found the caller is told so,
        instead of meeting a bare access error inside ingest.
        """
        self.ensure_one()
        tomb = self.sudo()
        Log = self.env['meta.sync.log'].sudo()
        log_domain = [('meta_leadgen_id', '=', tomb.leadgen_id),
                      ('page_id', '!=', False)]
        candidates = [
            tomb.page_id,
            tomb.form_id.page_id,
            # A Page that once returned the lead is the best guess.
            Log.search(log_domain + [('status', '=', 'success')],
                       order='id desc', limit=1).page_id,
            Log.search(log_domain, order='id desc', limit=1).page_id,
        ]
        meta_page_id = self._event_page_id()
        if meta_page_id:
            candidates.append(self.env['meta.page'].sudo().search(
                [('page_id', '=', meta_page_id)], limit=1))
        found = [page for page in candidates if page]
        for page in found:
            page = page.with_env(self.env)
            if page.has_access('read'):
                return page
        if found:
            raise UserError(_(
                "The Page this lead came from is not connected, or sends "
                "leads to a company you can't access."))
        return self.env['meta.page']

    def _event_page_id(self):
        """The Meta Page id of the newest webhook event for this lead."""
        event = self.env['meta.webhook.event'].sudo().search(
            [('leadgen_id', '=', self.sudo().leadgen_id),
             ('page_id', '!=', False)], order='id desc', limit=1)
        return event.page_id

    def _no_page_message(self):
        meta_page_id = self._event_page_id()
        # Only a Page id Odoo doesn't know at all is named. A known Page in
        # another company is reported as out of reach instead.
        if meta_page_id and not self.env['meta.page'].sudo().with_context(
                active_test=False).search_count(
                    [('page_id', '=', meta_page_id)], limit=1):
            return _(
                "Odoo doesn't know which Meta Page this lead came from, so it "
                "can't fetch it again. The removed record is kept. Use CRM > "
                "Configuration > Ingest Meta Lead by ID with the Page the "
                "lead came from (Meta Page id %(page)s).", page=meta_page_id)
        return _(
            "Odoo doesn't know which Meta Page this lead came from, so it "
            "can't fetch it again. The removed record is kept. Use CRM > "
            "Configuration > Ingest Meta Lead by ID with the Page the lead "
            "came from.")

    def _do_restore(self, page):
        # Read before the record goes. The unlink and the import share one
        # savepoint, so any failure brings the record back and the lead
        # stays protected.
        self.ensure_one()
        tomb = self.sudo()
        leadgen_id = tomb.leadgen_id
        busy = _("This lead is being imported or restored right now. Try "
                 "again in a moment.")
        try:
            with self.env.cr.savepoint():
                tomb.unlink()
                # As the caller, so ingest checks Meta Admin rights and
                # Page access itself.
                return self.env['meta.lead.ingest'].ingest_leadgen(
                    page, leadgen_id, trigger='manual')
        except MetaLeadRemovedError:
            raise UserError(self._refusal_message()) from None
        except MetaIngestRaceError:
            raise UserError(busy) from None
        except MetaRateLimitError:
            # The caller saves the pause, which an error would roll back.
            raise
        except MetaGraphError as e:
            # The Graph client keeps tokens out of its error text.
            raise UserError(_("Meta did not return this lead: %s",
                              str(e))) from None
        except (SerializationFailure, LockNotAvailable):
            # Another Restore of the same record got there first.
            raise UserError(busy) from None

    def _hold_lead_reads(self, page, exc):
        # Same wait as the drain and Retry, so further clicks don't keep
        # hitting a throttled Page. A notification rather than an error, so
        # the pause is saved. The savepoint has already put the record back.
        Event = self.env['meta.webhook.event'].sudo()
        until = Event._rate_limit_until(exc, fields.Datetime.now())
        if Event._rate_limit_is_page_scoped(exc):
            page.sudo()._pause_lead_fetch(until)
        else:
            Event._pause_app_lead_fetch(until)
        return {
            'type': 'ir.actions.client',
            'tag': 'display_notification',
            'params': {
                'message': _(
                    "Meta asked to pause lead reads until %(when)s. The "
                    "removed record is kept; restore it again after that.",
                    when=format_datetime(self.env, until)),
                'type': 'warning',
                'sticky': False,
            },
        }

    @api.model
    def _removed(self, leadgen_ids):
        """Return the subset of ``leadgen_ids`` recorded as removed."""
        ids = [lg for lg in leadgen_ids if lg]
        if not ids:
            return set()
        found = self.sudo().search([('leadgen_id', 'in', ids)])
        return set(found.mapped('leadgen_id'))

    @api.model
    def _vacuum(self, batch=5000):
        """Drop records too old for any sweep to read their lead again."""
        cutoff = fields.Datetime.now() - timedelta(days=_KEEP_DAYS)
        self.sudo().search([('create_date', '<', cutoff)],
                           limit=batch).unlink()
