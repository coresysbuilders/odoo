# Meta Lead Ads Integration for Odoo — © 2026 CoreSys Builders. All rights reserved.
# Licensed under the Odoo Proprietary License v1.0 (OPL-1).
# Unauthorized copying, redistribution, or resale of this software, in whole or in
# part, via any medium, is strictly prohibited and constitutes a license violation.
# OPL-1: https://www.odoo.com/documentation/18.0/legal/licenses.html#odoo-apps

"""Regression tests for attribution, dedup and log retention fixes.

Covers: backfill requesting the full attribution field set, the stored
normalized-phone column used by dedup, sync log vacuuming, webhook app secret
selection, and building utm.campaign from the resolved campaign name.
"""
from unittest import mock

from odoo.tests.common import TransactionCase, tagged
from odoo.exceptions import ValidationError


class RegressionFixtureMixin:
    def setUp(self):
        super().setUp()
        self.account = self.env['meta.account'].create({
            'name': 'Acct', 'account_id': 'ACC1',
            'app_id': 'app_test', 'app_secret': 'secret_test',
            'access_token': 'tok_acct',
        })
        self.page = self.env['meta.page'].create({
            'name': 'Page', 'page_id': 'PG1',
            'access_token': 'tok_test', 'account_id': self.account.id,
        })
        self.form = self.env['meta.lead.form'].create({
            'name': 'Contact Us', 'form_id': 'F1', 'page_id': self.page.id,
        })
        self.Form = self.env['meta.lead.form']
        self.Ingest = self.env['meta.lead.ingest']
        self.Lead = self.env['crm.lead']
        self.Log = self.env['meta.sync.log']
        self.ClientClass = type(self.env['meta.graph.client'])
        self.IngestClass = type(self.Ingest)


@tagged('post_install', '-at_install')
class TestBackfillAttributionFields(RegressionFixtureMixin, TransactionCase):
    """The backfill Graph read requests the full attribution set."""

    def test_backfill_requests_full_attribution_fields(self):
        captured = {}

        def _capture(token, path, params=None, app_secret=None):
            captured['params'] = dict(params or {})
            return iter([])

        with mock.patch.object(self.ClientClass, '_iter_paged',
                               side_effect=_capture), \
             mock.patch.object(self.IngestClass, 'ingest_leadgen',
                               return_value=self.Lead.create(
                                   {'name': 'x', 'type': 'lead'})):
            self.Form._cron_backfill()
        fields = captured.get('params', {}).get('fields', '')
        # Without these, a lead first seen by the backfill never gets
        # campaign / ad set / ad attribution.
        for token in ('campaign_id', 'campaign_name', 'adset_id', 'adset_name',
                      'ad_name'):
            self.assertIn(token, fields)


@tagged('post_install', '-at_install')
class TestNormalizedPhoneColumn(RegressionFixtureMixin, TransactionCase):
    """Stored, indexed normalized-phone column drives dedup."""

    def test_normalized_phone_is_stored_and_matches_normalizer(self):
        lead = self.Lead.create({
            'name': 'P', 'phone': '+1 (555) 123-4567', 'type': 'lead'})
        lead.flush_recordset()
        lead.invalidate_recordset(['meta_phone_normalized'])
        self.assertEqual(lead.meta_phone_normalized, '5551234567')
        # The dedup lookup normalizes the incoming phone the same way, so a
        # plain '=' on the stored column finds the lead.
        norm = self.Ingest._normalize_phone('5551234567')
        found = self.Lead.search(
            [('meta_phone_normalized', '=', norm)], limit=1)
        self.assertEqual(found, lead)

    def test_normalized_phone_blank_when_no_digits(self):
        lead = self.Lead.create({'name': 'N', 'phone': False, 'type': 'lead'})
        lead.flush_recordset()
        self.assertFalse(lead.meta_phone_normalized)


