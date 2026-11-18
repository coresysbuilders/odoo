# Meta Lead Ads Integration for Odoo — © 2026 CoreSys Builders. All rights reserved.
# Licensed under the Odoo Proprietary License v1.0 (OPL-1).
# Unauthorized copying, redistribution, or resale of this software, in whole or in
# part, via any medium, is strictly prohibited and constitutes a license violation.
# OPL-1: https://www.odoo.com/documentation/20.0/legal/licenses.html#odoo-apps

"""Failed sync-log rows: one per Meta lead, and each one retryable."""
from datetime import datetime, timedelta
from unittest import mock

from psycopg2 import IntegrityError

from odoo import Command, fields
from odoo.exceptions import AccessError, UserError, ValidationError
from odoo.tests.common import TransactionCase, new_test_user, tagged
from odoo.tools import mute_logger

from odoo.addons.meta_lead_ads.models.const import RATE_LIMIT_FALLBACK_MINUTES
from odoo.addons.meta_lead_ads.models.meta_lead_form import _meta_error_summary
from odoo.addons.meta_lead_ads.models.meta_sync_log import (
    _PAGE_ARCHIVED_MESSAGE)
from odoo.addons.meta_lead_ads.models.exceptions import (
    MetaAuthError, MetaPermanentError, MetaRateLimitError, MetaTransientError,
)

from .test_ingest import IngestFixtureMixin
from .test_multi_company import MultiCompanyFixtureMixin

# Same value as MAX_ATTEMPTS in models/meta_webhook_event.py.
MAX_ATTEMPTS = 5


@tagged('post_install', '-at_install')
class TestFailureRows(MultiCompanyFixtureMixin, IngestFixtureMixin,
                      TransactionCase):

    def setUp(self):
        super().setUp()
        self.Log = self.env['meta.sync.log']

    def _queue(self, leadgen_id, page_id='PG1', status='pending'):
        return self.env['meta.webhook.event'].sudo().create({
            'leadgen_id': leadgen_id, 'page_id': page_id,
            'form_id': 'F1', 'status': status})

    def _failed_rows(self, leadgen_id):
        return self.Log.sudo().search([('meta_leadgen_id', '=', leadgen_id),
                                       ('status', '=', 'failed')])

    # ---- the failure-row helper -------------------------------------------

    def test_record_failure_reuses_open_row(self):
        other = self.Log._record('LGFR_O', 'manual', 'success')
        Log = self.Log.with_context(meta_retry_origin_log_id=other.id)
        first = Log._record_failure('LGFR_R', 'webhook', 'first')
        second = Log._record_failure('LGFR_R', 'webhook', 'second')
        self.env.invalidate_all()
        self.assertEqual(first, second)
        self.assertEqual(len(self._failed_rows('LGFR_R')), 1)
        self.assertEqual(second.error_message, 'second')
        self.assertFalse(second.retry_origin_log_id)

    def test_record_failure_refreshes_page(self):
        row = self.Log._record_failure('LGFR_S', 'webhook', 'a', page=self.page)
        self.assertEqual(row.page_id, self.page)
        self.Log._record_failure('LGFR_S', 'webhook', 'b', page=self.page_b)
        self.env.invalidate_all()
        self.assertEqual(row.page_id, self.page_b)
        self.Log._record_failure('LGFR_S', 'webhook', 'c')
        self.env.invalidate_all()
        self.assertEqual(row.page_id, self.page_b)
        self.assertEqual(row.error_message, 'c')
        self.assertEqual(len(self._failed_rows('LGFR_S')), 1)

    def test_record_failure_keeps_other_company_row(self):
        row = self.Log._record_failure('LGFR_X', 'webhook', 'from b',
                                       page=self.page_b)
        self.assertEqual(row.company_id, self.co_b)
        again = self.Log.with_user(self.admin_a)._record_failure(
            'LGFR_X', 'webhook', 'from a', page=self.page)
        self.env.invalidate_all()
        self.assertEqual(again, row)
        self.assertEqual(row.page_id, self.page_b)
        self.assertEqual(row.error_message, 'from b')
        self.assertEqual(row.company_id, self.co_b)
        # admin_ab belongs to both companies, so it may refresh the row.
        self.Log.with_user(self.admin_ab)._record_failure(
            'LGFR_X', 'webhook', 'from ab', page=self.page)
        self.env.invalidate_all()
        self.assertEqual(row.page_id, self.page)
        self.assertEqual(row.error_message, 'from ab')

    def test_record_failure_form_row(self):
        row = self.Log._record_failure(False, 'cron', 'x', form=self.form)
        self.assertEqual(row.form_id, self.form)
        self.assertEqual(row.page_id, self.page)
        self.assertFalse(row.meta_leadgen_id)
        again = self.Log._record_failure(False, 'cron', 'y', form=self.form)
        self.env.invalidate_all()
        self.assertEqual(again, row)
        self.assertEqual(row.error_message, 'y')
        self.assertEqual(self.Log.sudo().search_count([
            ('form_id', '=', self.form.id), ('status', '=', 'failed')]), 1)

    def test_second_open_failed_row_refused_by_db(self):
        self.Log._record('LGFR_D', 'webhook', 'failed', error='a')
        with mute_logger('odoo.sql_db'), self.assertRaises(IntegrityError), \
                self.env.cr.savepoint():
            self.Log._record('LGFR_D', 'webhook', 'failed', error='a')
        self.env.invalidate_all()
        ok = self.Log._record('LGFR_D', 'webhook', 'success')
        self.assertTrue(ok)
        self.assertEqual(len(self._failed_rows('LGFR_D')), 1)

    def test_init_collapses_legacy_duplicates(self):
        cr = self.env.cr
        cr.execute("DROP INDEX meta_sync_log_failed_leadgen_uniq")
        ids = []
        for _i in range(2):
            cr.execute("""
                INSERT INTO meta_sync_log
                       (meta_leadgen_id, status, create_date, write_date)
                VALUES ('LGFR_L', 'failed', now() AT TIME ZONE 'UTC',
                        now() AT TIME ZONE 'UTC')
                RETURNING id""")
            ids.append(cr.fetchone()[0])
        self.env['meta.sync.log'].init()
        cr.execute("SELECT id FROM meta_sync_log "
                   "WHERE meta_leadgen_id = 'LGFR_L' AND status = 'failed'")
        self.assertEqual([r[0] for r in cr.fetchall()], [max(ids)])
        cr.execute("SELECT 1 FROM pg_indexes "
                   "WHERE indexname = 'meta_sync_log_failed_leadgen_uniq'")
        self.assertTrue(cr.fetchone())

    # ---- form_id link check -----------------------------------------------

    def test_form_id_link_refused_for_unreadable_form(self):
        form_b = self.env['meta.lead.form'].sudo().create({
            'name': 'Form B', 'form_id': 'FFRB', 'page_id': self.page_b.id})
        Log_a = self.Log.with_user(self.admin_a)
        with self.assertRaises(ValidationError), self.env.cr.savepoint():
            # A row with no company can't be created by admin_a at all, so
            # it carries admin_a's Page.
            Log_a.create({'meta_leadgen_id': 'LGFR_F', 'status': 'failed',
                          'page_id': self.page.id, 'form_id': form_b.id})
        row = self.Log._record('LGFR_F2', 'webhook', 'failed',
                               page=self.page)
        with self.assertRaises(ValidationError), self.env.cr.savepoint():
            row.with_user(self.admin_a).write({'form_id': form_b.id})

    def test_form_id_must_match_page(self):
        page2 = self.env['meta.page'].sudo().create({
            'name': 'Page FRF2', 'page_id': 'PGFRF2', 'access_token': 'tok_f2',
            'account_id': self.account.id})
        form2 = self.env['meta.lead.form'].sudo().create({
            'name': 'Form FRF2', 'form_id': 'FFRF2', 'page_id': page2.id})
        row = self.Log._record('LGFR_M', 'webhook', 'failed', page=self.page)
        with self.assertRaises(ValidationError), self.env.cr.savepoint():
            row.with_user(self.admin_a).write({'form_id': form2.id})
        self.env.invalidate_all()
        row.with_user(self.admin_a).write({'form_id': self.form.id})
        self.assertEqual(row.form_id, self.form)

    # ---- rows whose Page was unknown when they failed ---------------------

    def _unknown_page_row(self, leadgen_id):
        row = self.Log._record_failure(
            leadgen_id, 'webhook', 'Unknown page; cannot process event')
        self._queue(leadgen_id, page_id='PGFRNEW', status='failed')
        self.assertFalse(row.company_id)
        return row

    def _connect_new_page(self, company=None):
        vals = {'name': 'Page FRNEW', 'page_id': 'PGFRNEW',
                'access_token': 'tok_frnew', 'account_id': self.account.id}
        if company:
            vals['company_id'] = company.id
        return self.env['meta.page'].create(vals)

    def test_unknown_page_row_refused_until_page_connected(self):
        row = self._unknown_page_row('LGFR_U')
        with self._patch_graph(fetch_lead=self._fake_lead(id='LGFR_U')), \
                self.assertRaises(UserError) as ctx:
            row.with_user(self.admin_a).action_retry()
        self.assertIs(type(ctx.exception), UserError)
        self.assertNotIn('PGFRNEW', str(ctx.exception))
        self.env.invalidate_all()
        self.assertEqual(row.retry_count, 0)
        self.assertEqual(row.status, 'failed')

    def test_unknown_page_row_retried_after_page_connected(self):
        row = self._unknown_page_row('LGFR_U')
        new_page = self._connect_new_page()
        with self._patch_graph(fetch_lead=self._fake_lead(id='LGFR_U')):
            row.with_user(self.admin_a).action_retry()
        self.env.invalidate_all()
        self.assertEqual(row.status, 'success')
        self.assertEqual(row.page_id, new_page)
        self.assertEqual(row.company_id, self.co_a)
        self.assertEqual(row.lead_id.meta_leadgen_id, 'LGFR_U')
        self.assertEqual(self.Log.sudo().search_count(
            [('meta_leadgen_id', '=', 'LGFR_U')]), 1)

    def test_unknown_page_row_refused_for_other_company(self):
        row = self._unknown_page_row('LGFR_U')
        self._connect_new_page(company=self.co_b)
        with self._patch_graph(fetch_lead=self._fake_lead(id='LGFR_U')), \
                self.assertRaises(UserError) as ctx, self.env.cr.savepoint():
            row.with_user(self.admin_a).action_retry()
        self.assertIs(type(ctx.exception), UserError)
        self.assertNotIn('PGFRNEW', str(ctx.exception))
        self.env.invalidate_all()
        self.assertFalse(row.company_id)
        with self._patch_graph(fetch_lead=self._fake_lead(id='LGFR_U')):
            row.with_user(self.admin_b).action_retry()
        self.env.invalidate_all()
        self.assertEqual(row.status, 'success')
        self.assertEqual(row.company_id, self.co_b)
        self.assertEqual(row.lead_id.meta_leadgen_id, 'LGFR_U')

    def test_log_retry_requires_meta_admin(self):
        meta_user = new_test_user(
            self.env, 'fr_meta_user',
            groups='base.group_user,meta_lead_ads.group_meta_user',
            company_id=self.co_a.id, company_ids=[Command.set([self.co_a.id])])
        owned = self.Log._record_failure('LGFR_G1', 'webhook', 'x',
                                         page=self.page)
        shared = self._unknown_page_row('LGFR_G2')
        self._connect_new_page()
        with mock.patch.object(type(self.Log), '_resolve_page',
                               autospec=True) as resolve, \
                self._patch_graph(fetch_lead=self._fake_lead(id='LGFR_G1')):
            for user in (self.sales_mgr_a, self.sales_a, meta_user):
                for row in (owned, shared):
                    with self.subTest(user=user.login, row=row.id), \
                            self.assertRaises(AccessError):
                        row.with_user(user).action_retry()
            # Refused before the company-less pre-step writes anything.
            self.assertFalse(resolve.called)
            self.assertFalse(self._fetch_lead.called)
        self.env.invalidate_all()
        self.assertFalse(shared.page_id)
        self.assertEqual(owned.retry_count, 0)

    def test_unknown_page_row_busy_is_not_written(self):
        row = self._unknown_page_row('LGFR_UB')
        self._connect_new_page()
        with mock.patch.object(type(self.env['meta.webhook.event']),
                               '_lock_leadgen_event', return_value=None), \
                self._patch_graph(fetch_lead=self._fake_lead(id='LGFR_UB')):
            result = row.with_user(self.admin_a).action_retry()
        self.env.invalidate_all()
        self.assertFalse(self._fetch_lead.called)
        self.assertFalse(row.page_id)
        self.assertFalse(row.company_id)
        self.assertIn('already being retried', result['params']['message'])

    def test_log_retry_unexpected_error_not_logged(self):
        row = self.Log._record_failure('LGFR_LK', 'webhook', 'x',
                                       page=self.page)
        with mock.patch.object(type(self.Ingest), '_map_fields',
                               side_effect=ValueError('leaky detail')), \
                self.assertLogs('odoo.addons.meta_lead_ads.models.'
                                'meta_sync_log') as logs, \
                self._patch_graph(fetch_lead=self._fake_lead(id='LGFR_LK')):
            row.with_user(self.admin_a).action_retry()
        self.env.invalidate_all()
        self.assertEqual(row.error_message, 'Unexpected retry error')
        for record in logs.records:
            self.assertNotIn('leaky detail', record.getMessage())
            self.assertFalse(record.exc_info)

    def test_page_hidden_from_crm_managers(self):
        # CRM managers read sync logs but not Pages, so the Page link on a
        # row is not theirs to read either.
        row = self.Log._record_failure('LGFR_PH', 'webhook', 'x',
                                       page=self.page)
        as_mgr = row.with_user(self.sales_mgr_a)
        self.assertEqual(as_mgr.read(['meta_leadgen_id'])[0]['meta_leadgen_id'],
                         'LGFR_PH')
        with self.assertRaises(AccessError):
            as_mgr.read(['page_id'])
        self.assertEqual(row.with_user(self.admin_a).read(['page_id'])[0]
                         ['page_id'][0], self.page.id)

    def test_log_retry_unexpected_error_busy_row_untouched(self):
        # The savepoint rollback released the row lock; if someone else
        # took it meanwhile, the error is not written over their work.
        row = self.Log._record_failure('LGFR_UB', 'webhook', 'x',
                                       page=self.page)
        LogModel = type(self.Log)
        with mock.patch.object(LogModel, '_retry_one',
                               side_effect=ValueError('boom')), \
                mock.patch.object(LogModel, '_lock_for_retry',
                                  return_value=False), \
                mute_logger('odoo.addons.meta_lead_ads.models.meta_sync_log'):
            result = row.with_user(self.admin_a).action_retry()
        self.env.invalidate_all()
        self.assertEqual(row.error_message, 'x')
        self.assertEqual(row.retry_count, 0)
        self.assertIn('already being retried', result['params']['message'])
        self.assertIn('1 still failed', result['params']['message'])

    def test_resolve_page_prefers_row_page_over_form(self):
        both = self.Log._record('LGFR_RP', 'cron', 'failed',
                                page=self.page_b, form=self.form)
        self.assertEqual(both._resolve_page(), self.page_b)
        form_only = self.Log.sudo().create({
            'status': 'failed', 'form_id': self.form.id})
        self.assertEqual(form_only._resolve_page(), self.form.page_id)

    # ---- who writes the failure row ---------------------------------------

    def _drain(self, fetch_lead):
        with self._patch_graph(fetch_lead=fetch_lead):
            self.env['meta.webhook.event']._cron_drain()
        self.env.invalidate_all()

    def _raising(self, exc):
        return mock.Mock(side_effect=exc)

    def _assert_one_row_on_page(self, leadgen_id):
        rows = self._failed_rows(leadgen_id)
        self.assertEqual(len(rows), 1)
        self.assertEqual(rows.page_id, self.page)
        self.assertEqual(rows.meta_leadgen_id, leadgen_id)
        return rows

    def _retry_to_lead(self, row, leadgen_id):
        with self._patch_graph(fetch_lead=self._fake_lead(id=leadgen_id)):
            row.with_user(self.admin_a).action_retry()
        self.env.invalidate_all()
        self.assertEqual(row.status, 'success')
        self.assertEqual(row.lead_id.meta_leadgen_id, leadgen_id)
        self.assertEqual(len(self._failed_rows(leadgen_id)), 0)

    def test_ingest_fetch_failure_writes_no_row(self):
        with self._patch_graph(
                fetch_lead=self._raising(MetaPermanentError('gone'))), \
                self.assertRaises(MetaPermanentError):
            self.Ingest.ingest_leadgen(self.page, 'LGFR_I', trigger='manual')
        self.assertEqual(len(self._failed_rows('LGFR_I')), 0)

    def test_drain_permanent_one_row_then_retry(self):
        event = self._queue('LGFR_P')
        self._drain(self._raising(MetaPermanentError('gone')))
        self.assertEqual(event.status, 'failed')
        row = self._assert_one_row_on_page('LGFR_P')
        self.assertIn('gone', row.error_message)
        self._retry_to_lead(row, 'LGFR_P')

    def test_drain_auth_one_row_then_retry(self):
        event = self._queue('LGFR_A')
        self._drain(self._raising(MetaAuthError('token expired')))
        self.assertEqual(event.status, 'failed')
        row = self._assert_one_row_on_page('LGFR_A')
        self.assertIn('token expired', row.error_message)
        self._retry_to_lead(row, 'LGFR_A')

    def test_drain_exhausted_transient_one_row(self):
        event = self._queue('LGFR_T')
        for _i in range(MAX_ATTEMPTS - 1):
            self._drain(self._raising(MetaTransientError('slow')))
        self.assertEqual(len(self._failed_rows('LGFR_T')), 0)
        self.assertEqual(event.status, 'pending')
        self.assertEqual(event.attempts, MAX_ATTEMPTS - 1)
        self._drain(self._raising(MetaTransientError('slow')))
        self.assertEqual(event.status, 'failed')
        row = self._assert_one_row_on_page('LGFR_T')
        self._retry_to_lead(row, 'LGFR_T')

    def test_drain_unexpected_one_row(self):
        event = self._queue('LGFR_X')
        with mock.patch.object(type(self.Ingest), '_map_fields',
                               side_effect=ValueError('boom')), \
                mute_logger('odoo.addons.meta_lead_ads.models.'
                            'meta_webhook_event'):
            self._drain(self._fake_lead(id='LGFR_X'))
        self.assertEqual(event.status, 'failed')
        row = self._assert_one_row_on_page('LGFR_X')
        self.assertEqual(row.error_message, 'Unexpected drain error')
        self._retry_to_lead(row, 'LGFR_X')

    def test_drain_unknown_page_one_row(self):
        event = self._queue('LGFR_N', page_id='PGFRNOPE')
        self._drain(self._fake_lead(id='LGFR_N'))
        self.assertEqual(event.status, 'failed')
        rows = self._failed_rows('LGFR_N')
        self.assertEqual(len(rows), 1)
        self.assertFalse(rows.page_id)
        self.assertFalse(rows.company_id)
        self.assertNotIn('PGFRNOPE', rows.error_message or '')

    def test_failed_manual_retry_adds_no_row(self):
        self._queue('LGFR_P')
        self._drain(self._raising(MetaPermanentError('gone')))
        row = self._assert_one_row_on_page('LGFR_P')
        with self._patch_graph(
                fetch_lead=self._raising(MetaPermanentError('still gone'))):
            row.with_user(self.admin_a).action_retry()
        self.env.invalidate_all()
        rows = self._failed_rows('LGFR_P')
        self.assertEqual(rows, row)
        self.assertEqual(row.retry_count, 1)
        self.assertIn('still gone', row.error_message)

    def test_form_id_hidden_from_crm_manager(self):
        row = self.Log._record_failure('LGFR_H', 'cron', 'x', form=self.form)
        self.env.invalidate_all()
        as_mgr = row.with_user(self.sales_mgr_a)
        with self.assertRaises(AccessError):
            as_mgr.read(['form_id'])
        self.assertEqual(as_mgr.read(['status'])[0]['status'], 'failed')


