# Meta Lead Ads Integration for Odoo — © 2026 CoreSys Builders. All rights reserved.
# Licensed under the Odoo Proprietary License v1.0 (OPL-1).
# Unauthorized copying, redistribution, or resale of this software, in whole or in
# part, via any medium, is strictly prohibited and constitutes a license violation.
# OPL-1: https://www.odoo.com/documentation/19.0/legal/licenses.html#odoo-apps

# Errors raised by meta.graph.client, one class per kind of Graph failure, so
# callers can catch by type instead of reading Meta's error JSON. Plain Python
# exceptions, no Odoo imports.

import re

_MASK = '***'

# Meta's error text can echo the request, so token- and secret-shaped values
# are masked before anything keeps it. Applied in order. Meta lead, page and
# form ids are 15-17 digits, well under the hex and base64 run lengths below.
_SECRET_PATTERNS = (
    # key=value, key: value, JSON "key":"value" and URL-encoded key%3Dvalue.
    # The value ends at '&', an encoded '&' (%26) or a usual delimiter. A key
    # right after %26 has no word boundary, hence the lookbehind.
    (re.compile(
        r'(?i)(?:(?<=%26)|\b)(access_token|input_token|fb_exchange_token|'
        r'refresh_token|client_secret|appsecret_proof|app_secret)\b'
        r'(["\']?\s*(?:[=:]|%3[dD])\s*["\']?)'
        r'(?:(?!%26)[^\s&"\',;}])+'),
     r'\1\2' + _MASK),
    (re.compile(r'(?i)\bbearer\s+[A-Za-z0-9._~+/=-]+'), 'Bearer ' + _MASK),
    # User and Page tokens.
    (re.compile(r'\bEAA[A-Za-z0-9]{20,}'), _MASK),
    # App access token, app_id|app_secret.
    (re.compile(r'\b\d{6,}\|[A-Za-z0-9_-]{16,}'), _MASK),
    # App secret (32 hex) and appsecret_proof (64 hex).
    (re.compile(r'\b[0-9a-fA-F]{32,}\b'), _MASK),
    # Any other long base64 or url-safe run.
    (re.compile(r'[A-Za-z0-9+/_-]{40,}={0,2}'), _MASK),
)


def scrub_secrets(text):
    """Return ``text`` with token- and secret-shaped values replaced by '***'.

    Anything that is not a string (None included) comes back unchanged.
    """
    if not isinstance(text, str):
        return text
    for pattern, replacement in _SECRET_PATTERNS:
        text = pattern.sub(replacement, text)
    return text


class MetaGraphError(Exception):
    """Base class for Graph API errors.

    Carries the fields of Meta's error envelope. All of them are optional
    keyword arguments.
    """

    def __init__(self, message, code=None, subcode=None, fbtrace_id=None,
                 error_type=None, error_user_title=None, error_user_msg=None,
                 is_transient=None):
        # Every caller stores str(e), so scrubbing once here covers the sync
        # log, the webhook queue, backfill failures and Restore errors.
        super().__init__(scrub_secrets(message))
        self.code = code
        self.subcode = subcode
        self.fbtrace_id = fbtrace_id
        self.error_type = error_type                # envelope "type" (e.g. OAuthException)
        self.error_user_title = scrub_secrets(error_user_title)  # envelope "error_user_title"
        self.error_user_msg = scrub_secrets(error_user_msg)      # envelope "error_user_msg"
        self.is_transient = is_transient             # envelope "is_transient" flag


class MetaTransientError(MetaGraphError):
    """HTTP 5xx, connection or timeout errors, or is_transient=true. Safe to retry."""


class MetaIngestRaceError(MetaTransientError):
    """Another transaction stored the same record after ours started, so it is not visible yet. A retry in a new transaction finds it."""


class MetaPermanentError(MetaGraphError):
    """4xx app/parameter errors and empty or non-JSON bodies. Retrying won't help."""


class MetaAuthError(MetaGraphError):
    """OAuthException code 190 (subcodes 458/459/460/463/464/467/492). Needs a new token; not retried."""


# Not a Meta failure, so it does not extend the permanent error: a handler
# for real failures must never record it as one.
class MetaLeadRemovedError(MetaGraphError):
    """The lead was deleted in Odoo; nothing is fetched or created."""


class MetaRateLimitError(MetaGraphError):
    """Throttling (codes 4/17/32/613/80001/80005/80006, or HTTP 429), with the backoff hint."""

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
