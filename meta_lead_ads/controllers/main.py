# Meta Lead Ads Integration for Odoo — © 2026 CoreSys Builders. All rights reserved.
# Licensed under the Odoo Proprietary License v1.0 (OPL-1).
# Unauthorized copying, redistribution, or resale of this software, in whole or in
# part, via any medium, is strictly prohibited and constitutes a license violation.
# OPL-1: https://www.odoo.com/documentation/18.0/legal/licenses.html#odoo-apps

"""Public ack-then-defer webhook for Meta Lead Ads.

One route at /meta_lead_ads/webhook:
  * GET  completes Meta's verify-token handshake (constant-time on the
         configured-token path).
  * POST verifies the X-Hub-Signature-256 HMAC over the raw request bytes
         with hmac.compare_digest before any json.loads, then inserts one
         pending meta.webhook.event row and returns 200 only after that row
         is durably persisted (Odoo's request-end auto-commit). On a
         persistence failure it returns a token-free 500 so Meta retries.

Invariants:
  * type='http' (not 'json') so request.httprequest.get_data() returns the
    exact raw bytes Meta signed; csrf=False.
  * No Graph reads in-request: this file imports no Graph client and makes
    no outbound Graph call. The cron drain does the deferred Graph work.
  * No crm.lead creation here -- the sole create path is the deferred
    ingest_leadgen via the cron drain.
  * Every reject and the 500 return an empty body plus a status code --
    never echo the payload, the App Secret, or the verify token, and no log
    line ever carries a secret or the payload.
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
# A genuine Meta signature is 'sha256=' + a 64-char lowercase hex digest. The
# hex shape is validated before compare_digest so a crafted oversized/typed
# digest gets the same empty 403 and never reaches compare_digest (which would
# otherwise TypeError -> 500 -> Meta needlessly retries).
_SIG_HEX_RE = re.compile(r'[0-9a-f]{64}')
# A genuine leadgen notification is a few KB. Cap well above that and reject
# anything larger before buffering the body, so an unauthenticated caller
# cannot force a worker to read a large unsigned payload into memory.
MAX_WEBHOOK_BYTES = 64 * 1024


class MetaWebhookController(http.Controller):

    @http.route('/meta_lead_ads/webhook', type='http', auth='public',
                csrf=False, methods=['GET', 'POST'], save_session=False)
    def meta_webhook(self, **kwargs):
        # One fixed public route, auth='public', csrf=False, GET+POST.
        # save_session=False is a minor hardening; if an Odoo build rejects the
        # kwarg, a TypeError on an unsupported kwarg fails the whole controllers
        # package registration at module load, so omit it then -- behavior is
        # unaffected.
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
            # Setup diagnostic: a Meta-side handshake configured before the
            # wizard generates the token would otherwise be a silent 403
            # deadlock. Log only that it is empty -- never the token value.
            _logger.warning(
                'webhook verify_token not configured; GET handshake will fail')
        # Constant-time digest compare on the configured-token path (no == on the
        # token). NOTE: the overall branch is not constant-time across every reject
        # path (mode / empty-token short-circuits resolve fast) -- the
        # security-relevant guarantee is the constant-time compare_digest on the
        # configured-token path, not full-branch constant-time.
        if (mode == 'subscribe' and expected
                and hmac.compare_digest(str(token), str(expected))):
            return Response(challenge or '', status=200)
        return Response('', status=403)

    # ---- POST: signed leadgen event --------------------------------------
    def _handle_event(self):
        # Strict ordering matters here. Do the cheap header/size rejections
        # BEFORE buffering the body, so an unauthenticated caller cannot force a
        # worker to read a large unsigned payload into memory.
        # 1. Signature header must be "sha256=<hex>"; unsigned / wrong scheme
        #    -> 403. No body read yet.
        header = request.httprequest.headers.get('X-Hub-Signature-256', '')
        if not header.startswith('sha256='):
            return Response('', status=403)
        sent_hex = header.split('=', 1)[1]
        # 2. Validate the digest shape before compare: exactly 64 lowercase hex
        #    chars. A malformed/forged digest gets the same empty 403.
        if not _SIG_HEX_RE.fullmatch(sent_hex):
            return Response('', status=403)
        # 3. Reject an oversized declared body before reading it -> 413.
        content_length = request.httprequest.content_length
        if content_length is not None and content_length > MAX_WEBHOOK_BYTES:
            return Response('', status=413)
        # 4. Now buffer the raw bytes -- needed verbatim for the HMAC. The len
        #    re-check also covers a chunked request that declared no length.
        raw = request.httprequest.get_data()               # bytes, untouched
        if len(raw) > MAX_WEBHOOK_BYTES:
            return Response('', status=413)
        # 5. App Secret via a narrow named sudo (the public user has no record
        #    ACL; never log/echo it). No configured secret -> cannot
        #    authenticate any POST -> empty 403.
        app_secret = request.env['meta.account'].sudo()._webhook_app_secret()
        if not app_secret:
            return Response('', status=403)
        expected_hex = hmac.new(
            app_secret.encode('utf-8'), raw, hashlib.sha256).hexdigest()
        # 6. Constant-time compare before json.loads -- forged -> 403, no parse.
        if not hmac.compare_digest(sent_hex, expected_hex):
            return Response('', status=403)
        # 7. Parse only after the signature is proven genuine.
        try:
            data = json.loads(raw.decode('utf-8'))
        except (ValueError, UnicodeDecodeError):
            return Response('', status=400)                # signed but malformed
        # 8. Durable-persist-then-200: the model owns the dedup INSERT and
        #    creates no crm.lead. Do not manually commit the request cursor
        #    here -- a manual commit bypasses Odoo's transaction manager and can
        #    corrupt the ORM cache; Odoo's request-end auto-commit makes the
        #    pending row durable. If the persist raises, return a token-free
        #    empty 500 so Meta retries (never a premature 200, never a leak in
        #    the 500 body).
        try:
            request.env['meta.webhook.event'].sudo()._ingest_payload(
                data, raw=raw)
        except Exception:
            _logger.error("meta_webhook: could not persist verified event "
                          "(no secret/payload logged)")
            return Response('', status=500)
        # 9. Fast 200 for a genuine signature so Meta does not disable the
        #    subscription. A validly-signed replay reaches here too and also
        #    returns 200 (the model dedups; no duplicate) -- never a 4xx for a
        #    valid replay.
        return Response('EVENT_RECEIVED', status=200)
