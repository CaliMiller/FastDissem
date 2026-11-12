"""National Weather Service active alerts for chosen counties / zones.

Uses https://api.weather.gov/alerts/active?zone=... which accepts both
county codes (INC097) and forecast-zone codes (INZ040). Each alert's NWS id
is the duplicate key, so an alert covering several watched areas posts once.
"""

from __future__ import annotations

import logging
import re
import time
from datetime import datetime, timezone
from zoneinfo import ZoneInfo

from ..http import TIMEOUT, make_session
from ..models import Item, Line, LinkCard
from ..plugins import Field, Source, register_source
from ..text import abbreviate, parse_abbreviations, parse_tag_list, truncate
from .. import APP_NAME, __version__

log = logging.getLogger(__name__)

BASE = "https://api.weather.gov"
API = f"{BASE}/alerts/active"
CODE_RE = re.compile(r"^[A-Z]{2}[CZ]\d{3}$")

DEFAULT_ABBREVIATIONS = """\
# Long = Short   (used only when a post would otherwise be too long,
#                 unless "Abbreviate" is set to always)
Severe Thunderstorm = Svr T-Storm
Severe Thunderstorms = Svr T-Storms
Thunderstorms = T-Storms
Thunderstorm = T-Storm
Special Weather Statement = Special Wx Statement
Hazardous Weather Outlook = Hazardous Wx Outlook
miles per hour = mph
inches = in.
inch = in.
accumulations = accum.
accumulation = accum.
temperatures = temps
temperature = temp
including = incl.
Northern = N.
Southern = S.
Eastern = E.
Western = W.
Northeastern = NE
Northwestern = NW
Southeastern = SE
Southwestern = SW
Central = Cntrl.
County = Co.
Counties = Cos.
Mountains = Mtns
"""

SEVERITY_ORDER = ["Unknown", "Minor", "Moderate", "Severe", "Extreme"]

HAIL_NAMES = {0.25: "pea", 0.5: "marble", 0.75: "penny", 0.88: "nickel", 1.0: "quarter",
              1.25: "half-dollar", 1.5: "ping-pong ball", 1.75: "golf ball", 2.0: "hen egg",
              2.5: "tennis ball", 2.75: "baseball", 3.0: "teacup", 4.0: "softball",
              4.5: "grapefruit"}


def alert_kind(event: str) -> tuple[str, str]:
    """(marker, kind) for an event name."""
    e = event.lower()
    if "emergency" in e:
        return "🚨", "emergency"
    if e.endswith("warning"):
        return "🔴", "warning"
    if e.endswith("watch"):
        return "🟠", "watch"
    if e.endswith("advisory"):
        return "🟡", "advisory"
    return "🔵", "statement"


def _param(props: dict, name: str) -> str:
    v = (props.get("parameters") or {}).get(name) or []
    return str(v[0]).strip() if v else ""


def _hail(size: str) -> str:
    try:
        val = float(size.lstrip("<>"))
    except ValueError:
        return f"{size}\" hail"
    if val <= 0:
        return ""
    name = HAIL_NAMES.get(round(val, 2))
    inch = f"{val:.2f}".rstrip("0").rstrip(".")
    return f"{name}-size ({inch}\") hail" if name else f"{inch}\" hail"


def _clean(text: str) -> str:
    text = re.sub(r"\s+", " ", text or "").strip()
    text = text.rstrip(".")
    return text + "." if text else ""


def section(description: str, label: str) -> str:
    m = re.search(rf"\*\s*{label}\.\.\.(.*?)(?:\n\s*\n|\n\s*\*|\Z)", description or "", re.S)
    if m:
        return _clean(m.group(1))
    m = re.search(rf"\b{label}\.\.\.(.*?)(?:\n\s*\n|\n[A-Z ]+\.\.\.|\Z)", description or "", re.S)
    return _clean(m.group(1)) if m else ""


