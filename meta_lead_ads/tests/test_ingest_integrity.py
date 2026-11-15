# Meta Lead Ads Integration for Odoo — © 2026 CoreSys Builders. All rights reserved.
# Licensed under the Odoo Proprietary License v1.0 (OPL-1).
# Unauthorized copying, redistribution, or resale of this software, in whole or in
# part, via any medium, is strictly prohibited and constitutes a license violation.
# OPL-1: https://www.odoo.com/documentation/19.0/legal/licenses.html#odoo-apps

# Regression tests for lost races, attribution lookups that fail, and exact
# UTM campaign lookup.
import logging
import time
import uuid
from datetime import datetime, timedelta
from unittest import mock

from psycopg2 import IntegrityError
from psycopg2.errors import SerializationFailure

from odoo import Command, api, fields
from odoo.api import SUPERUSER_ID
from odoo.exceptions import AccessError, UserError
from odoo.modules.registry import Registry
from odoo.tests.common import (BaseCase, TransactionCase, get_db_name,
                               new_test_user, tagged)
from odoo.tools import mute_logger

from odoo.addons.meta_lead_ads.models.const import (
    ATTRIBUTION_RETRY_CAP, ATTRIBUTION_TRANSIENT_BACKOFF_HOURS,
    LEAD_NAME_PARAM, MAPPING_FORBIDDEN_FIELDS,
    NOTIFY_ENABLED_PARAM, NOTIFY_TARGET_PARAM, NOTIFY_USER_PARAM,
    RATE_LIMIT_FALLBACK_MINUTES,
)
from odoo.addons.meta_lead_ads.models.exceptions import (
    MetaAuthError, MetaIngestRaceError, MetaPermanentError,
    MetaRateLimitError, MetaTransientError,
)
from odoo.addons.meta_lead_ads.models.meta_lead_ingest import (
    _is_leadgen_conflict,
)

from .test_ingest import IngestFixtureMixin
from .test_multi_company import MultiCompanyFixtureMixin

_logger = logging.getLogger(__name__)

_ALERT_SUBJECT = "New Meta lead"


@tagged('post_install', '-at_install')
class TestExactCampaign(IngestFixtureMixin, TransactionCase):
    """Campaign names are matched literally, and a raised attempt leaves nothing."""

    def setUp(self):
        super().setUp()
        self.Campaign = self.env['utm.campaign']

    def _campaign(self, name, active=True):
        rec = self.Campaign.create({'name': name})
        if not active:
            rec.active = False
        rec.flush_recordset()
        return rec

    def _ingest(self, leadgen_id, campaign_name):
        with self._patch_graph(
                fetch_lead=self._fake_lead(id=leadgen_id,
                                           campaign_name=campaign_name)):
            return self.Ingest.ingest_leadgen(self.page, leadgen_id, 'manual')

    def _no_campaign_found(self):
        return mock.patch.object(type(self.Ingest), '_find_campaign',
                                 return_value=self.Campaign.browse())

    def _enable_notify(self):
        all_leads = self.env.ref('sales_team.group_sale_salesman_all_leads')
        user = self.env['res.users'].create({
            'name': 'Race Reader', 'login': 'race_reader',
            'group_ids': [(4, all_leads.id)],
        })
        ICP = self.env['ir.config_parameter'].sudo()
        ICP.set_param(NOTIFY_ENABLED_PARAM, '1')
        ICP.set_param(NOTIFY_TARGET_PARAM, 'user')
        ICP.set_param(NOTIFY_USER_PARAM, str(user.id))
        return user

    def _alerts(self):
        return self.env['mail.notification'].search([
            ('mail_message_id.model', '=', 'crm.lead'),
            ('mail_message_id.subject', '=', _ALERT_SUBJECT),
            ('notification_type', '=', 'inbox'),
        ])

    def _success_logs(self, leadgen_id):
        return self.env['meta.sync.log'].search([
            ('meta_leadgen_id', '=', leadgen_id),
            ('status', '=', 'success'),
        ])

    # ---- wildcards -------------------------------------------------------

    def test_underscore_is_literal(self):
        dash = self._campaign('Summer-2026')
        letter = self._campaign('SummerX2026')
        lead = self._ingest('LGE1', 'Summer_2026')
        self.assertEqual(lead.campaign_id.name, 'Summer_2026')
        self.assertNotIn(lead.campaign_id, dash | letter)

    def test_percent_is_literal(self):
        other = self._campaign('50 Percent Off')
        lead = self._ingest('LGE2', '50% Off')
        self.assertEqual(lead.campaign_id.name, '50% Off')
        self.assertNotEqual(lead.campaign_id, other)

    def test_backslash_is_literal(self):
        plain = self._campaign('Promo1')
        lead = self._ingest('LGE3', 'Promo\\1')
        self.assertEqual(lead.campaign_id.name, 'Promo\\1')
        self.assertNotEqual(lead.campaign_id, plain)
        again = self._ingest('LGE4', 'Promo\\1')
        self.assertEqual(again.campaign_id, lead.campaign_id)

    # ---- case and accents ------------------------------------------------

    def test_exact_case_preferred(self):
        self._campaign('SUMMER')
        exact = self._campaign('Summer')
        lead = self._ingest('LGE5', 'Summer')
        self.assertEqual(lead.campaign_id, exact)

    def test_case_insensitive_reuse(self):
        existing = self._campaign('Summer')
        lead = self._ingest('LGE6', 'summer')
        self.assertEqual(lead.campaign_id, existing)
        self.assertEqual(self.Campaign.with_context(active_test=False)
                         .search_count([('name', 'in', ('summer', 'Summer'))]),
                         1)

    def test_accent_not_folded(self):
        accented = self._campaign('Café')
        lead = self._ingest('LGE7', 'Cafe')
        self.assertEqual(lead.campaign_id.name, 'Cafe')
        self.assertNotEqual(lead.campaign_id, accented)

    # ---- archived campaigns and races ------------------------------------

    def test_archived_exact_reused(self):
        winter = self._campaign('Winter', active=False)
        with mute_logger('odoo.sql_db'):
            lead = self._ingest('LGE8', 'Winter')
        self.assertTrue(lead)
        self.assertEqual(lead.campaign_id, winter)
        self.assertFalse(winter.active)

    def test_invisible_campaign_raises_race(self):
        self.assertTrue(issubclass(MetaIngestRaceError, MetaTransientError))
        self._campaign('Ghost', active=False)
        with self._no_campaign_found(), mute_logger('odoo.sql_db'), \
                self.assertRaises(MetaIngestRaceError):
            self.Ingest._get_or_create_campaign('Ghost')

    def test_other_integrity_error_not_hidden(self):
        """Only the name UNIQUE conflict counts as a race; other errors surface."""
        other = self._campaign('Other')
        CampaignCls = type(self.Campaign)
        original = CampaignCls.create

        def create_with_bad_insert(recs, vals_list):
            # A NOT NULL violation inside the create, unrelated to the name.
            recs.env.cr.execute(
                "INSERT INTO meta_name_cache (object_type, graph_id) "
                "VALUES (NULL, 'x')")
            return original(recs, vals_list)

        finder = mock.Mock(side_effect=[self.Campaign.browse(), other])
        with mock.patch.object(type(self.Ingest), '_find_campaign', finder), \
                mock.patch.object(CampaignCls, 'create',
                                  create_with_bad_insert), \
                mute_logger('odoo.sql_db'), \
                self.assertRaises(IntegrityError):
            # A race error or a returned 'Other' would both fail here.
            self.Ingest._get_or_create_campaign('Fresh')

    # ---- the whole attempt rolls back ------------------------------------

    def test_campaign_race_rolls_back_whole_attempt(self):
        ghost = self._campaign('Ghost', active=False)
        reader = self._enable_notify()
        answers_before = self.env['meta.lead.answer'].search_count([])
        alerts_before = self._alerts()
        with self._no_campaign_found(), mute_logger('odoo.sql_db'), \
                self.assertRaises(MetaIngestRaceError):
            self._ingest('LGE10', 'Ghost')
        self.env.invalidate_all()
        self.assertEqual(self.Lead.with_context(active_test=False).search_count(
            [('meta_leadgen_id', '=', 'LGE10')]), 0)
        self.assertEqual(
            self.env['meta.lead.answer'].search_count([]), answers_before)
        self.assertFalse(self._success_logs('LGE10'))
        self.assertEqual(self._alerts(), alerts_before)

        lead = self._ingest('LGE10', 'Ghost')
        self.assertEqual(len(lead), 1)
        self.assertEqual(lead.campaign_id, ghost)
        self.assertTrue(lead.source_id)
        self.assertTrue(lead.medium_id)
        logs = self._success_logs('LGE10')
        self.assertEqual(len(logs), 1)
        self.assertEqual(logs.lead_id, lead)
        alerts = self._alerts().filtered(
            lambda n: n.mail_message_id.res_id == lead.id)
        self.assertEqual(len(alerts), 1)
        self.assertEqual(alerts.res_partner_id, reader.partner_id)

    def test_campaign_race_on_match_rolls_back(self):
        ghost = self._campaign('Ghost', active=False)
        blank = self.Lead.create({
            'name': 'Existing', 'email_from': 'Jane@Example.com',
            'type': 'lead', 'description': 'orig'})
        blank.flush_recordset()
        orig_description = blank.description
        with self._no_campaign_found(), mute_logger('odoo.sql_db'), \
                self.assertRaises(MetaIngestRaceError):
            self._ingest('LGE11', 'Ghost')
        self.env.invalidate_all()
        self.assertFalse(blank.meta_leadgen_id)
        self.assertEqual(blank.description, orig_description)
        self.assertFalse(blank.answer_ids)

        lead = self._ingest('LGE11', 'Ghost')
        self.assertEqual(lead, blank)
        self.assertEqual(blank.meta_leadgen_id, 'LGE11')
        self.assertEqual(blank.campaign_id, ghost)

    # ---- a failing alert keeps the lead ----------------------------------

    def _ingest_with_broken_alert(self, leadgen_id, side_effect):
        self._enable_notify()
        alerts_before = self._alerts()
        with mock.patch.object(type(self.Ingest), '_post_inbox_alert',
                               side_effect=side_effect), \
                mute_logger('odoo.sql_db',
                            'odoo.addons.meta_lead_ads.models.meta_lead_ingest'):
            lead = self._ingest(leadgen_id, 'Alert Test')
        self.assertEqual(len(lead), 1)
        self.env.flush_all()
        # Fails if the alert left the transaction aborted.
        self.env.cr.execute("SELECT 1")
        found = self.Lead.sudo().with_context(active_test=False).search(
            [('meta_leadgen_id', '=', leadgen_id)])
        self.assertEqual(found, lead)
        self.assertTrue(found.answer_ids)
        self.assertEqual(len(self._success_logs(leadgen_id)), 1)
        self.assertEqual(self._alerts(), alerts_before)

    def test_notify_db_error_keeps_lead(self):
        def _db_error(*args, **kwargs):
            self.env.cr.execute("SELECT 1/0")
        self._ingest_with_broken_alert('LGN1', _db_error)

    def test_notify_python_error_keeps_lead(self):
        self._ingest_with_broken_alert('LGN2', RuntimeError('alert down'))


