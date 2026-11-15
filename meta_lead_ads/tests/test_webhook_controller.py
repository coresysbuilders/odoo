# Meta Lead Ads Integration for Odoo — © 2026 CoreSys Builders. All rights reserved.
# Licensed under the Odoo Proprietary License v1.0 (OPL-1).
# Unauthorized copying, redistribution, or resale of this software, in whole or in
# part, via any medium, is strictly prohibited and constitutes a license violation.
# OPL-1: https://www.odoo.com/documentation/19.0/legal/licenses.html#odoo-apps

"""HTTP tests for the public webhook route ``/meta_lead_ads/webhook``.

Real requests against the test server, covering the GET verify-token
handshake and the POST handler, which checks the HMAC on the raw bytes before
parsing anything. What these tests hold the controller to:

- a validly signed replay returns 200 and still leaves one queue row;
- the POST makes no Graph calls and creates no crm.lead; the cron drain does
  that later;
- no reject path echoes the App Secret or the verify token.

The raw_payload ACL test lives in test_webhook_security.py.
"""
import hashlib
import hmac
import io
import json
import socket
from unittest import mock

import werkzeug.wrappers

from odoo.tests.common import (
    HOST, TEST_CURSOR_COOKIE_NAME, HttpCase, TransactionCase, tagged,
)

from odoo.addons.meta_lead_ads.controllers.main import (
    MAX_WEBHOOK_BYTES, MetaWebhookController,
)

WEBHOOK_PATH = '/meta_lead_ads/webhook'
# Well-formed but wrong: the size checks run before the HMAC.
JUNK_SIG = 'sha256=' + '0' * 64
VERIFY_TOKEN_PARAM = 'meta_lead_ads.webhook_verify_token'
APP_SECRET = 'secret_test_hmac_key'
VERIFY_TOKEN = 'vtok_known_value_123'


def _signed(body_bytes, secret):
    """Return an ``X-Hub-Signature-256`` value (``sha256=<hex>``) for the body.

    Accepts str or bytes for both arguments; str is utf-8 encoded."""
    if isinstance(secret, str):
        secret = secret.encode('utf-8')
    if isinstance(body_bytes, str):
        body_bytes = body_bytes.encode('utf-8')
    digest = hmac.new(secret, body_bytes, hashlib.sha256).hexdigest()
    return 'sha256=' + digest


def _leadgen_payload(leadgen_id='LG_WH_1', page_id='PG1',
                     form_id='F1', ad_id='AD1'):
    """Minimal Page leadgen webhook envelope."""
    return {
        'object': 'page',
        'entry': [{
            'id': page_id,
            'time': 1700000000,
            'changes': [{
                'field': 'leadgen',
                'value': {
                    'leadgen_id': leadgen_id,
                    'page_id': page_id,
                    'form_id': form_id,
                    'ad_id': ad_id,
                    'created_time': 1700000000,
                },
            }],
        }],
    }


class WebhookFixtureMixin:
    """Account with a known app_secret, a page, and a known verify token.

    The controller reads the secret through
    ``meta.account._webhook_app_secret()``, so ``_signed`` uses the same one."""

    def setUp(self):
        super().setUp()
        self.account = self.env['meta.account'].create({
            'name': 'Acct', 'account_id': 'ACC1',
            'app_id': 'app_test', 'app_secret': APP_SECRET,
            'access_token': 'tok_acct',
        })
        self.page = self.env['meta.page'].create({
            'name': 'Page', 'page_id': 'PG1',
            'access_token': 'tok_test', 'account_id': self.account.id,
        })
        self.env['ir.config_parameter'].sudo().set_param(
            VERIFY_TOKEN_PARAM, VERIFY_TOKEN)
        self.Event = self.env['meta.webhook.event']

    def _post(self, body, sig_header=None):
        """POST `body` (dict, str or bytes) to the webhook.

        With ``sig_header=None`` no signature header is sent."""
        if isinstance(body, dict):
            raw = json.dumps(body).encode('utf-8')
        elif isinstance(body, str):
            raw = body.encode('utf-8')
        else:
            raw = body
        headers = {'Content-Type': 'application/json'}
        if sig_header is not None:
            headers['X-Hub-Signature-256'] = sig_header
        return self.url_open(WEBHOOK_PATH, data=raw, headers=headers)


