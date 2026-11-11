"""Offline tests for the experimental networks. No real accounts or network:
HTTP calls go to a fake session that records them and returns canned replies.
They check that each network builds the request its API documentation
describes; they can't prove the real service accepts it."""

from __future__ import annotations

import json
from datetime import datetime, timedelta, timezone

import pytest

from fastdissem.config import ConfigStore
from fastdissem.engine import Engine, build_publishers
from fastdissem.models import Item, LinkCard
from fastdissem.oauth1 import signature
from fastdissem.plugins import PUBLISHERS, Context, load_plugins
from fastdissem.publishers.threads import threads_measure, topic_tag
from fastdissem.publishers.twitter import x_measure
from fastdissem.storage import Storage

load_plugins()


class Resp:
    def __init__(self, data=None, status=200):
        self._data, self.status_code = data or {}, status
        self.ok = status < 400
        self.text = json.dumps(self._data)

    def json(self):
        return self._data


class FakeHTTP:
    """routes: list of (METHOD, url-substring, reply dict or callable)."""

    def __init__(self, routes):
        self.routes, self.calls = routes, []
        self.headers = {}

    def _go(self, method, url, **kw):
        self.calls.append((method, url, kw))
        for m, frag, reply in self.routes:
            if m == method and frag in url:
                return reply(kw) if callable(reply) else Resp(reply)
        raise AssertionError(f"unexpected {method} {url}")

    def get(self, url, **kw):
        return self._go("GET", url, **kw)

    def post(self, url, **kw):
        return self._go("POST", url, **kw)


def video():
    url = "https://www.youtube.com/watch?v=abc123DEF45"
    return Item(source="youtube", key="abc123DEF45", title="Tracking Tonight's Storms", url=url,
                default_tags=["INwx", "WeatherAlert"], extra_tags=["SevereWeather", "radar"],
                card=LinkCard(url=url, title="Tracking Tonight's Storms",
                              thumb_url="https://i.ytimg.com/vi/abc123DEF45/hqdefault.jpg"))


def alert():
    return Item(source="nws", key="urn:1", title="🔴 Tornado Warning",
                url="https://forecast.weather.gov/x", default_tags=["INwx"],
                card=LinkCard(url="https://forecast.weather.gov/x", title="Tornado Warning"))


def pub(name, ctx=None, **settings):
    s = PUBLISHERS[name].defaults()
    s.update(enabled=True, **settings)
    return PUBLISHERS[name](s, ctx or Context())


# ----------------------------------------------------------------------- X
def test_oauth1_matches_x_documentation_example():
    # The worked example from X's "Creating a signature" documentation.
    params = {
        "status": "Hello Ladies + Gentlemen, a signed OAuth request!",
        "oauth_consumer_key": "xvz1evFS4wEEPTGEFPHBog",
        "oauth_nonce": "kYjzVBB8Y0ZFabxSWbWovY3uYSQ2pTgmZeNu2VS4cg",
        "oauth_signature_method": "HMAC-SHA1",
        "oauth_timestamp": "1318622958",
        "oauth_token": "370773112-GmHxMAgYyLbNEtIKZeRNFsMKPR9EyMZeS9weJAEb",
        "oauth_version": "1.0",
    }
    sig = signature("POST", "https://api.twitter.com/1.1/statuses/update.json?include_entities=true",
                    params, "kAcSOqF21Fu85e7zjz7ZN2U4ZRhfV3WpwPAoE3Z7kBw",
                    "LswwdoUaIvS8ltyTt5jkRh4J50vUPVVHtR2YPi5kE")
    assert sig == "hCtSmYh+iHYCEqBWrE7C7hYmtUk="


def test_x_weighted_length():
    assert x_measure("hello") == 5
    assert x_measure("see https://example.com/" + "a" * 80) == 4 + 23
    assert x_measure("🔴 Tornado Warning") == 2 + 16
    assert x_measure("日本") == 4


