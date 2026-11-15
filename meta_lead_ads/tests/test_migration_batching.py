# Meta Lead Ads Integration for Odoo — © 2026 CoreSys Builders. All rights reserved.
# Licensed under the Odoo Proprietary License v1.0 (OPL-1).
# Unauthorized copying, redistribution, or resale of this software, in whole or in
# part, via any medium, is strictly prohibited and constitutes a license violation.
# OPL-1: https://www.odoo.com/documentation/18.0/legal/licenses.html#odoo-apps

"""Upgrade migrations work in bounded id batches.

The scripts are loaded from disk and run against the test cursor. The batch
size is patched down to a few rows and every batch is recorded, so a handful
of seeded rows is enough to prove that the scripts page through the tables
instead of loading them whole.
"""
import json
import unittest
from contextlib import contextmanager, nullcontext
from datetime import datetime, timedelta, timezone
from unittest import mock

from odoo import Command, fields
from odoo.modules.migration import load_script
from odoo.tests.common import TransactionCase, new_test_user, tagged
from odoo.tools import SQL

from odoo.addons.meta_lead_ads.models import (meta_sync_log, migration_utils,
                                              phone_key)
from odoo.addons.meta_lead_ads.models.const import DEFAULT_LEAD_NAME

from .common_typed_targets import LinkTargetsMixin, TypedTargetsMixin
from .test_ingest import IngestFixtureMixin

PRE_COMPANY = 'meta_lead_ads/migrations/18.0.13.5.0/pre-migrate.py'
POST_NAME_FLAG = 'meta_lead_ads/migrations/18.0.10.1.0/post-migrate.py'
POST_RETENTION = 'meta_lead_ads/migrations/18.0.11.0.0/post-migrate.py'
POST_SUBMITTED = 'meta_lead_ads/migrations/18.0.13.4.0/post-migrate.py'
POST_DEDUP = 'meta_lead_ads/migrations/18.0.13.8.0/post-migrate.py'
POST_MAPPING_TARGETS = 'meta_lead_ads/migrations/18.0.13.9.0/post-migrate.py'
PRE_NAME_CACHE = 'meta_lead_ads/migrations/18.0.13.10.0/pre-migrate.py'
POST_SINGLE_APP = 'meta_lead_ads/migrations/18.0.13.11.0/post-migrate.py'
POST_OWNERSHIP = 'meta_lead_ads/migrations/18.0.13.12.0/post-migrate.py'
POST_WEBHOOK_EVIDENCE = 'meta_lead_ads/migrations/18.0.13.12.1/post-migrate.py'
POST_ARCHIVED_PAGE = 'meta_lead_ads/migrations/18.0.13.13.0/post-migrate.py'
RETENTION_KEY = 'meta_lead_ads.sync_log_retention_days'

# Every column the company pre-migrate fills, as (table, column).
COMPANY_COLUMNS = (
    ('meta_account', 'company_id'),
    ('meta_page', 'lead_company_id'),
    ('meta_lead_form', 'company_id'),
    ('meta_sync_log', 'page_id'),
    ('meta_sync_log', 'company_id'),
    ('meta_webhook_event', 'page_company_id'),
    ('meta_webhook_event', 'company_id'),
)


