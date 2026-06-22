# Meta Lead Ads Integration for Odoo — © 2026 CoreSys Builders. All rights reserved.
# Licensed under the Odoo Proprietary License v1.0 (OPL-1).
# Unauthorized copying, redistribution, or resale of this software, in whole or in
# part, via any medium, is strictly prohibited and constitutes a license violation.
# OPL-1: https://www.odoo.com/documentation/18.0/legal/licenses.html#odoo-apps

from odoo.tests.common import TransactionCase, tagged
from odoo.addons.meta_lead_ads.models.const import GRAPH_VERSION


@tagged('post_install', '-at_install')
class TestConstant(TransactionCase):
    def test_graph_version_constant(self):
        self.assertEqual(GRAPH_VERSION, 'v23.0')
