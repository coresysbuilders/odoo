# Meta Lead Ads Integration for Odoo — © 2026 CoreSys Builders. All rights reserved.
# Licensed under the Odoo Proprietary License v1.0 (OPL-1).
# Unauthorized copying, redistribution, or resale of this software, in whole or in
# part, via any medium, is strictly prohibited and constitutes a license violation.
# OPL-1: https://www.odoo.com/documentation/18.0/legal/licenses.html#odoo-apps

# Meta Graph API transport. Every Graph URL in the module is built here, against
# the pinned GRAPH_VERSION. Errors are classified from the response envelope into
# typed exceptions. No retry or backoff at this level; callers decide.
import hashlib
import hmac
import json
import logging

import requests

from odoo import api, models

# The URL reads const.GRAPH_VERSION at call time (not the imported name) so
# tests can patch the version and the built URL follows.
from . import const
from .const import GRAPH_VERSION, CONNECT_TIMEOUT, READ_TIMEOUT, GRAPH_BASE  # noqa: F401
from .exceptions import (
    MetaAuthError, MetaRateLimitError, MetaTransientError, MetaPermanentError,
)

_logger = logging.getLogger(__name__)

_SENSITIVE_PARAM_KEYS = {
    'access_token',
    'input_token',
    'client_secret',
    'fb_exchange_token',
}

# Graph throttle codes. 80006 is the LeadGen form throttle and 80001 the
# Page/system-user one; missing either would skip backoff and burn the quota.
RATE_LIMIT_CODES = {4, 17, 32, 613, 80001, 80006}
AUTH_CODE = 190

# Cap on pages per _iter_paged sweep, in case a cursor never terminates. Far
# above anything the forms/leads/pages edges return in practice.
_MAX_PAGES = 1000


def _redact(params):
    """Return a copy of ``params`` with secret values masked as '***'."""
    safe = dict(params or {})
    for key in list(safe):
        lowered = str(key or '').lower()
        if (lowered in _SENSITIVE_PARAM_KEYS or
                lowered.endswith('_token') or 'secret' in lowered):
            safe[key] = '***'
    return safe


def _buc_minutes(buc_header):
    """Return the largest backoff hint (minutes) in X-Business-Use-Case-Usage.

    The header is a JSON string: an object keyed by BUC/app/page id, each value
    a list of dicts that may carry ``estimated_time_to_regain_access``. Returns
    None when the header is missing or malformed; never raises.
    """
    if not buc_header:
        return None
    try:
        data = json.loads(buc_header)
    except (TypeError, ValueError):
        return None
    minutes = []
    if isinstance(data, dict):
        for value in data.values():
            if not isinstance(value, list):
                continue
            for entry in value:
                if not isinstance(entry, dict):
                    continue
                eta = entry.get('estimated_time_to_regain_access')
                if isinstance(eta, (int, float)) and not isinstance(eta, bool):
                    minutes.append(eta)
    return max(minutes) if minutes else None