def test_x_post_and_optional_link():
    p = pub("twitter", api_key="k", api_secret="s", access_token="t", access_secret="ts")
    assert "youtube.com" in p.render(video()).text
    p2 = pub("twitter", include_link=False)
    r = p2.render(video())
    assert "youtube.com" not in r.text and r.card is None
    p.http = FakeHTTP([("POST", "api.x.com/2/tweets", {"data": {"id": "123"}})])
    res = p.publish(video(), p.render(video()))
    method, url, kw = p.http.calls[0]
    assert kw["json"]["text"].startswith("Tracking Tonight's Storms")
    assert kw["headers"]["Authorization"].startswith('OAuth oauth_consumer_key="k"')
    assert res.post_url == "https://x.com/i/web/status/123"


def test_x_long_post_fits_280_weighted():
    it = Item(source="t", key="1", title="🔴 " + "Severe Thunderstorm Warning " * 6,
              default_tags=["INwx"], url="https://forecast.weather.gov/x")
    r = pub("twitter").render(it)
    assert x_measure(r.text) <= 280 and r.text.endswith("#INwx")


def test_x_missing_keys_message():
    with pytest.raises(RuntimeError, match="not all set"):
        pub("twitter").publish(video(), pub("twitter").render(video()))


# ---------------------------------------------------------------- Facebook
def test_facebook_posts_message_and_link():
    p = pub("facebook", page_id="999", page_token="PT")
    r = p.render(video())
    assert "youtube.com" not in r.text and "#INwx" in r.text
    p.http = FakeHTTP([("POST", "/999/feed", {"id": "999_555"})])
    res = p.publish(video(), r)
    _, url, kw = p.http.calls[0]
    assert url == "https://graph.facebook.com/v25.0/999/feed"
    assert kw["data"]["link"] == "https://www.youtube.com/watch?v=abc123DEF45"
    assert kw["data"]["access_token"] == "PT"
    assert res.post_url == "https://www.facebook.com/999_555"


# ----------------------------------------------------------------- Threads
def test_threads_one_topic_tag_and_emoji_bytes():
    assert threads_measure("🔴 a") == 4 + 2
    assert threads_measure("— …") == 3
    assert topic_tag(["St.Louis&Co", "x"]) == "StLouisCo"
    r = pub("threads").render(video())
    assert "#" not in r.text and r.tags == ["INwx"] and r.note == "Topic tag: #INwx"
    assert "youtube.com" not in r.text      # link goes in link_attachment


def test_threads_publish_flow(tmp_path):
    p = pub("threads", access_token="TOK")
    p.http = FakeHTTP([
        ("GET", "/v1.0/me", {"id": "42", "username": "cali"}),
        ("POST", "/42/threads_publish", {"id": "post9"}),
        ("POST", "/42/threads", {"id": "c1"}),
        ("GET", "/c1", {"status": "FINISHED"}),
        ("GET", "/post9", {"permalink": "https://www.threads.com/@cali/post/X"}),
    ])
    res = p.publish(video(), p.render(video()))
    create = next(kw for m, u, kw in p.http.calls if u.endswith("/42/threads"))["data"]
    assert create["media_type"] == "TEXT" and create["topic_tag"] == "INwx"
    assert create["link_attachment"] == "https://www.youtube.com/watch?v=abc123DEF45"
    pubcall = next(kw for m, u, kw in p.http.calls if u.endswith("threads_publish"))["data"]
    assert pubcall["creation_id"] == "c1"
    assert res.post_url.endswith("/post/X")


