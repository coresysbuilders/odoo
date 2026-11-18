# Meta Lead Ads Integration for Odoo — © 2026 CoreSys Builders. All rights reserved.
# Licensed under the Odoo Proprietary License v1.0 (OPL-1).
# Unauthorized copying, redistribution, or resale of this software, in whole or in
# part, via any medium, is strictly prohibited and constitutes a license violation.
# OPL-1: https://www.odoo.com/documentation/20.0/legal/licenses.html#odoo-apps

"""Regression tests for attribution, dedup and log retention fixes.

Covers: backfill requesting the full attribution field set, the stored
normalized-phone column used by dedup, sync log vacuuming, retention of the
webhook queue and of failed payloads, webhook app secret selection, and
building utm.campaign from the resolved campaign name.
"""
import json
import logging
from unittest import mock

from odoo.tests.common import TransactionCase, new_test_user, tagged
from odoo.exceptions import AccessError, ValidationError

from odoo.addons.meta_lead_ads.models.const import (
    DEFAULT_LEAD_NAME, FAILED_RETENTION_PARAM, LEAD_NAME_PARAM,
    QUEUE_DONE_RETENTION_PARAM, RETENTION_MAX_DAYS, SYNC_LOG_RETENTION_PARAM,
)


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

        def _capture(token, path, params=None, app_secret=None, status=None, **kw):
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

    def test_vacuum_retention_zero_falls_back_to_default(self):
        """A stored 0 no longer keeps rows forever: the default applies and
        the server log names the parameter."""
        old_ok = self.Log._record('LG_Z', 'cron', 'success')
        self._backdate(old_ok, 200)
        self.env['ir.config_parameter'].sudo().set_str(
            'meta_lead_ads.sync_log_retention_days', '0')
        with self.assertLogs('odoo.addons.meta_lead_ads.models.meta_sync_log',
                             'WARNING') as logs:
            self.Log._cron_vacuum_logs()
        self.assertFalse(old_ok.exists())
        self.assertTrue(any('meta_lead_ads.sync_log_retention_days' in line
                            for line in logs.output))

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

    def test_overlapping_vacuum_leaves_without_deleting(self):
        """A cleanup that can't take the lock does nothing and says so."""
        old_ok = self.Log._record('LG_LOCKED_OK', 'cron', 'success')
        self._backdate(old_ok, 200)
        Stats = self.env['meta.sync.stats'].sudo()
        stats_before = Stats.search_count([])
        LogClass = type(self.Log)
        TombstoneClass = type(self.env['meta.leadgen.tombstone'])
        # The level as a number: Odoo gives its level 25 the name INFO too.
        with mock.patch.object(LogClass, '_meta_try_vacuum_lock',
                               return_value=False), \
                mock.patch.object(TombstoneClass, '_vacuum') as tombstones, \
                self.assertLogs('odoo.addons.meta_lead_ads.models.meta_sync_log',
                                logging.INFO) as logs:
            self.Log._cron_vacuum_logs()
        tombstones.assert_not_called()
        self.assertTrue(old_ok.exists())
        self.assertEqual(Stats.search_count([]), stats_before)
        self.assertTrue(any('already running' in line for line in logs.output),
                        logs.output)

    def test_vacuum_lock_is_held_by_one_run_only(self):
        """While another transaction holds the cleanup's lock, it can't be
        taken here, and the cleanup leaves the old rows in place."""
        old_ok = self.Log._record('LG_LOCK_HELD_OK', 'cron', 'success')
        self._backdate(old_ok, 200)
        other = self.registry.cursor()
        try:
            # The other run has to hold the lock first.
            self.assertTrue(self.Log.with_env(
                self.env(cr=other))._meta_try_vacuum_lock())
            self.assertFalse(self.Log._meta_try_vacuum_lock())
            self.Log._cron_vacuum_logs()
            self.assertTrue(old_ok.exists())
        finally:
            other.rollback()
            other.close()
        self.assertTrue(self.Log._meta_try_vacuum_lock())
        self.Log._cron_vacuum_logs()
        self.assertFalse(old_ok.exists())