def details(props: dict) -> str:
    """The most useful one-line summary of what the alert is about."""
    bits = []
    tor = _param(props, "tornadoDetection")
    if tor:
        bits.append(f"Tornado: {tor.lower()}")
    wind = _param(props, "maxWindGust")
    if wind and not wind.startswith("0"):
        bits.append(f"{wind.lower()} gusts")
    hail = _param(props, "maxHailSize")
    if hail and _hail(hail):
        bits.append(_hail(hail))
    ff = _param(props, "flashFloodDetection")
    if ff:
        bits.append(f"Flash flooding: {ff.lower()}")
    for p in ("tornadoDamageThreat", "thunderstormDamageThreat", "flashFloodDamageThreat"):
        t = _param(props, p)
        if t.upper() in ("CONSIDERABLE", "DESTRUCTIVE", "CATASTROPHIC"):
            bits.append(f"{t.upper()} damage threat")
            break
    if bits:
        out = ", ".join(bits)
        return out[0].upper() + out[1:] + "."
    desc = props.get("description") or ""
    for label in ("WHAT", "HAZARD"):
        s = section(desc, label)
        if s:
            return s
    head = _param(props, "NWSheadline")
    if head:
        return _clean(head.capitalize())
    first = re.split(r"(?<=\.)\s", re.sub(r"\s+", " ", desc).strip(), maxsplit=1)[0]
    return _clean(first.lstrip("."))


def split_areas(area_desc: str) -> list[tuple[str, str]]:
    """"Boone, IN; Northern Marion" -> [("Boone", "IN"), ("Northern Marion", "")]."""
    out = []
    for part in (area_desc or "").split(";"):
        part = part.strip()
        if not part:
            continue
        name, _, state = part.rpartition(",")
        if not name:
            name, state = part, ""
        out.append((name.strip(), state.strip()))
    return out


def format_areas(area_desc: str, counties: bool, max_areas: int, short: bool,
                 priority: set[int] | frozenset = frozenset(), only_mine: bool = False) -> str:
    """"Boone, IN; Marion, IN; Vermilion, IL" -> "Boone, Marion Cos., IN; Vermilion Co., IL".

    ``priority`` = positions (in area_desc) of your own areas: they are listed
    first and always shown. ``only_mine`` lists just those, then "+N more".
    """
    parts = split_areas(area_desc)
    mine = [p for i, p in enumerate(parts) if i in priority]
    others = [p for i, p in enumerate(parts) if i not in priority]
    if only_mine and mine:
        chosen = mine
    else:
        room = max(0, max_areas - len(mine)) if max_areas else len(others)
        chosen = mine + others[:room]
    hidden = len(parts) - len(chosen)
    groups: dict[str, list[str]] = {}
    for name, state in chosen:
        groups.setdefault(state, []).append(name)
    chunks = []
    for state, names in groups.items():
        txt = ", ".join(names)
        if counties:
            txt += (" Cos." if len(names) > 1 else " Co.") if short else \
                   (" counties" if len(names) > 1 else " County")
        if state:
            txt += f", {state}"
        chunks.append(txt)
    out = "; ".join(chunks)
    if hidden > 0:
        out += f" +{hidden} more"
    return out


def _centroid(geometry: dict | None) -> tuple[float, float] | None:
    """A point inside a zone's (Multi)Polygon, as (lat, lon)."""
    if not geometry:
        return None
    polys = geometry.get("coordinates") or []
    if geometry.get("type") == "Polygon":
        polys = [polys]
    rings = [p[0] for p in polys if p and p[0]]
    if not rings:
        return None
    ring = max(rings, key=len)
    a = cx = cy = 0.0
    for (x0, y0), (x1, y1) in zip(ring, ring[1:] + ring[:1]):
        cross = x0 * y1 - x1 * y0
        a += cross
        cx += (x0 + x1) * cross
        cy += (y0 + y1) * cross
    if abs(a) < 1e-12:
        x = sum(p[0] for p in ring) / len(ring)
        y = sum(p[1] for p in ring) / len(ring)
    else:
        x, y = cx / (3 * a), cy / (3 * a)
    return round(y, 4), round(x, 4)


def fmt_time(iso_s: str, tz: ZoneInfo, now: datetime) -> str:
    dt = datetime.fromisoformat(iso_s).astimezone(tz)
    t = dt.strftime("%I:%M %p").lstrip("0").replace(":00 ", " ")
    zone = dt.strftime("%Z")
    if dt.date() == now.astimezone(tz).date():
        return f"{t} {zone}"
    return f"{t} {zone} {dt.strftime('%a')} {dt.month}/{dt.day}"


