# Meta Lead Ads Integration for Odoo — © 2026 CoreSys Builders. All rights reserved.
# Licensed under the Odoo Proprietary License v1.0 (OPL-1).
# Unauthorized copying, redistribution, or resale of this software, in whole or in
# part, via any medium, is strictly prohibited and constitutes a license violation.
# OPL-1: https://www.odoo.com/documentation/19.0/legal/licenses.html#odoo-apps

from odoo.tests.common import TransactionCase, tagged


@tagged('post_install', '-at_install')
class TestUtmSeed(TransactionCase):
    """The UTM records used for attribution exist under their external IDs."""

    def test_utm_external_ids_resolve(self):
        # Facebook is Odoo's stock source. Odoo ships no Instagram source or
        # Paid Social medium, so the module adds those.
        fb = self.env.ref('utm.utm_source_facebook')
        ig = self.env.ref('meta_lead_ads.utm_source_meta_instagram')
        ps = self.env.ref('meta_lead_ads.utm_medium_paid_social')
        self.assertEqual(fb._name, 'utm.source')
        self.assertEqual(fb.name, 'Facebook')
        self.assertEqual(ig._name, 'utm.source')
        self.assertEqual(ig.name, 'Instagram')
        self.assertEqual(ps._name, 'utm.medium')
        self.assertEqual(ps.name, 'Paid Social')