@tagged('post_install', '-at_install')
class TestIngestRace(MultiCompanyFixtureMixin, IngestFixtureMixin,
                     TransactionCase):
    """A lost leadgen race returns the real lead or asks for a retry."""

    def _lead(self, leadgen_id, active=True, **vals):
        vals.setdefault('name', 'Winner %s' % leadgen_id)
        lead = self.Lead.create(dict(vals, meta_leadgen_id=leadgen_id))
        if not active:
            lead.active = False
        lead.flush_recordset()
        return lead

    def _ingest(self, leadgen_id, **payload):
        with self._patch_graph(
                fetch_lead=self._fake_lead(id=leadgen_id, **payload)):
            return self.Ingest.ingest_leadgen(self.page, leadgen_id, 'manual')

    def _no_shortcut(self):
        return mock.patch.object(type(self.Ingest), '_find_by_leadgen_id',
                                 return_value=self.Lead.browse())

    def _winner_invisible(self):
        return mock.patch.object(type(self.Ingest), '_find_winner',
                                 side_effect=MetaIngestRaceError('x'))

    def _all_leads(self, leadgen_id):
        return self.Lead.with_context(active_test=False).search(
            [('meta_leadgen_id', '=', leadgen_id)])

    def _done_logs(self, leadgen_id):
        return self.env['meta.sync.log'].search([
            ('meta_leadgen_id', '=', leadgen_id),
            ('status', 'in', ('success', 'skipped_idempotent')),
        ])

    def _raise_and_catch(self, func):
        """Run ``func`` in a savepoint and return the IntegrityError it hit."""
        try:
            with mute_logger('odoo.sql_db'), self.env.cr.savepoint():
                func()
        except IntegrityError as e:
            return e
        self.fail("no IntegrityError raised")

    # ---- shortcut and create path ----------------------------------------

    def test_archived_lead_shortcut(self):
        archived = self._lead('LGA1', active=False)
        fetch = mock.Mock(return_value=self._fake_lead(id='LGA1'))
        with self._patch_graph(fetch_lead=fetch):
            lead = self.Ingest.ingest_leadgen(self.page, 'LGA1', 'manual')
        self.assertEqual(lead.id, archived.id)
        self.assertEqual(fetch.call_count, 0)
        self.assertEqual(len(self._all_leads('LGA1')), 1)

    def test_create_conflict_archived_winner(self):
        archived = self._lead('LGA2', active=False)
        with self._no_shortcut(), mute_logger('odoo.sql_db'):
            lead = self._ingest('LGA2')
        self.assertEqual(lead, archived)
        logs = self._done_logs('LGA2')
        self.assertIn(archived, logs.filtered(
            lambda l: l.status == 'skipped_idempotent').lead_id)
        self.assertFalse(logs.filtered(lambda l: not l.lead_id))
        self.assertEqual(len(self._all_leads('LGA2')), 1)

    def test_create_conflict_invisible_winner_raises(self):
        self._lead('LGA3')
        with self._no_shortcut(), self._winner_invisible(), \
                mute_logger('odoo.sql_db'), \
                self.assertRaises(MetaIngestRaceError):
            self._ingest('LGA3')
        self.env.invalidate_all()
        self.assertFalse(self._done_logs('LGA3'))
        self.assertEqual(len(self._all_leads('LGA3')), 1)

    def test_find_winner_raises_when_absent(self):
        with self.assertRaises(MetaIngestRaceError):
            self.Ingest._find_winner('LGNONE')
        archived = self._lead('LGA4', active=False)
        self.assertEqual(self.Ingest._find_winner('LGA4'), archived)

    def test_ingest_never_returns_empty(self):
        Log = self.env['meta.sync.log'].sudo()
        empty = self.Lead.browse()

        def create_nothing(recs, *args, **kwargs):
            # A success row with no lead, so a rollback of it can be seen.
            Log._record('LGA9', 'manual', 'success', lead=empty,
                        match_key='none')
            return empty

        with mock.patch.object(type(self.Ingest), '_create_lead',
                               create_nothing), \
                self.assertRaises(MetaIngestRaceError):
            self._ingest('LGA9', field_data=[
                {'name': 'full_name', 'values': ['Nobody']}])
        self.env.invalidate_all()
        self.assertFalse(self._all_leads('LGA9'))
        self.assertFalse(self._done_logs('LGA9'))

    def test_non_leadgen_integrity_error_propagates(self):
        LeadCls = type(self.Lead)
        original = LeadCls.create

        def create_with_bad_insert(recs, vals_list):
            # A NOT NULL violation that has nothing to do with the leadgen id.
            recs.env.cr.execute(
                "INSERT INTO meta_name_cache (object_type, graph_id) "
                "VALUES (NULL, 'x')")
            return original(recs, vals_list)

        with mock.patch.object(LeadCls, 'create', create_with_bad_insert), \
                mute_logger('odoo.sql_db'), \
                self.assertRaises(IntegrityError):
            self._ingest('LGA10')
        self.env.invalidate_all()
        self.assertFalse(self.env['meta.sync.log'].search([
            ('meta_leadgen_id', '=', 'LGA10'),
            ('status', '=', 'skipped_idempotent')]))
        self.assertFalse(self._all_leads('LGA10'))

    def test_is_leadgen_conflict(self):
        self._lead('LGA11')
        dup = self._raise_and_catch(lambda: self.Lead.create(
            {'name': 'Dup', 'meta_leadgen_id': 'LGA11'}).flush_recordset())
        self.assertTrue(_is_leadgen_conflict(dup))

        not_null = self._raise_and_catch(lambda: self.env.cr.execute(
            "INSERT INTO meta_name_cache (object_type, graph_id) "
            "VALUES (NULL, 'x')"))
        self.assertFalse(_is_leadgen_conflict(not_null))

        # Copy a campaign row as is, so only the name UNIQUE can object.
        campaign = self.env['utm.campaign'].create({'name': 'Dup Campaign'})
        campaign.flush_recordset()
        cr = self.env.cr
        cr.execute("""
            SELECT column_name FROM information_schema.columns
             WHERE table_name = 'utm_campaign' AND column_name != 'id'
               AND is_nullable = 'NO'""")
        cols = ', '.join('"%s"' % r[0] for r in cr.fetchall())
        campaign_dup = self._raise_and_catch(lambda: cr.execute(
            "INSERT INTO utm_campaign (%s) SELECT %s FROM utm_campaign "
            "WHERE id = %%s" % (cols, cols), (campaign.id,)))
        self.assertEqual(campaign_dup.diag.constraint_name,
                         'utm_campaign_unique_name')
        self.assertFalse(_is_leadgen_conflict(campaign_dup))

    def test_cross_company_winner_untouched(self):
        source = self.env['utm.source'].create({'name': 'Company B source'})
        winner = self._lead('LGA8', active=False, company_id=self.co_b.id,
                            user_id=self.admin_b.id, source_id=source.id,
                            email_from='other@example.com')
        before = (winner.company_id, winner.user_id, winner.source_id,
                  winner.medium_id, winner.campaign_id)
        with self._no_shortcut(), mute_logger('odoo.sql_db'):
            lead = self._ingest('LGA8')
        self.assertEqual(lead, winner)
        self.env.invalidate_all()
        self.assertEqual((winner.company_id, winner.user_id, winner.source_id,
                          winner.medium_id, winner.campaign_id), before)
        self.assertEqual(winner.company_id, self.co_b)
        self.assertFalse(winner.active)
        self.assertEqual(len(self._all_leads('LGA8')), 1)

    # ---- match path --------------------------------------------------------

    def _blank(self, **vals):
        blank = self.Lead.create(dict({
            'name': 'Open', 'email_from': 'Jane@Example.com', 'type': 'lead',
        }, **vals))
        blank.flush_recordset()
        return blank

    def _no_create(self):
        # These tests must be settled on the match, never by a new lead.
        return mock.patch.object(type(self.Ingest), '_create_lead',
                                 side_effect=AssertionError('create path'))

    def test_match_conflict_archived_winner(self):
        winner = self._lead('LGA5', active=False,
                            email_from='other@example.com')
        blank = self._blank(company_id=self.env.company.id)
        with self._no_shortcut(), self._no_create(), \
                mute_logger('odoo.sql_db'):
            lead = self._ingest('LGA5')
        self.assertEqual(lead, winner)
        self.env.invalidate_all()
        self.assertFalse(blank.meta_leadgen_id)
        skipped = self._done_logs('LGA5').filtered(
            lambda l: l.status == 'skipped_idempotent')
        self.assertEqual(skipped.lead_id, winner)
        self.assertEqual(len(self._all_leads('LGA5')), 1)

    def test_match_conflict_invisible_winner_raises(self):
        self._lead('LGA6', email_from='other@example.com')
        blank = self._blank(company_id=self.env.company.id)
        with self._no_shortcut(), self._winner_invisible(), \
                self._no_create(), mute_logger('odoo.sql_db'), \
                self.assertRaises(MetaIngestRaceError):
            self._ingest('LGA6')
        self.env.invalidate_all()
        self.assertFalse(self._done_logs('LGA6'))
        self.assertFalse(blank.meta_leadgen_id)
        self.assertEqual(len(self._all_leads('LGA6')), 1)

    def test_match_serialization_failure_retries(self):
        # The company is already set, so no company stamp is attempted.
        blank = self._blank(company_id=self.env.company.id)
        LeadCls = type(self.Lead)
        original = LeadCls.write

        def write_conflict(recs, vals):
            if 'meta_leadgen_id' in vals:
                raise SerializationFailure('x')
            return original(recs, vals)

        with mock.patch.object(LeadCls, 'write', write_conflict), \
                self._no_create(), self.assertRaises(MetaIngestRaceError):
            self._ingest('LGA7')
        self.env.invalidate_all()
        self.assertFalse(self._done_logs('LGA7'))
        self.assertFalse(blank.meta_leadgen_id)
        self.assertFalse(self._all_leads('LGA7'))


