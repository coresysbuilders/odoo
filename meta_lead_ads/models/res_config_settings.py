# Meta Lead Ads Integration for Odoo — © 2026 CoreSys Builders. All rights reserved.
# Licensed under the Odoo Proprietary License v1.0 (OPL-1).
# Unauthorized copying, redistribution, or resale of this software, in whole or in
# part, via any medium, is strictly prohibited and constitutes a license violation.
# OPL-1: https://www.odoo.com/documentation/18.0/legal/licenses.html#odoo-apps

# Settings transient hosting the Meta Lead Ads lead-naming configuration.
#
# Load-bearing rules:
#   - The naming template is a SINGLE GLOBAL value: stored on the bare
#     ``ir.config_parameter`` key with NO per-company / per-form scope. The
#     field is therefore NOT bound via the ``config_parameter=`` shortcut —
#     an explicit get_values/set_values round-trip is the only
#     deterministic hook for the validate-before-write gate.
#   - BOTH sudo mutators (``set_values``, ``action_retro_rename_meta_leads``)
#     enforce an IN-METHOD ``has_group('meta_lead_ads.group_meta_admin')`` gate
#     raising ``AccessError`` BEFORE any sudo write. The view ``groups=``
#     hides the UI but does not stop a direct ORM/RPC call.
#   - ``set_values`` validates the template (``_validate_lead_name_template``)
#     BEFORE the param write.
#   - The status compute sudo-reads ONLY the token-free ``access_status`` field;
#     ``app_secret`` / ``access_token`` are NEVER read here.
#
# The naming constants are imported from the public ``const`` module so the
# U+2022 bullet default can never drift. The render helper / validator /
# token map are reused from
# ``meta.lead.ingest`` and ``crm.lead`` — no logic is duplicated here.
import logging

from odoo import _, api, fields, models
from odoo.exceptions import AccessError

from .const import (
    DEFAULT_LEAD_NAME, LEAD_NAME_PARAM,
    NOTIFY_ENABLED_PARAM, NOTIFY_TARGET_PARAM,
    NOTIFY_USER_PARAM, NOTIFY_GROUP_PARAM,
)

_logger = logging.getLogger(__name__)

# Batch size for the retro-rename sweep — bounds the working set / ORM cache so
# a large Meta-lead corpus is never materialised at once (security M-02).
_RETRO_RENAME_BATCH = 500

# Representative token values for the live preview only — NOT persisted. Lets
# an admin see a sample title while editing the template in the form.
_SAMPLE = {
    'form_name': 'Contact Us',
    'campaign_name': 'Spring Sale',
    'adset_name': 'Lookalike 1%',
    'ad_name': 'Carousel A',
    'contact_name': 'Jane Doe',
    'page_name': 'Acme Page',
    'date': '2026-06-18',
}

# Best-of ranking for the connection-status surface (higher = more ready).
_STATUS_RANK = {
    'lead_retrieval_granted': 3,
    'dev_test_only': 2,
    'auth_failed': 1,
}