@tagged('post_install', '-at_install')
class TestBackfillFailureRows(MultiCompanyFixtureMixin, IngestFixtureMixin,
                              TransactionCase):
    """Backfill failures leave a row that knows its lead, Page and form."""

    def setUp(self):
        super().setUp()
        self.Log = self.env['meta.sync.log']
        self.Form = self.env['meta.lead.form']

    def _lead_item(self, leadgen_id, created_time):
        return {'id': leadgen_id, 'created_time': created_time}

    def _payload(self, leadgen_id):
        # Distinct contact per lead, so business dedup never links them.
        return self._fake_lead(id=leadgen_id, field_data=[
            {'name': 'email', 'values': ['%s@bf.test' % leadgen_id.lower()]},
            {'name': 'full_name', 'values': [leadgen_id]},
        ])

    def _listing(self, items=(), truncated=False, raises=None):
        def _paged(token, path, params=None, app_secret=None, status=None, **kw):
            if raises is not None:
                raise raises
            if truncated and status is not None:
                status['truncated'] = True
            return iter([dict(i) for i in items])
        return mock.patch.object(self.Client, '_iter_paged',
                                 side_effect=_paged)

    def _fetch(self, failing=None, exc=None):
        failing = failing or ()

        def _side(page, leadgen_id):
            if leadgen_id in failing:
                raise exc
            return self._payload(leadgen_id)
        return mock.Mock(side_effect=_side)

    def _sweep(self, items=(), fetch=None, **listing):
        with self._listing(items, **listing), \
                self._patch_graph(fetch_lead=fetch or self._fetch()):
            self.Form._cron_backfill()
        self.env.invalidate_all()

    def _cron_failed(self):
        return self.Log.sudo().search([('status', '=', 'failed'),
                                       ('trigger', '=', 'cron')])

    def _form_rows(self, form=None):
        return self.Log.sudo().search([
            ('status', '=', 'failed'), ('meta_leadgen_id', '=', False),
            ('form_id', '=', (form or self.form).id)])

    def _assert_lead_row(self, leadgen_id):
        rows = self._cron_failed()
        self.assertEqual(len(rows), 1)
        self.assertEqual(rows.meta_leadgen_id, leadgen_id)
        self.assertEqual(rows.page_id, self.page)
        self.assertEqual(rows.form_id, self.form)
        return rows

    # ---- per-lead failures --------------------------------------------------

    def test_backfill_permanent_row_has_lead_id(self):
        items = [self._lead_item('LGBF_1', '2026-06-01T08:00:00+0000'),
                 self._lead_item('LGBF_2', '2026-06-01T09:00:00+0000')]
        self._sweep(items, fetch=self._fetch(
            ('LGBF_2',), MetaPermanentError('lead gone')))
        row = self._assert_lead_row('LGBF_2')
        self.assertEqual(row.error_message, 'Meta refused to return this lead')
        self.assertNotIn('lead gone', row.error_message)
        with self._patch_graph(fetch_lead=self._payload('LGBF_2')):
            row.with_user(self.admin_a).action_retry()
        self.env.invalidate_all()
        self.assertEqual(row.status, 'success')
        self.assertEqual(row.lead_id.meta_leadgen_id, 'LGBF_2')

    def test_backfill_auth_row_has_lead_id(self):
        items = [self._lead_item('LGBF_A', '2026-06-01T09:00:00+0000')]
        self._sweep(items, fetch=self._fetch(
            ('LGBF_A',), MetaAuthError('token expired')))
        row = self._assert_lead_row('LGBF_A')
        self.assertEqual(
            row.error_message,
            _meta_error_summary(MetaAuthError('token expired'), 'lead'))
        self.assertNotIn('token expired', row.error_message)

    def test_backfill_unexpected_row_has_lead_id(self):
        Ingest = type(self.Ingest)
        original = Ingest._map_fields

        def _map(ingest, page, data, company=None):
            if data.get('id') == 'LGBF_U':
                raise ValueError('boom')
            return original(ingest, page, data, company=company)

        items = [self._lead_item('LGBF_OK', '2026-06-01T08:00:00+0000'),
                 self._lead_item('LGBF_U', '2026-06-01T09:00:00+0000')]
        with mock.patch.object(Ingest, '_map_fields', autospec=True,
                               side_effect=_map), \
                mute_logger('odoo.addons.meta_lead_ads.models.meta_lead_form'):
            self._sweep(items)
        row = self._assert_lead_row('LGBF_U')
        self.assertEqual(row.error_message, 'Unexpected backfill error')

    def test_backfill_malformed_time_lead_ingested(self):
        self._sweep([self._lead_item('LGBF_M', 'nope')])
        self.assertEqual(len(self._cron_failed()), 0)
        self.assertTrue(self.env['crm.lead'].with_context(
            active_test=False).search([('meta_leadgen_id', '=', 'LGBF_M')]))

    # ---- interruptions that are not failures ---------------------------------

    def test_backfill_transient_writes_no_row(self):
        items = [self._lead_item('LGBF_T1', '2026-06-01T08:00:00+0000'),
                 self._lead_item('LGBF_T2', '2026-06-01T09:00:00+0000')]
        self._sweep(items, fetch=self._fetch(
            ('LGBF_T2',), MetaTransientError('slow')))
        self.assertEqual(len(self._cron_failed()), 0)
        self.assertEqual(fields.Datetime.to_string(self.form.last_synced_time),
                         '2026-06-01 08:00:00')

    def test_backfill_listing_ratelimit_writes_no_row(self):
        self._sweep(raises=MetaRateLimitError('slow down'))
        self.assertEqual(len(self._cron_failed()), 0)

    # ---- failures of the listing itself ---------------------------------------

    def _listing_failure(self):
        self._sweep(raises=MetaPermanentError('listing gone'))
        rows = self._cron_failed()
        self.assertEqual(len(rows), 1)
        self.assertEqual(rows.form_id, self.form)
        self.assertEqual(rows.page_id, self.page)
        self.assertFalse(rows.meta_leadgen_id)
        return rows

    def test_backfill_listing_permanent_row_has_form(self):
        row = self._listing_failure()
        self.assertEqual(row.error_message,
                         'Meta refused the lead listing for this form')
        self.assertNotIn('listing gone', row.error_message)
        self._listing_failure()

    def test_clean_sweep_clears_form_row(self):
        self._listing_failure()
        self._sweep([])
        self.assertEqual(len(self._form_rows()), 0)

    def test_truncated_listing_keeps_form_row(self):
        row = self._listing_failure()
        self._sweep([], truncated=True)
        self.assertEqual(self._form_rows(), row)

    def test_idless_item_keeps_form_row(self):
        row = self._listing_failure()
        self._sweep([{'created_time': '2026-06-01T08:00:00+0000'}])
        self.assertEqual(self._form_rows(), row)

    def test_clean_sweep_keeps_legacy_page_row(self):
        legacy = self.Log._record(False, 'cron', 'failed', error='old',
                                  page=self.page)
        self.assertEqual(self.Form.search([('page_id', '=', self.page.id)]),
                         self.form)
        self._sweep([])
        self.assertTrue(legacy.exists())
        self.assertEqual(legacy.status, 'failed')

    # ---- retrying rows that concern a whole form or Page --------------------

    def _patch_trigger(self):
        return mock.patch.object(type(self.env['ir.cron']), '_trigger')

    def _retry_as_admin(self, rows):
        Ingest = type(self.Ingest)
        with self._patch_trigger() as trigger, \
                mock.patch.object(self.Client, '_iter_paged') as paged, \
                mock.patch.object(Ingest, 'ingest_leadgen') as ingest:
            result = rows.with_user(self.admin_a).action_retry()
        self.assertFalse(paged.called)
        self.assertFalse(ingest.called)
        self.env.invalidate_all()
        return result, trigger

    def _form_row(self, form=None):
        return self.Log._record_failure(False, 'cron', 'listing gone',
                                        form=form or self.form)

    def test_form_row_retry_triggers_backfill(self):
        row = self._form_row()
        result, trigger = self._retry_as_admin(row)
        self.assertEqual(trigger.call_count, 1)
        self.assertEqual(row.status, 'failed')
        self.assertEqual(row.retry_count, 1)
        self.assertEqual(
            row.error_message,
            'Backfill scheduled; this row clears when the form syncs cleanly')
        self.assertIn('1 scheduled to run shortly',
                      result['params']['message'])

    def test_multi_row_retry_triggers_once(self):
        forms = self.form | self.Form.sudo().create([
            {'name': 'Form R2', 'form_id': 'FBFR2', 'page_id': self.page.id},
            {'name': 'Form R3', 'form_id': 'FBFR3', 'page_id': self.page.id},
        ])
        rows = self.Log.browse()
        for form in forms:
            rows |= self._form_row(form)
        self.assertEqual(len(rows), 3)
        result, trigger = self._retry_as_admin(rows)
        self.assertEqual(trigger.call_count, 1)
        self.assertIn('3 scheduled to run shortly',
                      result['params']['message'])

    def _backfill_triggers(self):
        return self.env['ir.cron.trigger'].sudo().search([
            ('cron_id', '=',
             self.env.ref('meta_lead_ads.cron_meta_backfill').id)])

    def test_repeated_clicks_queue_one_backfill(self):
        # A sweep already due covers the row; each extra one spends Meta
        # quota every form shares.
        self._backfill_triggers().unlink()
        row = self._form_row()
        for _i in range(3):
            with mock.patch.object(self.Client, '_iter_paged') as paged:
                result = row.with_user(self.admin_a).action_retry()
            self.assertFalse(paged.called)
            self.assertIn('1 scheduled to run shortly',
                          result['params']['message'])
        self.assertEqual(len(self._backfill_triggers()), 1)
        self.env.invalidate_all()
        self.assertEqual(row.retry_count, 3)

    def test_form_row_held_while_page_paused(self):
        row = self._form_row()
        self.page.sudo().write(
            {'lead_fetch_paused_until': T0 + timedelta(hours=1)})
        before = row.error_message
        with mock.patch.object(fields.Datetime, 'now', return_value=T0):
            result, trigger = self._retry_as_admin(row)
        self.assertFalse(trigger.called)
        self.assertEqual(row.retry_count, 0)
        self.assertEqual(row.error_message, before)
        self.assertIn('held back', result['params']['message'])

    def _assert_refused(self, row, message):
        result, trigger = self._retry_as_admin(row)
        self.assertFalse(trigger.called)
        self.assertEqual(row.status, 'failed')
        self.assertEqual(row.error_message, message)
        self.assertIn('1 still failed', result['params']['message'])
        self.assertNotIn('scheduled', result['params']['message'])

    def test_form_row_retry_refused_when_sync_off(self):
        row = self._form_row()
        self.form.sync_enabled = False
        self._assert_refused(
            row, 'Backfill is off for this form; turn sync on to retry it')
        self.form.write({'sync_enabled': True, 'active': False})
        self._assert_refused(
            row, 'Backfill is off for this form; turn sync on to retry it')

    def test_form_row_retry_refused_when_job_inactive(self):
        row = self._form_row()
        self.env.ref('meta_lead_ads.cron_meta_backfill').sudo().active = False
        self._assert_refused(
            row, 'The backfill job is turned off; turn it on in Scheduled '
                 'Actions')

    def _legacy_row(self):
        return self.Log._record(False, 'cron', 'failed', error='old',
                                page=self.page)

    def test_legacy_page_row_retry_triggers_backfill(self):
        row = self._legacy_row()
        result, trigger = self._retry_as_admin(row)
        self.assertEqual(trigger.call_count, 1)
        self.assertEqual(row.status, 'failed')
        self.assertEqual(
            row.error_message,
            'Backfill scheduled; this older row has no Meta lead id, so '
            'delete it once the Page leads are checked')
        self.assertIn('1 scheduled to run shortly',
                      result['params']['message'])

    def test_legacy_page_row_refused_without_runnable_form(self):
        row = self._legacy_row()
        self.Form.sudo().search([('page_id', '=', self.page.id)]).write(
            {'sync_enabled': False})
        self._assert_refused(row, 'This Page has no form with sync turned on')

    def test_row_without_page_or_lead_id_refused_cleanly(self):
        row = self.Log.sudo().create({'status': 'failed', 'trigger': 'cron'})
        self.assertFalse(row.company_id)
        with self._patch_trigger() as trigger, \
                self.assertRaises(UserError) as ctx:
            row.with_user(self.admin_a).action_retry()
        self.assertIs(type(ctx.exception), UserError)
        self.assertFalse(trigger.called)

    def test_sync_log_views_show_page(self):
        views = self.Log.with_user(self.admin_a).get_views(
            [(False, 'list'), (False, 'form')])['views']
        for view_type in ('list', 'form'):
            self.assertIn('name="page_id"', views[view_type]['arch'])
            self.assertIn('name="form_id"', views[view_type]['arch'])
        as_mgr = self.Log.with_user(self.sales_mgr_a).get_views(
            [(False, 'list')])['views']
        self.assertNotIn('name="page_id"', as_mgr['list']['arch'])


