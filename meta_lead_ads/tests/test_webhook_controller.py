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
import json
from unittest import mock

from odoo.tests.common import HttpCase, tagged

WEBHOOK_PATH = '/meta_lead_ads/webhook'
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
        """An oversized unsigned body fails on the missing header (403)."""
        big = b'{"x":"' + b'A' * (64 * 1024 + 100) + b'"}'
        resp = self._post(big, sig_header=None)
        self.assertEqual(resp.status_code, 403)
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
