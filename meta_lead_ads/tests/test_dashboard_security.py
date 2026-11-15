# Meta Lead Ads Integration for Odoo — © 2026 CoreSys Builders. All rights reserved.
# Licensed under the Odoo Proprietary License v1.0 (OPL-1).
# Unauthorized copying, redistribution, or resale of this software, in whole or in
# part, via any medium, is strictly prohibited and constitutes a license violation.
# OPL-1: https://www.odoo.com/documentation/18.0/legal/licenses.html#odoo-apps

"""Security tests for ``meta.account.get_dashboard_metrics``.

- Non-admins calling the method directly get AccessError. The has_group
  check inside the method is the real gate; the menu's groups= only hides the
  menu. Both test users have base.group_user, otherwise Odoo makes them share
  (portal) users and the call would fail for the wrong reason.
- The payload never carries access_token, app_secret or raw_payload keys, or
  any seeded secret value. Health signals and sync_recent rows are limited to
  a fixed set of keys.
"""
from datetime import timedelta

from odoo.tests.common import TransactionCase, tagged
from odoo.exceptions import AccessError

from .test_dashboard_metrics import DashboardFixtureMixin
from .test_multi_company import MultiCompanyFixtureMixin

# Secrets seeded by IngestFixtureMixin.setUp; none may appear in the payload.
SEEDED_SECRETS = ('secret_test', 'tok_acct', 'tok_test', 'app_test')

# The only keys allowed in health signals and sync_recent rows.
HEALTH_KEYS = {'webhook', 'drain', 'backfill', 'token'}
HEALTH_SIGNAL_KEYS = {'status', 'label', 'caption'}
SYNC_RECENT_KEYS = {'status', 'meta_leadgen_id', 'create_date'}


def _flatten(obj):
    """Yield ('key', k) and ('value', v) for everything in a nested
    dict/list payload."""
    if isinstance(obj, dict):
        for k, v in obj.items():
            yield ('key', k)
            yield from _flatten(v)
    elif isinstance(obj, (list, tuple)):
        for v in obj:
            yield from _flatten(v)
    else:
        yield ('value', obj)