_EVENT_LOGGER = 'odoo.addons.meta_lead_ads.models.meta_webhook_event'
T0 = datetime(2026, 10, 16, 12, 0, 0)


@tagged('post_install', '-at_install')
class TestRateLimitBackoff(MultiCompanyFixtureMixin, IngestFixtureMixin,
                           TransactionCase):
    """A throttled webhook event waits for Meta's estimate, spending nothing."""

    def setUp(self):
        super().setUp()
        self.Event = self.env['meta.webhook.event'].sudo()
        self.Log = self.env['meta.sync.log'].sudo()
        self.page2 = self.env['meta.page'].create({
            'name': 'Page RL2', 'page_id': 'PGRL2', 'access_token': 'tok_rl2',
            'account_id': self.account.id})

    def _queue(self, leadgen_id, page_id='PG1'):
        return self.Event.create({'leadgen_id': leadgen_id,
                                  'page_id': page_id, 'form_id': 'F1'})

    def _drain(self, fetch_lead, at=T0):
        with self._patch_graph(fetch_lead=fetch_lead), \
                mock.patch.object(fields.Datetime, 'now', return_value=at):
            self.env['meta.webhook.event']._cron_drain()
        self.env.invalidate_all()

    def _throttle(self, hint, code=80005):
        return mock.Mock(side_effect=MetaRateLimitError(
            'slow down', code=code, retry_after_min=hint))

    def _deferred_until(self, leadgen_id, hint):
        # Each call checks one hint on a fresh event, so drop the Page pause
        # left by the previous one.
        self.page._clear_lead_fetch_pause()
        event = self._queue(leadgen_id)
        self._drain(self._throttle(hint))
        self.assertEqual(event.status, 'pending')
        self.assertEqual(event.attempts, 0)
        return event.next_attempt_at

    def _failed_rows(self, leadgen_id):
        return self.Log.search([('meta_leadgen_id', '=', leadgen_id),
                                ('status', '=', 'failed')])

    def test_hint_defers_event(self):
        event = self._queue('LGRL_H')
        self._drain(self._throttle(19))
        self.assertEqual(event.status, 'pending')
        self.assertEqual(event.attempts, 0)
        self.assertEqual(event.next_attempt_at, T0 + timedelta(minutes=19))
        self.assertTrue(event.error_message)
        self.assertNotIn('PG1', event.error_message)
        self.assertFalse(self._failed_rows('LGRL_H'))

    def test_not_fetched_before_hint(self):
        event = self._queue('LGRL_W')
        self._drain(self._throttle(19))
        early = mock.Mock(return_value=self._fake_lead(id='LGRL_W'))
        self._drain(early, at=T0 + timedelta(minutes=18))
        self.assertFalse(early.called)
        self.assertEqual(event.status, 'pending')
        self._drain(self._fake_lead(id='LGRL_W'),
                    at=T0 + timedelta(minutes=20))
        self.assertEqual(event.status, 'done')
        self.assertEqual(event.lead_id.meta_leadgen_id, 'LGRL_W')
        self.assertFalse(event.next_attempt_at)

    def test_no_hint_uses_fallback(self):
        expected = T0 + timedelta(minutes=RATE_LIMIT_FALLBACK_MINUTES)
        for i, hint in enumerate((None, 0, -3, True, 'x', float('nan'),
                                  float('-inf'))):
            with self.subTest(hint=hint):
                self.assertEqual(
                    self._deferred_until('LGRL_F%d' % i, hint), expected)

    def test_fractional_hint_rounds_up(self):
        self.assertEqual(self._deferred_until('LGRL_R1', 0.2),
                         T0 + timedelta(minutes=1))
        self.assertEqual(self._deferred_until('LGRL_R2', 19.5),
                         T0 + timedelta(minutes=20))

    def test_huge_hint_is_capped(self):
        # A day, the same as RATE_LIMIT_MAX_MINUTES.
        cap = T0 + timedelta(minutes=24 * 60)
        with self.assertLogs(_EVENT_LOGGER, 'WARNING'):
            self.assertEqual(self._deferred_until('LGRL_B', 10 ** 6), cap)
        # 10 ** 400 is too big for a float.
        for i, hint in enumerate((float('inf'), 10 ** 12, 10 ** 400)):
            with self.subTest(i=i), mute_logger(_EVENT_LOGGER):
                self.assertEqual(
                    self._deferred_until('LGRL_B%d' % i, hint), cap)
        self.assertEqual(self._deferred_until('LGRL_BU', 24 * 60 - 1),
                         cap - timedelta(minutes=1))

    def test_ratelimit_does_not_spend_attempts(self):
        event = self._queue('LGRL_S')
        at = T0
        for _i in range(MAX_ATTEMPTS + 1):
            self._drain(self._throttle(19), at=at)
            self.assertEqual(event.next_attempt_at,
                             at + timedelta(minutes=19))
            at = event.next_attempt_at + timedelta(minutes=1)
        self.assertEqual(event.status, 'pending')
        self.assertEqual(event.attempts, 0)
        self.assertFalse(self._failed_rows('LGRL_S'))

    def test_app_throttle_ends_run(self):
        a = self._queue('LGRL_A')
        c = self._queue('LGRL_C', page_id='PGRL2')
        # Both rows share the transaction's create_date; put c after a.
        self.env.cr.execute(
            "UPDATE meta_webhook_event SET create_date = create_date "
            "+ interval '1 second' WHERE id = %s", (c.id,))
        self.env.invalidate_all()
        fetch = self._throttle(10, code=4)
        self._drain(fetch)
        self.assertEqual(fetch.call_count, 1)
        self.assertEqual(a.next_attempt_at, T0 + timedelta(minutes=10))
        self.assertEqual(c.status, 'pending')
        self.assertFalse(c.next_attempt_at)

    def test_transient_still_counts(self):
        event = self._queue('LGRL_T')
        self._drain(mock.Mock(side_effect=MetaTransientError('slow')))
        self.assertEqual(event.status, 'pending')
        self.assertEqual(event.attempts, 1)
        self.assertFalse(event.next_attempt_at)

    def test_event_views_show_next_attempt(self):
        views = self.env['meta.webhook.event'].get_views(
            [(False, 'list'), (False, 'form')])['views']
        for view_type in ('list', 'form'):
            self.assertIn('name="next_attempt_at"', views[view_type]['arch'])

    # ---- Page and app pauses -----------------------------------------------

    _APP_PAUSE_KEY = 'meta_lead_ads.lead_fetch_paused_until'

    def _app_pause(self):
        return self.env['ir.config_parameter'].sudo().get_str(
            self._APP_PAUSE_KEY)

    def _in_order(self, *events):
        # Events made in one transaction share create_date, and the drain
        # orders by it alone, so space them a second apart.
        for i, event in enumerate(events):
            self.env.cr.execute(
                "UPDATE meta_webhook_event SET create_date = create_date "
                "+ %s * interval '1 second' WHERE id = %s", (i, event.id))
        self.env.invalidate_all()

    def _fetch_except(self, throttled, code=80005, hint=19):
        def fetch(page, leadgen_id, *args, **kwargs):
            if leadgen_id in throttled:
                raise MetaRateLimitError('slow down', code=code,
                                         retry_after_min=hint)
            return self._fake_lead(id=leadgen_id)
        return mock.Mock(side_effect=fetch)

    def _fetched(self, fetch):
        return [c.args[1] for c in fetch.call_args_list]

    def test_page_throttle_sets_page_pause(self):
        self._queue('LGRL_P1')
        self._drain(self._throttle(19))
        self.page.invalidate_recordset()
        self.assertEqual(self.page.lead_fetch_paused_until,
                         T0 + timedelta(minutes=19))
        self.assertFalse(self._app_pause())

    def test_page_throttle_skips_page_continues_others(self):
        a = self._queue('LGRL_PA')
        b = self._queue('LGRL_PB')
        c = self._queue('LGRL_PC', page_id='PGRL2')
        self._in_order(a, b, c)
        fetch = self._fetch_except({'LGRL_PA'})
        self._drain(fetch)
        self.assertEqual(self._fetched(fetch), ['LGRL_PA', 'LGRL_PC'])
        self.assertEqual(b.status, 'pending')
        self.assertFalse(b.next_attempt_at)
        self.assertEqual(b.attempts, 0)
        self.assertEqual(c.status, 'done')

    def test_new_event_waits_for_page_cooldown(self):
        self._queue('LGRL_PN')
        self._drain(self._throttle(19))
        d = self._queue('LGRL_PD')
        self.env.cr.execute(
            "UPDATE meta_webhook_event SET create_date = %s WHERE id = %s",
            (T0 + timedelta(minutes=1), d.id))
        self.env.invalidate_all()
        early = self._fetch_except(set())
        self._drain(early, at=T0 + timedelta(minutes=2))
        self.assertNotIn('LGRL_PD', self._fetched(early))
        self.assertEqual(d.status, 'pending')
        later = self._fetch_except(set())
        self._drain(later, at=T0 + timedelta(minutes=20))
        self.assertIn('LGRL_PD', self._fetched(later))
        self.assertEqual(d.status, 'done')

    def test_app_throttle_holds_every_page(self):
        a = self._queue('LGRL_AA')
        c = self._queue('LGRL_AC', page_id='PGRL2')
        self._in_order(a, c)
        self._drain(self._fetch_except({'LGRL_AA'}, code=4, hint=10))
        self.assertEqual(
            fields.Datetime.to_datetime(self._app_pause()),
            T0 + timedelta(minutes=10))
        self.page.invalidate_recordset()
        self.assertFalse(self.page.lead_fetch_paused_until)
        held = self._fetch_except(set())
        self._drain(held, at=T0 + timedelta(minutes=5))
        self.assertEqual(held.call_count, 0)
        self.assertEqual(c.status, 'pending')
        later = self._fetch_except(set())
        self._drain(later, at=T0 + timedelta(minutes=11))
        self.assertEqual(sorted(self._fetched(later)), ['LGRL_AA', 'LGRL_AC'])
        self.assertEqual(c.status, 'done')

    def test_cooldown_only_moves_later(self):
        self.page.sudo().write(
            {'lead_fetch_paused_until': T0 + timedelta(minutes=60)})
        self.assertTrue(self.page._pause_lead_fetch(T0 + timedelta(minutes=10)))
        self.assertEqual(self.page.lead_fetch_paused_until,
                         T0 + timedelta(minutes=60))
        self.page._pause_lead_fetch(T0 + timedelta(minutes=90))
        self.assertEqual(self.page.lead_fetch_paused_until,
                         T0 + timedelta(minutes=90))

        Event = self.env['meta.webhook.event']
        Event._pause_app_lead_fetch(T0 + timedelta(minutes=60))
        Event._pause_app_lead_fetch(T0 + timedelta(minutes=10))
        self.assertEqual(Event._app_lead_fetch_paused_until(),
                         T0 + timedelta(minutes=60))
        Event._pause_app_lead_fetch(T0 + timedelta(minutes=90))
        self.assertEqual(Event._app_lead_fetch_paused_until(),
                         T0 + timedelta(minutes=90))
        Event._clear_app_lead_fetch_pause()
        self.assertIsNone(Event._app_lead_fetch_paused_until())

    def test_bad_app_pause_value_is_ignored(self):
        self.env['ir.config_parameter'].sudo().set_str(
            self._APP_PAUSE_KEY, 'not a date')
        Event = self.env['meta.webhook.event']
        self.assertIsNone(Event._app_lead_fetch_paused_until())
        Event._pause_app_lead_fetch(T0 + timedelta(minutes=10))
        self.assertEqual(Event._app_lead_fetch_paused_until(),
                         T0 + timedelta(minutes=10))

    def test_page_pause_skipped_when_page_busy(self):
        import psycopg2.errors
        real_execute = self.env.cr.execute

        def _exec(query, params=None, *args, **kwargs):
            q = query if isinstance(query, str) else str(query)
            if 'meta_page' in q and 'NOWAIT' in q:
                raise psycopg2.errors.LockNotAvailable('row is locked')
            return real_execute(query, params, *args, **kwargs)

        with mock.patch.object(self.env.cr, 'execute', side_effect=_exec):
            done = self.page._pause_lead_fetch(T0 + timedelta(minutes=10))
        self.assertFalse(done)
        self.page.invalidate_recordset()
        self.assertFalse(self.page.lead_fetch_paused_until)

    def test_page_view_shows_pause(self):
        arch = self.env['meta.page'].get_views(
            [(False, 'form')])['views']['form']['arch']
        self.assertIn('name="lead_fetch_paused_until"', arch)


