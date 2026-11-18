# Meta Lead Ads Integration for Odoo — © 2026 CoreSys Builders. All rights reserved.
# Licensed under the Odoo Proprietary License v1.0 (OPL-1).
# Unauthorized copying, redistribution, or resale of this software, in whole or in
# part, via any medium, is strictly prohibited and constitutes a license violation.
# OPL-1: https://www.odoo.com/documentation/20.0/legal/licenses.html#odoo-apps

"""Own the fields earlier releases promoted, and start the health stamps.

1. Manual x_meta_ lead fields that a mapping (archived ones too) fills, or
   that the promote wizard's lead-form view shows, get an id of the module,
   so uninstalling removes them as it does for fields promoted from now on.
   A hand-made x_meta_ field nothing uses, and a field that already has an
   id of any module, are left alone.
2. The lead-form views the wizard added under the module's Meta tab get an
   id too, so uninstalling leaves no view that shows a dropped field.
3. The queue drain, the backfill and the webhook each get their own health
   stamp, taken from what the database already shows about past runs.
4. The old heartbeat every Meta job shared is removed.

All ids are noupdate, so a later upgrade keeps the records. Only counts are
logged, never a field name, id or secret.
"""
import logging

from odoo import SUPERUSER_ID, api, fields
from odoo.tools import SQL

_logger = logging.getLogger(__name__)

VERSION = '19.0.1.9.0'

_FIELD_PREFIX = 'x_meta_'
_VIEW_PREFIX = 'crm.lead.form.meta.custom.'

# Each job's stamp and the cron whose last run starts it.
_CRON_STAMPS = (
    ('meta_lead_ads.drain_last_run', 'cron_meta_webhook_drain'),
    ('meta_lead_ads.backfill_last_run', 'cron_meta_backfill'),
)


def _like_prefix(prefix):
    # Field names are full of underscores, which LIKE would read as "any
    # character". Escape them so only the literal prefix matches.
    escaped = (prefix.replace('\\', '\\\\')
               .replace('%', '\\%').replace('_', '\\_'))
    return escaped + '%'


def _anchor_view_id(cr):
    """The module's Meta tab on the lead form, which the wizard's views
    extend."""
    cr.execute("""
        SELECT res_id FROM ir_model_data
         WHERE module = 'meta_lead_ads' AND name = 'crm_lead_view_form_meta'
           AND model = 'ir.ui.view'
    """)
    row = cr.fetchone()
    return row[0] if row else None


def _adopt_fields(cr):
    """Own the fields; return how many got an id, and how many of those
    only a mapping fills, with no lead-form view of the wizard."""
    # A view only counts when it extends the Meta tab: one that merely
    # borrows the name stays in place at uninstall and would then point at
    # a dropped field.
    cr.execute(SQL("""
        WITH found AS (
            SELECT f.id, f.name,
                   EXISTS (SELECT 1 FROM ir_ui_view v
                            WHERE v.model = 'crm.lead'
                              AND v.inherit_id = %s
                              AND v.name = %s || f.name) AS has_view
              FROM ir_model_fields f
             WHERE f.model = 'crm.lead'
               AND f.state = 'manual'
               AND f.name LIKE %s ESCAPE '\\'
               AND NOT EXISTS (SELECT 1 FROM ir_model_data d
                                WHERE d.model = 'ir.model.fields'
                                  AND d.res_id = f.id)
        ), owned AS (
            INSERT INTO ir_model_data (module, name, model, res_id, noupdate)
            SELECT 'meta_lead_ads', 'field_crm_lead__' || found.name,
                   'ir.model.fields', found.id, true
              FROM found
             WHERE found.has_view
                OR EXISTS (SELECT 1 FROM meta_field_mapping m
                            WHERE m.crm_field_id = found.id)
            ON CONFLICT (module, name) DO NOTHING
            RETURNING res_id
        )
        SELECT count(*), count(*) FILTER (WHERE NOT found.has_view)
          FROM owned JOIN found ON found.id = owned.res_id
    """, _anchor_view_id(cr), _VIEW_PREFIX, _like_prefix(_FIELD_PREFIX)))
    adopted, mapping_only = cr.fetchone()
    return adopted, mapping_only


