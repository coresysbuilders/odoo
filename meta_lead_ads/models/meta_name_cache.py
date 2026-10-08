# Meta Lead Ads Integration for Odoo — © 2026 CoreSys Builders. All rights reserved.
# Licensed under the Odoo Proprietary License v1.0 (OPL-1).
# Unauthorized copying, redistribution, or resale of this software, in whole or in
# part, via any medium, is strictly prohibited and constitutes a license violation.
# OPL-1: https://www.odoo.com/documentation/19.0/legal/licenses.html#odoo-apps

# Cache of Meta object names (campaign, ad set, ad, form id -> name).
# Campaign and ad names are commercially sensitive, so only Meta groups can
# read it.
from psycopg2 import IntegrityError

from odoo import api, fields, models


class MetaNameCache(models.Model):
    _name = 'meta.name.cache'
    _description = 'Meta object name cache (campaign/adset/ad/form ID -> name)'

    object_type = fields.Selection(
        [('campaign', 'Campaign'), ('adset', 'Ad Set'),
         ('ad', 'Ad'), ('form', 'Form')],
        required=True, index=True)
    graph_id = fields.Char(required=True, index=True)
    name = fields.Char()
    resolved_at = fields.Datetime(default=fields.Datetime.now)

    # Enforced in the database because webhook and cron can write the same
    # entry at the same time. Odoo 19 uses models.Constraint instead of
    # _sql_constraints.
    _object_graph_uniq = models.Constraint(
        'unique(object_type, graph_id)',
        'A cache entry for this object already exists.',
    )

    @api.model
    def _lookup(self, object_type, graph_id):
        """Return the cached name for (object_type, graph_id) or False on a miss."""
        rec = self.search([('object_type', '=', object_type),
                           ('graph_id', '=', graph_id)], limit=1)
        return rec.name if rec else False

    @api.model
    def _store(self, object_type, graph_id, name):
        """Create or update the cache entry; safe against concurrent writers.

        The create runs in a savepoint. If another worker inserted the same
        entry first, the IntegrityError is caught there and that row is
        updated instead, without breaking the outer transaction.
        """
        existing = self.search([('object_type', '=', object_type),
                               ('graph_id', '=', graph_id)], limit=1)
        if existing:
            existing.write({'name': name, 'resolved_at': fields.Datetime.now()})
            return existing
        try:
            with self.env.cr.savepoint():
                rec = self.create({
                    'object_type': object_type, 'graph_id': graph_id,
                    'name': name, 'resolved_at': fields.Datetime.now(),
                })
                # Odoo delays the INSERT until flush. Flush now so a unique
                # violation is raised inside the savepoint, not after it.
                rec.flush_recordset()
            return rec
        except IntegrityError:
            # Another worker created it first; update that row.
            rec = self.search([('object_type', '=', object_type),
                              ('graph_id', '=', graph_id)], limit=1)
            rec.write({'name': name, 'resolved_at': fields.Datetime.now()})
            return rec

    def action_refresh_names(self):
        """Clear the cached names so they are fetched from Graph again on the
        next lookup. Makes no Graph call itself."""
        self.write({'name': False, 'resolved_at': False})
        return True