@tagged('post_install', '-at_install')
class TestQueueReconcile(MultiCompanyFixtureMixin, IngestFixtureMixin,
                         TransactionCase):
    """Once a Meta lead arrives, its queue entry and old failures close."""

    def setUp(self):
        super().setUp()
        self.Log = self.env['meta.sync.log'].sudo()
        self.Event = self.env['meta.webhook.event'].sudo()

    def _payload(self, leadgen_id):
        # Distinct contact per lead, so business dedup never links them.
        return self._fake_lead(id=leadgen_id, field_data=[
            {'name': 'email', 'values': ['%s@qr.test' % leadgen_id.lower()]},
            {'name': 'full_name', 'values': [leadgen_id]},
        ])

    def _raising(self, exc):
        return mock.Mock(side_effect=exc)

    def _queue(self, leadgen_id, status='pending'):
        return self.Event.create({'leadgen_id': leadgen_id, 'page_id': 'PG1',
                                  'form_id': 'F1', 'status': status})

    def _rows(self, leadgen_id, status=None):
        domain = [('meta_leadgen_id', '=', leadgen_id)]
        if status:
            domain.append(('status', '=', status))
        return self.Log.search(domain)

    def _drain(self, fetch_lead):
        with self._patch_graph(fetch_lead=fetch_lead):
            self.env['meta.webhook.event']._cron_drain()
        self.env.invalidate_all()

    def _retry(self, row, fetch_lead):
        with self._patch_graph(fetch_lead=fetch_lead):
            result = row.with_user(self.admin_a).action_retry()
        self.env.invalidate_all()
        return result

    def _ingest(self, leadgen_id):
        with self._patch_graph(fetch_lead=self._payload(leadgen_id)):
            lead = self.Ingest.ingest_leadgen(self.page, leadgen_id,
                                              trigger='webhook')
        self.env.invalidate_all()
        return lead

    def _failed_event(self, leadgen_id):
        # The drain fails the event for good and writes its one failed row.
        event = self._queue(leadgen_id)
        self._drain(self._raising(MetaPermanentError('gone')))
        self.assertEqual(event.status, 'failed')
        rows = self._rows(leadgen_id, 'failed')
        self.assertEqual(len(rows), 1)
        return event, rows

    def _lead_of(self, leadgen_id):
        return self.env['crm.lead'].with_context(active_test=False).search(
            [('meta_leadgen_id', '=', leadgen_id)])

    def _assert_closed(self, event, leadgen_id, lead):
        self.assertEqual(event.status, 'done')
        self.assertEqual(event.lead_id, lead)
        self.assertFalse(event.next_attempt_at)
        self.assertFalse(self._rows(leadgen_id, 'failed'))

    # ---- sync-log retry ---------------------------------------------------

    def test_log_retry_closes_failed_event(self):
        event, row = self._failed_event('LGQR_1')
        self._retry(row, self._payload('LGQR_1'))
        lead = self._lead_of('LGQR_1')
        self.assertEqual(len(lead), 1)
        self._assert_closed(event, 'LGQR_1', lead)
        self.assertEqual(row.status, 'success')
        self.assertEqual(row.lead_id, lead)

    def test_log_retry_when_lead_already_exists(self):
        lead = self._ingest('LGQR_K')
        original = self._rows('LGQR_K', 'success')
        self.assertEqual(len(original), 1)
        row = self.Log._record_failure('LGQR_K', 'webhook', 'x',
                                       page=self.page)
        event = self._queue('LGQR_K', status='failed')
        self._retry(row, self._payload('LGQR_K'))
        self.assertIn(row.status, ('success', 'skipped_idempotent'))
        self.assertEqual(row.lead_id, lead)
        self._assert_closed(event, 'LGQR_K', lead)
        self.assertTrue(original.exists())

    def test_log_retry_transient_requeues_event(self):
        event, row = self._failed_event('LGQR_T')
        self._retry(row, self._raising(MetaTransientError('timeout')))
        self.assertEqual(row.status, 'failed')
        self.assertIn('timeout', row.error_message)
        self.assertEqual(event.status, 'pending')
        self.assertEqual(event.attempts, 0)
        self.assertFalse(event.next_attempt_at)
        self._drain(self._payload('LGQR_T'))
        self._assert_closed(event, 'LGQR_T', self._lead_of('LGQR_T'))

    def test_log_retry_ratelimit_requeues_with_hint(self):
        event, row = self._failed_event('LGQR_R')
        throttle = self._raising(MetaRateLimitError(
            'slow', code=80005, retry_after_min=30))
        with mock.patch.object(fields.Datetime, 'now', return_value=T0):
            self._retry(row, throttle)
        until = T0 + timedelta(minutes=30)
        self.assertEqual(event.status, 'pending')
        self.assertEqual(event.attempts, 0)
        self.assertEqual(event.next_attempt_at, until)
        self.assertEqual(self.page.lead_fetch_paused_until, until)
        self.assertEqual(row.status, 'failed')
        self.assertIn('rate limit', row.error_message)
        self.assertNotIn('PG1', row.error_message)

    def test_log_retry_permanent_keeps_event_failed(self):
        event, row = self._failed_event('LGQR_P')
        self._retry(row, self._raising(MetaPermanentError('still gone')))
        self.assertEqual(event.status, 'failed')
        self.assertIn('still gone', event.error_message)
        rows = self._rows('LGQR_P', 'failed')
        self.assertEqual(rows, row)
        self.assertEqual(row.retry_count, 1)

    def test_log_retry_busy_event_changes_nothing(self):
        event, row = self._failed_event('LGQR_Y')
        before = (row.error_message, row.retry_count)
        fetch = mock.Mock(return_value=self._payload('LGQR_Y'))
        with mock.patch.object(type(self.Event), '_lock_leadgen_event',
                               return_value=None):
            result = self._retry(row, fetch)
        self.assertFalse(fetch.called)
        self.assertEqual((row.error_message, row.retry_count), before)
        self.assertEqual(event.status, 'failed')
        self.assertIn('already being retried', result['params']['message'])

    # ---- drain and backfill -----------------------------------------------

    def test_drain_success_clears_stale_rows(self):
        self.Log._record_failure('LGQR_3', 'webhook', 'old', page=self.page)
        event = self._queue('LGQR_3')
        self._drain(self._payload('LGQR_3'))
        self.assertEqual(event.status, 'done')
        self.assertFalse(self._rows('LGQR_3', 'failed'))
        self.assertEqual(len(self._rows('LGQR_3', 'success')), 1)

    def test_reconcile_keeps_history(self):
        lead = self._ingest('LGQR_KH')
        self.Log._record('LGQR_KH', 'webhook', 'skipped_idempotent',
                         lead=lead, page=self.page)
        history = self._rows('LGQR_KH')
        self.assertEqual(len(history), 2)
        self.Log._record_failure('LGQR_KH', 'webhook', 'x', page=self.page)
        done = self.Log._reconcile_leadgen('LGQR_KH', lead)
        self.env.invalidate_all()
        self.assertTrue(done)
        self.assertEqual(self._rows('LGQR_KH'), history)

    def _sweep(self, fetch):
        item = {'id': 'LGQR_B', 'created_time': '2026-06-01T08:00:00+0000'}

        def _paged(token, path, params=None, app_secret=None, status=None, **kw):
            return iter([dict(item)])
        with mock.patch.object(self.Client, '_iter_paged',
                               side_effect=_paged), \
                self._patch_graph(fetch_lead=fetch):
            self.env['meta.lead.form']._cron_backfill()
        self.env.invalidate_all()

    def test_backfill_success_reconciles(self):
        self.Log._record_failure('LGQR_B', 'webhook', 'x', page=self.page)
        event = self._queue('LGQR_B', status='failed')
        self._sweep(self._payload('LGQR_B'))
        self.assertFalse(self._rows('LGQR_B', 'failed'))
        rows = self._rows('LGQR_B')
        self.assertEqual(len(rows), 1)
        self.assertEqual(rows.status, 'success')
        self._assert_closed(event, 'LGQR_B', self._lead_of('LGQR_B'))

    def test_backfill_failure_keeps_rows(self):
        self.Log._record_failure('LGQR_B', 'webhook', 'x', page=self.page)
        event = self._queue('LGQR_B', status='failed')
        self._sweep(self._raising(MetaPermanentError('gone')))
        self.assertEqual(len(self._rows('LGQR_B', 'failed')), 1)
        self.assertEqual(event.status, 'failed')

    # ---- healing ------------------------------------------------------------

    def _lost_race(self, leadgen_id):
        lead = self._ingest(leadgen_id)
        self.Log._record_failure(leadgen_id, 'webhook', 'late',
                                 page=self.page)
        event = self._queue(leadgen_id, status='failed')
        return lead, event

    def test_failure_after_success_is_healed(self):
        lead, event = self._lost_race('LGQR_H')
        fetch = mock.Mock(return_value=self._payload('LGQR_H'))
        self._drain(fetch)
        self.assertFalse(fetch.called)
        self._assert_closed(event, 'LGQR_H', lead)

    def test_heal_uses_lead_when_history_vacuumed(self):
        lead, event = self._lost_race('LGQR_V')
        self._rows('LGQR_V', 'success').unlink()
        self._drain(mock.Mock(return_value=self._payload('LGQR_V')))
        self._assert_closed(event, 'LGQR_V', lead)

    def test_heal_is_bounded(self):
        ids = ('LGQR_N1', 'LGQR_N2', 'LGQR_N3')
        for leadgen_id in ids:
            self._lost_race(leadgen_id)

        def _open():
            return sum(1 for i in ids if self._rows(i, 'failed'))
        self.assertEqual(self.Log._heal_resolved_failures(limit=2), 2)
        self.env.invalidate_all()
        self.assertEqual(_open(), 1)
        self.assertEqual(self.Log._heal_resolved_failures(limit=2), 1)
        self.env.invalidate_all()
        self.assertEqual(_open(), 0)


