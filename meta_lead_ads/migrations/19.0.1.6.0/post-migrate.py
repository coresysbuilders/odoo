# Meta Lead Ads Integration for Odoo — © 2026 CoreSys Builders. All rights reserved.
# Licensed under the Odoo Proprietary License v1.0 (OPL-1).
# Unauthorized copying, redistribution, or resale of this software, in whole or in
# part, via any medium, is strictly prohibited and constitutes a license violation.
# OPL-1: https://www.odoo.com/documentation/20.0/legal/licenses.html#odoo-apps

"""Switch off mappings onto fields a form answer may no longer fill.

1. Active mappings whose crm.lead target is now blocked (salesperson, team,
   pipeline fields, title, customer, UTM, the fields the module sets
   itself, the partner assignment fields and any other link to a user or
   contact) are archived. Nothing is deleted, so an admin can still see what
   the row did and retarget it.
2. Each archived row gets a short reason, shown on the form, that says why
   and where the value comes from now.
3. The stored mapping count of every form that lost a row is worked out
   again, since SQL does not recompute it.

Rows on technical fields are left on: they still fill those fields as
before.
"""
import logging

from odoo.tools import SQL

from odoo.addons.meta_lead_ads.models.const import (
    MAPPING_BLOCKED_ADDON_FIELDS, MAPPING_BLOCKED_COMODELS,
    MAPPING_BLOCKED_FIELDS, MAPPING_BLOCKED_PREFIXES, MAPPING_FORBIDDEN_FIELDS)
from odoo.addons.meta_lead_ads.models.migration_utils import iter_id_batches

_logger = logging.getLogger(__name__)

VERSION = '19.0.1.6.0'

# Targets the sales team works with once the lead is in. The recurring_
# prefix is checked apart, so later recurring fields get the same text.
_PIPELINE_FIELDS = frozenset({
    'stage_id', 'probability', 'priority', 'lost_reason_id',
    'date_deadline', 'expected_revenue',
})
_UTM_FIELDS = frozenset({'campaign_id', 'source_id', 'medium_id'})


def _reason(name, label, relation=None):
    """Why the row on crm.lead field ``name`` was switched off.

    Plain English: there is no environment here to translate with. A label
    missing in English falls back to the technical name. ``relation`` is
    the model a relational target points at.
    """
    field = (label or '').strip() or name
    if name == 'user_id':
        text = ("the salesperson is now set per Page, in the Page's Lead "
                "Routing settings.")
    elif name == 'team_id':
        text = ("the sales team is now set per Page, in the Page's Lead "
                "Routing settings.")
    elif name in _PIPELINE_FIELDS or name.startswith(MAPPING_BLOCKED_PREFIXES):
        text = ("%s drives your sales pipeline, so it can no longer be "
                "filled from a form answer." % field)
    elif name == 'name':
        text = ("the lead title comes from the title template in Settings "
                "and would overwrite this answer.")
    elif name == 'partner_id':
        text = ("the customer is linked by Odoo's contact matching, not from "
                "a form answer.")
    elif name in _UTM_FIELDS:
        text = ("campaign, source and medium come from the ad that brought "
                "the lead in.")
    elif name in MAPPING_BLOCKED_ADDON_FIELDS:
        text = ("the assigned partner decides which reseller can see the "
                "lead, so it can no longer be filled from a form answer.")
    elif relation in MAPPING_BLOCKED_COMODELS:
        text = ("%s links the lead to a user or contact, which decides who "
                "owns or sees it, so it can no longer be filled from a form "
                "answer." % field)
    else:
        text = ("%s is set by the module when the lead is created, so it can "
                "no longer be filled from a form answer." % field)
    return "Disabled in %s: %s" % (VERSION, text)


def _like_prefix(prefix):
    # Field names are full of underscores, which LIKE would read as "any
    # character". Escape them so only the literal prefix matches.
    escaped = (prefix.replace('\\', '\\\\')
               .replace('%', '\\%').replace('_', '\\_'))
    return escaped + '%'


def _blocked_rows():
    """Active mappings whose target is a blocked crm.lead field."""
    names = sorted(MAPPING_BLOCKED_FIELDS | MAPPING_FORBIDDEN_FIELDS
                   | MAPPING_BLOCKED_ADDON_FIELDS)
    terms = [SQL("name = ANY(%s)", names),
             SQL("relation = ANY(%s)", sorted(MAPPING_BLOCKED_COMODELS))]
    # Matching against an array can't carry an ESCAPE clause, so each
    # prefix gets a term of its own.
    terms += [SQL("name LIKE %s ESCAPE '\\'", _like_prefix(prefix))
              for prefix in MAPPING_BLOCKED_PREFIXES]
    return SQL("""
        active AND crm_field_id IN (
            SELECT id FROM ir_model_fields
             WHERE model = 'crm.lead' AND (%s))
    """, SQL(" OR ").join(terms))


def _archive_blocked(cr):
    archived = 0
    # The forms touched so far live in the database, not in a Python set,
    # so a large table never grows memory here. A form whose rows fall in
    # several batches is counted once.
    cr.execute("DROP TABLE IF EXISTS meta_mapping_forms_touched")
    cr.execute(
        "CREATE TEMP TABLE meta_mapping_forms_touched (id int4 PRIMARY KEY)")
    for ids in iter_id_batches(cr, 'meta_field_mapping', _blocked_rows()):
        cr.execute(SQL("""
            SELECT m.id, f.name, f.field_description->>'en_US', f.relation
              FROM meta_field_mapping m
              JOIN ir_model_fields f ON f.id = m.crm_field_id
             WHERE m.id = ANY(%s)
        """, ids))
        rows = cr.fetchall()
        if not rows:
            continue
        values = SQL(", ").join(
            SQL("(%s::int4, %s::varchar)", mapping_id,
                _reason(name, label, relation))
            for mapping_id, name, label, relation in rows)
        cr.execute(SQL("""
            UPDATE meta_field_mapping m
               SET active = false, disabled_reason = v.reason
              FROM (VALUES %s) AS v(id, reason)
             WHERE m.id = v.id AND m.active
        """, values))
        archived += cr.rowcount
        batch_forms = SQL("""
            SELECT DISTINCT form_id FROM meta_field_mapping
             WHERE id = ANY(%s) AND form_id IS NOT NULL
        """, ids)
        cr.execute(SQL(
            "INSERT INTO meta_mapping_forms_touched %s ON CONFLICT DO NOTHING",
            batch_forms))
        # A form whose rows span batches is refreshed again in the later
        # batch, and ends with its final count.
        cr.execute(SQL("""
            UPDATE meta_lead_form f
               SET mapping_count = (
                   SELECT count(*) FROM meta_field_mapping m
                    WHERE m.form_id = f.id AND m.active)
             WHERE f.id IN (%s)
        """, batch_forms))
    cr.execute("SELECT count(*) FROM meta_mapping_forms_touched")
    forms = cr.fetchone()[0]
    cr.execute("DROP TABLE meta_mapping_forms_touched")
    return archived, forms


def migrate(cr, version):
    if not version:
        return None
    counts = {}
    counts['archived'], counts['forms'] = _archive_blocked(cr)
    _logger.info(
        "meta_lead_ads: %(archived)s mapping(s) on %(forms)s form(s) turned "
        "off because their target can no longer be filled from a form "
        "answer", counts)
    return counts
