"""Minimal OAuth 1.0a (HMAC-SHA1) request signing, as used by the X API.

Kept dependency-free on purpose. For JSON request bodies (X API v2) only the
oauth_* values and query parameters are signed, not the body.
"""

from __future__ import annotations

import base64
import hashlib
import hmac
import secrets
import time
from urllib.parse import quote, urlsplit, parse_qsl


def _enc(s) -> str:
    return quote(str(s), safe="~")


def signature(method: str, url: str, params: dict, consumer_secret: str, token_secret: str) -> str:
    parts = urlsplit(url)
    base_url = f"{parts.scheme.lower()}://{parts.netloc.lower()}{parts.path}"
    all_params = list(params.items()) + parse_qsl(parts.query, keep_blank_values=True)
    encoded = sorted((_enc(k), _enc(v)) for k, v in all_params)
    param_str = "&".join(f"{k}={v}" for k, v in encoded)
    base = "&".join([method.upper(), _enc(base_url), _enc(param_str)])
    key = f"{_enc(consumer_secret)}&{_enc(token_secret)}"
    digest = hmac.new(key.encode(), base.encode(), hashlib.sha1).digest()
    return base64.b64encode(digest).decode()


def auth_header(method: str, url: str, *, consumer_key: str, consumer_secret: str,
                token: str, token_secret: str, form: dict | None = None,
                nonce: str | None = None, timestamp: str | None = None) -> str:
    """Authorization header value. ``form`` = url-encoded body params (signed);
    leave it None for JSON bodies."""
    oauth = {
        "oauth_consumer_key": consumer_key,
        "oauth_nonce": nonce or secrets.token_hex(16),
        "oauth_signature_method": "HMAC-SHA1",
        "oauth_timestamp": timestamp or str(int(time.time())),
        "oauth_token": token,
        "oauth_version": "1.0",
    }
    oauth["oauth_signature"] = signature(method, url, {**oauth, **(form or {})},
                                         consumer_secret, token_secret)
    return "OAuth " + ", ".join(f'{_enc(k)}="{_enc(v)}"' for k, v in sorted(oauth.items()))
