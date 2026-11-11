"""Instagram publisher – EXPERIMENTAL, written from Meta's Instagram API
(Instagram Login) docs and not yet tested with a real account.

Instagram posts must have a picture, and it must be a JPEG at a public URL.
YouTube videos use their thumbnail. Items without a picture (e.g. NWS
alerts) are skipped. Links in captions are not clickable on Instagram.
"""

from __future__ import annotations

from dataclasses import replace

from ..http import TIMEOUT, make_session
from ..models import Item, PublishResult, Rendered
from ..plugins import Field, Publisher, register_publisher
from ..text import compose
from ._meta import LongLivedTokenMixin

MAX_HASHTAGS = 30


@register_publisher
class InstagramPublisher(LongLivedTokenMixin, Publisher):
    name = "instagram"
    label = "Instagram"
    description = "Posts the video thumbnail with a caption. Weather alerts are skipped (no picture)."
    experimental = True
    char_limit = 2200
    refresh_url = "https://graph.instagram.com/refresh_access_token"
    refresh_grant = "ig_refresh_token"
    fields = [
        Field("access_token", "Long-lived access token", "password", secret=True, default="",
              help="Professional (Business or Creator) account, via a Meta app using Instagram API with "
                   "Instagram Login (instagram_business_basic + instagram_business_content_publish). "
                   "Renewed automatically every ~20 days."),
        Field("include_link", "Put the link in the caption", "bool", default=True,
              help="Instagram doesn't make caption links clickable, but people can copy them."),
        Field("api_version", "API version", default="v25.0"),
    ]

    def __init__(self, settings, context):
        super().__init__(settings, context)
        self.http = make_session()
        self._user_id: str | None = None

    @property
    def api(self) -> str:
        return f"https://graph.instagram.com/{(self.settings.get('api_version') or 'v25.0').strip()}"

    def accepts(self, item: Item) -> str | None:
        thumb = item.card.thumb_url if item.card else None
        if not thumb:
            return "Instagram needs a picture; this item has none."
        if not thumb.lower().split("?")[0].endswith((".jpg", ".jpeg")):
            return "Instagram only accepts JPEG pictures."
        return None

    def render(self, item: Item) -> Rendered:
        capped = replace(item, default_tags=item.default_tags[:MAX_HASHTAGS],
                         extra_tags=item.extra_tags[:max(0, MAX_HASHTAGS - len(item.default_tags))])
        r = compose(capped, self.char_limit, self.measure,
                    include_url=bool(self.settings.get("include_link", True) and item.url))
        return r

    def user_id(self) -> str:
        if not self._user_id:
            r = self.http.get(f"{self.api}/me", params={"fields": "user_id,username",
                                                          "access_token": self._token()}, timeout=TIMEOUT)
            if not r.ok:
                raise RuntimeError(f"Instagram: HTTP {r.status_code} {r.text[:200]}")
            d = r.json()
            self._user_id = str(d.get("user_id") or d["id"])
        return self._user_id

    def publish(self, item: Item, rendered: Rendered) -> PublishResult:
        try:
            self.renew_token_if_due()
        except Exception as e:
            self.ctx.storage and self.ctx.storage.event("error", f"Instagram: {e}")
        uid = self.user_id()
        r = self.http.post(f"{self.api}/{uid}/media", timeout=TIMEOUT, data={
            "image_url": rendered.card.thumb_url if rendered.card else item.card.thumb_url,
            "caption": rendered.text, "access_token": self._token()})
        if not r.ok:
            raise RuntimeError(f"Instagram (create): HTTP {r.status_code} {r.text[:300]}")
        container = r.json()["id"]
        self.wait_until_ready(f"{self.api}/{container}", "status_code")
        r = self.http.post(f"{self.api}/{uid}/media_publish", timeout=TIMEOUT,
                           data={"creation_id": container, "access_token": self._token()})
        if not r.ok:
            raise RuntimeError(f"Instagram (publish): HTTP {r.status_code} {r.text[:300]}")
        mid = r.json().get("id", "")
        url = ""
        p = self.http.get(f"{self.api}/{mid}", params={"fields": "permalink", "access_token": self._token()},
                          timeout=TIMEOUT)
        if p.ok:
            url = p.json().get("permalink", "")
        return PublishResult(post_id=mid, post_url=url)

    def check(self) -> str:
        r = self.http.get(f"{self.api}/me", params={"fields": "user_id,username", "access_token": self._token()},
                          timeout=TIMEOUT)
        if not r.ok:
            raise RuntimeError(f"HTTP {r.status_code}: {r.text[:200]}")
        return f"Signed in as @{r.json().get('username')}"
