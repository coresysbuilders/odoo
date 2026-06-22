# Meta Lead Ads Integration for Odoo — © 2026 CoreSys Builders. All rights reserved.
# Licensed under the Odoo Proprietary License v1.0 (OPL-1).
# Unauthorized copying, redistribution, or resale of this software, in whole or in
# part, via any medium, is strictly prohibited and constitutes a license violation.
# OPL-1: https://www.odoo.com/documentation/18.0/legal/licenses.html#odoo-apps

"""RED security tests for the Leads Analytics Dashboard backend method.

Pins the two security invariants of the not-yet-implemented
``meta.account.get_dashboard_metrics`` (Plan 02), per DASH-05 / DASH-07 and
11-REVIEWS.md (T-11-EoP, T-11-ID):

  (S1/S2 admin gate) A direct RPC-style call to ``get_dashboard_metrics`` as a
      non-admin MUST raise ``AccessError`` -- proving the in-method
      ``has_group('meta_lead_ads.group_meta_admin')`` gate is the real control,
      not merely the menu ``groups=`` visibility. TWO non-admin variants are
      asserted (Codex-LOW):
        * variant 1: base.group_user + group_meta_user (a Meta User, NOT admin);
        * variant 2: base.group_user only, NO meta groups at all.
      ``base.group_user`` is MANDATORY on both -- without it Odoo sets
      ``share=True`` (portal) and the gate would pass for the wrong reason
      (test_promotion_security.py:16-19).

  (S3 no-secret payload + allowlist) The admin-call return dict, recursively
      flattened, contains NO ``access_token`` / ``app_secret`` / ``raw_payload``
      keys and NO string value equal to a seeded secret (incl. health captions --
      Codex-MEDIUM secret-value caption). Plus an explicit ALLOWLIST
      (LOW-CONSENSUS): every ``health`` signal dict has EXACTLY
      {status, label, caption}; every ``sync_recent`` row dict has EXACTLY
      {status, meta_leadgen_id, create_date}.

RED now: the method does not exist (-> AttributeError). Turns GREEN in Plan 02.

Odoo 18 conventions: @tagged('post_install','-at_install'); single-class
assertRaises (never a tuple); search_count (no count=).
"""
from odoo.tests.common import TransactionCase, tagged
from odoo.exceptions import AccessError

from .test_dashboard_metrics import DashboardFixtureMixin

# The secret values seeded by IngestFixtureMixin.setUp (account app_secret /
# access_token + page access_token) -- none of these may appear in the payload.
SEEDED_SECRETS = ('secret_test', 'tok_acct', 'tok_test', 'app_test')

# Allowlisted key sets (LOW-CONSENSUS): the dashboard payload must not widen
# these shapes to expose extra (potentially sensitive) fields.
HEALTH_SIGNAL_KEYS = {'status', 'label', 'caption'}
SYNC_RECENT_KEYS = {'status', 'meta_leadgen_id', 'create_date'}


def _flatten(obj):
    """Yield (key, value) for every dict key and every scalar value reachable in
    a nested dict/list payload, so the secret scan misses nothing."""
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
class TestDashboardSecurity(DashboardFixtureMixin, TransactionCase):
    """Admin-gate (two non-admin variants) + no-secret-payload + health/
    sync_recent allowlist. Fails RED until Plan 02 implements the method."""

    def setUp(self):
        super().setUp()
        base_internal = self.env.ref('base.group_user')
        user_group = self.env.ref('meta_lead_ads.group_meta_user')
        # Variant 1: a Meta User (base.group_user + group_meta_user), NOT admin.
        self.non_admin_user = self.env['res.users'].create({
            'name': 'Dash NonAdmin', 'login': 'dash_nonadmin',
            'groups_id': [(6, 0, [base_internal.id, user_group.id])]})
        # Variant 2: an internal user with base.group_user ONLY, no meta groups.
        self.plain_internal = self.env['res.users'].create({
            'name': 'Dash Plain', 'login': 'dash_plain',
            'groups_id': [(6, 0, [base_internal.id])]})

    # ---- S1/S2: in-method has_group gate, two non-admin variants ----------

    def test_meta_user_non_admin_call_raises(self):
        """A Meta User (group_meta_user, NOT group_meta_admin) calling
        get_dashboard_metrics directly raises AccessError -- the RPC cannot bypass
        the menu groups=. Single-class assertRaises, NO tuple."""
        with self.assertRaises(AccessError):
            self.Account.with_user(
                self.non_admin_user).get_dashboard_metrics()

    def test_plain_internal_non_admin_call_raises(self):
        """An internal user with NO meta groups at all calling
        get_dashboard_metrics directly raises AccessError -- proving the backend
        has_group gate is the real control, not menu visibility."""
        with self.assertRaises(AccessError):
            self.Account.with_user(
                self.plain_internal).get_dashboard_metrics()

    # ---- S3: no-secret payload (recursive flatten incl. health captions) --

    def test_admin_payload_has_no_secret_keys_or_values(self):
        """The admin-call payload contains NO access_token / app_secret /
        raw_payload key and NO string value equal to a seeded secret token /
        app-secret (recursively, incl. any health caption)."""
        self._seed_window()
        m = self._metrics(period_mode='month',
                          date_from=self.win_from, date_to=self.win_to)
        keys = {v for (kind, v) in _flatten(m) if kind == 'key'}
        for forbidden in ('access_token', 'app_secret', 'raw_payload'):
            self.assertNotIn(forbidden, keys)
        values = [v for (kind, v) in _flatten(m)
                  if kind == 'value' and isinstance(v, str)]
        for secret in SEEDED_SECRETS:
            for val in values:
                self.assertNotIn(secret, val)

    # ---- S3: explicit allowlist on health + sync_recent -------------------

    def test_health_signal_keys_are_allowlisted(self):
        """Every health signal dict exposes EXACTLY {status, label, caption} --
        no extra keys that could leak token/expiry/account metadata."""
        self._seed_window()
        m = self._metrics(period_mode='month',
                          date_from=self.win_from, date_to=self.win_to)
        self.assertIn('health', m)
        for signal_name, signal in m['health'].items():
            self.assertEqual(
                set(signal.keys()), HEALTH_SIGNAL_KEYS,
                "health[%r] keys %r != allowlist %r"
                % (signal_name, set(signal.keys()), HEALTH_SIGNAL_KEYS))

    def test_sync_recent_row_keys_are_allowlisted(self):
        """Every sync_recent row exposes EXACTLY
        {status, meta_leadgen_id, create_date} -- never raw_payload or any other
        field."""
        self._seed_window()
        m = self._metrics(period_mode='month',
                          date_from=self.win_from, date_to=self.win_to)
        self.assertIn('sync_recent', m)
        for row in m['sync_recent']:
            self.assertEqual(
                set(row.keys()), SYNC_RECENT_KEYS,
                "sync_recent row keys %r != allowlist %r"
                % (set(row.keys()), SYNC_RECENT_KEYS))
