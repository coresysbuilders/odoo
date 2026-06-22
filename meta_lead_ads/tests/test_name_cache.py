# Meta Lead Ads Integration for Odoo — © 2026 CoreSys Builders. All rights reserved.
# Licensed under the Odoo Proprietary License v1.0 (OPL-1).
# Unauthorized copying, redistribution, or resale of this software, in whole or in
# part, via any medium, is strictly prohibited and constitutes a license violation.
# OPL-1: https://www.odoo.com/documentation/18.0/legal/licenses.html#odoo-apps

"""Tests for meta.name.cache.

Covers uniqueness, the concurrency-safe upsert, ACL posture and the refresh
action shape, plus the payload-first / cache-hit behaviour of resolve_name.

Note: Odoo's assertRaises accepts a SINGLE exception class, never a tuple.
IntegrityError tests use a savepoint + mute_logger('odoo.sql_db').
"""
import os
import json
from unittest import mock

from psycopg2 import IntegrityError

from odoo.tests.common import TransactionCase, tagged
from odoo.tools import mute_logger
from odoo.exceptions import AccessError

_FIXTURES = os.path.join(os.path.dirname(__file__), 'fixtures')


def _load_json(filename):
    with open(os.path.join(_FIXTURES, filename), encoding='utf-8') as fh:
        return json.load(fh)


@tagged('post_install', '-at_install')
class TestNameCache(TransactionCase):

    # ---- cache model -----------------------------------------------------

    def test_name_cache_unique(self):
        """Composite (object_type, graph_id) is DB-UNIQUE."""
        self.env['meta.name.cache'].create({
            'object_type': 'campaign', 'graph_id': 'DUP', 'name': 'First'})
        with self.assertRaises(IntegrityError), mute_logger('odoo.sql_db'):
            with self.env.cr.savepoint():
                self.env['meta.name.cache'].create({
                    'object_type': 'campaign', 'graph_id': 'DUP', 'name': 'Second'})

    def test_name_cache_user_readonly(self):
        """Meta User can read but not create; a non-Meta internal user cannot
        even read (the cached names are commercially sensitive)."""
        base_internal = self.env.ref('base.group_user')
        meta_user_group = self.env.ref('meta_lead_ads.group_meta_user')
        meta_user = self.env['res.users'].create({
            'name': 'NC Meta U', 'login': 'nc_meta_u',
            'groups_id': [(6, 0, [base_internal.id, meta_user_group.id])]})
        plain_user = self.env['res.users'].create({
            'name': 'NC Plain', 'login': 'nc_plain',
            'groups_id': [(6, 0, [base_internal.id])]})

        self.env['meta.name.cache'].create({
            'object_type': 'campaign', 'graph_id': 'A', 'name': 'X'})

        # Meta User: read allowed
        self.env['meta.name.cache'].with_user(meta_user).search([])
        # Meta User: create denied
        with self.assertRaises(AccessError):
            self.env['meta.name.cache'].with_user(meta_user).create(
                {'object_type': 'campaign', 'graph_id': 'B', 'name': 'Y'})
        # Ordinary internal user (no Meta group): read denied
        with self.assertRaises(AccessError):
            self.env['meta.name.cache'].with_user(plain_user).search([])

    def test_store_upsert_single_row(self):
        """Repeated _store on the same key keeps ONE row, latest write wins --
        no duplicate, no poisoned transaction."""
        Cache = self.env['meta.name.cache']
        Cache._store('campaign', '123', 'First')
        Cache._store('campaign', '123', 'Second')
        rows = Cache.search([('object_type', '=', 'campaign'),
                             ('graph_id', '=', '123')])
        self.assertEqual(len(rows), 1)
        self.assertEqual(rows.name, 'Second')

    def test_action_refresh_names_shape(self):
        """action_refresh_names blanks name / resets resolved_at and makes NO
        Graph call (the client need not exist for this no-op)."""
        rec = self.env['meta.name.cache'].create({
            'object_type': 'campaign', 'graph_id': 'R1', 'name': 'Old Name'})
        self.assertTrue(rec.resolved_at)
        # Spy: if the client model exists, ensure resolve_name is never called.
        client = self.env.get('meta.graph.client') \
            if 'meta.graph.client' in self.env else None
        if client is not None:
            with mock.patch.object(type(client), 'resolve_name') as spy:
                rec.action_refresh_names()
                spy.assert_not_called()
        else:
            rec.action_refresh_names()
        self.assertFalse(rec.name)
        self.assertFalse(rec.resolved_at)

    # ---- resolve_name (uses meta.graph.client) ---------------------------

    def test_payload_name_skips_graph(self):
        """A payload-provided name short-circuits — ZERO HTTP."""
        client = self.env['meta.graph.client']
        # Class-level patch (recordset rejects method setattr on Odoo 18).
        with mock.patch.object(type(client), '_get_session') as get_session:
            result = client.resolve_name(
                'tok', 'campaign', '123', payload_name='Given')
            self.assertEqual(result, 'Given')
            get_session.return_value.request.assert_not_called()

    def test_cache_prevents_second_call(self):
        """First miss hits Graph; the second call for the same id makes NO
        second Graph call."""
        client = self.env['meta.graph.client']
        node = _load_json('success_node.json')
        fake_resp = mock.Mock()
        fake_resp.status_code = 200
        fake_resp.ok = True
        fake_resp.headers = {}
        fake_resp.json.return_value = node
        # Class-level patch (recordset rejects method setattr on Odoo 18).
        with mock.patch.object(type(client), '_get_session') as get_session:
            get_session.return_value.request.return_value = fake_resp
            client.resolve_name('tok', 'campaign', '999')
            client.resolve_name('tok', 'campaign', '999')
            self.assertEqual(get_session.return_value.request.call_count, 1)
