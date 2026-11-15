# Meta Lead Ads Integration for Odoo — © 2026 CoreSys Builders. All rights reserved.
# Licensed under the Odoo Proprietary License v1.0 (OPL-1).
# Unauthorized copying, redistribution, or resale of this software, in whole or in
# part, via any medium, is strictly prohibited and constitutes a license violation.
# OPL-1: https://www.odoo.com/documentation/18.0/legal/licenses.html#odoo-apps

import json
import logging
import re
from collections import defaultdict

import lxml.html
from lxml import etree
from markupsafe import Markup, escape

from odoo import _, api, fields, models, Command
from odoo.exceptions import AccessError, UserError
from odoo.tools import email_normalize, format_date, is_html_empty, sql
from odoo.tools.translate import code_translations

from .const import (
    RETENTION_DEFAULTS, SYNC_LOG_RETENTION_PARAM, is_blocked_target)
from .meta_lead_ingest import (
    ALERT_MESSAGE_TAG, CANONICAL, QA_CLASS, QA_HEADING)

_logger = logging.getLogger(__name__)

# Cursor cache key: maps each merged-away lead id to the id of the lead it
# was merged into.
_MERGE_HEADS = 'meta_lead_ads.merge_heads'

# The erase runs in one transaction, so a list call is capped.
_META_ERASE_LIMIT = 200
# Never cleared by the erase, besides the fields a form answer may never
# fill (const.is_blocked_target) and the module's own meta_* fields.
_META_ERASE_KEEP = frozenset({'type', 'company_id', 'active'})
# Contact and address fields cleared on every erased lead, whatever filled
# them. mobile only exists on some versions.
_META_ERASE_CONTACT = frozenset(CANONICAL.values()) | {
    'mobile', 'street', 'street2', 'zip', 'city', 'state_id', 'country_id'}
# Compared by their digits, and by the address, when the erase looks for a
# lead's value in the stored Meta data.
_META_PHONE_FIELDS = frozenset({'phone', 'mobile'})
_META_EMAIL_FIELDS = frozenset({'email_from', 'email_cc'})
# The class on crm's merge note (template crm.crm_lead_merge_summary).
_CRM_MERGE_NOTE_CLASS = 'crm_lead_merge_summary'
# The subjects crm gives the messages it moves in a merge
# (_merge_dependences_history), as written in crm's source.
_CRM_MOVED_SUBJECTS = ('From %(source_name)s: %(source_subject)s',
                       'From %(source_name)s')
_CRM_SUBJECT_TOKEN = re.compile(r'%\((source_name|source_subject)\)s')
# Elements that start a line of their own inside a Meta answer block.
_META_BLOCK_TAGS = frozenset({
    'address', 'article', 'blockquote', 'div', 'dl', 'figure', 'footer',
    'h1', 'h2', 'h3', 'h4', 'h5', 'h6', 'header', 'hr', 'li', 'ol', 'p',
    'pre', 'section', 'table', 'ul'})


def _meta_squash(text):
    # The editor may turn spaces into non-breaking ones; str.split() treats
    # those as spaces too.
    return ' '.join((text or '').split())


