# Meta Lead Ads Integration for Odoo — © 2026 CoreSys Builders. All rights reserved.
# Licensed under the Odoo Proprietary License v1.0 (OPL-1).
# Unauthorized copying, redistribution, or resale of this software, in whole or in
# part, via any medium, is strictly prohibited and constitutes a license violation.
# OPL-1: https://www.odoo.com/documentation/19.0/legal/licenses.html#odoo-apps

"""Tests for meta.graph.client.

Drives a mocked Session against real-shaped JSON fixtures to cover typed-
exception classification, token redaction and no-mutation of caller params,
path validation, and the static scan that guards against a hardcoded Graph
URL/version anywhere in the addon.

Note: assertRaises takes a single exception class, never a tuple — Odoo uses
class-based tests with no conftest.
"""
import os
import json
import hmac
import hashlib
import logging
from unittest import mock

from odoo.tests.common import TransactionCase, tagged

from odoo.addons.meta_lead_ads.models import const
from odoo.addons.meta_lead_ads.models.const import CONNECT_TIMEOUT, READ_TIMEOUT
from odoo.addons.meta_lead_ads.models.exceptions import (
    MetaAuthError, MetaRateLimitError, MetaTransientError, MetaPermanentError,
)

_FIXTURES = os.path.join(os.path.dirname(__file__), 'fixtures')
_TOKEN = 'EAALeak123SECRETtokenZZZ'


def _load_json(filename):
    with open(os.path.join(_FIXTURES, filename), encoding='utf-8') as fh:
        return json.load(fh)


def _load_text(filename):
    with open(os.path.join(_FIXTURES, filename), encoding='utf-8') as fh:
        return fh.read()


def _make_response(status=200, body=None, headers=None, raw_text=None):
    """Build a fake requests.Response-like object.

    .json() raises ValueError when raw_text is set or body is non-JSON, so the
    client must translate that into a typed exception, never a raw ValueError.
    """
    resp = mock.Mock()
    resp.status_code = status
    resp.ok = status < 400
    resp.headers = dict(headers or {})
    if raw_text is not None:
        resp.text = raw_text
        resp.json.side_effect = ValueError('No JSON object could be decoded')
    else:
        resp.text = json.dumps(body) if body is not None else ''
        resp.json.return_value = body
    return resp