@tagged('post_install', '-at_install')
class TestSyncLogVacuum(RegressionFixtureMixin, TransactionCase):
    """Retention sweep prunes old non-actionable rows, keeps failed."""

    def _backdate(self, records, days):
        # create_date is set by the ORM, so age it with SQL.
        self.env.cr.execute(
            "UPDATE meta_sync_log "
            "SET create_date = (now() AT TIME ZONE 'UTC') - %s * interval '1 day' "
            "WHERE id IN %s",
            (days, tuple(records.ids)))
        records.invalidate_recordset(['create_date'])

    def test_vacuum_prunes_old_success_keeps_failed(self):
        old_ok = self.Log._record('LG_OLD_OK', 'cron', 'success')
        old_skip = self.Log._record('LG_OLD_SKIP', 'cron', 'skipped_idempotent')
        old_failed = self.Log._record('LG_OLD_FAIL', 'cron', 'failed',
                                      error='boom')
        fresh_ok = self.Log._record('LG_FRESH_OK', 'cron', 'success')
        self._backdate(old_ok + old_skip + old_failed, 200)

        self.Log._cron_vacuum_logs()

        self.assertFalse(old_ok.exists())
        self.assertFalse(old_skip.exists())
        # Failed rows stay for triage.
        self.assertTrue(old_failed.exists())
        self.assertTrue(fresh_ok.exists())

    def test_vacuum_retention_zero_disables(self):
        old_ok = self.Log._record('LG_Z', 'cron', 'success')
        self._backdate(old_ok, 200)
        self.env['ir.config_parameter'].sudo().set_param(
            'meta_lead_ads.sync_log_retention_days', '0')
        self.Log._cron_vacuum_logs()
        self.assertTrue(old_ok.exists())

    def test_default_retention_is_14_days(self):
        """Retention defaults to 14 days when no parameter is set."""
        self.env['ir.config_parameter'].sudo().search(
            [('key', '=', 'meta_lead_ads.sync_log_retention_days')]).unlink()
        self.assertEqual(self.Log._sync_log_retention_days(), 14)

    def test_vacuum_prunes_at_14_day_default(self):
        """A 20-day-old success row is pruned under the default; a failed one is kept."""
        self.env['ir.config_parameter'].sudo().search(
            [('key', '=', 'meta_lead_ads.sync_log_retention_days')]).unlink()
        aged_ok = self.Log._record('LG_20D_OK', 'cron', 'success')
        aged_failed = self.Log._record('LG_20D_FAIL', 'cron', 'failed',
                                       error='boom')
        self._backdate(aged_ok + aged_failed, 20)
        self.Log._cron_vacuum_logs()
        self.assertFalse(aged_ok.exists())
        self.assertTrue(aged_failed.exists())


@tagged('post_install', '-at_install')
class TestWebhookAppSecretSelection(RegressionFixtureMixin, TransactionCase):
    """An empty-secret lowest-id account does not shadow a configured one."""

    def test_empty_first_account_is_skipped(self):
        # Blank the lowest-id account's secret and add a later one that has it.
        self.account.app_secret = False
        second = self.env['meta.account'].create({
            'name': 'Acct2', 'account_id': 'ACC2',
            'app_id': 'app2', 'app_secret': 'secret_two',
            'access_token': 'tok2',
        })
        self.assertGreater(second.id, self.account.id)
        self.assertEqual(
            self.env['meta.account']._webhook_app_secret(), 'secret_two')


@tagged('post_install', '-at_install')
class TestUtmFromResolvedCampaign(RegressionFixtureMixin, TransactionCase):
    """utm.campaign uses the resolved name when the payload has only the id."""

    def _fake_lead(self):
        return {
            'id': 'LG1', 'created_time': '2026-06-14T10:00:00+0000',
            'field_data': [{'name': 'email', 'values': ['z@example.com']}],
            'campaign_id': 'C1',
            'campaign_name': False,       # id present, name missing
            'adset_id': 'A1', 'adset_name': 'Set',
            'ad_id': 'AD1', 'ad_name': 'Creative',
            'form_id': 'F1', 'platform': 'fb',
        }

    def test_campaign_resolved_name_creates_utm_campaign(self):
        fetch = mock.Mock(return_value=self._fake_lead())
        resolve = mock.Mock(return_value='Resolved Champ')
        with mock.patch.multiple(self.ClientClass,
                                 fetch_lead=fetch, resolve_name=resolve):
            lead = self.Ingest.ingest_leadgen(self.page, 'LG1', 'manual')
        self.assertEqual(self.env['utm.campaign'].search_count(
            [('name', '=ilike', 'Resolved Champ')]), 1)
        self.assertEqual(lead.campaign_id.name, 'Resolved Champ')


@tagged('post_install', '-at_install')
class TestFormIdValidation(RegressionFixtureMixin, TransactionCase):
    """A malformed Form ID is rejected on save, so it can't break the backfill later."""

    def test_rejects_malformed_form_id_on_create(self):
        # Each value has a character the Graph path guard would refuse.
        for bad in ('123/leads', 'a b', '..', 'id?x=1', 'id%2fx',
                    'http://x', 'a#b'):
            with self.assertRaises(ValidationError):
                with self.env.cr.savepoint():
                    self.Form.create({'name': 'Bad', 'form_id': bad,
                                      'page_id': self.page.id})

    def test_rejects_malformed_form_id_on_write(self):
        with self.assertRaises(ValidationError):
            self.form.form_id = 'bad/id'
            self.form.flush_recordset()

    def test_accepts_valid_form_id(self):
        ok = self.Form.create({'name': 'OK', 'form_id': '1234567890_AB-cd',
                               'page_id': self.page.id})
        self.assertTrue(ok.exists())