@tagged('post_install', '-at_install')
class TestIngestRaceCallers(MultiCompanyFixtureMixin, IngestFixtureMixin,
                            TransactionCase):
    """Callers retry a lost race and never acknowledge a missing lead."""

    T0 = datetime(2026, 6, 1, 10, 0, 0)

    def setUp(self):
        super().setUp()
        self.Event = self.env['meta.webhook.event']
        self.Log = self.env['meta.sync.log']
        self.IngestCls = type(self.Ingest)
        self.form_b = self.env['meta.lead.form'].create({
            'name': 'Form B', 'form_id': 'FB1', 'page_id': self.page_b.id,
        })

    # ---- helpers -----------------------------------------------------------

    def _patch_ingest(self, **kwargs):
        return mock.patch.object(self.IngestCls, 'ingest_leadgen', **kwargs)

    def _event(self, leadgen_id, page_id='PG1'):
        return self.Event.create({
            'leadgen_id': leadgen_id, 'page_id': page_id,
            'form_id': 'F1', 'status': 'pending'})

    def _failed_row(self, leadgen_id, page):
        return self.Log.create({
            'meta_leadgen_id': leadgen_id, 'status': 'failed',
            'trigger': 'webhook', 'error_message': 'boom',
            'page_id': page.id})

    def _all_leads(self, leadgen_id):
        return self.Lead.with_context(active_test=False).search(
            [('meta_leadgen_id', '=', leadgen_id)])

    def _payload_b(self, leadgen_id, campaign_name):
        return self._fake_lead(id=leadgen_id, form_id='FB1',
                               campaign_name=campaign_name)

    def _ghost(self, name):
        ghost = self.env['utm.campaign'].create({'name': name})
        ghost.active = False
        ghost.flush_recordset()
        return ghost

    def _no_campaign_found(self):
        return mock.patch.object(self.IngestCls, '_find_campaign',
                                 return_value=self.env['utm.campaign'])

    def _notify_b_reader(self):
        # Someone allowed in company B, so the alert has a reader.
        user = new_test_user(
            self.env, 'race_reader_b',
            groups='base.group_user,sales_team.group_sale_salesman_all_leads',
            company_id=self.co_b.id,
            company_ids=[Command.set([self.co_a.id, self.co_b.id])])
        ICP = self.env['ir.config_parameter'].sudo()
        ICP.set_param(NOTIFY_ENABLED_PARAM, '1')
        ICP.set_param(NOTIFY_TARGET_PARAM, 'user')
        ICP.set_param(NOTIFY_USER_PARAM, str(user.id))
        return user

    def _alerts(self, lead):
        return self.env['mail.notification'].search([
            ('mail_message_id.model', '=', 'crm.lead'),
            ('mail_message_id.res_id', '=', lead.id),
            ('mail_message_id.subject', '=', _ALERT_SUBJECT),
            ('notification_type', '=', 'inbox'),
        ])

    def _assert_complete_b_lead(self, lead, leadgen_id, ghost, reader):
        self.assertEqual(len(lead), 1)
        self.assertEqual(lead.meta_leadgen_id, leadgen_id)
        self.assertEqual(lead.company_id, self.co_b)
        self.assertTrue(not lead.user_id
                        or self.co_b in lead.user_id.company_ids)
        self.assertEqual(lead.campaign_id, ghost)
        self.assertTrue(lead.source_id)
        self.assertTrue(lead.medium_id)
        logs = self.Log.search([('meta_leadgen_id', '=', leadgen_id),
                                ('status', '=', 'success')])
        self.assertEqual(len(logs), 1)
        self.assertEqual(logs.lead_id, lead)
        alerts = self._alerts(lead)
        self.assertEqual(len(alerts), 1)
        self.assertEqual(alerts.res_partner_id, reader.partner_id)
        return logs

    # ---- guards ------------------------------------------------------------

    def test_drain_race_keeps_event_pending(self):
        evt = self._event('LGB1')
        with self._patch_ingest(side_effect=MetaIngestRaceError('x')):
            evt._process_one()
        self.assertEqual(evt.status, 'pending')
        self.assertEqual(evt.attempts, 1)
        self.assertFalse(evt.lead_id)

        with self._patch_graph(fetch_lead=self._fake_lead(id='LGB1')):
            evt._process_one()
        self.assertEqual(evt.status, 'done')
        self.assertTrue(evt.lead_id)
        self.assertEqual(evt.lead_id, self._all_leads('LGB1'))

    def test_drain_empty_result_not_done(self):
        evt = self._event('LGB2')
        with self._patch_ingest(return_value=self.Lead.browse()):
            evt._process_one()
        self.assertEqual(evt.status, 'pending')
        self.assertEqual(evt.attempts, 1)
        self.assertFalse(evt.lead_id)

    def test_backfill_race_keeps_cursor(self):
        self.form.last_synced_time = self.T0
        item = self._fake_lead(id='LGB5')
        with mock.patch.object(self.Client, '_iter_paged',
                               return_value=iter([item])), \
                self._patch_ingest(side_effect=MetaIngestRaceError('x')):
            self.form._sweep_form()
        self.form.invalidate_recordset(['last_synced_time'])
        self.assertEqual(self.form.last_synced_time, self.T0)

    def test_sync_retry_race_is_transient(self):
        row = self._failed_row('LGB3', self.page)
        with self._patch_ingest(side_effect=MetaIngestRaceError('x')):
            outcome = row._retry_one()
        self.assertEqual(outcome, 'transient')
        self.assertEqual(row.status, 'failed')
        self.assertFalse(row.lead_id)

    def test_sync_retry_empty_result_not_success(self):
        row = self._failed_row('LGB6', self.page)
        with self._patch_ingest(return_value=self.Lead.browse()):
            outcome = row._retry_one()
        self.assertNotEqual(outcome, 'ok')
        self.assertEqual(row.status, 'failed')
        self.assertFalse(row.lead_id)
        # The reconcile step on its own refuses an empty lead too.
        row._reconcile_after_ingest(self.Lead.browse())
        self.assertEqual(row.status, 'failed')

    def test_wizard_race_user_error(self):
        wizard = self.env['meta.ingest.leadgen'].create({
            'page_id': self.page.id, 'leadgen_id': 'LGB4'})
        with self._patch_ingest(side_effect=MetaIngestRaceError('x')), \
                self.assertRaises(UserError):
            wizard.action_ingest()

    # ---- campaign race, then a retry through each caller -------------------

    def test_drain_campaign_race_completes_on_retry(self):
        ghost = self._ghost('Ghost D')
        reader = self._notify_b_reader()
        evt = self._event('LGB10', page_id='PGB')
        payload = self._payload_b('LGB10', 'Ghost D')
        with self._patch_graph(fetch_lead=payload), \
                self._no_campaign_found(), mute_logger('odoo.sql_db'), \
                self.env.cr.savepoint():
            evt._process_one()
        self.env.invalidate_all()
        self.assertEqual(evt.status, 'pending')
        self.assertEqual(evt.attempts, 1)
        self.assertFalse(self._all_leads('LGB10'))

        with self._patch_graph(fetch_lead=payload):
            evt._process_one()
        self.assertEqual(evt.status, 'done')
        lead = evt.lead_id
        self.assertEqual(lead, self._all_leads('LGB10'))
        self._assert_complete_b_lead(lead, 'LGB10', ghost, reader)

    def test_backfill_campaign_race_completes_on_retry(self):
        ghost = self._ghost('Ghost B')
        reader = self._notify_b_reader()
        self.form_b.last_synced_time = self.T0
        item = self._payload_b('LGB11', 'Ghost B')
        with self._patch_graph(fetch_lead=item), \
                mock.patch.object(self.Client, '_iter_paged',
                                  return_value=iter([item])), \
                self._no_campaign_found(), mute_logger('odoo.sql_db'), \
                self.env.cr.savepoint():
            self.form_b._sweep_form()
        self.env.invalidate_all()
        self.assertFalse(self._all_leads('LGB11'))
        self.assertEqual(self.form_b.last_synced_time, self.T0)

        with self._patch_graph(fetch_lead=item), \
                mock.patch.object(self.Client, '_iter_paged',
                                  return_value=iter([item])):
            self.form_b._sweep_form()
        self.env.invalidate_all()
        lead = self._all_leads('LGB11')
        self._assert_complete_b_lead(lead, 'LGB11', ghost, reader)
        self.assertEqual(self.form_b.last_synced_time,
                         datetime(2026, 6, 14, 10, 0, 0))

    def test_sync_retry_campaign_race_completes_on_retry(self):
        ghost = self._ghost('Ghost S')
        reader = self._notify_b_reader()
        row = self._failed_row('LGB12', self.page_b)
        payload = self._payload_b('LGB12', 'Ghost S')
        with self._patch_graph(fetch_lead=payload), \
                self._no_campaign_found(), mute_logger('odoo.sql_db'), \
                self.env.cr.savepoint():
            outcome = row._retry_one()
        self.env.invalidate_all()
        self.assertEqual(outcome, 'transient')
        self.assertEqual(row.status, 'failed')
        self.assertFalse(self._all_leads('LGB12'))

        with self._patch_graph(fetch_lead=payload):
            outcome = row._retry_one()
        self.env.invalidate_all()
        self.assertEqual(outcome, 'ok')
        lead = self._all_leads('LGB12')
        logs = self._assert_complete_b_lead(lead, 'LGB12', ghost, reader)
        # The fresh row from ingest was merged into the one retried.
        self.assertEqual(logs, row)
        self.assertEqual(row.lead_id, lead)


class CommittedFixtureMixin:
    """Helpers for tests that commit rows in their own cursors.

    The local Odoo service runs crons on every database, and a drain or
    backfill run could pick up these committed rows without the Graph mock.
    So the module's crons are pushed a day ahead for the whole class and put
    back afterwards. Use with BaseCase and call _setup_committed from
    setUpClass.
    """

    _CRON_IDS_SQL = """
        SELECT id, nextcall FROM ir_cron
         WHERE id IN (SELECT res_id FROM ir_model_data
                       WHERE module = 'meta_lead_ads' AND model = 'ir.cron')
    """

    @classmethod
    def _setup_committed(cls):
        cls.registry = Registry(get_db_name())
        with cls.registry.cursor() as cr:
            cls._parked_crons = cls._park_module_crons(cr)
        # A class cleanup runs even when a test fails.
        cls.addClassCleanup(cls._unpark_committed)

    @classmethod
    def _unpark_committed(cls):
        with cls.registry.cursor() as cr:
            cls._restore_module_crons(cr, cls._parked_crons)

    @classmethod
    def _park_module_crons(cls, cr):
        """Push the module's crons a day ahead; return {id: old nextcall}.

        A cron that is running holds its row lock, so this waits for it.
        Active flags are left alone; other tests check them.
        """
        cr.execute(cls._CRON_IDS_SQL)
        saved = dict(cr.fetchall())
        if saved:
            cr.execute(
                "UPDATE ir_cron "
                "SET nextcall = (now() AT TIME ZONE 'UTC') + interval '1 day' "
                "WHERE id = ANY(%s)", (list(saved),))
        return saved

    @classmethod
    def _restore_module_crons(cls, cr, saved):
        for cron_id, nextcall in saved.items():
            cr.execute("UPDATE ir_cron SET nextcall = %s WHERE id = %s",
                       (nextcall, cron_id))

    def _run_cleanup(self, steps):
        # A running server can lock committed rows for a moment, and some
        # deletes depend on others, so failed steps are retried in order.
        for attempt in range(3):
            if not steps:
                return
            if attempt:
                time.sleep(1)
            failed = []
            with self.registry.cursor() as cr:
                env = api.Environment(cr, SUPERUSER_ID, {})
                for step in steps:
                    try:
                        with cr.savepoint():
                            step(env)
                    except Exception:   # noqa: BLE001 - keep cleaning the rest
                        _logger.warning(
                            "%s cleanup step failed (attempt %s)",
                            type(self).__name__, attempt + 1, exc_info=True)
                        failed.append(step)
            steps = failed
        if steps:
            # Rows left behind break unrelated tests in later runs.
            self.fail("%s left committed rows behind; clean the database "
                      "before the next run" % type(self).__name__)


