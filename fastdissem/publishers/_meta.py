"""Shared helpers for Meta's Threads and Instagram APIs (not a network itself;
files starting with "_" aren't loaded as plug-ins).

Long-lived Threads/Instagram tokens last 60 days and can be renewed once they
are at least a day old. Renewing every ~20 days keeps them alive as long as
Fast Dissem keeps running, even when nothing is being posted.
"""

from __future__ import annotations

import hashlib
import logging
import time
from datetime import datetime, timedelta, timezone

from ..http import TIMEOUT

log = logging.getLogger(__name__)

RENEW_AFTER = timedelta(days=20)


def _fingerprint(token: str) -> str:
    return hashlib.sha256(token.encode()).hexdigest()[:16]


class LongLivedTokenMixin:
    """Needs: self.settings["access_token"], self.http, self.remember(),
    and class attrs refresh_url / refresh_grant."""

    refresh_url = ""
    refresh_grant = ""

    def _token(self) -> str:
        tok = (self.settings.get("access_token") or "").strip()
        if not tok:
            raise RuntimeError(f"{self.label}: access token is not set.")
        return tok

    def renew_token_if_due(self, force: bool = False) -> bool:
        tok = self._token()
        now = datetime.now(timezone.utc)
        if self.settings.get("token_fingerprint") != _fingerprint(tok):
            # A token pasted in Settings: new tokens are good for 60 days.
            self.remember("token_fingerprint", _fingerprint(tok))
            self.remember("token_renewed_at", now.isoformat(timespec="seconds"))
            if not force:
                return False
        try:
            last = datetime.fromisoformat(self.settings.get("token_renewed_at") or "")
        except ValueError:
            last = now - RENEW_AFTER
        if not force and now - last < RENEW_AFTER:
            return False
        r = self.http.get(self.refresh_url, timeout=TIMEOUT,
                          params={"grant_type": self.refresh_grant, "access_token": tok})
        if not r.ok:
            raise RuntimeError(f"couldn't renew the access token (HTTP {r.status_code}: {r.text[:200]}). "
                               "If it has expired, paste a new one in Settings.")
        new = r.json().get("access_token")
        if new:
            self.remember("access_token", new)
            self.remember("token_fingerprint", _fingerprint(new))
        self.remember("token_renewed_at", now.isoformat(timespec="seconds"))
        days = int(r.json().get("expires_in", 0)) // 86400
        log.info("%s access token renewed%s", self.label, f" (valid {days} days)" if days else "")
        return True

    def maintain(self) -> None:
        if self.settings.get("access_token"):
            self.renew_token_if_due()

    def wait_until_ready(self, url: str, field: str, timeout_s: int = 90) -> None:
        """Poll a media container until it is ready to publish."""
        deadline = time.time() + timeout_s
        delay = 3
        while True:
            r = self.http.get(url, params={"fields": f"{field},error_message",
                                           "access_token": self._token()}, timeout=TIMEOUT)
            status = r.json().get(field) if r.ok else None
            if status in ("FINISHED", None) and r.ok:
                return
            if status in ("ERROR", "EXPIRED"):
                raise RuntimeError(f"{self.label}: media processing {status.lower()}: "
                                   f"{r.json().get('error_message', '')}")
            if time.time() > deadline:
                raise RuntimeError(f"{self.label}: media still processing after {timeout_s}s")
            time.sleep(delay)
            delay = min(delay * 2, 15)