@tagged('post_install', '-at_install')
class TestGraphClient(TransactionCase):

    def _client(self):
        return self.env['meta.graph.client']

    def _patched_session(self, response=None, side_effect=None):
        """Patch the client's _get_session to return a Mock whose .request
        yields the given response (or raises side_effect)."""
        client = self._client()
        # Patch on the CLASS, not the recordset instance: Odoo recordsets reject
        # setattr of a method (read-only), so patch.object(client, ...) raises
        # AttributeError on Odoo 18.
        get_session = mock.patch.object(type(client), '_get_session').start()
        self.addCleanup(mock.patch.stopall)
        session = get_session.return_value
        if side_effect is not None:
            session.request.side_effect = side_effect
        else:
            session.request.return_value = response
        return client, session

    # ---- base URL / Session / timeouts -----------------------------------

    def test_request_uses_v23_base_and_timeout(self):
        client, session = self._patched_session(
            _make_response(200, {'name': 'X'}))
        client._request(_TOKEN, '123', params={'fields': 'name'})
        args, kwargs = session.request.call_args
        url = args[1] if len(args) > 1 else kwargs.get('url')
        self.assertIn('/v23.0/', url)
        self.assertEqual(kwargs.get('timeout'), (CONNECT_TIMEOUT, READ_TIMEOUT))

    def test_session_is_reused(self):
        client = self._client()
        s1 = client._get_session()
        s2 = client._get_session()
        self.assertIs(s1, s2)

    def test_session_carries_no_auth_state(self):
        """The reused Session must hold no Authorization header and no cookies:
        the token rides per-request, never stuck on the Session."""
        session = self._client()._get_session()
        self.assertNotIn('Authorization', session.headers)
        self.assertFalse(session.cookies)

    def test_token_not_stored_on_session(self):
        """The token never sticks on the Session headers, and the client holds
        no token attribute."""
        client = self._client()
        session = client._get_session()
        self.assertNotIn('access_token', str(session.headers))
        self.assertFalse(hasattr(client, '_token'))
        self.assertFalse(hasattr(client, 'access_token'))

    # ---- envelope -> typed exception classification ----------------------

    def test_envelope_maps_transient(self):
        client, session = self._patched_session(
            _make_response(status=503, body={'error': {'message': 'boom'}}))
        with self.assertRaises(MetaTransientError):
            client._request(_TOKEN, '123')

    def test_envelope_maps_permanent(self):
        client, session = self._patched_session(
            _make_response(status=400, body=_load_json('error_permanent_100.json')))
        with self.assertRaises(MetaPermanentError):
            client._request(_TOKEN, '123')

    def test_envelope_190_is_auth_error(self):
        client, session = self._patched_session(
            _make_response(status=400, body=_load_json('error_auth_190.json')))
        with self.assertRaises(MetaAuthError) as cm:
            client._request(_TOKEN, '123')
        self.assertEqual(cm.exception.subcode, 463)

    def test_envelope_ratelimit_carries_hint(self):
        buc = json.dumps(_load_json('headers_buc_usage.json'))
        client, session = self._patched_session(_make_response(
            status=400, body=_load_json('error_ratelimit_80006.json'),
            headers={'X-Business-Use-Case-Usage': buc}))
        with self.assertRaises(MetaRateLimitError) as cm:
            client._request(_TOKEN, '123')
        self.assertEqual(cm.exception.retry_after_min, 19)

    def test_bare_429_is_ratelimit(self):
        """A 429 with no dominant envelope code classifies as rate-limit."""
        client, session = self._patched_session(
            _make_response(status=429, body={}))
        with self.assertRaises(MetaRateLimitError):
            client._request(_TOKEN, '123')

    def test_200_with_error_envelope_classifies_by_code(self):
        """HTTP 200 + error code 190 -> MetaAuthError (the error envelope
        dominates the 200 status)."""
        client, session = self._patched_session(_make_response(
            status=200, body=_load_json('error_200_with_envelope.json')))
        with self.assertRaises(MetaAuthError):
            client._request(_TOKEN, '123')

    def test_is_transient_envelope_maps_transient(self):
        """Non-5xx response with is_transient:true -> MetaTransientError."""
        client, session = self._patched_session(_make_response(
            status=400, body=_load_json('error_transient_envelope.json')))
        with self.assertRaises(MetaTransientError):
            client._request(_TOKEN, '123')

    def test_non_json_5xx_is_transient(self):
        """A 500 whose .json() raises ValueError -> MetaTransientError, never a
        raw ValueError."""
        client, session = self._patched_session(_make_response(
            status=500, raw_text=_load_text('error_html_500.txt')))
        with self.assertRaises(MetaTransientError):
            client._request(_TOKEN, '123')

    def test_invalid_json_200_is_permanent(self):
        """A 200 whose .json() raises ValueError -> MetaPermanentError, never a
        raw ValueError."""
        client, session = self._patched_session(_make_response(
            status=200, raw_text='not json at all'))
        with self.assertRaises(MetaPermanentError):
            client._request(_TOKEN, '123')

    def test_buc_malformed_header_keeps_raw_usage(self):
        """A structurally-present-but-unparseable BUC header -> retry_after_min
        is None BUT the raw buc_usage string is preserved."""
        buc = json.dumps(_load_json('headers_buc_usage_malformed.json'))
        client, session = self._patched_session(_make_response(
            status=400, body=_load_json('error_ratelimit_80006.json'),
            headers={'X-Business-Use-Case-Usage': buc}))
        with self.assertRaises(MetaRateLimitError) as cm:
            client._request(_TOKEN, '123')
        self.assertIsNone(cm.exception.retry_after_min)
        self.assertEqual(cm.exception.buc_usage, buc)

    # ---- version sourced from const; no hardcode -------------------------

    def test_url_uses_const_version(self):
        client, session = self._patched_session(
            _make_response(200, {'name': 'X'}))
        with mock.patch.object(const, 'GRAPH_VERSION', 'v99.0'):
            client._request(_TOKEN, '123')
        args, kwargs = session.request.call_args
        url = args[1] if len(args) > 1 else kwargs.get('url')
        self.assertIn('/v99.0/', url)

    # ---- cross: token redaction / no-mutation / path validation ----------

    def test_token_redacted_in_logs(self):
        client, session = self._patched_session(
            _make_response(200, {'name': 'X'}))
        with self.assertLogs('odoo.addons.meta_lead_ads', level=logging.DEBUG) as cap:
            client._request(_TOKEN, '123', params={'fields': 'name'})
        blob = '\n'.join(cap.output)
        self.assertNotIn(_TOKEN, blob)
        self.assertIn('***', blob)
        self.assertNotIn('access_token=%s' % _TOKEN, blob)

    def test_exchange_token_redacts_secret_params_in_logs(self):
        client, _session = self._patched_session(
            _make_response(200, _load_json('oauth_exchange.json')))
        secret = 'SECRET_APP_LOG'
        short = 'SHORT_TOKEN_LOG'
        with self.assertLogs('odoo.addons.meta_lead_ads', level=logging.DEBUG) as cap:
            client.exchange_token('APPID', secret, short)
        blob = '\n'.join(cap.output)
        self.assertNotIn(secret, blob)
        self.assertNotIn(short, blob)
        self.assertIn('***', blob)

    def test_debug_token_redacts_secret_params_in_logs(self):
        client, _session = self._patched_session(
            _make_response(200, _load_json('debug_token_valid_sut.json')))
        secret = 'SECRET_APP_LOG'
        inspected = 'INPUT_TOKEN_LOG'
        with self.assertLogs('odoo.addons.meta_lead_ads', level=logging.DEBUG) as cap:
            client.debug_token('APPID', secret, inspected)
        blob = '\n'.join(cap.output)
        self.assertNotIn(secret, blob)
        self.assertNotIn(inspected, blob)
        self.assertNotIn('APPID|%s' % secret, blob)
        self.assertIn('***', blob)

    def test_request_does_not_mutate_caller_params(self):
        client, session = self._patched_session(
            _make_response(200, {'name': 'X'}))
        p = {'fields': 'name'}
        client._request(_TOKEN, '123', params=p)
        self.assertNotIn('access_token', p)

    def test_request_rejects_full_url_path(self):
        """Callers cannot smuggle a full URL or query string to escape
        GRAPH_BASE/GRAPH_VERSION."""
        client, session = self._patched_session(
            _make_response(200, {'name': 'X'}))
        with self.assertRaises(MetaPermanentError):
            client._request(_TOKEN, 'https://graph.facebook.com/v23.0/123')
        with self.assertRaises(MetaPermanentError):
            client._request(_TOKEN, '123?access_token=leak')
        with self.assertRaises(MetaPermanentError):
            client._request(_TOKEN, '')

    def test_request_rejects_path_traversal(self):
        """Dot-segment and percent-encoded traversal are rejected, so a stored
        or payload id cannot normalize off the pinned /vXX.0 version."""
        client, session = self._patched_session(
            _make_response(200, {'name': 'X'}))
        for bad in ('../../debug_token', '..%2f..%2fdebug_token',
                    '123/../../me', '%2e%2e/leads'):
            with self.assertRaises(MetaPermanentError):
                client._request(_TOKEN, bad)
        # The blocked traversal never reached the transport.
        session.request.assert_not_called()

    # ---- appsecret_proof + inject_token kwarg ----------------------------

    def test_appsecret_proof_injected_when_app_secret_passed(self):
        """appsecret_proof is the HMAC-SHA256 of the SENT token keyed by the
        App Secret."""
        client, session = self._patched_session(
            _make_response(200, {'name': 'X'}))
        client._request('TOKEN', 'me', params={}, app_secret='SECRET')
        params = session.request.call_args.kwargs['params']
        expected = hmac.new(b'SECRET', b'TOKEN', hashlib.sha256).hexdigest()
        self.assertEqual(params['appsecret_proof'], expected)

    def test_appsecret_proof_absent_when_no_app_secret(self):
        """No app_secret -> no appsecret_proof, so plain callers are
        unaffected."""
        client, session = self._patched_session(
            _make_response(200, {'name': 'X'}))
        client._request('TOKEN', 'me', params={})
        self.assertNotIn('appsecret_proof',
                         session.request.call_args.kwargs['params'])

    def test_appsecret_proof_uses_per_call_token(self):
        """The proof is computed per-call from the token actually sent — two
        different tokens yield two different proofs."""
        client, session = self._patched_session(
            _make_response(200, {'name': 'X'}))
        client._request('TOKEN_ONE', 'me', params={}, app_secret='SECRET')
        proof_one = session.request.call_args.kwargs['params']['appsecret_proof']
        client._request('TOKEN_TWO', 'me', params={}, app_secret='SECRET')
        proof_two = session.request.call_args.kwargs['params']['appsecret_proof']
        self.assertNotEqual(proof_one, proof_two)
        self.assertEqual(
            proof_one, hmac.new(b'SECRET', b'TOKEN_ONE', hashlib.sha256).hexdigest())
        self.assertEqual(
            proof_two, hmac.new(b'SECRET', b'TOKEN_TWO', hashlib.sha256).hexdigest())

    def test_resolve_name_miss_sends_appsecret_proof(self):
        """A cache-miss name resolve threads app_secret through, so the
        appsecret_proof is present for name lookups too."""
        client, session = self._patched_session(
            _make_response(200, {'name': 'Resolved'}))
        name = client.resolve_name('TOKEN', 'campaign', 'C9',
                                   app_secret='SECRET')
        self.assertEqual(name, 'Resolved')
        params = session.request.call_args.kwargs['params']
        self.assertEqual(
            params['appsecret_proof'],
            hmac.new(b'SECRET', b'TOKEN', hashlib.sha256).hexdigest())

    def test_iter_paged_stops_on_repeated_cursor(self):
        """A Graph response repeating the same 'after' cursor must not loop
        forever — iteration halts once a cursor is seen again."""
        client = self._client()
        body = {'data': [{'id': '1'}],
                'paging': {'cursors': {'after': 'SAME'}}}
        with mock.patch.object(type(client), '_request',
                               return_value=body) as req:
            items = list(client._iter_paged('TOKEN', 'F1/leads'))
        # First page yields, second page repeats the cursor -> stop. Two
        # _request calls at most, never an unbounded loop.
        self.assertEqual(len(items), 2)
        self.assertEqual(req.call_count, 2)

    def test_request_inject_token_default_true(self):
        """Default inject_token=True attaches the access_token param."""
        client, session = self._patched_session(
            _make_response(200, {'name': 'X'}))
        client._request('TOKEN', 'me', params={})
        self.assertEqual(
            session.request.call_args.kwargs['params']['access_token'], 'TOKEN')

    def test_request_inject_token_false_omits_access_token(self):
        """inject_token=False omits access_token — needed for the tokenless
        OAuth exchange endpoint."""
        client, session = self._patched_session(
            _make_response(200, {'name': 'X'}))
        client._request('TOKEN', 'oauth/access_token',
                        params={'client_id': 'A'}, inject_token=False)
        self.assertNotIn('access_token',
                         session.request.call_args.kwargs['params'])

    # ---- static single-URL/version scan ----------------------------------

    def test_no_hardcoded_graph_url_or_version(self):
        """Walk the addon's .py/.xml/.csv; SKIP tests/, const.py and
        meta_graph_client.py; fail if any forbidden URL/version token leaks
        elsewhere. Mechanically enforces that the URL and version live in one
        place only."""
        addon_root = os.path.dirname(os.path.dirname(__file__))
        forbidden = ('graph.facebook.com', 'https://graph', '/v23.0', 'GRAPH_VERSION')
        allow_basenames = {'const.py', 'meta_graph_client.py'}
        offenders = []
        for dirpath, _dirs, files in os.walk(addon_root):
            if os.sep + 'tests' in dirpath + os.sep:
                continue
            for fname in files:
                if not fname.endswith(('.py', '.xml', '.csv')):
                    continue
                if fname in allow_basenames:
                    continue
                fpath = os.path.join(dirpath, fname)
                try:
                    text = open(fpath, encoding='utf-8').read()
                except (OSError, UnicodeDecodeError):
                    continue
                for needle in forbidden:
                    if needle in text:
                        offenders.append('%s contains %r' % (fpath, needle))
        self.assertFalse(offenders, 'SC#4 leak(s): %s' % offenders)