@tagged('post_install', '-at_install')
class TestMetaWebhookVerify(WebhookFixtureMixin, HttpCase):
    """GET handshake: the right token echoes the challenge, anything else is
    a 403 without it."""

    def test_handshake_ok(self):
        challenge = 'challenge_42'
        resp = self.url_open(
            '%s?hub.mode=subscribe&hub.challenge=%s&hub.verify_token=%s'
            % (WEBHOOK_PATH, challenge, VERIFY_TOKEN))
        self.assertEqual(resp.status_code, 200)
        self.assertEqual(resp.text.strip(), challenge)

    def test_handshake_bad_token(self):
        challenge = 'challenge_42'
        # wrong token
        resp = self.url_open(
            '%s?hub.mode=subscribe&hub.challenge=%s&hub.verify_token=%s'
            % (WEBHOOK_PATH, challenge, 'WRONG_TOKEN'))
        self.assertEqual(resp.status_code, 403)
        self.assertNotIn(challenge, resp.text)
        # missing token
        resp2 = self.url_open(
            '%s?hub.mode=subscribe&hub.challenge=%s' % (WEBHOOK_PATH, challenge))
        self.assertEqual(resp2.status_code, 403)
        self.assertNotIn(challenge, resp2.text)


@tagged('post_install', '-at_install')
class TestMetaWebhookEvent(WebhookFixtureMixin, HttpCase):
    """POST receiver: verify, queue a pending row, answer 200."""

    def test_signed_post_queues(self):
        """A valid signature gives 200 and one pending event."""
        payload = _leadgen_payload(leadgen_id='LG_Q1')
        raw = json.dumps(payload).encode('utf-8')
        resp = self._post(raw, _signed(raw, APP_SECRET))
        self.assertEqual(resp.status_code, 200)
        self.assertEqual(
            self.Event.search_count([('leadgen_id', '=', 'LG_Q1')]), 1)
        evt = self.Event.search([('leadgen_id', '=', 'LG_Q1')], limit=1)
        self.assertEqual(evt.status, 'pending')

    def test_forged_sig_rejected(self):
        """A signature made with the wrong key gives 403 and nothing queued."""
        payload = _leadgen_payload(leadgen_id='LG_FORGE')
        raw = json.dumps(payload).encode('utf-8')
        forged = _signed(raw, 'this_is_the_wrong_secret')
        resp = self._post(raw, forged)
        self.assertEqual(resp.status_code, 403)
        self.assertEqual(
            self.Event.search_count([('leadgen_id', '=', 'LG_FORGE')]), 0)

    def test_unsigned_rejected(self):
        """No signature header gives 403 and nothing queued."""
        payload = _leadgen_payload(leadgen_id='LG_UNSIGNED')
        raw = json.dumps(payload).encode('utf-8')
        resp = self._post(raw, sig_header=None)
        self.assertEqual(resp.status_code, 403)
        self.assertEqual(
            self.Event.search_count([('leadgen_id', '=', 'LG_UNSIGNED')]), 0)

    def test_oversized_signed_body_rejected_413(self):
        """A body over the size cap gets 413 even when correctly signed."""
        big = b'{"x":"' + b'A' * (64 * 1024 + 100) + b'"}'
        resp = self._post(big, _signed(big, APP_SECRET))
        self.assertEqual(resp.status_code, 413)
        self.assertEqual(self.Event.search_count([]), 0)

    def test_oversized_unsigned_body_rejected_fast(self):
        """A declared length over the cap is refused by the route limit
        before the signature or the body is looked at."""
        big = b'{"x":"' + b'A' * (64 * 1024 + 100) + b'"}'
        resp = self._post(big, sig_header=None)
        self.assertEqual(resp.status_code, 413)
        self.assertEqual(resp.headers.get('Connection'), 'close')
        self.assertEqual(self.Event.search_count([]), 0)

    def test_replay_idempotent(self):
        """The same signed body sent twice gets 200 both times and one row."""
        payload = _leadgen_payload(leadgen_id='LG_REPLAY')
        raw = json.dumps(payload).encode('utf-8')
        sig = _signed(raw, APP_SECRET)
        resp1 = self._post(raw, sig)
        resp2 = self._post(raw, sig)
        self.assertEqual(resp1.status_code, 200)
        self.assertEqual(resp2.status_code, 200)
        # A replay is absorbed, not rejected: Meta re-sends on its own.
        self.assertFalse(400 <= resp1.status_code < 500)
        self.assertFalse(400 <= resp2.status_code < 500)
        self.assertEqual(
            self.Event.search_count([('leadgen_id', '=', 'LG_REPLAY')]), 1)

    def test_signed_malformed_json(self):
        """A good signature over non-JSON bytes gives 400 and nothing queued."""
        raw = b'this is not json {{{'
        sig = _signed(raw, APP_SECRET)
        before = self.Event.search_count([])
        resp = self._post(raw, sig)
        self.assertEqual(resp.status_code, 400)
        self.assertEqual(self.Event.search_count([]), before)

    def test_no_graph_in_request(self):
        """The POST makes no Graph calls and doesn't run ingest."""
        Client = type(self.env['meta.graph.client'])
        Ingest = type(self.env['meta.lead.ingest'])
        payload = _leadgen_payload(leadgen_id='LG_NOGRAPH')
        raw = json.dumps(payload).encode('utf-8')
        sig = _signed(raw, APP_SECRET)
        with mock.patch.object(Client, 'fetch_lead') as m_fetch, \
                mock.patch.object(Client, '_request') as m_request, \
                mock.patch.object(Ingest, 'ingest_leadgen') as m_ingest:
            resp = self._post(raw, sig)
        self.assertEqual(resp.status_code, 200)
        m_fetch.assert_not_called()
        m_request.assert_not_called()
        m_ingest.assert_not_called()

    def test_signed_post_no_lead_created(self):
        """The POST creates no crm.lead; only the cron drain does."""
        Lead = self.env['crm.lead']
        before = Lead.search_count([])
        payload = _leadgen_payload(leadgen_id='LG_NOLEAD')
        raw = json.dumps(payload).encode('utf-8')
        resp = self._post(raw, _signed(raw, APP_SECRET))
        self.assertEqual(resp.status_code, 200)
        self.assertEqual(Lead.search_count([]), before)

    def test_no_secret_leak(self):
        """No reject response contains the App Secret or the verify token."""
        payload = _leadgen_payload(leadgen_id='LG_LEAK')
        raw = json.dumps(payload).encode('utf-8')
        # forged signature
        forged = self._post(raw, _signed(raw, 'wrong_secret'))
        # unsigned
        unsigned = self._post(raw, sig_header=None)
        # bad verify-token handshake
        bad_token = self.url_open(
            '%s?hub.mode=subscribe&hub.challenge=c&hub.verify_token=WRONG'
            % WEBHOOK_PATH)
        for resp in (forged, unsigned, bad_token):
            self.assertNotIn(APP_SECRET, resp.text)
            self.assertNotIn(VERIFY_TOKEN, resp.text)

    # ---- body cap without Content-Length ---------------------------------

    def test_chunked_oversized_body_rejected_413(self):
        """A chunked body over the cap gets 413 and queues nothing."""
        def body():
            yield b'{"x":"'
            for _i in range(17):        # 17 x 4 KiB is over 64 KiB
                yield b'A' * 4096
            yield b'"}'
        # A generator body makes requests send Transfer-Encoding: chunked.
        resp = self.url_open(WEBHOOK_PATH, data=body(), headers={
            'Content-Type': 'application/json', 'Connection': 'close',
            'X-Hub-Signature-256': JUNK_SIG})
        self.assertEqual(resp.status_code, 413)
        self.assertEqual(self.Event.search_count([]), 0)

    def test_chunked_signed_body_under_cap_accepted(self):
        """A normal signed delivery sent chunked is still queued."""
        raw = json.dumps(_leadgen_payload(leadgen_id='LG_CHUNKED')).encode()

        def body():
            yield raw[:20]
            yield raw[20:]
        resp = self.url_open(WEBHOOK_PATH, data=body(), headers={
            'Content-Type': 'application/json',
            'X-Hub-Signature-256': _signed(raw, APP_SECRET)})
        self.assertEqual(resp.status_code, 200)
        self.assertEqual(
            self.Event.search_count([('leadgen_id', '=', 'LG_CHUNKED')]), 1)

    def _raw_exchange(self, head, body_parts):
        """Send a hand-built request on a fresh socket and read until the
        server closes it. A timeout here means the socket was kept open."""
        self.env.cr.flush()
        self.env.cr.clear()
        with self.allow_requests():
            cookie = '%s=%s' % (TEST_CURSOR_COOKIE_NAME, self.http_request_key)
            sock = socket.create_connection((HOST, self.http_port()),
                                            timeout=10)
            try:
                lines = ['POST %s HTTP/1.1' % WEBHOOK_PATH,
                         'Host: %s:%d' % (HOST, self.http_port()),
                         'Cookie: %s' % cookie] + head
                sock.sendall(('\r\n'.join(lines) + '\r\n\r\n').encode())
                for part in body_parts:
                    sock.sendall(part)
                received = b''
                while True:
                    chunk = sock.recv(65536)
                    if not chunk:
                        break
                    received += chunk
            finally:
                sock.close()
        head_bytes = received.split(b'\r\n\r\n', 1)[0]
        status_line, _sep, headers = head_bytes.partition(b'\r\n')
        return status_line, headers.lower()

    def test_oversized_chunked_closes_connection(self):
        """An oversized chunked body on a keep-alive request gets 413 and the
        server drops the socket, so the unread rest can't pose as a request."""
        parts = [b'1000\r\n' + b'A' * 4096 + b'\r\n'] * 17 + [b'0\r\n\r\n']
        status_line, headers = self._raw_exchange([
            'Content-Type: application/json',
            'Transfer-Encoding: chunked',
            'X-Hub-Signature-256: %s' % JUNK_SIG,
        ], parts)
        self.assertIn(b' 413 ', status_line)
        self.assertIn(b'connection: close', headers)
        self.assertEqual(self.Event.search_count([]), 0)
        # A new connection afterwards is served normally.
        raw = json.dumps(_leadgen_payload(leadgen_id='LG_AFTER')).encode()
        resp = self._post(raw, _signed(raw, APP_SECRET))
        self.assertEqual(resp.status_code, 200)
        self.assertEqual(self.Event.search_count([]), 1)

    def test_content_length_oversized_closes_connection(self):
        """A declared length over the cap gets 413 and a closed socket
        without the body being read."""
        status_line, headers = self._raw_exchange([
            'Content-Type: application/json',
            'Content-Length: %d' % (MAX_WEBHOOK_BYTES * 4),
            'X-Hub-Signature-256: %s' % JUNK_SIG,
        ], [b'{"x":"short"}'])
        self.assertIn(b' 413 ', status_line)
        self.assertIn(b'connection: close', headers)
        self.assertEqual(self.Event.search_count([]), 0)


