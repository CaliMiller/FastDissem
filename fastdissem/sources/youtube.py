"""YouTube: detect new uploads on one or more channels.

Detection uses the channel's public RSS feed (no key needed). If a YouTube
Data API v3 key is set, the uploads playlist is read through the API instead
and each video's tags are fetched (RSS does not carry tags). Without a key,
hashtags written in the video description are used as tags.
"""

from __future__ import annotations

import logging
import re
import xml.etree.ElementTree as ET

from ..http import TIMEOUT, make_session
from ..models import Item, LinkCard
from ..plugins import Field, Source, register_source
from ..text import HASHTAG_RE, dedupe_tags, parse_tag_list, to_hashtag, truncate

log = logging.getLogger(__name__)

NS = {
    "a": "http://www.w3.org/2005/Atom",
    "yt": "http://www.youtube.com/xml/schemas/2015",
    "media": "http://search.yahoo.com/mrss/",
}
API = "https://www.googleapis.com/youtube/v3"
CHANNEL_ID_RE = re.compile(r"\b(UC[\w-]{22})\b")


def thumb_url(video_id: str) -> str:
    # hqdefault always exists (maxresdefault does not for every video)
    return f"https://i.ytimg.com/vi/{video_id}/hqdefault.jpg"


def parse_rss(xml_text: str) -> list[dict]:
    root = ET.fromstring(xml_text)
    channel_title = root.findtext("a:title", default="", namespaces=NS)
    out = []
    for e in root.findall("a:entry", NS):
        vid = e.findtext("yt:videoId", default="", namespaces=NS)
        link = e.find("a:link[@rel='alternate']", NS)
        out.append({
            "id": vid,
            "title": e.findtext("a:title", default="", namespaces=NS),
            "url": link.get("href") if link is not None else f"https://www.youtube.com/watch?v={vid}",
            "published": e.findtext("a:published", default="", namespaces=NS),
            "channel": e.findtext("a:author/a:name", default=channel_title, namespaces=NS),
            "description": e.findtext("media:group/media:description", default="", namespaces=NS),
            "tags": [],
            "live": "none",
        })
    return [v for v in out if v["id"]]


def description_hashtags(desc: str) -> list[str]:
    return [m.group(1) for m in HASHTAG_RE.finditer(desc or "")]


