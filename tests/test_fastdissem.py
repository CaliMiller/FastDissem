"""Offline tests (no network): run with  python -m pytest -q"""

from __future__ import annotations

import json
from datetime import datetime, timedelta, timezone

import pytest

from fastdissem.config import ConfigStore
from fastdissem.engine import Engine
from fastdissem.models import Item, LinkCard, PublishResult
from fastdissem.plugins import PUBLISHERS, SOURCES, Context, load_plugins
from fastdissem.publishers.bluesky import build_facets
from fastdissem.sources.nws import NWSSource, format_areas
from fastdissem.sources.youtube import YouTubeSource, parse_rss
from fastdissem.storage import Storage
from fastdissem.text import compose, grapheme_len, mastodon_measure, parse_tag_list, to_hashtag

load_plugins()
NOW = datetime(2026, 11, 9, 21, 0, tzinfo=timezone.utc)   # 4 PM EST

RSS = """<?xml version="1.0" encoding="UTF-8"?>
<feed xmlns:yt="http://www.youtube.com/xml/schemas/2015" xmlns:media="http://search.yahoo.com/mrss/" xmlns="http://www.w3.org/2005/Atom">
 <title>Indy Weather Guy</title>
 <entry>
  <id>yt:video:abc123DEF45</id><yt:videoId>abc123DEF45</yt:videoId>
  <yt:channelId>UCaaaaaaaaaaaaaaaaaaaaaa</yt:channelId>
  <title>Tracking Tonight's Storms &amp; Tornado Risk</title>
  <link rel="alternate" href="https://www.youtube.com/watch?v=abc123DEF45"/>
  <author><name>Indy Weather Guy</name></author>
  <published>2026-11-09T20:00:00+00:00</published>
  <media:group><media:description>Live look at radar. #INwx #severeweather #2026</media:description></media:group>
 </entry>
</feed>"""


def tor_warning(**over):
    p = {
        "id": "https://api.weather.gov/alerts/urn:oid:2.49.0.1.840.0.abc",
        "status": "Actual", "messageType": "Alert", "event": "Tornado Warning",
        "severity": "Extreme", "areaDesc": "Boone, IN; Hamilton, IN; Marion, IN",
        "geocode": {"UGC": ["INC011", "INC057", "INC097"]},
        "sent": "2026-11-09T15:50:00-05:00", "effective": "2026-11-09T15:50:00-05:00",
        "onset": "2026-11-09T15:50:00-05:00", "expires": "2026-11-09T16:45:00-05:00",
        "ends": "2026-11-09T16:45:00-05:00", "senderName": "NWS Indianapolis IN",
        "headline": "Tornado Warning issued November 9 at 3:50PM EST until November 9 at 4:45PM EST by NWS Indianapolis IN",
        "description": "At 350 PM EST, a severe thunderstorm capable of producing a tornado was located...",
        "parameters": {"tornadoDetection": ["RADAR INDICATED"], "maxHailSize": ["1.00"],
                       "maxWindGust": ["60 MPH"]},
    }
    p.update(over)
    return p


def winter_adv():
    return {
        "id": "urn:winter1", "status": "Actual", "messageType": "Alert",
        "event": "Winter Weather Advisory", "severity": "Moderate",
        "areaDesc": "Northern Marion; Southeastern Hamilton; Central Boone; Johnson; Morgan; Shelby; Hancock; Hendricks",
        "geocode": {"UGC": ["INZ040", "INZ041", "INZ039", "INZ054", "INZ053", "INZ055", "INZ048", "INZ047"]},
        "sent": "2026-11-09T04:00:00-05:00", "onset": "2026-11-10T19:00:00-05:00",
        "ends": "2026-11-11T07:00:00-05:00", "expires": "2026-11-10T04:00:00-05:00",
        "senderName": "NWS Indianapolis IN",
        "description": "* WHAT...Snow expected. Total snow accumulations of 2 to 4 inches.\n\n"
                       "* WHERE...Portions of central Indiana.\n\n* WHEN...From 7 PM Tuesday to 7 AM Wednesday.",
        "parameters": {"NWSheadline": ["WINTER WEATHER ADVISORY IN EFFECT FROM 7 PM TUESDAY"]},
    }


