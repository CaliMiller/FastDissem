"""Bluesky (AT Protocol) publisher, using plain XRPC calls.

Posts carry rich-text facets so hashtags and links are clickable, and an
``app.bsky.embed.external`` card with a thumbnail. For YouTube links the
Bluesky apps turn that card into an inline, playable video player.
"""

from __future__ import annotations

import logging
import threading
from datetime import datetime, timezone

from ..http import TIMEOUT, make_session
from ..models import Item, PublishResult, Rendered
from ..plugins import Field, Publisher, register_publisher
from ..text import HASHTAG_RE, URL_RE, grapheme_len, truncate

log = logging.getLogger(__name__)

MAX_THUMB_BYTES = 1_000_000


def byte_index(text: str, char_index: int) -> int:
    return len(text[:char_index].encode("utf-8"))


def build_facets(text: str) -> list[dict]:
    facets = []
    for m in URL_RE.finditer(text):
        facets.append({
            "index": {"byteStart": byte_index(text, m.start()), "byteEnd": byte_index(text, m.end())},
            "features": [{"$type": "app.bsky.richtext.facet#link", "uri": m.group(0)}],
        })
    for m in HASHTAG_RE.finditer(text):
        tag = m.group(1)
        if tag.isdigit() or grapheme_len(tag) > 64:
            continue
        start = m.start(1) - 1  # include '#'
        facets.append({
            "index": {"byteStart": byte_index(text, start), "byteEnd": byte_index(text, m.end(1))},
            "features": [{"$type": "app.bsky.richtext.facet#tag", "tag": tag}],
        })
    return facets


class BlueskyError(RuntimeError):
    pass


@register_publisher
class BlueskyPublisher(Publisher):
    name = "bluesky"
    label = "Bluesky"
    description = "Posts with clickable hashtags and a link/video preview card."
    char_limit = 300
    include_url_in_text = False   # the preview card carries the link
    fields = [
        Field("handle", "Handle", default="", help="e.g. yourname.bsky.social"),
        Field("app_password", "App password", "password", secret=True, default="",
              help="Create one in Bluesky: Settings → Privacy and security → App passwords. "
                   "Never use your main password."),
        Field("pds_url", "PDS / server URL", default="https://bsky.social",
              help="Leave as is unless you host your own server."),
        Field("language", "Post language", default="en"),
        Field("link_in_text_when_no_card", "Put the link in the text when there's no card", "bool",
              default=True),
    ]

    def __init__(self, settings, context):
        super().__init__(settings, context)
        self.http = make_session()
        self._lock = threading.Lock()
        self.session: dict | None = None
        self.pds = (settings.get("pds_url") or "https://bsky.social").rstrip("/")

    # ---------------------------------------------------------------- auth
    def _xrpc(self, method: str, nsid: str, *, auth: str | None = None, **kw):
        headers = kw.pop("headers", {})
        if auth:
            headers["Authorization"] = f"Bearer {auth}"
        r = self.http.request(method, f"{self.pds}/xrpc/{nsid}", headers=headers, timeout=TIMEOUT, **kw)
        try:
            data = r.json()
        except ValueError:
            data = {}
        if not r.ok:
            raise BlueskyError(f"{nsid}: HTTP {r.status_code} {data.get('error', '')} {data.get('message', '')}".strip())
        return data

    def _login(self) -> None:
        if not self.settings.get("handle") or not self.settings.get("app_password"):
            raise BlueskyError("Bluesky handle and app password are not set.")
        data = self._xrpc("POST", "com.atproto.server.createSession", json={
            "identifier": self.settings["handle"].strip().lstrip("@"),
            "password": self.settings["app_password"].strip()})
        self._set_session(data)

    def _set_session(self, data: dict) -> None:
        self.session = data
        for svc in (data.get("didDoc") or {}).get("service", []):
            if svc.get("id", "").endswith("#atproto_pds") and svc.get("serviceEndpoint"):
                self.pds = svc["serviceEndpoint"].rstrip("/")

    def _refresh(self) -> None:
        try:
            data = self._xrpc("POST", "com.atproto.server.refreshSession",
                              auth=self.session["refreshJwt"])
            self._set_session({**self.session, **data})
        except BlueskyError:
            self._login()

    def call(self, method: str, nsid: str, **kw):
        with self._lock:
            if not self.session:
                self._login()
            try:
                return self._xrpc(method, nsid, auth=self.session["accessJwt"], **kw)
            except BlueskyError as e:
                if "ExpiredToken" not in str(e) and "InvalidToken" not in str(e):
                    raise
                self._refresh()
                return self._xrpc(method, nsid, auth=self.session["accessJwt"], **kw)

    # -------------------------------------------------------------- posting
    def render(self, item: Item) -> Rendered:
        from ..text import compose
        with_url = item.card is None and self.settings.get("link_in_text_when_no_card", True)
        return compose(item, self.char_limit, self.measure, include_url=with_url)

    def upload_thumb(self, url: str) -> dict | None:
        try:
            r = self.http.get(url, timeout=TIMEOUT)
            r.raise_for_status()
            if len(r.content) > MAX_THUMB_BYTES:
                log.warning("Thumbnail too large for Bluesky (%d bytes), skipping", len(r.content))
                return None
            ctype = r.headers.get("Content-Type", "image/jpeg").split(";")[0]
            return self.call("POST", "com.atproto.repo.uploadBlob", data=r.content,
                             headers={"Content-Type": ctype})["blob"]
        except Exception as e:  # a missing thumbnail must not block the post
            log.warning("Thumbnail upload failed: %s", e)
            return None

    def build_record(self, rendered: Rendered, thumb_blob: dict | None) -> dict:
        record = {
            "$type": "app.bsky.feed.post",
            "text": rendered.text,
            "createdAt": datetime.now(timezone.utc).isoformat(timespec="milliseconds").replace("+00:00", "Z"),
            "langs": [self.settings.get("language") or "en"],
        }
        facets = build_facets(rendered.text)
        if facets:
            record["facets"] = facets
        card = rendered.card
        if card:
            ext = {"uri": card.url, "title": truncate(card.title, 300),
                   "description": truncate(card.description, 1000)}
            if thumb_blob:
                ext["thumb"] = thumb_blob
            record["embed"] = {"$type": "app.bsky.embed.external", "external": ext}
        return record

    def publish(self, item: Item, rendered: Rendered) -> PublishResult:
        thumb = self.upload_thumb(rendered.card.thumb_url) if rendered.card and rendered.card.thumb_url else None
        if not self.session:
            with self._lock:
                self._login()
        record = self.build_record(rendered, thumb)
        data = self.call("POST", "com.atproto.repo.createRecord", json={
            "repo": self.session["did"], "collection": "app.bsky.feed.post", "record": record})
        uri = data.get("uri", "")
        rkey = uri.rsplit("/", 1)[-1]
        handle = self.session.get("handle") or self.session["did"]
        return PublishResult(post_id=uri, post_url=f"https://bsky.app/profile/{handle}/post/{rkey}")

    def check(self) -> str:
        with self._lock:
            self._login()
        return f"Signed in as @{self.session.get('handle')} ({self.pds})"