@register_source
class YouTubeSource(Source):
    name = "youtube"
    label = "YouTube"
    description = "Post when a watched channel uploads a new video."
    default_interval = 300
    backfill_default = False
    fields = [
        Field("channels", "Channels", "rows", default=[], columns=[
            Field("channel", "Channel (@handle, URL or UC… id)"),
            Field("channel_id", "Channel ID (auto)"),
            Field("hashtags", "Extra hashtags"),
            Field("enabled", "On", "bool", default=True),
        ], help="Add one row per channel. The channel ID is looked up automatically when you save."),
        Field("default_hashtags", "Default hashtags", "tags", default=[],
              help="Added to every video post, before the video's own tags."),
        Field("template", "Post text", "text", default="{title}",
              help="Placeholders: {title} {channel}. The video itself appears as a playable card."),
        Field("max_video_tags", "Max video tags to add", "int", default=5),
        Field("tag_source", "Video tags come from", "select", default="auto",
              options=["auto", "youtube_tags", "description_hashtags", "none"],
              help="auto = YouTube tags when an API key is set, else hashtags in the description."),
        Field("include_shorts", "Include Shorts", "bool", default=True),
        Field("api_key", "YouTube Data API key (optional)", "password", secret=True, default="",
              help="Enables tags, skips not-yet-premiered videos, and is more reliable than RSS."),
    ]

    def __init__(self, settings, context):
        super().__init__(settings, context)
        self.http = make_session()

    # ---------------------------------------------------------- resolution
    def resolve_channel_id(self, value: str) -> str | None:
        value = (value or "").strip()
        m = CHANNEL_ID_RE.search(value)
        if m:
            return m.group(1)
        handle = value
        hm = re.search(r"youtube\.com/(@[\w.\-]+)", value)
        if hm:
            handle = hm.group(1)
        if not handle.startswith("@"):
            handle = "@" + handle.split("/")[-1]
        key = self.settings.get("api_key")
        if key:
            r = self.http.get(f"{API}/channels", params={"part": "id", "forHandle": handle, "key": key},
                              timeout=TIMEOUT)
            r.raise_for_status()
            items = r.json().get("items") or []
            if items:
                return items[0]["id"]
        r = self.http.get(f"https://www.youtube.com/{handle}", timeout=TIMEOUT,
                          headers={"Accept-Language": "en-US"}, cookies={"CONSENT": "YES+1"})
        if r.ok:
            for pat in (r'"externalId":"(UC[\w-]{22})"',
                        r'<link rel="canonical" href="https://www\.youtube\.com/channel/(UC[\w-]{22})"',
                        r'"channelId":"(UC[\w-]{22})"'):
                m = re.search(pat, r.text)
                if m:
                    return m.group(1)
        return None

    def prepare(self, section: dict) -> bool:
        changed = False
        for row in section.get("channels", []):
            if row.get("channel") and not CHANNEL_ID_RE.fullmatch(row.get("channel_id") or ""):
                try:
                    cid = self.resolve_channel_id(row["channel"])
                except Exception as e:  # network trouble: try again next save/poll
                    log.warning("Could not resolve %s: %s", row["channel"], e)
                    cid = None
                if cid:
                    row["channel_id"] = cid
                    changed = True
        return changed

    def _channels(self) -> list[dict]:
        rows = []
        for row in self.settings.get("channels", []):
            if not row.get("enabled", True) or not row.get("channel"):
                continue
            cid = row.get("channel_id") or ""
            if not CHANNEL_ID_RE.fullmatch(cid):
                cid = self.resolve_channel_id(row["channel"]) or ""
                row["channel_id"] = cid
            if cid:
                rows.append(row)
            else:
                log.warning("YouTube channel %r: could not find its channel ID", row["channel"])
        return rows

    def targets(self) -> list[str]:
        return [r.get("channel_id") for r in self.settings.get("channels", [])
                if r.get("enabled", True) and r.get("channel_id")]

    # ------------------------------------------------------------- fetching
    def fetch_rss(self, cid: str) -> list[dict]:
        r = self.http.get("https://www.youtube.com/feeds/videos.xml", params={"channel_id": cid},
                          timeout=TIMEOUT)
        r.raise_for_status()
        return parse_rss(r.text)

    def fetch_api(self, cid: str) -> list[dict]:
        key = self.settings["api_key"]
        r = self.http.get(f"{API}/playlistItems", timeout=TIMEOUT, params={
            "part": "contentDetails", "playlistId": "UU" + cid[2:], "maxResults": 10, "key": key})
        r.raise_for_status()
        ids = [i["contentDetails"]["videoId"] for i in r.json().get("items", [])]
        return self.fetch_details(ids) if ids else []

    def fetch_details(self, ids: list[str]) -> list[dict]:
        r = self.http.get(f"{API}/videos", timeout=TIMEOUT, params={
            "part": "snippet", "id": ",".join(ids), "key": self.settings["api_key"]})
        r.raise_for_status()
        out = []
        for v in r.json().get("items", []):
            sn = v["snippet"]
            out.append({
                "id": v["id"], "title": sn.get("title", ""),
                "url": f"https://www.youtube.com/watch?v={v['id']}",
                "published": sn.get("publishedAt", ""), "channel": sn.get("channelTitle", ""),
                "description": sn.get("description", ""), "tags": sn.get("tags", []) or [],
                "live": sn.get("liveBroadcastContent", "none"),
            })
        return out

    def is_short(self, v: dict) -> bool:
        if "/shorts/" in v.get("url", ""):
            return True
        try:  # /shorts/<id> answers 200 for Shorts and redirects for normal videos
            r = self.http.head(f"https://www.youtube.com/shorts/{v['id']}", allow_redirects=False,
                               timeout=TIMEOUT)
            return r.status_code == 200
        except Exception:
            return False

    # --------------------------------------------------------------- items
    def make_item(self, v: dict, row: dict) -> Item:
        s = self.settings
        tag_source = s.get("tag_source", "auto")
        if tag_source == "auto":
            tag_source = "youtube_tags" if v.get("tags") else "description_hashtags"
        raw_tags = []
        if tag_source == "youtube_tags":
            raw_tags = v.get("tags", [])
        elif tag_source == "description_hashtags":
            raw_tags = description_hashtags(v.get("description", ""))
        video_tags = dedupe_tags(t for t in (to_hashtag(x) for x in raw_tags) if t)
        video_tags = video_tags[: max(0, int(s.get("max_video_tags", 5) or 0))]
        defaults = dedupe_tags(parse_tag_list(s.get("default_hashtags")) +
                               parse_tag_list(row.get("hashtags")))
        title = (s.get("template") or "{title}").format_map(
            {"title": v["title"], "channel": v.get("channel", "")}).strip() or v["title"]
        desc = re.sub(r"\s+", " ", v.get("description", "")).strip()
        url = f"https://www.youtube.com/watch?v={v['id']}"   # canonical; Shorts links play too
        return Item(
            source=self.name, key=v["id"], title=title, url=url,
            default_tags=defaults, extra_tags=video_tags, target=row["channel_id"],
            card=LinkCard(url=url, title=v["title"], description=truncate(desc, 200),
                          thumb_url=thumb_url(v["id"])),
            meta={"channel": v.get("channel", ""), "published": v.get("published", ""),
                  "shorts": "/shorts/" in v.get("url", "")},
        )

    def poll(self) -> list[Item]:
        items = []
        use_api = bool(self.settings.get("api_key"))
        for row in self._channels():
            cid = row["channel_id"]
            if use_api:
                videos = self.fetch_api(cid)
            else:
                videos = self.fetch_rss(cid)
                if self.settings.get("tag_source") == "youtube_tags":
                    log.info("YouTube tags need an API key; using description hashtags.")
            for v in videos:
                if v.get("live") == "upcoming":
                    continue           # scheduled premiere / stream: post once it's live
                if not self.settings.get("include_shorts", True) and self.is_short(v):
                    continue
                items.append(self.make_item(v, row))
        return items
