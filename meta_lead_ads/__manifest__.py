# Meta Lead Ads Integration for Odoo — © 2026 CoreSys Builders. All rights reserved.
# Licensed under the Odoo Proprietary License v1.0 (OPL-1).
# Unauthorized copying, redistribution, or resale of this software, in whole or in
# part, via any medium, is strictly prohibited and constitutes a license violation.
# OPL-1: https://www.odoo.com/documentation/18.0/legal/licenses.html#odoo-apps

{
    'name': 'Facebook Lead Ads to CRM',
    'version': '18.0.13.4.1',
    'category': 'Sales/CRM',
    'license': 'OPL-1',
    'summary': 'Bring Facebook & Instagram Lead Ads straight into Odoo CRM. A '
               'signature-verified webhook plus scheduled backfill capture every '
               'lead exactly once, enriched with full campaign attribution '
               '(campaign, ad set, ad, form mapped to UTM), lossless custom-question '
               'capture, and a built-in leads analytics dashboard.',
    'author': 'CoreSys Builders',
    'maintainer': 'CoreSys Builders',
    'support': 'info@coresysbuilders.com',
    # Paid app on apps.odoo.com. The price set on the portal wins; these keys
    # only pre-fill it.
    'price': 30.00,
    'currency': 'USD',
    # No 'website' key on purpose: with one set, the Apps "Learn More" link
    # leaves Odoo instead of opening static/description/index.html.
    'depends': ['crm', 'utm', 'mail'],
    'post_init_hook': 'post_init_hook',
    'data': [
        'security/meta_security.xml',
        'security/ir.model.access.csv',
        'data/utm_data.xml',
        'data/meta_webhook_cron.xml',
        'data/meta_backfill_cron.xml',
        'views/meta_account_views.xml',
        'views/meta_page_views.xml',
        'views/meta_lead_form_views.xml',
        'views/meta_field_mapping_views.xml',
        'wizard/meta_onboarding_views.xml',
        'wizard/meta_ingest_leadgen_views.xml',
        'wizard/meta_promote_answer_views.xml',
        'views/crm_lead_views.xml',
        'views/meta_sync_log_views.xml',
        'views/meta_webhook_event_views.xml',
        'views/meta_menus.xml',
        # Must come after meta_menus.xml and meta_onboarding_views.xml: its
        # <delete model="ir.ui.menu"> records need those menu ids to exist.
        'views/meta_settings_menu.xml',
        # References %(...)d actions from the files above, so it loads after them.
        'views/res_config_settings_views.xml',
        # Dashboard client action and the admin-only "Meta Leads" menu.
        'views/meta_dashboard.xml',
    ],
    # Dashboard OWL component, template and styles. The QWeb template goes in
    # the asset bundle, not in 'data'.
    'assets': {
        'web.assets_backend': [
            'meta_lead_ads/static/src/dashboard/**/*.js',
            'meta_lead_ads/static/src/dashboard/**/*.xml',
            'meta_lead_ads/static/src/dashboard/**/*.scss',
        ],
    },
    'external_dependencies': {'python': []},
    # The first image is the store cover (animated GIF); the rest are the
    # gallery screenshots also used in index.html. cover.jpg stays on disk as a
    # static fallback, and the Apps icon is static/description/icon.png.
    'images': [
        'static/description/cover.gif',
        'static/description/screenshots/01-dashboard.jpg',
        'static/description/screenshots/02-settings.jpg',
        'static/description/screenshots/03-connection.jpg',
        'static/description/screenshots/04-lead-attribution.jpg',
        'static/description/screenshots/05-field-mapping.jpg',
        'static/description/screenshots/06-sync-logs.jpg',
    ],
    'installable': True,
    'application': False,
}
