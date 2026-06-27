# Meta Lead Ads Integration for Odoo — © 2026 CoreSys Builders. All rights reserved.
# Licensed under the Odoo Proprietary License v1.0 (OPL-1).
# Unauthorized copying, redistribution, or resale of this software, in whole or in
# part, via any medium, is strictly prohibited and constitutes a license violation.
# OPL-1: https://www.odoo.com/documentation/19.0/legal/licenses.html#odoo-apps

# Caches Meta object names (campaign/adset/ad/form ID -> name) behind a
# concurrency-safe upsert. Campaign/ad names are commercially sensitive, so
# access is restricted to the Meta group.
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

    # DB-level uniqueness over a Python search() — holds under
    # webhook+cron+resend concurrency. Odoo 19: declared as a models.Constraint
    # (the _sql_constraints list is no longer honoured).
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
        """Concurrency-safe upsert.

        Two workers (webhook + cron) may try to cache the same
        (object_type, graph_id) at once. We update an existing row if present;
        otherwise we attempt the create inside a SAVEPOINT so that a unique
        violation from a racing worker does NOT poison the outer transaction —
        we catch IntegrityError, re-read the now-existing row, and update it.
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
                # Force the INSERT (and the unique-constraint check) to execute
                # inside the savepoint. Odoo defers writes to flush time, so
                # without this the IntegrityError from a racing worker could fire
                # after the savepoint exits and poison the outer transaction —
                # the exact failure this upsert exists to prevent.
                rec.flush_recordset()
            return rec
        except IntegrityError:
            # Another worker won the race — re-read and update the now-existing row.
            rec = self.search([('object_type', '=', object_type),
                              ('graph_id', '=', graph_id)], limit=1)
            rec.write({'name': name, 'resolved_at': fields.Datetime.now()})
            return rec

    def action_refresh_names(self):
        """Admin-gated clear/refresh action.

        Blank the name and reset resolved_at so the next resolve_name miss
        re-fetches via meta.graph.client.resolve_name once a token arg exists.
        Does not call Graph directly here.
        """
        self.write({'name': False, 'resolved_at': False})
        return True
