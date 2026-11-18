# Meta Lead Ads Integration for Odoo — © 2026 CoreSys Builders. All rights reserved.
# Licensed under the Odoo Proprietary License v1.0 (OPL-1).
# Unauthorized copying, redistribution, or resale of this software, in whole or in
# part, via any medium, is strictly prohibited and constitutes a license violation.
# OPL-1: https://www.odoo.com/documentation/20.0/legal/licenses.html#odoo-apps

"""Real transactions racing over the failure rows and queue of one Meta lead."""
import time
import uuid
from datetime import timedelta
from unittest import mock

from odoo import api, fields
from odoo.api import SUPERUSER_ID
from odoo.tests.common import BaseCase, tagged
from odoo.tools import mute_logger

from .test_ingest_integrity import CommittedFixtureMixin

_APP_PAUSE_KEY = 'meta_lead_ads.lead_fetch_paused_until'


@tagged('post_install', '-at_install')
class TestFailureRowRace(CommittedFixtureMixin, BaseCase):

    @classmethod
    def setUpClass(cls):
        super().setUpClass()
        cls._setup_committed()
        # Hex only, so the =like cleanups never meet a wildcard, and a
        # rerun or another class can't reuse or remove these ids.
        cls.token = uuid.uuid4().hex[:10]

    def _setup_page(self, ids, suffix):
        with self.registry.cursor() as cr:
            env = api.Environment(cr, SUPERUSER_ID, {})
            account = env['meta.account'].create({
                'name': 'Failure Acct %s' % suffix,
                'account_id': 'RFRACC' + self.token + suffix,
                'app_secret': 'secret_rfr', 'access_token': 'tok_rfr'})
            ids['account'] = [account.id]
            page = env['meta.page'].create({
                'name': 'Failure Page %s' % suffix,
                'page_id': 'PGRFR' + self.token + suffix,
                'access_token': 'tok_pgrfr',
                'account_id': account.id})
            ids['pages'] = [page.id]
            return page.id

    def _cleanup(self, ids):
        steps = [
            lambda env: env['meta.sync.log'].search(
                [('meta_leadgen_id', '=like',
                  'LGRFR' + self.token + '%')]).unlink(),
            lambda env: env['meta.page'].browse(
                ids.get('pages', [])).exists().unlink(),
            lambda env: env['meta.account'].browse(
                ids.get('account', [])).exists().unlink(),
        ]
        self._run_cleanup(steps)

    def _lg(self, n):
        return 'LGRFR' + self.token + n

    def _record(self, cr, leadgen_id, error, page_id=None):
        env = api.Environment(cr, SUPERUSER_ID, {})
        page = env['meta.page'].browse(page_id) if page_id else None
        return env['meta.sync.log']._record_failure(
            leadgen_id, 'webhook', error, page=page)

    def _failed(self, leadgen_id):
        with self.registry.cursor() as cr:
            cr.execute("SELECT id, error_message FROM meta_sync_log "
                       "WHERE meta_leadgen_id = %s AND status = 'failed'",
                       (leadgen_id,))
            return cr.fetchall()

    def test_concurrent_failure_writers_leave_one_row(self):
        ids = {}
        self.addCleanup(self._cleanup, ids)
        page_id = self._setup_page(ids, 'FR1')
        cr_a = self.registry.cursor()
        cr_b = self.registry.cursor()
        try:
            # Fixes cr_b's snapshot before cr_a commits its row.
            cr_b.execute("SELECT 1 FROM meta_sync_log LIMIT 1")
            self._record(cr_a, self._lg('1'), 'a', page_id)
            cr_a.commit()
            with mute_logger('odoo.sql_db'):
                res = self._record(cr_b, self._lg('1'), 'b', page_id)
            self.assertFalse(res)
            cr_b.execute("SELECT 1")
            self.assertEqual(cr_b.fetchone(), (1,))
        finally:
            cr_a.close()
            cr_b.close()
        rows = self._failed(self._lg('1'))
        self.assertEqual(len(rows), 1)
        self.assertEqual(rows[0][1], 'a')

    def test_insert_waits_are_bounded(self):
        ids = {}
        self.addCleanup(self._cleanup, ids)
        cr_a = self.registry.cursor()
        cr_b = self.registry.cursor()
        try:
            self._record(cr_a, self._lg('3'), 'a')
            cr_a.flush()
            cr_b.execute("SHOW lock_timeout")
            before = cr_b.fetchone()[0]
            started = time.monotonic()
            with mute_logger('odoo.sql_db'):
                res = self._record(cr_b, self._lg('3'), 'b')
            elapsed = time.monotonic() - started
            self.assertFalse(res)
            self.assertGreaterEqual(elapsed, 4)
            self.assertLess(elapsed, 30)
            cr_b.execute("SHOW lock_timeout")
            self.assertEqual(cr_b.fetchone()[0], before)
        finally:
            cr_a.close()
            cr_b.close()
        self.assertEqual(self._failed(self._lg('3')), [])

    def test_failure_writer_skips_busy_row(self):
        ids = {}
        self.addCleanup(self._cleanup, ids)
        with self.registry.cursor() as cr:
            row_id = self._record(cr, self._lg('2'), 'old').id
        cr_a = self.registry.cursor()
        cr_b = self.registry.cursor()
        try:
            cr_a.execute("SELECT id FROM meta_sync_log WHERE id = %s "
                         "FOR UPDATE", (row_id,))
            # Safety net: if the helper ever waited, this ends the wait.
            cr_b.execute("SET LOCAL lock_timeout = '2s'")
            started = time.monotonic()
            with mute_logger('odoo.sql_db'):
                res = self._record(cr_b, self._lg('2'), 'new')
            self.assertLess(time.monotonic() - started, 1.5)
            self.assertEqual(res.id, row_id)
            cr_b.commit()
        finally:
            cr_a.close()
            cr_b.close()
        self.assertEqual(self._failed(self._lg('2')), [(row_id, 'old')])