@tagged('post_install', '-at_install')
class TestSyncStatsRollup(RegressionFixtureMixin, TransactionCase):
    """The cleanup keeps the counts of the rows it deletes, once each."""

    def _backdate(self, records, days):
        # Same as the vacuum tests: create_date can only be aged with SQL.
        self.env.cr.execute(
            "UPDATE meta_sync_log "
            "SET create_date = (now() AT TIME ZONE 'UTC') - %s * interval '1 day' "
            "WHERE id IN %s",
            (days, tuple(records.ids)))
        records.invalidate_recordset(['create_date'])

    def _totals(self):
        totals = {}
        for row in self.env['meta.sync.stats'].sudo().search([]):
            key = (row.day, row.company_id.id or False, row.trigger,
                   row.status)
            totals[key] = totals.get(key, 0) + row.row_count
        return totals

    def _added(self, before):
        after = self._totals()
        return {key: count - before.get(key, 0)
                for key, count in after.items()
                if count != before.get(key, 0)}

    def _key(self, row):
        return (row.create_date.date(), row.company_id.id or False,
                row.trigger, row.status)

    def test_deleted_rows_are_counted_by_day_company_trigger_status(self):
        oks = self.Log.browse()
        for n in range(3):
            oks |= self.Log._record('LG_ROLL_OK_%s' % n, 'webhook', 'success',
                                    page=self.page)
        skip = self.Log._record('LG_ROLL_SKIP', 'cron', 'skipped_idempotent',
                                page=self.page)
        self._backdate(oks, 200)
        self._backdate(skip, 201)
        ok_key, skip_key = self._key(oks[0]), self._key(skip)
        self.assertNotEqual(ok_key[0], skip_key[0])
        self.assertTrue(ok_key[1])
        before = self._totals()

        self.Log._cron_vacuum_logs()

        self.assertFalse(oks.exists())
        self.assertFalse(skip.exists())
        self.assertEqual(self._added(before), {ok_key: 3, skip_key: 1})

    def test_rows_inside_the_retention_are_not_counted(self):
        fresh = self.Log._record('LG_ROLL_FRESH', 'webhook', 'success',
                                 page=self.page)
        before = self._totals()
        self.Log._cron_vacuum_logs()
        self.assertTrue(fresh.exists())
        self.assertEqual(self._added(before), {})

    def test_old_failed_row_is_kept_and_not_counted(self):
        failed = self.Log._record('LG_ROLL_FAIL', 'cron', 'failed',
                                  error='boom', page=self.page)
        self._backdate(failed, 200)
        before = self._totals()
        self.Log._cron_vacuum_logs()
        self.assertTrue(failed.exists())
        self.assertEqual(self._added(before), {})

    def test_removed_skip_is_counted_only_once_its_record_is_gone(self):
        kept = self.Log._record('LG_ROLL_TOMB', 'cron', 'skipped_removed',
                                page=self.page)
        gone = self.Log._record('LG_ROLL_NOTOMB', 'cron', 'skipped_removed',
                                page=self.page)
        self.env['meta.leadgen.tombstone']._remember(['LG_ROLL_TOMB'])
        self._backdate(kept + gone, 200)
        gone_key = self._key(gone)
        before = self._totals()
        self.Log._cron_vacuum_logs()
        # Still recorded as removed, so the skip row stays.
        self.assertTrue(kept.exists())
        self.assertFalse(gone.exists())
        self.assertEqual(self._added(before), {gone_key: 1})

    def test_batches_count_each_row_once(self):
        rows = self.Log.browse()
        for n in range(3):
            rows |= self.Log._record('LG_ROLL_B_%s' % n, 'cron', 'success',
                                     page=self.page)
        self._backdate(rows, 200)
        key = self._key(rows[0])
        before = self._totals()

        self.Log._cron_vacuum_logs(batch=2)
        self.assertEqual(len(rows.exists()), 1)
        self.assertEqual(self._added(before), {key: 2})

        self.Log._cron_vacuum_logs(batch=2)
        self.assertFalse(rows.exists())
        self.assertEqual(self._added(before), {key: 3})

        # Nothing left to delete, so nothing more is counted.
        self.Log._cron_vacuum_logs(batch=2)
        self.assertEqual(self._added(before), {key: 3})

    def test_blank_company_row_is_counted_without_company(self):
        blank = self.Log._record('LG_ROLL_BLANK', 'manual', 'success')
        self._backdate(blank, 200)
        self.assertFalse(blank.company_id)
        key = self._key(blank)
        before = self._totals()
        self.Log._cron_vacuum_logs()
        self.assertFalse(blank.exists())
        self.assertEqual(self._added(before), {key: 1})
        self.assertFalse(key[1])

    def test_table_holds_counts_only(self):
        self.env.cr.execute("""
            SELECT column_name FROM information_schema.columns
             WHERE table_name = 'meta_sync_stats'
        """)
        self.assertEqual(
            {row[0] for row in self.env.cr.fetchall()},
            {'id', 'day', 'company_id', 'trigger', 'status', 'row_count'})

    def test_erasing_a_failed_row_deletes_nothing(self):
        admin = new_test_user(
            self.env, login='rollup_erase_admin',
            groups='base.group_user,meta_lead_ads.group_meta_admin')
        failed = self.Log.sudo().create({
            'meta_leadgen_id': 'LG_ROLL_ERASE', 'trigger': 'cron',
            'status': 'failed', 'page_id': self.page.id,
            'raw_payload': json.dumps({'id': 'LG_ROLL_ERASE', 'field_data': [
                {'name': 'email', 'values': ['erase@example.com']}]}),
        })
        self.assertEqual(failed.company_id, admin.company_id)
        all_rows = self.Log.sudo().search_count([])
        before = self._totals()

        failed.with_user(admin).action_erase_personal_data()

        self.assertTrue(failed.exists())
        self.assertEqual(failed.status, 'skipped_removed')
        self.assertEqual(self.Log.sudo().search_count([]), all_rows)
        self.assertEqual(self._added(before), {})
        # The erased lead stays recorded as removed, so the cleanup keeps
        # its skip row and counts nothing for it either.
        self._backdate(failed, 200)
        self.Log._cron_vacuum_logs()
        self.assertTrue(failed.exists())
        self.assertEqual(self._added(before), {})


