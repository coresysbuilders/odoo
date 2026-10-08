# Meta Lead Ads Integration for Odoo — © 2026 CoreSys Builders. All rights reserved.
# Licensed under the Odoo Proprietary License v1.0 (OPL-1).
# Unauthorized copying, redistribution, or resale of this software, in whole or in
# part, via any medium, is strictly prohibited and constitutes a license violation.
# OPL-1: https://www.odoo.com/documentation/19.0/legal/licenses.html#odoo-apps

# v19.0 was sunset on 2026-05-21. Re-test the Lead Ads fields before bumping.
GRAPH_VERSION = 'v23.0'

# Applied to every Graph request.
CONNECT_TIMEOUT = 5     # seconds
READ_TIMEOUT = 30       # seconds; stops a hung Graph call from tying up a worker
GRAPH_BASE = 'https://graph.facebook.com'   # GRAPH_VERSION is appended by the client

# --------------------------------------------------------------------------- #
# Lead title template. Shared by the ingest service and Settings so the
# default and the token list can't drift apart.
# --------------------------------------------------------------------------- #

# ir.config_parameter key holding the active template.
LEAD_NAME_PARAM = 'meta_lead_ads.lead_name_template'

# The separator is a U+2022 bullet, matching the titles of leads created
# before templates existed. Don't retype it.
DEFAULT_LEAD_NAME = "Meta Lead • {form_name} • {date}"

# Tokens a template may use; anything else is rejected by the validator.
# The retroactive rename only trusts {date} on leads with a stored
# meta_submitted_at.
VALID_TOKENS = ('form_name', 'campaign_name', 'adset_name', 'ad_name',
                'contact_name', 'page_name', 'date')

# --------------------------------------------------------------------------- #
# New-lead notification settings (ir.config_parameter keys).
# --------------------------------------------------------------------------- #
NOTIFY_ENABLED_PARAM = 'meta_lead_ads.notify_enabled'
NOTIFY_TARGET_PARAM = 'meta_lead_ads.notify_target_type'   # 'user' | 'group'
NOTIFY_USER_PARAM = 'meta_lead_ads.notify_user_id'
NOTIFY_GROUP_PARAM = 'meta_lead_ads.notify_group_id'

# --------------------------------------------------------------------------- #
# Field mapping targets.
# --------------------------------------------------------------------------- #

# crm.lead field types a form question can be mapped to. html is left out so
# raw Meta answers never end up in stored HTML.
MAPPABLE_TTYPES = ('char', 'text', 'selection', 'boolean', 'integer', 'float',
                   'monetary', 'date', 'datetime', 'many2one', 'many2many')

# Fields a mapping may never write: ORM bookkeeping, fields that drive record
# rules or visibility, and this module's own attribution and idempotency
# columns.
MAPPING_FORBIDDEN_FIELDS = frozenset({
    'id', 'create_uid', 'create_date', 'write_uid', 'write_date',
    'active', 'type', 'company_id',
    'meta_leadgen_id', 'meta_name_is_generated', 'meta_phone_normalized',
    'meta_submitted_at', 'meta_platform',
    'meta_campaign_id', 'meta_campaign_name', 'meta_adset_id',
    'meta_adset_name', 'meta_ad_id', 'meta_ad_name', 'meta_form_id',
    'meta_form_name', 'meta_page_id', 'meta_page_name',
    'meta_form_id_ref', 'meta_page_id_ref', 'meta_account_id',
})