def time_line(props: dict, tz: ZoneInfo, now: datetime) -> str:
    end = props.get("ends") or props.get("expires")
    onset = props.get("onset") or props.get("effective")
    parts = []
    if onset:
        start = datetime.fromisoformat(onset)
        if (start - now).total_seconds() > 30 * 60:
            parts.append(f"From {fmt_time(onset, tz, now)}")
    if end:
        parts.append(("until " if parts else "Until ") + fmt_time(end, tz, now))
    return "⏰ " + " ".join(parts) if parts else ""


@register_source
class NWSSource(Source):
    name = "nws"
    label = "NWS Alerts"
    description = "Post National Weather Service watches, warnings and advisories."
    default_interval = 120
    backfill_default = True
    fields = [
        Field("areas", "Counties / zones", "rows", default=[], columns=[
            Field("code", "Code (e.g. INC097 or INZ040)"),
            Field("label", "Label (for you)"),
            Field("enabled", "On", "bool", default=True),
        ], help="County codes look like INC145, forecast zones like INZ055. "
                "Find them at api.weather.gov/zones?area=IN&type=county. Each code's name and "
                "matching county/zone are looked up from the NWS automatically."),
        Field("contact", "Contact for NWS (website or email)", "text", default="",
              help="api.weather.gov asks every app to identify itself with a contact in its User-Agent."),
        Field("default_hashtags", "Default hashtags", "tags", default=[]),
        Field("min_severity", "Minimum severity", "select", default="Unknown",
              options=SEVERITY_ORDER, help="Unknown = post everything."),
        Field("include_events", "Only these alert types", "textarea", default="",
              help="One per line, e.g. Tornado Warning. Leave empty for all."),
        Field("exclude_events", "Never post these alert types", "textarea", default="",
              help="One per line."),
        Field("post_updates", "Post updates/extensions of alerts", "bool", default=False),
        Field("post_cancels", "Post cancellations", "bool", default=False),
        Field("abbreviate", "Abbreviate", "select", default="when_needed",
              options=["when_needed", "always", "never"]),
        Field("abbreviations", "Abbreviations", "textarea", default=DEFAULT_ABBREVIATIONS,
              help="One per line: Long = Short."),
        Field("max_areas", "Max areas to list", "int", default=6,
              help="Longer lists end with \"+N more\". Your own areas are always shown."),
        Field("area_focus", "Your areas in the list", "select", default="mine_first",
              options=["mine_first", "only_mine", "nws_order"],
              help="mine first = your counties/zones lead the list; only mine = list just yours, "
                   "then \"+N more\"; nws order = as the NWS lists them."),
        Field("link_card", "Attach link card", "bool", default=True),
        Field("link_template", "Link URL", "text",
              default="https://forecast.weather.gov/showsigwx.php?warnzone={zone}&warncounty={county}",
              help="Placeholders: {zone} {county} (your area's codes; a county's forecast zone is "
                   "looked up automatically), {lat} {lon}, {id}, {state}."),
    ]

    def __init__(self, settings, context):
        super().__init__(settings, context)
        contact = (settings.get("contact") or "").strip() or "contact not set"
        self.http = make_session(f"{APP_NAME}/{__version__} ({contact})")
        self.http.headers["Accept"] = "application/geo+json"
        # one quick try for name/zone lookups, so saving Settings never hangs
        import requests
        self.lookup_http = requests.Session()
        self.lookup_http.headers.update(self.http.headers)

    def codes(self) -> list[str]:
        out = []
        for row in self.settings.get("areas", []):
            code = (row.get("code") or "").strip().upper()
            if row.get("enabled", True) and CODE_RE.match(code):
                out.append(code)
        return list(dict.fromkeys(out))

    def targets(self) -> list[str]:
        return self.codes()

    def fetch(self, codes: list[str]) -> list[dict]:
        feats = []
        for i in range(0, len(codes), 25):
            r = self.http.get(API, params={"zone": ",".join(codes[i:i + 25])}, timeout=TIMEOUT)
            r.raise_for_status()
            feats.extend(r.json().get("features", []))
        return feats

    def wanted(self, props: dict, now: datetime) -> bool:
        s = self.settings
        if props.get("status") != "Actual":
            return False
        mtype = props.get("messageType")
        if mtype == "Update" and not s.get("post_updates"):
            return False
        if mtype == "Cancel" and not s.get("post_cancels"):
            return False
        event = (props.get("event") or "").strip()
        inc = [x.strip().lower() for x in (s.get("include_events") or "").splitlines() if x.strip()]
        exc = [x.strip().lower() for x in (s.get("exclude_events") or "").splitlines() if x.strip()]
        if inc and event.lower() not in inc:
            return False
        if event.lower() in exc:
            return False
        sev = props.get("severity") or "Unknown"
        mins = s.get("min_severity") or "Unknown"
        if mins in SEVERITY_ORDER and sev in SEVERITY_ORDER and \
                SEVERITY_ORDER.index(sev) < SEVERITY_ORDER.index(mins):
            return False
        end = props.get("ends") or props.get("expires")
        if end and datetime.fromisoformat(end) <= now and mtype != "Cancel":
            return False
        return True

    # ------------------------------------------------- area lookups (cached)
    def area_info(self, code: str) -> dict:
        """Name, state, and the linked forecast zone / county for a watched
        code, from api.weather.gov. Cached in settings["zone_info"]."""
        cache = self.settings.setdefault("zone_info", {})
        info = cache.get(code)
        if info and not info.get("error"):
            return info
        if info and time.time() - info.get("at", 0) < 3600:
            return info                       # failed recently: don't hammer the API
        try:
            info = self._lookup(code)
        except Exception as e:
            log.warning("NWS lookup for %s failed: %s", code, e)
            info = {"error": str(e)[:200], "at": time.time()}
        cache[code] = info
        return info

    def _lookup(self, code: str) -> dict:
        kind = "county" if code[2] == "C" else "forecast"
        r = self.lookup_http.get(f"{BASE}/zones/{kind}/{code}", timeout=8)
        r.raise_for_status()
        feat = r.json()
        props = feat.get("properties") or {}
        info = {"name": props.get("name", ""), "state": props.get("state", ""),
                "zone": code if kind == "forecast" else "", "county": code if kind == "county" else ""}
        pt = _centroid(feat.get("geometry"))
        if pt:
            info["lat"], info["lon"] = pt
            p = self.lookup_http.get(f"{BASE}/points/{pt[0]},{pt[1]}", timeout=8)
            if p.ok:
                pp = p.json().get("properties") or {}
                for key, url_key in (("zone", "forecastZone"), ("county", "county")):
                    if not info[key] and pp.get(url_key):
                        info[key] = pp[url_key].rstrip("/").rsplit("/", 1)[-1]
        return info

    def prepare(self, section: dict) -> bool:
        """On settings save: look up any new codes so the first alert is right."""
        self.settings = section
        before = dict(section.get("zone_info") or {})
        for code in self.codes():
            self.area_info(code)
        return section.get("zone_info") != before

    def my_codes(self) -> set[str]:
        """Watched codes plus their linked county/zone codes."""
        out = set()
        for code in self.codes():
            out.add(code)
            info = self.area_info(code)
            out.update(c for c in (info.get("zone"), info.get("county")) if c)
        return out

    def my_positions(self, props: dict) -> set[int]:
        """Which entries of areaDesc are your areas."""
        parts = split_areas(props.get("areaDesc", ""))
        ugc = (props.get("geocode") or {}).get("UGC") or []
        mine = self.my_codes()
        found = set()
        if len(parts) == len(ugc):            # NWS lists areaDesc in UGC order
            found |= {i for i, c in enumerate(ugc) if c in mine}
        names = set()
        for row in self.settings.get("areas", []):
            code = (row.get("code") or "").strip().upper()
            if not (row.get("enabled", True) and CODE_RE.match(code)):
                continue
            info = self.area_info(code)
            if info.get("name"):
                names.add((info["name"].lower(), info.get("state", "")))
            label = re.sub(r"\b(county|co\.?|parish)\b", "", (row.get("label") or ""), flags=re.I).strip(" ,")
            if label:
                names.add((label.lower(), code[:2]))
        same_order = len(parts) == len(ugc)
        for i, (name, state) in enumerate(parts):
            if not state and same_order:
                state = ugc[i][:2]            # zone lists omit the state; the code has it
            if any(name.lower() == n and (not state or not st or state == st) for n, st in names):
                found.add(i)
        return found

    def link(self, props: dict) -> str:
        watched = self.codes()
        ugc = (props.get("geocode") or {}).get("UGC") or []
        hits = [c for c in watched if c in ugc]
        infos = [self.area_info(c) for c in (hits or watched)]
        ugc_z = [c for c in ugc if len(c) > 2 and c[2] == "Z"]
        ugc_c = [c for c in ugc if len(c) > 2 and c[2] == "C"]
        mine = self.my_codes()

        def pick(kind: str, from_ugc: list[str]) -> str:
            for c in from_ugc:                         # your area, as the alert names it
                if c in mine:
                    return c
            for info in infos:                         # your area's linked code
                if info.get(kind):
                    return info[kind]
            return from_ugc[0] if from_ugc else ""     # any area in the alert

        zone, county = pick("zone", ugc_z), pick("county", ugc_c)
        lat = next((i["lat"] for i in infos if i.get("lat")), "")
        lon = next((i["lon"] for i in infos if i.get("lon")), "")
        state = (hits or watched or [""])[0][:2]
        template = self.settings.get("link_template") or ""
        values = {"zone": zone, "county": county, "id": props.get("id", ""), "state": state,
                  "lat": lat, "lon": lon}
        needed = [k for k in values if "{" + k + "}" in template]
        if any(not values[k] for k in needed if k in ("zone", "lat", "lon")):
            if lat and lon:
                return f"https://forecast.weather.gov/MapClick.php?lat={lat}&lon={lon}"
            return "https://alerts.weather.gov/"
        try:
            return template.format(**values)
        except (KeyError, IndexError, ValueError):
            return "https://alerts.weather.gov/"

    def make_item(self, props: dict, now: datetime | None = None) -> Item:
        s = self.settings
        now = now or datetime.now(timezone.utc)
        tz = ZoneInfo(self.ctx.timezone)
        pairs = parse_abbreviations(s.get("abbreviations", DEFAULT_ABBREVIATIONS))
        mode = s.get("abbreviate", "when_needed")
        event = props.get("event") or "Weather Alert"
        marker, _kind = alert_kind(event)
        prefix = {"Update": "UPDATED: ", "Cancel": "CANCELLED: "}.get(props.get("messageType"), "")
        ugc = (props.get("geocode") or {}).get("UGC") or []
        counties = bool(ugc) and all(len(c) > 2 and c[2] == "C" for c in ugc)
        max_areas = int(s.get("max_areas") or 0)
        info = details(props)
        when = time_line(props, tz, now)
        url = self.link(props) or None
        hit = [c for c in self.codes() if c in ugc]
        focus = s.get("area_focus", "mine_first")
        priority = self.my_positions(props) if focus != "nws_order" else set()

        def build(short: bool) -> Item:
            ab = (lambda t: abbreviate(t, pairs)) if short else (lambda t: t)
            title = f"{marker} {prefix}{ab(event)}"
            body = [
                Line("📍 " + ab(format_areas(props.get("areaDesc", ""), counties, max_areas, short,
                                             priority, only_mine=focus == "only_mine")),
                     essential=True),
                Line(when, essential=True),
                Line(ab(info)),
            ]
            card = None
            if url and s.get("link_card", True):
                card = LinkCard(url=url, title=f"{event} – NWS {props.get('senderName', '').replace('NWS ', '')}".strip(" –"),
                                description=truncate(props.get("headline") or info, 250))
            return Item(source=self.name, key=props.get("id") or props.get("@id", ""), title=title,
                        body=body, url=url, card=card,
                        default_tags=parse_tag_list(s.get("default_hashtags")),
                        target=",".join(hit), expires_at=props.get("ends") or props.get("expires"),
                        meta={"event": event, "severity": props.get("severity"),
                              "messageType": props.get("messageType"), "sent": props.get("sent")})

        if mode == "always":
            return build(True)
        full = build(False)
        if mode == "when_needed":
            full.compact = build(True)
        return full

    def poll(self) -> list[Item]:
        codes = self.codes()
        if not codes:
            return []
        now = datetime.now(timezone.utc)
        items = []
        for f in self.fetch(codes):
            props = f.get("properties") or {}
            if self.wanted(props, now):
                items.append(self.make_item(props, now))
        # newest last so posts appear in the order NWS sent them
        items.sort(key=lambda i: i.meta.get("sent") or "")
        return items
