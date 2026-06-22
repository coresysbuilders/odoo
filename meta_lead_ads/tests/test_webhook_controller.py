# Meta Lead Ads Integration for Odoo — © 2026 CoreSys Builders. All rights reserved.
# Licensed under the Odoo Proprietary License v1.0 (OPL-1).
# Unauthorized copying, redistribution, or resale of this software, in whole or in
# part, via any medium, is strictly prohibited and constitutes a license violation.
# OPL-1: https://www.odoo.com/documentation/18.0/legal/licenses.html#odoo-apps

"""Tests for the public webhook controller.

Pins the security + behavioral contract of the public route
``/meta_lead_ads/webhook``. This is the module's HttpCase: real HTTP requests
hit the live test server, so the GET verify-token handshake and the POST
HMAC-verify-before-parse gate are exercised end-to-end.

Load-bearing contract pinned here:
  * A validly-signed replay of the same payload returns 200 with exactly one
    queue row (idempotency, not a 4xx). ``test_replay_idempotent`` explicitly
    asserts neither response is a 4xx.
  * The POST handler does zero Graph work in-request: ``fetch_lead`` and
    ``_request`` and ``ingest_leadgen`` are all asserted not-called during the
    request.
  * A valid signed POST creates zero ``crm.lead`` in-request -- the sole
    lead-creation path is the deferred cron drain.
  * No reject path (forged / unsigned / bad-token) ever echoes the App Secret or
    the verify_token.

The raw_payload PII-ACL contract (TestMetaWebhookSecurity.test_raw_payload_acl)
lives in its own module ``tests/test_webhook_security.py`` (TransactionCase).

Conventions used throughout:
  - ``assertRaises`` takes a single exception class, never a tuple.
  - Use ``search_count(...)``; the legacy ``count=`` kwarg was removed.
  - A group-gated explicit read raises ``AccessError`` rather than silently
    omitting.
  - Patch Graph/ingest methods on ``type(...)`` (the class), never a recordset.
"""
import hashlib
import hmac
import json
from unittest import mock

from odoo.tests.common import HttpCase, tagged

WEBHOOK_PATH = '/meta_lead_ads/webhook'
VERIFY_TOKEN_PARAM = 'meta_lead_ads.webhook_verify_token'
APP_SECRET = 'secret_test_hmac_key'
VERIFY_TOKEN = 'vtok_known_value_123'


def _signed(body_bytes, secret):
    """Build a Meta ``X-Hub-Signature-256`` header value over the raw bytes.

    Returns ``'sha256=' + HMAC-SHA256(secret, body).hexdigest()`` -- a 64-char
    lowercase hex digest, exactly the shape the controller verifies with
    ``hmac.compare_digest`` before ``json.loads``. ``secret`` and ``body_bytes``
    are bytes; accepts str for convenience and encodes utf-8."""
    if isinstance(secret, str):
        secret = secret.encode('utf-8')
    if isinstance(body_bytes, str):
        body_bytes = body_bytes.encode('utf-8')
    digest = hmac.new(secret, body_bytes, hashlib.sha256).hexdigest()
    return 'sha256=' + digest


def _leadgen_payload(leadgen_id='LG_WH_1', page_id='PG1',
                     form_id='F1', ad_id='AD1'):
    """A minimal Meta Page-leadgen webhook envelope: object='page',
    entry[].id == page_id, changes[].field=='leadgen', value carries the
    ids."""
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
    """meta.account (with a known app_secret) + meta.page + a known
    verify_token config param. The app_secret is what ``_signed`` HMACs with;
    the controller reads it via ``meta.account._webhook_app_secret()`` (sudo)."""

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
        """POST raw bytes to the webhook route with the live test session.

        ``body`` may be a dict (json-encoded to bytes) or raw bytes/str. When
        ``sig_header`` is None NO ``X-Hub-Signature-256`` is sent (unsigned
        path). Returns the ``requests``-style response from ``url_open``."""
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
    """GET verify-token handshake. Correct token + mode echoes the challenge; a
    wrong/missing token is rejected 403 with no challenge echoed."""

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
        # missing token entirely
        resp2 = self.url_open(
            '%s?hub.mode=subscribe&hub.challenge=%s' % (WEBHOOK_PATH, challenge))
        self.assertEqual(resp2.status_code, 403)
        self.assertNotIn(challenge, resp2.text)