def nws(settings=None):
    s = NWSSource.defaults()
    s.update({"enabled": True, "areas": [{"code": "INC097", "label": "Marion", "enabled": True},
                                         {"code": "INZ040", "label": "", "enabled": True}],
              "default_hashtags": ["INwx", "WeatherAlert"]})
    s.update(settings or {})
    return NWSSource(s, Context(timezone="America/Indiana/Indianapolis"))


# ------------------------------------------------------------------- text
def test_hashtags():
    assert to_hashtag("severe weather") == "SevereWeather"
    assert to_hashtag("#INwx") == "INwx"
    assert to_hashtag("2026") is None
    assert to_hashtag("rock & roll") == "RockRoll"
    assert parse_tag_list("INwx, weather, inwx") == ["INwx", "weather"]
    assert parse_tag_list("#a #b") == ["a", "b"]


def test_graphemes():
    assert grapheme_len("🇺🇸") == 1
    assert grapheme_len("👨‍👩‍👧") == 1
    assert grapheme_len("👍🏽") == 1
    assert grapheme_len("é") == 1          # e + combining acute
    assert grapheme_len("abc") == 3


def test_compose_trims_extra_tags_first():
    it = Item(source="t", key="1", title="x" * 280, default_tags=["Keep"],
              extra_tags=["drop1", "drop2", "drop3"])
    r = compose(it, 300)
    assert r.length <= 300 and r.text.endswith("#Keep #drop1") and r.trimmed


def test_compose_title_truncation():
    it = Item(source="t", key="1", title="word " * 100, default_tags=["A"])
    r = compose(it, 300)
    assert r.length <= 300 and r.text.endswith("#A")   # title shortened, default tag kept
    assert "…" in r.text


def test_mastodon_url_weight():
    assert mastodon_measure("hi https://example.com/" + "x" * 100) == 3 + 23


# --------------------------------------------------------------- youtube
def test_youtube_item():
    v = parse_rss(RSS)[0]
    s = YouTubeSource.defaults()
    s.update({"default_hashtags": ["YouTube"], "max_video_tags": 5})
    src = YouTubeSource(s, Context())
    item = src.make_item(v, {"channel_id": "UCaaaaaaaaaaaaaaaaaaaaaa", "hashtags": "IndyWx"})
    assert item.uid == "youtube:abc123DEF45"
    assert item.title == "Tracking Tonight's Storms & Tornado Risk"
    assert item.default_tags == ["YouTube", "IndyWx"]
    assert item.extra_tags == ["INwx", "severeweather"]       # "2026" rejected
    assert item.card.thumb_url.endswith("/abc123DEF45/hqdefault.jpg")
    r = PUBLISHERS["bluesky"](PUBLISHERS["bluesky"].defaults(), Context()).render(item)
    assert "youtube.com" not in r.text       # link lives in the card
    assert r.text.endswith("#YouTube #IndyWx #INwx #severeweather")


def test_youtube_api_tags():
    s = YouTubeSource.defaults()
    src = YouTubeSource(s, Context())
    v = {"id": "x" * 11, "title": "T", "url": "", "description": "", "channel": "C",
         "tags": ["severe weather", "Indiana", "radar", "tornado", "storm chasing", "extra"]}
    item = src.make_item(v, {"channel_id": "UC" + "a" * 22})
    assert item.extra_tags == ["SevereWeather", "Indiana", "radar", "tornado", "StormChasing"]


# ------------------------------------------------------------------- nws
def test_nws_tornado_warning():
    item = nws().make_item(tor_warning(), NOW)
    r = PUBLISHERS["bluesky"](PUBLISHERS["bluesky"].defaults(), Context()).render(item)
    print("\n" + r.text)
    assert r.text.startswith("🔴 Tornado Warning")
    assert "Boone, Hamilton, Marion counties, IN" in r.text
    assert "Until 4:45 PM EST" in r.text
    assert "Tornado: radar indicated" in r.text
    assert "quarter-size (1\") hail" in r.text
    assert "#INwx" in r.text and r.length <= 300
    assert "warncounty=INC097" in item.card.url and "warnzone=INZ040" in item.card.url


