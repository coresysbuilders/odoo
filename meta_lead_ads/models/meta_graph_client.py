# Meta Lead Ads Integration for Odoo — © 2026 CoreSys Builders. All rights reserved.
# Licensed under the Odoo Proprietary License v1.0 (OPL-1).
# Unauthorized copying, redistribution, or resale of this software, in whole or in
# part, via any medium, is strictly prohibited and constitutes a license violation.
# OPL-1: https://www.odoo.com/documentation/19.0/legal/licenses.html#odoo-apps

# Meta Graph API transport for an Odoo AbstractModel: pinned version,
# envelope-based error classification, and payload-first name resolution.
# This is the only place a Graph URL is built and the only consumer of
# GRAPH_VERSION.
#
# Transport only — no retry/backoff here. Endpoint methods reuse the private
# _request helper.
import hashlib
import hmac
import json
import logging

import requests

from odoo import api, models

# GRAPH_VERSION is imported, but the URL is built from
# ``const.GRAPH_VERSION`` so the pinned version is read at call time: tests
# patch ``const.GRAPH_VERSION`` and the built URL must follow, so no version
# is hardcoded here.
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

# Graph throttle codes. 80006 = LeadGen form throttle, 80001 = Page/system-user
# throttle — both must be included, since a misclassified LeadGen throttle would
# skip backoff and burn the quota.
RATE_LIMIT_CODES = {4, 17, 32, 613, 80001, 80006}
AUTH_CODE = 190

# Hard ceiling on pages followed in a single _iter_paged sweep — a backstop
# against a non-terminating cursor. Far above any realistic page count for the
# edges we read (forms, leads, pages, subscribed_apps).
_MAX_PAGES = 1000


def _redact(params):
    """Return a COPY of ``params`` with secret-bearing values masked.

    Never mutates the caller's dict, to avoid leaking secrets.
    """
    safe = dict(params or {})
    for key in list(safe):
        lowered = str(key or '').lower()
        if (lowered in _SENSITIVE_PARAM_KEYS or
                lowered.endswith('_token') or 'secret' in lowered):
            safe[key] = '***'
    return safe