@tagged('post_install', '-at_install')
class TestLeadgenRaceConcurrency(CommittedFixtureMixin, BaseCase):
    """Two real transactions ingest the same leadgen id.

    The second one can't see the first one's lead yet. It must ask for a
    retry rather than log a skip without a lead, and the retry in a fresh
    transaction finds the lead.
    """

    @classmethod
    def setUpClass(cls):
        super().setUpClass()
        cls._setup_committed()
        # Hex only, so the =like cleanups never meet a wildcard, and a
        # rerun or another class can't reuse or remove these ids.
        cls.token = uuid.uuid4().hex[:10]

    def _payload(self, leadgen_id):
        # No campaign, ad set or ad: a campaign row the other transaction
        # can't see is a different race.
        return {'id': leadgen_id, 'created_time': '2026-06-14T10:00:00+0000',
                'platform': 'fb', 'field_data': [
                    {'name': 'email',
                     'values': ['%s@race.example.com' % leadgen_id.lower()]},
                    {'name': 'phone_number', 'values': ['+1 555 010 0199']},
                    {'name': 'full_name', 'values': ['Race Test']}]}

    def _graph(self, env, leadgen_id):
        Client = type(env['meta.graph.client'])
        return mock.patch.multiple(
            Client,
            fetch_lead=mock.Mock(return_value=self._payload(leadgen_id)),
            resolve_name=mock.Mock(return_value=False))

    def _ingest(self, cr, page_id, leadgen_id):
        env = api.Environment(cr, SUPERUSER_ID, {})
        with self._graph(env, leadgen_id):
            lead = env['meta.lead.ingest'].ingest_leadgen(
                env['meta.page'].browse(page_id), leadgen_id, 'webhook')
        if 'iap_enrich_done' in lead._fields:
            # Keeps the enrichment cron of a running server off this row.
            lead.iap_enrich_done = True
        return lead.id

    def _setup_page(self, ids, suffix):
        with self.registry.cursor() as cr:
            env = api.Environment(cr, SUPERUSER_ID, {})
            account = env['meta.account'].create({
                'name': 'Race Acct %s' % suffix,
                'account_id': 'RACEACC' + self.token + suffix,
                'app_secret': 'secret_race', 'access_token': 'tok_race'})
            ids['account'] = [account.id]
            page = env['meta.page'].create({
                'name': 'Race Page %s' % suffix,
                'page_id': 'PGR' + self.token + suffix,
                'access_token': 'tok_pgr',
                'account_id': account.id})
            ids['pages'] = [page.id]
            return page.id

    def _cleanup(self, ids):
        mine = 'LGR' + self.token + '%'

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

    def test_cleanup_failure_fails_the_test(self):
        def stuck(env):
            raise RuntimeError('row still locked')
        with mock.patch.object(time, 'sleep'), mute_logger(__name__), \
                self.assertRaises(AssertionError):
            self._run_cleanup([stuck])

    def test_crons_parked_during_committed_fixtures(self):
        saved = type(self)._parked_crons
        self.assertTrue(saved)
        with self.registry.cursor() as cr:
            cr.execute(self._CRON_IDS_SQL)
            now = dict(cr.fetchall())
        self.assertEqual(set(now), set(saved))
        floor = fields.Datetime.now() + timedelta(hours=23)
        for cron_id, nextcall in now.items():
            self.assertGreater(nextcall, floor)

        # Exercise restore and park on a cursor that is never committed.
        cr = self.registry.cursor()
        try:
            self._restore_module_crons(cr, saved)
            cr.execute(self._CRON_IDS_SQL)
            self.assertEqual(dict(cr.fetchall()), saved)
            again = self._park_module_crons(cr)
            self.assertEqual(set(again), set(saved))
        finally:
            cr.rollback()
            cr.close()

    def test_second_transaction_retries_then_finds_winner(self):
        ids = {'leads': []}
        leadgen_id = 'LGR' + self.token + '1'
        try:
            page_id = self._setup_page(ids, '1')
            cr1 = self.registry.cursor()
            cr2 = self.registry.cursor()
            try:
                # cr2's snapshot is taken before cr1 commits its lead.
                cr2.execute("SELECT 1")
                lead_id = self._ingest(cr1, page_id, leadgen_id)
                ids['leads'].append(lead_id)
                cr1.commit()
                with mute_logger('odoo.sql_db'), \
                        self.assertRaises(MetaIngestRaceError):
                    self._ingest(cr2, page_id, leadgen_id)
                cr2.rollback()
            finally:
                cr1.close()
                cr2.close()

            with self.registry.cursor() as cr:
                self.assertEqual(
                    self._ingest(cr, page_id, leadgen_id), lead_id)

            with self.registry.cursor() as cr:
                env = api.Environment(cr, SUPERUSER_ID, {})
                self.assertFalse(env['meta.sync.log'].search([
                    ('meta_leadgen_id', '=', leadgen_id),
                    ('status', 'in', ('success', 'skipped_idempotent')),
                    ('lead_id', '=', False)]))
                leads = env['crm.lead'].with_context(
                    active_test=False).search(
                    [('meta_leadgen_id', '=', leadgen_id)])
                self.assertEqual(leads.ids, [lead_id])
        finally:
            self._cleanup(ids)

    def test_drain_retries_in_new_transaction(self):
        ids = {'leads': []}
        leadgen_id = 'LGR' + self.token + '2'
        try:
            page_id = self._setup_page(ids, '2')
            with self.registry.cursor() as cr:
                env = api.Environment(cr, SUPERUSER_ID, {})
                event_id = env['meta.webhook.event'].create({
                    'leadgen_id': leadgen_id,
                    'page_id': 'PGR' + self.token + '2',
                    'form_id': 'FR2', 'status': 'pending'}).id

            cr1 = self.registry.cursor()
            cr2 = self.registry.cursor()
            try:
                cr2.execute("SELECT 1")
                lead_id = self._ingest(cr1, page_id, leadgen_id)
                ids['leads'].append(lead_id)
                cr1.commit()
                env2 = api.Environment(cr2, SUPERUSER_ID, {})
                with self._graph(env2, leadgen_id), mute_logger('odoo.sql_db'):
                    env2['meta.webhook.event'].browse(event_id)._process_one()
                cr2.commit()
            finally:
                cr1.close()
                cr2.close()

            with self.registry.cursor() as cr:
                env = api.Environment(cr, SUPERUSER_ID, {})
                event = env['meta.webhook.event'].browse(event_id)
                self.assertEqual(event.status, 'pending')
                self.assertEqual(event.attempts, 1)
                self.assertFalse(event.lead_id)

            with self.registry.cursor() as cr:
                env = api.Environment(cr, SUPERUSER_ID, {})
                with self._graph(env, leadgen_id):
                    env['meta.webhook.event'].browse(event_id)._process_one()

            with self.registry.cursor() as cr:
                env = api.Environment(cr, SUPERUSER_ID, {})
                event = env['meta.webhook.event'].browse(event_id)
                self.assertEqual(event.status, 'done')
                self.assertEqual(event.lead_id.id, lead_id)
        finally:
            self._cleanup(ids)


class _LogCapture(logging.Handler):
    """Keeps each record as it would be printed, traceback included."""

    def __init__(self):
        super().__init__(logging.DEBUG)
        self.lines = []
        self._formatter = logging.Formatter('%(message)s')

    def emit(self, record):
        self.lines.append(self._formatter.format(record))


