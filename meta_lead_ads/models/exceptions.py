# Meta Lead Ads Integration for Odoo — © 2026 CoreSys Builders. All rights reserved.
# Licensed under the Odoo Proprietary License v1.0 (OPL-1).
# Unauthorized copying, redistribution, or resale of this software, in whole or in
# part, via any medium, is strictly prohibited and constitutes a license violation.
# OPL-1: https://www.odoo.com/documentation/19.0/legal/licenses.html#odoo-apps

# Typed exception hierarchy; field set mirrors the Meta error envelope
# (error-handling + rate-limiting).
#
# Plain Python — these are stdlib Exception subclasses, deliberately not an
# Odoo ORM class (no Odoo imports here). The meta.graph.client imports and
# raises these; callers `except` the typed classes and never inspect raw
# Meta JSON.
#
# The base carries richer optional metadata
# (error_type/error_user_title/error_user_msg/is_transient) for later
# webhook/cron observability. Every metadata kwarg is keyword-only-with-default,
# so existing positional usage stays unchanged (non-breaking).


class MetaGraphError(Exception):
    """Base class for every Meta Graph transport error.

    Carries the raw envelope metadata so callers (alerting / retry) can branch
    without re-parsing Meta JSON. All metadata kwargs default to ``None`` —
    positional usage like
    ``MetaPermanentError(msg, code=100, subcode=33, fbtrace_id=...)`` is
    unchanged by the optional-metadata additions.
    """

    def __init__(self, message, code=None, subcode=None, fbtrace_id=None,
                 error_type=None, error_user_title=None, error_user_msg=None,
                 is_transient=None):
        super().__init__(message)
        self.code = code
        self.subcode = subcode
        self.fbtrace_id = fbtrace_id
        # Richer metadata for observability.
        self.error_type = error_type                # envelope "type" (e.g. OAuthException)
        self.error_user_title = error_user_title     # envelope "error_user_title"
        self.error_user_msg = error_user_msg         # envelope "error_user_msg"
        self.is_transient = is_transient             # envelope "is_transient" flag


class MetaTransientError(MetaGraphError):
    """HTTP 5xx, connection/timeout, or an is_transient:true envelope — safe to retry later."""


class MetaPermanentError(MetaGraphError):
    """4xx app/param errors, non-JSON/empty bodies — won't fix themselves on retry."""


class MetaAuthError(MetaGraphError):
    """OAuthException code 190 (+subcodes 458/459/460/463/464/467/492). Alerting, never retried."""


class MetaRateLimitError(MetaGraphError):
    """Throttle codes 4/17/32/613/80001/80006 + any 429. Carries the backoff hint for retries."""

    def __init__(self, message, code=None, subcode=None,
                 app_usage=None, buc_usage=None, retry_after_min=None,
                 fbtrace_id=None, error_type=None,
                 error_user_title=None, error_user_msg=None, is_transient=None):
        super().__init__(
            message, code=code, subcode=subcode, fbtrace_id=fbtrace_id,
            error_type=error_type, error_user_title=error_user_title,
            error_user_msg=error_user_msg, is_transient=is_transient,
        )
        self.app_usage = app_usage              # raw X-App-Usage JSON string
        # Keep the raw BUC header even when minute-parsing fails, so
        # observability does not lose the throttle context.
        self.buc_usage = buc_usage              # raw X-Business-Use-Case-Usage JSON string
        self.retry_after_min = retry_after_min  # estimated_time_to_regain_access, minutes (or None)
