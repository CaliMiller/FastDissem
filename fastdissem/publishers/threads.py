"""Threads publisher – EXPERIMENTAL, written from Meta's Threads API docs and
not yet tested with a real account.

Two steps: create a TEXT container (with link_attachment for the preview
card), wait until it's ready, then publish it. Threads allows only ONE tag
per post, so the first hashtag is sent as the post's topic tag and the
others are left out.
"""

from __future__ import annotations

import re
from dataclasses import replace

from ..http import TIMEOUT, make_session
from ..models import Item, PublishResult, Rendered
from ..plugins import Field, Publisher, register_publisher
from ..text import compose, graphemes
from ._meta import LongLivedTokenMixin

API = "https://graph.threads.net/v1.0"


def threads_measure(text: str) -> int:
    """Threads counts each emoji as its UTF-8 byte length."""
    n = 0
    for g in graphemes(text):
        if any(ord(c) >= 0x2000 and not c.isalnum() and ord(c) not in range(0x2010, 0x2070) for c in g):
            n += len(g.encode())
        else:
            n += 1
    return n


def topic_tag(tags: list[str]) -> str | None:
    for t in tags:
        t = re.sub(r"[.&]", "", t)[:50]
        if t:
            return t
    return None


@register_publisher
class ThreadsPublisher(LongLivedTokenMixin, Publisher):
    name = "threads"
    label = "Threads"
    description = "Posts to Threads with a link preview. Threads allows one tag per post."
    experimental = True
    char_limit = 500
    refresh_url = "https://graph.threads.net/refresh_access_token"
    refresh_grant = "th_refresh_token"
    fields = [
        Field("access_token", "Long-lived access token", "password", secret=True, default="",
              help="From a Meta developer app with the Threads use case (threads_basic + "
                   "threads_content_publish). Fast Dissem renews it automatically every ~20 days."),
    ]

    def __init__(self, settings, context):
        super().__init__(settings, context)
        self.http = make_session()
        self._user_id: str | None = None

    def measure(self, text: str) -> int:
        return threads_measure(text)

    def render(self, item: Item) -> Rendered:
        tag = topic_tag(item.default_tags + item.extra_tags)
        compact = replace(item.compact, default_tags=[], extra_tags=[]) if item.compact else None
        bare = replace(item, default_tags=[], extra_tags=[], compact=compact)
        r = compose(bare, self.char_limit, self.measure, include_url=item.card is None and bool(item.url))
        r.tags = [tag] if tag else []
        r.note = f"Topic tag: #{tag}" if tag else ""
        return r

    def user_id(self) -> str:
        if not self._user_id:
            r = self.http.get(f"{API}/me", params={"fields": "id,username", "access_token": self._token()},
                              timeout=TIMEOUT)
            if not r.ok:
                raise RuntimeError(f"Threads: HTTP {r.status_code} {r.text[:200]}")
            self._user_id = r.json()["id"]
        return self._user_id

    def publish(self, item: Item, rendered: Rendered) -> PublishResult:
        try:
            self.renew_token_if_due()
        except Exception as e:      # a renewal hiccup shouldn't block a post
            self.ctx.storage and self.ctx.storage.event("error", f"Threads: {e}")
        uid = self.user_id()
        data = {"media_type": "TEXT", "text": rendered.text, "access_token": self._token()}
        link = rendered.card.url if rendered.card else None
        if link:
            data["link_attachment"] = link
        if rendered.tags:
            data["topic_tag"] = rendered.tags[0]
        r = self.http.post(f"{API}/{uid}/threads", data=data, timeout=TIMEOUT)
        if not r.ok:
            raise RuntimeError(f"Threads (create): HTTP {r.status_code} {r.text[:300]}")
        container = r.json()["id"]
        self.wait_until_ready(f"{API}/{container}", "status")
        r = self.http.post(f"{API}/{uid}/threads_publish", timeout=TIMEOUT,
                           data={"creation_id": container, "access_token": self._token()})
        if not r.ok:
            raise RuntimeError(f"Threads (publish): HTTP {r.status_code} {r.text[:300]}")
        pid = r.json().get("id", "")
        url = ""
        p = self.http.get(f"{API}/{pid}", params={"fields": "permalink", "access_token": self._token()},
                          timeout=TIMEOUT)
        if p.ok:
            url = p.json().get("permalink", "")
        return PublishResult(post_id=pid, post_url=url)

    def check(self) -> str:
        r = self.http.get(f"{API}/me", params={"fields": "id,username", "access_token": self._token()},
                          timeout=TIMEOUT)
        if not r.ok:
            raise RuntimeError(f"HTTP {r.status_code}: {r.text[:200]}")
        return f"Signed in as @{r.json().get('username')}"