@tagged('post_install', '-at_install')
class TestAttributionPending(IngestFixtureMixin, TransactionCase):
    """A name lookup that fails never costs the lead; the lead is flagged."""

    def _nameless(self, leadgen_id, **over):
        payload = self._fake_lead(id=leadgen_id, campaign_name=None,
                                  adset_name=None, ad_name=None)
        payload.update(over)
        return payload

    def _raising(self, exc, only=None):
        """resolve_name stub that raises ``exc`` for ``only`` (or all types)."""
        def _side(token, object_type, graph_id, payload_name=None,
                  app_secret=None, company=None):
            if only is None or object_type == only:
                raise exc
            return payload_name or 'Name %s' % graph_id
        return mock.Mock(side_effect=_side)

    def _ingest(self, leadgen_id, resolve_name, payload=None, **kwargs):
        payload = payload or self._nameless(leadgen_id)
        with self._patch_graph(fetch_lead=payload, resolve_name=resolve_name):
            return self.Ingest.ingest_leadgen(
                self.page, leadgen_id, 'manual', **kwargs)

    def _stored_attempts(self, lead):
        lead.flush_recordset()
        self.env.cr.execute(
            "SELECT meta_attribution_attempts FROM crm_lead WHERE id = %s",
            (lead.id,))
        return self.env.cr.fetchone()[0]

    # ---- create path -------------------------------------------------------

    def test_lookup_failure_still_creates_lead(self):
        errors = (MetaPermanentError, MetaTransientError,
                  MetaRateLimitError, MetaAuthError)
        for n, error in enumerate(errors, start=1):
            leadgen_id = 'LGP%s' % n
            with self.subTest(error=error.__name__):
                lead = self._ingest(leadgen_id,
                                    self._raising(error('lookup failed')))
                self.assertEqual(len(lead), 1)
                self.assertEqual(lead.email_from, 'Jane@Example.com')
                self.assertEqual(lead.contact_name, 'Jane Doe')
                self.assertEqual(lead.meta_campaign_id, 'C1')
                self.assertEqual(lead.meta_adset_id, 'A1')
                self.assertEqual(lead.meta_ad_id, 'AD1')
                self.assertFalse(lead.meta_campaign_name)
                self.assertTrue(lead.meta_attribution_pending)
                self.assertFalse(lead.campaign_id)
                self.assertEqual(self.env['meta.sync.log'].search_count([
                    ('meta_leadgen_id', '=', leadgen_id),
                    ('status', '=', 'success')]), 1)

    def test_rate_limit_stops_further_lookups(self):
        resolve = self._raising(MetaRateLimitError('slow down'))
        lead = self._ingest('LGP5', resolve)
        self.assertEqual(resolve.call_count, 1)
        self.assertTrue(lead.meta_attribution_pending)
        self.assertEqual(lead.meta_attribution_attempts, 0)
        # Written as 0, not left empty.
        self.assertEqual(self._stored_attempts(lead), 0)

    def test_transient_stops_further_lookups(self):
        resolve = self._raising(MetaTransientError('read timed out'))
        lead = self._ingest('LGP6', resolve)
        self.assertEqual(resolve.call_count, 1)
        self.assertTrue(lead.meta_attribution_pending)

    def test_permanent_does_not_stop_lookups(self):
        resolve = self._raising(MetaPermanentError('ad deleted'),
                                only='campaign')
        lead = self._ingest('LGP7', resolve)
        # Campaign, ad set and ad; the form name comes from the synced form.
        self.assertEqual(resolve.call_count, 3)
        self.assertFalse(lead.meta_campaign_name)
        self.assertEqual(lead.meta_adset_name, 'Name A1')
        self.assertEqual(lead.meta_ad_name, 'Name AD1')
        self.assertEqual(lead.meta_form_name, 'Contact Us')
        self.assertTrue(lead.meta_attribution_pending)

    def test_shared_lookup_state_skips_later_ingests(self):
        state = {}
        resolve = self._raising(MetaAuthError('token refused'))
        fetch = mock.Mock(side_effect=lambda page, lg: self._nameless(lg))
        Ingest = self.Ingest
        with self._patch_graph(fetch_lead=fetch, resolve_name=resolve):
            first = Ingest.ingest_leadgen(self.page, 'LGP20', 'cron',
                                          lookup_state=state)
            self.assertEqual(resolve.call_count, 1)
            second = Ingest.ingest_leadgen(self.page, 'LGP21', 'cron',
                                           lookup_state=state)
            self.assertEqual(resolve.call_count, 1)
            # Without a shared state each ingest tries again.
            third = Ingest.ingest_leadgen(self.page, 'LGP22', 'cron')
            self.assertEqual(resolve.call_count, 2)
        self.assertTrue(first.meta_attribution_pending)
        self.assertTrue(second.meta_attribution_pending)
        self.assertTrue(third.meta_attribution_pending)

    def test_all_names_resolved_not_pending(self):
        lead = self._ingest('LGP8', None, payload=self._fake_lead(id='LGP8'))
        self.assertEqual(lead.meta_campaign_name, 'Summer')
        self.assertFalse(lead.meta_attribution_pending)

    def test_empty_graph_name_is_pending(self):
        resolve = mock.Mock(return_value=False)
        lead = self._ingest('LGP9', resolve, payload=self._fake_lead(
            id='LGP9', campaign_name=None))
        self.assertEqual(resolve.call_count, 1)
        self.assertEqual(lead.meta_campaign_id, 'C1')
        self.assertFalse(lead.meta_campaign_name)
        self.assertTrue(lead.meta_attribution_pending)

    def test_form_name_failure_is_pending(self):
        resolve = self._raising(MetaPermanentError('form gone'), only='form')
        lead = self._ingest('LGP10', resolve, payload=self._fake_lead(
            id='LGP10', form_id='FX9'))
        self.assertEqual(resolve.call_count, 1)
        self.assertEqual(lead.meta_campaign_name, 'Summer')
        self.assertEqual(lead.meta_form_id, 'FX9')
        self.assertFalse(lead.meta_form_name)
        self.assertTrue(lead.meta_attribution_pending)

    # ---- match path --------------------------------------------------------

    def test_match_path_pending_from_match_gaps(self):
        blank = self.Lead.create({
            'name': 'Walk-in', 'type': 'lead',
            'email_from': 'Jane@Example.com', 'meta_campaign_name': 'Known'})
        resolve = self._raising(MetaPermanentError('gone'), only='campaign')
        lead = self._ingest('LGP11', resolve)
        self.assertEqual(lead, blank)
        self.assertEqual(lead.meta_campaign_name, 'Known')
        self.assertEqual(lead.meta_campaign_id, 'C1')
        self.assertEqual(lead.meta_adset_name, 'Name A1')
        self.assertEqual(lead.meta_ad_name, 'Name AD1')
        # Every id on the matched lead has a name, so nothing is pending.
        self.assertFalse(lead.meta_attribution_pending)

    def test_match_path_pending_when_match_lacks_name(self):
        blank = self.Lead.create({
            'name': 'Walk-in', 'type': 'lead',
            'email_from': 'Jane@Example.com'})
        resolve = self._raising(MetaPermanentError('gone'), only='campaign')
        lead = self._ingest('LGP14', resolve)
        self.assertEqual(lead, blank)
        self.assertEqual(lead.meta_campaign_id, 'C1')
        self.assertFalse(lead.meta_campaign_name)
        self.assertTrue(lead.meta_attribution_pending)
        self.assertEqual(self._stored_attempts(lead), 0)

    # ---- logging, backfill, mappings ---------------------------------------

    def test_lookup_failure_logs_no_error_text(self):
        capture = _LogCapture()
        logger = logging.getLogger('odoo.addons.meta_lead_ads')
        self.addCleanup(logger.setLevel, logger.level)
        self.addCleanup(logger.removeHandler, capture)
        logger.addHandler(capture)
        logger.setLevel(logging.DEBUG)
        resolve = self._raising(
            MetaPermanentError('SENTINEL_9f3a tok_test'))
        self._ingest('LGP12', resolve)
        self.assertTrue(any('MetaPermanentError' in line
                            for line in capture.lines))
        for line in capture.lines:
            self.assertNotIn('SENTINEL_9f3a', line)
            self.assertNotIn('tok_test', line)

    def test_backfill_not_stuck_on_lookup_failure(self):
        self.form.last_synced_time = datetime(2026, 6, 1, 10, 0, 0)
        item = self._nameless('LGP13',
                              created_time='2026-06-14T10:05:00+0000')
        resolve = self._raising(MetaPermanentError('gone'))
        with self._patch_graph(fetch_lead=item, resolve_name=resolve), \
                mock.patch.object(self.Client, '_iter_paged',
                                  return_value=iter([item])):
            self.form._sweep_form()
        lead = self.Lead.search([('meta_leadgen_id', '=', 'LGP13')])
        self.assertEqual(len(lead), 1)
        self.assertTrue(lead.meta_attribution_pending)
        self.form.invalidate_recordset(['last_synced_time'])
        self.assertEqual(self.form.last_synced_time,
                         datetime(2026, 6, 14, 10, 5, 0))

    def test_new_fields_not_mappable(self):
        for name in ('meta_attribution_pending', 'meta_attribution_attempts',
                     'meta_attribution_retry_after'):
            self.assertIn(name, MAPPING_FORBIDDEN_FIELDS)

    def test_form_and_search_views_expose_pending(self):
        form_view = self.env.ref('crm.crm_lead_view_form')
        arch = self.env['crm.lead'].get_views(
            [(form_view.id, 'form')])['views']['form']['arch']
        for snippet in ('meta_attribution_pending', 'name="meta_form_id"',
                        'name="meta_campaign_id"', 'alert-warning'):
            self.assertIn(snippet, arch)
        for xmlid in ('meta_lead_ads.crm_lead_view_search_meta_leads',
                      'meta_lead_ads.crm_lead_view_search_meta_opportunities',
                      'meta_lead_ads.crm_lead_view_search_meta_analysis'):
            self.assertIn('filter_meta_attribution_pending',
                          self.env.ref(xmlid).arch_db)

    # ---- one stop per Page for a whole run ---------------------------------

    def _sweep(self, error):
        ids = ('LGP30', 'LGP31', 'LGP32')
        items = [self._nameless(lg, created_time='2026-06-14T10:0%d:00+0000'
                                % i) for i, lg in enumerate(ids)]
        by_id = {item['id']: item for item in items}

        def _paged(token, path, params=None, app_secret=None, status=None, **kw):
            return iter(list(items) if path == 'F1/leads' else [])

        fetch = mock.Mock(side_effect=lambda page, lg: by_id[lg])
        resolve = self._raising(error)
        self.form.last_synced_time = datetime(2026, 6, 1, 10, 0, 0)
        with self._patch_graph(fetch_lead=fetch, resolve_name=resolve), \
                mock.patch.object(self.Client, '_iter_paged',
                                  side_effect=_paged):
            self.env['meta.lead.form']._cron_backfill()
        self.assertEqual(resolve.call_count, 1)
        leads = self.Lead.search([('meta_leadgen_id', 'in', ids)])
        self.assertEqual(len(leads), 3)
        self.assertTrue(all(leads.mapped('meta_attribution_pending')))
        self.form.invalidate_recordset(['last_synced_time'])
        self.assertEqual(self.form.last_synced_time,
                         datetime(2026, 6, 14, 10, 2, 0))

    def test_sweep_stops_name_lookups_after_throttle(self):
        self._sweep(MetaRateLimitError('slow down'))

    def test_sweep_stops_name_lookups_after_timeout(self):
        self._sweep(MetaTransientError('read timed out'))

    def test_drain_stops_name_lookups_for_throttled_page(self):
        self.env['meta.page'].create({
            'name': 'Page 2', 'page_id': 'PG2',
            'access_token': 'tok_p2', 'account_id': self.account.id})
        Event = self.env['meta.webhook.event']
        throttled = Event.browse()
        for leadgen_id in ('LGP40', 'LGP41', 'LGP42'):
            throttled |= Event.create({
                'leadgen_id': leadgen_id, 'page_id': 'PG1',
                'form_id': 'F1', 'status': 'pending'})
        other = Event.create({
            'leadgen_id': 'LGP43', 'page_id': 'PG2',
            'form_id': 'F1', 'status': 'pending'})

        def _side(token, object_type, graph_id, payload_name=None,
                  app_secret=None, company=None):
            if token == 'tok_test':
                raise MetaRateLimitError('slow down')
            return 'Name %s' % graph_id

        resolve = mock.Mock(side_effect=_side)
        fetch = mock.Mock(side_effect=lambda page, lg: self._nameless(lg))
        self.env.flush_all()
        with self._patch_graph(fetch_lead=fetch, resolve_name=resolve):
            Event._cron_drain()
        self.assertEqual(len([c for c in resolve.call_args_list
                              if c.args[0] == 'tok_test']), 1)
        for event in throttled | other:
            self.assertEqual(event.status, 'done')
            self.assertTrue(event.lead_id)
        self.assertTrue(all(throttled.lead_id.mapped(
            'meta_attribution_pending')))
        self.assertEqual(other.lead_id.meta_campaign_name, 'Name C1')
        self.assertFalse(other.lead_id.meta_attribution_pending)


@tagged('post_install', '-at_install')
class TestNameCacheConcurrency(CommittedFixtureMixin, BaseCase):
    """A cache write that loses to another transaction never fails the caller."""

    @classmethod
    def setUpClass(cls):
        super().setUpClass()
        cls._setup_committed()

    @staticmethod
    def _company(env):
        return env.ref('base.main_company')

    def _seed(self, graph_id):
        with self.registry.cursor() as cr:
            env = api.Environment(cr, SUPERUSER_ID, {})
            env['meta.name.cache'].create({
                'object_type': 'campaign', 'graph_id': graph_id,
                'name': False, 'company_id': self._company(env).id})

    def _committed_name(self, graph_id):
        with self.registry.cursor() as cr:
            cr.execute("SELECT name FROM meta_name_cache "
                       "WHERE object_type = 'campaign' AND graph_id = %s",
                       (graph_id,))
            return [row[0] for row in cr.fetchall()]

    def _cleanup(self):
        self._run_cleanup([lambda env: env['meta.name.cache'].search(
            [('graph_id', 'in', ('RACE_C1', 'RACE_C2'))]).unlink()])

    def _race(self, graph_id, second):
        """Commit name 'A' from one cursor, then run ``second`` in a cursor
        whose snapshot is older; return what ``second`` returned."""
        self._seed(graph_id)
        cr1 = self.registry.cursor()
        cr2 = self.registry.cursor()
        try:
            cr2.execute("SELECT 1")
            env1 = api.Environment(cr1, SUPERUSER_ID, {})
            env1['meta.name.cache']._store(
                'campaign', graph_id, 'A', self._company(env1))
            cr1.commit()
            env2 = api.Environment(cr2, SUPERUSER_ID, {})
            # assertLogs misses this record under the Odoo runner, so a
            # plain handler is used to prove the conflict really happened.
            capture = _LogCapture()
            logger = logging.getLogger(
                'odoo.addons.meta_lead_ads.models.meta_name_cache')
            logger.addHandler(capture)
            try:
                with mute_logger('odoo.sql_db'):
                    result = second(env2)
            finally:
                logger.removeHandler(capture)
            self.assertTrue(any('not saved (SerializationFailure)' in line
                                for line in capture.lines))
            # The transaction is still usable.
            cr2.execute("SELECT 1")
            self.assertEqual(cr2.fetchone(), (1,))
            cr2.rollback()
        finally:
            cr1.close()
            cr2.close()
        self.assertEqual(self._committed_name(graph_id), ['A'])
        return result

    def test_store_conflict_is_swallowed(self):
        try:
            stored = self._race('RACE_C1', lambda env: env[
                'meta.name.cache']._store(
                    'campaign', 'RACE_C1', 'B', self._company(env)))
            self.assertFalse(stored)
        finally:
            self._cleanup()

    def test_resolve_name_survives_cache_conflict(self):
        def _resolve(env):
            Client = type(env['meta.graph.client'])
            with mock.patch.object(Client, '_request',
                                   return_value={'name': 'B'}):
                return env['meta.graph.client'].resolve_name(
                    'tok', 'campaign', 'RACE_C2', company=self._company(env))
        try:
            self.assertEqual(self._race('RACE_C2', _resolve), 'B')
        finally:
            self._cleanup()


