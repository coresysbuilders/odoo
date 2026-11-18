# Meta Lead Ads Integration for Odoo — © 2026 CoreSys Builders. All rights reserved.
# Licensed under the Odoo Proprietary License v1.0 (OPL-1).
# Unauthorized copying, redistribution, or resale of this software, in whole or in
# part, via any medium, is strictly prohibited and constitutes a license violation.
# OPL-1: https://www.odoo.com/documentation/20.0/legal/licenses.html#odoo-apps

"""The uninstall hook removes the settings the module stamps at runtime.

Odoo's uninstall only deletes records with an external id. The run stamps
written through set_str have none, so the hook clears them. It is called
directly here: a real uninstall can't run inside a test transaction.
"""
from unittest import mock

from odoo.modules.module import get_manifest
from odoo.tests.common import TransactionCase, tagged

from odoo.addons import meta_lead_ads

_RUNTIME_KEYS = (
    'meta_lead_ads.drain_last_run',
    'meta_lead_ads.backfill_last_run',
    'meta_lead_ads.installed_at',
    'meta_lead_ads.webhook_last_ok',
)


@tagged('post_install', '-at_install')
class TestUninstallHook(TransactionCase):

    def setUp(self):
        super().setUp()
        self.Param = self.env['ir.config_parameter'].sudo()
        for key in _RUNTIME_KEYS:
            self.Param.set_str(key, 'stamp-value-%s' % key)
        # Matches the prefix only if '_' were left as a LIKE wildcard.
        self.Param.set_str('meta-lead-ads.lookalike', 'keep-me')
        self.Param.set_str('meta_lead_ads_other.key', 'keep-me-too')
        if not self.Param.get_str('web.base.url'):
            self.Param.set_str('web.base.url', 'http://localhost:8069')

    def _keys(self):
        return set(self.Param.search([]).mapped('key'))

    def test_hook_exported_and_registered(self):
        """The manifest names a hook the package actually exports."""
        manifest = get_manifest('meta_lead_ads')
        self.assertEqual(manifest.get('uninstall_hook'), 'uninstall_hook')
        self.assertTrue(callable(getattr(meta_lead_ads, 'uninstall_hook',
                                         None)))

    def test_removes_only_own_keys(self):
        """Every meta_lead_ads.* key goes; every other key stays, value
        unchanged."""
        others = {p.key: p.value for p in self.Param.search([])
                  if not p.key.startswith('meta_lead_ads.')}
        self.assertIn('web.base.url', others, "precondition")
        self.assertIn('meta-lead-ads.lookalike', others, "precondition")
        self.assertTrue(set(_RUNTIME_KEYS) <= self._keys(), "precondition")

        meta_lead_ads.uninstall_hook(self.env)

        self.assertFalse(
            [k for k in self._keys() if k.startswith('meta_lead_ads.')])
        after = {p.key: p.value for p in self.Param.search([])}
        self.assertEqual(after, others)
        for key in _RUNTIME_KEYS:
            self.assertFalse(self.Param.get_str(key))

    def test_logs_count_never_values(self):
        """The log line carries how many keys went, not their keys or
        values."""
        with mock.patch.object(meta_lead_ads, '_logger') as logger:
            meta_lead_ads.uninstall_hook(self.env)
        logger.info.assert_called_once()
        args = logger.info.call_args.args
        self.assertTrue(any(isinstance(a, int) and a >= len(_RUNTIME_KEYS)
                            for a in args[1:]), args)
        for call in logger.mock_calls:
            self.assertNotIn('stamp-value', repr(call))
            self.assertNotIn('meta_lead_ads.', repr(call))

    def test_second_run_is_harmless(self):
        """Nothing left to remove: the hook runs again without error."""
        meta_lead_ads.uninstall_hook(self.env)
        before = self._keys()
        meta_lead_ads.uninstall_hook(self.env)
        self.assertEqual(self._keys(), before)
