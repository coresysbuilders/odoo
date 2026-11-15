# Meta Lead Ads Integration for Odoo — © 2026 CoreSys Builders. All rights reserved.
# Licensed under the Odoo Proprietary License v1.0 (OPL-1).
# Unauthorized copying, redistribution, or resale of this software, in whole or in
# part, via any medium, is strictly prohibited and constitutes a license violation.
# OPL-1: https://www.odoo.com/documentation/18.0/legal/licenses.html#odoo-apps

# Cache of Meta object names (campaign, ad set, ad, form id -> name).
# Campaign and ad names are commercially sensitive, so only Meta groups can
# read it, and each company only sees the names its own Pages fetched.
import logging

from psycopg2 import IntegrityError
from psycopg2.extensions import TransactionRollbackError

from odoo import api, fields, models

from .meta_lead_ingest import _quiet_lost_races

_logger = logging.getLogger(__name__)


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
    company_id = fields.Many2one(
        'res.company', required=True, index=True, ondelete='cascade')

    # Enforced in the database because webhook and cron can write the same
    # entry at the same time. The key stays the same as before the company
    # was added, so an upgrade replaces the old constraint instead of keeping
    # it next to this one.
    _sql_constraints = [
        ('object_graph_uniq', 'unique(object_type, graph_id, company_id)',
         'A cache entry for this object already exists.'),
    ]

    @api.model
    def _key_domain(self, object_type, graph_id, company):
        # Callers run as sudo, so the company has to be in the domain; the
        # record rule doesn't apply there.
        return [('object_type', '=', object_type),
                ('graph_id', '=', graph_id),
                ('company_id', '=', company.id)]

    @api.model
    def _lookup(self, object_type, graph_id, company):
        """Return the name ``company`` cached for (object_type, graph_id), or
        False on a miss."""
        rec = self.search(
            self._key_domain(object_type, graph_id, company), limit=1)
        return rec.name if rec else False

    @api.model
    def _store(self, object_type, graph_id, name, company):
        """Create or update ``company``'s cache entry, best effort.

        Every write runs in its own savepoint and is flushed there. If another
        worker inserted the same entry first, that row is updated instead. If
        a concurrent write wins outright, nothing is stored and an empty
        recordset is returned; the caller's transaction stays usable and the
        name it already has is still right.
        """
        # Write out anything pending first, so an error from someone else's
        # write isn't mistaken for a lost cache write below.
        self.env.cr.flush()
        try:
            # Two workers refreshing the same entry conflict under repeatable
            # read. That is expected, so keep it out of the error log.
            with _quiet_lost_races():
                return self._store_row(object_type, graph_id, name, company)
        except TransactionRollbackError as e:
            _logger.info(
                "meta_lead_ads: name cache entry for %s %s (company %s) not "
                "saved (%s); it is looked up again next time",
                object_type, graph_id, company.id, type(e).__name__)
            return self.browse()

    @api.model
    def _store_row(self, object_type, graph_id, name, company):
        values = {'name': name, 'resolved_at': fields.Datetime.now()}
        domain = self._key_domain(object_type, graph_id, company)
        existing = self.search(domain, limit=1)
        if existing:
            with self.env.cr.savepoint():
                existing.write(values)
                existing.flush_recordset()
            return existing
        try:
            with self.env.cr.savepoint():
                rec = self.create(dict(values, object_type=object_type,
                                       graph_id=graph_id,
                                       company_id=company.id))
                # Odoo delays the INSERT until flush. Flush now so a unique
                # violation is raised inside the savepoint, not after it.
                rec.flush_recordset()
            return rec
        except IntegrityError:
            # Another worker created it first; update that row.
            with self.env.cr.savepoint():
                rec = self.search(domain, limit=1)
                rec.write(values)
                rec.flush_recordset()
            return rec

    def action_refresh_names(self):
        """Clear the cached names so they are fetched from Graph again on the
        next lookup. Makes no Graph call itself."""
        self.write({'name': False, 'resolved_at': False})
        return True
