"""Mastodon / Fediverse publisher (any server with the Mastodon API:
Mastodon, GoToSocial, Pleroma/Akkoma, ...). The server builds the link
preview from the URL in the text, so the link is included in the post."""

from __future__ import annotations

from ..http import TIMEOUT, make_session
from ..models import Item, PublishResult, Rendered
from ..plugins import Field, Publisher, register_publisher
from ..text import mastodon_measure


@register_publisher
class MastodonPublisher(Publisher):
    name = "mastodon"
    label = "Mastodon / Fediverse"
    description = "Any server that speaks the Mastodon API."
    experimental = True
    include_url_in_text = True
    fields = [
        Field("instance_url", "Server URL", default="", help="e.g. https://mastodon.social"),
        Field("access_token", "Access token", "password", secret=True, default="",
              help="Preferences → Development → New application, scope write:statuses."),
        Field("visibility", "Visibility", "select", default="public",
              options=["public", "unlisted", "private"]),
        Field("char_limit", "Character limit", "int", default=500),
    ]

    def __init__(self, settings, context):
        super().__init__(settings, context)
        self.char_limit = int(settings.get("char_limit") or 500)
        self.http = make_session()
        self.base = (settings.get("instance_url") or "").rstrip("/")

    def measure(self, text: str) -> int:
        return mastodon_measure(text)

    def _headers(self, **extra):
        if not self.base or not self.settings.get("access_token"):
            raise RuntimeError("Mastodon server URL and access token are not set.")
        return {"Authorization": f"Bearer {self.settings['access_token']}", **extra}

    def publish(self, item: Item, rendered: Rendered) -> PublishResult:
        # Idempotency-Key makes the server ignore an accidental resend.
        r = self.http.post(f"{self.base}/api/v1/statuses", timeout=TIMEOUT,
                           headers=self._headers(**{"Idempotency-Key": item.uid[:64]}),
                           data={"status": rendered.text, "visibility": self.settings.get("visibility", "public")})
        if not r.ok:
            raise RuntimeError(f"Mastodon: HTTP {r.status_code} {r.text[:200]}")
        d = r.json()
        return PublishResult(post_id=str(d.get("id", "")), post_url=d.get("url", ""))

    def check(self) -> str:
        r = self.http.get(f"{self.base}/api/v1/accounts/verify_credentials", headers=self._headers(),
                          timeout=TIMEOUT)
        if not r.ok:
            raise RuntimeError(f"HTTP {r.status_code}: {r.text[:200]}")
        return f"Signed in as @{r.json().get('acct')} on {self.base}"