@tagged('post_install', '-at_install')
class TestDashboardSecurity(MultiCompanyFixtureMixin, DashboardFixtureMixin,
                            TransactionCase):
    """Admin gate and payload contents of the dashboard method."""

    def setUp(self):
        super().setUp()
        base_internal = self.env.ref('base.group_user')
        user_group = self.env.ref('meta_lead_ads.group_meta_user')
        # A Meta User, not an admin.
        self.non_admin_user = self.env['res.users'].create({
            'name': 'Dash NonAdmin', 'login': 'dash_nonadmin',
            'groups_id': [(6, 0, [base_internal.id, user_group.id])]})
        # An internal user with no Meta groups at all.
        self.plain_internal = self.env['res.users'].create({
            'name': 'Dash Plain', 'login': 'dash_plain',
            'groups_id': [(6, 0, [base_internal.id])]})

    # ---- admin gate --------------------------------------------------------

    def test_meta_user_non_admin_call_raises(self):
        """A Meta User calling the method over RPC gets AccessError."""
        with self.assertRaises(AccessError):
            self.Account.with_user(
                self.non_admin_user).get_dashboard_metrics()

    def test_plain_internal_non_admin_call_raises(self):
        """An internal user with no Meta groups gets AccessError."""
        with self.assertRaises(AccessError):
            self.Account.with_user(
                self.plain_internal).get_dashboard_metrics()

    def test_forged_company_context_denied(self):
        """An admin of company A can't ask for company B through the
        context; the method runs as sudo afterwards, so this is the only
        check on it."""
        self._mk_lead(self.win_from, extra={
            'company_id': self.co_b.id, 'user_id': False, 'team_id': False})
        for forged in ([self.co_b.id], [self.co_a.id, self.co_b.id]):
            with self.assertRaises(AccessError):
                self.Account.with_user(self.admin_a).with_context(
                    allowed_company_ids=forged).get_dashboard_metrics()

    # ---- no secrets in the payload ------------------------------------------

    def test_admin_payload_has_no_secret_keys_or_values(self):
        """No secret key names and no seeded secret values anywhere in the
        payload, health captions included."""
        self._seed_window()
        # Put the webhook and the drain into their problem states, so the
        # count captions are scanned too.
        failed = self._mk_log(self.win_from, 'failed', trigger='webhook',
                              page=self.page)
        self._age_rows(failed, timedelta(minutes=30))
        # A lead waiting on an archived Page adds a clause to the caption.
        held_page = self.env['meta.page'].create({
            'name': 'Held Page', 'page_id': 'PG_HOLD_SEC',
            'account_id': self.account.id, 'lead_type': 'lead'})
        held = self._mk_log(self.win_from, 'failed', trigger='webhook',
                            page=held_page)
        self._age_rows(held, timedelta(minutes=30))
        held_page.sudo().active = False
        event = self.env['meta.webhook.event'].sudo().create({
            'leadgen_id': 'LG_SEC_SLOW', 'page_id': 'PG1',
            'status': 'pending'})
        self._age_rows(event, timedelta(minutes=20))
        m = self._metrics(period_mode='month',
                          date_from=self.win_from, date_to=self.win_to)
        self.assertEqual(m['health']['webhook']['status'], 'failing')
        self.assertIn('waiting on an archived Page',
                      m['health']['webhook']['caption'])
        keys = {v for (kind, v) in _flatten(m) if kind == 'key'}
        for forbidden in ('access_token', 'app_secret', 'raw_payload'):
            self.assertNotIn(forbidden, keys)
        values = [v for (kind, v) in _flatten(m)
                  if kind == 'value' and isinstance(v, str)]
        for secret in SEEDED_SECRETS:
            for val in values:
                self.assertNotIn(secret, val)

    # ---- key allowlists --------------------------------------------------------

    def test_health_signal_keys_are_allowlisted(self):
        """Health has exactly the webhook, drain, backfill and token pills,
        and each pill has exactly status, label and caption, all text."""
        self._seed_window()
        m = self._metrics(period_mode='month',
                          date_from=self.win_from, date_to=self.win_to)
        self.assertIn('health', m)
        self.assertEqual(set(m['health']), HEALTH_KEYS)
        for signal_name, signal in m['health'].items():
            self.assertEqual(
                set(signal.keys()), HEALTH_SIGNAL_KEYS,
                "health[%r] keys %r != allowlist %r"
                % (signal_name, set(signal.keys()), HEALTH_SIGNAL_KEYS))
            for value in signal.values():
                self.assertIsInstance(value, str, signal_name)

    # ---- daily totals --------------------------------------------------------

    def _totals_row(self, company):
        self.env.cr.execute("""
            INSERT INTO meta_sync_stats
                   (day, company_id, trigger, status, row_count)
            VALUES (%s, %s, 'cron', 'success', 3)
            RETURNING id
        """, (self.win_from.date(), company.id))
        return self.env['meta.sync.stats'].browse(self.env.cr.fetchone()[0])

    def test_meta_user_cannot_read_totals(self):
        self._totals_row(self.co_a)
        Stats = self.env['meta.sync.stats'].with_user(self.non_admin_user)
        with self.assertRaises(AccessError):
            Stats.search([])
        with self.assertRaises(AccessError):
            self.env['meta.sync.stats'].with_user(
                self.plain_internal).search([])

    def test_meta_admin_reads_totals_but_cannot_change_them(self):
        row = self._totals_row(self.co_a)
        Stats = self.env['meta.sync.stats'].with_user(self.admin_a)
        self.assertEqual(Stats.search([('id', '=', row.id)]), row)
        with self.assertRaises(AccessError):
            Stats.create({'day': self.win_from.date(),
                          'company_id': self.co_a.id, 'trigger': 'cron',
                          'status': 'success', 'row_count': 1})
        with self.assertRaises(AccessError):
            row.with_user(self.admin_a).write({'row_count': 99})
        with self.assertRaises(AccessError):
            row.with_user(self.admin_a).unlink()
        self.assertEqual(row.row_count, 3)

    def test_meta_admin_sees_only_own_company_totals(self):
        """The record rule hides another company's totals from a Meta
        Admin reading the table directly."""
        own = self._totals_row(self.co_a)
        other = self._totals_row(self.co_b)
        Stats = self.env['meta.sync.stats'].with_user(self.admin_a)
        found = Stats.search([('id', 'in', (own | other).ids)])
        self.assertEqual(found, own)

    def test_sync_recent_row_keys_are_allowlisted(self):
        """Each sync_recent row has exactly status, meta_leadgen_id and
        create_date."""
        self._seed_window()
        m = self._metrics(period_mode='month',
                          date_from=self.win_from, date_to=self.win_to)
        self.assertIn('sync_recent', m)
        for row in m['sync_recent']:
            self.assertEqual(
                set(row.keys()), SYNC_RECENT_KEYS,
                "sync_recent row keys %r != allowlist %r"
                % (set(row.keys()), SYNC_RECENT_KEYS))