class MetaGraphClient(models.AbstractModel):
    _name = 'meta.graph.client'
    _description = 'Meta Graph API transport (pinned version, isolated outbound HTTP)'

    # One lazily created Session per process. It never carries headers, cookies
    # or the token (that goes in per-call params), so sharing it across threads
    # is safe and we keep connection pooling.
    _session = None

    @api.model
    def _get_session(self):
        cls = type(self)
        if cls._session is None:
            cls._session = requests.Session()
        return cls._session

    @api.model
    def _request(self, token, path, params=None, method='GET',
                 app_secret=None, inject_token=True):
        """Make one Graph call against the pinned-version base URL.

        The path must be bare (no URL, query or traversal), so a stored or
        payload id can't redirect the call elsewhere. The token only travels as
        a query param on a private copy of ``params`` and is never logged or
        left in a re-raised transport error.
        """
        # '..' and '%' matter: an id like '../../debug_token' (or its
        # %2e%2e/%2f form) would step off the pinned /vXX.0 once requests
        # normalises the URL. '#' would silently cut the path short. No real
        # caller passes any of these characters.
        if (not path or '://' in path or '?' in path or '#' in path
                or '\\' in path or '..' in path or '%' in path
                or any(c.isspace() for c in path)):
            raise MetaPermanentError(
                'Unsafe Graph path; callers must pass a bare path, not a full '
                'URL, query string, fragment, path traversal, percent-encoding, '
                'or whitespace-bearing value.')

        url = '%s/%s/%s' % (GRAPH_BASE, const.GRAPH_VERSION, path.lstrip('/'))

        safe_params = dict(params or {})
        if inject_token:
            safe_params['access_token'] = token

        # appsecret_proof = HMAC-SHA256(token, app secret). Computed from the
        # token argument even when inject_token=False, so it always matches the
        # token Meta sees. The proof itself is not secret.
        if app_secret:
            safe_params['appsecret_proof'] = hmac.new(
                app_secret.encode('utf-8'),
                token.encode('utf-8'),
                hashlib.sha256,
            ).hexdigest()

        # Log the path, not the URL: the URL's query string holds the token.
        redacted = _redact(safe_params)
        _logger.debug('Graph %s %s params=%s', method, path, redacted)

        try:
            resp = self._get_session().request(
                method, url, params=safe_params,
                timeout=(CONNECT_TIMEOUT, READ_TIMEOUT),
            )
        except requests.RequestException:
            # requests errors embed the full URL, token included, in their
            # args. Re-raise with a clean message and drop the cause (from None)
            # so the token can't leak through a traceback.
            raise MetaTransientError(
                'Transient transport error for path %s' % path) from None
        return self._handle_response(resp)

    @api.model
    def _handle_response(self, resp):
        """Return the JSON body, or raise the matching typed Meta exception.

        An error envelope wins over the HTTP status: code 190 is auth, then the
        throttle codes, then is_transient, else permanent. Without an envelope
        the HTTP status decides. A JSON decode error never escapes.
        """
        try:
            body = resp.json()
        except (ValueError, json.JSONDecodeError):
            body = None

        if isinstance(body, dict) and body.get('error'):
            err = body['error']
            code = err.get('code')
            subcode = err.get('error_subcode')
            fbtrace = err.get('fbtrace_id')
            etype = err.get('type')
            euser_title = err.get('error_user_title')
            euser_msg = err.get('error_user_msg')
            transient = err.get('is_transient')

            # 190 first: a dead token is auth even on a 200/429. Treating it as
            # transient would retry forever and lose leads.
            if code == AUTH_CODE:
                raise MetaAuthError(
                    err.get('message'), code=code, subcode=subcode,
                    fbtrace_id=fbtrace, error_type=etype,
                    error_user_title=euser_title, error_user_msg=euser_msg,
                    is_transient=transient)
            if code in RATE_LIMIT_CODES:
                buc = resp.headers.get('X-Business-Use-Case-Usage')
                raise MetaRateLimitError(
                    err.get('message'), code=code, subcode=subcode,
                    app_usage=resp.headers.get('X-App-Usage'),
                    buc_usage=buc,
                    retry_after_min=_buc_minutes(buc),
                    fbtrace_id=fbtrace, error_type=etype,
                    error_user_title=euser_title, error_user_msg=euser_msg,
                    is_transient=transient)
            if transient is True:
                raise MetaTransientError(
                    err.get('message'), code=code, subcode=subcode,
                    fbtrace_id=fbtrace, error_type=etype, is_transient=True)
            # Envelope with nothing actionable: fall back to the status so a 5xx
            # stays transient and a 429 stays a throttle. Only a plain 4xx is
            # permanent.
            if resp.status_code >= 500:
                raise MetaTransientError(
                    err.get('message') or 'Graph %s server error' % resp.status_code,
                    code=code, subcode=subcode, fbtrace_id=fbtrace,
                    error_type=etype)
            if resp.status_code == 429:
                buc = resp.headers.get('X-Business-Use-Case-Usage')
                raise MetaRateLimitError(
                    err.get('message') or 'Graph 429 throttle',
                    code=code, subcode=subcode,
                    app_usage=resp.headers.get('X-App-Usage'),
                    buc_usage=buc, retry_after_min=_buc_minutes(buc),
                    fbtrace_id=fbtrace, error_type=etype)
            raise MetaPermanentError(
                err.get('message'), code=code, subcode=subcode,
                fbtrace_id=fbtrace, error_type=etype,
                error_user_title=euser_title, error_user_msg=euser_msg)

        if isinstance(body, dict) and resp.ok:
            return body

        if resp.status_code >= 500:
            raise MetaTransientError('Graph %s server error' % resp.status_code)
        if resp.status_code == 429:
            buc = resp.headers.get('X-Business-Use-Case-Usage')
            raise MetaRateLimitError(
                'Graph 429 throttle',
                app_usage=resp.headers.get('X-App-Usage'),
                buc_usage=buc, retry_after_min=_buc_minutes(buc))
        if resp.ok:
            # 2xx with an empty or non-JSON body.
            raise MetaPermanentError(
                'Empty/invalid JSON in a 2xx Graph response')
        raise MetaPermanentError(
            'Graph %s error (no JSON envelope)' % resp.status_code)

    @api.model
    def fetch_lead(self, page, leadgen_id):
        """GET /{leadgen_id}: read one lead using the meta.page's token.

        Signs the call with the account's App Secret. Typed Meta exceptions
        propagate to the caller.
        """
        return self._request(
            page.access_token, leadgen_id,
            params={'fields': 'id,created_time,field_data,ad_id,ad_name,'
                              'adset_id,adset_name,campaign_id,campaign_name,'
                              'form_id,platform'},
            app_secret=page.account_id.app_secret,
        )

    @api.model
    def resolve_name(self, token, object_type, graph_id, payload_name=None,
                     app_secret=None):
        """Return the name of a campaign/ad set/ad, calling Graph only on a miss.

        Uses ``payload_name`` if the lead already carried it, then the
        meta.name.cache table, and only then one Graph call whose result is
        cached. Pass ``app_secret`` so the lookup still works on apps with
        "Require App Secret Proof" turned on.
        """
        if payload_name:
            return payload_name
        Cache = self.env['meta.name.cache']
        hit = Cache._lookup(object_type, graph_id)
        if hit:
            return hit
        data = self._request(token, graph_id, params={'fields': 'name'},
                             app_secret=app_secret)
        name = data.get('name')
        # Don't cache an empty name: _lookup would read it as a miss and we'd
        # hit Graph on every lead.
        if name:
            Cache._store(object_type, graph_id, name)
        return name

    @api.model
    def exchange_token(self, app_id, app_secret, short_lived_token):
        """Exchange a short-lived user token for a long-lived one.

        /oauth/access_token authenticates with client_id + client_secret, so no
        access_token is injected. Returns (access_token, expires_in);
        expires_in is None for a token that never expires.
        """
        data = self._request(
            short_lived_token,
            'oauth/access_token',
            params={
                'grant_type': 'fb_exchange_token',
                'client_id': app_id,
                'client_secret': app_secret,
                'fb_exchange_token': short_lived_token,
            },
            inject_token=False,
        )
        return data['access_token'], data.get('expires_in')

    @api.model
    def debug_token(self, app_id, app_secret, input_token):
        """Inspect input_token via /debug_token using the app access token.

        Returns the 'data' object (is_valid, type, expires_at, scopes, ...). A
        190 envelope raises MetaAuthError; a 200 with is_valid false is returned
        as-is for the caller to map to auth_failed.
        """
        app_token = '%s|%s' % (app_id, app_secret)
        body = self._request(app_token, 'debug_token',
                             params={'input_token': input_token})
        return body.get('data', {})

    @api.model
    def _iter_paged(self, token, path, params=None, app_secret=None):
        """Yield every item in data[] across all pages.

        Follows paging.cursors.after on the bare path. paging.next is a full
        URL, which _request would refuse.
        """
        call_params = dict(params or {})
        # Stop on a repeated cursor or the page cap, and log it, so a looping
        # cursor can't pin a worker forever.
        seen_after = set()
        pages = 0
        while True:
            body = self._request(token, path, params=call_params,
                                 app_secret=app_secret)
            for item in (body.get('data') or []):
                yield item
            after = (((body.get('paging') or {}).get('cursors') or {})
                     .get('after'))
            if not after:
                return
            pages += 1
            if after in seen_after or pages >= _MAX_PAGES:
                _logger.warning(
                    "meta.graph: pagination halted for path %s after %s page(s) "
                    "(repeated cursor or page cap reached)", path, pages)
                return
            seen_after.add(after)
            call_params = dict(params or {})
            call_params['after'] = after

    @api.model
    def discover_pages(self, user_token, app_secret=None):
        """GET /me/accounts: list the user's Pages, each with its own token."""
        return list(self._iter_paged(
            user_token, 'me/accounts',
            params={'fields': 'id,name,access_token,category,tasks'},
            app_secret=app_secret))

    # ---- Page leadgen webhook subscription --------------------------------
    @api.model
    def subscribe_page(self, page):
        """POST /{page_id}/subscribed_apps?subscribed_fields=leadgen."""
        return self._request(
            page.access_token, '%s/subscribed_apps' % page.page_id,
            params={'subscribed_fields': 'leadgen'}, method='POST',
            app_secret=page.account_id.app_secret)

    @api.model
    def page_subscription_status(self, page):
        """GET /{page_id}/subscribed_apps; feeds the subscription status badge."""
        return self._request(
            page.access_token, '%s/subscribed_apps' % page.page_id,
            method='GET', app_secret=page.account_id.app_secret)

    @api.model
    def unsubscribe_page(self, page):
        """DELETE /{page_id}/subscribed_apps.

        No subscribed_fields is sent, so this may drop the whole app
        subscription rather than just leadgen. Not yet confirmed against live
        Meta.
        """
        return self._request(
            page.access_token, '%s/subscribed_apps' % page.page_id,
            method='DELETE', app_secret=page.account_id.app_secret)

    @api.model
    def discover_forms(self, page_token, page_id, app_secret=None):
        """GET /{page_id}/leadgen_forms. Needs the Page token, not the user token."""
        return list(self._iter_paged(
            page_token, '%s/leadgen_forms' % page_id,
            params={'fields': 'id,name,status,locale'},
            app_secret=app_secret))