@tagged('post_install', '-at_install')
class TestQueueRetry(MultiCompanyFixtureMixin, IngestFixtureMixin,
                     TransactionCase):
    """Retry from the webhook queue, through the sync-log retry engine."""

    _APP_PAUSE_KEY = 'meta_lead_ads.lead_fetch_paused_until'
    _NO_COMPANY = "Sync rows that belong to no company can't be retried here."
    _NOTHING = "Select at least one failed or waiting webhook event to retry."

    def setUp(self):
        super().setUp()
        self.Log = self.env['meta.sync.log'].sudo()
        self.Event = self.env['meta.webhook.event'].sudo()

    def _payload(self, leadgen_id):
        return self._fake_lead(id=leadgen_id, field_data=[
            {'name': 'email', 'values': ['%s@qretry.test' % leadgen_id.lower()]},
            {'name': 'full_name', 'values': [leadgen_id]},
        ])

    def _raising(self, exc):
        return mock.Mock(side_effect=exc)

    def _queue(self, leadgen_id, page_id='PG1', status='pending', **vals):
        vals.update({'leadgen_id': leadgen_id, 'page_id': page_id,
                     'form_id': 'F1', 'status': status})
        return self.Event.create(vals)

    def _rows(self, leadgen_id, status=None):
        domain = [('meta_leadgen_id', '=', leadgen_id)]
        if status:
            domain.append(('status', '=', status))
        return self.Log.search(domain)

    def _lead_of(self, leadgen_id):
        return self.env['crm.lead'].with_context(active_test=False).search(
            [('meta_leadgen_id', '=', leadgen_id)])

    def _drain(self, fetch_lead):
        with self._patch_graph(fetch_lead=fetch_lead):
            self.env['meta.webhook.event']._cron_drain()
        self.env.invalidate_all()

    def _failed_event(self, leadgen_id):
        # The drain fails the event for good and writes its one failed row.
        event = self._queue(leadgen_id)
        self._drain(self._raising(MetaPermanentError('gone')))
        self.assertEqual(event.status, 'failed')
        self.assertEqual(len(self._rows(leadgen_id, 'failed')), 1)
        return event

    def _retry(self, event, fetch_lead, user=None):
        with self._patch_graph(fetch_lead=fetch_lead):
            result = event.with_user(user or self.admin_a).action_retry()
        self.env.invalidate_all()
        return result

    def _assert_done(self, event, leadgen_id):
        lead = self._lead_of(leadgen_id)
        self.assertEqual(len(lead), 1)
        self.assertEqual(event.status, 'done')
        self.assertEqual(event.lead_id, lead)
        self.assertFalse(self._rows(leadgen_id, 'failed'))
        return lead

    # ---- outcomes, shared with the sync-log retry -------------------------

    def test_event_retry_success_reconciles(self):
        event = self._failed_event('LGQR_E')
        result = self._retry(event, self._payload('LGQR_E'))
        self.assertEqual(result['tag'], 'display_notification')
        lead = self._assert_done(event, 'LGQR_E')
        rows = self._rows('LGQR_E')
        self.assertEqual(len(rows), 1)
        self.assertEqual(rows.status, 'success')
        self.assertEqual(rows.lead_id, lead)

    def test_event_retry_without_log_row(self):
        event = self._queue('LGQR_L', status='failed', error_message='gone')
        self.assertFalse(self._rows('LGQR_L'))
        self._retry(event, self._payload('LGQR_L'))
        self._assert_done(event, 'LGQR_L')
        rows = self._rows('LGQR_L')
        self.assertEqual(len(rows), 1)
        self.assertEqual(rows.status, 'success')

    def test_event_retry_transient_requeues(self):
        event = self._failed_event('LGQR_T')
        self._retry(event, self._raising(MetaTransientError('timeout')))
        self.assertEqual(event.status, 'pending')
        self.assertEqual(event.attempts, 0)
        self.assertFalse(event.next_attempt_at)
        row = self._rows('LGQR_T', 'failed')
        self.assertEqual(len(row), 1)
        self.assertIn('timeout', row.error_message)
        self._drain(self._payload('LGQR_T'))
        self._assert_done(event, 'LGQR_T')

    def test_event_retry_ratelimit_requeues_after_hint(self):
        event = self._failed_event('LGQR_R')
        throttle = self._raising(MetaRateLimitError(
            'slow', code=80005, retry_after_min=30))
        with mock.patch.object(fields.Datetime, 'now', return_value=T0):
            self._retry(event, throttle)
        until = T0 + timedelta(minutes=30)
        self.assertEqual(event.status, 'pending')
        self.assertEqual(event.attempts, 0)
        self.assertEqual(event.next_attempt_at, until)
        self.assertEqual(self.page.lead_fetch_paused_until, until)

    def test_event_retry_permanent_stays_failed(self):
        event = self._failed_event('LGQR_P')
        self._retry(event, self._raising(MetaPermanentError('still gone')))
        self.assertEqual(event.status, 'failed')
        self.assertIn('still gone', event.error_message)
        rows = self._rows('LGQR_P', 'failed')
        self.assertEqual(len(rows), 1)
        self.assertEqual(rows.retry_count, 1)

    def test_event_retry_unexpected_error_persisted(self):
        event = self._failed_event('LGQR_X')
        with mock.patch.object(type(self.Ingest), '_map_fields',
                               side_effect=ValueError('leaky detail')), \
                self.assertLogs(_EVENT_LOGGER) as logs:
            result = self._retry(event, self._payload('LGQR_X'))
        self.assertEqual(event.status, 'failed')
        self.assertEqual(event.error_message, 'Unexpected retry error')
        rows = self._rows('LGQR_X', 'failed')
        self.assertEqual(len(rows), 1)
        self.assertEqual(rows.error_message, 'Unexpected retry error')
        self.assertIn('1 still failed', result['params']['message'])
        for record in logs.records:
            self.assertNotIn('leaky detail', record.getMessage())
            self.assertFalse(record.exc_info)

    # ---- who may retry what -----------------------------------------------

    def _meta_user(self):
        return new_test_user(
            self.env, 'qr_meta_user',
            groups='base.group_user,meta_lead_ads.group_meta_user',
            company_id=self.co_a.id, company_ids=[Command.set([self.co_a.id])])

    def test_event_retry_requires_meta_admin(self):
        event = self._queue('LGQR_A', status='failed', error_message='gone')
        for user in (self.sales_mgr_a, self.sales_a, self._meta_user()):
            with self.subTest(user=user.login), \
                    self.assertRaises(AccessError):
                event.with_user(user).action_retry()
        self.assertEqual(event.status, 'failed')

    def test_event_retry_other_company_refused(self):
        event = self._queue('LGQR_B', page_id='PGB', status='failed',
                            error_message='gone',
                            page_company_id=self.co_b.id)
        fetch = mock.Mock(return_value=self._payload('LGQR_B'))
        with self.assertRaises(AccessError):
            self._retry(event, fetch)
        self.assertFalse(fetch.called)
        self.assertEqual(event.status, 'failed')
        self.assertEqual(event.error_message, 'gone')
        self.assertFalse(self._rows('LGQR_B'))
        self._retry(event, self._payload('LGQR_B'), user=self.admin_b)
        lead = self._assert_done(event, 'LGQR_B')
        self.assertEqual(lead.company_id, self.co_b)

    def test_event_leadgen_id_is_fixed(self):
        event = self._queue('LGQR_I', status='failed', error_message='gone',
                            page_company_id=self.co_a.id)
        # The admin may change other fields of the event.
        event.with_user(self.admin_a).write({'error_message': 'noted'})
        with self.assertRaises(AccessError):
            event.with_user(self.admin_a).write({'leadgen_id': 'LGQR_I2'})
        self.env.invalidate_all()
        self.assertEqual(event.leadgen_id, 'LGQR_I')
        # The drain and the controller write as superuser.
        event.write({'leadgen_id': 'LGQR_I3'})
        self.assertEqual(event.leadgen_id, 'LGQR_I3')

    def test_event_retry_leaves_other_company_row(self):
        event = self._queue('LGQR_Z', status='failed', error_message='gone')
        row = self.Log._record_failure('LGQR_Z', 'webhook', 'from b',
                                       page=self.page_b)
        self.assertEqual(row.company_id, self.co_b)
        fetch = mock.Mock(return_value=self._payload('LGQR_Z'))
        result = self._retry(event, fetch)
        self.assertFalse(fetch.called)
        self.assertEqual(row.company_id, self.co_b)
        self.assertEqual(row.page_id, self.page_b)
        self.assertEqual(row.error_message, 'from b')
        self.assertEqual(row.retry_count, 0)
        self.assertEqual(event.status, 'failed')
        self.assertIn('1 still failed', result['params']['message'])

    def test_event_retry_page_without_company_refused(self):
        page = self.env['meta.page'].sudo().create({
            'name': 'Page QRN', 'page_id': 'PGQRN', 'access_token': 'tok_qrn',
            'account_id': self.account.id})
        # Flush first, or the pending compute writes the company back.
        self.env.flush_all()
        self.env.cr.execute(
            "UPDATE meta_page SET lead_company_id = NULL WHERE id = %s",
            (page.id,))
        self.env.invalidate_all()
        event = self._queue('LGQR_N', page_id='PGQRN', status='failed',
                            error_message='gone')
        fetch = mock.Mock(return_value=self._payload('LGQR_N'))

        def _assert_unchanged():
            self.env.invalidate_all()
            self.assertFalse(fetch.called)
            self.assertEqual(event.status, 'failed')
            self.assertEqual(event.error_message, 'gone')
            self.assertFalse(self._rows('LGQR_N'))

        # The Page rule hides it from admin_a.
        with self.assertRaises(UserError) as ctx:
            self._retry(event, fetch)
        self.assertIs(type(ctx.exception), UserError)
        self.assertNotIn('PGQRN', str(ctx.exception))
        _assert_unchanged()

        # Should that rule ever let it through, the company check still
        # refuses it.
        PageModel = type(self.env['meta.page'])
        real_search = PageModel.search

        def _search(this, domain, *args, **kwargs):
            if domain == [('page_id', '=', 'PGQRN')]:
                return this.browse(page.id)
            return real_search(this, domain, *args, **kwargs)
        with mock.patch.object(PageModel, 'search', new=_search), \
                self.assertRaises(UserError) as ctx:
            self._retry(event, fetch)
        self.assertEqual(ctx.exception.args[0], self._NO_COMPANY)
        _assert_unchanged()

    def _unknown_page_event(self, leadgen_id, page_id):
        # What the drain leaves for a Page that was not connected yet.
        message = 'Unknown page; cannot process event'
        event = self._queue(leadgen_id, page_id=page_id, status='failed',
                            error_message=message)
        self.Log._record_failure(leadgen_id, 'webhook', message)
        self.assertFalse(event.company_id)
        return event

    def test_unknown_page_event_connected_elsewhere_refused(self):
        event = self._unknown_page_event('LGQR_UX', 'PGQRX')
        self.env['meta.page'].create({
            'name': 'Page QRX', 'page_id': 'PGQRX', 'access_token': 'tok_qrx',
            'account_id': self.account.id, 'company_id': self.co_b.id})
        fetch = mock.Mock(return_value=self._payload('LGQR_UX'))
        with self.assertRaises(UserError) as ctx:
            self._retry(event, fetch)
        self.assertIs(type(ctx.exception), UserError)
        self.assertNotIn('PGQRX', str(ctx.exception))
        self.assertFalse(fetch.called)
        self.assertEqual(event.status, 'failed')
        self.assertFalse(event.company_id)
        self._retry(event, self._payload('LGQR_UX'), user=self.admin_b)
        lead = self._assert_done(event, 'LGQR_UX')
        self.assertEqual(lead.company_id, self.co_b)
        self.assertEqual(event.company_id, self.co_b)

    def test_unknown_page_event_claimed_after_connect(self):
        event = self._unknown_page_event('LGQR_UY', 'PGQRY')
        self.env['meta.page'].create({
            'name': 'Page QRY', 'page_id': 'PGQRY', 'access_token': 'tok_qry',
            'account_id': self.account.id})
        self._retry(event, self._payload('LGQR_UY'))
        self._assert_done(event, 'LGQR_UY')
        self.assertEqual(event.company_id, self.co_a)
        rows = self._rows('LGQR_UY')
        self.assertEqual(len(rows), 1)
        self.assertEqual(rows.company_id, self.co_a)
        with self.assertRaises(AccessError):
            event.with_user(self.admin_b).check_access('read')

    # ---- archived Page ----------------------------------------------------

    _ARCHIVED_NOTE = ("belong to an archived Page; reactivate the Page, then "
                      "retry.")

    def _archive_page(self):
        self.page.sudo().write({'active': False})

    def test_queue_retry_refused_while_page_archived(self):
        event = self._failed_event('LGQA1')
        row = self._rows('LGQA1', 'failed')
        before = (row.trigger, row.failed_at, row.retry_count)
        self._archive_page()
        fetch = mock.Mock(return_value=self._payload('LGQA1'))
        result = self._retry(event, fetch)
        self.assertEqual(fetch.call_count, 0)
        self.assertEqual(event.status, 'failed')
        self.assertEqual(event.error_message, _PAGE_ARCHIVED_MESSAGE)
        self.assertEqual(row.status, 'failed')
        self.assertEqual(row.page_id, self.page)
        self.assertEqual(row.error_message, _PAGE_ARCHIVED_MESSAGE)
        self.assertEqual((row.trigger, row.failed_at, row.retry_count),
                         before)
        self.assertFalse(self._lead_of('LGQA1'))
        message = result['params']['message']
        self.assertIn(self._ARCHIVED_NOTE, message)
        self.assertIn('1 still failed', message)

    def test_queue_retry_of_unknown_page_still_refused(self):
        event = self._unknown_page_event('LGQA2', 'PG_NONE')
        fetch = mock.Mock(return_value=self._payload('LGQA2'))
        with self.assertRaises(UserError) as ctx:
            self._retry(event, fetch)
        self.assertEqual(ctx.exception.args[0],
                         self.Event._page_out_of_reach_message())
        self.assertFalse(fetch.called)
        self.assertEqual(event.status, 'failed')
        self.assertEqual(event.error_message,
                         'Unknown page; cannot process event')

    def test_company_less_row_of_archived_page_gets_its_page_on_retry(self):
        # admin_a works in company A only, so the Page sends its leads there.
        self.page.sudo().write({'company_id': self.co_a.id})
        event = self._unknown_page_event('LGQA3', 'PG1')
        self.assertFalse(event.page_company_id)
        row = self._rows('LGQA3', 'failed')
        self.assertFalse(row.company_id)
        self.assertFalse(row.page_id)
        self._archive_page()
        self.assertEqual(self.page.lead_company_id, self.co_a)
        fetch = mock.Mock(return_value=self._payload('LGQA3'))
        with self._patch_graph(fetch_lead=fetch):
            result = row.with_user(self.admin_a).action_retry()
        self.env.invalidate_all()
        self.assertEqual(fetch.call_count, 0)
        self.assertEqual(row.status, 'failed')
        self.assertEqual(row.page_id, self.page)
        self.assertEqual(row.company_id, self.co_a)
        self.assertEqual(row.error_message, _PAGE_ARCHIVED_MESSAGE)
        self.assertEqual(row.retry_count, 0)
        self.assertFalse(self._lead_of('LGQA3'))
        self.assertIn(self._ARCHIVED_NOTE, result['params']['message'])

    def _pause_everything(self, until):
        self.page.sudo().write({'lead_fetch_paused_until': until})
        self.env['ir.config_parameter'].sudo().set_str(
            self._APP_PAUSE_KEY, fields.Datetime.to_string(until))

    def _assert_still_paused(self, until):
        self.assertEqual(self.page.lead_fetch_paused_until, until)
        self.assertEqual(fields.Datetime.to_datetime(
            self.env['ir.config_parameter'].sudo().get_str(
                self._APP_PAUSE_KEY)), until)

    def test_waiting_event_of_archived_page_keeps_every_pause(self):
        """Ending the wait would lift Meta's throttle for a lead the drain
        would not fetch anyway, so the click changes nothing."""
        later = fields.Datetime.now() + timedelta(days=1)
        event = self._queue('LGQA4', next_attempt_at=later)
        self._pause_everything(later)
        self._archive_page()
        fetch = mock.Mock(return_value=self._payload('LGQA4'))
        with mock.patch.object(type(self.env['ir.cron']), '_trigger',
                               autospec=True) as trigger:
            result = self._retry(event, fetch)
        self.assertFalse(fetch.called)
        self.assertFalse(trigger.called)
        self.assertEqual(event.status, 'pending')
        self.assertEqual(event.next_attempt_at, later)
        self._assert_still_paused(later)
        message = result['params']['message']
        self.assertIn(self._ARCHIVED_NOTE, message)
        self.assertNotIn('scheduled', message)

    def test_failed_event_of_archived_page_is_archived_while_reads_held(self):
        """While Meta asks to wait, both Retry buttons still say the Page is
        archived rather than that the lead was held back."""
        event = self._failed_event('LGQA5')
        row = self._rows('LGQA5', 'failed')
        self._archive_page()
        later = fields.Datetime.now() + timedelta(days=1)
        self._pause_everything(later)
        fetch = mock.Mock(return_value=self._payload('LGQA5'))
        queue_message = self._retry(event, fetch)['params']['message']
        with self._patch_graph(fetch_lead=fetch):
            log_message = row.with_user(self.admin_a).action_retry()[
                'params']['message']
        self.env.invalidate_all()
        self.assertFalse(fetch.called)
        for message in (queue_message, log_message):
            self.assertIn(self._ARCHIVED_NOTE, message)
            self.assertNotIn('held back', message)
        self.assertEqual(queue_message, log_message)
        self.assertEqual(event.status, 'failed')
        self.assertEqual(event.error_message, _PAGE_ARCHIVED_MESSAGE)
        self.assertEqual(row.status, 'failed')
        self.assertEqual(row.error_message, _PAGE_ARCHIVED_MESSAGE)
        self.assertEqual(row.retry_count, 0)
        self._assert_still_paused(later)

    # ---- locking ----------------------------------------------------------

    def test_event_retry_busy(self):
        event = self._failed_event('LGQR_Y')
        row = self._rows('LGQR_Y', 'failed')
        before = (event.status, event.error_message, row.retry_count,
                  row.error_message)
        fetch = mock.Mock(return_value=self._payload('LGQR_Y'))
        with mock.patch.object(type(self.Event), '_lock_leadgen_event',
                               return_value=None):
            result = self._retry(event, fetch)
        self.assertFalse(fetch.called)
        self.assertEqual((event.status, event.error_message, row.retry_count,
                          row.error_message), before)
        self.assertIn('already being retried', result['params']['message'])

    def test_event_retry_finished_elsewhere(self):
        event = self._failed_event('LGQR_D')
        lead = self.env['crm.lead'].create({'name': 'Elsewhere',
                                            'type': 'lead'})

        def _lock(leadgen_id):
            # Another transaction finished it just before the lock.
            event.write({'status': 'done', 'lead_id': lead.id})
            return event
        fetch = mock.Mock(return_value=self._payload('LGQR_D'))
        with mock.patch.object(type(self.Event), '_lock_leadgen_event',
                               side_effect=_lock):
            result = self._retry(event, fetch)
        self.assertFalse(fetch.called)
        self.assertIn('1 succeeded', result['params']['message'])
        self.assertEqual(event.status, 'done')

    # ---- ending a rate-limit wait -----------------------------------------

    def test_waiting_event_retry_now(self):
        later = T0 + timedelta(days=1)
        event = self._queue('LGQR_WN', next_attempt_at=later)
        self.page.sudo().write({'lead_fetch_paused_until': later})
        self.env['ir.config_parameter'].sudo().set_str(
            self._APP_PAUSE_KEY, fields.Datetime.to_string(later))
        fetch = mock.Mock(return_value=self._payload('LGQR_WN'))
        with mock.patch.object(fields.Datetime, 'now', return_value=T0), \
                mock.patch.object(type(self.env['ir.cron']), '_trigger',
                                  autospec=True) as trigger:
            result = self._retry(event, fetch)
        self.assertFalse(fetch.called)
        self.assertEqual(event.status, 'pending')
        self.assertFalse(event.next_attempt_at)
        self.assertFalse(self.page.lead_fetch_paused_until)
        self.assertFalse(self.env['ir.config_parameter'].sudo().get_str(
            self._APP_PAUSE_KEY))
        self.assertEqual(trigger.call_count, 1)
        self.assertEqual(
            trigger.call_args.args[0],
            self.env.ref('meta_lead_ads.cron_meta_webhook_drain'))
        self.assertIn('1 scheduled to run shortly',
                      result['params']['message'])

    def test_page_wait_leaves_app_setting_alone(self):
        # Only this Page was throttled, so ending its wait has no business
        # with the app-wide pause other companies' Pages share.
        later = T0 + timedelta(days=1)
        event = self._queue('LGQR_WP', next_attempt_at=later)
        self.page.sudo().write({'lead_fetch_paused_until': later})
        EventModel = type(self.env['meta.webhook.event'])
        with mock.patch.object(fields.Datetime, 'now', return_value=T0), \
                mock.patch.object(type(self.env['ir.cron']), '_trigger',
                                  autospec=True) as trigger, \
                mock.patch.object(EventModel, '_clear_app_lead_fetch_pause',
                                  autospec=True) as clear_app:
            result = self._retry(event, mock.Mock())
        self.assertFalse(clear_app.called)
        self.assertFalse(event.next_attempt_at)
        self.assertFalse(self.page.lead_fetch_paused_until)
        self.assertEqual(trigger.call_count, 1)
        self.assertIn('1 scheduled', result['params']['message'])

    def test_end_wait_yields_to_app_pause_holder(self):
        import psycopg2.errors
        later = T0 + timedelta(days=1)
        event = self._queue('LGQR_WA', next_attempt_at=later)
        self.page.sudo().write({'lead_fetch_paused_until': later})
        ICP = self.env['ir.config_parameter'].sudo()
        ICP.set_str(self._APP_PAUSE_KEY, fields.Datetime.to_string(later))
        real_execute = self.env.cr.execute

        def _exec(query, params=None, *args, **kwargs):
            q = query if isinstance(query, str) else str(query)
            if 'ir_config_parameter' in q and 'NOWAIT' in q:
                raise psycopg2.errors.LockNotAvailable('row is locked')
            return real_execute(query, params, *args, **kwargs)
        with mock.patch.object(fields.Datetime, 'now', return_value=T0), \
                mock.patch.object(type(self.env['ir.cron']), '_trigger',
                                  autospec=True) as trigger, \
                mock.patch.object(self.env.cr, 'execute', side_effect=_exec):
            result = self._retry(event, mock.Mock())
        self.assertEqual(event.next_attempt_at, later)
        self.assertEqual(self.page.lead_fetch_paused_until, later)
        self.assertEqual(fields.Datetime.to_datetime(
            ICP.get_str(self._APP_PAUSE_KEY)), later)
        self.assertFalse(trigger.called)
        self.assertIn('already being retried', result['params']['message'])

    def test_pending_event_without_wait_refused(self):
        event = self._queue('LGQR_PW')
        with self.assertRaises(UserError) as ctx:
            self._retry(event, self._payload('LGQR_PW'))
        self.assertEqual(ctx.exception.args[0], self._NOTHING)

    def test_done_event_refused(self):
        event = self._queue('LGQR_DN', status='done')
        with self.assertRaises(UserError) as ctx:
            self._retry(event, self._payload('LGQR_DN'))
        self.assertEqual(ctx.exception.args[0], self._NOTHING)

    # ---- views ------------------------------------------------------------

    def test_event_retry_ui(self):
        Event = self.env['meta.webhook.event']
        arch = Event.with_user(self.admin_a).get_views(
            [(False, 'form')])['views']['form']['arch']
        self.assertIn('name="action_retry"', arch)
        action = self.env.ref(
            'meta_lead_ads.action_meta_webhook_event_retry_server')
        self.assertEqual(action.binding_model_id.model, 'meta.webhook.event')
        groups = ('group_ids' if 'group_ids' in action._fields
                  else 'groups_id')
        self.assertIn(self.env.ref('meta_lead_ads.group_meta_admin'),
                      action[groups])
        # CRM managers can't open the queue at all; Meta Users can, without
        # the button.
        with self.assertRaises(AccessError):
            Event.with_user(self.sales_mgr_a).get_views([(False, 'form')])
        as_user = Event.with_user(self._meta_user()).get_views(
            [(False, 'form')])['views']['form']['arch']
        self.assertNotIn('name="action_retry"', as_user)


