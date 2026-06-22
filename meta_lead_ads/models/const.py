# Meta Lead Ads Integration for Odoo — © 2026 CoreSys Builders. All rights reserved.
# Licensed under the Odoo Proprietary License v1.0 (OPL-1).
# Unauthorized copying, redistribution, or resale of this software, in whole or in
# part, via any medium, is strictly prohibited and constitutes a license violation.
# OPL-1: https://www.odoo.com/documentation/18.0/legal/licenses.html#odoo-apps

# Pin a single GRAPH_VERSION constant. v19.0 sunset 2026-05-21 (past); do not
# use it. Re-test Lead Ads fields before any bump.
GRAPH_VERSION = 'v23.0'

# Enforced on every Graph call by the client.
CONNECT_TIMEOUT = 5     # seconds — fail fast on a dead host
READ_TIMEOUT = 30       # seconds — bounded read; keeps a hung Graph call from pinning a worker
GRAPH_BASE = 'https://graph.facebook.com'   # base host; the client appends GRAPH_VERSION

# --------------------------------------------------------------------------- #
# Lead-name composition constants.
#
# Defined here (the public const module) so BOTH the create path
# (meta_lead_ingest.render_lead_name / _create_lead) AND the Settings model
# (res_config_settings preview + retro-rename) import the SAME
# literals. Re-declaring the U+2022 bullet default or the token set in two
# files would let them drift.
# --------------------------------------------------------------------------- #

# ir.config_parameter key under which the active lead-name template is stored.
LEAD_NAME_PARAM = 'meta_lead_ads.lead_name_template'

# Default template — reproduces today's hardcoded title byte-for-byte. The
# separator is a real U+2022 BULLET (UTF-8 E2 80 A2), copied verbatim from the
# original "Meta Lead • %s • %s" literal; do NOT retype it (any
# drift in this byte breaks the byte-for-byte default contract).
DEFAULT_LEAD_NAME = "Meta Lead • {form_name} • {date}"

# The complete set of tokens a template may legally reference. The validator
# rejects any other braced body; render only substitutes these.
# {date} is included for the forward/preview path but the retro-rename guard
# refuses to trust it (no stored Meta create-date).
VALID_TOKENS = ('form_name', 'campaign_name', 'adset_name', 'ad_name',
                'contact_name', 'page_name', 'date')

# --------------------------------------------------------------------------- #
# New-lead notification settings (ir.config_parameter keys).
#
# When enabled, a fresh Meta lead notifies either one user or every active
# member of a chosen group. Stored as plain ir.config_parameter values so the
# Settings model and the ingestion service read the same keys.
# --------------------------------------------------------------------------- #
NOTIFY_ENABLED_PARAM = 'meta_lead_ads.notify_enabled'
NOTIFY_TARGET_PARAM = 'meta_lead_ads.notify_target_type'   # 'user' | 'group'
NOTIFY_USER_PARAM = 'meta_lead_ads.notify_user_id'
NOTIFY_GROUP_PARAM = 'meta_lead_ads.notify_group_id'
