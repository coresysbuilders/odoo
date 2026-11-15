# Meta Lead Ads Integration for Odoo — © 2026 CoreSys Builders. All rights reserved.
# Licensed under the Odoo Proprietary License v1.0 (OPL-1).
# Unauthorized copying, redistribution, or resale of this software, in whole or in
# part, via any medium, is strictly prohibited and constitutes a license violation.
# OPL-1: https://www.odoo.com/documentation/19.0/legal/licenses.html#odoo-apps

"""Real transactions racing to put a second Meta app on the active accounts."""
import threading
import time
import uuid

from psycopg2.errors import SerializationFailure

from odoo import api
from odoo.api import SUPERUSER_ID
from odoo.exceptions import UserError
from odoo.tests.common import BaseCase, tagged
from odoo.tools import mute_logger

from .test_ingest_integrity import CommittedFixtureMixin


@tagged('post_install', '-at_install')
class TestSingleAppRace(CommittedFixtureMixin, BaseCase):
    """Two transactions connect accounts of two different apps at once.

    Odoo cursors run at REPEATABLE READ, so the transaction that waited on
    the app lock still reads the snapshot it took before it waited. It must
    fail with a serialization error, which Odoo retries on a fresh
    snapshot, instead of passing the one-app check and committing a second
    app.
    """

    @classmethod
    def setUpClass(cls):
        super().setUpClass()
        cls._setup_committed()
        # Hex only, so the =like cleanup never meets a wildcard, and a rerun
        # can't reuse or remove another run's rows.
        cls.token = uuid.uuid4().hex[:10]
        cls.app_a = 'race_a_' + cls.token
        cls.app_b = 'race_b_' + cls.token

    def setUp(self):
        super().setUp()
        self.addCleanup(self._cleanup)
        with self.registry.cursor() as cr:
            env = api.Environment(cr, SUPERUSER_ID, {})
            self.assertFalse(
                env['meta.account']._active_app_ids(),
                "needs a database without active Meta accounts")

    def _vals(self, suffix, app_id, **extra):
        vals = {'name': 'Race %s' % suffix,
                'account_id': 'RACE' + self.token + suffix,
                'app_id': app_id}
        vals.update(extra)
        return vals

    def _cleanup(self):
        self._run_cleanup([
            lambda env: env['meta.account'].with_context(
                active_test=False).search(
                    [('account_id', '=like', 'RACE' + self.token + '%')]
                ).unlink(),
        ])

    def _active_apps(self):
        with self.registry.cursor() as cr:
            env = api.Environment(cr, SUPERUSER_ID, {})
            return env['meta.account']._active_app_ids()

    def _wait_until_blocked(self, pid, outcome):
        # Polls from its own cursor until the backend waits on a lock.
        deadline = time.monotonic() + 20
        while time.monotonic() < deadline:
            with self.registry.cursor() as cr:
                cr.execute("SELECT 1 FROM pg_locks"
                           " WHERE pid = %s AND NOT granted", (pid,))
                if cr.fetchone():
                    return
            time.sleep(0.05)
        self.fail("the second transaction never waited on the app lock "
                  "(%r)" % (outcome,))

    def test_connect_that_waited_on_the_lock_fails(self):
        cr_a = self.registry.cursor()
        cr_b = self.registry.cursor()
        outcome = {}
        try:
            env_a = api.Environment(cr_a, SUPERUSER_ID, {})
            env_a['meta.account'].create(self._vals('A1', self.app_a))
            env_a.flush_all()
            cr_b.execute("SELECT pg_backend_pid()")
            pid_b = cr_b.fetchone()[0]
            # Takes cr_b's snapshot now, as a request's session and access
            # queries would before it reaches the lock.
            cr_b.execute("SELECT 1 FROM meta_account LIMIT 1")
            # Built here: the test runner holds the registry lock, so the
            # thread could not build an environment itself.
            env_b = api.Environment(cr_b, SUPERUSER_ID, {})

            def connect_b():
                try:
                    env_b['meta.account'].create(
                        self._vals('B1', self.app_b))
                    env_b.flush_all()
                    cr_b.commit()
                    outcome['committed'] = True
                except Exception as exc:   # noqa: BLE001 - checked below
                    outcome['error'] = exc

            with mute_logger('odoo.sql_db'):
                thread = threading.Thread(target=connect_b)
                thread.start()
                self._wait_until_blocked(pid_b, outcome)
                cr_a.commit()
                thread.join(30)
            self.assertFalse(thread.is_alive())
        finally:
            cr_a.close()
            cr_b.close()
        self.assertNotIn('committed', outcome)
        self.assertIsInstance(outcome.get('error'), SerializationFailure)
        self.assertEqual(self._active_apps(), [self.app_a])
        # Odoo's retry runs the request again on a fresh snapshot, which
        # sees app A and refuses app B.
        with self.registry.cursor() as cr:
            env = api.Environment(cr, SUPERUSER_ID, {})
            with self.assertRaises(UserError):
                env['meta.account'].create(self._vals('B1', self.app_b))
            cr.rollback()

    def test_unarchive_against_a_committed_connect_fails(self):
        with self.registry.cursor() as cr:
            env = api.Environment(cr, SUPERUSER_ID, {})
            env['meta.account'].create(
                self._vals('B2', self.app_b, active=False))
        cr_a = self.registry.cursor()
        cr_b = self.registry.cursor()
        try:
            env_b = api.Environment(cr_b, SUPERUSER_ID, {})
            old = env_b['meta.account'].with_context(active_test=False).search(
                [('account_id', '=', 'RACE' + self.token + 'B2')])
            self.assertTrue(old)
            env_a = api.Environment(cr_a, SUPERUSER_ID, {})
            env_a['meta.account'].create(self._vals('A2', self.app_a))
            cr_a.commit()
            with mute_logger('odoo.sql_db'), \
                    self.assertRaises(SerializationFailure):
                old.write({'active': True})
                env_b.flush_all()
        finally:
            cr_a.close()
            cr_b.close()
        self.assertEqual(self._active_apps(), [self.app_a])
