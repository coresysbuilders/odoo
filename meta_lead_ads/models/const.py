# Meta Lead Ads Integration for Odoo — © 2026 CoreSys Builders. All rights reserved.
# Licensed under the Odoo Proprietary License v1.0 (OPL-1).
# Unauthorized copying, redistribution, or resale of this software, in whole or in
# part, via any medium, is strictly prohibited and constitutes a license violation.
# OPL-1: https://www.odoo.com/documentation/20.0/legal/licenses.html#odoo-apps

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
# Retention (ir.config_parameter keys, in days).
# --------------------------------------------------------------------------- #

# Success and skipped sync-log rows.
SYNC_LOG_RETENTION_PARAM = 'meta_lead_ads.sync_log_retention_days'
# Webhook queue rows that were processed.
QUEUE_DONE_RETENTION_PARAM = 'meta_lead_ads.queue_done_retention_days'
# Failed webhook queue rows, and the stored Meta payload of failed sync-log
# rows. Meta serves a lead for about 90 days, so a payload older than that
# can't help a retry much anyway.
FAILED_RETENTION_PARAM = 'meta_lead_ads.failed_retention_days'
# The minimum is 1 day. There is no "keep forever" value: 0, a negative
# number or junk falls back to these defaults.
RETENTION_DEFAULTS = {
    SYNC_LOG_RETENTION_PARAM: 14,
    QUEUE_DONE_RETENTION_PARAM: 14,
    FAILED_RETENTION_PARAM: 90,
}
# The maximum is ten years. A longer stored value also falls back to the
# default: far enough out, the cleanup can't even work out the cut-off
# date, and it would stop every retention step.
RETENTION_MAX_DAYS = 3650

# --------------------------------------------------------------------------- #
# Scheduler and webhook health (ir.config_parameter keys and thresholds).
# --------------------------------------------------------------------------- #

# Each job stamps its own key when it runs, so a stopped backfill can't hide
# behind a running drain. They live in system parameters, not the sync log,
# so the log cleanup can't erase them. A job writes each key at most once a
# run, because every write clears the parameter cache on every worker.
DRAIN_RUN_PARAM = 'meta_lead_ads.drain_last_run'
# The drain runs every minute but refreshes its stamp at most this often.
# Far below the 15 minutes after which the scheduler reads as stopped.
DRAIN_STAMP_MINUTES = 5
BACKFILL_RUN_PARAM = 'meta_lead_ads.backfill_last_run'
# Last drain run that finished at least one webhook event, either ingested or
# settled because the lead was removed in Odoo. Failures don't count.
WEBHOOK_OK_PARAM = 'meta_lead_ads.webhook_last_ok'
# A webhook success older than this shows as quiet rather than running.
WEBHOOK_FRESH_HOURS = 24
# With no webhook success recorded, only failures this recent turn the
# webhook pill red.
WEBHOOK_FAIL_LOOKBACK_DAYS = 7
# A webhook event still pending after this long means the drain is behind.
QUEUE_BACKLOG_MINUTES = 10
# The backfill counts as stalled after this many of its own intervals without
# a run, read from the cron at check time, so an admin who slows it down
# doesn't get a false alarm.
BACKFILL_STALE_FACTOR = 3

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
    'meta_attribution_pending', 'meta_attribution_attempts',
    'meta_attribution_retry_after',
})

# Plumbing fields that are no longer offered as mapping targets. Kept apart
# from MAPPING_FORBIDDEN_FIELDS because rows saved before this list existed
# may still point at them, and those rows keep filling them on ingest. Some
# are already readonly or computed today; they are listed anyway so a later
# Odoo making them writable does not offer them.
MAPPING_TECHNICAL_PREFIXES = (
    'message_', 'activity_', 'website_message', 'rating_', 'iap_',
    'reveal_', 'lead_mining_',
)
MAPPING_TECHNICAL_FIELDS = frozenset({
    'color', 'stage_id_color',
    'date_open', 'date_last_stage_update', 'date_automation_last',
    'date_closed', 'date_conversion',
    'lead_mining_request_id', 'reveal_id',
    'email_normalized', 'email_domain_criterion', 'email_state',
    'phone_sanitized', 'phone_state', 'phone_mobile_search',
    'automated_probability', 'is_automated_probability', 'won_status',
    'is_blacklisted', 'phone_blacklisted', 'mobile_blacklisted',
    'duplicate_lead_ids', 'duplicate_lead_count',
    'show_enrich_button', 'calendar_event_ids', 'sequence',
})

# Workflow and create-path fields a form answer may never fill. The
# salesperson and team come from the Page, UTM from attribution and the
# title from the lead name template. Unlike the technical fields above,
# rows on these are refused outright and switched off on upgrade.
MAPPING_BLOCKED_FIELDS = frozenset({
    'name', 'user_id', 'team_id', 'stage_id', 'probability', 'priority',
    'lost_reason_id', 'date_deadline', 'expected_revenue', 'partner_id',
    'campaign_id', 'source_id', 'medium_id',
})
# All recurring revenue fields, including the plan, and any added later.
MAPPING_BLOCKED_PREFIXES = ('recurring_',)