@tagged('post_install', '-at_install')
class TestRetryHoldsBack(MultiCompanyFixtureMixin, IngestFixtureMixin,
                         TransactionCase):
    """A Retry click stops calling Meta once Meta asks it to wait."""

    _APP_PAUSE_KEY = 'meta_lead_ads.lead_fetch_paused_until'
    _CONST = 'odoo.addons.meta_lead_ads.models.const'

    def setUp(self):
        super().setUp()
        self.Log = self.env['meta.sync.log'].sudo()
        self.Event = self.env['meta.webhook.event'].sudo()
        self.page2 = self.env['meta.page'].create({
            'name': 'Page HB2', 'page_id': 'PGHB2', 'access_token': 'tok_hb2',
            'account_id': self.account.id})

    def _payload(self, leadgen_id):
        return self._fake_lead(id=leadgen_id, field_data=[
            {'name': 'email', 'values': ['%s@hold.test' % leadgen_id.lower()]},
            {'name': 'full_name', 'values': [leadgen_id]},
        ])

    def _fetch(self, throttled=(), code=80005):
        def fetch(page, leadgen_id, *args, **kwargs):
            if leadgen_id in throttled:
                raise MetaRateLimitError('slow down', code=code,
                                         retry_after_min=30)
            return self._payload(leadgen_id)
        return mock.Mock(side_effect=fetch)

    def _fetched(self, fetch):
        return [c.args[1] for c in fetch.call_args_list]

    def _rows(self, *specs):
        rows = self.Log.browse()
        for leadgen_id, page in specs:
            rows |= self.Log._record_failure(leadgen_id, 'webhook', 'old',
                                             page=page)
        return rows

    def _retry(self, records, fetch):
        with self._patch_graph(fetch_lead=fetch), \
                mock.patch.object(fields.Datetime, 'now', return_value=T0), \
                mock.patch.object(type(self.env['ir.cron']), '_trigger'):
            result = records.with_user(self.admin_a).action_retry()
        self.env.invalidate_all()
        return result['params']['message']

    def _assert_untouched(self, row):
        self.assertEqual(row.status, 'failed')
        self.assertEqual(row.retry_count, 0)
        self.assertEqual(row.error_message, 'old')

    # ---- sync-log retry ---------------------------------------------------

    def test_log_retry_stops_page_after_throttle(self):
        rows = self._rows(('LGHB_1', self.page), ('LGHB_2', self.page),
                          ('LGHB_3', self.page2))
        fetch = self._fetch(throttled={'LGHB_1'})
        message = self._retry(rows, fetch)
        self.assertEqual(self._fetched(fetch), ['LGHB_1', 'LGHB_3'])
        self._assert_untouched(rows[1])
        self.assertEqual(rows[2].status, 'success')
        self.assertIn('1 of them were held back', message)

    def test_log_retry_stops_all_after_app_throttle(self):
        rows = self._rows(('LGHB_A1', self.page), ('LGHB_A2', self.page2))
        fetch = self._fetch(throttled={'LGHB_A1'}, code=4)
        message = self._retry(rows, fetch)
        self.assertEqual(self._fetched(fetch), ['LGHB_A1'])
        self._assert_untouched(rows[1])
        self.assertIn('held back', message)

    def test_log_retry_waits_for_page_pause(self):
        rows = self._rows(('LGHB_P1', self.page), ('LGHB_P2', self.page2))
        self.page.sudo().write(
            {'lead_fetch_paused_until': T0 + timedelta(minutes=10)})
        fetch = self._fetch()
        message = self._retry(rows, fetch)
        self.assertEqual(self._fetched(fetch), ['LGHB_P2'])
        self._assert_untouched(rows[0])
        self.assertIn('held back', message)

    def test_log_retry_waits_for_app_pause(self):
        rows = self._rows(('LGHB_W1', self.page2))
        self.env['ir.config_parameter'].sudo().set_str(
            self._APP_PAUSE_KEY,
            fields.Datetime.to_string(T0 + timedelta(minutes=10)))
        fetch = self._fetch()
        self._retry(rows, fetch)
        self.assertFalse(fetch.called)
        self._assert_untouched(rows)

    def test_log_retry_ignores_pause_that_is_over(self):
        rows = self._rows(('LGHB_O1', self.page))
        self.page.sudo().write(
            {'lead_fetch_paused_until': T0 - timedelta(minutes=1)})
        fetch = self._fetch()
        self._retry(rows, fetch)
        self.assertEqual(self._fetched(fetch), ['LGHB_O1'])
        self.assertEqual(rows.status, 'success')

    def test_log_retry_batch_is_capped(self):
        rows = self._rows(('LGHB_C1', self.page), ('LGHB_C2', self.page),
                          ('LGHB_C3', self.page))
        fetch = self._fetch()
        with mock.patch(self._CONST + '.RETRY_BATCH_LIMIT', 2):
            message = self._retry(rows, fetch)
        self.assertEqual(self._fetched(fetch), ['LGHB_C1', 'LGHB_C2'])
        self._assert_untouched(rows[2])
        self.assertIn('1 more were not retried', message)

    def test_log_retry_stops_at_time_budget(self):
        rows = self._rows(('LGHB_T1', self.page), ('LGHB_T2', self.page))
        fetch = self._fetch()
        clock = mock.Mock(side_effect=[0, 0, 1000, 1000, 1000])
        with mock.patch('odoo.addons.meta_lead_ads.models.meta_sync_log.'
                        '_monotonic', clock):
            message = self._retry(rows, fetch)
        self.assertEqual(self._fetched(fetch), ['LGHB_T1'])
        self._assert_untouched(rows[1])
        self.assertIn('1 more were not retried', message)

    # ---- queue retry ------------------------------------------------------

    def _failed_events(self, *specs):
        events = self.Event.browse()
        for leadgen_id, page in specs:
            events |= self.Event.create({
                'leadgen_id': leadgen_id, 'page_id': page.page_id,
                'form_id': 'F1', 'status': 'failed', 'error_message': 'old',
                'page_company_id': page.lead_company_id.id})
        return events

    def test_event_retry_stops_page_after_throttle(self):
        events = self._failed_events(('LGHB_E1', self.page),
                                     ('LGHB_E2', self.page),
                                     ('LGHB_E3', self.page2))
        fetch = self._fetch(throttled={'LGHB_E1'})
        message = self._retry(events, fetch)
        self.assertEqual(self._fetched(fetch), ['LGHB_E1', 'LGHB_E3'])
        self.assertEqual(events[1].status, 'failed')
        self.assertEqual(events[1].error_message, 'old')
        self.assertFalse(self.Log.search([('meta_leadgen_id', '=', 'LGHB_E2')]))
        self.assertEqual(events[2].status, 'done')
        self.assertIn('1 of them were held back', message)

    def test_event_retry_waits_for_page_pause(self):
        events = self._failed_events(('LGHB_EP', self.page))
        self.page.sudo().write(
            {'lead_fetch_paused_until': T0 + timedelta(minutes=10)})
        fetch = self._fetch()
        message = self._retry(events, fetch)
        self.assertFalse(fetch.called)
        self.assertEqual(events.status, 'failed')
        self.assertIn('held back', message)

    def test_event_retry_batch_is_capped(self):
        events = self._failed_events(('LGHB_EC1', self.page),
                                     ('LGHB_EC2', self.page),
                                     ('LGHB_EC3', self.page))
        fetch = self._fetch()
        with mock.patch(self._CONST + '.RETRY_BATCH_LIMIT', 2):
            message = self._retry(events, fetch)
        self.assertEqual(len(fetch.call_args_list), 2)
        self.assertEqual(events.mapped('status').count('failed'), 1)
        self.assertIn('1 more were not retried', message)