def _buc_minutes(buc_header):
    """Parse the ``X-Business-Use-Case-Usage`` header for the backoff hint.

    The header is a JSON STRING: an object keyed by BUC/app/page id whose values
    are LISTS of dicts; each dict may carry ``estimated_time_to_regain_access``
    (minutes). Walk it defensively and return the MAX minute hint found, or
    ``None`` when the header is absent/malformed/partial. This must not raise;
    the caller keeps the raw header on the exception regardless.
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

    # Process-shared singleton Session (lazily created). It sets no default
    # headers and no cookies and is never mutated per request: the access_token
    # travels only as a per-call ``params`` value, so concurrent GETs across
    # Odoo workers/threads are safe (the Session stays immutable; threading.local
    # is not required). Pooled connections survive across calls within a worker.
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
        """Issue one Graph call against the pinned-version base URL.

        Path is validated first so callers cannot smuggle a full URL/query
        string to bypass GRAPH_BASE/GRAPH_VERSION. The token is copied onto a
        private params dict (never the caller's), travels only as a query param,
        is never set on the Session, never logged, and is scrubbed from any
        re-raised transport error.
        """
        # Validate the path first: reject empty paths, full URLs, and smuggled
        # query strings. As defense-in-depth, also reject a '#' fragment (which
        # would silently drop the rest of the path before it reaches the
        # server), a backslash, any whitespace, '..' dot-segments, and '%'
        # percent-encoding. Without the last two a stored/payload id like
        # '../../debug_token' would normalize off the pinned /vXX.0 version once
        # requests builds the URL, and '%2e%2e'/'%2f' would smuggle the same
        # traversal in encoded form. None of these appear in a legitimate bare
        # path (numeric ids / edges like 'me/accounts', '<id>/leads',
        # 'oauth/access_token'), so this only blocks smuggling attempts without
        # affecting any real caller.
        if (not path or '://' in path or '?' in path or '#' in path
                or '\\' in path or '..' in path or '%' in path
                or any(c.isspace() for c in path)):
            raise MetaPermanentError(
                'Unsafe Graph path; callers must pass a bare path, not a full '
                'URL, query string, fragment, path traversal, percent-encoding, '
                'or whitespace-bearing value.')

        url = '%s/%s/%s' % (GRAPH_BASE, const.GRAPH_VERSION, path.lstrip('/'))

        # COPY before injecting the token — never mutate the caller's dict.
        safe_params = dict(params or {})
        if inject_token:
            safe_params['access_token'] = token

        # appsecret_proof: HMAC-SHA256 of this call's access_token keyed by the
        # App Secret. Computed from the exact ``token`` arg so the proof always
        # matches the sent token. The proof itself is non-secret; _redact still
        # masks access_token. Only added when an App Secret is supplied. The proof
        # is keyed on the real token even when inject_token=False, so a signed
        # tokenless call stays valid.
        if app_secret:
            safe_params['appsecret_proof'] = hmac.new(
                app_secret.encode('utf-8'),
                token.encode('utf-8'),
                hashlib.sha256,
            ).hexdigest()

        # Log only method + path + redacted params (access_token masked to ***).
        # The built request target and the raw token are never logged — the
        # token lives in the querystring.
        redacted = _redact(safe_params)
        _logger.debug('Graph %s %s params=%s', method, path, redacted)

        try:
            resp = self._get_session().request(
                method, url, params=safe_params,
                timeout=(CONNECT_TIMEOUT, READ_TIMEOUT),   # enforced on every call
            )
        except requests.RequestException:
            # Any requests error (ConnectionError/Timeout and siblings such as
            # TooManyRedirects/ContentDecodingError/ChunkedEncodingError/InvalidURL)
            # embeds the full URL — including the access_token query param — in
            # its message/args. Catch the base class and re-raise a token-free
            # message, dropping the chained cause (from None) so the secret never
            # leaks via a propagated traceback.
            raise MetaTransientError(
                'Transient transport error for path %s' % path) from None
        return self._handle_response(resp)

    @api.model
    def _handle_response(self, resp):
        """Envelope-dominant classification into the typed exception hierarchy.

        Order: parse JSON defensively -> if an error envelope is present,
        classify by the envelope regardless of HTTP status (190 first, then the
        throttle set, then is_transient, else permanent) -> success -> otherwise
        classify by transport status. A raw ValueError/JSONDecodeError never
        escapes this method. No retry/backoff here.
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

            # 190 checked first — token death even on a 200/429 body is auth,
            # never a transient blip (otherwise leads are silently lost).
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
                    buc_usage=buc,                      # raw header kept
                    retry_after_min=_buc_minutes(buc),
                    fbtrace_id=fbtrace, error_type=etype,
                    error_user_title=euser_title, error_user_msg=euser_msg,
                    is_transient=transient)
            if transient is True:
                raise MetaTransientError(
                    err.get('message'), code=code, subcode=subcode,
                    fbtrace_id=fbtrace, error_type=etype, is_transient=True)
            # Envelope present but with no actionable code and is_transient not
            # set: defer to the transport status so a 5xx (server-side) stays
            # transient and a 429 stays rate-limit; only a clean 4xx with no
            # recognized signal is a true permanent app/param error.
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

        # Success: a dict with no error envelope on a 2xx response.
        if isinstance(body, dict) and resp.ok:
            return body

        # No parseable error envelope — classify by transport status.
        if resp.status_code >= 500:
            raise MetaTransientError('Graph %s server error' % resp.status_code)
        if resp.status_code == 429:
            buc = resp.headers.get('X-Business-Use-Case-Usage')
            raise MetaRateLimitError(
                'Graph 429 throttle',
                app_usage=resp.headers.get('X-App-Usage'),
                buc_usage=buc, retry_after_min=_buc_minutes(buc))
        if resp.ok:
            # A 2xx with empty/invalid JSON where the caller expected data must
            # not surface a raw ValueError.
            raise MetaPermanentError(
                'Empty/invalid JSON in a 2xx Graph response')
        raise MetaPermanentError(
            'Graph %s error (no JSON envelope)' % resp.status_code)

    @api.model
    def fetch_lead(self, page, leadgen_id):
        """GET /{leadgen_id} — read one Meta lead. page is a meta.page recordset.

        Reads page.access_token + page.account_id.app_secret (appsecret_proof).
        leadgen_id is a bare object id (no '://', no '?') so it passes the
        _request path guard. Typed exceptions (MetaAuthError/MetaRateLimitError/
        MetaTransientError/MetaPermanentError) propagate from _handle_response.
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
        """Payload-first / cache-on-miss name resolution, to limit Graph calls.

        1. ``payload_name`` present -> return it, zero HTTP and no cache write
           (the common case — the LeadGen payload carries the *_name).
        2. Persistent cache hit -> return it, no Graph call.
        3. True miss -> exactly one Graph call for {'fields': 'name'}, write the
           concurrency-safe cache (indefinite, no TTL), return the name.

        ``app_secret`` is threaded into the cache-miss Graph call so the
        appsecret_proof is sent for name lookups too — without it, an app that
        has "Require App Secret Proof" enabled would reject attribution name
        resolution for leads whose payload omits the *_name.
        """
        if payload_name:
            return payload_name
        Cache = self.env['meta.name.cache']
        hit = Cache._lookup(object_type, graph_id)
        if hit:
            return hit
        # graph_id is a bare path -> passes the _request path validation.
        data = self._request(token, graph_id, params={'fields': 'name'},
                             app_secret=app_secret)
        name = data.get('name')
        # Only cache a real resolved name — storing a falsy name would create a
        # row that _lookup reads back as a miss, re-fetching on every call and
        # defeating the rate-limit defense.
        if name:
            Cache._store(object_type, graph_id, name)
        return name

    @api.model
    def exchange_token(self, app_id, app_secret, short_lived_token):
        """Exchange a short-lived user token for a long-lived one.

        GET /oauth/access_token?grant_type=fb_exchange_token&client_id=&
        client_secret=&fb_exchange_token=  ->  {access_token, token_type,
        expires_in}. This OAuth endpoint authenticates by client_id +
        client_secret in the query — it must not receive an injected bearer
        access_token, so we call _request with inject_token=False. The call
        still routes through _request for the pinned version, timeouts, and
        token-free re-raise. Returns (access_token, expires_in); expires_in may
        be absent for a never-expiring result.
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
        """Introspect input_token via GET /debug_token?input_token=&
        access_token={app_id|app_secret}. Meta accepts an app access token to
        inspect another token. Returns the 'data' object (is_valid, type,
        expires_at, data_access_expires_at, scopes, granular_scopes, user_id,
        error, ...). A transport-level 190 envelope is raised as MetaAuthError by
        _handle_response; an HTTP-200 body with is_valid:false is returned as-is
        for the caller's status mapper to classify as auth_failed.
        """
        app_token = '%s|%s' % (app_id, app_secret)
        body = self._request(app_token, 'debug_token',
                             params={'input_token': input_token})
        return body.get('data', {})

    @api.model
    def _iter_paged(self, token, path, params=None, app_secret=None):
        """Yield each item in data[] across all pages, following
        paging.cursors.after with the bare path (never paging.next, which is a
        full URL the _request guard rejects). Guards against silent truncation.
        Re-issues through _request so timeouts/redaction/appsecret_proof all
        still apply.
        """
        call_params = dict(params or {})
        # Guard against a non-terminating paging loop: Meta returning the same
        # 'after' cursor again (or a buggy mock) would otherwise spin forever and
        # pin a worker. Stop on a repeated cursor or a hard page cap, logging so
        # the truncation is observable rather than silent.
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
        """GET /me/accounts -> [{id, name, access_token, category, tasks}, ...],
        paginated. Each Page carries its own access_token."""
        return list(self._iter_paged(
            user_token, 'me/accounts',
            params={'fields': 'id,name,access_token,category,tasks'},
            app_secret=app_secret))

    # ---- Subscription edge (Page leadgen webhook) -------------------------
    # Three thin _request wrappers cloned from fetch_lead: bare path, page
    # access_token, app_secret=page.account_id.app_secret. This is the only
    # place a /{page_id}/subscribed_apps Graph URL is built -- the pinned
    # const.GRAPH_VERSION, appsecret_proof and token redaction all apply inside
    # _request. Do not f-string a Graph URL anywhere else.
    @api.model
    def subscribe_page(self, page):
        """POST /{page_id}/subscribed_apps?subscribed_fields=leadgen."""
        return self._request(
            page.access_token, '%s/subscribed_apps' % page.page_id,
            params={'subscribed_fields': 'leadgen'}, method='POST',
            app_secret=page.account_id.app_secret)

    @api.model
    def page_subscription_status(self, page):
        """GET /{page_id}/subscribed_apps -> data[] (drives the status badge)."""
        return self._request(
            page.access_token, '%s/subscribed_apps' % page.page_id,
            method='GET', app_secret=page.account_id.app_secret)

    @api.model
    def unsubscribe_page(self, page):
        """DELETE /{page_id}/subscribed_apps (unsubscribe).

        The DELETE param granularity still needs live verification — a bare
        DELETE may remove the whole app subscription, not just the leadgen
        field. v1 does not pass subscribed_fields on DELETE; confirm the
        granularity against live Meta.
        """
        return self._request(
            page.access_token, '%s/subscribed_apps' % page.page_id,
            method='DELETE', app_secret=page.account_id.app_secret)

    @api.model
    def discover_forms(self, page_token, page_id, app_secret=None):
        """GET /{page_id}/leadgen_forms -> [{id, name, status, locale}, ...],
        paginated. Page-scoped read — pass the per-Page access_token, not the
        user token."""
        return list(self._iter_paged(
            page_token, '%s/leadgen_forms' % page_id,
            params={'fields': 'id,name,status,locale'},
            app_secret=app_secret))