@tagged('post_install', '-at_install')
class TestMigrationBatching(LinkTargetsMixin, TypedTargetsMixin,
                            IngestFixtureMixin, TransactionCase):

    def setUp(self):
        super().setUp()
        self.main = self.env.ref('base.main_company')
        self.co_a = self.account.company_id
        self.Log = self.env['meta.sync.log']
        self.Event = self.env['meta.webhook.event']

    # ------------------------------------------------------------------ #
    # helpers
    # ------------------------------------------------------------------ #
    @contextmanager
    def _batched(self, mod, size=3):
        """Shrink the batch size and record (table, size) of every batch.

        Tables paged by parent through iter_child_batches are also listed
        in self.child_tables.
        """
        batches = []
        self.child_tables = []

        def spy_on(real, child):
            def spy(cr, table, *args, **kwargs):
                if child:
                    self.child_tables.append(table)
                for ids in real(cr, table, *args, **kwargs):
                    batches.append((table, len(ids)))
                    yield ids
            return spy

        with mock.patch.object(migration_utils, 'BATCH_SIZE', size), \
                mock.patch.object(mod, 'iter_id_batches', spy_on(
                    migration_utils.iter_id_batches, False)):
            if hasattr(mod, 'iter_child_batches'):
                with mock.patch.object(mod, 'iter_child_batches', spy_on(
                        migration_utils.iter_child_batches, True)):
                    yield batches
            else:
                yield batches

    def _run_script(self, path, alias, version, size=3, patches=None):
        """Run ``migrate`` with small batches. ``patches`` replaces names in
        the loaded script for the run."""
        mod = load_script(path, alias)
        self.env.flush_all()
        replaced = (mock.patch.multiple(mod, **patches) if patches
                    else nullcontext())
        with self._batched(mod, size) as batches, replaced:
            result = mod.migrate(self.env.cr, version)
        self.env.invalidate_all()
        return result, batches

    def _run_pre(self, version='18.0.13.4.1', size=3):
        return self._run_script(PRE_COMPANY, 'mig_company_pre', version, size)

    def _set(self, records, **vals):
        """Write columns with raw SQL, the way an old schema left them."""
        self.env.flush_all()
        self.env.cr.execute(SQL(
            "UPDATE %s SET %s WHERE id = ANY(%s)",
            SQL.identifier(records._table),
            SQL(", ").join(SQL("%s = %s", SQL.identifier(name), value)
                           for name, value in vals.items()),
            records.ids))
        self.env.invalidate_all()

    def _lead(self, leadgen_id, **sql_vals):
        lead = self.env['crm.lead'].create({
            'name': 'Lead %s' % leadgen_id, 'type': 'lead',
            'meta_leadgen_id': leadgen_id})
        if sql_vals:
            self._set(lead, **sql_vals)
        return lead

    def _drop_account_not_null(self):
        # DDL is transactional in PostgreSQL, so the test rollback restores it.
        self.env.flush_all()
        self.env.cr.execute(
            "ALTER TABLE meta_account ALTER COLUMN company_id DROP NOT NULL")

    def _snapshot(self):
        snap = {}
        for table, column in COMPANY_COLUMNS:
            self.env.cr.execute(SQL(
                "SELECT id, %s FROM %s ORDER BY id",
                SQL.identifier(column), SQL.identifier(table)))
            snap[(table, column)] = self.env.cr.fetchall()
        return snap

    def _seed_company_backfill(self):
        """Rows in every table the pre-migrate fills, all with blank
        company columns as an upgraded database has them."""
        self.co_b = self.env['res.company'].create({'name': 'Meta Co B'})
        self.page_b = self.env['meta.page'].create({
            'name': 'Page B', 'page_id': 'PGB', 'account_id': self.account.id,
            'company_id': self.co_b.id})
        self.lead_b = self._lead('LGB', company_id=self.co_b.id)
        self.lead_page = self._lead('LGP', company_id=self.co_a.id,
                                    meta_page_id_ref=self.page.id)
        self.logs = self.Log.create([
            {'meta_leadgen_id': 'B%d' % i, 'status': 'failed',
             'page_id': self.page.id} for i in range(7)])
        self.log_b = self.Log.create({
            'meta_leadgen_id': 'LGB', 'status': 'success',
            'lead_id': self.lead_b.id, 'page_id': self.page.id})
        # No Page on the row: it comes from the lead's linked Page.
        self.log_no_page = self.Log.create({
            'meta_leadgen_id': 'LGP', 'status': 'success',
            'lead_id': self.lead_page.id})
        self.events = self.Event.create([
            {'leadgen_id': 'E%d' % i, 'page_id': 'PG1'} for i in range(7)])
        self.event_b = self.Event.create({
            'leadgen_id': 'LGB', 'page_id': 'PG1', 'lead_id': self.lead_b.id})
        self.event_pb = self.Event.create({
            'leadgen_id': 'EPB', 'page_id': 'PGB'})
        self._drop_account_not_null()
        self._set(self.account, company_id=None)
        self._set(self.page | self.page_b, lead_company_id=None)
        self._set(self.form, company_id=None)
        self._set(self.log_no_page, page_id=None)
        self._set(self.logs | self.log_b | self.log_no_page, company_id=None)
        self._set(self.events | self.event_b | self.event_pb,
                  page_company_id=None, company_id=None)

    # ------------------------------------------------------------------ #
    # batching helper
    # ------------------------------------------------------------------ #
    def test_iter_id_batches_keyset(self):
        logs = self.Log.create([
            {'meta_leadgen_id': 'KS%d' % i, 'status': 'failed'}
            for i in range(7)])
        self.env.flush_all()
        cr = self.env.cr
        mine = SQL("meta_leadgen_id LIKE %s", 'KS%')

        # Batch size read at call time, so a patched constant applies.
        with mock.patch.object(migration_utils, 'BATCH_SIZE', 3):
            batches = list(migration_utils.iter_id_batches(
                cr, 'meta_sync_log', mine))
        self.assertEqual([len(b) for b in batches], [3, 3, 1])
        flat = [i for b in batches for i in b]
        self.assertEqual(flat, sorted(logs.ids))

        # Rows the caller updates out of the predicate are neither repeated
        # nor skipped.
        seen = []
        for ids in migration_utils.iter_id_batches(
                cr, 'meta_sync_log',
                SQL("%s AND status = 'failed'", mine), batch_size=3):
            seen.extend(ids)
            cr.execute("UPDATE meta_sync_log SET status = 'success' "
                       "WHERE id = ANY(%s)", (ids,))
        self.assertEqual(seen, sorted(logs.ids))

        # A predicate that stays true for every row still ends.
        seen = []
        for ids in migration_utils.iter_id_batches(
                cr, 'meta_sync_log', mine, batch_size=2):
            seen.extend(ids)
            self.assertLessEqual(len(seen), 7)
        self.assertEqual(seen, sorted(logs.ids))

    def test_iter_child_batches_keyset(self):
        lead_x = self._lead('KX')
        lead_y = self._lead('KY')
        # Ids interleave between the two leads.
        logs = self.Log.create([
            {'meta_leadgen_id': 'KC%d' % i, 'status': 'success',
             'lead_id': (lead_y if i % 2 else lead_x).id}
            for i in range(7)])
        self.Log.create({'meta_leadgen_id': 'KCF', 'status': 'failed',
                         'lead_id': lead_x.id})
        self.env.flush_all()
        cr = self.env.cr
        parents = [lead_y.id, lead_x.id]
        batches = list(migration_utils.iter_child_batches(
            cr, 'meta_sync_log', 'lead_id', parents,
            SQL("status = 'success'"), batch_size=2))
        self.assertEqual([len(b) for b in batches], [2, 2, 2, 1])
        rows = self.Log.browse([i for b in batches for i in b])
        # Ordered by parent, then id, and every matching row exactly once.
        expected = sorted(logs, key=lambda log: (log.lead_id.id, log.id))
        self.assertEqual(rows.ids, [log.id for log in expected])
        # Without a condition the failed row is included too.
        seen = [i for b in migration_utils.iter_child_batches(
            cr, 'meta_sync_log', 'lead_id', parents, batch_size=3)
            for i in b]
        self.assertEqual(len(seen), 8)
        self.assertEqual(len(set(seen)), 8)

    # ------------------------------------------------------------------ #
    # company / ownership pre-migrate
    # ------------------------------------------------------------------ #
    def test_company_backfill_multi_batch(self):
        self._seed_company_backfill()
        result, batches = self._run_pre()

        self.assertEqual(self.account.company_id, self.main)
        self.assertEqual(self.page.lead_company_id, self.main)
        self.assertEqual(self.page_b.lead_company_id, self.co_b,
                         "the Page override wins over the account company")
        self.assertEqual(self.form.company_id, self.main)
        self.assertEqual(set(self.logs.mapped('company_id')), {self.main})
        self.assertEqual(self.log_b.company_id, self.co_b,
                         "a log follows its lead before its Page")
        self.assertEqual(self.log_no_page.page_id, self.page)
        self.assertEqual(self.log_no_page.company_id, self.co_a)
        self.assertEqual(set(self.events.mapped('page_company_id')),
                         {self.main})
        self.assertEqual(set(self.events.mapped('company_id')), {self.main})
        self.assertEqual(self.event_b.page_company_id, self.main)
        self.assertEqual(self.event_b.company_id, self.co_b,
                         "an event follows its lead before its Page")
        self.assertEqual(self.event_pb.page_company_id, self.co_b)
        self.assertEqual(self.event_pb.company_id, self.co_b)

        self.assertTrue(batches)
        self.assertLessEqual(max(size for _t, size in batches), 3)
        for table in ('meta_sync_log', 'meta_webhook_event'):
            self.assertGreaterEqual(
                len([1 for t, _s in batches if t == table]), 3,
                "%s should be read in several batches" % table)
        self.assertEqual(result['meta_sync_log.company_id']['visited'], 9)
        self.assertGreaterEqual(
            result['meta_sync_log.company_id']['batches'], 3)

    def test_sync_log_page_backfill(self):
        page2 = self.env['meta.page'].sudo().create({
            'name': 'Page 2', 'page_id': 'PG2',
            'account_id': self.account.id})
        blank_refs = {'meta_page_id_ref': None, 'meta_form_id_ref': None,
                      'meta_page_id': None, 'company_id': self.co_a.id}
        # (a) the lead's linked Page
        lead_a = self._lead('LGA', **dict(blank_refs,
                                          meta_page_id_ref=self.page.id))
        # (b) the Page of the lead's form
        lead_b = self._lead('LGBF', **dict(blank_refs,
                                           meta_form_id_ref=self.form.id))
        # (c) the lead's raw Meta page id
        lead_c = self._lead('LGC', **dict(blank_refs,
                                          meta_page_id=self.page.page_id))
        # (e) the lead's Page wins over the webhook event's Page
        lead_e = self._lead('LGE', **dict(blank_refs,
                                          meta_page_id_ref=self.page.id))
        self.Event.create([
            {'leadgen_id': 'LGD', 'page_id': self.page.page_id},
            {'leadgen_id': 'LGE', 'page_id': page2.page_id},
        ])
        log_a, log_b, log_c, log_d, log_e, log_f = self.Log.create([
            {'meta_leadgen_id': 'LGA', 'status': 'failed',
             'lead_id': lead_a.id},
            {'meta_leadgen_id': 'LGBF', 'status': 'failed',
             'lead_id': lead_b.id},
            {'meta_leadgen_id': 'LGC', 'status': 'failed',
             'lead_id': lead_c.id},
            # (d) no lead, but a webhook event with the same leadgen id
            {'meta_leadgen_id': 'LGD', 'status': 'failed'},
            {'meta_leadgen_id': 'LGE', 'status': 'failed',
             'lead_id': lead_e.id},
            # (f) nothing to resolve from, and it still holds a payload
            {'meta_leadgen_id': 'LGF', 'status': 'failed',
             'raw_payload': '{"id": "LGF"}'},
        ])
        all_logs = log_a | log_b | log_c | log_d | log_e | log_f
        self._set(all_logs, page_id=None, company_id=None)

        result, _batches = self._run_pre()

        for log in (log_a, log_b, log_c, log_d, log_e):
            self.assertEqual(log.page_id, self.page, log.meta_leadgen_id)
            self.assertEqual(log.company_id, self.co_a, log.meta_leadgen_id)
        self.assertFalse(log_f.page_id)
        self.assertFalse(log_f.company_id)
        self.assertEqual(result['meta_sync_log.page_id']['unresolved'], 1)
        self.assertEqual(result['meta_sync_log.company_id']['unresolved'], 1)
        self.assertEqual(result['meta_sync_log.company_id']['quarantined'], 1)

        admin = new_test_user(
            self.env, 'mig_meta_admin',
            groups='base.group_user,meta_lead_ads.group_meta_admin',
            company_id=self.co_a.id,
            company_ids=[Command.set([self.co_a.id])])
        visible = self.Log.with_user(admin).search(
            [('id', 'in', all_logs.ids)])
        self.assertEqual(set(visible.ids), set((all_logs - log_f).ids))
        self.assertNotIn(log_f.id, visible.ids,
                         "a blank row with a payload stays quarantined")

    def test_company_backfill_leaves_crm_lead_untouched(self):
        co_b = self.env['res.company'].create({'name': 'Meta Co B'})
        lead_b = self._lead('LGU1', company_id=co_b.id)
        lead_none = self._lead('LGU2', company_id=None)
        logs = self.Log.create([
            {'meta_leadgen_id': 'LGU1', 'status': 'success',
             'lead_id': lead_b.id, 'page_id': self.page.id},
            {'meta_leadgen_id': 'LGU2', 'status': 'success',
             'lead_id': lead_none.id, 'page_id': self.page.id},
        ])
        self._set(logs, company_id=None)
        self.env.cr.execute(
            "SELECT id, company_id, write_date FROM crm_lead "
            "WHERE id = ANY(%s) ORDER BY id", ([lead_b.id, lead_none.id],))
        before = self.env.cr.fetchall()

        self._run_pre()

        self.env.cr.execute(
            "SELECT id, company_id, write_date FROM crm_lead "
            "WHERE id = ANY(%s) ORDER BY id", ([lead_b.id, lead_none.id],))
        self.assertEqual(self.env.cr.fetchall(), before)
        self.assertEqual(lead_b.company_id, co_b)
        self.assertFalse(lead_none.company_id)

    def test_company_backfill_fresh_install_noop(self):
        log = self.Log.create({'meta_leadgen_id': 'LGN', 'status': 'failed',
                               'page_id': self.page.id})
        self._drop_account_not_null()
        self._set(self.account, company_id=None)
        self._set(log, company_id=None)
        before = self._snapshot()

        result, batches = self._run_pre(version=None)

        self.assertFalse(result)
        self.assertEqual(batches, [])
        self.assertEqual(self._snapshot(), before)

    def test_company_backfill_idempotent_counts(self):
        self._seed_company_backfill()
        unresolved = self.Log.create({
            'meta_leadgen_id': 'LGX', 'status': 'failed',
            'raw_payload': '{"id": "LGX"}'})
        self._set(unresolved, company_id=None)

        first, _b = self._run_pre()
        for key, counts in first.items():
            self.assertGreater(counts['changed'], 0, key)
        after_first = self._snapshot()

        second, _b = self._run_pre()
        self.assertEqual(set(second), set(first))
        for key, counts in second.items():
            self.assertEqual(counts['changed'], 0, key)
            self.assertEqual(counts['visited'], first[key]['unresolved'], key)
            self.assertEqual(counts['unresolved'], first[key]['unresolved'],
                             key)
        self.assertEqual(second['meta_sync_log.company_id']['visited'], 1)
        self.assertEqual(second['meta_account.company_id']['visited'], 0)
        self.assertEqual(self._snapshot(), after_first)

    def test_premigrate_warns_on_multi_company(self):
        mod = load_script(PRE_COMPANY, 'mig_company_pre')
        if self.env['res.company'].search_count([]) == 1:
            with self.assertNoLogs(mod._logger, 'WARNING'):
                mod.migrate(self.env.cr, '18.0.13.4.1')

        co_b = self.env['res.company'].create({'name': 'Meta Co B'})
        lead = self._lead('LGW', company_id=co_b.id)
        self._drop_account_not_null()
        self._set(self.account, company_id=None)
        with self.assertLogs(mod._logger, 'WARNING') as cap:
            mod.migrate(self.env.cr, '18.0.13.4.1')
        warnings = [r for r in cap.records if r.levelname == 'WARNING']
        self.assertEqual(len(warnings), 1)
        message = warnings[0].getMessage()
        self.assertIn(str(co_b.id), message)
        self.assertIn(str(self.main.id), message)
        self.assertNotIn(co_b.name, message)
        self.assertNotIn(self.main.name, message)
        self.assertNotIn(lead.name, message)
        self.assertNotIn(self.account.name, message)

    # ------------------------------------------------------------------ #
    # converted history scripts
    # ------------------------------------------------------------------ #
    def _envelope(self, leadgen_id, epoch):
        return json.dumps({'object': 'page', 'entry': [{
            'id': self.page.page_id, 'changes': [{
                'field': 'leadgen', 'value': {
                    'leadgen_id': leadgen_id, 'page_id': self.page.page_id,
                    'created_time': epoch}}]}]})

    @staticmethod
    def _utc(epoch):
        return datetime.fromtimestamp(epoch, tz=timezone.utc).replace(
            tzinfo=None)

    def test_name_flag_migration_batched(self):
        generated = self.env['crm.lead']
        for i in range(7):
            name = DEFAULT_LEAD_NAME.format(
                form_name='Spring %d' % i, date='2026-06-1%d' % i)
            generated |= self._lead('NG%d' % i, name=name)
        hand = (self._lead('NH1', name='Call back Jane')
                | self._lead('NH2', name='Meta lead from the fair'))
        # The original script searched active leads only.
        archived = self._lead('NA', name=DEFAULT_LEAD_NAME.format(
            form_name='Old', date='2026-01-01'), active=False)
        self._set(generated | hand | archived, meta_name_is_generated=False)

        _result, batches = self._run_script(
            POST_NAME_FLAG, 'mig_name_flag', '18.0.10.0.0')

        self.assertEqual(set(generated.mapped('meta_name_is_generated')),
                         {True})
        self.assertEqual(set(hand.mapped('meta_name_is_generated')), {False})
        self.assertFalse(archived.meta_name_is_generated)
        lead_batches = [size for table, size in batches
                        if table == 'crm_lead']
        self.assertGreaterEqual(len(lead_batches), 3)
        self.assertLessEqual(max(lead_batches), 3)

    def test_submitted_at_migration_batched(self):
        Lead = self.env['crm.lead']
        from_log, from_envelope, nothing = Lead, Lead, Lead
        for i in range(3):
            lead = self._lead('SL%d' % i)
            self.Log.create({
                'meta_leadgen_id': lead.meta_leadgen_id, 'status': 'success',
                'lead_id': lead.id, 'raw_payload': json.dumps({
                    'created_time': '2026-06-1%dT10:00:00+0000' % i})})
            from_log |= lead
        for i in range(3):
            lead = self._lead('SE%d' % i)
            self.Event.create({
                'leadgen_id': lead.meta_leadgen_id, 'page_id': 'PG1',
                'lead_id': lead.id,
                'raw_payload': self._envelope(lead.meta_leadgen_id,
                                              1780000000 + i)})
            from_envelope |= lead
        # Both sources: the sync-log payload wins.
        both = self._lead('SB')
        self.Log.create({
            'meta_leadgen_id': 'SB', 'status': 'success', 'lead_id': both.id,
            'raw_payload': json.dumps(
                {'created_time': '2026-05-01T09:15:00+0000'})})
        self.Event.create({
            'leadgen_id': 'SB', 'page_id': 'PG1', 'lead_id': both.id,
            'raw_payload': self._envelope('SB', 1780000000)})
        for i in range(2):
            nothing |= self._lead('SN%d' % i)
        everyone = from_log | from_envelope | both | nothing
        self._set(everyone, meta_submitted_at=None)

        _result, batches = self._run_script(
            POST_SUBMITTED, 'mig_submitted', '18.0.13.3.6')

        for i, lead in enumerate(from_log):
            self.assertEqual(lead.meta_submitted_at,
                             datetime(2026, 6, 10 + i, 10, 0))
        for i, lead in enumerate(from_envelope):
            self.assertEqual(lead.meta_submitted_at,
                             self._utc(1780000000 + i))
        self.assertEqual(both.meta_submitted_at, datetime(2026, 5, 1, 9, 15))
        self.assertFalse(any(nothing.mapped('meta_submitted_at')))
        lead_batches = [size for table, size in batches
                        if table == 'crm_lead']
        self.assertGreaterEqual(len(lead_batches), 3)
        self.assertLessEqual(max(lead_batches), 3)

    def test_submitted_at_child_reads_bounded(self):
        lead = self._lead('CB')
        times = ('not a date', '2026-06-15T08:30:00+0000',
                 '2026-06-16T08:30:00+0000', '2026-06-17T08:30:00+0000',
                 '2026-06-18T08:30:00+0000')
        for created in times:
            self.Log.create({
                'meta_leadgen_id': 'CB', 'status': 'success',
                'lead_id': lead.id,
                'raw_payload': json.dumps({'created_time': created})})
        # Envelope-only lead with several events: the first by id is for
        # another lead id and does not count.
        lead_env = self._lead('CE')
        self.Event.create([
            {'leadgen_id': 'CE-other', 'page_id': 'PG1',
             'lead_id': lead_env.id,
             'raw_payload': self._envelope('CE-other', 1780000000)},
            {'leadgen_id': 'CE', 'page_id': 'PG1', 'lead_id': lead_env.id,
             'raw_payload': self._envelope('CE', 1780000500)},
            {'leadgen_id': 'CE-late', 'page_id': 'PG1',
             'lead_id': lead_env.id,
             'raw_payload': self._envelope('CE', 1780000900)},
        ])
        self._set(lead | lead_env, meta_submitted_at=None)

        _result, batches = self._run_script(
            POST_SUBMITTED, 'mig_submitted', '18.0.13.3.6', size=2)

        self.assertEqual(lead.meta_submitted_at, datetime(2026, 6, 15, 8, 30),
                         "the first parseable payload by id wins")
        self.assertEqual(lead_env.meta_submitted_at,
                         self._utc(1780000500))
        log_batches = [s for t, s in batches if t == 'meta_sync_log']
        event_batches = [s for t, s in batches if t == 'meta_webhook_event']
        self.assertGreaterEqual(len(log_batches), 3)
        self.assertGreaterEqual(len(event_batches), 2)
        self.assertLessEqual(max(log_batches + event_batches), 2)

    def test_submitted_at_children_paged_by_lead(self):
        # Child rows are paged by (lead, id) so the lead index is usable on
        # a large table. Rows of two leads interleave by id and span
        # batches; the first parseable payload of each lead still wins.
        lead_x = self._lead('PX')
        lead_y = self._lead('PY')
        rows = (
            (lead_x, 'not a date'), (lead_y, '2026-07-01T08:00:00+0000'),
            (lead_x, '2026-07-02T08:00:00+0000'),
            (lead_y, '2026-07-03T08:00:00+0000'),
            (lead_x, '2026-07-04T08:00:00+0000'),
        )
        for lead, created in rows:
            self.Log.create({
                'meta_leadgen_id': lead.meta_leadgen_id, 'status': 'success',
                'lead_id': lead.id,
                'raw_payload': json.dumps({'created_time': created})})
        lead_env = self._lead('PE')
        self.Event.create([
            {'leadgen_id': 'PE-%d' % i, 'page_id': 'PG1',
             'lead_id': lead_env.id,
             'raw_payload': self._envelope('PE', 1780000000 + i)}
            for i in (0, 900)])
        self._set(lead_x | lead_y | lead_env, meta_submitted_at=None)

        _result, batches = self._run_script(
            POST_SUBMITTED, 'mig_submitted', '18.0.13.3.6', size=2)

        self.assertEqual(lead_x.meta_submitted_at, datetime(2026, 7, 2, 8, 0))
        self.assertEqual(lead_y.meta_submitted_at, datetime(2026, 7, 1, 8, 0))
        self.assertEqual(lead_env.meta_submitted_at, self._utc(1780000000))
        self.assertIn('meta_sync_log', self.child_tables)
        self.assertIn('meta_webhook_event', self.child_tables)
        self.assertLessEqual(max(s for _t, s in batches), 2)

    # ------------------------------------------------------------------ #
    # 18.0.13.8.0: phone keys, date formats, removed-lead records
    # ------------------------------------------------------------------ #
    def _run_dedup_post(self, version='18.0.13.7.0', size=3):
        return self._run_script(POST_DEDUP, 'mig_dedup_post', version, size)

    def _phone_lead(self, leadgen_id, phone, **sql_vals):
        vals = {'name': 'Lead %s' % leadgen_id, 'type': 'lead',
                'meta_leadgen_id': leadgen_id}
        if phone:
            vals['phone'] = phone
        lead = self.env['crm.lead'].create(vals)
        if sql_vals:
            self._set(lead, **sql_vals)
        return lead

    def _seed_phone_keys(self):
        egypt = self.env.ref('base.eg')
        saudi = self.env.ref('base.sa')
        self.co_eg = self.env['res.company'].create(
            {'name': 'Meta Co Egypt', 'country_id': egypt.id})
        self.assertEqual(self.co_eg.partner_id.country_id, egypt)
        stale = {'meta_phone_normalized': 'STALE'}
        self.eg_national = self._phone_lead(
            'PKN', '01001234567', company_id=self.co_eg.id, country_id=None,
            **stale)
        self.eg_plus = self._phone_lead(
            'PKP', '+20 100 123 4567', company_id=self.co_eg.id,
            country_id=None, **stale)
        self.sa_lead = self._phone_lead(
            'PKS', '0501234567', company_id=self.co_eg.id,
            country_id=saudi.id, **stale)
        self.blank_lead = self._phone_lead(
            'PKB', '01001234567', company_id=None, country_id=None, **stale)
        self.no_phone = self._phone_lead(
            'PKX', None, company_id=self.co_eg.id, meta_phone_normalized=None)

    def _assert_phone_keys(self, batches):
        self.assertEqual(self.eg_national.meta_phone_normalized,
                         '201001234567')
        self.assertEqual(self.eg_plus.meta_phone_normalized, '201001234567')
        self.assertEqual(self.sa_lead.meta_phone_normalized, '966501234567',
                         "the lead's own country wins over its company's")
        self.assertEqual(self.blank_lead.meta_phone_normalized,
                         '01001234567')
        self.assertFalse(self.no_phone.meta_phone_normalized)
        self.env.cr.execute(
            "SELECT count(*) FROM crm_lead WHERE phone IS NOT NULL")
        phone_count = self.env.cr.fetchone()[0]
        lead_batches = [s for t, s in batches if t == 'crm_lead']
        self.assertGreaterEqual(len(lead_batches), 2)
        self.assertLessEqual(max(lead_batches), 3)
        self.assertEqual(sum(lead_batches), phone_count,
                         "every lead with a phone is read, and only those")

    def test_dedup_post_phone_keys_fallback(self):
        self._seed_phone_keys()
        with mock.patch.object(phone_key, 'phonenumbers', None):
            result, batches = self._run_dedup_post()
        self._assert_phone_keys(batches)
        self.assertGreaterEqual(result['keys'], 4)

    @unittest.skipUnless(phone_key.phonenumbers, "phonenumbers not installed")
    def test_dedup_post_phone_keys_library(self):
        self._seed_phone_keys()
        _result, batches = self._run_dedup_post()
        self._assert_phone_keys(batches)

    def test_dedup_post_phone_keys_default_mode(self):
        # Whatever the install has, library or not.
        self._seed_phone_keys()
        _result, batches = self._run_dedup_post()
        self._assert_phone_keys(batches)

    @contextmanager
    def _count_updates(self, *tables):
        """Record every UPDATE statement run on one of ``tables``."""
        statements = []
        cursor_class = type(self.env.cr)
        real = cursor_class.execute

        def spy(cur, query, *args, **kwargs):
            code = query.code if isinstance(query, SQL) else str(query)
            words = code.split()[:2]
            if len(words) == 2 and words[0] == 'UPDATE' \
                    and words[1] in tables:
                statements.append(code)
            return real(cur, query, *args, **kwargs)

        with mock.patch.object(cursor_class, 'execute', spy):
            yield statements

    def _count_lead_updates(self):
        return self._count_updates('crm_lead')

    def test_dedup_post_one_update_per_batch(self):
        self._seed_phone_keys()
        # Make every phone lead stale, so every batch has something to write.
        self._phone_lead('PKY', '0123 456 789')
        self.env.flush_all()
        self.env.cr.execute("UPDATE crm_lead SET meta_phone_normalized = "
                            "'STALE' WHERE phone IS NOT NULL")
        with self._count_lead_updates() as statements:
            result, batches = self._run_dedup_post()
        lead_batches = [s for t, s in batches if t == 'crm_lead']
        self.assertGreaterEqual(len(lead_batches), 2)
        self.assertEqual(len(statements), len(lead_batches),
                         "one UPDATE per batch, never one per lead")
        self.assertEqual(result['keys'], sum(lead_batches))

        # A second run finds nothing to change and writes nothing.
        with self._count_lead_updates() as statements:
            second, _b = self._run_dedup_post()
        self.assertEqual(statements, [])
        self.assertEqual(second['keys'], 0)
        self.assertEqual(second['leads'], result['leads'])

    def _mapping(self, form, meta_key, field_name, **sql_vals):
        field = self.env['ir.model.fields']._get('crm.lead', field_name)
        mapping = self.env['meta.field.mapping'].create({
            'form_id': form.id, 'meta_key': meta_key,
            'crm_field_id': field.id})
        if sql_vals:
            self._set(mapping, **sql_vals)
        return mapping

    def test_dedup_post_date_formats(self):
        form2 = self.env['meta.lead.form'].create({
            'name': 'Second', 'form_id': 'F2', 'page_id': self.page.id})
        form3 = self.env['meta.lead.form'].create({
            'name': 'Third', 'form_id': 'F3', 'page_id': self.page.id})
        blank = self._mapping(self.form, 'when', 'x_meta_typed_date',
                              date_format=None)
        blank_dt = self._mapping(form2, 'closed', 'x_meta_typed_when',
                                 date_format=None)
        mdy = self._mapping(form2, 'by_when', 'x_meta_typed_date',
                            date_format='mdy')
        ymd = self._mapping(form3, 'due', 'x_meta_typed_date',
                            date_format='ymd')
        phone = self._mapping(self.form, 'tel', 'phone', date_format=None)

        result, _b = self._run_dedup_post()

        self.assertEqual(blank.date_format, 'dmy')
        self.assertEqual(blank_dt.date_format, 'dmy')
        self.assertEqual(mdy.date_format, 'mdy')
        self.assertEqual(ymd.date_format, 'ymd')
        self.assertFalse(phone.date_format)
        self.assertEqual(result['mappings'], 2)
        # Mappings made after the upgrade get the same default.
        form4 = self.env['meta.lead.form'].create({
            'name': 'Fourth', 'form_id': 'F4', 'page_id': self.page.id})
        self.assertFalse(self._mapping(form4, 'other', 'phone').date_format)
        self.assertEqual(
            self._mapping(form4, 'start', 'x_meta_typed_date').date_format,
            'dmy')

    def _tomb(self, leadgen_id, **sql_vals):
        tomb = self.env['meta.leadgen.tombstone'].sudo().create(
            {'leadgen_id': leadgen_id})
        self._set(tomb, **dict({'page_id': None, 'form_id': None,
                                'company_id': None}, **sql_vals))
        return tomb

    def test_dedup_post_removed_record_links(self):
        co_b = self.env['res.company'].create({'name': 'Meta Co B'})
        page_b = self.env['meta.page'].create({
            'name': 'Page B', 'page_id': 'PGB', 'account_id': self.account.id,
            'company_id': co_b.id})
        form_b = self.env['meta.lead.form'].create({
            'name': 'Form B', 'form_id': 'FB', 'page_id': page_b.id})
        page_c = self.env['meta.page'].create({
            'name': 'Page C', 'page_id': 'PGC', 'account_id': self.account.id,
            'company_id': co_b.id})

        # All empty, two sync rows: the newer one wins.
        empty = self._tomb('TBE')
        older = self.Log.create({'meta_leadgen_id': 'TBE', 'status': 'failed',
                                 'page_id': self.page.id})
        self._set(older, form_id=self.form.id, company_id=self.co_a.id,
                  status='success')
        newer = self.Log.create({'meta_leadgen_id': 'TBE', 'status': 'failed',
                                 'page_id': page_b.id})
        self._set(newer, form_id=form_b.id, company_id=co_b.id)
        # Has a Page only: the form and company are filled, the Page kept.
        page_only = self._tomb('TBP', page_id=self.page.id)
        log_p = self.Log.create({'meta_leadgen_id': 'TBP', 'status': 'failed',
                                 'page_id': page_b.id})
        self._set(log_p, form_id=form_b.id, company_id=co_b.id)
        # Page and form, no sync row: the company comes from the Page.
        page_form = self._tomb('TBF', page_id=page_c.id, form_id=form_b.id)
        account_page = self._tomb('TBA', page_id=self.page.id,
                                  form_id=self.form.id)
        self._set(self.page, company_id=None, lead_company_id=None)
        # No trace anywhere.
        lost = self._tomb('TBL')
        # Complete already: left as it is.
        full = self._tomb('TBC', page_id=self.page.id, form_id=self.form.id,
                          company_id=co_b.id)
        log_c = self.Log.create({'meta_leadgen_id': 'TBC', 'status': 'failed',
                                 'page_id': page_b.id})
        self._set(log_c, form_id=form_b.id, company_id=self.co_a.id)

        result, batches = self._run_dedup_post(size=2)

        self.assertEqual((empty.page_id, empty.form_id, empty.company_id),
                         (page_b, form_b, co_b))
        self.assertEqual(
            (page_only.page_id, page_only.form_id, page_only.company_id),
            (self.page, form_b, co_b))
        self.assertEqual(page_form.company_id, co_b)
        self.assertEqual(page_form.page_id, page_c)
        self.assertEqual(account_page.company_id, self.account.company_id,
                         "a Page without a company uses its account's")
        self.assertFalse(lost.page_id or lost.form_id or lost.company_id)
        self.assertEqual((full.page_id, full.form_id, full.company_id),
                         (self.page, self.form, co_b))
        self.assertEqual(result['removed_linked'], 4)
        tomb_batches = [s for t, s in batches
                        if t == 'meta_leadgen_tombstone']
        self.assertGreaterEqual(len(tomb_batches), 2)
        self.assertLessEqual(max(tomb_batches), 2)

        second, _b = self._run_dedup_post(size=2)
        self.assertEqual(second['removed_linked'], 0)

    def test_dedup_post_settles_failures_of_removed_leads(self):
        self._tomb('TSR', page_id=self.page.id)
        removed_row = self.Log.create({
            'meta_leadgen_id': 'TSR', 'status': 'failed',
            'page_id': self.page.id, 'error_message': 'Graph timeout'})
        kept_row = self.Log.create({
            'meta_leadgen_id': 'TSK', 'status': 'failed',
            'page_id': self.page.id, 'error_message': 'Graph timeout'})
        removed_event, kept_event = self.Event.create([
            {'leadgen_id': 'TSR', 'page_id': 'PG1'},
            {'leadgen_id': 'TSK', 'page_id': 'PG1'}])
        self._set(removed_event | kept_event, status='failed',
                  error_message='Graph timeout')
        self._tomb('TSP', page_id=self.page.id)
        pending = self.Event.create({'leadgen_id': 'TSP', 'page_id': 'PG1'})

        result, _b = self._run_dedup_post()

        self.assertEqual(removed_row.status, 'skipped_removed')
        self.assertEqual(removed_row.error_message, 'Removed in Odoo; skipped')
        self.assertEqual(kept_row.status, 'failed')
        self.assertEqual(removed_event.status, 'done')
        self.assertEqual(removed_event.error_message,
                         'Removed in Odoo; skipped')
        self.assertFalse(removed_event.next_attempt_at)
        self.assertEqual(kept_event.status, 'failed')
        self.assertEqual(pending.status, 'pending',
                         "pending events are left to the drain")
        self.assertEqual(result['rows_settled'], 1)
        self.assertEqual(result['events_settled'], 1)

    def test_dedup_post_log_has_counts_only(self):
        self._seed_phone_keys()
        mod = load_script(POST_DEDUP, 'mig_dedup_post')
        self.env.flush_all()
        with mock.patch.object(mod, '_logger') as logger:
            mod.migrate(self.env.cr, '18.0.13.7.0')
        self.env.invalidate_all()
        self.assertEqual(logger.info.call_count, 1)
        message, counts = logger.info.call_args.args
        text = message % counts
        self.assertIn('4 changed', text)
        for secret in ('01001234567', '201001234567', '0501234567', 'PKN',
                       str(self.eg_national.id)):
            self.assertNotIn(secret, text)

    def test_dedup_post_fresh_install_noop(self):
        self._seed_phone_keys()
        self._mapping(self.form, 'when', 'x_meta_typed_date', date_format=None)
        self._tomb('TBN', page_id=self.page.id)
        result, batches = self._run_dedup_post(version=None)
        self.assertIsNone(result)
        self.assertEqual(batches, [])
        self.assertEqual(self.eg_national.meta_phone_normalized, 'STALE')

    # ------------------------------------------------------------------ #
    # 18.0.13.9.0: mappings onto blocked targets are switched off
    # ------------------------------------------------------------------ #
    DISABLED_PREFIX = 'Disabled in 18.0.13.9.0:'
    PIPELINE = 'drives your sales pipeline'

    def _run_mapping_targets_post(self, version='18.0.13.8.2', size=3):
        return self._run_script(POST_MAPPING_TARGETS,
                                'mig_mapping_targets_post', version, size)

    def _legacy_row(self, form, meta_key, field_name, **sql_vals):
        """A mapping as an older version could save it.

        The ORM refuses blocked targets now, so the row is made on city and
        its target put back with SQL. A form's own city row has to be made
        after its legacy rows, or the once-per-form check refuses them.
        """
        mapping = self._mapping(form, meta_key, 'city')
        field = self.env['ir.model.fields']._get('crm.lead', field_name)
        self._set(mapping, crm_field_id=field.id, **sql_vals)
        return mapping

    def _label(self, field_name):
        return self.env['ir.model.fields']._get(
            'crm.lead', field_name).with_context(lang='en_US').field_description

    def _seed_blocked_rows(self):
        """Eight active rows on blocked targets over two forms, four each,
        so with batches of 3 both forms span two batches. Each form also
        has one row that stays on."""
        self.form2 = self.env['meta.lead.form'].create({
            'name': 'Second', 'form_id': 'F2', 'page_id': self.page.id})
        targets = (
            (self.form, 'legacy_rep', 'user_id'),
            (self.form, 'legacy_squad', 'team_id'),
            (self.form, 'legacy_when', 'date_deadline'),
            (self.form, 'legacy_plan', 'recurring_plan'),
            (self.form2, 'legacy_title', 'name'),
            (self.form2, 'legacy_company', 'partner_id'),
            (self.form2, 'legacy_heard', 'source_id'),
            (self.form2, 'legacy_live', 'active'),
        )
        self.blocked = {
            name: self._legacy_row(form, key, name)
            for form, key, name in targets}
        self.seeded_keys = [key for _form, key, _name in targets] + [
            'legacy_town', 'legacy_colour']
        # An allowed target and a technical one: both keep working.
        self.kept_city = self._legacy_row(self.form, 'legacy_town', 'city')
        self.kept_color = self._legacy_row(self.form2, 'legacy_colour', 'color')
        self.seeded_rows = self.kept_city | self.kept_color
        for mapping in self.blocked.values():
            self.seeded_rows |= mapping

    def _mapping_rows(self):
        self.env.cr.execute(
            "SELECT id, form_id, meta_key, crm_field_id FROM meta_field_mapping "
            "ORDER BY id")
        return self.env.cr.fetchall()

    def _forms_temp_table(self):
        self.env.cr.execute(
            "SELECT to_regclass('pg_temp.meta_mapping_forms_touched')")
        return self.env.cr.fetchone()[0]

    def test_mapping_targets_post_archives_blocked_rows_only(self):
        self._seed_blocked_rows()
        before = self._mapping_rows()

        result, _b = self._run_mapping_targets_post()

        for name, mapping in self.blocked.items():
            self.assertFalse(mapping.active, name)
        self.assertTrue(self.kept_city.active)
        self.assertTrue(self.kept_color.active,
                        "technical targets keep working")
        self.assertFalse(self.kept_city.disabled_reason)
        self.assertFalse(self.kept_color.disabled_reason)
        self.assertEqual(self._mapping_rows(), before,
                         "rows are archived in place, none removed or moved")
        self.assertEqual(result['archived'], 8)

    def test_mapping_targets_post_reasons_per_target(self):
        self._seed_blocked_rows()
        self._run_mapping_targets_post()
        reason = {name: mapping.disabled_reason
                  for name, mapping in self.blocked.items()}

        for name, text in reason.items():
            self.assertTrue(text and text.startswith(self.DISABLED_PREFIX),
                            name)
        self.assertEqual(
            reason['user_id'],
            "Disabled in 18.0.13.9.0: the salesperson is now set per Page, "
            "in the Page's Lead Routing settings.")
        self.assertEqual(
            reason['team_id'],
            "Disabled in 18.0.13.9.0: the sales team is now set per Page, "
            "in the Page's Lead Routing settings.")
        for name in ('date_deadline', 'recurring_plan'):
            self.assertEqual(
                reason[name],
                "Disabled in 18.0.13.9.0: %s drives your sales pipeline, so "
                "it can no longer be filled from a form answer."
                % self._label(name))
        self.assertIn(self._label('date_deadline'), reason['date_deadline'])
        self.assertEqual(
            reason['name'],
            "Disabled in 18.0.13.9.0: the lead title comes from the title "
            "template in Settings and would overwrite this answer.")
        self.assertEqual(
            reason['partner_id'],
            "Disabled in 18.0.13.9.0: the customer is linked by Odoo's "
            "contact matching, not from a form answer.")
        self.assertEqual(
            reason['source_id'],
            "Disabled in 18.0.13.9.0: campaign, source and medium come from "
            "the ad that brought the lead in.")
        self.assertEqual(
            reason['active'],
            "Disabled in 18.0.13.9.0: %s is set by the module when the lead "
            "is created, so it can no longer be filled from a form answer."
            % self._label('active'))

    def test_mapping_targets_post_missing_en_US_label_uses_the_name(self):
        self._seed_blocked_rows()
        # Labels as a database might hold them: no en_US key at all, or an
        # empty one.
        self.env.flush_all()
        self.env.cr.execute(
            "UPDATE ir_model_fields SET field_description = %s::jsonb "
            "WHERE model = 'crm.lead' AND name = 'date_deadline'",
            (json.dumps({'fr_FR': 'Echeance'}),))
        self.env.cr.execute(
            "UPDATE ir_model_fields SET field_description = %s::jsonb "
            "WHERE model = 'crm.lead' AND name = 'recurring_plan'",
            (json.dumps({'en_US': ''}),))

        self._run_mapping_targets_post()

        for name in ('date_deadline', 'recurring_plan'):
            text = self.blocked[name].disabled_reason
            self.assertIn(name, text)
            self.assertIn(self.PIPELINE, text)
            self.assertTrue(text.startswith(self.DISABLED_PREFIX))
        self.assertNotIn('Echeance', self.blocked['date_deadline']
                         .disabled_reason)

    def test_mapping_targets_post_bounded_batches_one_update_each(self):
        self._seed_blocked_rows()
        with self._count_updates('meta_field_mapping') as statements:
            result, batches = self._run_mapping_targets_post(size=3)
        mapping_batches = [s for t, s in batches if t == 'meta_field_mapping']
        self.assertEqual(mapping_batches, [3, 3, 2])
        self.assertEqual(len(statements), len(mapping_batches),
                         "one UPDATE per batch, never one per row")
        self.assertEqual(result['archived'], sum(mapping_batches))

    def test_mapping_targets_post_refreshes_form_counts(self):
        self._seed_blocked_rows()
        form3 = self.env['meta.lead.form'].create({
            'name': 'Third', 'form_id': 'F3', 'page_id': self.page.id})
        self._mapping(form3, 'legacy_other_town', 'city')
        self.env.flush_all()
        self.assertEqual(self.form.mapping_count, 5)
        self.assertEqual(self.form2.mapping_count, 5)

        result, _b = self._run_mapping_targets_post(size=3)

        for form in (self.form, self.form2, form3):
            self.env.cr.execute(
                "SELECT count(*) FROM meta_field_mapping "
                "WHERE form_id = %s AND active", (form.id,))
            # Fetched first: reading the form runs a query on this cursor.
            active_rows = self.env.cr.fetchone()[0]
            self.assertEqual(form.mapping_count, active_rows, form.name)
        self.assertEqual(self.form.mapping_count, 1,
                         "a form whose rows span two batches ends right")
        self.assertEqual(self.form2.mapping_count, 1)
        self.assertEqual(form3.mapping_count, 1)
        self.assertEqual(result['forms'], 2,
                         "a form seen in two batches is counted once")
        self.assertIsNone(self._forms_temp_table())

    def test_mapping_targets_post_second_run_changes_nothing(self):
        self._seed_blocked_rows()
        first, _b = self._run_mapping_targets_post()
        self.assertIsNone(self._forms_temp_table())
        reasons = self.seeded_rows.mapped('disabled_reason')

        with self._count_updates('meta_field_mapping',
                                 'meta_lead_form') as statements:
            second, _b = self._run_mapping_targets_post()
        self.assertEqual(second, {'archived': 0, 'forms': 0})
        self.assertEqual(statements, [])
        self.assertEqual(self.seeded_rows.mapped('disabled_reason'), reasons)
        self.assertEqual(first['archived'], 8)
        self.assertIsNone(self._forms_temp_table())

    def test_mapping_targets_post_fresh_install_noop(self):
        self._seed_blocked_rows()
        result, batches = self._run_mapping_targets_post(version=None)
        self.assertIsNone(result)
        self.assertEqual(batches, [])
        for name, mapping in self.blocked.items():
            self.assertTrue(mapping.active, name)
            self.assertFalse(mapping.disabled_reason, name)
        self.assertIsNone(self._forms_temp_table())

    def test_mapping_targets_post_archived_row_left_alone(self):
        # Made first, while the form has no city row yet.
        old = self._legacy_row(self.form, 'legacy_old_rep', 'user_id',
                               active=False)
        self._seed_blocked_rows()
        self.assertFalse(old.disabled_reason)

        result, _b = self._run_mapping_targets_post()

        self.assertFalse(old.active)
        self.assertFalse(old.disabled_reason,
                         "a row switched off before keeps no reason")
        self.assertEqual(result['archived'], 8)

    def test_mapping_targets_post_archives_links_to_users_and_contacts(self):
        rows = {name: self._legacy_row(self.form, 'legacy_link_%s' % name, name)
                for name in self.LINKS}

        result, _b = self._run_mapping_targets_post()

        for name, row in rows.items():
            self.assertFalse(row.active, name)
            self.assertEqual(
                row.disabled_reason,
                "%s %s links the lead to a user or contact, which decides "
                "who owns or sees it, so it can no longer be filled from a "
                "form answer." % (self.DISABLED_PREFIX, self._label(name)))
        self.assertEqual(result['archived'], len(rows))

    def test_mapping_targets_post_reason_for_partner_assignment_fields(self):
        # Manual fields must start with x_, so these two can't be made here.
        mod = load_script(POST_MAPPING_TARGETS, 'mig_mapping_targets_post')
        for name, label in (('partner_assigned_id', 'Assigned Partner'),
                            ('date_partner_assign',
                             'Partner Assignment Date')):
            self.assertEqual(
                mod._reason(name, label),
                "%s the assigned partner decides which reseller can see the "
                "lead, so it can no longer be filled from a form answer."
                % self.DISABLED_PREFIX, name)

    def test_mapping_targets_post_log_has_counts_only(self):
        self._seed_blocked_rows()
        mod = load_script(POST_MAPPING_TARGETS, 'mig_mapping_targets_post')
        self.env.flush_all()
        with mock.patch.object(mod, '_logger') as logger:
            result = mod.migrate(self.env.cr, '18.0.13.8.2')
        self.env.invalidate_all()
        self.assertEqual(logger.info.call_count, 1)
        message, counts = logger.info.call_args.args
        self.assertEqual(counts, result)
        self.assertEqual(result, {'archived': 8, 'forms': 2})
        # The formatted info line, and anything else logged at any level.
        lines = [message % counts] + [repr(c) for c in logger.mock_calls]
        for line in lines:
            for secret in self.seeded_keys + ['Contact Us', 'Second']:
                self.assertNotIn(secret, line)

    def test_retention_migration_unchanged(self):
        icp = self.env['ir.config_parameter'].sudo()
        mod = load_script(POST_RETENTION, 'mig_retention')
        for before, after in ((False, '14'), ('90', '14'), ('30', '30')):
            icp.set_param(RETENTION_KEY, before)
            mod.migrate(self.env.cr, '18.0.10.1.0')
            self.assertEqual(icp.get_param(RETENTION_KEY), after, before)

    # ------------------------------------------------------------------ #
    # name cache: company-less rows dropped before the company column
    # ------------------------------------------------------------------ #
    def _seed_name_cache(self, count):
        Cache = self.env['meta.name.cache']
        # Start from an empty table so the counts below are exact.
        Cache.sudo().search([]).unlink()
        rows = Cache.browse()
        for i in range(count):
            rows |= Cache.create({
                'object_type': 'campaign', 'graph_id': 'NCM%s' % i,
                'name': 'Hidden Campaign %s' % i,
                'company_id': (self.main if i % 2 else self.co_a).id})
        self.env.flush_all()
        return rows

    def _cache_count(self):
        self.env.cr.execute("SELECT count(*) FROM meta_name_cache")
        return self.env.cr.fetchone()[0]

    def test_name_cache_dropped_in_batches(self):
        self._seed_name_cache(7)
        logger = mock.Mock()
        # On a real upgrade the company column is not there yet.
        result, batches = self._run_script(
            PRE_NAME_CACHE, 'mig_name_cache_pre', '18.0.13.9.1', size=3,
            patches={'column_exists': lambda cr, table, column: False,
                     '_logger': logger})
        self.assertEqual(result, {'name_cache_dropped': 7})
        self.assertEqual(batches, [('meta_name_cache', 3),
                                   ('meta_name_cache', 3),
                                   ('meta_name_cache', 1)])
        self.assertEqual(self._cache_count(), 0)
        # Count only in the log, never a name.
        self.assertEqual(logger.info.call_args.args[1:], (7,))
        for call in logger.mock_calls:
            self.assertNotIn('Hidden Campaign', repr(call))

    def test_name_cache_rows_with_company_kept(self):
        rows = self._seed_name_cache(4)
        result, batches = self._run_script(
            PRE_NAME_CACHE, 'mig_name_cache_pre', '18.0.13.9.1', size=3)
        self.assertEqual(result, {'name_cache_dropped': 0})
        self.assertEqual(batches, [])
        self.assertEqual(self._cache_count(), 4)
        self.assertEqual(rows.exists(), rows)

    def test_name_cache_rerun_drops_only_blank_rows(self):
        rows = self._seed_name_cache(5)
        # DDL is transactional in PostgreSQL, so the test rollback restores it.
        self.env.cr.execute(
            "ALTER TABLE meta_name_cache ALTER COLUMN company_id DROP NOT NULL")
        self._set(rows[:2], company_id=None)
        result, batches = self._run_script(
            PRE_NAME_CACHE, 'mig_name_cache_pre', '18.0.13.9.1', size=3)
        self.assertEqual(result, {'name_cache_dropped': 2})
        self.assertEqual(batches, [('meta_name_cache', 2)])
        self.assertEqual(rows.exists(), rows[2:])
        again, _batches = self._run_script(
            PRE_NAME_CACHE, 'mig_name_cache_pre', '18.0.13.9.1', size=3)
        self.assertEqual(again, {'name_cache_dropped': 0})
        self.assertEqual(self._cache_count(), 3)

    def test_name_cache_fresh_install_noop(self):
        self._seed_name_cache(3)
        mod = load_script(PRE_NAME_CACHE, 'mig_name_cache_pre')
        with mock.patch.object(mod, 'column_exists', return_value=False):
            self.assertIsNone(mod.migrate(self.env.cr, None))
        self.assertEqual(self._cache_count(), 3)

    # ------------------------------------------------------------------ #
    # 18.0.13.11.0: a database holding more than one app is only reported
    # ------------------------------------------------------------------ #
    def _plant_account(self, account_id, app_id, secret, active=True):
        # The one-app rule refuses a second app through the ORM, so the
        # account is created archived and given its app in the table, as a
        # database connected to two apps before the rule has it.
        other = self.env['meta.account'].sudo().create({
            'name': 'Other %s' % account_id, 'account_id': account_id,
            'active': False, 'access_token': 'tok_%s' % account_id})
        self._set(other, app_id=app_id, app_secret=secret, active=active)
        return other

    def _accounts_state(self):
        self.env.cr.execute(
            "SELECT id, app_id, app_secret, active FROM meta_account"
            " ORDER BY id")
        return self.env.cr.fetchall()

    def test_single_app_post_migrate_counts_apps(self):
        self._plant_account('ACC2', 'other_app', 'other_secret')
        # Same app with spaces, an archived third app and a blank app are
        # not counted as more apps.
        self._plant_account('ACC3', ' app_test ', 'secret_test')
        self._plant_account('ACC4', 'retired_app', 'retired_secret',
                            active=False)
        self._plant_account('ACC5', '  ', 'blank_secret')
        before = self._accounts_state()
        mod = load_script(POST_SINGLE_APP, 'mig_single_app_post')
        self.env.flush_all()
        with self.assertLogs(mod._logger, 'WARNING') as cap:
            result = mod.migrate(self.env.cr, '18.0.13.10.0')
        self.env.invalidate_all()
        self.assertEqual(result, {'active_app_ids': 2})
        warnings = [r for r in cap.records if r.levelname == 'WARNING']
        self.assertEqual(len(warnings), 1)
        text = warnings[0].getMessage()
        self.assertIn('2 different App IDs on active Meta accounts', text)
        for value in ('app_test', 'other_app', 'retired_app', 'secret_test',
                      'other_secret', 'retired_secret', 'blank_secret',
                      'ACC1', 'ACC2', 'tok_'):
            self.assertNotIn(value, text)
        for record in cap.records:
            self.assertNotIn('secret', record.getMessage())
        # Nothing on the accounts changes.
        self.assertEqual(self._accounts_state(), before)

    def test_single_app_post_migrate_one_app_no_warning(self):
        self._plant_account('ACC3', ' app_test ', 'secret_test')
        mod = load_script(POST_SINGLE_APP, 'mig_single_app_post')
        self.env.flush_all()
        with mock.patch.object(mod, '_logger') as logger:
            result = mod.migrate(self.env.cr, '18.0.13.10.0')
        self.assertEqual(result, {'active_app_ids': 1})
        logger.warning.assert_not_called()
        self.assertEqual(logger.info.call_args.args[1:], (1,))
        for call in logger.mock_calls:
            self.assertNotIn('app_test', repr(call))

    def test_single_app_post_migrate_fresh_install_noop(self):
        self._plant_account('ACC2', 'other_app', 'other_secret')
        mod = load_script(POST_SINGLE_APP, 'mig_single_app_post')
        with mock.patch.object(mod, '_logger') as logger:
            self.assertIsNone(mod.migrate(self.env.cr, None))
        self.assertEqual(logger.mock_calls, [])