@tagged('post_install', '-at_install')
class TestResumeLeadReads(MultiCompanyFixtureMixin, IngestFixtureMixin,
                          TransactionCase):
    """A Meta Admin can end a Page's or the app's lead read pause by hand."""

    _APP_PAUSE_KEY = 'meta_lead_ads.lead_fetch_paused_until'

    def setUp(self):
        super().setUp()
        self.later = T0 + timedelta(days=1)
        self.ICP = self.env['ir.config_parameter'].sudo()
        self.drain = self.env.ref('meta_lead_ads.cron_meta_webhook_drain')

    def _meta_user(self):
        return new_test_user(
            self.env, 'rl_meta_user',
            groups='base.group_user,meta_lead_ads.group_meta_user',
            company_id=self.co_a.id, company_ids=[Command.set([self.co_a.id])])

    def _pause(self, page):
        page.sudo().write({'lead_fetch_paused_until': self.later})

    def _call(self, records, method):
        fetch = mock.Mock()
        with mock.patch.object(fields.Datetime, 'now', return_value=T0), \
                mock.patch.object(type(self.env['ir.cron']), '_trigger',
                                  autospec=True) as trigger, \
                self._patch_graph(fetch_lead=fetch):
            result = getattr(records, method)()
        self.env.invalidate_all()
        self.assertFalse(fetch.called)
        return result, trigger

    def _busy(self, table):
        import psycopg2.errors
        real_execute = self.env.cr.execute

        def _exec(query, params=None, *args, **kwargs):
            q = query if isinstance(query, str) else str(query)
            if table in q and 'NOWAIT' in q:
                raise psycopg2.errors.LockNotAvailable('row is locked')
            return real_execute(query, params, *args, **kwargs)
        return mock.patch.object(self.env.cr, 'execute', side_effect=_exec)

    # ---- one Page ---------------------------------------------------------

    def test_page_resume(self):
        self._pause(self.page)
        self._pause(self.page_b)
        result, trigger = self._call(self.page.with_user(self.admin_a),
                                     'action_resume_lead_reads')
        self.assertFalse(self.page.lead_fetch_paused_until)
        self.assertEqual(self.page_b.lead_fetch_paused_until, self.later)
        self.assertEqual(trigger.call_count, 1)
        self.assertEqual(trigger.call_args.args[0], self.drain)
        self.assertEqual(result['tag'], 'display_notification')

    def test_page_resume_admin_only(self):
        self._pause(self.page)
        for user in (self.sales_mgr_a, self._meta_user()):
            with self.subTest(user=user.login), \
                    self.assertRaises(AccessError):
                self.page.with_user(user).action_resume_lead_reads()
        self.env.invalidate_all()
        self.assertEqual(self.page.lead_fetch_paused_until, self.later)

    def test_page_resume_other_company_refused(self):
        self._pause(self.page_b)
        with self.assertRaises(AccessError):
            self.page_b.with_user(self.admin_a).action_resume_lead_reads()
        self.env.invalidate_all()
        self.assertEqual(self.page_b.lead_fetch_paused_until, self.later)
        self._call(self.page_b.with_user(self.admin_b),
                   'action_resume_lead_reads')
        self.assertFalse(self.page_b.lead_fetch_paused_until)

    def test_page_resume_busy(self):
        self._pause(self.page)
        with self._busy('meta_page'), self.assertRaises(UserError):
            self._call(self.page.with_user(self.admin_a),
                       'action_resume_lead_reads')
        self.env.invalidate_all()
        self.assertEqual(self.page.lead_fetch_paused_until, self.later)

    def test_page_resume_ui(self):
        Page = self.env['meta.page']
        arch = Page.with_user(self.admin_a).get_views(
            [(False, 'form')])['views']['form']['arch']
        self.assertIn('name="action_resume_lead_reads"', arch)
        as_user = Page.with_user(self._meta_user()).get_views(
            [(False, 'form')])['views']['form']['arch']
        self.assertNotIn('name="action_resume_lead_reads"', as_user)

    # ---- the whole app ----------------------------------------------------

    def _settings(self, user):
        return (self.env['res.config.settings'].with_user(user)
                .with_context(allowed_company_ids=user.company_ids.ids)
                .create({}))

    def test_app_resume(self):
        self.ICP.set_str(self._APP_PAUSE_KEY,
                           fields.Datetime.to_string(self.later))
        with mock.patch.object(fields.Datetime, 'now', return_value=T0):
            settings = self._settings(self.admin_a)
            self.assertEqual(settings.meta_lead_fetch_paused_until,
                             self.later)
        result, trigger = self._call(settings, 'action_resume_app_lead_reads')
        self.assertFalse(self.ICP.get_str(self._APP_PAUSE_KEY))
        self.assertEqual(trigger.call_count, 1)
        self.assertEqual(trigger.call_args.args[0], self.drain)
        self.assertEqual(result['tag'], 'display_notification')

    def test_app_pause_not_shown_once_over(self):
        self.ICP.set_str(self._APP_PAUSE_KEY, fields.Datetime.to_string(T0))
        with mock.patch.object(fields.Datetime, 'now',
                               return_value=T0 + timedelta(minutes=1)):
            settings = self._settings(self.admin_a)
            self.assertFalse(settings.meta_lead_fetch_paused_until)

    def test_app_resume_admin_only(self):
        self.ICP.set_str(self._APP_PAUSE_KEY,
                           fields.Datetime.to_string(self.later))
        settings = self.env['res.config.settings'].create({})
        for user in (self.sales_mgr_a, self._meta_user()):
            with self.subTest(user=user.login), \
                    self.assertRaises(AccessError):
                settings.with_user(user).action_resume_app_lead_reads()
        self.assertTrue(self.ICP.get_str(self._APP_PAUSE_KEY))

    def test_app_resume_busy(self):
        self.ICP.set_str(self._APP_PAUSE_KEY,
                           fields.Datetime.to_string(self.later))
        settings = self._settings(self.admin_a)
        with self._busy('ir_config_parameter'), \
                self.assertRaises(UserError):
            self._call(settings, 'action_resume_app_lead_reads')
        self.assertTrue(self.ICP.get_str(self._APP_PAUSE_KEY))

    def test_app_resume_ui(self):
        arch = self.env['res.config.settings'].with_user(
            self.admin_a).get_view()['arch']
        self.assertIn('name="action_resume_app_lead_reads"', arch)
        self.assertIn('name="meta_lead_fetch_paused_until"', arch)