def test_nws_future_onset_and_what():
    item = nws().make_item(winter_adv(), NOW)
    text = compose(item, 300).text
    print("\n" + text)
    assert "From 7 PM EST Tue 11/10 until 7 AM EST Wed 11/11" in text
    assert "Total snow accumulations of 2 to 4 inches" in text
    assert "+2 more" in text   # 8 zones, max 6 listed


def test_nws_abbreviates_when_needed():
    item = nws().make_item(winter_adv(), NOW)
    r = compose(item, 190)
    print("\n" + r.text)
    assert r.length <= 190
    assert "accum." in r.text or "N. Marion" in r.text


def test_nws_filters():
    src = nws({"min_severity": "Severe"})
    assert src.wanted(tor_warning(), NOW)
    assert not src.wanted(winter_adv(), NOW)
    assert not nws().wanted(tor_warning(messageType="Update"), NOW)
    assert nws({"post_updates": True}).wanted(tor_warning(messageType="Update"), NOW)
    assert not nws().wanted(tor_warning(status="Test"), NOW)
    assert not nws({"exclude_events": "tornado warning"}).wanted(tor_warning(), NOW)
    expired = tor_warning(ends="2026-11-09T15:00:00-05:00", expires="2026-11-09T15:00:00-05:00")
    assert not nws().wanted(expired, NOW)


def test_format_areas_multi_state():
    s = format_areas("Vigo, IN; Clark, IL; Edgar, IL", True, 6, True)
    assert s == "Vigo Co., IN; Clark, Edgar Cos., IL"


# --------------------------------------------------------------- bluesky
def test_facets_bytes():
    text = "⚠️ Héllo\n\n#INwx #Snow https://x.com/a"
    f = build_facets(text)
    b = text.encode()
    got = sorted(b[x["index"]["byteStart"]:x["index"]["byteEnd"]].decode() for x in f)
    assert got == ["#INwx", "#Snow", "https://x.com/a"]


# ---------------------------------------------------------------- engine
class FakeSource:
    label = "Fake"
    default_interval = 60

    def __init__(self, items, targets):
        self.items, self._targets = items, targets

    def poll(self):
        return list(self.items)

    def targets(self):
        return self._targets


class FakePub:
    label = "Fake net"

    def __init__(self):
        self.sent = []
        self.fail = 0

    def render(self, item):
        return compose(item, 300)

    def publish(self, item, rendered):
        if self.fail:
            self.fail -= 1
            raise RuntimeError("boom")
        self.sent.append(item.uid)
        return PublishResult(post_url="https://example/p/" + item.key)


@pytest.fixture
def eng(tmp_path):
    cfg = ConfigStore(tmp_path / "config.json")
    c = cfg.raw()
    c["general"]["dry_run"] = False
    cfg.save(c)
    st = Storage(tmp_path / "db.sqlite")
    e = Engine(cfg, st)
    e._rebuild_if_needed()
    e.publishers = {"fake": FakePub()}
    return e


def it(key, target="CH1"):
    return Item(source="fake", key=key, title=f"Video {key}", target=target)


def test_seed_then_post_new_and_no_duplicates(eng):
    eng.cfg["sources"]["fake"] = {"backfill": False}
    src = FakeSource([it("old1"), it("old2")], ["CH1"])
    eng.ingest("fake", src, src.poll()); eng.deliver_due()
    assert eng.publishers["fake"].sent == []                 # existing videos not posted
    src.items.append(it("new1"))
    for _ in range(3):                                        # repeated polls
        eng.ingest("fake", src, src.poll()); eng.deliver_due()
    assert eng.publishers["fake"].sent == ["fake:new1"]       # posted exactly once
    # a second channel added later: its back catalog is seeded, not posted
    src._targets = ["CH1", "CH2"]
    src.items += [it("ch2old", "CH2")]
    eng.ingest("fake", src, src.poll()); eng.deliver_due()
    assert eng.publishers["fake"].sent == ["fake:new1"]


