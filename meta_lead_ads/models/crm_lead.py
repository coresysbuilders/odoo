# Meta Lead Ads Integration for Odoo — © 2026 CoreSys Builders. All rights reserved.
# Licensed under the Odoo Proprietary License v1.0 (OPL-1).
# Unauthorized copying, redistribution, or resale of this software, in whole or in
# part, via any medium, is strictly prohibited and constitutes a license violation.
# OPL-1: https://www.odoo.com/documentation/18.0/legal/licenses.html#odoo-apps

from odoo import api, fields, models


class CrmLead(models.Model):
    _inherit = 'crm.lead'   # extend the core model in place — do not set _name

    # Idempotency key. Nullable (no required=True — non-Meta leads stay NULL).
    # No index=True: the UNIQUE constraint below auto-creates the backing index.
    # copy=False so duplicating a lead in the UI does not carry the unique id.
    meta_leadgen_id = fields.Char(string='Meta Lead ID', copy=False)

    # Title provenance. True ONLY while ``name`` is still the
    # Meta-rendered title: set by the create path, kept True by the retroactive
    # rename, and CLEARED by any manual ``name`` write (see ``write`` below).
    # The retro-rename acts ONLY on flag-True leads, so a human-edited title is
    # never clobbered (even one shaped like the default), and a lead generated
    # under ANY template — not just the default skeleton — is recognized.
    # copy=False so a duplicated lead is treated as a fresh (non-generated) row.
    meta_name_is_generated = fields.Boolean(
        string='Meta Title Auto-Generated', default=False, copy=False)

    def write(self, vals):
        """Clear the auto-generated provenance flag on any MANUAL ``name`` edit so
        the retroactive rename never overwrites a human-set title. A write
        that explicitly carries ``meta_name_is_generated`` (the retro rename and
        the create path's follow-up writes) is honoured as-is."""
        if 'name' in vals and 'meta_name_is_generated' not in vals:
            vals = dict(vals, meta_name_is_generated=False)
        return super().write(vals)

    # Stored, indexed normalized-phone column so business dedup matches by an
    # indexed SQL '=' instead of loading every open phone-bearing lead and
    # normalizing in Python on the ingest hot path. The normalizer lives on
    # meta.lead.ingest and is reused here, so the
    # stored value and the incoming lookup key are normalized identically.
    # copy=False — a duplicated lead recomputes from its own phone.
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
        """Return the ``{token: value}`` map for ``meta.lead.ingest.render_lead_name``
        from this lead's stored ``meta_*`` fields (plus ``contact_name``).

        ``{date}`` here is the ORM ``create_date`` audit date — an APPROXIMATION
        of the Meta ``created_time`` (no Meta create-date is stored on crm.lead).
        It is provided for the FORWARD / PREVIEW path only. The retroactive-rename
        action (``res.config.settings.action_retro_rename_meta_leads``)
        NEVER trusts this for ``{date}``-bearing templates: when the active
        template contains ``{date}`` it SKIPS those leads outright rather than
        risk a wrong-date rename — especially for cron-backfilled leads whose
        Meta ``created_time`` differs from the Odoo row's ``create_date``. This
        is the single policy reconciling the forward-path approximation kept here
        with the conservative retro guard.
        """
        self.ensure_one()
        return {
            'form_name': self.meta_form_name or '',
            'campaign_name': self.meta_campaign_name or '',
            'adset_name': self.meta_adset_name or '',
            'ad_name': self.meta_ad_name or '',
            'contact_name': self.contact_name or '',
            'page_name': self.meta_page_name or '',
            'date': (fields.Date.to_string(self.create_date.date())
                     if self.create_date else ''),
        }

    # Raw Char id+name pairs — source of truth, no FK.
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

    # Selection with room to extend (e.g. messenger) — not a free Char.
    meta_platform = fields.Selection(
        [('facebook', 'Facebook'), ('instagram', 'Instagram')],
        string='Meta Platform')

    # Optional navigation m2o links. Must be nullable (a lead can arrive for a
    # not-yet-discovered form/page) and ondelete='set null' so deleting a
    # meta.page/form never deletes leads. These are populated by matching the
    # raw Char ids elsewhere. The _ref suffix avoids colliding with the
    # raw-Char meta_form_id / meta_page_id.
    meta_form_id_ref = fields.Many2one('meta.lead.form', string='Meta Form',
                                       ondelete='set null')
    meta_page_id_ref = fields.Many2one('meta.page', string='Meta Page',
                                       ondelete='set null')
    meta_account_id = fields.Many2one('meta.account', string='Meta Account',
                                      ondelete='set null')

    # Lossless capture of unmapped form questions (one row each).
    answer_ids = fields.One2many('meta.lead.answer', 'lead_id',
                                 string='Meta Lead Answers')

    _sql_constraints = [
        ('meta_leadgen_id_uniq', 'unique(meta_leadgen_id)',
         'A lead with this Meta Lead ID already exists.'),
    ]