_RL_T0 = datetime(2026, 10, 16, 12, 0, 0)


class _AttributionRetryHelpers:
    """Shared by the retry test classes; needs IngestFixtureMixin."""

    _NAMES = {'C1': 'Summer', 'A1': 'Set', 'AD1': 'Creative'}

    def _pending(self, leadgen_id, page=None, **over):
        """Ingest a lead whose name lookups all fail, so it is flagged."""
        payload = self._fake_lead(
            id=leadgen_id, campaign_name=None, adset_name=None, ad_name=None,
            field_data=[
                {'name': 'email',
                 'values': ['%s@example.com' % leadgen_id.lower()]},
                {'name': 'full_name', 'values': ['Jane Doe']},
            ])
        payload.update(over)
        failing = mock.Mock(side_effect=MetaPermanentError('down'))
        with self._patch_graph(fetch_lead=payload, resolve_name=failing):
            lead = self.Ingest.ingest_leadgen(page or self.page, leadgen_id,
                                              'manual')
        self.assertTrue(lead.meta_attribution_pending)
        return lead

    def _resolver(self, names=None, fail=None):
        """resolve_name stub. ``fail`` maps a graph id or a token to an
        exception; other ids get their name from ``names``."""
        names = self._NAMES if names is None else names
        fail = fail or {}

        def _side(token, object_type, graph_id, payload_name=None,
                  app_secret=None, company=None):
            for key in (graph_id, token):
                if key in fail:
                    raise fail[key]
            return names.get(graph_id, 'Name %s' % graph_id)
        return mock.Mock(side_effect=_side)

    def _cron(self, resolve, limit=None, Ingest=None):
        self.env.flush_all()
        with self._patch_graph(resolve_name=resolve):
            (Ingest or self.Ingest)._cron_retry_attribution(limit=limit)
        self.env.invalidate_all()

    def _calls_with(self, resolve, token):
        return [c for c in resolve.call_args_list if c.args[0] == token]

    def _attempts(self, lead):
        lead.flush_recordset()
        self.env.cr.execute(
            "SELECT meta_attribution_attempts FROM crm_lead WHERE id = %s",
            (lead.id,))
        return self.env.cr.fetchone()[0]


