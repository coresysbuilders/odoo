# Meta Lead Ads Integration for Odoo — © 2026 CoreSys Builders. All rights reserved.
# Licensed under the Odoo Proprietary License v1.0 (OPL-1).
# Unauthorized copying, redistribution, or resale of this software, in whole or in
# part, via any medium, is strictly prohibited and constitutes a license violation.
# OPL-1: https://www.odoo.com/documentation/19.0/legal/licenses.html#odoo-apps

from odoo import api, fields, models


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

    def write(self, vals):
        """Clear meta_name_is_generated when the name is edited by hand.

        Writes that set the flag explicitly (the retroactive rename) keep it."""
        if 'name' in vals and 'meta_name_is_generated' not in vals:
            vals = dict(vals, meta_name_is_generated=False)
        return super().write(vals)

    # Stored, indexed normalized phone so dedup can match with SQL '=' instead
    # of normalizing every open lead in Python. Uses the same normalizer as
    # meta.lead.ingest so both sides agree.
    meta_phone_normalized = fields.Char(
        string='Normalized Phone (Meta dedup)',
        compute='_compute_meta_phone_normalized', store=True, index=True,
        copy=False)

    @api.depends('phone')
    def _compute_meta_phone_normalized(self):
        Ingest = self.env['meta.lead.ingest']
        for lead in self:
            lead.meta_phone_normalized = Ingest._normalize_phone(lead.phone) or False

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

    # Optional links to the synced records. Nullable because a lead can arrive
    # before its form is synced; set null so deleting a page or form keeps the
    # leads. The labels differ from the Char fields above because Odoo 19 warns
    # about two fields with the same label on one model.
    meta_form_id_ref = fields.Many2one('meta.lead.form', string='Linked Meta Form',
                                       ondelete='set null')
    meta_page_id_ref = fields.Many2one('meta.page', string='Linked Meta Page',
                                       ondelete='set null')
    meta_account_id = fields.Many2one('meta.account', string='Meta Account',
                                      ondelete='set null')

    # Form answers that weren't mapped to a lead field, one row per question.
    answer_ids = fields.One2many('meta.lead.answer', 'lead_id',
                                 string='Meta Lead Answers')

    # Odoo 19 ignores _sql_constraints; this produces the same PG constraint
    # name as the 18.0 version (crm_lead_meta_leadgen_id_uniq). This constraint
    # is what makes ingestion idempotent.
    _meta_leadgen_id_uniq = models.Constraint(
        'unique(meta_leadgen_id)',
        'A lead with this Meta Lead ID already exists.',
    )
