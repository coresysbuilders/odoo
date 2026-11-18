import odoo.tests


@odoo.tests.tagged('post_install', '-at_install')
class TestDashboardJs(odoo.tests.HttpCase):

    @odoo.tests.no_retry
    def test_dashboard_hoot_suite(self):
        # Runs only this module's Hoot tests in the unit-test runner.
        self.browser_js(
            '/web/tests?headless&loglevel=2&preset=desktop&timeout=15000'
            '&filter=%40meta_lead_ads',
            '', '', login='admin', timeout=1800,
            success_signal='[HOOT] Test suite succeeded',
            error_checker=lambda message: '[HOOT]' not in message,
        )