@tagged('post_install', '-at_install')
class TestAttributionRetry(_AttributionRetryHelpers, MultiCompanyFixtureMixin,
                           IngestFixtureMixin, TransactionCase):
    """The retry job fills in names Meta could not return at ingest."""

    def _retry(self, lead, resolve, deadline=float('inf')):
        page = self.Ingest._attribution_retry_page(lead)
        with self._patch_graph(resolve_name=resolve):
            return self.Ingest._retry_attribution_one(lead, page, deadline)

    # ---- one lead ----------------------------------------------------------

    def test_retry_fills_names_and_clears_flag(self):
        lead = self._pending('LGT1')
        self.assertEqual(self._retry(lead, self._resolver()), 'done')
        self.assertEqual(lead.meta_campaign_name, 'Summer')
        self.assertEqual(lead.meta_adset_name, 'Set')
        self.assertEqual(lead.meta_ad_name, 'Creative')
        self.assertFalse(lead.meta_attribution_pending)
        self.assertEqual(lead.campaign_id.name, 'Summer')

    def test_retry_retitles_generated_name(self):
        self.env['ir.config_parameter'].sudo().set_param(
            LEAD_NAME_PARAM, '{campaign_name} - {contact_name}')
        lead = self._pending('LGT2')
        self.assertTrue(lead.meta_name_is_generated)
        self.assertNotIn('Summer', lead.name)
        self.assertEqual(self._retry(lead, self._resolver()), 'done')
        self.assertIn('Summer', lead.name)
        self.assertEqual(lead.name, 'Summer - Jane Doe')
        self.assertTrue(lead.meta_name_is_generated)

    def test_retry_keeps_manual_title(self):
        self.env['ir.config_parameter'].sudo().set_param(
            LEAD_NAME_PARAM, '{campaign_name} - {contact_name}')
        lead = self._pending('LGT3')
        lead.write({'name': 'Typed by hand'})
        self.assertFalse(lead.meta_name_is_generated)
        self.assertEqual(self._retry(lead, self._resolver()), 'done')
        self.assertEqual(lead.name, 'Typed by hand')
        self.assertFalse(lead.meta_name_is_generated)

    def test_retry_keeps_company_owner_and_existing_utm(self):
        lead = self._pending('LGT4', page=self.page_b)
        self.assertEqual(lead.company_id, self.co_b)
        source = self.env['utm.source'].create({'name': 'Fair Source'})
        hand = self.env['utm.campaign'].create({'name': 'Hand Campaign'})
        lead.write({'user_id': self.admin_b.id, 'source_id': source.id,
                    'campaign_id': hand.id})
        self.assertEqual(self._retry(lead, self._resolver()), 'done')
        self.assertEqual(lead.company_id, self.co_b)
        self.assertEqual(lead.user_id, self.admin_b)
        self.assertEqual(lead.source_id, source)
        self.assertEqual(lead.campaign_id, hand)
        self.assertEqual(lead.meta_campaign_name, 'Summer')

    def test_retry_archived_page_still_uses_its_token(self):
        lead = self._pending('LGT5', page=self.page_b)
        self.page_b.active = False
        resolve = self._resolver()
        self.assertEqual(self._retry(lead, resolve), 'done')
        self.assertEqual(resolve.call_args_list[0].args[0], 'tok_b')

    def test_retry_partial_then_rate_limit_keeps_names(self):
        lead = self._pending('LGT6')
        resolve = self._resolver(fail={'A1': MetaRateLimitError('slow')})
        self.assertEqual(self._retry(lead, resolve), 'rate_limited')
        self.assertEqual(lead.meta_campaign_name, 'Summer')
        self.assertEqual(lead.campaign_id.name, 'Summer')
        self.assertFalse(lead.meta_adset_name)
        self.assertTrue(lead.meta_attribution_pending)
        self.assertEqual(lead.meta_attribution_attempts, 0)

    def test_retry_auth_status(self):
        lead = self._pending('LGT7')
        resolve = self._resolver(fail={'tok_test': MetaAuthError('refused')})
        self.assertEqual(self._retry(lead, resolve), 'auth_failed')
        self.assertEqual(resolve.call_count, 1)
        self.assertTrue(lead.meta_attribution_pending)

    def test_retry_permanent_failure_is_pending(self):
        lead = self._pending('LGT8')
        resolve = self._resolver(fail={'C1': MetaPermanentError('gone')})
        self.assertEqual(self._retry(lead, resolve), 'pending')
        self.assertFalse(lead.meta_campaign_name)
        self.assertEqual(lead.meta_adset_name, 'Set')
        self.assertEqual(lead.meta_ad_name, 'Creative')
        self.assertTrue(lead.meta_attribution_pending)

    def test_retry_form_name_from_synced_form(self):
        lead = self._pending('LGT9', form_id='FX7', campaign_name='Summer',
                             adset_name='Set', ad_name='Creative')
        self.assertFalse(lead.meta_form_name)
        self.env['meta.lead.form'].create({
            'name': 'Synced Form', 'form_id': 'FX7', 'page_id': self.page.id})
        resolve = mock.Mock(side_effect=MetaPermanentError('gone'))
        self.assertEqual(self._retry(lead, resolve), 'done')
        self.assertEqual(lead.meta_form_name, 'Synced Form')
        self.assertFalse(lead.meta_attribution_pending)
        resolve.assert_not_called()

    def test_retry_out_of_time_before_graph(self):
        lead = self._pending('LGT10')
        resolve = self._resolver()
        self.assertEqual(
            self._retry(lead, resolve, deadline=time.monotonic() - 1),
            'out_of_time')
        resolve.assert_not_called()
        self.assertTrue(lead.meta_attribution_pending)

    def test_retry_missing_page(self):
        lead = self.Lead.create({
            'name': 'Orphan', 'meta_leadgen_id': 'LGT11',
            'meta_page_id': 'NO_SUCH_PAGE', 'meta_campaign_id': 'C1',
            'meta_attribution_pending': True,
            'meta_attribution_attempts': 0})
        page = self.Ingest._attribution_retry_page(lead)
        self.assertFalse(page)
        resolve = self._resolver()
        with self._patch_graph(resolve_name=resolve):
            status = self.Ingest._retry_attribution_one(
                lead, page, float('inf'))
        self.assertEqual(status, 'no_page')
        resolve.assert_not_called()

    def test_retry_transient_only_status(self):
        lead = self._pending('LGT56')
        resolve = mock.Mock(side_effect=MetaTransientError('down'))
        self.assertEqual(self._retry(lead, resolve), 'transient')
        self.assertTrue(lead.meta_attribution_pending)
        self.assertFalse(lead.meta_campaign_name)
        self.assertFalse(lead.meta_adset_name)
        self.assertFalse(lead.meta_ad_name)

    def test_retry_transient_with_permanent_is_pending(self):
        lead = self._pending('LGT57')
        resolve = self._resolver(fail={
            'C1': MetaPermanentError('gone'),
            'A1': MetaTransientError('down'),
            'AD1': MetaTransientError('down'),
        })
        self.assertEqual(self._retry(lead, resolve), 'pending')
        self.assertTrue(lead.meta_attribution_pending)

    def test_retry_transient_with_progress_not_counted(self):
        lead = self._pending('LGT58')
        resolve = self._resolver(fail={
            'A1': MetaTransientError('down'),
            'AD1': MetaTransientError('down'),
        })
        self.assertEqual(self._retry(lead, resolve), 'transient')
        self.assertEqual(lead.meta_campaign_name, 'Summer')
        self.assertEqual(lead.campaign_id.name, 'Summer')
        self.assertFalse(lead.meta_adset_name)
        self.assertTrue(lead.meta_attribution_pending)

    # ---- the cron run ------------------------------------------------------

    def _calls_for(self, resolve, graph_id):
        return [c for c in resolve.call_args_list if c.args[2] == graph_id]

    def test_cron_auth_failure_is_page_scoped(self):
        p1 = self._pending('LGT20')
        p3 = self._pending('LGT21')
        p2 = self._pending('LGT22', page=self.page_b)
        resolve = self._resolver(fail={'tok_test': MetaAuthError('refused')})
        before = fields.Datetime.now()
        self._cron(resolve)
        self.assertFalse(p2.meta_attribution_pending)
        self.assertEqual(p2.meta_campaign_name, 'Summer')
        self.assertEqual(p1.meta_attribution_attempts, 1)
        self.assertEqual(p3.meta_attribution_attempts, 0)
        for lead in (p1, p3):
            self.assertTrue(lead.meta_attribution_pending)
            self.assertGreater(lead.meta_attribution_retry_after,
                               before + timedelta(hours=5))
            self.assertLess(lead.meta_attribution_retry_after,
                            before + timedelta(hours=7))
        self.assertEqual(len(self._calls_with(resolve, 'tok_test')), 1)
        # Still paused on the next run.
        self._cron(resolve)
        self.assertEqual(len(self._calls_with(resolve, 'tok_test')), 1)

    def test_cron_dead_page_does_not_starve_batch(self):
        dead = [self._pending('LGT2%d' % n) for n in (3, 4, 5)]
        alive = self._pending('LGT26', page=self.page_b)
        resolve = self._resolver(fail={'tok_test': MetaAuthError('refused')})
        for _run in range(2):
            self._cron(resolve, limit=2)
            if not alive.meta_attribution_pending:
                break
        self.assertFalse(alive.meta_attribution_pending)
        self.assertEqual(len(self._calls_with(resolve, 'tok_test')), 1)
        self.assertTrue(all(lead.meta_attribution_pending for lead in dead))

    def test_cron_rate_limit_stops_run(self):
        first = self._pending('LGT27')
        second = self._pending('LGT28', page=self.page_b)
        resolve = mock.Mock(side_effect=MetaRateLimitError('slow down'))
        with self._clock():
            self._cron(resolve)
        self.assertEqual(resolve.call_count, 1)
        for lead in (first, second):
            self.assertTrue(lead.meta_attribution_pending)
            self.assertEqual(self._attempts(lead), 0)
        self.assertEqual(
            first.meta_attribution_retry_after,
            _RL_T0 + timedelta(minutes=RATE_LIMIT_FALLBACK_MINUTES))
        self.assertFalse(second.meta_attribution_retry_after)

    # ---- throttled name lookups --------------------------------------------

    def _clock(self, at=None):
        return mock.patch.object(fields.Datetime, 'now',
                                 return_value=at or _RL_T0)

    def test_cron_rate_limit_pauses_page_for_hint(self):
        first = self._pending('LGT41')
        second = self._pending('LGT42')
        other = self._pending('LGT43', page=self.page_b)
        with self._clock() as clock:
            def _side(*args, **kwargs):
                # Meta answers five minutes into the run.
                clock.return_value = _RL_T0 + timedelta(minutes=5)
                raise MetaRateLimitError('slow', retry_after_min=19)
            resolve = mock.Mock(side_effect=_side)
            self._cron(resolve)
        self.assertEqual(resolve.call_count, 1)
        for lead in (first, second):
            self.assertEqual(lead.meta_attribution_retry_after,
                             _RL_T0 + timedelta(minutes=24))
            self.assertEqual(self._attempts(lead), 0)
            self.assertTrue(lead.meta_attribution_pending)
        self.assertFalse(other.meta_attribution_retry_after)
        self.assertEqual(self._attempts(other), 0)
        self.page.invalidate_recordset()
        self.assertFalse(self.page.lead_fetch_paused_until)

    def test_cron_rate_limit_fallback_and_long_hint(self):
        first = self._pending('LGT46')
        with self._clock():
            self._cron(mock.Mock(side_effect=MetaRateLimitError('slow')))
        self.assertEqual(
            first.meta_attribution_retry_after,
            _RL_T0 + timedelta(minutes=RATE_LIMIT_FALLBACK_MINUTES))

        fresh = self._pending('LGT47')
        first.meta_attribution_retry_after = False
        long_hint = mock.Mock(side_effect=MetaRateLimitError(
            'slow', retry_after_min=10 ** 6))
        with self._clock(), mute_logger(
                'odoo.addons.meta_lead_ads.models.meta_webhook_event'):
            self._cron(long_hint)
        # Cut to a day.
        self.assertEqual(fresh.meta_attribution_retry_after,
                         _RL_T0 + timedelta(minutes=24 * 60))

    def test_cron_rate_limit_keeps_later_wait(self):
        waiting = self._pending('LGT44')
        waiting.meta_attribution_retry_after = _RL_T0 + timedelta(hours=3)
        due = self._pending('LGT45')
        resolve = mock.Mock(side_effect=MetaRateLimitError(
            'slow', retry_after_min=19))
        with self._clock():
            self._cron(resolve)
        self.assertEqual(resolve.call_count, 1)
        self.assertEqual(due.meta_attribution_retry_after,
                         _RL_T0 + timedelta(minutes=19))
        self.assertEqual(waiting.meta_attribution_retry_after,
                         _RL_T0 + timedelta(hours=3))

    def test_retry_one_reports_rate_limit(self):
        lead = self._pending('LGT48')
        page = self.Ingest._attribution_retry_page(lead)
        resolve = mock.Mock(side_effect=MetaRateLimitError(
            'slow', retry_after_min=7))
        info = {}
        with self._patch_graph(resolve_name=resolve):
            status = self.Ingest._retry_attribution_one(
                lead, page, float('inf'), info=info)
        self.assertEqual(status, 'rate_limited')
        self.assertEqual(info['rate_limit'].retry_after_min, 7)
        self.assertEqual(self._retry(lead, resolve), 'rate_limited')

    def test_cron_time_budget(self):
        first = self._pending('LGT29', campaign_id='CT29')
        second = self._pending('LGT30', campaign_id='CT30')
        clock = [0.0]
        names = {'CT29': 'Budget One', 'CT30': 'Budget Two'}

        def _side(token, object_type, graph_id, payload_name=None,
                  app_secret=None, company=None):
            # The first lookup takes the whole budget.
            clock[0] += 1000
            return names.get(graph_id, 'Name %s' % graph_id)

        resolve = mock.Mock(side_effect=_side)
        with mock.patch(
                'odoo.addons.meta_lead_ads.models.meta_lead_ingest._monotonic',
                side_effect=lambda: clock[0]):
            self._cron(resolve)
        self.assertEqual(resolve.call_count, 1)
        self.assertEqual(first.meta_campaign_name, 'Budget One')
        self.assertFalse(first.meta_adset_name)
        self.assertTrue(first.meta_attribution_pending)
        self.assertFalse(self._calls_for(resolve, 'CT30'))
        self.assertFalse(second.meta_campaign_name)
        self.assertEqual(self._attempts(first), 0)
        self.assertEqual(self._attempts(second), 0)

    def test_cron_failure_increments_attempts(self):
        lead = self._pending('LGT31')
        self.assertEqual(self._attempts(lead), 0)
        self._cron(mock.Mock(side_effect=MetaPermanentError('gone')))
        self.assertTrue(lead.meta_attribution_pending)
        self.assertEqual(self._attempts(lead), 1)

    def test_cron_cap_stops_retries(self):
        lead = self._pending('LGT32')
        lead.meta_attribution_attempts = ATTRIBUTION_RETRY_CAP
        resolve = self._resolver()
        self._cron(resolve)
        resolve.assert_not_called()
        self.assertTrue(lead.meta_attribution_pending)
        self.assertEqual(self._attempts(lead), ATTRIBUTION_RETRY_CAP)

    def test_cron_serialization_failure_not_counted(self):
        lead = self._pending('LGT33')
        with mock.patch.object(type(self.Ingest), '_retry_attribution_one',
                               side_effect=SerializationFailure('x')):
            self._cron(self._resolver())
        self.assertTrue(lead.meta_attribution_pending)
        self.assertEqual(self._attempts(lead), 0)

    def test_cron_bookkeeping_failure_isolated(self):
        l1 = self._pending('LGT34', campaign_id='CB34')
        l2 = self._pending('LGT35', campaign_id='CB35')
        l3 = self._pending('LGT36', campaign_id='CB36')
        self.assertLess(l1.id, l2.id)
        self.assertLess(l2.id, l3.id)
        resolve = self._resolver(fail={'CB35': RuntimeError('boom')})
        Lead = type(self.Lead)
        original_write = Lead.write

        def _write(records, vals):
            if 'meta_attribution_attempts' in vals and l2.id in records.ids:
                raise SerializationFailure('x')
            return original_write(records, vals)

        with mock.patch.object(Lead, 'write', _write), \
                mute_logger('odoo.addons.meta_lead_ads.models.meta_lead_ingest'):
            self._cron(resolve)
        self.assertFalse(l1.meta_attribution_pending)
        self.assertFalse(l3.meta_attribution_pending)
        self.assertTrue(l2.meta_attribution_pending)
        self.assertEqual(self._attempts(l2), 0)

    def test_cron_logs_no_exception_text(self):
        self._pending('LGT37', campaign_id='CS37')
        self._pending('LGT38', campaign_id='CS38')
        capture = _LogCapture()
        logger = logging.getLogger('odoo.addons.meta_lead_ads')
        self.addCleanup(logger.setLevel, logger.level)
        self.addCleanup(logger.removeHandler, capture)
        logger.addHandler(capture)
        logger.setLevel(logging.DEBUG)
        resolve = self._resolver(fail={
            'CS37': RuntimeError('SENTINEL_77 tok_test'),
            'CS38': MetaPermanentError('SENTINEL_77'),
        })
        self._cron(resolve)
        self.assertTrue(any('RuntimeError' in line for line in capture.lines))
        self.assertTrue(any('MetaPermanentError' in line
                            for line in capture.lines))
        for line in capture.lines:
            self.assertNotIn('SENTINEL_77', line)
            self.assertNotIn('tok_test', line)

    def test_cron_never_creates_leads(self):
        self._pending('LGT39')
        self._pending('LGT40', page=self.page_b)
        AllLeads = self.Lead.with_context(active_test=False)
        before = AllLeads.search_count([])
        self._cron(self._resolver())
        self._cron(mock.Mock(side_effect=MetaPermanentError('gone')))
        self.assertEqual(AllLeads.search_count([]), before)

    def test_cron_selects_null_attempts(self):
        tried = self._pending('LGT41', campaign_id='CN41')
        tried.meta_attribution_attempts = 3
        fresh = self._pending('LGT42', campaign_id='CN42')
        self.assertLess(tried.id, fresh.id)
        self.env.flush_all()
        self.env.cr.execute(
            "UPDATE crm_lead SET meta_attribution_attempts = NULL "
            "WHERE id = %s", (fresh.id,))
        self.env.invalidate_all()
        self.assertIsNone(self._attempts(fresh))
        resolve = mock.Mock(side_effect=MetaPermanentError('gone'))
        self._cron(resolve, limit=1)
        self.assertTrue(self._calls_for(resolve, 'CN42'))
        self.assertFalse(self._calls_for(resolve, 'CN41'))
        self.assertEqual(self._attempts(fresh), 1)
        self.assertEqual(self._attempts(tried), 3)

    # ---- Meta timeouts and 5xx ---------------------------------------------

    def test_transient_round_defers_lead(self):
        lead = self._pending('LGT60')
        resolve = mock.Mock(side_effect=MetaTransientError('down'))
        start = fields.Datetime.now()
        self._cron(resolve)
        self.assertEqual(self._attempts(lead), 0)
        self.assertTrue(lead.meta_attribution_pending)
        self.assertTrue(lead.meta_attribution_retry_after)
        due = start + timedelta(hours=ATTRIBUTION_TRANSIENT_BACKOFF_HOURS)
        self.assertGreater(lead.meta_attribution_retry_after,
                           due - timedelta(minutes=1))
        self.assertLess(lead.meta_attribution_retry_after,
                        due + timedelta(minutes=1))
        calls = len(self._calls_for(resolve, 'C1'))
        self.assertEqual(calls, 1)
        self._cron(resolve)
        self.assertEqual(len(self._calls_for(resolve, 'C1')), calls)

    def test_outage_longer_than_cap_does_not_strand(self):
        lead = self._pending('LGT61')
        down = mock.Mock(side_effect=MetaTransientError('down'))
        for _run in range(ATTRIBUTION_RETRY_CAP + 2):
            self._cron(down)
            # As if the backoff had passed.
            lead.sudo().write({'meta_attribution_retry_after': False})
        self.assertEqual(self._attempts(lead), 0)
        self._cron(self._resolver())
        self.assertFalse(lead.meta_attribution_pending)
        self.assertEqual(lead.meta_campaign_name, 'Summer')
        self.assertEqual(lead.meta_ad_name, 'Creative')

    def test_transient_lead_does_not_starve_others(self):
        stuck = self._pending('LGT62', campaign_id='CXT1', adset_id='AXT1',
                              ad_id='ADXT1')
        other = self._pending('LGT63')
        self.assertLess(stuck.id, other.id)
        resolve = self._resolver(fail={
            gid: MetaTransientError('down')
            for gid in ('CXT1', 'AXT1', 'ADXT1')})
        self._cron(resolve)
        self.assertFalse(other.meta_attribution_pending)
        self.assertEqual(other.meta_campaign_name, 'Summer')
        self.assertTrue(stuck.meta_attribution_pending)
        self.assertEqual(self._attempts(stuck), 0)

    def test_transient_streak_ends_run(self):
        leads = [self._pending('LGT6%d' % n, campaign_id='CST%d' % n)
                 for n in (4, 5, 6, 7)]
        resolve = mock.Mock(side_effect=MetaTransientError('down'))
        self._cron(resolve)
        self.assertTrue(self._calls_for(resolve, 'CST6'))
        self.assertFalse(self._calls_for(resolve, 'CST7'))
        for lead in leads:
            self.assertEqual(self._attempts(lead), 0)

    def test_persistent_transient_leads_do_not_starve_queue(self):
        stuck = []
        fail = {}
        for n in (1, 2, 3, 4):
            ids = {'campaign_id': 'CPT%d' % n, 'adset_id': 'APT%d' % n,
                   'ad_id': 'ADPT%d' % n}
            stuck.append(self._pending('LGT7%d' % n, **ids))
            fail.update({gid: MetaTransientError('down')
                         for gid in ids.values()})
        normal = self._pending('LGT75')
        resolve = self._resolver(fail=fail)
        for _run in range(3):
            self._cron(resolve)
        self.assertFalse(normal.meta_attribution_pending)
        self.assertEqual(normal.meta_campaign_name, 'Summer')
        now = fields.Datetime.now()
        for n, lead in enumerate(stuck, start=1):
            self.assertTrue(lead.meta_attribution_pending)
            self.assertEqual(self._attempts(lead), 0)
            self.assertTrue(lead.meta_attribution_retry_after)
            self.assertGreater(lead.meta_attribution_retry_after, now)
            self.assertLessEqual(
                len(self._calls_for(resolve, 'CPT%d' % n)), 1)

    def test_cron_uses_sudo_token_as_plain_user(self):
        lead = self._pending('LGT43')
        user = new_test_user(self.env, login='ri_plain',
                             groups='base.group_user')
        resolve = self._resolver()
        self._cron(resolve, Ingest=self.Ingest.with_user(user))
        self.assertEqual(resolve.call_args_list[0].args[0], 'tok_test')
        self.assertFalse(lead.meta_attribution_pending)
        self.assertEqual(lead.meta_campaign_name, 'Summer')

    # ---- schedule and restart ----------------------------------------------

    def test_retry_cron_record(self):
        cron = self.env.ref('meta_lead_ads.cron_meta_attribution_retry')
        self.assertTrue(cron.active)
        self.assertEqual(cron.model_id.model, 'meta.lead.ingest')
        self.assertIn('_cron_retry_attribution', cron.code)
        self.assertEqual(cron.interval_number, 30)
        self.assertEqual(cron.interval_type, 'minutes')
        cron.method_direct_trigger()

    def _exhausted(self, leadgen_id):
        lead = self._pending(leadgen_id)
        lead.write({
            'meta_attribution_attempts': ATTRIBUTION_RETRY_CAP,
            'meta_attribution_retry_after':
                fields.Datetime.now() + timedelta(hours=3),
        })
        return lead

    def test_reset_restarts_exhausted_lead(self):
        lead = self._exhausted('LGT50')
        self.assertEqual(lead.company_id, self.co_a)
        resolve = self._resolver()
        self._cron(resolve)
        resolve.assert_not_called()
        user = new_test_user(
            self.env, login='ri_meta_admin_crm',
            groups='base.group_user,meta_lead_ads.group_meta_admin,'
                   'sales_team.group_sale_salesman_all_leads',
            company_id=self.co_a.id, company_ids=[Command.set([self.co_a.id])])
        lead.with_user(user).action_reset_meta_attribution_retry()
        self.assertEqual(lead.meta_attribution_attempts, 0)
        self.assertFalse(lead.meta_attribution_retry_after)
        self._cron(resolve)
        self.assertFalse(lead.meta_attribution_pending)
        self.assertEqual(lead.meta_campaign_name, 'Summer')

    def test_reset_refused_for_non_admin(self):
        lead = self._exhausted('LGT51')
        with self.assertRaises(AccessError):
            lead.with_user(self.sales_a).action_reset_meta_attribution_retry()
        self.assertEqual(lead.meta_attribution_attempts,
                         ATTRIBUTION_RETRY_CAP)

    def test_reset_refused_without_lead_access(self):
        lead = self._exhausted('LGT52')
        self.assertEqual(lead.company_id, self.co_a)
        with self.assertRaises(AccessError):
            lead.with_user(self.admin_b).action_reset_meta_attribution_retry()
        self.assertEqual(lead.meta_attribution_attempts,
                         ATTRIBUTION_RETRY_CAP)

    def test_form_shows_reset_button(self):
        # arch_db, because get_views drops a groups= node for a user outside
        # the group.
        arch = self.env.ref('meta_lead_ads.crm_lead_view_form_meta').arch_db
        self.assertIn('action_reset_meta_attribution_retry', arch)
        self.assertIn('group_meta_admin', arch)
        self.assertIn('stops after 10 failed tries', arch)


