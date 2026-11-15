# Meta Lead Ads Integration for Odoo — © 2026 CoreSys Builders. All rights reserved.
# Licensed under the Odoo Proprietary License v1.0 (OPL-1).
# Unauthorized copying, redistribution, or resale of this software, in whole or in
# part, via any medium, is strictly prohibited and constitutes a license violation.
# OPL-1: https://www.odoo.com/documentation/18.0/legal/licenses.html#odoo-apps

"""Tests for meta.graph.client.

A mocked Session returns real-shaped JSON fixtures. Covers error
classification, token redaction, path validation, and a scan that keeps the
Graph URL and version out of every file except const.py and the client.
"""
import os
import json
import hmac
import hashlib
import logging
from unittest import mock

from odoo.exceptions import UserError
from odoo.tests.common import TransactionCase, new_test_user, tagged

from odoo.addons.meta_lead_ads.models import const, meta_graph_client
from odoo.addons.meta_lead_ads.models.const import CONNECT_TIMEOUT, READ_TIMEOUT
from odoo.addons.meta_lead_ads.models.exceptions import (
    MetaAuthError, MetaIngestRaceError, MetaRateLimitError, MetaTransientError,
    MetaPermanentError, scrub_secrets,
)

from .test_ingest import IngestFixtureMixin

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

    With raw_text set, .json() raises ValueError like requests does on a
    non-JSON body.
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
        """Patch _get_session so its .request returns `response` or raises
        `side_effect`."""
        client = self._client()
        # Patch the class: patch.object on a recordset raises AttributeError
        # because recordsets don't allow setattr of a method.
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
        """The shared Session has no Authorization header and no cookies."""
        session = self._client()._get_session()
        self.assertNotIn('Authorization', session.headers)
        self.assertFalse(session.cookies)

    def test_token_not_stored_on_session(self):
        """The token isn't kept on the Session or on the client."""
        client = self._client()
        session = client._get_session()
        self.assertNotIn('access_token', str(session.headers))
        self.assertFalse(hasattr(client, '_token'))
        self.assertFalse(hasattr(client, 'access_token'))

    # ---- error envelope classification ------------------------------------

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

    def test_leadgen_80005_is_ratelimit(self):
        """80005 is Meta's Lead Ads throttle; it must keep the backoff hint."""
        buc = json.dumps(_load_json('headers_buc_usage.json'))
        client, session = self._patched_session(_make_response(
            status=400, body=_load_json('error_ratelimit_80005.json'),
            headers={'X-Business-Use-Case-Usage': buc}))
        with self.assertRaises(MetaRateLimitError) as cm:
            client._request(_TOKEN, '123')
        self.assertEqual(cm.exception.code, 80005)
        self.assertEqual(cm.exception.retry_after_min, 19)

    def test_bare_429_is_ratelimit(self):
        """A 429 with no error code in the body is a rate limit."""
        client, session = self._patched_session(
            _make_response(status=429, body={}))
        with self.assertRaises(MetaRateLimitError):
            client._request(_TOKEN, '123')

    def test_200_with_error_envelope_classifies_by_code(self):
        """An error envelope wins over a 200 status: code 190 is an auth error."""
        client, session = self._patched_session(_make_response(
            status=200, body=_load_json('error_200_with_envelope.json')))
        with self.assertRaises(MetaAuthError):
            client._request(_TOKEN, '123')

    def test_is_transient_envelope_maps_transient(self):
        """is_transient true on a 4xx is still transient."""
        client, session = self._patched_session(_make_response(
            status=400, body=_load_json('error_transient_envelope.json')))
        with self.assertRaises(MetaTransientError):
            client._request(_TOKEN, '123')

    def test_non_json_5xx_is_transient(self):
        """A 500 with an HTML body raises MetaTransientError, not ValueError."""
        client, session = self._patched_session(_make_response(
            status=500, raw_text=_load_text('error_html_500.txt')))
        with self.assertRaises(MetaTransientError):
            client._request(_TOKEN, '123')

    def test_invalid_json_200_is_permanent(self):
        """A 200 with a non-JSON body raises MetaPermanentError, not ValueError."""
        client, session = self._patched_session(_make_response(
            status=200, raw_text='not json at all'))
        with self.assertRaises(MetaPermanentError):
            client._request(_TOKEN, '123')

    def test_buc_malformed_header_keeps_raw_usage(self):
        """An unparseable usage header gives no retry hint but keeps the raw
        header text."""
        buc = json.dumps(_load_json('headers_buc_usage_malformed.json'))
        client, session = self._patched_session(_make_response(
            status=400, body=_load_json('error_ratelimit_80006.json'),
            headers={'X-Business-Use-Case-Usage': buc}))
        with self.assertRaises(MetaRateLimitError) as cm:
            client._request(_TOKEN, '123')
        self.assertIsNone(cm.exception.retry_after_min)
        self.assertEqual(cm.exception.buc_usage, buc)

    def test_buc_drops_estimates_that_are_not_finite(self):
        """Infinity, NaN, 1e400 or a 400 digit number is not a wait time."""
        buc_minutes = meta_graph_client._buc_minutes
        entry = '{"1": [{"estimated_time_to_regain_access": %s}]}'
        for value in ('Infinity', '-Infinity', 'NaN', '1e400', '9' * 400):
            with self.subTest(value=value[:10]):
                self.assertIsNone(buc_minutes(entry % value))
        # A bad entry beside a good one only drops itself.
        mixed = ('{"1": [{"estimated_time_to_regain_access": 1e400}, '
                 '{"estimated_time_to_regain_access": 7}]}')
        self.assertEqual(buc_minutes(mixed), 7)

    def test_buc_reads_the_called_object_first(self):
        """Other Pages or objects in the header don't hold this call back."""
        buc_minutes = meta_graph_client._buc_minutes
        buc = json.dumps({
            'PGBUC': [{'estimated_time_to_regain_access': 3}],
            'OTHER': [{'estimated_time_to_regain_access': 600}]})
        self.assertEqual(buc_minutes(buc, 'PGBUC'), 3)
        self.assertEqual(buc_minutes(buc, 'MISSING'), 600)
        self.assertEqual(buc_minutes(buc), 600)

    def test_fetch_lead_hint_from_its_page(self):
        buc = json.dumps({
            'PGBUC': [{'estimated_time_to_regain_access': 3}],
            'OTHER': [{'estimated_time_to_regain_access': 600}]})
        client, session = self._patched_session(_make_response(
            status=400, body=_load_json('error_ratelimit_80005.json'),
            headers={'X-Business-Use-Case-Usage': buc}))
        page = mock.Mock(page_id='PGBUC', access_token=_TOKEN)
        page.account_id.app_secret = 'SECRET'
        with self.assertRaises(MetaRateLimitError) as cm:
            client.fetch_lead(page, 'LGBUC')
        self.assertEqual(cm.exception.retry_after_min, 3)

    # ---- version comes from const ----------------------------------------

    def test_url_uses_const_version(self):
        client, session = self._patched_session(
            _make_response(200, {'name': 'X'}))
        with mock.patch.object(const, 'GRAPH_VERSION', 'v99.0'):
            client._request(_TOKEN, '123')
        args, kwargs = session.request.call_args
        url = args[1] if len(args) > 1 else kwargs.get('url')
        self.assertIn('/v99.0/', url)

    # ---- token redaction, caller params, path validation -----------------

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
        """A full URL, a query string or an empty path is refused, so callers
        can't escape the pinned base URL and version."""
        client, session = self._patched_session(
            _make_response(200, {'name': 'X'}))
        with self.assertRaises(MetaPermanentError):
            client._request(_TOKEN, 'https://graph.facebook.com/v23.0/123')
        with self.assertRaises(MetaPermanentError):
            client._request(_TOKEN, '123?access_token=leak')
        with self.assertRaises(MetaPermanentError):
            client._request(_TOKEN, '')

    def test_request_rejects_path_traversal(self):
        """Dot segments, plain or percent-encoded, are refused so an id from a
        payload can't step off the pinned version."""
        client, session = self._patched_session(
            _make_response(200, {'name': 'X'}))
        for bad in ('../../debug_token', '..%2f..%2fdebug_token',
                    '123/../../me', '%2e%2e/leads'):
            with self.assertRaises(MetaPermanentError):
                client._request(_TOKEN, bad)
        session.request.assert_not_called()

    # ---- appsecret_proof and inject_token -----------------------------------

    def test_appsecret_proof_injected_when_app_secret_passed(self):
        """appsecret_proof is HMAC-SHA256 of the sent token, keyed by the App
        Secret."""
        client, session = self._patched_session(
            _make_response(200, {'name': 'X'}))
        client._request('TOKEN', 'me', params={}, app_secret='SECRET')
        params = session.request.call_args.kwargs['params']
        expected = hmac.new(b'SECRET', b'TOKEN', hashlib.sha256).hexdigest()
        self.assertEqual(params['appsecret_proof'], expected)

    def test_appsecret_proof_absent_when_no_app_secret(self):
        """Without app_secret no appsecret_proof is sent."""
        client, session = self._patched_session(
            _make_response(200, {'name': 'X'}))
        client._request('TOKEN', 'me', params={})
        self.assertNotIn('appsecret_proof',
                         session.request.call_args.kwargs['params'])

    def test_appsecret_proof_uses_per_call_token(self):
        """The proof is computed from each call's own token."""
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
        """Name lookups that miss the cache also send appsecret_proof."""
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
        """Paging stops when Graph hands back an 'after' cursor it already
        gave."""
        client = self._client()
        body = {'data': [{'id': '1'}],
                'paging': {'cursors': {'after': 'SAME'}, 'next': 'x'}}
        status = {}
        with mock.patch.object(type(client), '_request',
                               return_value=body) as req:
            items = list(client._iter_paged('TOKEN', 'F1/leads',
                                            status=status))
        # Page one, then a repeated cursor on page two, then stop.
        self.assertEqual(len(items), 2)
        self.assertEqual(req.call_count, 2)
        self.assertIs(status.get('truncated'), True)

    def test_iter_paged_stops_without_next(self):
        """A last page that still echoes a cursor ends the listing as
        complete: Meta says to stop when there is no next link."""
        client = self._client()
        body = {'data': [{'id': '1'}], 'paging': {'cursors': {'after': 'A'}}}
        status = {}
        with mock.patch.object(type(client), '_request',
                               return_value=body) as req:
            items = list(client._iter_paged('TOKEN', 'F1/leads',
                                            status=status))
        self.assertEqual(len(items), 1)
        self.assertEqual(req.call_count, 1)
        self.assertNotIn('truncated', status)

    def test_iter_paged_forwards_buc_id(self):
        """Every page request names the object whose throttle counts."""
        client = self._client()
        pages = [{'data': [{'id': '1'}],
                  'paging': {'cursors': {'after': 'A'}, 'next': 'x'}},
                 {'data': [{'id': '2'}]}]
        with mock.patch.object(type(client), '_request',
                               side_effect=pages) as req:
            list(client._iter_paged('TOKEN', 'F1/leads', buc_id='PG1'))
        self.assertEqual(req.call_count, 2)
        for call in req.call_args_list:
            self.assertEqual(call.kwargs['buc_id'], 'PG1')

    def test_iter_paged_next_without_cursor_is_truncated(self):
        """A next link with no cursor means more data we can't read, so the
        listing is reported as truncated and the log names the path only."""
        client = self._client()
        body = {'data': [{'id': '1'}], 'paging': {'next': 'x'}}
        status = {}
        with mock.patch.object(type(client), '_request',
                               return_value=body) as req, \
                self.assertLogs('odoo.addons.meta_lead_ads.models.'
                                'meta_graph_client', level='WARNING') as logs:
            items = list(client._iter_paged('TOKEN', 'F1/leads',
                                            params={'filtering': 'FILTERVALUE'},
                                            status=status))
        self.assertEqual(len(items), 1)
        self.assertEqual(req.call_count, 1)
        self.assertIs(status.get('truncated'), True)
        text = '\n'.join(logs.output)
        self.assertIn('F1/leads', text)
        self.assertNotIn('TOKEN', text)
        self.assertNotIn('FILTERVALUE', text)

    def test_iter_paged_stops_at_deadline(self):
        """Once the caller's deadline has passed no further page is read, and
        that is reported apart from a truncated listing."""
        client = self._client()
        pages = [{'data': [{'id': '1'}],
                  'paging': {'cursors': {'after': 'A'}, 'next': 'x'}},
                 {'data': [{'id': '2'}],
                  'paging': {'cursors': {'after': 'B'}, 'next': 'x'}}]
        clock = [50]

        def _req(*args, **kwargs):
            body = pages.pop(0)
            clock[0] = 150
            return body

        status = {}
        with mock.patch('odoo.addons.meta_lead_ads.models.meta_graph_client.'
                        '_monotonic', lambda: clock[0]), \
                mock.patch.object(type(client), '_request',
                                  side_effect=_req) as req:
            items = list(client._iter_paged('TOKEN', 'F1/leads',
                                            status=status, deadline=100))
        self.assertEqual([i['id'] for i in items], ['1'])
        self.assertEqual(req.call_count, 1)
        self.assertIs(status.get('deadline_hit'), True)
        self.assertNotIn('truncated', status)

        # Without a deadline the clock is never read.
        def _boom():
            raise AssertionError('clock read without a deadline')

        pages = [{'data': [{'id': '1'}],
                  'paging': {'cursors': {'after': 'A'}, 'next': 'x'}},
                 {'data': [{'id': '2'}]}]
        with mock.patch('odoo.addons.meta_lead_ads.models.meta_graph_client.'
                        '_monotonic', _boom), \
                mock.patch.object(type(client), '_request',
                                  side_effect=pages):
            items = list(client._iter_paged('TOKEN', 'F1/leads'))
        self.assertEqual(len(items), 2)

    def test_iter_paged_reports_truncation(self):
        """A caller passing a status dict learns that the listing was cut
        short by the page cap, and not when the listing simply ended."""
        client = self._client()
        three = [{'data': [{'id': '1'}],
                  'paging': {'cursors': {'after': 'A'}, 'next': 'x'}},
                 {'data': [{'id': '2'}],
                  'paging': {'cursors': {'after': 'B'}, 'next': 'x'}},
                 {'data': [{'id': '3'}]}]
        status = {}
        with mock.patch('odoo.addons.meta_lead_ads.models.meta_graph_client.'
                        '_MAX_PAGES', 2), \
                mock.patch.object(type(client), '_request',
                                  side_effect=three):
            items = list(client._iter_paged('TOKEN', 'F1/leads',
                                            status=status))
        self.assertEqual(len(items), 2)
        self.assertIs(status.get('truncated'), True)

        two = [{'data': [{'id': '1'}],
                'paging': {'cursors': {'after': 'A'}, 'next': 'x'}},
               {'data': [{'id': '2'}], 'paging': {'cursors': {}}}]
        status = {}
        with mock.patch('odoo.addons.meta_lead_ads.models.meta_graph_client.'
                        '_MAX_PAGES', 2), \
                mock.patch.object(type(client), '_request',
                                  side_effect=two):
            items = list(client._iter_paged('TOKEN', 'F1/leads',
                                            status=status))
        self.assertEqual(len(items), 2)
        self.assertNotIn('truncated', status)

    def test_request_inject_token_default_true(self):
        """access_token is sent by default."""
        client, session = self._patched_session(
            _make_response(200, {'name': 'X'}))
        client._request('TOKEN', 'me', params={})
        self.assertEqual(
            session.request.call_args.kwargs['params']['access_token'], 'TOKEN')

    def test_request_inject_token_false_omits_access_token(self):
        """inject_token=False leaves access_token out, as the OAuth exchange
        endpoint needs."""
        client, session = self._patched_session(
            _make_response(200, {'name': 'X'}))
        client._request('TOKEN', 'oauth/access_token',
                        params={'client_id': 'A'}, inject_token=False)
        self.assertNotIn('access_token',
                         session.request.call_args.kwargs['params'])

    # ---- no hardcoded Graph URL or version ---------------------------------

    def test_no_hardcoded_graph_url_or_version(self):
        """Only const.py and meta_graph_client.py may mention the Graph URL or
        version. Scans .py/.xml/.csv outside tests/."""
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
        self.assertFalse(offenders, 'token leaked into: %s' % offenders)