@tagged('post_install', '-at_install')
class TestRetention(RegressionFixtureMixin, TransactionCase):
    """Retention settings, the queue and payload cleanup, and the payload
    marker."""

    _KEYS = (SYNC_LOG_RETENTION_PARAM, QUEUE_DONE_RETENTION_PARAM,
             FAILED_RETENTION_PARAM)
    _FIELDS = ('meta_sync_log_retention_days',
               'meta_queue_done_retention_days',
               'meta_failed_retention_days')
    _LOGGER = 'odoo.addons.meta_lead_ads.models.meta_sync_log'

    def setUp(self):
        super().setUp()
        self.ICP = self.env['ir.config_parameter'].sudo()
        self.Event = self.env['meta.webhook.event']
        # An Access Rights user who is also a Meta admin: execute() runs the
        # full save, so set_values saves the Meta values and then the other
        # apps' settings, as it does for any Administration user.
        self.meta_admin = self.env['res.users'].create({
            'name': 'Retention Admin', 'login': 'retention_admin',
            'group_ids': [(6, 0, [
                self.env.ref('base.group_user').id,
                self.env.ref('base.group_erp_manager').id,
                self.env.ref('meta_lead_ads.group_meta_admin').id])]})

    def _clear_params(self):
        self.ICP.search([('key', 'in', self._KEYS)]).unlink()

    def _stored(self):
        return [self.ICP.get_str(key) for key in self._KEYS]

    def _settings(self, vals):
        return self.env['res.config.settings'].with_user(
            self.meta_admin).create(vals)

    # ---- reader -------------------------------------------------------------

    def test_defaults_without_params(self):
        self._clear_params()
        self.assertEqual([self.Log._retention_days(key) for key in self._KEYS],
                         [14, 14, 90])

    def test_bad_values_fall_back_with_warning(self):
        for key, default in zip(self._KEYS, (14, 14, 90)):
            for raw in ('-3', 'abc', '0'):
                with self.subTest(key=key, raw=raw):
                    self.ICP.set_str(key, raw)
                    with self.assertLogs(self._LOGGER, 'WARNING') as logs:
                        self.assertEqual(self.Log._retention_days(key),
                                         default)
                    self.assertTrue(any(key in line for line in logs.output))

    def test_valid_value_used(self):
        self.ICP.set_str(FAILED_RETENTION_PARAM, '30')
        self.assertEqual(self.Log._retention_days(FAILED_RETENTION_PARAM), 30)
        self.ICP.set_str(FAILED_RETENTION_PARAM, str(RETENTION_MAX_DAYS))
        self.assertEqual(self.Log._retention_days(FAILED_RETENTION_PARAM),
                         RETENTION_MAX_DAYS)

    def test_too_long_values_fall_back_with_warning(self):
        for key, default in zip(self._KEYS, (14, 14, 90)):
            for raw in (str(RETENTION_MAX_DAYS + 1), '740000', '2000000000'):
                with self.subTest(key=key, raw=raw):
                    self.ICP.set_str(key, raw)
                    with self.assertLogs(self._LOGGER, 'WARNING') as logs:
                        self.assertEqual(self.Log._retention_days(key),
                                         default)
                    self.assertTrue(any(key in line for line in logs.output))

    def test_cleanup_runs_with_a_huge_stored_value(self):
        """A value too far out for a date used to stop the whole cleanup.
        Every step now runs, on the defaults."""
        for key in self._KEYS:
            self.ICP.set_str(key, '2000000000')
        done_old = self._event('LG_Q_HUGE_D', 'done', 15)
        failed_old = self._event('LG_Q_HUGE_F', 'failed', 91)
        payload_old = self._failed_log('LG_X_HUGE', 91, page=self.page)
        with self.assertLogs(self._LOGGER, 'WARNING'):
            self.Log._cron_vacuum_logs()
        self.assertFalse(done_old.exists())
        self.assertFalse(failed_old.exists())
        self.assertFalse(payload_old.raw_payload)
        self.assertEqual(payload_old.payload_cleared, 'expired')

    # ---- settings -----------------------------------------------------------

    def test_settings_refuse_below_one(self):
        self.ICP.set_str(SYNC_LOG_RETENTION_PARAM, '20')
        self.ICP.set_str(QUEUE_DONE_RETENTION_PARAM, '21')
        self.ICP.set_str(FAILED_RETENTION_PARAM, '95')
        before = self._stored()
        for field_name in self._FIELDS:
            for value in (0, -1):
                with self.subTest(field=field_name, value=value):
                    settings = self._settings({field_name: value})
                    with self.assertRaises(ValidationError):
                        settings.execute()
                    self.assertEqual(self._stored(), before)

    def test_settings_refuse_above_maximum(self):
        self.ICP.set_str(SYNC_LOG_RETENTION_PARAM, '20')
        self.ICP.set_str(QUEUE_DONE_RETENTION_PARAM, '21')
        self.ICP.set_str(FAILED_RETENTION_PARAM, '95')
        before = self._stored()
        for field_name in self._FIELDS:
            for value in (RETENTION_MAX_DAYS + 1, 40000, 2000000000):
                with self.subTest(field=field_name, value=value):
                    settings = self._settings({field_name: value})
                    with self.assertRaisesRegex(
                            ValidationError, 'between 1 and %s days'
                            % RETENTION_MAX_DAYS):
                        settings.execute()
                    self.assertEqual(self._stored(), before)
        # The maximum itself is kept.
        self._settings(dict.fromkeys(self._FIELDS,
                                     RETENTION_MAX_DAYS)).execute()
        self.assertEqual(self._stored(), [str(RETENTION_MAX_DAYS)] * 3)

    def test_settings_save_and_read_back(self):
        self._settings(dict(zip(self._FIELDS, (7, 10, 60)))).execute()
        self.assertEqual(self._stored(), ['7', '10', '60'])
        # The form reads its values through default_get.
        values = self.env['res.config.settings'].with_user(
            self.meta_admin).default_get(list(self._FIELDS))
        self.assertEqual([values[f] for f in self._FIELDS], [7, 10, 60])

    def test_settings_show_effective_value_of_bad_param(self):
        self.ICP.set_str(QUEUE_DONE_RETENTION_PARAM, '0')
        settings = self._settings({})
        self.assertEqual(settings.meta_queue_done_retention_days, 14)

    def test_partial_settings_record_keeps_retention(self):
        """A settings record created without the retention fields (another
        module, a direct RPC call) reads the values in use and saves them
        back unchanged."""
        template = self.ICP.get_str(LEAD_NAME_PARAM) or DEFAULT_LEAD_NAME
        # No params stored: the defaults are read and kept.
        self._clear_params()
        for vals in ({}, {'lead_name_template': template}):
            with self.subTest(vals=vals):
                settings = self._settings(vals)
                self.assertEqual([settings[f] for f in self._FIELDS],
                                 [14, 14, 90])
                settings.execute()
                self.assertEqual(
                    [self.Log._retention_days(k) for k in self._KEYS],
                    [14, 14, 90])
        # Stored values: read as they are and left as they are.
        for key, value in zip(self._KEYS, ('21', '30', '120')):
            self.ICP.set_str(key, value)
        for vals in ({}, {'lead_name_template': template}):
            with self.subTest(vals=vals, stored=True):
                settings = self._settings(vals)
                self.assertEqual([settings[f] for f in self._FIELDS],
                                 [21, 30, 120])
                settings.execute()
                self.assertEqual(self._stored(), ['21', '30', '120'])

    # ---- payload marker -----------------------------------------------------

    def test_payload_marker_only_set_by_system(self):
        row = self.Log._record('LG_MARK', 'cron', 'failed', error='boom')
        event = self.Event.create({'leadgen_id': 'LG_MARK', 'status': 'done'})
        for record in (row, event):
            with self.subTest(model=record._name):
                with self.assertRaisesRegex(AccessError, 'payload marker'):
                    record.with_user(self.meta_admin).write(
                        {'payload_cleared': 'erased'})
                record.sudo().write({'payload_cleared': 'erased'})
                self.assertEqual(record.payload_cleared, 'erased')
        with self.assertRaisesRegex(AccessError, 'payload marker'):
            self.Log.with_user(self.meta_admin).create({
                'meta_leadgen_id': 'LG_MARK2', 'status': 'failed',
                'payload_cleared': 'expired'})
        with self.assertRaisesRegex(AccessError, 'payload marker'):
            self.Event.with_user(self.meta_admin).create({
                'leadgen_id': 'LG_MARK2', 'payload_cleared': 'expired'})

    # ---- daily cleanup ------------------------------------------------------

    def _age(self, records, column, days):
        # ORM timestamps can't be written, so age them with SQL. Flush first
        # so a pending write can't put the date back.
        records.flush_recordset()
        self.env.cr.execute(
            "UPDATE " + records._table + " SET " + column
            + " = (now() AT TIME ZONE 'UTC') - %s * interval '1 day' "
            "WHERE id IN %s", (days, tuple(records.ids)))
        records.invalidate_recordset()

    def _event(self, leadgen_id, status, days):
        event = self.Event.create({'leadgen_id': leadgen_id,
                                   'status': status})
        self._age(event, 'write_date', days)
        return event

    def _failed_log(self, leadgen_id, days, **kw):
        row = self.Log._record(leadgen_id, 'webhook', 'failed', error='boom',
                               raw={'id': leadgen_id, 'field_data': []}, **kw)
        self._age(row, 'create_date', days)
        return row

    def test_queue_default_windows(self):
        self._clear_params()
        done_old = self._event('LG_Q_D15', 'done', 15)
        done_new = self._event('LG_Q_D13', 'done', 13)
        failed_old = self._event('LG_Q_F91', 'failed', 91)
        failed_new = self._event('LG_Q_F89', 'failed', 89)
        pending = self._event('LG_Q_P400', 'pending', 400)
        self.Log._cron_vacuum_logs()
        self.assertFalse(done_old.exists())
        self.assertTrue(done_new.exists())
        self.assertFalse(failed_old.exists())
        self.assertTrue(failed_new.exists())
        self.assertTrue(pending.exists())

    def test_queue_custom_windows(self):
        self.ICP.set_str(QUEUE_DONE_RETENTION_PARAM, '3')
        self.ICP.set_str(FAILED_RETENTION_PARAM, '5')
        done_old = self._event('LG_Q_D4', 'done', 4)
        done_new = self._event('LG_Q_D2', 'done', 2)
        failed_old = self._event('LG_Q_F6', 'failed', 6)
        failed_new = self._event('LG_Q_F4', 'failed', 4)
        pending = self._event('LG_Q_P400', 'pending', 400)
        self.Log._cron_vacuum_logs()
        self.assertFalse(done_old.exists())
        self.assertTrue(done_new.exists())
        self.assertFalse(failed_old.exists())
        self.assertTrue(failed_new.exists())
        self.assertTrue(pending.exists())

    def test_failed_payload_expires(self):
        self._clear_params()
        old = self._failed_log('LG_X91', 91, page=self.page)
        recent = self._failed_log('LG_X89', 89, page=self.page)
        # No page and no lead: a row that belongs to no company.
        blank = self._failed_log('LG_XBLANK', 91)
        success = self.Log._record('LG_XOK', 'cron', 'success')
        self._age(success, 'create_date', 91)
        self.Log._cron_vacuum_logs()

        self.assertTrue(old.exists())
        self.assertFalse(old.sudo().raw_payload)
        self.assertEqual(old.payload_cleared, 'expired')
        self.assertEqual(old.status, 'failed')
        self.assertTrue(recent.sudo().raw_payload)
        self.assertFalse(recent.payload_cleared)
        self.assertFalse(blank.company_id)
        self.assertTrue(blank.exists())
        self.assertFalse(blank.sudo().raw_payload)
        self.assertEqual(blank.payload_cleared, 'expired')
        # The sync log window still applies to success rows.
        self.assertFalse(success.exists())

    def test_vacuum_batch_limit(self):
        events = (self._event('LG_B1', 'done', 30)
                  | self._event('LG_B2', 'done', 30)
                  | self._event('LG_B3', 'done', 30))
        self.Log._cron_vacuum_logs(batch=2)
        self.assertEqual(len(events.exists()), 1)
        self.Log._cron_vacuum_logs(batch=2)
        self.assertFalse(events.exists())

    def test_expired_row_still_retryable(self):
        lead = self.Lead.create({'name': 'Expired', 'type': 'lead',
                                 'meta_leadgen_id': 'LG_XR',
                                 'meta_page_id_ref': self.page.id})
        row = self._failed_log('LG_XR', 91, lead=lead)
        self.Log._cron_vacuum_logs()
        self.assertEqual(row.payload_cleared, 'expired')

        def _ingest(self_ingest, page, leadgen_id, trigger='webhook',
                    raw=None):
            self_ingest.env['meta.sync.log']._record(
                leadgen_id, 'manual', 'success', lead=lead, raw=raw)
            return lead

        with mock.patch.object(self.IngestClass, 'ingest_leadgen',
                               autospec=True, side_effect=_ingest) as m:
            row.action_retry()
        # With the payload gone the retry reads the lead from Meta again.
        self.assertEqual(m.call_count, 1)
        self.assertEqual(m.call_args.args[1], self.page)
        self.assertEqual(m.call_args.args[2], 'LG_XR')
        self.assertIsNone(m.call_args.kwargs.get('raw'))
        row.invalidate_recordset()
        self.assertEqual(row.status, 'success')

    def test_retention_indexes_exist(self):
        cr = self.env.cr
        for name, table, predicates in (
                ('meta_webhook_event_retention_idx', 'meta_webhook_event',
                 ("'done'", "'failed'")),
                ('meta_sync_log_failed_payload_idx', 'meta_sync_log',
                 ("'failed'", 'raw_payload IS NOT NULL'))):
            with self.subTest(index=name):
                cr.execute("SELECT tablename, pg_get_indexdef(indexname::regclass) "
                           "FROM pg_indexes WHERE indexname = %s", (name,))
                row = cr.fetchone()
                self.assertTrue(row, name)
                self.assertEqual(row[0], table)
                self.assertIn('WHERE', row[1])
                for predicate in predicates:
                    self.assertIn(predicate, row[1])