def test_backfill_posts_existing(eng):
    eng.cfg["sources"]["fake"] = {"backfill": True}
    src = FakeSource([it("a1", "INC097")], ["INC097"])
    eng.ingest("fake", src, src.poll()); eng.deliver_due()
    assert eng.publishers["fake"].sent == ["fake:a1"]


def test_retry_then_success(eng):
    eng.cfg["sources"]["fake"] = {"backfill": True}
    pub = eng.publishers["fake"]
    pub.fail = 1
    src = FakeSource([it("r1")], ["CH1"])
    eng.ingest("fake", src, src.poll()); eng.deliver_due()
    assert pub.sent == []
    eng.storage._q("UPDATE deliveries SET next_try='2000-01-01T00:00:00+00:00'")
    eng.deliver_due()
    assert pub.sent == ["fake:r1"]


def test_expired_alert_not_sent(eng):
    eng.cfg["sources"]["fake"] = {"backfill": True}
    i = it("e1"); i.expires_at = "2000-01-01T00:00:00+00:00"
    eng.ingest("fake", FakeSource([i], ["CH1"]), [i]); eng.deliver_due()
    assert eng.publishers["fake"].sent == []


def test_dry_run_records_but_does_not_send(eng):
    eng.dry_run_override = True
    eng.cfg["sources"]["fake"] = {"backfill": True}
    eng.ingest("fake", FakeSource([it("d1")], ["CH1"]), [it("d1")]); eng.deliver_due()
    assert eng.publishers["fake"].sent == []
    assert eng.storage.recent()[0]["status"] == "dry_run"


# ------------------------------------------------------------------- web
def test_web_settings_roundtrip(tmp_path):
    from fastdissem.web import create_app
    cfg = ConfigStore(tmp_path / "config.json")
    st = Storage(tmp_path / "db.sqlite")
    app = create_app(cfg, st, Engine(cfg, st))
    app.testing = True
    c = app.test_client()
    assert c.get("/").status_code == 200
    page = c.get("/settings")
    assert page.status_code == 200 and b"color-scheme: dark" in page.data
    with c.session_transaction() as s:
        tok = s["csrf"]
    form = {
        "_csrf": tok,
        "general.dry_run": "1", "general.timezone": "America/Indiana/Indianapolis",
        "general.max_attempts": "5", "general.log_level": "INFO",
        "sources.nws.enabled": "1", "sources.nws.interval_seconds": "120",
        "sources.nws.areas.0.code": "INC097", "sources.nws.areas.0.label": "Marion",
        "sources.nws.areas.0.enabled": "1",
        "sources.nws.default_hashtags": "INwx, weather alert",
        "sources.youtube.channels.0.channel": "UCbbbbbbbbbbbbbbbbbbbbbb",
        "sources.youtube.channels.0.enabled": "1",
        "publishers.bluesky.enabled": "1", "publishers.bluesky.handle": "me.bsky.social",
        "publishers.bluesky.app_password": "abcd-efgh-ijkl-mnop",
        "web.host": "127.0.0.1", "web.port": "8787",
    }
    assert c.post("/settings", data=form).status_code == 302
    saved = json.loads((tmp_path / "config.json").read_text())
    assert saved["sources"]["nws"]["areas"] == [{"code": "INC097", "label": "Marion", "enabled": True}]
    assert saved["sources"]["nws"]["default_hashtags"] == ["INwx", "WeatherAlert"]
    assert saved["sources"]["youtube"]["channels"][0]["channel_id"] == "UCbbbbbbbbbbbbbbbbbbbbbb"
    assert saved["publishers"]["bluesky"]["app_password"] == "abcd-efgh-ijkl-mnop"
    # blank password field keeps the saved secret
    form["publishers.bluesky.app_password"] = ""
    c.post("/settings", data=form)
    saved = json.loads((tmp_path / "config.json").read_text())
    assert saved["publishers"]["bluesky"]["app_password"] == "abcd-efgh-ijkl-mnop"
    # CSRF enforced
    assert c.post("/settings", data={**form, "_csrf": "bad"}).status_code == 400
