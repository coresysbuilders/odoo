# Meta Lead Ads Integration for Odoo — © 2026 CoreSys Builders. All rights reserved.
# Licensed under the Odoo Proprietary License v1.0 (OPL-1).
# Unauthorized copying, redistribution, or resale of this software, in whole or in
# part, via any medium, is strictly prohibited and constitutes a license violation.
# OPL-1: https://www.odoo.com/documentation/18.0/legal/licenses.html#odoo-apps

"""Tests for meta.name.cache and resolve_name.

Covers the unique key, the upsert, access rights, the refresh action,
resolve_name preferring the payload name and then the cache over Graph, and
keeping each company's cached names to that company.
"""
import os
import json
from unittest import mock

from psycopg2 import IntegrityError

from odoo import Command
from odoo.tests.common import TransactionCase, new_test_user, tagged
from odoo.tools import mute_logger
from odoo.exceptions import AccessError

from .test_ingest import IngestFixtureMixin
from .test_multi_company import MultiCompanyFixtureMixin

_FIXTURES = os.path.join(os.path.dirname(__file__), 'fixtures')


def _load_json(filename):
    with open(os.path.join(_FIXTURES, filename), encoding='utf-8') as fh:
        return json.load(fh)


@tagged('post_install', '-at_install')
class TestNameCache(TransactionCase):

    def setUp(self):
        super().setUp()
        self.company = self.env.company

    def test_name_cache_unique(self):
        """(object_type, graph_id) is unique per company in the database."""
        self.env['meta.name.cache'].create({
            'object_type': 'campaign', 'graph_id': 'DUP', 'name': 'First',
            'company_id': self.company.id})
        with self.assertRaises(IntegrityError), mute_logger('odoo.sql_db'):
            with self.env.cr.savepoint():
                self.env['meta.name.cache'].create({
                    'object_type': 'campaign', 'graph_id': 'DUP',
                    'name': 'Second', 'company_id': self.company.id})

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
            'object_type': 'campaign', 'graph_id': 'A', 'name': 'X',
            'company_id': self.company.id})

        self.env['meta.name.cache'].with_user(meta_user).search([])
        with self.assertRaises(AccessError):
            self.env['meta.name.cache'].with_user(meta_user).create(
                {'object_type': 'campaign', 'graph_id': 'B', 'name': 'Y',
                 'company_id': self.company.id})
        with self.assertRaises(AccessError):
            self.env['meta.name.cache'].with_user(plain_user).search([])

    def test_store_upsert_single_row(self):
        """Storing the same key twice keeps one row with the latest name."""
        Cache = self.env['meta.name.cache']
        Cache._store('campaign', '123', 'First', self.company)
        Cache._store('campaign', '123', 'Second', self.company)
        rows = Cache.search([('object_type', '=', 'campaign'),
                             ('graph_id', '=', '123')])
        self.assertEqual(len(rows), 1)
        self.assertEqual(rows.name, 'Second')

    def test_action_refresh_names_shape(self):
        """action_refresh_names clears name and resolved_at without calling Graph."""
        rec = self.env['meta.name.cache'].create({
            'object_type': 'campaign', 'graph_id': 'R1', 'name': 'Old Name',
            'company_id': self.company.id})
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
            client.resolve_name('tok', 'campaign', '999',
                                company=self.company)
            client.resolve_name('tok', 'campaign', '999',
                                company=self.company)
            self.assertEqual(get_session.return_value.request.call_count, 1)


@tagged('post_install', '-at_install')
class TestNameCacheCompany(MultiCompanyFixtureMixin, IngestFixtureMixin,
                           TransactionCase):
    """A cached name belongs to the company whose Page fetched it."""

    def setUp(self):
        super().setUp()
        self.Cache = self.env['meta.name.cache']

    def _rows(self, graph_id, object_type='campaign'):
        return self.Cache.sudo().search([
            ('object_type', '=', object_type), ('graph_id', '=', graph_id)])

    def test_same_object_cached_once_per_company(self):
        self.Cache._store('campaign', 'C1', 'Name A', self.co_a)
        self.Cache._store('campaign', 'C1', 'Name B', self.co_b)
        rows = self._rows('C1')
        self.assertEqual(len(rows), 2)
        self.assertEqual(
            {(r.company_id, r.name) for r in rows},
            {(self.co_a, 'Name A'), (self.co_b, 'Name B')})

    def test_lookup_bound_to_company_under_sudo(self):
        Cache = self.Cache.sudo()
        Cache._store('campaign', 'C1', 'Name B', self.co_b)
        self.assertFalse(Cache._lookup('campaign', 'C1', self.co_a))
        Cache._store('campaign', 'C1', 'Name A', self.co_a)
        self.assertEqual(Cache._lookup('campaign', 'C1', self.co_a), 'Name A')
        self.assertEqual(Cache._lookup('campaign', 'C1', self.co_b), 'Name B')

    def test_meta_user_sees_only_own_company(self):
        a_row = self.Cache._store('campaign', 'C1', 'Name A', self.co_a)
        b_row = self.Cache._store('campaign', 'C1', 'Name B', self.co_b)
        user = new_test_user(
            self.env, 'nc_meta_user_a',
            groups='base.group_user,meta_lead_ads.group_meta_user',
            company_id=self.co_a.id,
            company_ids=[Command.set([self.co_a.id])])
        as_user = self.Cache.with_user(user)
        seen = as_user.search([])
        self.assertIn(a_row.id, seen.ids)
        self.assertNotIn(b_row.id, seen.ids)
        self.assertEqual(seen.sudo().company_id, self.co_a)
        with self.assertRaises(AccessError):
            as_user.browse(b_row.id).read(['name'])

    def test_resolve_without_company_does_not_cache(self):
        Client = type(self.env['meta.graph.client'])
        with mock.patch.object(Client, '_request',
                               return_value={'name': 'Fetched'}) as request:
            name = self.env['meta.graph.client'].resolve_name(
                'tok', 'campaign', 'NC1')
            again = self.env['meta.graph.client'].resolve_name(
                'tok', 'campaign', 'NC1')
        self.assertEqual((name, again), ('Fetched', 'Fetched'))
        # Nothing was cached, so the second call went to Graph as well.
        self.assertEqual(request.call_count, 2)
        self.assertFalse(self._rows('NC1'))

    def test_resolve_without_company_ignores_cached_rows(self):
        self.Cache._store('campaign', 'NC2', 'Cached A', self.co_a)
        Client = type(self.env['meta.graph.client'])
        with mock.patch.object(Client, '_request',
                               return_value={'name': 'Fetched'}):
            name = self.env['meta.graph.client'].resolve_name(
                'tok', 'campaign', 'NC2')
        self.assertEqual(name, 'Fetched')
        self.assertEqual(self._rows('NC2').name, 'Cached A')

    def test_ingest_caches_under_page_company(self):
        payload = self._fake_lead(id='LGNC1', campaign_name=None,
                                  form_name='Contact Us')
        Client = type(self.env['meta.graph.client'])
        with mock.patch.object(Client, 'fetch_lead', return_value=payload), \
                mock.patch.object(Client, '_request',
                                  return_value={'name': 'Spring'}):
            lead = self.Ingest.ingest_leadgen(self.page_b, 'LGNC1', 'manual')
        self.assertEqual(lead.company_id, self.co_b)
        self.assertEqual(lead.meta_campaign_name, 'Spring')
        rows = self._rows('C1')
        self.assertEqual(len(rows), 1)
        self.assertEqual(rows.company_id, self.co_b)
        self.assertEqual(rows.name, 'Spring')
