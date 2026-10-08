# Meta Lead Ads Integration for Odoo — © 2026 CoreSys Builders. All rights reserved.
# Licensed under the Odoo Proprietary License v1.0 (OPL-1).
# Unauthorized copying, redistribution, or resale of this software, in whole or in
# part, via any medium, is strictly prohibited and constitutes a license violation.
# OPL-1: https://www.odoo.com/documentation/18.0/legal/licenses.html#odoo-apps

"""Tests for meta.name.cache and resolve_name.

Covers the unique key, the upsert, access rights, the refresh action, and
resolve_name preferring the payload name and then the cache over Graph.
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

    def test_name_cache_unique(self):
        """(object_type, graph_id) is unique in the database."""
        self.env['meta.name.cache'].create({
            'object_type': 'campaign', 'graph_id': 'DUP', 'name': 'First'})
        with self.assertRaises(IntegrityError), mute_logger('odoo.sql_db'):
            with self.env.cr.savepoint():
                self.env['meta.name.cache'].create({
                    'object_type': 'campaign', 'graph_id': 'DUP', 'name': 'Second'})

    def test_name_cache_user_readonly(self):
        """Meta users can read but not create; other internal users can't read.

        Campaign and ad names are commercially sensitive.
        """
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

        self.env['meta.name.cache'].with_user(meta_user).search([])
        with self.assertRaises(AccessError):
            self.env['meta.name.cache'].with_user(meta_user).create(
                {'object_type': 'campaign', 'graph_id': 'B', 'name': 'Y'})
        with self.assertRaises(AccessError):
            self.env['meta.name.cache'].with_user(plain_user).search([])

    def test_store_upsert_single_row(self):
        """Storing the same key twice keeps one row with the latest name."""
        Cache = self.env['meta.name.cache']
        Cache._store('campaign', '123', 'First')
        Cache._store('campaign', '123', 'Second')
        rows = Cache.search([('object_type', '=', 'campaign'),
                             ('graph_id', '=', '123')])
        self.assertEqual(len(rows), 1)
        self.assertEqual(rows.name, 'Second')

    def test_action_refresh_names_shape(self):
        """action_refresh_names clears name and resolved_at without calling Graph."""
        rec = self.env['meta.name.cache'].create({
            'object_type': 'campaign', 'graph_id': 'R1', 'name': 'Old Name'})
        self.assertTrue(rec.resolved_at)
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

    def test_payload_name_skips_graph(self):
        """A name already in the payload is returned with no HTTP call."""
        client = self.env['meta.graph.client']
        # Patch the class; Odoo recordsets don't allow setting methods.
        with mock.patch.object(type(client), '_get_session') as get_session:
            result = client.resolve_name(
                'tok', 'campaign', '123', payload_name='Given')
            self.assertEqual(result, 'Given')
            get_session.return_value.request.assert_not_called()

    def test_cache_prevents_second_call(self):
        """The second lookup of the same id is served from the cache."""
        client = self.env['meta.graph.client']
        node = _load_json('success_node.json')
        fake_resp = mock.Mock()
        fake_resp.status_code = 200
        fake_resp.ok = True
        fake_resp.headers = {}
        fake_resp.json.return_value = node
        with mock.patch.object(type(client), '_get_session') as get_session:
            get_session.return_value.request.return_value = fake_resp
            client.resolve_name('tok', 'campaign', '999')
            client.resolve_name('tok', 'campaign', '999')
            self.assertEqual(get_session.return_value.request.call_count, 1)
