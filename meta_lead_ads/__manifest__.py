# Meta Lead Ads Integration for Odoo — © 2026 CoreSys Builders. All rights reserved.
# Licensed under the Odoo Proprietary License v1.0 (OPL-1).
# Unauthorized copying, redistribution, or resale of this software, in whole or in
# part, via any medium, is strictly prohibited and constitutes a license violation.
# OPL-1: https://www.odoo.com/documentation/18.0/legal/licenses.html#odoo-apps

{
    'name': 'Facebook Lead Ads to CRM',
    'version': '18.0.13.3.5',
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
    # Paid app on apps.odoo.com. The portal listing price is authoritative; these
    # keys pre-fill it. OPL-1 (license below) makes this a proprietary paid app.
    'price': 30.00,
    'currency': 'USD',
    # NOTE: no 'website' key — when set, the Apps card "Learn More" link
    # redirects out to that URL instead of opening this module's own
    # description page (static/description/index.html). Company info lives in
    # the description instead.
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
        # Loaded after meta_menus.xml and meta_onboarding_views.xml so the three
        # <delete model="ir.ui.menu"> ids still resolve in the same upgrade, and
        # so the field-mapping and settings actions the settings view references
        # next are already defined.
        'views/meta_settings_menu.xml',
        # The <app> settings form references %(...)d actions defined just above
        # and earlier in the list, so it must parse last.
        'views/res_config_settings_views.xml',
        # Phase 11: client action + top-level admin-gated "Meta Leads" menu.
        # group_meta_admin is defined first (security/meta_security.xml), so the
        # menuitem groups= references resolve at parse time.
        'views/meta_dashboard.xml',
    ],
    # Phase 11: first-ever asset bundle — the read-only Leads Analytics Dashboard
    # OWL component, its QWeb template, and brand SCSS. The QWeb template lives in
    # the bundle (NOT the data list above).
    'assets': {
        'web.assets_backend': [
            'meta_lead_ads/static/src/dashboard/**/*.js',
            'meta_lead_ads/static/src/dashboard/**/*.xml',
            'meta_lead_ads/static/src/dashboard/**/*.scss',
        ],
    },
    'external_dependencies': {'python': []},
    # Apps-store / description-page imagery. The FIRST entry is the wide CoreSys
    # cover banner (optimized to ~270 KB so it loads fast) — it leads the
    # description page (index.html) and is the main store image. The remaining
    # entries are the in-product screenshots shown in the apps.odoo.com gallery;
    # they are the same files embedded in the description walkthrough. The small
    # square Apps-list icon is static/description/icon.png (not listed here).
    'images': [
        'static/description/cover.jpg',
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