@tagged('post_install', '-at_install')
class TestHealthStampSeed(TransactionCase):
    """The 18.0.13.12.0 upgrade starts each health stamp from past runs."""

    DRAIN = 'meta_lead_ads.drain_last_run'
    BACKFILL = 'meta_lead_ads.backfill_last_run'
    WEBHOOK = 'meta_lead_ads.webhook_last_ok'
    OLD = 'meta_lead_ads.cron_last_run'

    def setUp(self):
        super().setUp()
        self.ICP = self.env['ir.config_parameter'].sudo()
        for key in (self.DRAIN, self.BACKFILL, self.WEBHOOK):
            self.ICP.set_param(key, False)
        self.drain = self.env.ref('meta_lead_ads.cron_meta_webhook_drain')
        self.backfill = self.env.ref('meta_lead_ads.cron_meta_backfill')
        self._lastcall(self.drain, None)
        self._lastcall(self.backfill, None)
        self.env.cr.execute("DELETE FROM meta_sync_log")
        self.Log = self.env['meta.sync.log'].sudo()
        self.now = fields.Datetime.now().replace(microsecond=0)

    def _lastcall(self, cron, value):
        self.env.flush_all()
        self.env.cr.execute(
            "UPDATE ir_cron SET lastcall = %s WHERE id = %s", (value, cron.id))
        self.env.invalidate_all()

    def _row(self, status, trigger='webhook', age=timedelta(hours=1)):
        row = self.Log._record(
            'L%s' % self.Log.search_count([]), trigger, status,
            error='x' if status == 'failed' else None)
        self.env.flush_all()
        self.env.cr.execute(
            "UPDATE meta_sync_log SET create_date = %s WHERE id = %s",
            (self.now - age, row.id))
        self.env.invalidate_all()
        return self.now - age

    def _migrate(self):
        mod = load_script(POST_OWNERSHIP, 'mig_ownership_post')
        self.env.flush_all()
        with mock.patch.object(mod, '_logger') as logger:
            counts = mod.migrate(self.env.cr, '18.0.13.11.0')
        self.env.invalidate_all()
        return counts, logger

    def _get(self, key):
        return self.ICP.get_param(key)

    def test_cron_stamps_from_lastcall(self):
        drain_at = self.now - timedelta(minutes=3)
        backfill_at = self.now - timedelta(minutes=10)
        self._lastcall(self.drain, drain_at)
        self._lastcall(self.backfill, backfill_at)
        counts, logger = self._migrate()
        self.assertEqual(self._get(self.DRAIN),
                         fields.Datetime.to_string(drain_at))
        self.assertEqual(self._get(self.BACKFILL),
                         fields.Datetime.to_string(backfill_at))
        self.assertFalse(self._get(self.WEBHOOK))
        self.assertEqual(counts['stamps'], 2)
        # Only the three counts reach the log.
        logger.info.assert_called_once()
        self.assertEqual(logger.info.call_args.args[1],
                         {'fields': 0, 'views': 0, 'stamps': 2})
        for call in logger.mock_calls:
            self.assertNotIn('x_meta_', repr(call))
            self.assertNotIn('meta_lead_ads.', repr(call))

    def test_set_stamp_kept(self):
        kept = fields.Datetime.to_string(self.now - timedelta(minutes=1))
        self.ICP.set_param(self.DRAIN, kept)
        self._lastcall(self.drain, self.now - timedelta(hours=2))
        self._lastcall(self.backfill, self.now - timedelta(minutes=5))
        counts, _logger = self._migrate()
        self.assertEqual(self._get(self.DRAIN), kept)
        self.assertEqual(counts['stamps'], 1)

    def test_no_lastcall_leaves_key_unset(self):
        """No evidence of a run leaves the key empty, so the dashboard says
        the job hasn't run yet."""
        self._lastcall(self.backfill, self.now - timedelta(minutes=5))
        counts, _logger = self._migrate()
        self.assertFalse(self._get(self.DRAIN))
        self.assertTrue(self._get(self.BACKFILL))
        self.assertEqual(counts['stamps'], 1)

    def test_webhook_stamp_from_newest_success(self):
        self._row('success', age=timedelta(hours=5))
        newest = self._row('skipped_removed', age=timedelta(hours=2))
        self._row('skipped_idempotent', age=timedelta(hours=3))
        self._row('skipped_duplicate', age=timedelta(hours=4))
        # A newer failure and a newer cron success are not webhook success.
        self._row('failed', age=timedelta(minutes=30))
        self._row('success', trigger='cron', age=timedelta(minutes=10))
        counts, _logger = self._migrate()
        self.assertEqual(self._get(self.WEBHOOK),
                         fields.Datetime.to_string(newest))
        self.assertEqual(counts['stamps'], 1)

    def test_failures_only_leave_webhook_unset(self):
        self._row('failed')
        self._row('success', trigger='cron')
        counts, _logger = self._migrate()
        self.assertFalse(self._get(self.WEBHOOK))
        self.assertEqual(counts['stamps'], 0)

    def test_set_webhook_stamp_kept(self):
        kept = fields.Datetime.to_string(self.now - timedelta(days=3))
        self.ICP.set_param(self.WEBHOOK, kept)
        self._row('success')
        self._migrate()
        self.assertEqual(self._get(self.WEBHOOK), kept)

    def test_old_heartbeat_removed(self):
        self.ICP.set_param(self.OLD, fields.Datetime.to_string(self.now))
        self._migrate()
        self.assertFalse(self._get(self.OLD))
        self.assertFalse(self.ICP.search_count([('key', '=', self.OLD)]))

    def test_second_run_writes_nothing(self):
        self._lastcall(self.drain, self.now - timedelta(minutes=3))
        self._lastcall(self.backfill, self.now - timedelta(minutes=10))
        self._row('success')
        first, _logger = self._migrate()
        self.assertEqual(first['stamps'], 3)
        keys = (self.DRAIN, self.BACKFILL, self.WEBHOOK)
        before = [self._get(key) for key in keys]
        rows = self.ICP.search_count([])
        # Newer runs since the first upgrade must not move the stamps back
        # or forward: the jobs keep their own stamps from here on.
        self._lastcall(self.drain, self.now - timedelta(minutes=1))
        second, _logger = self._migrate()
        self.assertEqual(second['stamps'], 0)
        self.assertEqual([self._get(key) for key in keys], before)
        self.assertEqual(self.ICP.search_count([]), rows)

    def test_fresh_install_noop(self):
        self._lastcall(self.drain, self.now)
        mod = load_script(POST_OWNERSHIP, 'mig_ownership_post')
        with mock.patch.object(mod, '_logger') as logger:
            self.assertIsNone(mod.migrate(self.env.cr, None))
        self.assertEqual(logger.mock_calls, [])
        self.assertFalse(self._get(self.DRAIN))


