"""Shelby County scenarios: your county listed first, the link's zone filled
in, and posting alerts that were composed during dry run."""

from __future__ import annotations

from datetime import datetime, timedelta, timezone

from fastdissem.config import ConfigStore
from fastdissem.engine import Engine
from fastdissem.plugins import Context
from fastdissem.sources.nws import NWSSource, _centroid, format_areas
from fastdissem.storage import Storage
from fastdissem.models import Item

NOW = datetime(2026, 11, 12, 0, 30, tzinfo=timezone.utc)
SHELBY = {"name": "Shelby", "state": "IN", "zone": "INZ055", "county": "INC145",
          "lat": 39.5236, "lon": -85.7917}

ZONES = ["Boone", "Hamilton", "Madison", "Delaware", "Hendricks", "Marion", "Hancock",
         "Henry", "Putnam", "Morgan", "Johnson", "Shelby", "Rush", "Decatur", "Bartholomew"]


def shelby_src(**over):
    s = NWSSource.defaults()
    s.update(enabled=True, areas=[{"code": "INC145", "label": "", "enabled": True}],
             zone_info={"INC145": dict(SHELBY)}, default_hashtags=["INwx"])
    s.update(over)
    return NWSSource(s, Context(timezone="America/Indiana/Indianapolis"))


def watch():   # zone-based: UGC are INZ codes, areaDesc has no state
    return {
        "id": "urn:watch1", "status": "Actual", "messageType": "Alert",
        "event": "Severe Thunderstorm Watch", "severity": "Severe",
        "areaDesc": "; ".join(ZONES),
        # same order as areaDesc; Shelby (index 11) is INZ055
        "geocode": {"UGC": [f"INZ{n:03d}" for n in range(30, 41)] + ["INZ055", "INZ056", "INZ057", "INZ058"]},
        "ends": "2026-11-12T03:00:00-05:00", "expires": "2026-11-12T03:00:00-05:00",
        "sent": "2026-11-11T19:00:00-05:00", "senderName": "NWS Indianapolis IN",
        "description": "* WHAT...Severe thunderstorms possible.",
    }


def warning():  # storm-based: UGC are county codes, areaDesc has ", IN"
    return {
        "id": "urn:warn1", "status": "Actual", "messageType": "Alert",
        "event": "Severe Thunderstorm Warning", "severity": "Severe",
        "areaDesc": "Johnson, IN; Bartholomew, IN; Shelby, IN",
        "geocode": {"UGC": ["INC081", "INC005", "INC145"]},
        "ends": "2026-11-11T20:15:00-05:00", "expires": "2026-11-11T20:15:00-05:00",
        "sent": "2026-11-11T19:30:00-05:00", "senderName": "NWS Indianapolis IN",
        "parameters": {"maxWindGust": ["60 MPH"], "maxHailSize": ["1.00"]},
    }


def text(src, props):
    from fastdissem.text import compose
    return compose(src.make_item(props, NOW), 300).text


# ------------------------------------------------------ your county first
def test_shelby_listed_first_in_long_watch():
    w = watch()
    t = text(shelby_src(), w)
    print("\n" + t)
    assert "📍 Shelby, Boone, Hamilton, Madison, Delaware, Hendricks +9 more" in t


def test_shelby_found_by_name_even_without_matching_codes():
    w = watch()
    w["geocode"]["UGC"] = ["INZ999"]          # counts differ: name matching does the work
    assert "📍 Shelby, Boone" in text(shelby_src(), w)


def test_shelby_found_by_code_position():
    w = warning()
    t = text(shelby_src(zone_info={"INC145": {"name": "", "state": "IN", "zone": "INZ055"}}), w)
    assert "📍 Shelby, Johnson, Bartholomew counties, IN" in t


def test_only_mine_and_nws_order():
    assert "📍 Shelby +14 more" in text(shelby_src(area_focus="only_mine"), watch())
    assert "📍 Boone, Hamilton" in text(shelby_src(area_focus="nws_order"), watch())


def test_label_used_when_lookup_failed():
    src = shelby_src(areas=[{"code": "INC145", "label": "Shelby County", "enabled": True}],
                     zone_info={"INC145": {"error": "offline", "at": 9e18}})
    assert "📍 Shelby, Boone" in text(src, watch())


def test_format_areas_keeps_all_of_mine_beyond_max():
    s = format_areas("A, IN; B, IN; C, IN; D, IN", True, 1, True, priority={2, 3})
    assert s == "C, D Cos., IN +2 more"


# ------------------------------------------------------------- link zone
def test_link_fills_zone_for_county_only_setup():
    item = shelby_src().make_item(warning(), NOW)
    assert item.card.url == ("https://forecast.weather.gov/showsigwx.php"
                             "?warnzone=INZ055&warncounty=INC145")


def test_link_prefers_zone_named_in_alert():
    w = watch()
    w["geocode"]["UGC"] = ["INZ040", "INZ055"]
    w["areaDesc"] = "Marion; Shelby"
    assert "warnzone=INZ055" in shelby_src().make_item(w, NOW).card.url


