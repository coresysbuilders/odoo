# Meta Lead Ads Integration for Odoo — © 2026 CoreSys Builders. All rights reserved.
# Licensed under the Odoo Proprietary License v1.0 (OPL-1).
# Unauthorized copying, redistribution, or resale of this software, in whole or in
# part, via any medium, is strictly prohibited and constitutes a license violation.
# OPL-1: https://www.odoo.com/documentation/18.0/legal/licenses.html#odoo-apps

"""Public webhook endpoint for Meta Lead Ads.

GET answers Meta's verify-token handshake. POST checks the
X-Hub-Signature-256 HMAC over the raw request bytes, stores a pending
meta.webhook.event row and returns 200. The Graph lookups and the lead
creation happen later in the cron drain, so the response stays fast; Meta
disables a subscription that keeps timing out.

The route is type='http' so get_data() gives us the exact bytes Meta signed.
Rejections return an empty body: never echo the payload or any secret, and
never log them either.
"""
import hashlib
import hmac
import json
import logging
import re

from odoo import http
from odoo.http import request, Response

_logger = logging.getLogger(__name__)

VERIFY_TOKEN_PARAM = 'meta_lead_ads.webhook_verify_token'
# Meta signs with 'sha256=' plus 64 lowercase hex chars. Checking the shape
# first means junk gets a plain 403; compare_digest raises TypeError on
# non-ASCII input, which would turn into a 500 and make Meta retry.
_SIG_HEX_RE = re.compile(r'[0-9a-f]{64}')
# Real leadgen notifications are a few KB. Anything over this is refused
# before we read the body, so an unsigned caller can't make us buffer it.
MAX_WEBHOOK_BYTES = 64 * 1024


class MetaWebhookController(http.Controller):

    @http.route('/meta_lead_ads/webhook', type='http', auth='public',
                csrf=False, methods=['GET', 'POST'], save_session=False)
    def meta_webhook(self, **kwargs):
        # save_session=False just avoids creating a session per webhook call.
        # If an Odoo build ever rejects the kwarg, drop it: the route would
        # otherwise fail to register at module load.
        if request.httprequest.method == 'GET':
            return self._handle_verify(kwargs)
        return self._handle_event()

    # ---- GET: verify-token handshake -------------------------------------
    def _handle_verify(self, kwargs):
        mode = kwargs.get('hub.mode')
        challenge = kwargs.get('hub.challenge')
        token = kwargs.get('hub.verify_token') or ''
        expected = request.env['ir.config_parameter'].sudo().get_param(
            VERIFY_TOKEN_PARAM) or ''
        if not expected:
            # Without this, subscribing in Meta before the onboarding wizard
            # has generated a token is just a silent 403.
            _logger.warning(
                'webhook verify_token not configured; GET handshake will fail')
        # The token comparison itself is constant-time. The early outs on mode
        # and an empty token are not, but they leak nothing about the token.
        if (mode == 'subscribe' and expected
                and hmac.compare_digest(str(token), str(expected))):
            return Response(challenge or '', status=200)
        return Response('', status=403)

    # ---- POST: signed leadgen event --------------------------------------
    def _handle_event(self):
        # Order matters: cheap header and size checks come before reading the
        # body, and the HMAC check comes before json parsing.
        header = request.httprequest.headers.get('X-Hub-Signature-256', '')
        if not header.startswith('sha256='):
            return Response('', status=403)
        sent_hex = header.split('=', 1)[1]
        if not _SIG_HEX_RE.fullmatch(sent_hex):
            return Response('', status=403)
        content_length = request.httprequest.content_length
        if content_length is not None and content_length > MAX_WEBHOOK_BYTES:
            return Response('', status=413)
        # The HMAC needs the bytes exactly as sent. Re-check the length for
        # chunked requests that declared none.
        raw = request.httprequest.get_data()
        if len(raw) > MAX_WEBHOOK_BYTES:
            return Response('', status=413)
        # The public user can't read meta.account, hence the sudo. With no
        # secret configured we can't authenticate anything, so refuse.
        app_secret = request.env['meta.account'].sudo()._webhook_app_secret()
        if not app_secret:
            return Response('', status=403)
        expected_hex = hmac.new(
            app_secret.encode('utf-8'), raw, hashlib.sha256).hexdigest()
        if not hmac.compare_digest(sent_hex, expected_hex):
            return Response('', status=403)
        try:
            data = json.loads(raw.decode('utf-8'))
        except (ValueError, UnicodeDecodeError):
            return Response('', status=400)                # signed but malformed
        # Don't commit the cursor by hand; Odoo commits at the end of the
        # request. If storing the event fails, answer 500 so Meta retries.
        try:
            request.env['meta.webhook.event'].sudo()._ingest_payload(
                data, raw=raw)
        except Exception:
            _logger.error("meta_webhook: could not persist verified event "
                          "(no secret/payload logged)")
            return Response('', status=500)
        # A signed replay also gets 200; the model dedups it.
        return Response('EVENT_RECEIVED', status=200)