@tagged('post_install', '-at_install')
class TestWebhookEvidenceSeed(TransactionCase):
    """The 18.0.13.12.1 upgrade gives old failed rows a failure time and leaves
    the webhook stamp to the drain."""

    WEBHOOK = 'meta_lead_ads.webhook_last_ok'

    def setUp(self):
        super().setUp()
        self.ICP = self.env['ir.config_parameter'].sudo()
        self.ICP.set_param(self.WEBHOOK, False)
        self.env.cr.execute("DELETE FROM meta_sync_log")
        self.env.cr.execute("DELETE FROM meta_webhook_event")
        self.Log = self.env['meta.sync.log'].sudo()
        self.Event = self.env['meta.webhook.event'].sudo()
        self.now = fields.Datetime.now().replace(microsecond=0)

    def _row(self, leadgen_id, status, age, failed_at=None):
        """A sync row created ``age`` ago, with failed_at as an older
        release left it (empty unless given)."""
        row = self.Log._record(leadgen_id, 'webhook', status,
                               error='x' if status == 'failed' else None)
        self.env.flush_all()
        self.env.cr.execute(
            "UPDATE meta_sync_log SET create_date = %s, failed_at = %s "
            "WHERE id = %s", (self.now - age, failed_at, row.id))
        self.env.invalidate_all()
        return row

    def _event(self, leadgen_id, status, age, cleared=None):
        event = self.Event.create({'leadgen_id': leadgen_id,
                                   'page_id': 'PG_WR5', 'status': status})
        self.env.flush_all()
        self.env.cr.execute(
            "UPDATE meta_webhook_event SET write_date = %s, "
            "payload_cleared = %s WHERE id = %s",
            (self.now - age, cleared, event.id))
        self.env.invalidate_all()
        return event

    def _migrate(self, version='18.0.13.12.0'):
        mod = load_script(POST_WEBHOOK_EVIDENCE, 'mig_webhook_evidence_post')
        self.env.flush_all()
        counts = mod.migrate(self.env.cr, version)
        self.env.invalidate_all()
        return counts

    def _get(self):
        return self.ICP.get_param(self.WEBHOOK)

    def test_failed_rows_get_their_creation_time(self):
        first = self._row('LG_WR5_F1', 'failed', timedelta(days=40))
        second = self._row('LG_WR5_F2', 'failed', timedelta(days=3))
        success = self._row('LG_WR5_OK', 'success', timedelta(days=1))
        # One row per batch, so the loop has to page.
        with mock.patch.object(migration_utils, 'BATCH_SIZE', 1):
            counts = self._migrate()
        self.assertEqual(counts['rows'], 2)
        for row in first | second:
            self.assertEqual(row.failed_at, row.create_date)
        self.assertFalse(success.failed_at)

    def test_existing_failed_at_is_kept(self):
        kept = self.now - timedelta(hours=2)
        row = self._row('LG_WR5_KEEP', 'failed', timedelta(days=5),
                        failed_at=kept)
        counts = self._migrate()
        self.assertEqual(counts['rows'], 0)
        self.assertEqual(row.failed_at, kept)

    def test_done_events_do_not_seed_webhook_stamp(self):
        """A finished queue event is not a webhook success."""
        self._event('LG_WR5_D1', 'done', timedelta(days=3))
        self._event('LG_WR5_D2', 'done', timedelta(days=5))
        # An erased event is left alone too; erasing it rewrote its
        # write_date anyway.
        self._event('LG_WR5_ER', 'done', timedelta(days=1), cleared='erased')
        self._event('LG_WR5_FL', 'failed', timedelta(hours=1))
        self._event('LG_WR5_PD', 'pending', timedelta(hours=1))
        counts = self._migrate()
        self.assertFalse(self._get())
        self.assertNotIn('stamps', counts)

    def test_reconcile_finished_event_does_not_seed_stamp(self):
        """A queue event the backfill closed is not a webhook success."""
        event = self._event('LG_WR5_REC', 'failed', timedelta(hours=1))
        self._row('LG_WR5_REC', 'failed', timedelta(hours=1))
        # The backfill recovers the lead and closes what the webhook left.
        lead = self.env['crm.lead'].create({'name': 'Recovered',
                                            'type': 'lead'})
        self.Log._record('LG_WR5_REC', 'cron', 'success', lead=lead)
        self.Log._reconcile_leadgen('LG_WR5_REC', lead)
        self.env.flush_all()
        self.env.invalidate_all()
        self.assertEqual(event.status, 'done')
        self.assertFalse(self.Log.search_count([
            ('meta_leadgen_id', '=', 'LG_WR5_REC'),
            ('status', '=', 'failed')]))
        self._migrate()
        self.assertFalse(self._get())
        webhook = self.env['meta.account']._meta_cron_health()['webhook']
        self.assertEqual(webhook['status'], 'no_activity')
        self.assertFalse(webhook['last_ok'])

    def test_upgrade_without_stamp_counts_recent_webhook_failures(self):
        """With no stamp, only last week's webhook failures count."""
        recent = self._row('LG_WR5_WK', 'failed', timedelta(days=3))
        old = self._row('LG_WR5_OLD', 'failed', timedelta(days=30))
        self._event('LG_WR5_WK_EV', 'done', timedelta(days=1))
        self._migrate()
        self.assertFalse(self._get())
        for row in recent | old:
            self.assertEqual(row.failed_at, row.create_date)
        health = self.env['meta.account']._meta_cron_health()
        webhook = health['webhook']
        self.assertEqual(webhook['status'], 'failing')
        self.assertEqual(webhook['failed_count'], 1)
        self.assertFalse(webhook['last_ok'])
        self.assertEqual(
            self.env['meta.account']._health_webhook(health)['caption'],
            "1 failed in the last 7 days, no success recorded yet")

    def test_set_webhook_stamp_is_kept(self):
        kept = fields.Datetime.to_string(self.now - timedelta(days=10))
        self.ICP.set_param(self.WEBHOOK, kept)
        self._event('LG_WR5_NEW', 'done', timedelta(days=1))
        self._migrate()
        self.assertEqual(self._get(), kept)

    def test_no_done_events_leaves_stamp_unset(self):
        self._event('LG_WR5_ONLY_FL', 'failed', timedelta(hours=1))
        self._event('LG_WR5_ONLY_ER', 'done', timedelta(hours=1),
                    cleared='erased')
        counts = self._migrate()
        self.assertFalse(self._get())
        self.assertNotIn('stamps', counts)

    def test_fresh_install_does_nothing(self):
        row = self._row('LG_WR5_FRESH', 'failed', timedelta(days=2))
        self._event('LG_WR5_FRESH_EV', 'done', timedelta(days=1))
        self.assertIsNone(self._migrate(version=None))
        self.assertFalse(row.failed_at)
        self.assertFalse(self._get())

    def test_logs_counts_only(self):
        self._row('LG_WR5_LOG1', 'failed', timedelta(days=2))
        self._row('LG_WR5_LOG2', 'failed', timedelta(days=4))
        self._event('LG_WR5_LOG_EV', 'done', timedelta(days=1))
        mod = load_script(POST_WEBHOOK_EVIDENCE, 'mig_webhook_evidence_post')
        self.env.flush_all()
        with self.assertLogs(mod._logger.name, 'DEBUG') as logs:
            mod.migrate(self.env.cr, '18.0.13.12.0')
        self.env.invalidate_all()
        # The exact line: one count, no id, name or key.
        self.assertEqual(logs.output, [
            'INFO:%s:meta_lead_ads: 2 failed sync row(s) got a failure '
            'time.' % mod._logger.name])
        text = ' '.join(logs.output)
        self.assertNotIn('LG_WR5_', text)
        self.assertNotIn('PG_WR5', text)