def test_link_never_has_blank_zone():
    src = shelby_src(zone_info={"INC145": {"error": "offline", "at": 9e18}})
    url = src.make_item(warning(), NOW).card.url
    assert "warnzone=&" not in url and url == "https://alerts.weather.gov/"
    src = shelby_src(zone_info={"INC145": {"name": "Shelby", "state": "IN", "zone": "", "county": "INC145",
                                           "lat": 39.5, "lon": -85.8}})
    assert src.make_item(warning(), NOW).card.url == \
        "https://forecast.weather.gov/MapClick.php?lat=39.5&lon=-85.8"


def test_lookup_resolves_county_to_forecast_zone():
    class R:
        def __init__(self, d): self.d, self.ok = d, True
        def json(self): return self.d
        def raise_for_status(self): pass

    calls = []

    def get(url, **kw):
        calls.append(url)
        if "/zones/county/INC145" in url:
            return R({"properties": {"name": "Shelby", "state": "IN"},
                      "geometry": {"type": "Polygon", "coordinates": [[[-86, 39.4], [-85.6, 39.4],
                                                                        [-85.6, 39.7], [-86, 39.7], [-86, 39.4]]]}})
        if "/points/" in url:
            return R({"properties": {"forecastZone": "https://api.weather.gov/zones/forecast/INZ055",
                                     "county": "https://api.weather.gov/zones/county/INC145"}})
        raise AssertionError(url)

    src = shelby_src(zone_info={})
    src.lookup_http.get = get
    section = src.settings
    assert src.prepare(section) is True
    info = section["zone_info"]["INC145"]
    assert info["name"] == "Shelby" and info["zone"] == "INZ055" and info["lat"] == 39.55
    assert calls[1].endswith("/points/39.55,-85.8")


def test_centroid_multipolygon_uses_largest_part():
    g = {"type": "MultiPolygon", "coordinates": [
        [[[0, 0], [0.1, 0], [0.1, 0.1], [0, 0]]],
        [[[10, 10], [12, 10], [12, 12], [10, 12], [10, 10]]]]}
    assert _centroid(g) == (11.0, 11.0)


# ------------------------------------------------ post dry-run alerts now
def test_post_dry_run_alerts_now(tmp_path):
    from fastdissem.web import create_app
    cfg = ConfigStore(tmp_path / "config.json")
    st = Storage(tmp_path / "db.sqlite")
    eng = Engine(cfg, st)
    app = create_app(cfg, st, eng)
    c = app.test_client()
    future = (datetime.now(timezone.utc) + timedelta(hours=1)).isoformat()
    past = (datetime.now(timezone.utc) - timedelta(hours=1)).isoformat()
    for key, exp in (("active", future), ("over", past)):
        it = Item(source="nws", key=key, title=key, expires_at=exp)
        st.add_item(it, "queued"); st.queue(it.uid, "bluesky")
        st.record(it.uid, "bluesky", "dry_run", text="x")
    assert [r["uid"] for r in st.active_dry_runs()] == ["nws:active"]
    c.get("/")
    with c.session_transaction() as s:
        tok = s["csrf"]
    # still in dry run: refuses
    c.post("/post-active", data={"_csrf": tok})
    assert st.due() == []
    # live: dashboard offers it, and the button queues only the active alert
    raw = cfg.raw(); raw["general"]["dry_run"] = False; cfg.save(raw)
    eng._rebuild_if_needed()
    page = c.get("/").data.decode()
    assert "Post them now" in page and "post now" in page
    c.post("/post-active", data={"_csrf": tok})
    assert [d["uid"] for d in st.due()] == ["nws:active"]
    # single "post now" on the expired one is queued but the engine won't send it
    c.post("/post-now", data={"_csrf": tok, "uid": "nws:over", "publisher": "bluesky"})
    sent = []
    eng.publishers = {"bluesky": type("P", (), {
        "label": "B", "accepts": lambda s, i: None, "maintain_interval": 1e9, "maintain": lambda s: None,
        "render": lambda s, i: __import__("fastdissem.text", fromlist=["compose"]).compose(i, 300),
        "publish": lambda s, i, r: sent.append(i.uid) or __import__("fastdissem.models", fromlist=["x"]).PublishResult()})()}
    eng.deliver_due()
    assert sent == ["nws:active"]
    statuses = {r["uid"]: r["status"] for r in st.recent()}
    assert statuses == {"nws:active": "sent", "nws:over": "expired"}


def test_same_name_in_another_state_not_mistaken_for_yours():
    w = watch()
    w["areaDesc"] = "Clark; Shelby; Boone"
    w["geocode"]["UGC"] = ["ILZ061", "ILZ062", "INZ030"]   # Shelby here is Illinois
    assert "📍 Clark, Shelby, Boone" in text(shelby_src(), w)
