"""Facebook Page publisher – EXPERIMENTAL, written from Meta's Pages API docs
and not yet tested with a real Page.

POST https://graph.facebook.com/{version}/{page-id}/feed with message + link.
Facebook builds the link preview (YouTube links play inline). Posting to a
personal profile is not possible through the API; it must be a Page.
"""

from __future__ import annotations

from ..http import TIMEOUT, make_session
from ..models import Item, PublishResult, Rendered
from ..plugins import Field, Publisher, register_publisher
from ..text import compose


@register_publisher
class FacebookPublisher(Publisher):
    name = "facebook"
    label = "Facebook Page"
    description = "Posts to a Facebook Page you manage, with a link preview."
    experimental = True
    char_limit = 5000       # Facebook allows ~63k; keep posts readable
    fields = [
        Field("page_id", "Page ID", default="",
              help="Your Page → About → Page transparency, or the number in the Page's profile link."),
        Field("page_token", "Page access token", "password", secret=True, default="",
              help="Needs pages_manage_posts and pages_read_engagement. A Page token made from a "
                   "long-lived user token does not expire."),
        Field("api_version", "Graph API version", default="v25.0"),
    ]

    def __init__(self, settings, context):
        super().__init__(settings, context)
        self.http = make_session()

    def _base(self) -> str:
        s = self.settings
        if not s.get("page_id") or not s.get("page_token"):
            raise RuntimeError("Facebook Page ID and Page access token are not set.")
        return f"https://graph.facebook.com/{(s.get('api_version') or 'v25.0').strip()}"

    def render(self, item: Item) -> Rendered:
        # The link goes in the "link" field (Facebook makes the preview), not the text.
        return compose(item, self.char_limit, self.measure, include_url=False)

    def publish(self, item: Item, rendered: Rendered) -> PublishResult:
        data = {"message": rendered.text, "access_token": self.settings["page_token"]}
        link = (rendered.card.url if rendered.card else None) or item.url
        if link:
            data["link"] = link
        r = self.http.post(f"{self._base()}/{self.settings['page_id']}/feed", data=data, timeout=TIMEOUT)
        if not r.ok:
            raise RuntimeError(f"Facebook: HTTP {r.status_code} {r.text[:300]}")
        pid = r.json().get("id", "")
        return PublishResult(post_id=pid, post_url=f"https://www.facebook.com/{pid}" if pid else "")

    def check(self) -> str:
        r = self.http.get(f"{self._base()}/{self.settings['page_id']}", timeout=TIMEOUT,
                          params={"fields": "name", "access_token": self.settings["page_token"]})
        if not r.ok:
            raise RuntimeError(f"HTTP {r.status_code}: {r.text[:200]}")
        return f"Connected to the Page “{r.json().get('name')}”"