@tagged('post_install', '-at_install')
class TestArchivedPageRepair(TransactionCase):
    """The 18.0.13.13.0 upgrade gives the old failures of archived Pages
    their Page and company, so they wait like new ones do."""

    UNKNOWN = 'Unknown page; cannot process event'

    def setUp(self):
        super().setUp()
        self.env.cr.execute("DELETE FROM meta_sync_log")
        self.env.cr.execute("DELETE FROM meta_webhook_event")
        self.Log = self.env['meta.sync.log'].sudo()
        self.Event = self.env['meta.webhook.event'].sudo()
        account = self.env['meta.account'].sudo().create({
            'name': 'Acct AR', 'account_id': 'ACC_AR1',
            'app_id': 'app_test', 'app_secret': 'secret_test',
            'access_token': 'tok_acct',
        })
        Page = self.env['meta.page'].sudo()
        self.archived = Page.create({
            'name': 'Harbor Clinic', 'page_id': 'PG_AR1',
            'access_token': 'tok_ar1', 'account_id': account.id,
            'lead_type': 'lead',
        })
        self.active_page = Page.create({
            'name': 'Bay Dental', 'page_id': 'PG_AR_ON',
            'access_token': 'tok_ar_on', 'account_id': account.id,
            'lead_type': 'lead',
        })
        self.archived.active = False
        self.company = self.archived.lead_company_id
        self.assertTrue(self.company)

    def _row(self, leadgen_id, status='failed'):
        """A webhook sync row as the old Unknown page branch left it: no
        Page and no company."""
        row = self.Log._record(leadgen_id, 'webhook', status,
                               error='x' if status == 'failed' else None)
        self.env.flush_all()
        self.env.cr.execute(
            "UPDATE meta_sync_log SET page_id = NULL, company_id = NULL, "
            "error_message = %s WHERE id = %s",
            (self.UNKNOWN if status == 'failed' else None, row.id))
        self.env.invalidate_all()
        return row

    def _event(self, leadgen_id, meta_page_id, status='failed'):
        event = self.Event.create({'leadgen_id': leadgen_id,
                                   'page_id': meta_page_id,
                                   'status': status})
        self.env.flush_all()
        self.env.cr.execute(
            "UPDATE meta_webhook_event SET page_company_id = NULL, "
            "company_id = NULL, error_message = %s WHERE id = %s",
            (self.UNKNOWN if status == 'failed' else None, event.id))
        self.env.invalidate_all()
        return event

    def _migrate(self, version='18.0.13.12.1'):
        mod = load_script(POST_ARCHIVED_PAGE, 'mig_archived_page_post')
        self.env.flush_all()
        counts = mod.migrate(self.env.cr, version)
        self.env.invalidate_all()
        return counts

    def _snapshot(self):
        self.env.flush_all()
        cr = self.env.cr
        cr.execute("SELECT id, status, page_id, company_id, error_message "
                   "FROM meta_sync_log ORDER BY id")
        rows = cr.fetchall()
        cr.execute("SELECT id, status, page_company_id, company_id, "
                   "error_message FROM meta_webhook_event ORDER BY id")
        return rows, cr.fetchall()

    def test_rows_of_an_archived_page_get_its_page_and_company(self):
        rows = self.Log.browse()
        events = self.Event.browse()
        for leadgen_id in ('LG_AR1', 'LG_AR2'):
            events |= self._event(leadgen_id, 'PG_AR1')
            rows |= self._row(leadgen_id)
        # One row per batch, so both loops have to page.
        with mock.patch.object(migration_utils, 'BATCH_SIZE', 1):
            counts = self._migrate()
        self.assertEqual(counts, {'rows': 2, 'events': 2})
        message = meta_sync_log._PAGE_ARCHIVED_MESSAGE
        for row in rows:
            self.assertEqual(row.page_id, self.archived)
            self.assertEqual(row.company_id, self.company)
            self.assertEqual(row.error_message, message)
            self.assertEqual(row.status, 'failed')
        for event in events:
            self.assertEqual(event.page_company_id, self.company)
            self.assertEqual(event.company_id, self.company)
            self.assertEqual(event.error_message, message)
            self.assertEqual(event.status, 'failed')

    def test_unknown_page_rows_are_left_alone(self):
        self._event('LG_AR_NOPE', 'PG_NOPE')
        self._row('LG_AR_NOPE')
        before = self._snapshot()
        counts = self._migrate()
        self.assertEqual(counts, {'rows': 0, 'events': 0})
        self.assertEqual(self._snapshot(), before)

    def test_rows_without_their_event_are_left_alone(self):
        """A row whose queue event was vacuumed can't be traced to a Page."""
        self._row('LG_AR_GONE')
        before = self._snapshot()
        counts = self._migrate()
        self.assertEqual(counts['rows'], 0)
        self.assertEqual(self._snapshot(), before)

    def test_rows_of_a_company_less_archived_page_are_left_alone(self):
        """A Page with no company has none to give, so its rows are
        skipped, as its events are."""
        self._event('LG_AR_NOCO', 'PG_AR1')
        self._row('LG_AR_NOCO')
        self.env.flush_all()
        self.env.cr.execute(
            "UPDATE meta_page SET lead_company_id = NULL WHERE id = %s",
            (self.archived.id,))
        self.env.invalidate_all()
        before = self._snapshot()
        counts = self._migrate()
        self.assertEqual(counts, {'rows': 0, 'events': 0})
        self.assertEqual(self._snapshot(), before)

    def test_active_page_and_other_statuses_are_left_alone(self):
        self._event('LG_AR_ON', 'PG_AR_ON')
        self._row('LG_AR_ON')
        self._event('LG_AR_OK', 'PG_AR1', status='done')
        self._row('LG_AR_OK', status='success')
        before = self._snapshot()
        counts = self._migrate()
        self.assertEqual(counts, {'rows': 0, 'events': 0})
        self.assertEqual(self._snapshot(), before)

    def test_second_run_changes_nothing(self):
        self._event('LG_AR_TWICE', 'PG_AR1')
        self._row('LG_AR_TWICE')
        self.assertEqual(self._migrate(), {'rows': 1, 'events': 1})
        before = self._snapshot()
        self.assertEqual(self._migrate(), {'rows': 0, 'events': 0})
        self.assertEqual(self._snapshot(), before)

    def test_fresh_install_does_nothing(self):
        self._event('LG_AR_FRESH', 'PG_AR1')
        self._row('LG_AR_FRESH')
        before = self._snapshot()
        self.assertIsNone(self._migrate(version=None))
        self.assertEqual(self._snapshot(), before)

    def test_logs_counts_only(self):
        self._event('LG_AR_LOG', 'PG_AR1')
        self._row('LG_AR_LOG')
        mod = load_script(POST_ARCHIVED_PAGE, 'mig_archived_page_post')
        self.env.flush_all()
        with self.assertLogs(mod._logger.name, 'DEBUG') as logs:
            mod.migrate(self.env.cr, '18.0.13.12.1')
        self.env.invalidate_all()
        # The exact line: counts only, no id, name or token.
        self.assertEqual(logs.output, [
            'INFO:%s:meta_lead_ads: 1 failed sync row(s) and 1 queue '
            'event(s) of archived Pages were given their company.'
            % mod._logger.name])
        text = ' '.join(logs.output)
        for secret in ('LG_AR', 'PG_AR', 'Harbor Clinic', 'tok_'):
            self.assertNotIn(secret, text)

    def test_message_matches_the_drain(self):
        """The script repeats the drain's message, since a migration does
        not import a models module."""
        mod = load_script(POST_ARCHIVED_PAGE, 'mig_archived_page_post')
        self.assertEqual(mod._PAGE_ARCHIVED_MESSAGE,
                         meta_sync_log._PAGE_ARCHIVED_MESSAGE)