class _EndlessBody(io.RawIOBase):
    """A request body that never ends, counting what was read from it."""

    def __init__(self):
        super().__init__()
        self.read_bytes = 0

    def readable(self):
        return True

    def readinto(self, b):
        size = len(b)
        b[:size] = b'A' * size
        self.read_bytes += size
        return size


@tagged('post_install', '-at_install')
class TestWebhookBodyCap(TransactionCase):
    """The webhook's read stops one byte past the cap."""

    def test_bounded_read_stops_at_limit(self):
        source = _EndlessBody()
        req = werkzeug.wrappers.Request({
            'wsgi.input': source, 'wsgi.input_terminated': True,
            'wsgi.url_scheme': 'http', 'REQUEST_METHOD': 'POST',
            'CONTENT_TYPE': 'application/json', 'PATH_INFO': WEBHOOK_PATH,
        })
        req.max_content_length = MAX_WEBHOOK_BYTES + 1
        self.assertEqual(len(req.get_data()), MAX_WEBHOOK_BYTES + 1)
        self.assertLessEqual(source.read_bytes, MAX_WEBHOOK_BYTES + 1)

    def test_route_carries_limit(self):
        routing = getattr(MetaWebhookController.meta_webhook,
                          'original_routing', None)
        if routing is None:
            rules = [rule for rule in self.env['ir.http'].routing_map()
                     .iter_rules() if rule.rule == WEBHOOK_PATH]
            self.assertTrue(rules)
            routing = rules[0].endpoint.routing
        self.assertEqual(routing.get('max_content_length'),
                         MAX_WEBHOOK_BYTES + 1)