def _adopt_views(cr):
    anchor = _anchor_view_id(cr)
    if not anchor:
        return 0
    start = len(_VIEW_PREFIX) + 1
    # Every such view is the wizard's, also for a reused field without the
    # prefix. A name the id could not hold (a space, a dot) is skipped
    # rather than failing the upgrade.
    cr.execute(SQL("""
        INSERT INTO ir_model_data (module, name, model, res_id, noupdate)
        SELECT 'meta_lead_ads', 'crm_lead_form_meta_custom_' || substr(v.name, %s),
               'ir.ui.view', v.id, true
          FROM ir_ui_view v
         WHERE v.model = 'crm.lead'
           AND v.inherit_id = %s
           AND v.name LIKE %s ESCAPE '\\'
           AND substr(v.name, %s) ~ '^[A-Za-z0-9_]+$'
           AND NOT EXISTS (SELECT 1 FROM ir_model_data d
                            WHERE d.model = 'ir.ui.view' AND d.res_id = v.id)
        ON CONFLICT (module, name) DO NOTHING
    """, start, anchor, _like_prefix(_VIEW_PREFIX), start))
    return cr.rowcount


def _seed_stamps(cr):
    # No evidence means the key stays unset, so the dashboard says the job
    # hasn't run yet rather than guessing. A key already set is the job's
    # own stamp and is kept.
    env = api.Environment(cr, SUPERUSER_ID, {})
    ICP = env['ir.config_parameter']
    written = 0
    for key, cron in _CRON_STAMPS:
        if ICP.get_str(key):
            continue
        cr.execute("""
            SELECT c.lastcall
              FROM ir_cron c
              JOIN ir_model_data d
                ON d.res_id = c.id AND d.model = 'ir.cron'
               AND d.module = 'meta_lead_ads' AND d.name = %s
        """, [cron])
        row = cr.fetchone()
        if row and row[0]:
            ICP.set_str(key, fields.Datetime.to_string(row[0]))
            written += 1
    key = 'meta_lead_ads.webhook_last_ok'
    if not ICP.get_str(key):
        # The rows the drain writes when it finishes a webhook lead, either
        # ingested or settled as removed. Failures and other triggers are
        # not webhook success.
        cr.execute("""
            SELECT max(create_date) FROM meta_sync_log
             WHERE trigger = 'webhook'
               AND status IN ('success', 'skipped_idempotent',
                              'skipped_duplicate', 'skipped_removed')
        """)
        last_ok = cr.fetchone()[0]
        if last_ok:
            ICP.set_str(key, fields.Datetime.to_string(last_ok))
            written += 1
    # Nothing reads the shared heartbeat any more. On Odoo 20 setting False
    # only empties the value, so the row itself is removed.
    ICP.search([('key', '=', 'meta_lead_ads.cron_last_run')]).unlink()
    return written


def migrate(cr, version):
    if not version:
        return None
    adopted, mapping_only = _adopt_fields(cr)
    counts = {
        'fields': adopted,
        'views': _adopt_views(cr),
        'stamps': _seed_stamps(cr),
    }
    _logger.info(
        "meta_lead_ads: %(fields)s promoted field(s) and %(views)s lead-form "
        "view(s) are now owned by the module; %(stamps)s health stamp(s) "
        "seeded.", counts)
    if mapping_only:
        # These were plain custom fields until now: the wizard never showed
        # them on the lead form, so an admin may not see them as part of
        # the module.
        _logger.warning(
            "meta_lead_ads: %s custom lead field(s) that a field mapping "
            "fills, with no lead-form view of the module, are now owned by "
            "the module. Uninstalling the module will now remove them and "
            "their values; export them first if you need to keep them.",
            mapping_only)
    return counts