@tagged('post_install', '-at_install')
class TestWebhookAppSecretSelection(RegressionFixtureMixin, TransactionCase):
    """An empty-secret lowest-id account does not shadow a configured one."""

    def test_empty_first_account_is_skipped(self):
        # Blank the lowest-id account's secret and add a later one that has
        # it. The new secret is saved on every account, the blank one too.
        self.account.app_secret = False
        second = self.env['meta.account'].create({
            'name': 'Acct2', 'account_id': 'ACC2',
            'app_id': 'app_test', 'app_secret': 'secret_two',
            'access_token': 'tok2',
        })
        self.assertGreater(second.id, self.account.id)
        self.account.invalidate_recordset()
        self.assertEqual(self.account.app_secret, 'secret_two')
        self.assertEqual(second.app_secret, 'secret_two')
        self.assertEqual(
            self.env['meta.account']._webhook_app_secret(), 'secret_two')

    def test_blank_secret_is_not_propagated(self):
        second = self.env['meta.account'].create({
            'name': 'Acct2', 'account_id': 'ACC2',
            'app_id': 'app_test', 'app_secret': 'secret_test',
            'access_token': 'tok2',
        })
        self.account.write({'app_secret': False})
        second.invalidate_recordset()
        self.assertEqual(second.app_secret, 'secret_test')
        self.assertEqual(
            self.env['meta.account']._webhook_app_secret(), 'secret_test')


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