# website_crm_partner_assign adds these. The assigned partner's portal users
# can read the lead, so an answer must not pick them. Listed by name so they
# are refused whether or not that module is installed.
MAPPING_BLOCKED_ADDON_FIELDS = frozenset({
    'partner_assigned_id', 'date_partner_assign',
})
# A link to a user or contact decides who owns or sees the lead, so any field
# pointing there is refused, including ones later add-ons bring.
MAPPING_BLOCKED_COMODELS = frozenset({'res.users', 'res.partner'})


def is_blocked_target(name, comodel=None):
    """True when a form answer may never fill crm.lead field ``name``.

    The picker, the save checks and ingest all ask this, so a field added
    here is refused everywhere at once. Callers pass ``comodel``, the model
    a relational field points at, so links to users and contacts are
    refused whatever the field is called.
    """
    return bool(name) and (name in MAPPING_FORBIDDEN_FIELDS
                           or name in MAPPING_BLOCKED_FIELDS
                           or name in MAPPING_BLOCKED_ADDON_FIELDS
                           or name.startswith(MAPPING_BLOCKED_PREFIXES)
                           or comodel in MAPPING_BLOCKED_COMODELS)

# --------------------------------------------------------------------------- #
# Attribution name retry.
# --------------------------------------------------------------------------- #

# After this many failed rounds the retry job leaves a lead alone. The pending
# flag stays visible and a Meta Admin can start the retries again.
ATTRIBUTION_RETRY_CAP = 10
ATTRIBUTION_RETRY_BATCH = 100
# A Page whose token is refused is skipped for this long.
ATTRIBUTION_AUTH_COOLDOWN_HOURS = 6
# Seconds of work per cron run. Stays under the default 120 s cron time
# limit even with one slow request still in flight.
ATTRIBUTION_RETRY_TIME_BUDGET = 60
# This many leads in a row failing only on timeouts or Meta 5xx ends the run.
ATTRIBUTION_TRANSIENT_STREAK = 3
# A lead that only hit timeouts or Meta 5xx waits this long before its next
# try, so it doesn't keep its place at the front of the queue. Two cron
# intervals.
ATTRIBUTION_TRANSIENT_BACKOFF_HOURS = 1

# --------------------------------------------------------------------------- #
# Meta rate-limit backoff.
# --------------------------------------------------------------------------- #

# Wait used when Meta sends no usable estimate.
RATE_LIMIT_FALLBACK_MINUTES = 5
# Longest wait taken from Meta's estimate. A longer one is cut to this and
# logged, so a garbled header can't park leads for years. If Meta still
# refuses after that, its next answer sets a new wait.
RATE_LIMIT_MAX_MINUTES = 24 * 60
# Throttles that only cover one Page. Any other throttle is treated as
# covering the whole app.
RATE_LIMIT_PAGE_CODES = frozenset({32, 80001, 80005})

# Most rows one Retry click sends to Meta, and the seconds after which it
# starts no more. Each row can take a Graph call of up to READ_TIMEOUT, and
# the click has to finish well inside the worker's time limit. The rest stay
# failed and are reported, so the admin can retry them again.
RETRY_BATCH_LIMIT = 20
RETRY_TIME_BUDGET = 60

# --------------------------------------------------------------------------- #
# Cron backfill.
# --------------------------------------------------------------------------- #

# This many leads in a row failing in one form stop that form for the run, so
# a bug can't write a failure row for every lead of a backlog.
BACKFILL_FAILURE_STREAK = 5
# Incomplete listings in a row before the form shows a failure row.
BACKFILL_STUCK_SWEEPS = 3
# Window read per sweep while a form catches up after an incomplete listing.
# One slice per 15-minute run is about 96 days of backlog per day, so a year
# takes about four days. That is fine: the webhook keeps delivering new leads
# meanwhile.
BACKFILL_SLICE_HOURS = 24
# Shortest slice the catch-up narrows to when a whole day still doesn't list
# in one run. Same floor as the wide sweep, for the same reason.
BACKFILL_MIN_SLICE_HOURS = 1
# Shortest window the daily wide sweep narrows to after an incomplete read.
# Below an hour the per-read overhead outweighs the gain, and leads already
# stored are skipped anyway, so re-reading the same hour still moves on.
RECONCILE_MIN_SLICE_HOURS = 1
# Seconds of work per cron run. Checked before each form, each listing page
# after the first, each lead and each name lookup, so no Graph call starts
# after it and at most one is still in flight (CONNECT_TIMEOUT + READ_TIMEOUT
# = 35 s). 45 + 35 = about 80 s plus a few seconds of database work, well
# under the default 120 s cron limit (limit_time_real_cron falls back to
# limit_time_real). A killed run rolls back everything. Lower than the 60 s
# of the other jobs because one lead can make up to five Graph calls (the
# fetch plus four name lookups).
BACKFILL_TIME_BUDGET = 45
# Same reasoning for the webhook drain: it takes up to 50 events a run, each
# one a lead read plus name lookups. Checked before each event and each name
# lookup; events left over stay pending for the next run, a minute later.
DRAIN_TIME_BUDGET = 45