@tagged('post_install', '-at_install')
class TestAttributionRetryIsolation(_AttributionRetryHelpers,
                                    MultiCompanyFixtureMixin,
                                    IngestFixtureMixin, TransactionCase):
    """The retry only uses the Page ingest linked, for that Page's company,
    and users can't plant the ids it looks up."""

    def setUp(self):
        super().setUp()
        self.sales_b = new_test_user(
            self.env, 'ri_sales_b',
            groups='base.group_user,sales_team.group_sale_salesman_all_leads',
            company_id=self.co_b.id, company_ids=[Command.set([self.co_b.id])])

    def _guarded_samples(self):
        return [
            ('meta_campaign_id', 'X1'),
            ('meta_adset_id', 'X1'),
            ('meta_ad_id', 'X1'),
            ('meta_form_id', 'X1'),
            ('meta_page_id', 'X1'),
            ('meta_attribution_pending', True),
            ('meta_attribution_attempts', 5),
            ('meta_attribution_retry_after', datetime(2026, 1, 1, 12, 0)),
        ]

    def test_user_cannot_write_retry_inputs(self):
        lead = self._pending('LGI1', page=self.page_b)
        as_user = lead.with_user(self.sales_b)
        for name, value in self._guarded_samples():
            before = lead[name]
            with self.assertRaises(AccessError):
                as_user.write({name: value})
            lead.invalidate_recordset()
            self.assertEqual(lead[name], before, name)

    def test_user_cannot_redirect_retry_to_other_company(self):
        lead = self._pending('LGI7', page=self.page_b)
        with self.assertRaises(AccessError):
            lead.with_user(self.sales_b).write({
                'meta_page_id_ref': False,
                'meta_page_id': 'PG1',
                'meta_campaign_id': 'X1',
                'meta_campaign_name': False,
                'meta_attribution_pending': True,
            })
        lead.invalidate_recordset()
        self.assertEqual(lead.meta_page_id_ref, self.page_b)

    def test_user_cannot_create_with_retry_inputs(self):
        Lead = self.env['crm.lead'].with_user(self.sales_b)
        for name, value in self._guarded_samples():
            with self.assertRaises(AccessError):
                Lead.create({'name': 'Planted', 'type': 'lead', name: value})
        lead = Lead.create({'name': 'Plain', 'type': 'lead',
                            'meta_attribution_pending': False,
                            'meta_campaign_id': False})
        self.assertTrue(lead)

    def test_retry_ignores_char_page_id(self):
        lead = self._pending('LGI2', page=self.page_b)
        lead.sudo().write({
            'meta_page_id_ref': False, 'meta_page_id': 'PG1',
            'meta_campaign_id': 'X1', 'meta_campaign_name': False,
        })
        self.assertFalse(self.Ingest._attribution_retry_page(lead))
        resolve = self._resolver()
        self._cron(resolve)
        self.assertEqual(self._calls_with(resolve, 'tok_test'), [])
        self.assertFalse(lead.meta_campaign_name)

    def test_retry_refuses_page_of_other_company(self):
        lead = self._pending('LGI3', page=self.page_b)
        lead.sudo().write({'meta_page_id_ref': self.page.id})
        self.assertFalse(self.Ingest._attribution_retry_page(lead))
        resolve = self._resolver()
        self._cron(resolve)
        self.assertEqual(self._calls_with(resolve, 'tok_test'), [])

    def test_retry_refuses_page_for_lead_without_company(self):
        # A lead without a company is readable from every company, so it
        # must not get names fetched with company A's token.
        lead = self._pending('LGI6')
        self.assertEqual(lead.company_id, self.co_a)
        lead.sudo().write({'company_id': False})
        self.assertFalse(self.Ingest._attribution_retry_page(lead))
        resolve = self._resolver()
        self._cron(resolve)
        self.assertEqual(self._calls_with(resolve, 'tok_test'), [])
        self.assertTrue(lead.meta_attribution_pending)
        self.assertEqual(self._attempts(lead), 1)

    def test_user_copy_drops_retry_inputs(self):
        lead = self._pending('LGI4', page=self.page_b)
        self.assertTrue(lead.meta_campaign_id)
        copy = lead.with_user(self.sales_b).copy()
        copy = copy.sudo()
        self.assertFalse(copy.meta_campaign_id)
        self.assertFalse(copy.meta_page_id)
        self.assertFalse(copy.meta_attribution_pending)
        self.assertTrue(lead.meta_campaign_id)

    def test_retry_same_company_still_runs(self):
        lead = self._pending('LGI5', page=self.page_b)
        resolve = self._resolver()
        self._cron(resolve)
        self.assertEqual(resolve.call_args_list[0].args[0], 'tok_b')
        self.assertFalse(lead.meta_attribution_pending)
