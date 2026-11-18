# Meta Lead Ads Integration for Odoo — © 2026 CoreSys Builders. All rights reserved.
# Licensed under the Odoo Proprietary License v1.0 (OPL-1).
# Unauthorized copying, redistribution, or resale of this software, in whole or in
# part, via any medium, is strictly prohibited and constitutes a license violation.
# OPL-1: https://www.odoo.com/documentation/20.0/legal/licenses.html#odoo-apps

from . import test_constant
from . import test_hierarchy
from . import test_mapping
from . import test_security
from . import test_graph_client
from . import test_name_cache
from . import test_onboarding
from . import test_meta_lead
from . import test_sync_log
from . import test_sync_log_security
from . import test_utm_seed
from . import test_ingest
from . import test_webhook_controller
from . import test_webhook_drain
from . import test_webhook_subscription
from . import test_webhook_security
from . import test_backfill
from . import test_token_health
from . import test_sync_retry
from . import test_regressions
from . import test_cron_health
from . import test_promotion
from . import test_promotion_security
from . import test_lead_naming
from . import test_retro_rename
from . import test_lead_notify
from . import test_dashboard_metrics
from . import test_dashboard_security
from . import test_lead_data_gaps
from . import test_answer_fidelity
from . import test_dedup_fidelity
from . import test_multi_company
from . import test_multi_company_ownership
from . import test_multi_company_ingest
from . import test_migration_batching
from . import test_dashboard_js
from . import test_ingest_integrity
from . import test_failure_recovery
from . import test_failure_recovery_concurrency
from . import test_archived_page
from . import test_removed_leads
from . import test_removed_leads_concurrency
from . import test_restricted_users
from . import test_mapping_targets
from . import test_page_routing
from . import test_privacy_access
from . import test_privacy_erase
from . import test_privacy_erase_blocks
from . import test_app_conflict
from . import test_single_app_race
from . import test_promotion_ownership
from . import test_uninstall_hook
from . import test_published_claims
from . import test_hygiene
from . import test_sync_guards