class CrmLead(models.Model):
    _inherit = 'crm.lead'

    # Idempotency key; NULL on non-Meta leads. The UNIQUE constraint below
    # creates the index. copy=False so a duplicated lead doesn't clash.
    meta_leadgen_id = fields.Char(string='Meta Lead ID', copy=False)

    # True while ``name`` is still the generated title. A manual rename clears
    # it (see write), and the retroactive rename only touches leads where it
    # is set, so hand-edited titles are never overwritten.
    meta_name_is_generated = fields.Boolean(
        string='Meta Title Auto-Generated', default=False, copy=False)

    # Links to Meta records. Only ingest sets them, as sudo, but the ORM
    # accepts any id here and the lead form would then show that record's
    # name, even from another company.
    _meta_link_fields = ('meta_account_id', 'meta_page_id_ref',
                         'meta_form_id_ref')

    # The name retry runs as superuser on these, so only ingest (as sudo)
    # may set them.
    _meta_ingest_only_fields = (
        'meta_campaign_id', 'meta_adset_id', 'meta_ad_id', 'meta_form_id',
        'meta_page_id', 'meta_attribution_pending',
        'meta_attribution_attempts', 'meta_attribution_retry_after',
        'meta_filled_fields', 'meta_personal_data_erased_at')

    # Set only by the erase, never by RPC.
    meta_personal_data_erased_at = fields.Datetime(
        string='Personal Data Erased On', copy=False, readonly=True)

    # The lead fields Meta filled on this lead, by technical name only, so
    # an erase still finds them after a mapping is retargeted or deleted.
    # Stored as ',city,phone,' (',' when nothing was filled). Empty means
    # the lead was not tracked: a plain lead, or one ingested before this
    # field existed.
    meta_filled_fields = fields.Char(string='Fields Filled by Meta',
                                     copy=False, readonly=True)

    # Meta leads were merged into this lead. Only the form reads it, to
    # offer the erase on a lead without a Meta lead id of its own. Meta
    # Admins only: it tells that a Meta lead was merged in, and maybe
    # erased, even once the lead's history no longer shows it.
    meta_holds_merged_leads = fields.Boolean(
        string='Holds Merged Meta Leads',
        compute='_compute_meta_holds_merged_leads',
        groups='meta_lead_ads.group_meta_admin')

    def _compute_meta_holds_merged_leads(self):
        held = set(self._origin._meta_holder_ids())
        for lead in self:
            lead.meta_holds_merged_leads = lead._origin.id in held

    @api.model_create_multi
    def create(self, vals_list):
        if self.env.su:
            return super().create(vals_list)
        for vals in vals_list:
            self._check_meta_ingest_only(vals, creating=True)
            self._check_meta_links(vals)
        return super(CrmLead, self._without_meta_context_defaults()).create(
            vals_list)

    def _without_meta_context_defaults(self):
        # create fills missing fields from default_<field> context keys,
        # which any caller can send over RPC, so the checks on vals alone
        # would let a user plant these fields that way. Drop such keys, and
        # link defaults pointing at records the user can't read.
        context = self.env.context
        guarded = ('meta_leadgen_id',) + self._meta_ingest_only_fields
        dropped = ['default_' + name for name in guarded
                   if 'default_' + name in context]
        for name in self._meta_link_fields:
            value = context.get('default_' + name)
            if value and (not isinstance(value, int)
                          or not self._meta_link_readable(name, value)):
                dropped.append('default_' + name)
        if not dropped:
            return self
        return self.with_context(
            {key: val for key, val in context.items() if key not in dropped})

    def write(self, vals):
        """Clear meta_name_is_generated when the name is edited by hand.

        Writes that set the flag explicitly (the retroactive rename) keep it."""
        if not self.env.su:
            self._check_meta_ingest_only(vals)
            self._check_meta_links(vals, current=self)
        if 'name' in vals and 'meta_name_is_generated' not in vals:
            vals = dict(vals, meta_name_is_generated=False)
        return super().write(vals)

    def _merge_opportunity(self, *args, **kwargs):
        # A merge that fails inside a savepoint leaves its tails in place,
        # so drop what it recorded. Otherwise a later plain delete of a tail
        # would count as a merge and Restore would refuse it.
        cache = self.env.cr.cache
        before = dict(cache.get(_MERGE_HEADS, {}))
        snapshot = self._meta_merge_snapshot()
        try:
            head = super()._merge_opportunity(*args, **kwargs)
        except Exception:
            cache[_MERGE_HEADS] = before
            raise
        if snapshot:
            head._meta_note_merge_copies(snapshot)
        return head

    @api.model
    def _meta_merge_value(self, lead, name):
        value = lead[name]
        if isinstance(value, models.BaseModel):
            return tuple(value.ids)
        return value

    def _meta_merge_snapshot(self):
        """What _meta_note_merge_copies needs once these leads are merged,
        or None when none of them holds Meta data.

        For each lead holding Meta data, the fields that count as Meta's
        on it: on a Meta lead what its own erase would clear, on a lead
        Meta leads were merged into the fields recorded for it. And the
        value of every field crm merges, read before the merge."""
        leads = self.sudo()
        held = set(leads._meta_holder_ids())
        targets = None
        promoted = None
        meta = {}
        tracked_ids = set()
        for lead in leads:
            filled, tracked = lead._meta_filled_names()
            if tracked:
                tracked_ids.add(lead.id)
            if lead.meta_leadgen_id:
                if targets is None:
                    targets = leads._meta_mapping_targets()
                    promoted = self._meta_promoted_fields()
                names = (_META_ERASE_CONTACT
                         | targets[lead.meta_form_id_ref.id]
                         | (filled if tracked else promoted))
            elif tracked:
                names = set(filled)
            elif lead.id in held:
                names = _META_ERASE_CONTACT | self._meta_promoted_fields()
            else:
                continue
            meta[lead.id] = set(names)
        if not meta:
            return None
        specific = self._merge_get_fields_specific()
        fnames = [name for name in self._merge_get_fields()
                  if name in self._fields and name not in specific]
        values = {lead.id: {name: self._meta_merge_value(lead, name)
                            for name in fnames}
                  for lead in leads}
        return {'meta': meta, 'values': values, 'tracked': tracked_ids,
                'held': held}

    def _meta_note_merge_copies(self, snapshot):
        """Record on this merge head, when it isn't a Meta lead itself,
        the fields crm filled from a Meta lead merged into it.

        The head's own values aren't Meta's, so a field counts only when
        the merge changed it to the value a Meta lead held. A Meta head
        gets the merged leads' lists in _meta_merge_filled instead, since
        its erase clears the contact fields whatever filled them."""
        self.ensure_one()
        head = self.sudo()
        before = snapshot['values'].get(head.id)
        if head.meta_leadgen_id or before is None:
            return
        tails = {lead_id: names for lead_id, names in snapshot['meta'].items()
                 if lead_id != head.id}
        if not tails:
            return
        if head.id in snapshot['tracked']:
            names = set(snapshot['meta'][head.id])
        elif head.id in snapshot['held']:
            # Earlier merges into it were never recorded, so its erase
            # keeps checking each field against the stored Meta data.
            return
        else:
            names = set()
        for lead_id, tail_names in tails.items():
            tail_values = snapshot['values'][lead_id]
            for name in tail_names:
                if name not in before:
                    continue
                now = self._meta_merge_value(head, name)
                if now and now != before[name] and now == tail_values[name]:
                    names.add(name)
        head.with_context(mail_notrack=True).write(
            {'meta_filled_fields': self._meta_format_filled(names)})

    def _merge_dependences(self, opportunities):
        # self is the lead the others are merged into. crm unlinks them
        # later in the same transaction, sometimes much later (lead
        # assignment), so the link is kept on the cursor until commit.
        # cr.cache rather than precommit.data: the latter is emptied by any
        # flush that runs a pending hook, such as mail tracking.
        res = super()._merge_dependences(opportunities)
        self.env.cr.cache.setdefault(_MERGE_HEADS, {}).update(
            {tail.id: self.id for tail in opportunities})
        # Answers are deleted with their lead, so move the tails' answers
        # over. Sudo, since whoever merges may not see Meta answers at all.
        self.env['meta.lead.answer'].sudo().search(
            [('lead_id', 'in', opportunities.ids)]).write({'lead_id': self.id})
        self._meta_merge_filled(opportunities)
        return res

    def _meta_merge_filled(self, opportunities):
        # crm merge copies field values from the tails onto the head, so
        # their inventories go with them. A legacy lead among them may have
        # brought values nobody recorded, so the head becomes legacy too.
        # A head that isn't a Meta lead keeps its own values, so only the
        # fields crm copies onto it count; _merge_opportunity records those
        # once crm has written them.
        self.ensure_one()
        if not self.sudo().meta_leadgen_id:
            return
        leads = (self | opportunities).sudo().filtered(
            lambda l: l.meta_leadgen_id or l.meta_filled_fields)
        if not leads:
            return
        if any(l.meta_leadgen_id and not l.meta_filled_fields for l in leads):
            value = False
        else:
            names = set()
            for lead in leads:
                names |= lead._meta_filled_names()[0]
            value = self._meta_format_filled(names)
        self.sudo().with_context(mail_notrack=True).write(
            {'meta_filled_fields': value})

    @api.model
    def _meta_format_filled(self, names):
        # Wrapped in commas so a name can be matched as ',name,'. A single
        # comma, not an empty string, so the lead still counts as tracked.
        names = sorted(set(names))
        return ',%s,' % ','.join(names) if names else ','

    def _meta_filled_names(self):
        """Return (names, tracked): the fields Meta filled on this lead and
        whether the lead keeps that list at all."""
        self.ensure_one()
        stored = self.sudo().meta_filled_fields
        names = frozenset(n for n in (stored or '').split(',') if n)
        return names, bool(stored)

    def _meta_union_filled(self, names):
        """The inventory value after Meta filled ``names`` on this lead, or
        None when the lead should stay untracked."""
        self.ensure_one()
        stored, tracked = self._meta_filled_names()
        if tracked:
            return self._meta_format_filled(stored | set(names))
        if self.sudo().meta_leadgen_id:
            # A legacy Meta lead stays legacy, so the erase keeps its
            # wider sweep over the fields nobody recorded.
            return None
        # A plain lead's own values aren't Meta's.
        return self._meta_format_filled(names)

    def unlink(self):
        # Every automatic path checks these ids, so a lead someone deleted
        # or merged away doesn't come back, and a merged one is never
        # restored over its merge. Only ids and links are kept.
        heads = self.env.cr.cache.get(_MERGE_HEADS, {})
        details = {}
        for lead in self.sudo():
            if not lead.meta_leadgen_id:
                continue
            head = heads.get(lead.id)
            company = lead.company_id or lead.meta_page_id_ref.lead_company_id
            # An erased lead stays erased when it is merged away, with the
            # merge link kept. A merged record could be restored from Meta
            # once the lead it went into is deleted.
            if lead.meta_personal_data_erased_at:
                reason = 'erased'
            elif head:
                reason = 'merged'
            else:
                reason = 'deleted'
            details[lead.meta_leadgen_id] = {
                'page_id': lead.meta_page_id_ref.id or None,
                'form_id': lead.meta_form_id_ref.id or None,
                'company_id': company.id or None,
                'reason': reason,
                'merged_into': head or None,
            }
        self._meta_removed_trace(details)
        # A deleted lead takes its stored Meta payloads with it. A merged
        # one keeps them: its data lives on in the lead it went into, and
        # the copies go when that one is erased or deleted. Leads merged
        # into a deleted one are read now, since the delete empties their
        # merged_into_id link.
        deleted = self.filtered(lambda l: l.id not in heads)
        scrub_ids = [lg for lg, vals in details.items()
                     if vals['reason'] != 'merged']
        scrub_ids += deleted._meta_merged_in_leadgen_ids()
        # A lead merged away may itself hold earlier merges. Point those
        # records at the lead it went into before the delete clears the
        # link, or they could never be opened or restored.
        moved = {lead.id: heads[lead.id] for lead in self if lead.id in heads}
        if moved:
            self.env['meta.leadgen.tombstone'].sudo()._follow_merges(moved)
        res = super().unlink()
        if details:
            self.env['meta.leadgen.tombstone'].sudo()._remember(
                list(details), details=details)
        self.env['crm.lead']._meta_clear_payloads(scrub_ids)
        return res

    def _meta_removed_trace(self, details):
        # A lead with neither a company nor a Page link (ingested before the
        # links were kept, or whose company was cleared later) would leave a
        # record only Settings admins can see. The sync log or the webhook
        # event usually still knows the Page, the same sources the upgrade
        # uses for older records.
        missing = [lg for lg, vals in details.items() if not vals['company_id']]
        if not missing:
            return
        found = {}
        logs = self.env['meta.sync.log'].sudo().search(
            [('meta_leadgen_id', 'in', missing), ('page_id', '!=', False)],
            order='id desc')
        for log in logs:
            found.setdefault(log.meta_leadgen_id,
                             (log.page_id, log.form_id, log.company_id))
        rest = [lg for lg in missing if lg not in found]
        if rest:
            Page = self.env['meta.page'].sudo().with_context(active_test=False)
            events = self.env['meta.webhook.event'].sudo().search(
                [('leadgen_id', 'in', rest), ('page_id', '!=', False)],
                order='id desc')
            for event in events:
                if event.leadgen_id in found:
                    continue
                page = Page.search([('page_id', '=', event.page_id)], limit=1)
                if page or event.company_id:
                    found[event.leadgen_id] = (
                        page, self.env['meta.lead.form'], event.company_id)
        for lg, (page, form, company) in found.items():
            vals = details[lg]
            vals['page_id'] = vals['page_id'] or page.id or None
            vals['form_id'] = vals['form_id'] or form.id or None
            vals['company_id'] = (company or page.lead_company_id).id or None

    def copy_data(self, default=None):
        # A lead routed through another company's account keeps that
        # account, which its own users can't read. Drop such links on a
        # copy instead of refusing the copy.
        vals_list = super().copy_data(default=default)
        if not self.env.su:
            for vals in vals_list:
                for name in self._meta_link_fields:
                    if vals.get(name) and not self._meta_link_readable(
                            name, vals[name]):
                        vals[name] = False
                # The copy wasn't ingested from Meta, so it gets no Meta ids
                # and is never retried.
                for name in self._meta_ingest_only_fields:
                    vals[name] = False
        return vals_list

    def _check_meta_ingest_only(self, vals, creating=False):
        # The form sends empty defaults on create, so only real values are
        # refused there. It never sends these fields on write.
        if creating:
            planted = [name for name in self._meta_ingest_only_fields
                       if vals.get(name)]
        else:
            planted = [name for name in self._meta_ingest_only_fields
                       if name in vals]
        if planted:
            raise AccessError(_(
                "Meta attribution fields are set by the integration only."))
        # The Meta lead id decides whether a lead is new, so a hand-set one
        # would bring back a lead deleted in Odoo, or claim an id before
        # Meta sends it. Only ingest, as sudo, sets it.
        if (vals.get('meta_leadgen_id') if creating
                else 'meta_leadgen_id' in vals):
            raise AccessError(_(
                "The Meta Lead ID is set by the integration only."))

    @api.depends('user_id', 'team_id', 'partner_id')
    def _compute_company_id(self):
        # crm empties the company of a lead with no salesperson once its
        # customer or team changes, and an empty company shares the lead
        # with every company. A Meta lead's company comes from its Page, so
        # it is kept. Moves to another company are left to crm.
        kept = {lead: lead.company_id for lead in self
                if lead.meta_leadgen_id and lead.company_id
                and not lead.user_id}
        super()._compute_company_id()
        for lead, company in kept.items():
            if not lead.company_id:
                lead.company_id = company

    def _meta_link_readable(self, name, value):
        if isinstance(value, models.BaseModel):
            value = value.id
        comodel = self.env[self._fields[name].comodel_name]
        return comodel.browse(value).has_access('read')

    def _check_meta_links(self, vals, current=None):
        for name in self._meta_link_fields:
            value = vals.get(name)
            if not value:
                continue
            if isinstance(value, models.BaseModel):
                value = value.id
            # Writing back the value a lead already has changes nothing.
            if current and all(rec[name].id == value
                               for rec in current.sudo()):
                continue
            if not self._meta_link_readable(name, value):
                raise AccessError(_(
                    "You can only link Meta records you can access."))

    # Stored, indexed phone key so dedup can match in SQL instead of
    # normalizing every open lead in Python. A number without a country code
    # is read in the lead's country, else in its company's country, using the
    # same helper as the ingest lookup. Changing a company's country
    # recomputes all its leads; that is a rare admin action. The depends goes
    # through the partner because the company's own country field isn't
    # stored.
    meta_phone_normalized = fields.Char(
        string='Normalized Phone (Meta dedup)',
        compute='_compute_meta_phone_normalized', store=True, index=True,
        copy=False)

    @api.depends('phone', 'country_id', 'company_id', 'company_id.partner_id.country_id')
    def _compute_meta_phone_normalized(self):
        Ingest = self.env['meta.lead.ingest']
        for lead in self:
            country = lead.country_id or lead.company_id.partner_id.country_id
            lead.meta_phone_normalized = Ingest._phone_key(lead.phone, country) or False

    def action_reset_meta_attribution_retry(self):
        """Start the automatic Meta name retries again for these leads.

        For leads that ran out of tries or were paused after Meta refused
        their Page's token. No Graph call is made here; the next scheduled
        run picks the leads up."""
        if not self.env.user.has_group('meta_lead_ads.group_meta_admin'):
            raise AccessError(_("Meta Admin access required."))
        # Meta Admin doesn't imply CRM rights, but the caller must at least
        # be able to see the leads.
        self.check_access('read')
        # Only the two retry counters are written, so sudo is enough here
        # and a Meta Admin without CRM write rights can still restart.
        self.filtered('meta_attribution_pending').sudo().write({
            'meta_attribution_attempts': 0,
            'meta_attribution_retry_after': False,
        })
        return True

    # ------------------------------------------------------------------ #
    # Personal data erase
    # ------------------------------------------------------------------ #
    def action_meta_erase_personal_data(self):
        """Remove the personal data Meta brought into these leads.

        The leads stay, with their stage, salesperson, revenue and Meta
        attribution. Only a Meta Admin who can read the leads may do this.
        A lead erased before can be erased again: a merge or a salesperson
        may have put personal data back on it since. A lead that Meta
        leads were merged into is accepted without a Meta lead id of its
        own; there only what can be shown to come from those Meta leads
        is erased."""
        if not self.env.su and not self.env.user.has_group(
                'meta_lead_ads.group_meta_admin'):
            raise AccessError(_("Meta Admin access required."))
        # Checked as the caller, since the erase itself runs as sudo.
        self.check_access('read')
        held = set(self._meta_holder_ids())
        leads = self.filtered(lambda l: l.meta_leadgen_id or l.id in held)
        if not leads:
            raise UserError(_(
                "Select at least one Meta lead, or a lead a Meta lead was "
                "merged into."))
        if len(leads) > _META_ERASE_LIMIT:
            raise UserError(_("Select at most %(limit)s leads at a time.",
                              limit=_META_ERASE_LIMIT))
        with_notes = leads._meta_erase_personal_data()
        message = _("Personal data erased on %(count)s lead(s).",
                    count=len(leads))
        if with_notes:
            message += ' ' + _(
                "See the note on %(count)s of them for data that may remain.",
                count=with_notes)
        return {
            'type': 'ir.actions.client',
            'tag': 'display_notification',
            'params': {
                'type': 'success',
                'sticky': False,
                'message': message,
                'next': {'type': 'ir.actions.client', 'tag': 'soft_reload'},
            },
        }

    def _meta_erasable(self, name, not_null=None):
        """Whether the erase may empty this field. A field the lead can't
        be saved without is kept: emptying it would fail the whole erase,
        or leave a lead nobody can save. not_null is the set of NOT NULL
        columns, read once by the caller when it checks many fields."""
        return (self._meta_erase_candidate(name)
                and not self._meta_required(name, not_null))

    def _meta_erase_candidate(self, name):
        # Stage, salesperson, team, revenue, customer, UTM and the like are
        # pipeline data, and so are the module's own meta_* fields.
        field = self._fields.get(name)
        return bool(
            field and field.store and not field.readonly
            and field.type != 'one2many'
            and name not in ('name', 'description')
            and not name.startswith('meta_')
            and name not in _META_ERASE_KEEP
            and not is_blocked_target(
                name, getattr(field, 'comodel_name', None)))

    def _meta_required(self, name, not_null=None):
        if self._fields[name].required:
            return True
        if not_null is None:
            not_null = self._meta_not_null_columns()
        return name in not_null

    def _meta_not_null_columns(self):
        # Odoo sets NOT NULL along with required, but a column changed in
        # the database by hand can have it on its own.
        return {name for name, column in sql.table_columns(
            self.env.cr, self._table).items()
            if column['is_nullable'] == 'NO'}

    def _meta_mapping_targets(self):
        """{form id: names of the lead fields its mapping rows fill} for
        the forms of these leads. Archived rows count too: an archived row
        may have filled a lead before it was switched off."""
        Mapping = self.env['meta.field.mapping'].sudo().with_context(
            active_test=False)
        targets = defaultdict(set)
        forms = self.sudo().meta_form_id_ref
        if forms:
            for row in Mapping.search([('form_id', 'in', forms.ids)]):
                if row.crm_field_id:
                    targets[row.form_id.id].add(row.crm_field_id.name)
        return targets

    @api.model
    def _meta_promoted_fields(self):
        # Only the promote wizard creates these, from Meta answers.
        return {name for name, field in self._fields.items()
                if name.startswith('x_meta_') and field.store}

    def _meta_merged_in_proof(self):
        """{lead id: (texts, complete, expired)} for these leads, about the
        Meta leads merged into each of them.

        texts are the answers those Meta leads sent, from the copies the
        sync log still keeps and from the Meta answers now on the lead.
        complete is True when every one of those Meta leads still has its
        copy, so a value that isn't among the texts didn't come from them.
        expired is True when a copy is missing that no erase removed, which
        means the sync log retention took it.
        The queue keeps only Meta's notification, which holds no answers."""
        records = self._meta_merged_in_records()
        merged = defaultdict(set)
        for record in records:
            merged[record.merged_into_id.id].add(record.leadgen_id)
        sent = {}
        erased = set()
        if records:
            logs = self.env['meta.sync.log'].sudo().search([
                ('meta_leadgen_id', 'in', sorted(set(records.mapped(
                    'leadgen_id')))),
                '|', ('raw_payload', '!=', False),
                ('payload_cleared', '=', 'erased')])
            for log in logs:
                if log.payload_cleared == 'erased':
                    erased.add(log.meta_leadgen_id)
                if not log.raw_payload:
                    continue
                texts = self._meta_payload_texts(log.raw_payload)
                if texts is not None:
                    sent.setdefault(log.meta_leadgen_id, []).extend(texts)
        on_lead = defaultdict(list)
        answers = self.env['meta.lead.answer'].sudo().with_context(
            active_test=False).search([('lead_id', 'in', self.ids)])
        for answer in answers:
            if answer.value:
                on_lead[answer.lead_id.id].append(answer.value)
        proofs = {}
        for lead in self:
            ids = merged.get(lead.id, set())
            texts = list(on_lead.get(lead.id, ()))
            for leadgen_id in sorted(ids):
                texts.extend(sent.get(leadgen_id, ()))
            missing = ids - set(sent)
            proofs[lead.id] = (texts, bool(ids) and not missing,
                               bool(missing - erased))
        return proofs

    @api.model
    def _meta_payload_texts(self, raw):
        """The answer texts in a stored Meta lead payload, or None when it
        isn't one."""
        try:
            data = json.loads(raw)
        except ValueError:
            return None
        entries = data.get('field_data') if isinstance(data, dict) else None
        if not isinstance(entries, list):
            return None
        texts = []
        for entry in entries:
            if not isinstance(entry, dict):
                continue
            values = entry.get('values')
            if not isinstance(values, list):
                values = [values]
            values = [str(v) for v in values if v not in (None, '')]
            texts.extend(values)
            if len(values) > 1:
                # A field or an answer filled from them holds them joined.
                texts.append(', '.join(values))
        return texts

    @api.model
    def _meta_compare_key(self, name, text):
        # Case and spacing don't count, nor how a phone number is
        # written. A short run of digits is compared as text, so a number
        # inside some other answer doesn't pass for a phone.
        text = '' if text is None or text is False else str(text)
        if name in _META_PHONE_FIELDS:
            digits = re.sub(r'\D', '', text)
            if len(digits) >= 6:
                return 'tel:' + digits
        if name in _META_EMAIL_FIELDS:
            address = email_normalize(text)
            if address:
                return address
        return ' '.join(text.split()).casefold()

    def _meta_check_value(self, name, proof):
        """'meta' when this lead's value of field ``name`` is among the
        Meta answers in ``proof`` (see _meta_merged_in_proof), 'own' when
        it can be told it isn't, None when it can't be told.

        Only a text field can be told apart: a link or a choice may have
        been matched from an answer written another way."""
        self.ensure_one()
        texts, complete = proof[:2]
        field = self._fields[name]
        value = self[name]
        if field.type in ('char', 'text'):
            options = [value]
        elif field.type == 'many2one':
            options = [value.display_name]
            for fname in (value._rec_name, 'code'):
                if fname and fname in value._fields:
                    options.append(value[fname])
        elif field.type == 'selection':
            labels = dict(field._description_selection(self.env))
            options = [value, labels.get(value)]
        else:
            options = []
        keys = {self._meta_compare_key(name, text) for text in options}
        keys.discard('')
        if keys & {self._meta_compare_key(name, text) for text in texts}:
            return 'meta'
        if complete and keys and field.type in ('char', 'text'):
            return 'own'
        return None

    @api.model
    def _meta_tracked_texts(self, name, row):
        """The values a tracking value of field ``name`` shows, old and new,
        in the forms _meta_check_value reads that field in: a link also by
        its name and code, a choice also by its key."""
        field = self._fields[name]
        texts = [row.old_value_char, row.new_value_char,
                 row.old_value_text, row.new_value_text]
        if field.type == 'many2one':
            # The history keeps a link's display name and its id.
            ids = {row.old_value_integer, row.new_value_integer} - {0}
            links = self.env[field.comodel_name].sudo().browse(
                sorted(ids)).exists()
            for link in links:
                for fname in (link._rec_name, 'code'):
                    if fname and fname in link._fields:
                        texts.append(link[fname])
        elif field.type == 'selection':
            # The history keeps a choice's label only.
            shown = {row.old_value_char, row.new_value_char} - {False, ''}
            texts.extend(key for key, label
                         in field._description_selection(self.env)
                         if label in shown)
        elif field.type not in ('char', 'text'):
            return []
        return [text for text in texts if text]

    def _meta_history_quoting_proof(self, swept, proofs):
        """The tracking values on these leads, of the fields in ``swept``
        ({lead id: field names}), whose old or new value is among the Meta
        answers in ``proofs`` (see _meta_merged_in_proof). Compared the way
        _meta_check_value compares the field itself."""
        Tracking = self.env['mail.tracking.value'].sudo()
        names = set().union(*swept.values()) if swept else set()
        if not names:
            return Tracking
        rows = Tracking.search([
            ('mail_message_id.model', '=', self._name),
            ('mail_message_id.res_id', 'in', sorted(swept)),
            ('field_id.name', 'in', sorted(names))])
        # Built once per lead and field, not per row.
        found = {}

        def quotes(row):
            lead_id = row.mail_message_id.res_id
            name = row.field_id.name
            if name not in swept.get(lead_id, ()):
                return False
            if (lead_id, name) not in found:
                keys = {self._meta_compare_key(name, text)
                        for text in proofs[lead_id][0]}
                keys.discard('')
                found[lead_id, name] = keys
            return any(self._meta_compare_key(name, text)
                       in found[lead_id, name]
                       for text in self._meta_tracked_texts(name, row))
        return rows.filtered(quotes)

    def _meta_field_labels(self, names):
        described = self.fields_get(sorted(names), ['string'])
        return ', '.join(sorted(
            described.get(name, {}).get('string') or name for name in names))

    def _meta_erase_personal_data(self):
        """Erase the personal data these leads hold from Meta, without any
        checks.

        On a Meta lead: its contact and address fields, the fields its
        form's mapping filled and those recorded as filled by Meta. On a
        lead that isn't a Meta lead but holds merged-in ones: the fields
        recorded as copied from them by the merge (see
        _meta_note_merge_copies), unless the stored Meta data shows the
        value was changed since. When the merge was never recorded, only
        the contact and address fields whose value is found in that data.
        A field the lead can't be saved without is kept and named in the
        note. The history of a cleared field goes; on a lead that isn't a Meta
        lead, every other field looked at loses the history entries showing
        a value found in that data. Then, on both, the answers, answer
        blocks, merge notes and stored payloads.

        Returns how many of the notes left on the leads say more than the
        standard sentence."""
        leads = self.sudo().with_context(mail_notrack=True)
        # A change made earlier in this transaction would otherwise be
        # tracked at commit, with the old value in it.
        leads._track_discard()

        targets = leads._meta_mapping_targets()
        promoted = self._meta_promoted_fields()
        not_null = leads._meta_not_null_columns()
        # Read before the answers and the stored copies go.
        proofs = leads.filtered(
            lambda l: not l.meta_leadgen_id)._meta_merged_in_proof()

        cleared = {}
        legacy = set()
        # On leads that aren't Meta leads, the fields kept, named in the
        # note. Merge not recorded: {lead id: (names whose value isn't in
        # the Meta data, names that couldn't be checked)}. Merge recorded:
        # {lead id: names that no longer hold the Meta value}.
        unrecorded = {}
        changed = {}
        # On every lead, the fields it would clear but can't be saved
        # without, named in the note.
        required = {}
        # On those leads, the fields looked at but not cleared, whose
        # history is checked for Meta values: kept, own or empty now.
        swept = {}
        for lead in leads:
            filled, tracked = lead._meta_filled_names()
            if lead.meta_leadgen_id:
                names = (_META_ERASE_CONTACT
                         | targets[lead.meta_form_id_ref.id] | filled)
                if not tracked:
                    names |= promoted
                    legacy.add(lead.id)
            elif tracked:
                # Its own values were never Meta's, and a recorded field
                # typed over since the merge holds them again.
                proof = proofs[lead.id]
                names = set()
                kept = changed[lead.id] = set()
                for name in filled:
                    if (self._meta_erase_candidate(name) and lead[name]
                            and lead._meta_check_value(name, proof) == 'own'):
                        kept.add(name)
                    else:
                        names.add(name)
            else:
                # Nothing says what the merges brought over, so only a
                # value found in the Meta data goes.
                proof = proofs[lead.id]
                names = set()
                own, unchecked = unrecorded[lead.id] = (set(), set())
                for name in _META_ERASE_CONTACT | promoted:
                    if not self._meta_erase_candidate(name) or not lead[name]:
                        continue
                    found = lead._meta_check_value(name, proof)
                    if found == 'meta':
                        names.add(name)
                    elif found == 'own':
                        own.add(name)
                    else:
                        unchecked.add(name)
            vals = {}
            for name in names:
                if self._meta_erasable(name, not_null):
                    if self._fields[name].type == 'many2many':
                        vals[name] = [Command.clear()]
                    else:
                        vals[name] = False
                elif self._meta_erase_candidate(name) and lead[name]:
                    required.setdefault(lead.id, set()).add(name)
            if lead.meta_name_is_generated:
                # The generated title often holds the contact's name. The
                # write override drops the generated flag with it.
                vals['name'] = _("Meta lead (personal data erased)")
            lead.write(vals)
            cleared[lead.id] = set(vals)
            if not lead.meta_leadgen_id:
                swept[lead.id] = {
                    name for name in _META_ERASE_CONTACT | promoted | filled
                    if self._meta_erase_candidate(name)} - cleared[lead.id]

        answers = self.env['meta.lead.answer'].sudo().with_context(
            active_test=False).search([('lead_id', 'in', leads.ids)])
        answer_count = len(answers)
        # The description clean-up needs the question keys to tell the
        # answer lines from text a salesperson typed into the block.
        answer_keys = defaultdict(set)
        for answer in answers:
            answer_keys[answer.lead_id.id].add(answer.question_key)
        answers.unlink()

        # Tracking rows keep old and new values, so those of the cleared
        # fields go too. Other tracked changes, such as the stage, stay.
        all_cleared = set().union(*cleared.values())
        tracking = self.env['mail.tracking.value'].sudo().search([
            ('mail_message_id.model', '=', self._name),
            ('mail_message_id.res_id', 'in', leads.ids),
            ('field_id.name', 'in', sorted(all_cleared)),
        ]).filtered(lambda t: t.field_id.name in cleared.get(
            t.mail_message_id.res_id, ()))
        tracking_count = len(tracking)
        tracking.unlink()
        # The other fields may still show a Meta value in their history:
        # the merge that copied it, a retype over it, or an edit of the
        # Meta lead before the merge, which crm moves over with its
        # messages. An entry goes when the stored Meta data holds a value
        # it shows, even if the field itself couldn't be checked: one copy
        # left is proof enough for that entry. Entries showing no such
        # value stay.
        quoting = leads._meta_history_quoting_proof(
            {lead_id: names for lead_id, names in swept.items() if names},
            proofs)
        history_cleaned = set(quoting.mapped('mail_message_id.res_id'))
        tracking_count += len(quoting)
        quoting.unlink()

        leadgen_ids = set(filter(None, leads.mapped('meta_leadgen_id')))
        merged_ids = set(leads._meta_merged_in_leadgen_ids())
        leadgen_ids |= merged_ids
        self._meta_clear_payloads(leadgen_ids)
        # The leads merged into these ones had their data erased with them,
        # so their removed-lead records say so too. Left as merged, they
        # could be restored from Meta once these leads are deleted and the
        # merge link is gone. The link itself stays.
        self.env['meta.leadgen.tombstone'].sudo()._mark_erased(merged_ids)

        extra = self._meta_erase_extra(leads, answer_keys)

        # The latest erase: a lead erased again holds nothing from before
        # that date. Each erase leaves its own note with its date.
        leads.write({'meta_personal_data_erased_at': fields.Datetime.now()})
        user = self.env.user
        day = format_date(self.env, fields.Date.context_today(self))
        history_note = _(
            "Where a field was kept, the entries in its history that showed "
            "a value from the stored Meta data were removed.")
        with_notes = 0
        for lead in leads:
            parts = [_("Personal data erased by %(user)s on %(date)s. The "
                       "linked contact and hand-written messages were not "
                       "changed.", user=user.name, date=day)]
            if lead.id in legacy:
                parts.append(_(
                    "This lead was imported before Odoo recorded which "
                    "fields Meta filled, so a field whose mapping was "
                    "changed earlier may still hold a Meta answer."))
            if lead.id in unrecorded:
                parts.append(_(
                    "Odoo has no record of which fields were brought over "
                    "when Meta leads were merged into this lead, so a "
                    "contact or address field was cleared only when its "
                    "value was found in the stored Meta data of those "
                    "leads. Another field a merge filled from a Meta answer "
                    "may still hold it."))
                own, unchecked = unrecorded[lead.id]
                if own:
                    parts.append(_(
                        "These fields hold values not found in that data "
                        "and were kept: %(fields)s.",
                        fields=lead._meta_field_labels(own)))
                if unchecked and proofs[lead.id][2]:
                    parts.append(_(
                        "These fields couldn't be checked against the Meta "
                        "data, as the stored copy of a merged Meta lead has "
                        "expired (it is kept only for the sync log retention "
                        "period, %(days)s days by default), and were kept. "
                        "Review them and their history and clear them by "
                        "hand if needed: %(fields)s.",
                        days=RETENTION_DEFAULTS[SYNC_LOG_RETENTION_PARAM],
                        fields=lead._meta_field_labels(unchecked)))
                elif unchecked:
                    parts.append(_(
                        "These fields couldn't be checked against the Meta "
                        "data and were kept. Review them and their history "
                        "and clear them by hand if needed: %(fields)s.",
                        fields=lead._meta_field_labels(unchecked)))
            if changed.get(lead.id):
                parts.append(_(
                    "These fields were filled from a merged Meta lead but "
                    "don't hold the Meta value now, so they were kept: "
                    "%(fields)s.",
                    fields=lead._meta_field_labels(changed[lead.id])))
            if required.get(lead.id):
                parts.append(_(
                    "These fields are required and were kept; clear them by "
                    "hand if needed: %(fields)s.",
                    fields=lead._meta_field_labels(required[lead.id])))
            if lead.id in history_cleaned:
                parts.append(history_note)
            parts.extend(extra.get(lead.id, []))
            if len(parts) > 1:
                with_notes += 1
            # A plain str; _message_log escapes it.
            lead._message_log(body=' '.join(parts))
        _logger.info(
            "meta_lead_ads: personal data erased on lead ids %s "
            "(%d answers, %d tracking values, %d Meta lead ids)",
            leads.ids, answer_count, tracking_count, len(leadgen_ids))
        return with_notes

    def _meta_erase_extra(self, leads, answer_keys=None):
        """Further clean-up for the erase of ``leads``: the Meta answer
        blocks in the description, the module's inbox alerts and the copies
        of the lead title kept on messages and activities.

        ``answer_keys`` maps a lead id to the question keys of the Meta
        answers it had, read before those were deleted.

        Returns {lead id: [note sentences]} for sentences to add to the
        lead's note."""
        answer_keys = answer_keys or {}
        notes = defaultdict(list)
        for lead in leads:
            if not lead.description:
                continue
            new_html, left_behind, kept = self._meta_strip_qa_blocks(
                lead.description, answer_keys.get(lead.id, ()))
            if new_html != lead.description:
                lead.write({'description': new_html})
            if kept:
                notes[lead.id].append(_(
                    "Lines in the Meta answer block of the description that "
                    "aren't this lead's Meta answers were kept, such as notes "
                    "typed into it."))
            if left_behind:
                notes[lead.id].append(_(
                    "An older Meta answer block in the description couldn't "
                    "be located safely and was left as is."))

        # The new-lead alerts quote the old title. Alerts posted before they
        # were tagged are found as system notifications by OdooBot, which
        # can also catch an old assignment ping that quoted the title.
        Message = self.env['mail.message'].sudo()
        alerts = Message.search([
            ('model', '=', self._name), ('res_id', 'in', leads.ids),
            '|', ('message_id', '=like',
                  '%%-openerp-%s@%%' % ALERT_MESSAGE_TAG),
            '&', ('message_type', '=', 'user_notification'),
            ('author_id', '=', self.env.ref('base.partner_root').id),
        ])
        attachments = alerts.attachment_ids
        if attachments:
            # Only attachments nothing else points at are removed.
            shared = Message.search([
                ('attachment_ids', 'in', attachments.ids),
                ('id', 'not in', alerts.ids),
            ]).attachment_ids & attachments
            # Deleting a message also deletes the attachments filed under
            # it, so a shared one filed under an alert moves to the lead.
            for alert in alerts:
                filed = (shared & alert.attachment_ids).filtered(
                    lambda a: a.res_model == Message._name
                    and a.res_id in (0, alert.id))
                if filed:
                    filed.write({'res_model': self._name,
                                 'res_id': alert.res_id})
            attachments -= shared
        alert_count = len(alerts)
        alerts.unlink()
        attachments.exists().unlink()

        # crm's merge note quotes each lead merged into this one: title,
        # contact details, address and notes with the Meta answer block.
        # crm also put that title into the subject of every message it
        # moved over ("From <title>"), so the titles are read from the
        # notes before they go. Only subjects change; bodies stay.
        merge_notes = Message.search([
            ('model', '=', self._name), ('res_id', 'in', leads.ids),
            ('message_type', '!=', 'comment'),
            ('body', 'ilike', _CRM_MERGE_NOTE_CLASS)])
        titles = {}
        for note in merge_notes:
            found = self._meta_merge_note_titles(note.body)
            if found is None:
                merge_notes -= note
            else:
                titles[note.id] = (note.res_id, {t for t in found if t})
        merge_count = len(merge_notes)
        merge_notes.unlink()
        self._meta_rewrite_moved_subjects(titles)

        # Both are stored columns only on some versions; message bodies are
        # never edited. The leads are already renamed here.
        names = defaultdict(list)
        for lead in leads:
            names[lead.display_name].append(lead.id)
        if Message._fields['record_name'].store:
            messages = Message.search([
                ('model', '=', self._name), ('res_id', 'in', leads.ids)])
            for name, ids in names.items():
                messages.filtered(lambda m: m.res_id in ids).write(
                    {'record_name': name})
        Activity = self.env['mail.activity'].sudo()
        if Activity._fields['res_name'].store:
            activities = Activity.search([
                ('res_model', '=', self._name), ('res_id', 'in', leads.ids)])
            for name, ids in names.items():
                activities.filtered(lambda a: a.res_id in ids).write(
                    {'res_name': name})
        if alert_count or merge_count:
            _logger.info(
                "meta_lead_ads: erase removed %d inbox alerts and %d merge "
                "notes on lead ids %s", alert_count, merge_count, leads.ids)
        return dict(notes)

    @api.model
    def _meta_rewrite_moved_subjects(self, notes):
        """Take the merged leads' titles out of the subjects crm gave the
        messages it moved in a merge.

        ``notes`` maps the id of each merge note just removed to (lead id,
        titles it quoted). crm posts its note before it moves the messages
        over, so only a message older than a note can carry its titles.
        A subject is rewritten only when the whole of it has crm's form,
        "From <title>: <subject>" or "From <title>", in any installed
        language; a run of spaces in a title matches any other run. The
        inner subject is handled the same way, for a message moved by an
        earlier merge first."""
        by_lead = defaultdict(list)
        for note_id, (lead_id, found) in notes.items():
            if found:
                by_lead[lead_id].append((note_id, found))
        if not by_lead:
            return
        placeholder = _("Meta lead (personal data erased)")
        templates = self._meta_moved_subject_templates()
        Message = self.env['mail.message'].sudo()
        for lead_id, lead_notes in by_lead.items():
            inner = self._meta_subject_patterns(
                templates, set().union(*(found for _nid, found in lead_notes)))
            newest = max(note_id for note_id, _found in lead_notes)
            patterns = {}
            for message in Message.search([
                    ('model', '=', self._name), ('res_id', '=', lead_id),
                    ('subject', '!=', False), ('id', '<', newest)]):
                key = frozenset(title for note_id, found in lead_notes
                                if note_id > message.id for title in found)
                if key not in patterns:
                    patterns[key] = self._meta_subject_patterns(templates, key)
                subject = self._meta_rewrite_subject(
                    message.subject, patterns[key], inner, placeholder)
                if subject != message.subject:
                    message.write({'subject': subject})

    @api.model
    def _meta_moved_subject_templates(self):
        # crm wrote the subject in the language of whoever merged, with
        # crm's own translations.
        templates = set(_CRM_MOVED_SUBJECTS)
        for lang in dict(self.env['res.lang'].get_installed()):
            translated = code_translations.get_python_translations(
                'crm', lang)
            templates.update(translated.get(source) or source
                             for source in _CRM_MOVED_SUBJECTS)
        return templates

    @api.model
    def _meta_subject_patterns(self, templates, titles):
        """Compiled full-subject patterns for crm's moved-message subject
        templates with one of ``titles`` as the source name."""
        titles = sorted({t for t in titles if t and t.split()},
                        key=len, reverse=True)
        if not titles:
            return []
        name = '|'.join(r'\s+'.join(map(re.escape, title.split()))
                        for title in titles)
        patterns = []
        for template in sorted(templates):
            parts = _CRM_SUBJECT_TOKEN.split(template)
            tokens = parts[1::2]
            if (tokens.count('source_name') != 1
                    or tokens.count('source_subject') > 1):
                continue
            regex = []
            for index, part in enumerate(parts):
                if index % 2 == 0:
                    regex.extend(
                        r'\s+' if piece.isspace() else re.escape(piece)
                        for piece in re.split(r'(\s+)', part.replace('%%', '%'))
                        if piece)
                elif part == 'source_name':
                    regex.append(r'\s*(?P<name>%s)\s*' % name)
                else:
                    regex.append(r'(?P<subject>.*)')
            patterns.append(re.compile(''.join(regex), re.DOTALL))
        return patterns

    @api.model
    def _meta_rewrite_subject(self, subject, patterns, inner, placeholder,
                              depth=0):
        for pattern in patterns:
            match = pattern.fullmatch(subject)
            if not match:
                continue
            pieces = [(match.span('name'), placeholder)]
            if 'subject' in pattern.groupindex:
                rest = match.group('subject')
                if depth < 20:
                    rest = self._meta_rewrite_subject(
                        rest, inner, inner, placeholder, depth + 1)
                pieces.append((match.span('subject'), rest))
            out, pos = [], 0
            for (start, end), text in sorted(pieces):
                out.extend((subject[pos:start], text))
                pos = end
            out.append(subject[pos:])
            return ''.join(out)
        return subject

    @api.model
    def _meta_merge_note_titles(self, html):
        """The titles of the merged leads quoted in a crm merge note, or None
        when ``html`` isn't one."""
        try:
            wrapper = lxml.html.fragment_fromstring(
                str(html or ''), create_parent='div')
        except (etree.ParserError, ValueError):
            return None
        cls = ' %s ' % _CRM_MERGE_NOTE_CLASS
        summaries = wrapper.xpath(
            './/*[contains(concat(" ", normalize-space(@class), " "), $cls)]',
            cls=cls)
        if not summaries:
            return None
        # Each merged lead's part opens with a line giving its title in bold.
        return {' '.join(span.text_content().split())
                for summary in summaries
                for span in summary.xpath(
                    './div/span[contains(concat(" ", normalize-space(@class),'
                    ' " "), " fw-bold ")]')}

    @api.model
    def _meta_strip_qa_blocks(self, html, answer_keys=()):
        """Remove the Meta answer blocks from a description.

        Returns (new html or False when nothing is left, left_behind, kept).
        In a block marked with QA_CLASS the heading line and the lines of
        the lead's own answers go: a line starting with one of
        ``answer_keys`` and a colon. The editor copies the class onto lines
        typed inside the block, so any other line is the salesperson's and
        stays, without the class; kept says whether that happened.
        Older blocks have no marker, so they only go in two shapes where
        their end is certain:
        - an element whose text starts with the heading and holds nothing
          but line breaks;
        - a text run holding the heading with nothing after it in its
          parent, cut from the heading on.
        Anything else is left, and left_behind says so. The rest of the
        HTML is kept as it is, never escaped."""
        if is_html_empty(html):
            return html, False, False
        try:
            wrapper = lxml.html.fragment_fromstring(
                str(html), create_parent='div')
        except (etree.ParserError, ValueError):
            return html, QA_HEADING in html or QA_CLASS in html, False

        def attached(el):
            # False once an outer element has been dropped already.
            return any(a is wrapper for a in el.iterancestors())

        heading = _meta_squash(QA_HEADING)
        prefixes = tuple(sorted(
            '%s:' % key for key in map(_meta_squash, answer_keys) if key))

        def is_meta_line(text):
            return text == heading or bool(
                prefixes and text.startswith(prefixes))

        changed = False
        kept = False
        for el in wrapper.xpath(
                './/*[contains(concat(" ", normalize-space(@class), " "), '
                '$cls)]', cls=' %s ' % QA_CLASS):
            if not attached(el):
                continue
            changed = True
            if self._meta_cut_lines(el, is_meta_line)[1]:
                kept = True
                classes = [c for c in el.get('class', '').split()
                           if c != QA_CLASS]
                if classes:
                    el.set('class', ' '.join(classes))
                else:
                    del el.attrib['class']
            else:
                # drop_tree keeps the tail text, which isn't part of it.
                el.drop_tree()
        for el in list(wrapper.iterdescendants()):
            if (isinstance(el.tag, str) and attached(el)
                    and (el.text or '').lstrip().startswith(QA_HEADING)
                    and all(child.tag == 'br' for child in el)):
                el.drop_tree()
                changed = True
        for el in [wrapper] + list(wrapper.iterdescendants()):
            if not isinstance(el.tag, str):
                continue
            if el.text and QA_HEADING in el.text and not len(el):
                el.text = el.text[:el.text.index(QA_HEADING)]
                changed = True
            if (el is not wrapper and el.tail and QA_HEADING in el.tail
                    and el.getnext() is None):
                el.tail = el.tail[:el.tail.index(QA_HEADING)]
                changed = True
        left_behind = QA_HEADING in wrapper.text_content()
        if not changed:
            return html, left_behind, kept
        new_html = Markup(''.join(
            [escape(wrapper.text or '')]
            + [lxml.html.tostring(child, encoding='unicode')
               for child in wrapper]))
        return ((False if is_html_empty(new_html) else new_html),
                left_behind, kept)

    @api.model
    def _meta_cut_lines(self, el, is_meta_line):
        """Cut the Meta lines out of ``el``, in place.

        A line is what sits between two <br> elements. A block element
        inside, or anything holding a <br> of its own, is cut the same way
        on its own. ``is_meta_line`` gets the line's text with its spaces
        squashed. Returns (cut, kept): whether a line went, and whether
        any text or image is left."""
        lines = [[]]
        blocks = set()
        if el.text:
            lines[-1].append(el.text)
        for child in el:
            if child.tag == 'br':
                lines.append([])
            elif isinstance(child.tag, str) and (
                    child.tag in _META_BLOCK_TAGS
                    or child.find('.//br') is not None):
                blocks.add(len(lines))
                lines.append([child])
                lines.append([])
            else:
                lines[-1].append(child)
            if child.tail:
                lines[-1].append(child.tail)

        def text_of(line):
            return _meta_squash(''.join(
                node if isinstance(node, str)
                else node.text_content() if isinstance(node.tag, str)
                else '' for node in line))

        def has_image(line):
            return any(not isinstance(node, str) and (
                node.tag == 'img' or node.find('.//img') is not None)
                for node in line)

        cut = False
        kept = []
        kept_text = False
        for index, line in enumerate(lines):
            if index in blocks:
                sub_cut, sub_kept = self._meta_cut_lines(line[0], is_meta_line)
                cut |= sub_cut
                if sub_kept or not sub_cut:
                    kept.append((index, line))
                    kept_text |= sub_kept
                continue
            text = text_of(line)
            if text and is_meta_line(text):
                cut = True
                continue
            kept.append((index, line))
            kept_text |= bool(text) or has_image(line)
        if not cut:
            return False, kept_text

        def blank(item):
            index, line = item
            return (index not in blocks and not text_of(line)
                    and all(isinstance(node, str) for node in line))

        while kept and blank(kept[0]):
            kept.pop(0)
        while kept and blank(kept[-1]):
            kept.pop()

        # Rebuild el from the lines that stay; text stays text.
        el.text = None
        for child in list(el):
            el.remove(child)

        def put(node):
            if isinstance(node, str):
                if len(el):
                    el[-1].tail = (el[-1].tail or '') + node
                else:
                    el.text = (el.text or '') + node
            else:
                node.tail = None
                el.append(node)

        previous = None
        for index, line in kept:
            if (previous is not None and previous not in blocks
                    and index not in blocks):
                el.append(el.makeelement('br', {}))
            for node in line:
                put(node)
            previous = index
        return True, kept_text

    def _meta_merged_in_leadgen_ids(self):
        # The Meta lead ids of leads merged into these ones. Their data
        # lives on in these leads, so their copies go with them. An erased
        # record keeps its merge link, so a later merge still finds it.
        return self._meta_merged_in_records().mapped('leadgen_id')

    def _meta_holder_ids(self):
        """Ids of the leads among these that Meta leads were merged into."""
        return self._meta_merged_in_records().merged_into_id.ids

    def _meta_merged_in_records(self):
        Tombstone = self.env['meta.leadgen.tombstone'].sudo()
        ids = [i for i in self.ids if i]
        if not ids:
            return Tombstone
        return Tombstone.search([
            ('removal_reason', 'in', ('merged', 'erased')),
            ('merged_into_id', 'in', ids),
        ])

    @api.model
    def _meta_clear_payloads(self, leadgen_ids):
        """Drop the stored Meta payloads of these Meta lead ids from the
        sync log and the webhook queue. The rows stay, marked erased."""
        leadgen_ids = sorted(lg for lg in leadgen_ids if lg)
        if not leadgen_ids:
            return
        vals = {'raw_payload': False, 'payload_cleared': 'erased'}
        # Rows cleared by an earlier erase are left alone, so their write
        # date still counts towards the queue clean-up.
        for rows in (self.env['meta.sync.log'].sudo().search(
                         [('meta_leadgen_id', 'in', leadgen_ids)]),
                     self.env['meta.webhook.event'].sudo().search(
                         [('leadgen_id', 'in', leadgen_ids)])):
            rows.filtered(lambda r: r.raw_payload
                          or r.payload_cleared != 'erased').write(vals)

    def _meta_token_map(self):
        """Return the token values for render_lead_name from this lead's meta_* fields.

        ``date`` is the Meta submission date when stored, otherwise the
        create_date as an approximation for older leads. That fallback is only
        used for previews: the retroactive rename skips leads without
        meta_submitted_at when the template uses {date}.
        """
        self.ensure_one()
        if self.meta_submitted_at:
            day = fields.Date.to_string(self.meta_submitted_at.date())
        elif self.create_date:
            day = fields.Date.to_string(self.create_date.date())
        else:
            day = ''
        return {
            'form_name': self.meta_form_name or '',
            'campaign_name': self.meta_campaign_name or '',
            'adset_name': self.meta_adset_name or '',
            'ad_name': self.meta_ad_name or '',
            'contact_name': self.contact_name or '',
            'page_name': self.meta_page_name or '',
            'date': day,
        }

    # Meta's created_time, in UTC. Differs from create_date for leads pulled
    # in later by the cron or a retry.
    meta_submitted_at = fields.Datetime(string='Meta Submitted On', copy=False,
                                        index=True)

    # Plain id/name pairs as Meta sent them; no foreign keys.
    meta_campaign_id = fields.Char(string='Meta Campaign ID')
    meta_campaign_name = fields.Char(string='Meta Campaign')
    meta_adset_id = fields.Char(string='Meta Ad Set ID')
    meta_adset_name = fields.Char(string='Meta Ad Set')
    meta_ad_id = fields.Char(string='Meta Ad ID')
    meta_ad_name = fields.Char(string='Meta Ad')
    meta_form_id = fields.Char(string='Meta Form ID')
    meta_form_name = fields.Char(string='Meta Form')
    meta_page_id = fields.Char(string='Meta Page ID')
    meta_page_name = fields.Char(string='Meta Page')

    meta_platform = fields.Selection(
        [('facebook', 'Facebook'), ('instagram', 'Instagram')],
        string='Meta Platform')

    # Set when a campaign, ad set, ad or form name couldn't be read from Meta
    # at ingest. The raw ids are kept and the names are filled in later.
    meta_attribution_pending = fields.Boolean(
        string='Meta Attribution Pending', default=False, copy=False,
        index=True)
    # No default on purpose: a default of 0 makes the upgrade rewrite every
    # existing lead in one statement. Empty reads as 0.
    meta_attribution_attempts = fields.Integer(
        string='Meta Attribution Retries', copy=False)
    # Name retries for this lead's Page wait until then, e.g. after its token
    # stopped working.
    meta_attribution_retry_after = fields.Datetime(
        string='Meta Attribution Retry After', copy=False)

    # Optional links to the synced records. Nullable because a lead can arrive
    # before its form is synced; set null so deleting a page or form keeps the
    # leads. The _ref suffix keeps them apart from the raw Char ids above.
    meta_form_id_ref = fields.Many2one('meta.lead.form', string='Meta Form',
                                       ondelete='set null')
    meta_page_id_ref = fields.Many2one('meta.page', string='Meta Page',
                                       ondelete='set null')
    meta_account_id = fields.Many2one('meta.account', string='Meta Account',
                                      ondelete='set null')

    # Form answers that weren't mapped to a lead field, one row per question.
    answer_ids = fields.One2many('meta.lead.answer', 'lead_id',
                                 string='Meta Lead Answers')

    # This constraint is what makes ingestion idempotent.
    _sql_constraints = [
        ('meta_leadgen_id_uniq', 'unique(meta_leadgen_id)',
         'A lead with this Meta Lead ID already exists.'),
    ]