# Shaped like the real thing: a Page token and a 64-hex appsecret_proof.
_LEAKED_TOKEN = 'EAAB' + 'Zx9' * 14
_LEAKED_PROOF = '0f1e2d3c' * 8


@tagged('post_install', '-at_install')
class TestSecretScrub(IngestFixtureMixin, TransactionCase):
    """Tokens and secrets in Meta's error text never reach a stored field."""

    def _assert_clean(self, text):
        self.assertNotIn('EAAB', text or '')
        self.assertNotIn(_LEAKED_PROOF, text or '')

    # ---- the patterns ------------------------------------------------------

    def test_each_pattern_masked(self):
        cases = {
            'access_token=%s' % _LEAKED_TOKEN: 'access_token=***',
            'appsecret_proof=%s' % _LEAKED_PROOF: 'appsecret_proof=***',
            'secret %s here' % ('a1b2' * 8): 'secret *** here',
            'token 123456789|abcdefghijklmnopQR end': 'token *** end',
            'blob %s end' % ('Zm9vYmFy' * 6): 'blob *** end',
            '{"access_token":"abc.def"}': '{"access_token":"***"}',
            'Authorization: Bearer xyz': 'Authorization: Bearer ***',
        }
        for raw, expected in cases.items():
            self.assertEqual(scrub_secrets(raw), expected)

    def test_non_text_unchanged(self):
        self.assertIsNone(scrub_secrets(None))
        self.assertEqual(scrub_secrets(42), 42)

    def test_meta_ids_survive(self):
        err = MetaIngestRaceError('No lead for 1234567890123456 yet')
        self.assertIn('1234567890123456', str(err))
        text = scrub_secrets(
            'Page 102938475610293 has no access to form 5566778899001122')
        self.assertIn('102938475610293', text)
        self.assertIn('5566778899001122', text)

    def test_user_facing_fields_scrubbed(self):
        err = MetaPermanentError(
            'x', error_user_title='Bearer abc',
            error_user_msg='access_token=%s' % _LEAKED_TOKEN)
        self.assertEqual(err.error_user_title, 'Bearer ***')
        self._assert_clean(err.error_user_msg)
        self.assertIsNone(MetaPermanentError('x').error_user_msg)

    def test_url_and_encoded_forms(self):
        url = ('https://graph.facebook.com/v23.0/123?access_token=%s'
               '&appsecret_proof=%s&fields=id' % (_LEAKED_TOKEN, _LEAKED_PROOF))
        text = scrub_secrets(url)
        self._assert_clean(text)
        self.assertIn('access_token=***&appsecret_proof=***&fields=id', text)
        encoded = 'access_token%%3D%s%%26appsecret_proof%%3D%s' % (
            _LEAKED_TOKEN, _LEAKED_PROOF)
        text = scrub_secrets(encoded)
        self._assert_clean(text)
        self.assertEqual(text, 'access_token%3D***%26appsecret_proof%3D***')

    def test_graph_envelope_scrubbed(self):
        resp = _make_response(400, {'error': {
            'message': 'Invalid OAuth access token: access_token=%s '
                       'appsecret_proof=%s' % (_LEAKED_TOKEN, _LEAKED_PROOF),
            'code': 100}})
        with self.assertRaises(MetaPermanentError) as cm:
            self.env['meta.graph.client']._handle_response(resp)
        self._assert_clean(str(cm.exception))
        self.assertIn('***', str(cm.exception))

    # ---- stored error text -------------------------------------------------

    def _leaky(self, cls=MetaPermanentError):
        return cls('bad access_token=%s' % _LEAKED_TOKEN)

    def test_queue_and_log_store_scrubbed_text(self):
        event = self.env['meta.webhook.event'].create({
            'leadgen_id': 'LGSCRQ', 'page_id': 'PG1', 'form_id': 'F1',
            'status': 'pending'})
        fetch = mock.Mock(side_effect=self._leaky())
        with self._patch_graph(fetch_lead=fetch):
            self.env['meta.webhook.event']._cron_drain()
        self.assertEqual(fetch.call_count, 1)
        self.assertEqual(event.status, 'failed')
        self.assertIn('***', event.error_message)
        self._assert_clean(event.error_message)
        queue_row = self.env['meta.sync.log'].search(
            [('meta_leadgen_id', '=', 'LGSCRQ'), ('status', '=', 'failed')])
        self.assertTrue(queue_row)
        self._assert_clean(queue_row.error_message)

        row = self.env['meta.sync.log']._record_failure(
            'LGSCRL', 'webhook', 'boom', page=self.page)
        fetch = mock.Mock(side_effect=self._leaky())
        with self._patch_graph(fetch_lead=fetch):
            row.action_retry()
        row.invalidate_recordset()
        self.assertEqual(fetch.call_count, 1)
        self.assertEqual(row.status, 'failed')
        self.assertIn('***', row.error_message)
        self._assert_clean(row.error_message)

    def test_no_token_in_other_sinks(self):
        # Restore shows Meta's text in its error.
        admin = new_test_user(
            self.env, 'scrub_admin', email='scrub_admin@example.com',
            groups='base.group_user,sales_team.group_sale_salesman_all_leads,'
                   'meta_lead_ads.group_meta_admin')
        Tomb = self.env['meta.leadgen.tombstone']
        Tomb._remember(['LGSCRT'], details={'LGSCRT': {
            'page_id': self.page.id, 'company_id': self.env.company.id,
            'reason': 'deleted'}})
        tomb = Tomb.sudo().search([('leadgen_id', '=', 'LGSCRT')])
        with self._patch_graph(fetch_lead=mock.Mock(
                side_effect=MetaPermanentError(
                    'access_token=%s' % _LEAKED_TOKEN))), \
                self.assertRaises(UserError) as cm:
            tomb.with_user(admin).action_restore()
        self.assertIn('***', str(cm.exception))
        self._assert_clean(str(cm.exception))

        # Backfill listing failure row.
        with mock.patch.object(self.Client, '_iter_paged',
                               side_effect=self._leaky()):
            self.env['meta.lead.form']._cron_backfill()
        rows = self.env['meta.sync.log'].search(
            [('form_id', '=', self.form.id), ('status', '=', 'failed')])
        self.assertTrue(rows)
        for row in rows:
            self._assert_clean(row.error_message)

        # Token health: status fields, the To-Do and the chatter.
        with mock.patch.object(self.Client, 'debug_token',
                               side_effect=MetaAuthError(
                                   'Invalid OAuth access token: '
                                   'access_token=%s' % _LEAKED_TOKEN)):
            self.env['meta.account']._cron_token_health()
        account = self.account.sudo()
        account.invalidate_recordset()
        self.assertFalse(account.token_valid)
        for name, field in account._fields.items():
            if field.store and field.type in ('char', 'text', 'html'):
                self._assert_clean(str(account[name] or ''))
        self.assertTrue(account.activity_ids)
        for note in account.activity_ids.mapped('note'):
            self._assert_clean(str(note or ''))
        for body in account.message_ids.mapped('body'):
            self._assert_clean(str(body or ''))
        mails = self.env['mail.mail'].search(
            [('model', '=', 'meta.account'), ('res_id', '=', account.id)])
        for body in mails.mapped('body_html'):
            self._assert_clean(str(body or ''))