@tagged('post_install', '-at_install')
class TestRecoveryLocking(CommittedFixtureMixin, BaseCase):
    """Retries, the drain and reconciliation against real row locks.

    Cursor A holds a lock with a plain FOR UPDATE. Cursor B runs the code
    under test with a short lock_timeout, so a path that waits fails with a
    lock timeout instead of hanging the run. Outcomes are read from a fresh
    cursor once both are closed.
    """

    @classmethod
    def setUpClass(cls):
        super().setUpClass()
        cls._setup_committed()
        # Hex only, so the =like cleanups never meet a wildcard, and a
        # rerun or another class can't reuse or remove these ids.
        cls.token = uuid.uuid4().hex[:10]

    def _setup(self, suffix, event_status, failed_row=True, lead=False):
        ids = {'leadgen_id': 'LGRL' + self.token + suffix}
        self.addCleanup(self._cleanup, ids)
        with self.registry.cursor() as cr:
            env = api.Environment(cr, SUPERUSER_ID, {})
            account = env['meta.account'].create({
                'name': 'Locking Acct %s' % suffix,
                'account_id': 'RLKACC' + self.token + suffix,
                'app_secret': 'secret_rlk', 'access_token': 'tok_rlk'})
            ids['account'] = [account.id]
            page = env['meta.page'].create({
                'name': 'Locking Page %s' % suffix,
                'page_id': 'PGRLK' + self.token + suffix,
                'access_token': 'tok_pgrlk',
                'account_id': account.id})
            ids['pages'] = [page.id]
            event = env['meta.webhook.event'].create({
                'leadgen_id': ids['leadgen_id'], 'page_id': page.page_id,
                'form_id': 'FRLK', 'status': event_status})
            ids['event'] = event.id
            if failed_row:
                ids['row'] = env['meta.sync.log']._record_failure(
                    ids['leadgen_id'], 'webhook', 'boom', page=page).id
            if lead:
                rec = env['crm.lead'].create({
                    'name': 'Locking lead', 'type': 'lead',
                    'meta_leadgen_id': ids['leadgen_id']})
                ids['lead'] = rec.id
                self._mark_enriched(env, ids['leadgen_id'])
        return ids

    def _mark_enriched(self, env, leadgen_id):
        # Keeps the enrichment cron of a running server off these rows.
        leads = env['crm.lead'].with_context(active_test=False).search(
            [('meta_leadgen_id', '=', leadgen_id)])
        if 'iap_enrich_done' in leads._fields:
            leads.iap_enrich_done = True

    def _cleanup(self, ids):
        mine = 'LGRL' + self.token + '%'

        def leads(env):
            env['crm.lead'].with_context(active_test=False).search(
                [('meta_leadgen_id', '=like', mine)]).unlink()
            # The unlink records each id as removed. Same step, so a retry
            # of a failed unlink also clears what the retry writes.
            env['meta.leadgen.tombstone'].search(
                [('leadgen_id', '=like', mine)]).unlink()
        steps = [
            lambda env: env['meta.webhook.event'].search(
                [('leadgen_id', '=like', mine)]).unlink(),
            lambda env: env['meta.sync.log'].search(
                [('meta_leadgen_id', '=like', mine)]).unlink(),
            leads,
            lambda env: env['meta.page'].browse(
                ids.get('pages', [])).exists().unlink(),
            lambda env: env['meta.account'].browse(
                ids.get('account', [])).exists().unlink(),
        ]
        self._run_cleanup(steps)

    def _cursors(self):
        cr_a = self.registry.cursor()
        cr_b = self.registry.cursor()
        # Safety net: a path that waits on the lock held by A fails instead
        # of hanging.
        cr_b.execute("SET LOCAL lock_timeout = '3s'")
        return cr_a, cr_b

    def _graph(self, env, leadgen_id):
        payload = {
            'id': leadgen_id, 'created_time': '2026-06-14T10:00:00+0000',
            'platform': 'fb', 'field_data': [
                {'name': 'email',
                 'values': ['%s@locking.example.com' % leadgen_id.lower()]},
                {'name': 'full_name', 'values': ['Locking Test']}]}
        fetch = mock.Mock(return_value=payload)
        patch = mock.patch.multiple(
            type(env['meta.graph.client']), fetch_lead=fetch,
            resolve_name=mock.Mock(return_value=False))
        return fetch, patch

    def _row(self, row_id):
        with self.registry.cursor() as cr:
            cr.execute("SELECT status, retry_count, error_message "
                       "FROM meta_sync_log WHERE id = %s", (row_id,))
            return cr.fetchone()

    def _event(self, event_id):
        with self.registry.cursor() as cr:
            cr.execute("SELECT status, attempts, lead_id "
                       "FROM meta_webhook_event WHERE id = %s", (event_id,))
            return cr.fetchone()

    def _failed_count(self, leadgen_id):
        with self.registry.cursor() as cr:
            cr.execute("SELECT count(*) FROM meta_sync_log "
                       "WHERE meta_leadgen_id = %s AND status = 'failed'",
                       (leadgen_id,))
            return cr.fetchone()[0]

    def _retry_in(self, cr, ids):
        env = api.Environment(cr, SUPERUSER_ID, {})
        fetch, patch = self._graph(env, ids['leadgen_id'])
        started = time.monotonic()
        with patch:
            result = env['meta.sync.log'].browse(ids['row']).action_retry()
        self.assertLess(time.monotonic() - started, 2)
        self.assertFalse(fetch.called)
        self.assertIn('already being retried', result['params']['message'])

    def test_log_retry_yields_to_event_holder(self):
        ids = self._setup('1', 'failed')
        cr_a, cr_b = self._cursors()
        try:
            cr_a.execute("SELECT id FROM meta_webhook_event WHERE id = %s "
                         "FOR UPDATE", (ids['event'],))
            self._retry_in(cr_b, ids)
            # Committed, so anything B had written would show below.
            cr_b.commit()
        finally:
            cr_a.close()
            cr_b.close()
        self.assertEqual(self._row(ids['row']), ('failed', 0, 'boom'))
        self.assertEqual(self._event(ids['event'])[0], 'failed')

    def test_log_retry_yields_to_row_holder(self):
        ids = self._setup('2', 'failed')
        cr_a, cr_b = self._cursors()
        try:
            cr_a.execute("SELECT id FROM meta_sync_log WHERE id = %s "
                         "FOR UPDATE", (ids['row'],))
            self._retry_in(cr_b, ids)
            cr_b.commit()
        finally:
            cr_a.close()
            cr_b.close()
        self.assertEqual(self._row(ids['row']), ('failed', 0, 'boom'))
        self.assertEqual(self._event(ids['event'])[0], 'failed')

    def test_drain_skips_event_held_by_retry(self):
        ids = self._setup('3', 'pending', failed_row=False)
        leadgen_id = ids['leadgen_id']
        cr_a, cr_b = self._cursors()
        try:
            env_a = api.Environment(cr_a, SUPERUSER_ID, {})
            held = env_a['meta.webhook.event']._lock_leadgen_event(leadgen_id)
            self.assertEqual(held.ids, [ids['event']])
            env_b = api.Environment(cr_b, SUPERUSER_ID, {})
            fetch, patch = self._graph(env_b, leadgen_id)
            started = time.monotonic()
            with patch:
                env_b['meta.webhook.event']._cron_drain()
            self.assertLess(time.monotonic() - started, 2)
            self.assertNotIn(leadgen_id,
                             [c.args[1] for c in fetch.call_args_list])
            cr_b.commit()
        finally:
            cr_a.close()
            cr_b.close()
        self.assertEqual(self._event(ids['event']), ('pending', 0, None))
        with self.registry.cursor() as cr_c:
            env_c = api.Environment(cr_c, SUPERUSER_ID, {})
            fetch, patch = self._graph(env_c, leadgen_id)
            with patch:
                env_c['meta.webhook.event']._cron_drain()
            self._mark_enriched(env_c, leadgen_id)
        status, _attempts, lead_id = self._event(ids['event'])
        self.assertEqual(status, 'done')
        self.assertTrue(lead_id)

    def test_retry_yields_to_drain_holder(self):
        # A pending event beside a failed row left by an older run.
        ids = self._setup('4', 'pending')
        cr_a, cr_b = self._cursors()
        try:
            # The drain's own locking select, kept open.
            cr_a.execute(
                "SELECT e.id FROM meta_webhook_event e "
                "WHERE e.status = 'pending' AND e.leadgen_id = %s "
                "FOR UPDATE OF e SKIP LOCKED", (ids['leadgen_id'],))
            self.assertEqual(len(cr_a.fetchall()), 1)
            self._retry_in(cr_b, ids)
            cr_b.commit()
        finally:
            cr_a.close()
            cr_b.close()
        self.assertEqual(self._row(ids['row']), ('failed', 0, 'boom'))
        self.assertEqual(self._event(ids['event'])[0], 'pending')

    def test_event_retry_yields_to_drain_holder(self):
        ids = self._setup('6', 'failed')
        cr_a, cr_b = self._cursors()
        try:
            cr_a.execute("SELECT id FROM meta_webhook_event WHERE id = %s "
                         "FOR UPDATE", (ids['event'],))
            env_b = api.Environment(cr_b, SUPERUSER_ID, {})
            fetch, patch = self._graph(env_b, ids['leadgen_id'])
            started = time.monotonic()
            with patch:
                result = env_b['meta.webhook.event'].browse(
                    ids['event']).action_retry()
            self.assertLess(time.monotonic() - started, 2)
            self.assertFalse(fetch.called)
            self.assertIn('already being retried', result['params']['message'])
            cr_b.commit()
        finally:
            cr_a.close()
            cr_b.close()
        self.assertEqual(self._row(ids['row']), ('failed', 0, 'boom'))
        self.assertEqual(self._event(ids['event']), ('failed', 0, None))

    def test_end_wait_yields_to_page_holder(self):
        # The drain keeps the row of a Page it paused until its run ends.
        ids = self._setup('7', 'pending', failed_row=False)
        with self.registry.cursor() as cr:
            cr.execute("UPDATE meta_webhook_event SET next_attempt_at = "
                       "now() AT TIME ZONE 'UTC' + interval '1 day' "
                       "WHERE id = %s", (ids['event'],))
            cr.execute("UPDATE meta_page SET lead_fetch_paused_until = "
                       "now() AT TIME ZONE 'UTC' + interval '1 day' "
                       "WHERE id = %s", (ids['pages'][0],))
        cr_a, cr_b = self._cursors()
        try:
            cr_a.execute("SELECT id FROM meta_page WHERE id = %s FOR UPDATE",
                         (ids['pages'][0],))
            env_b = api.Environment(cr_b, SUPERUSER_ID, {})
            fetch, patch = self._graph(env_b, ids['leadgen_id'])
            started = time.monotonic()
            # A busy row is an expected answer, not a bad query.
            with patch, mock.patch.object(type(env_b['ir.cron']),
                                          '_trigger') as trigger, \
                    self.assertNoLogs('odoo.sql_db', 'ERROR'):
                result = env_b['meta.webhook.event'].browse(
                    ids['event']).action_retry()
            self.assertLess(time.monotonic() - started, 2)
            self.assertFalse(fetch.called)
            self.assertFalse(trigger.called)
            self.assertIn('already being retried', result['params']['message'])
            cr_b.commit()
        finally:
            cr_a.close()
            cr_b.close()
        with self.registry.cursor() as cr:
            cr.execute("SELECT e.next_attempt_at IS NOT NULL, "
                       "p.lead_fetch_paused_until IS NOT NULL "
                       "FROM meta_webhook_event e, meta_page p "
                       "WHERE e.id = %s AND p.id = %s",
                       (ids['event'], ids['pages'][0]))
            self.assertEqual(cr.fetchone(), (True, True))

    def test_page_pause_skips_held_page_quietly(self):
        ids = self._setup('9', 'pending', failed_row=False)
        cr_a, cr_b = self._cursors()
        try:
            cr_a.execute("SELECT id FROM meta_page WHERE id = %s FOR UPDATE",
                         (ids['pages'][0],))
            env_b = api.Environment(cr_b, SUPERUSER_ID, {})
            page = env_b['meta.page'].browse(ids['pages'][0])
            started = time.monotonic()
            with self.assertNoLogs('odoo.sql_db', 'ERROR'):
                done = page._pause_lead_fetch(
                    fields.Datetime.now() + timedelta(hours=1))
            self.assertLess(time.monotonic() - started, 2)
            self.assertFalse(done)
            cr_b.commit()
        finally:
            cr_a.close()
            cr_b.close()
        with self.registry.cursor() as cr:
            cr.execute("SELECT lead_fetch_paused_until FROM meta_page "
                       "WHERE id = %s", (ids['pages'][0],))
            self.assertEqual(cr.fetchone(), (None,))

    def _drop_app_pause(self):
        with self.registry.cursor() as cr:
            env = api.Environment(cr, SUPERUSER_ID, {})
            env['ir.config_parameter'].set_str(_APP_PAUSE_KEY, False)

    def test_end_wait_yields_to_app_pause_holder(self):
        # The drain keeps the app pause setting locked until its run ends.
        ids = self._setup('8', 'pending', failed_row=False)
        self.addCleanup(self._drop_app_pause)
        with self.registry.cursor() as cr:
            cr.execute("UPDATE meta_webhook_event SET next_attempt_at = "
                       "now() AT TIME ZONE 'UTC' + interval '1 day' "
                       "WHERE id = %s", (ids['event'],))
            env = api.Environment(cr, SUPERUSER_ID, {})
            env['ir.config_parameter'].set_str(
                _APP_PAUSE_KEY, fields.Datetime.to_string(
                    fields.Datetime.now() + timedelta(days=1)))
        cr_a, cr_b = self._cursors()
        try:
            cr_a.execute("SELECT id FROM ir_config_parameter WHERE key = %s "
                         "FOR UPDATE", (_APP_PAUSE_KEY,))
            env_b = api.Environment(cr_b, SUPERUSER_ID, {})
            fetch, patch = self._graph(env_b, ids['leadgen_id'])
            started = time.monotonic()
            with patch, mock.patch.object(type(env_b['ir.cron']),
                                          '_trigger') as trigger:
                result = env_b['meta.webhook.event'].browse(
                    ids['event']).action_retry()
            self.assertLess(time.monotonic() - started, 2)
            self.assertFalse(fetch.called)
            self.assertFalse(trigger.called)
            self.assertIn('already being retried', result['params']['message'])
            cr_b.commit()
        finally:
            cr_a.close()
            cr_b.close()
        with self.registry.cursor() as cr:
            cr.execute("SELECT next_attempt_at IS NOT NULL "
                       "FROM meta_webhook_event WHERE id = %s",
                       (ids['event'],))
            self.assertEqual(cr.fetchone(), (True,))
            cr.execute("SELECT count(*) FROM ir_config_parameter "
                       "WHERE key = %s", (_APP_PAUSE_KEY,))
            self.assertEqual(cr.fetchone(), (1,))

    def test_reconcile_skips_held_event_then_heals(self):
        ids = self._setup('5', 'failed', lead=True)
        leadgen_id = ids['leadgen_id']
        cr_a, cr_b = self._cursors()
        try:
            cr_a.execute("SELECT id FROM meta_webhook_event WHERE id = %s "
                         "FOR UPDATE", (ids['event'],))
            env_b = api.Environment(cr_b, SUPERUSER_ID, {})
            started = time.monotonic()
            done = env_b['meta.sync.log']._reconcile_leadgen(
                leadgen_id, env_b['crm.lead'].browse(ids['lead']))
            self.assertLess(time.monotonic() - started, 2)
            self.assertFalse(done)
            cr_b.commit()
        finally:
            cr_a.close()
            cr_b.close()
        self.assertEqual(self._failed_count(leadgen_id), 1)
        self.assertEqual(self._event(ids['event'])[0], 'failed')
        with self.registry.cursor() as cr_c:
            env_c = api.Environment(cr_c, SUPERUSER_ID, {})
            self.assertGreaterEqual(
                env_c['meta.sync.log']._heal_resolved_failures(), 1)
        self.assertEqual(self._failed_count(leadgen_id), 0)
        self.assertEqual(self._event(ids['event']), ('done', 0, ids['lead']))
