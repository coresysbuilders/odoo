# Meta Lead Ads Integration for Odoo — © 2026 CoreSys Builders. All rights reserved.
# Licensed under the Odoo Proprietary License v1.0 (OPL-1).
# Unauthorized copying, redistribution, or resale of this software, in whole or in
# part, via any medium, is strictly prohibited and constitutes a license violation.
# OPL-1: https://www.odoo.com/documentation/19.0/legal/licenses.html#odoo-apps

"""A lead deleted in Odoo while another transaction imports it stays deleted.

These tests commit in their own cursors, so the rows they write carry a
per-run id prefix and are removed afterwards.
"""
import uuid
from unittest import mock

from odoo import api
from odoo.api import SUPERUSER_ID
from odoo.tests.common import BaseCase, tagged
from odoo.tools import mute_logger

from odoo.addons.meta_lead_ads.models.exceptions import MetaLeadRemovedError

from .test_ingest_integrity import CommittedFixtureMixin

_DRAIN_LOGGER = 'odoo.addons.meta_lead_ads.models.meta_webhook_event'


@tagged('post_install', '-at_install')
class TestRemovedLeadRace(CommittedFixtureMixin, BaseCase):
    """The lead row and its removed record never both go missing or both
    exist after an import and a delete overlap.

    Covered: a delete committed before the import starts, a delete
    committed while an import already sees the lead, and a Restore
    committed while a drain still sees the removed record. Not covered, and
    accepted: a lead created and then deleted by other transactions while
    one import of the same id is still in flight. That import's snapshot
    sees neither the lead nor its removed record, so it creates the lead
    again. It needs a person to delete a lead seconds after its first
    import, during a second concurrent import of it, and a second check in
    the same transaction would read the same snapshot.
    """

    @classmethod
    def setUpClass(cls):
        super().setUpClass()
        cls._setup_committed()
        # Hex only, so the =like cleanup never meets a wildcard and never
        # reaches another run's rows.
        cls.prefix = 'LGRM' + uuid.uuid4().hex[:10]

    def _payload(self, leadgen_id):
        return {'id': leadgen_id, 'created_time': '2026-06-14T10:00:00+0000',
                'platform': 'fb', 'field_data': [
                    {'name': 'email',
                     'values': ['%s@removed.example.com' % leadgen_id.lower()]},
                    {'name': 'full_name', 'values': ['Removed Race']}]}

    def _graph(self, env, leadgen_id):
        Client = type(env['meta.graph.client'])
        return mock.patch.multiple(
            Client,
            fetch_lead=mock.Mock(return_value=self._payload(leadgen_id)),
            resolve_name=mock.Mock(return_value=False))

    def _ingest(self, cr, page_id, leadgen_id):
        env = api.Environment(cr, SUPERUSER_ID, {})
        with self._graph(env, leadgen_id):
            return env['meta.lead.ingest'].ingest_leadgen(
                env['meta.page'].browse(page_id), leadgen_id, 'webhook')

    def _create_lead(self, page_id, leadgen_id, ids):
        with self.registry.cursor() as cr:
            lead = self._ingest(cr, page_id, leadgen_id)
            if 'iap_enrich_done' in lead._fields:
                # Keeps the enrichment cron of a running server off this row.
                lead.iap_enrich_done = True
            ids['leads'].append(lead.id)
            return lead.id

    def _delete_lead(self, lead_id):
        with self.registry.cursor() as cr:
            env = api.Environment(cr, SUPERUSER_ID, {})
            env['crm.lead'].browse(lead_id).unlink()

    def _setup_page(self, ids, suffix):
        with self.registry.cursor() as cr:
            env = api.Environment(cr, SUPERUSER_ID, {})
            account = env['meta.account'].create({
                'name': 'Removed Race Acct %s' % suffix,
                'account_id': 'RMACC' + self.prefix + suffix,
                'app_secret': 'secret_rm', 'access_token': 'tok_rm'})
            ids['account'] = [account.id]
            page = env['meta.page'].create({
                'name': 'Removed Race Page %s' % suffix,
                'page_id': 'PGRM' + self.prefix + suffix,
                'access_token': 'tok_pgrm',
                'account_id': account.id})
            ids['pages'] = [page.id]
            return page.id

    def _cleanup(self, ids):
        mine = self.prefix + '%'

        def leads(env):
            env['crm.lead'].with_context(active_test=False).search(
                ['|', ('meta_leadgen_id', '=like', mine),
                 ('id', 'in', ids.get('leads', []))]).unlink()
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

    def _assert_removed(self, leadgen_id):
        with self.registry.cursor() as cr:
            env = api.Environment(cr, SUPERUSER_ID, {})
            self.assertFalse(env['crm.lead'].with_context(
                active_test=False).search(
                    [('meta_leadgen_id', '=', leadgen_id)]))
            self.assertEqual(env['meta.leadgen.tombstone'].search_count(
                [('leadgen_id', '=', leadgen_id)]), 1)

    def test_delete_before_import(self):
        ids = {'leads': []}
        leadgen_id = self.prefix + '1'
        try:
            page_id = self._setup_page(ids, '1')
            lead_id = self._create_lead(page_id, leadgen_id, ids)
            self._delete_lead(lead_id)
            with self.registry.cursor() as cr:
                with self.assertRaises(MetaLeadRemovedError):
                    self._ingest(cr, page_id, leadgen_id)
                cr.rollback()
            self._assert_removed(leadgen_id)
        finally:
            self._cleanup(ids)

    def test_delete_while_import_sees_lead(self):
        ids = {'leads': []}
        leadgen_id = self.prefix + '2'
        try:
            page_id = self._setup_page(ids, '2')
            lead_id = self._create_lead(page_id, leadgen_id, ids)
            cr_b = self.registry.cursor()
            try:
                env_b = api.Environment(cr_b, SUPERUSER_ID, {})
                # B's snapshot is taken here and still holds the lead.
                self.assertTrue(env_b['crm.lead'].search(
                    [('meta_leadgen_id', '=', leadgen_id)]))
                self._delete_lead(lead_id)
                committed = False
                with mute_logger('odoo.sql_db'):
                    try:
                        self._ingest(cr_b, page_id, leadgen_id)
                        env_b.flush_all()
                        cr_b.commit()
                        committed = True
                    except Exception:   # noqa: BLE001 - any refusal will do
                        cr_b.rollback()
                # B's skip row points at a lead that is gone, so it can't
                # be kept.
                self.assertFalse(committed)
            finally:
                cr_b.close()
            # Tried again in a new transaction, B sees the removal.
            with self.registry.cursor() as cr:
                with self.assertRaises(MetaLeadRemovedError):
                    self._ingest(cr, page_id, leadgen_id)
                cr.rollback()
            self._assert_removed(leadgen_id)
        finally:
            self._cleanup(ids)

    def _drain(self, cr, leadgen_id):
        # Runs the drain step on this test's event only. The full drain
        # would take every pending event in the database and import it with
        # this test's fake payload, then commit.
        env = api.Environment(cr, SUPERUSER_ID, {})
        cr.execute("SELECT id FROM meta_webhook_event "
                   "WHERE leadgen_id = %s AND status = 'pending' "
                   "FOR UPDATE", (leadgen_id,))
        events = env['meta.webhook.event'].browse(
            [row[0] for row in cr.fetchall()])
        with self._graph(env, leadgen_id):
            for event in events:
                event._process_one()
        env.flush_all()
        cr.commit()

    def test_restore_during_drain(self):
        ids = {'leads': []}
        leadgen_id = self.prefix + '3'
        try:
            page_id = self._setup_page(ids, '3')
            with self.registry.cursor() as cr:
                env = api.Environment(cr, SUPERUSER_ID, {})
                page = env['meta.page'].browse(page_id)
                env['meta.leadgen.tombstone']._remember(
                    [leadgen_id], details={leadgen_id: {
                        'page_id': page_id,
                        'company_id': page.lead_company_id.id,
                        'reason': 'deleted'}})
                env['meta.webhook.event'].create({
                    'leadgen_id': leadgen_id, 'page_id': page.page_id,
                    'form_id': 'F1', 'status': 'pending'})
            cr_d = self.registry.cursor()
            try:
                env_d = api.Environment(cr_d, SUPERUSER_ID, {})
                # The drain's snapshot is taken here and still holds the
                # removed record.
                self.assertEqual(env_d['meta.leadgen.tombstone'].search_count(
                    [('leadgen_id', '=', leadgen_id)]), 1)
                with self.registry.cursor() as cr_r:
                    env_r = api.Environment(cr_r, SUPERUSER_ID, {})
                    tomb = env_r['meta.leadgen.tombstone'].search(
                        [('leadgen_id', '=', leadgen_id)])
                    with self._graph(env_r, leadgen_id):
                        action = tomb.action_restore()
                    self.assertEqual(action['res_model'], 'crm.lead')
                drained = False
                with mute_logger('odoo.sql_db', _DRAIN_LOGGER):
                    try:
                        self._drain(cr_d, leadgen_id)
                        drained = True
                    except Exception:   # noqa: BLE001 - retried below
                        cr_d.rollback()
            finally:
                cr_d.close()
            if not drained:
                with self.registry.cursor() as cr, \
                        mute_logger(_DRAIN_LOGGER):
                    self._drain(cr, leadgen_id)
            with self.registry.cursor() as cr:
                env = api.Environment(cr, SUPERUSER_ID, {})
                self.assertEqual(env['crm.lead'].with_context(
                    active_test=False).search_count(
                        [('meta_leadgen_id', '=', leadgen_id)]), 1)
                self.assertFalse(env['meta.leadgen.tombstone'].search_count(
                    [('leadgen_id', '=', leadgen_id)]))
                # The drain's old snapshot still saw the record, so it may
                # leave a removed note for the lead. That is history only.
                self.assertEqual(env['meta.webhook.event'].search(
                    [('leadgen_id', '=', leadgen_id)]).status, 'done')
        finally:
            self._cleanup(ids)
