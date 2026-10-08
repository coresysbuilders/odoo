# Meta Lead Ads Integration for Odoo — © 2026 CoreSys Builders. All rights reserved.
# Licensed under the Odoo Proprietary License v1.0 (OPL-1).
# Unauthorized copying, redistribution, or resale of this software, in whole or in
# part, via any medium, is strictly prohibited and constitutes a license violation.
# OPL-1: https://www.odoo.com/documentation/19.0/legal/licenses.html#odoo-apps

# Errors raised by meta.graph.client, one class per kind of Graph failure, so
# callers can catch by type instead of reading Meta's error JSON. Plain Python
# exceptions, no Odoo imports.


class MetaGraphError(Exception):
    """Base class for Graph API errors.

    Carries the fields of Meta's error envelope. All of them are optional
    keyword arguments.
    """

    def __init__(self, message, code=None, subcode=None, fbtrace_id=None,
                 error_type=None, error_user_title=None, error_user_msg=None,
                 is_transient=None):
        super().__init__(message)
        self.code = code
        self.subcode = subcode
        self.fbtrace_id = fbtrace_id
        self.error_type = error_type                # envelope "type" (e.g. OAuthException)
        self.error_user_title = error_user_title     # envelope "error_user_title"
        self.error_user_msg = error_user_msg         # envelope "error_user_msg"
        self.is_transient = is_transient             # envelope "is_transient" flag


class MetaTransientError(MetaGraphError):
    """HTTP 5xx, connection or timeout errors, or is_transient=true. Safe to retry."""


class MetaPermanentError(MetaGraphError):
    """4xx app/parameter errors and empty or non-JSON bodies. Retrying won't help."""


class MetaAuthError(MetaGraphError):
    """OAuthException code 190 (subcodes 458/459/460/463/464/467/492). Needs a new token; not retried."""


class MetaRateLimitError(MetaGraphError):
    """Throttling (codes 4/17/32/613/80001/80006, or HTTP 429), with the backoff hint."""

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
        # Kept raw even when the retry time can't be parsed out of it.
        self.buc_usage = buc_usage              # raw X-Business-Use-Case-Usage JSON string
        self.retry_after_min = retry_after_min  # estimated_time_to_regain_access, minutes (or None)