@tagged('post_install', '-at_install')
class TestMetaWebhookEvent(WebhookFixtureMixin, HttpCase):
    """The POST receiver. HMAC over raw bytes before any parse; ack-then-defer
    (queue a pending row, return 200 fast); zero Graph in request; zero crm.lead
    in request; no secret leak on any reject path."""

    def test_signed_post_queues(self):
        """A valid signature -> 200 + exactly one pending
        meta.webhook.event."""
        payload = _leadgen_payload(leadgen_id='LG_Q1')
        raw = json.dumps(payload).encode('utf-8')
        resp = self._post(raw, _signed(raw, APP_SECRET))
        self.assertEqual(resp.status_code, 200)
        self.assertEqual(
            self.Event.search_count([('leadgen_id', '=', 'LG_Q1')]), 1)
        evt = self.Event.search([('leadgen_id', '=', 'LG_Q1')], limit=1)
        self.assertEqual(evt.status, 'pending')

    def test_forged_sig_rejected(self):
        """A signature computed with the wrong key -> 403 and zero queue rows
        (no parse, no persist)."""
        payload = _leadgen_payload(leadgen_id='LG_FORGE')
        raw = json.dumps(payload).encode('utf-8')
        forged = _signed(raw, 'this_is_the_wrong_secret')
        resp = self._post(raw, forged)
        self.assertEqual(resp.status_code, 403)
        self.assertEqual(
            self.Event.search_count([('leadgen_id', '=', 'LG_FORGE')]), 0)

    def test_unsigned_rejected(self):
        """No X-Hub-Signature-256 header -> 403, zero queue rows."""
        payload = _leadgen_payload(leadgen_id='LG_UNSIGNED')
        raw = json.dumps(payload).encode('utf-8')
        resp = self._post(raw, sig_header=None)
        self.assertEqual(resp.status_code, 403)
        self.assertEqual(
            self.Event.search_count([('leadgen_id', '=', 'LG_UNSIGNED')]), 0)

    def test_oversized_signed_body_rejected_413(self):
        """A body past the size cap is rejected with 413 and never queued, even
        with a valid signature — the size gate fires before any persist."""
        big = b'{"x":"' + b'A' * (64 * 1024 + 100) + b'"}'
        resp = self._post(big, _signed(big, APP_SECRET))
        self.assertEqual(resp.status_code, 413)
        self.assertEqual(self.Event.search_count([]), 0)

    def test_oversized_unsigned_body_rejected_fast(self):
        """An oversized unsigned body is rejected (403 on the missing header,
        before the body is processed) and never queued."""
        big = b'{"x":"' + b'A' * (64 * 1024 + 100) + b'"}'
        resp = self._post(big, sig_header=None)
        self.assertEqual(resp.status_code, 403)
        self.assertEqual(self.Event.search_count([]), 0)

    def test_replay_idempotent(self):
        """POST the same validly-signed bytes twice -> both responses 200
        (neither is a 4xx) and exactly one queue row. A replayed valid payload is
        idempotent, not a rejection -- the dedup absorbs it."""
        payload = _leadgen_payload(leadgen_id='LG_REPLAY')
        raw = json.dumps(payload).encode('utf-8')
        sig = _signed(raw, APP_SECRET)
        resp1 = self._post(raw, sig)
        resp2 = self._post(raw, sig)
        self.assertEqual(resp1.status_code, 200)
        self.assertEqual(resp2.status_code, 200)
        # Explicitly NOT a 4xx on either response (idempotency, not rejection).
        self.assertFalse(400 <= resp1.status_code < 500)
        self.assertFalse(400 <= resp2.status_code < 500)
        self.assertEqual(
            self.Event.search_count([('leadgen_id', '=', 'LG_REPLAY')]), 1)

    def test_signed_malformed_json(self):
        """A valid signature over non-JSON bytes -> 400 (signature genuine but
        body unparseable) + zero queue rows."""
        raw = b'this is not json {{{'
        sig = _signed(raw, APP_SECRET)
        before = self.Event.search_count([])
        resp = self._post(raw, sig)
        self.assertEqual(resp.status_code, 400)
        self.assertEqual(self.Event.search_count([]), before)

    def test_no_graph_in_request(self):
        """A valid signed POST does zero Graph work in-request. Patch
        ``fetch_lead`` and ``_request`` (on the graph client class) and
        ``ingest_leadgen`` (on the ingest class) and assert none was called
        during the request -- the handler only persists a pending row; the cron
        drain (out of band) is the sole Graph caller."""
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
        """A valid signed POST creates zero crm.lead in-request. The
        controller/queue never create a lead; only the deferred cron drain does.
        Capture the crm.lead count before and assert it is unchanged after the
        200."""
        Lead = self.env['crm.lead']
        before = Lead.search_count([])
        payload = _leadgen_payload(leadgen_id='LG_NOLEAD')
        raw = json.dumps(payload).encode('utf-8')
        resp = self._post(raw, _signed(raw, APP_SECRET))
        self.assertEqual(resp.status_code, 200)
        self.assertEqual(Lead.search_count([]), before)

    def test_no_secret_leak(self):
        """Across every reject path (forged sig, unsigned, bad verify-token) the
        response body never echoes the App Secret or the verify_token."""
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