class ResConfigSettings(models.TransientModel):
    _inherit = 'res.config.settings'

    # Single GLOBAL template. NOT bound via config_parameter=:
    # the explicit get/set_values round-trip is the validate-before-write hook.
    lead_name_template = fields.Char(
        string="Lead title template", default=DEFAULT_LEAD_NAME)
    lead_name_preview = fields.Char(
        string="Preview", compute='_compute_lead_name_preview')
    meta_connection_status = fields.Selection(
        [('auth_failed', 'Authentication failed'),
         ('dev_test_only', 'Development mode — test leads only'),
         ('lead_retrieval_granted',
          'leads_retrieval granted (App Review still gates production)'),
         ('untested', 'Active account — not yet token-tested'),
         ('none', 'No active Meta account')],
        string="Connection status",
        compute='_compute_meta_connection_status')

    # New-lead notification config. Stored as plain ir.config_parameter values
    # (same round-trip pattern as the naming template), so a Meta admin who is
    # not a full Settings admin can still manage them.
    meta_notify_enabled = fields.Boolean(
        string="Notify on new lead",
        help="Send an Odoo notification when a new lead arrives from Meta.")
    meta_notify_target_type = fields.Selection(
        [('user', 'Specific user'), ('group', 'Role / group')],
        string="Notify", default='user')
    meta_notify_user_id = fields.Many2one(
        'res.users', string="User to notify")
    meta_notify_group_id = fields.Many2one(
        'res.groups', string="Role to notify")

    # ------------------------------------------------------------------ #
    # ir.config_parameter round-trip.
    # ------------------------------------------------------------------ #
    def get_values(self):
        res = super().get_values()
        ICP = self.env['ir.config_parameter'].sudo()
        res['lead_name_template'] = ICP.get_param(
            LEAD_NAME_PARAM, DEFAULT_LEAD_NAME)
        res['meta_notify_enabled'] = ICP.get_param(NOTIFY_ENABLED_PARAM) == '1'
        res['meta_notify_target_type'] = ICP.get_param(
            NOTIFY_TARGET_PARAM, 'user')
        notify_uid = ICP.get_param(NOTIFY_USER_PARAM)
        res['meta_notify_user_id'] = int(notify_uid) if notify_uid else False
        notify_gid = ICP.get_param(NOTIFY_GROUP_PARAM)
        res['meta_notify_group_id'] = int(notify_gid) if notify_gid else False
        return res

    def set_values(self):
        # IN-METHOD ADMIN GATE FIRST: set_values mutates a
        # GLOBAL config value under sudo; the view groups= gate hides
        # the UI but cannot stop a direct ORM/RPC call. Mirrors the
        # in-method has_group checks used elsewhere in this module.
        if not self.env.user.has_group('meta_lead_ads.group_meta_admin'):
            raise AccessError(_(
                "Only Meta administrators can change Meta Lead Ads settings."))
        # Normalize once so the validated and persisted values are
        # provably identical (an empty template becomes the default).
        template = self.lead_name_template or DEFAULT_LEAD_NAME
        # Validate BEFORE any write — reject unknown/malformed
        # tokens so a broken template never lands in ir.config_parameter.
        self.env['meta.lead.ingest']._validate_lead_name_template(template)
        # Persist the Meta-owned params directly.
        ICP = self.env['ir.config_parameter'].sudo()
        ICP.set_param(LEAD_NAME_PARAM, template)
        # New-lead notification config. m2o ids are stored as their string id;
        # an empty selection clears the key.
        ICP.set_param(NOTIFY_ENABLED_PARAM, '1' if self.meta_notify_enabled else '0')
        ICP.set_param(NOTIFY_TARGET_PARAM, self.meta_notify_target_type or 'user')
        ICP.set_param(
            NOTIFY_USER_PARAM,
            str(self.meta_notify_user_id.id) if self.meta_notify_user_id else '')
        ICP.set_param(
            NOTIFY_GROUP_PARAM,
            str(self.meta_notify_group_id.id) if self.meta_notify_group_id else '')
        # CONTAIN the write surface: ``res.config.settings.set_values`` is
        # the single mutation surface for EVERY module's settings. The ACL grant
        # that lets a non-system Meta admin reach this method must NOT become a
        # back door to mutate unrelated global settings via crafted RPC. So only a
        # full Settings admin (base.group_system) is allowed to drive
        # ``super().set_values()`` (which writes the other modules' fields); a pure
        # Meta admin persists the Meta param above and nothing else.
        if self.env.user.has_group('base.group_system'):
            return super().set_values()
        return False

    # ------------------------------------------------------------------ #
    # Live preview. Swallows render errors so the form renders mid-edit;
    # the hard rejection happens at set_values, not here.
    # ------------------------------------------------------------------ #
    @api.depends('lead_name_template')
    def _compute_lead_name_preview(self):
        Ingest = self.env['meta.lead.ingest']
        for rec in self:
            try:
                rec.lead_name_preview = Ingest.render_lead_name(
                    rec.lead_name_template, _SAMPLE)
            except Exception as exc:
                # render_lead_name is documented never to raise; this is
                # belt-and-suspenders for the non-blocking preview. Log so a
                # genuinely unexpected failure stays observable.
                _logger.debug("lead-name preview render failed: %s", exc)
                rec.lead_name_preview = _("(invalid template)")

    # ------------------------------------------------------------------ #
    # Best-of connection status. Token-free: sudo-reads ONLY access_status
    # (no groups=); NEVER app_secret/access_token.
    # ------------------------------------------------------------------ #
    @api.depends_context('uid')
    def _compute_meta_connection_status(self):
        # Global state, identical for every transient record — compute once
        # (meta_account._compute_cron_status idiom). order='id asc' is the
        # deterministic account-pick rule.
        accounts = self.env['meta.account'].sudo().search(
            [('active', '=', True)], order='id asc')
        # Distinguish "no active account" from "account(s) present but not
        # yet token-tested" (access_status is nullable until a token test runs).
        # An existing-but-untested account must NOT read as "No active account".
        if not accounts:
            best = 'none'
        else:
            tested = [s for s in accounts.mapped('access_status') if s]
            best = (max(tested, key=lambda s: _STATUS_RANK.get(s, 0))
                    if tested else 'untested')
        for rec in self:
            rec.meta_connection_status = best

    # ------------------------------------------------------------------ #
    # Admin-gated, clobber-safe retroactive rename.
    # ------------------------------------------------------------------ #
    def action_retro_rename_meta_leads(self):
        """Re-apply the active template to existing Meta leads, clobber-safely.

        Guards:
          1. IN-METHOD admin gate FIRST — a non-``group_meta_admin`` caller (even
             ``base.group_system``) hits ``AccessError`` before any sudo write;
             the action bulk-writes ``crm.lead.name`` under sudo, so the view
             ``groups=`` gate is necessary but not sufficient.
          2. {date}-template SKIP — when the ACTIVE template contains
             ``{date}`` we rename NOTHING: there is no stored Meta create-date and
             the ``create_date`` approximation is never trusted for matching/renaming.
             This is the single place that decides the {date} policy;
             ``_meta_token_map``'s ``{date}`` is for the forward/preview path only.
          3. Meta-leads-only domain (``meta_leadgen_id != False``) — non-Meta leads
             are never touched.
          4. Clobber-safe via STORED PROVENANCE — re-title only leads
             with ``meta_name_is_generated = True`` (set on create, cleared by any
             manual ``name`` write). This replaces the former default-shape skeleton
             heuristic: a lead generated under ANY template (not just the default
             shape) is recognized, and a human-edited title — even one shaped like
             the default — is never overwritten.
          5. Explicit idempotent no-op — ``continue`` BEFORE any write when the new
             render already equals the current name.

        NEVER creates a lead — only writes ``name`` on existing rows.
        """
        self.ensure_one()
        # Guard 1 — in-method admin gate FIRST.
        if not self.env.user.has_group('meta_lead_ads.group_meta_admin'):
            raise AccessError(_(
                "Only Meta administrators can re-apply the lead title template."))

        template = self.lead_name_template or DEFAULT_LEAD_NAME
        # Reject a broken template before any bulk write.
        self.env['meta.lead.ingest']._validate_lead_name_template(template)

        # Guard 2 — committed {date}-template SKIP (never trust create_date here).
        if '{date}' in template:
            return self._retro_notification(0, skipped_date=True)

        Ingest = self.env['meta.lead.ingest']
        Lead = self.env['crm.lead'].sudo()
        # Guards 3 + 4 — Meta-leads-only AND still-generated (stored provenance).
        domain = [('meta_leadgen_id', '!=', False),
                  ('meta_name_is_generated', '=', True)]
        # Process in bounded batches so a large Meta-lead corpus never
        # materialises every record (and its ORM cache) at once (security M-02).
        # Re-titled leads KEEP meta_name_is_generated=True, so they still match
        # the domain — the matched set is stable and offset paging is safe.
        renamed = 0
        offset = 0
        while True:
            leads = Lead.search(domain, limit=_RETRO_RENAME_BATCH,
                                offset=offset, order='id')
            if not leads:
                break
            for lead in leads:
                token_map = lead._meta_token_map()
                generated_now = Ingest.render_lead_name(template, token_map)
                # Guard 5 — explicit idempotent no-op BEFORE any write.
                if generated_now == lead.name:
                    continue
                # Keep the provenance flag True (this is still a generated title)
                # so the write override does not clear it.
                lead.write({'name': generated_now,
                            'meta_name_is_generated': True})
                renamed += 1
            offset += _RETRO_RENAME_BATCH
            # Persist this batch's writes, then drop the batch from the ORM
            # cache so memory stays bounded across a large corpus.
            leads.flush_recordset()
            leads.invalidate_recordset()
        # Surface how many Meta leads were left alone because a human edited them
        # (flag cleared) so "0 re-titled" is never silently ambiguous.
        skipped_manual = Lead.search_count(
            [('meta_leadgen_id', '!=', False),
             ('meta_name_is_generated', '=', False)])
        return self._retro_notification(renamed, skipped_manual=skipped_manual)

    def _retro_notification(self, renamed, skipped_manual=0, skipped_date=False):
        if skipped_date:
            message = _(
                "Templates containing {date} are skipped for retroactive "
                "rename (no stored Meta create-date to trust). No leads "
                "were re-titled.")
        else:
            message = _("%s lead(s) re-titled.") % renamed
            if skipped_manual:
                message += _(
                    " %s manually-edited lead(s) left unchanged.") % skipped_manual
        return {
            'type': 'ir.actions.client',
            'tag': 'display_notification',
            'params': {
                'title': _("Retroactive rename"),
                'message': message,
                'type': 'success',
                'sticky': False,
            },
        }