# --------------------------------------------------------------- Instagram
def test_instagram_skips_alerts_and_posts_thumbnail():
    p = pub("instagram", access_token="TOK")
    assert "needs a picture" in p.accepts(alert())
    assert p.accepts(video()) is None
    r = p.render(video())
    assert "#INwx" in r.text and "youtube.com/watch" in r.text
    p.http = FakeHTTP([
        ("GET", "/v25.0/me", {"id": "1", "user_id": "178", "username": "cali"}),
        ("POST", "/178/media_publish", {"id": "m77"}),
        ("POST", "/178/media", {"id": "c5"}),
        ("GET", "/c5", {"status_code": "FINISHED"}),
        ("GET", "/m77", {"permalink": "https://www.instagram.com/p/abc/"}),
    ])
    res = p.publish(video(), r)
    create = next(kw for m, u, kw in p.http.calls if u.endswith("/178/media"))["data"]
    assert create["image_url"].endswith("hqdefault.jpg") and create["caption"] == r.text
    assert res.post_url == "https://www.instagram.com/p/abc/"


def test_instagram_caps_hashtags_at_30():
    it = video()
    it.extra_tags = [f"t{i}" for i in range(40)]
    assert pub("instagram").render(it).text.count("#") <= 30


# ------------------------------------------------------ token auto-renewal
def test_token_renewal_saved_to_config(tmp_path):
    cfg = ConfigStore(tmp_path / "config.json")
    raw = cfg.raw()
    raw["publishers"]["threads"].update(enabled=True, access_token="OLD")
    cfg.save(raw)
    st = Storage(tmp_path / "db.sqlite")
    p = build_publishers(cfg.get(), st, config=cfg)["threads"]
    calls = []
    p.http = FakeHTTP([("GET", "refresh_access_token",
                        lambda kw: calls.append(kw) or Resp({"access_token": "NEW", "expires_in": 5184000}))])
    p.maintain()                                  # just pasted: no renewal yet
    assert calls == []
    saved = json.loads((tmp_path / "config.json").read_text())["publishers"]["threads"]
    assert saved["token_fingerprint"] and saved["access_token"] == "OLD"
    old = (datetime.now(timezone.utc) - timedelta(days=21)).isoformat(timespec="seconds")
    p.settings["token_renewed_at"] = old
    p.maintain()                                  # 21 days later: renewed and saved
    assert calls[0]["params"]["grant_type"] == "th_refresh_token"
    saved = json.loads((tmp_path / "config.json").read_text())["publishers"]["threads"]
    assert saved["access_token"] == "NEW"


def test_settings_page_shows_beta_and_keeps_renewal_fields(tmp_path):
    from fastdissem.web import create_app
    cfg = ConfigStore(tmp_path / "config.json")
    raw = cfg.raw()
    raw["publishers"]["threads"].update(access_token="T", token_renewed_at="2026-11-01T00:00:00+00:00")
    cfg.save(raw)
    st = Storage(tmp_path / "db.sqlite")
    app = create_app(cfg, st, Engine(cfg, st))
    c = app.test_client()
    page = c.get("/settings").data.decode()
    assert page.count("beta · untested") == 5     # X, Facebook, Threads, Instagram, Mastodon
    assert page.index("Bluesky") < page.index("Threads")
    with c.session_transaction() as s:
        tok = s["csrf"]
    c.post("/settings", data={"_csrf": tok, "general.dry_run": "1", "web.port": "8787"})
    saved = json.loads((tmp_path / "config.json").read_text())["publishers"]["threads"]
    assert saved["access_token"] == "T" and saved["token_renewed_at"].startswith("2026-11-01")


# ----------------------------------------------------- engine skip handling
def test_engine_marks_skipped(tmp_path):
    cfg = ConfigStore(tmp_path / "config.json")
    raw = cfg.raw()
    raw["general"]["dry_run"] = False
    raw["publishers"]["instagram"].update(enabled=True, access_token="T")
    cfg.save(raw)
    st = Storage(tmp_path / "db.sqlite")
    e = Engine(cfg, st)
    e._rebuild_if_needed()
    e.publishers = {"instagram": e.publishers["instagram"]}
    a = alert()
    st.add_item(a, "queued")
    st.queue(a.uid, "instagram")
    e.deliver_due()
    row = st.recent()[0]
    assert row["status"] == "skipped" and "picture" in row["last_error"]
    assert st.due() == []                          # not retried
