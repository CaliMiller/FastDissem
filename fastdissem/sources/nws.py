"""National Weather Service active alerts for chosen counties / zones.

Uses https://api.weather.gov/alerts/active?zone=... which accepts both
county codes (INC097) and forecast-zone codes (INZ040). Each alert's NWS id
is the duplicate key, so an alert covering several watched areas posts once.
"""

from __future__ import annotations

import logging
import re
from datetime import datetime, timezone
from zoneinfo import ZoneInfo

from ..http import TIMEOUT, make_session
from ..models import Item, Line, LinkCard
from ..plugins import Field, Source, register_source
from ..text import abbreviate, parse_abbreviations, parse_tag_list, truncate
from .. import APP_NAME, __version__

log = logging.getLogger(__name__)

API = "https://api.weather.gov/alerts/active"
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


def format_areas(area_desc: str, counties: bool, max_areas: int, short: bool) -> str:
    """"Boone, IN; Marion, IN; Vermilion, IL" -> "Boone, Marion Cos., IN; Vermilion Co., IL"."""
    groups: dict[str, list[str]] = {}
    for part in (area_desc or "").split(";"):
        part = part.strip()
        if not part:
            continue
        name, _, state = part.rpartition(",")
        if not name:
            name, state = part, ""
        groups.setdefault(state.strip(), []).append(name.strip())
    total = sum(len(v) for v in groups.values())
    shown = 0
    chunks = []
    for state, names in groups.items():
        take = names[: max(0, max_areas - shown)] if max_areas else names
        shown += len(take)
        if not take:
            continue
        txt = ", ".join(take)
        if counties:
            txt += (" Cos." if len(take) > 1 else " Co.") if short else \
                   (" counties" if len(take) > 1 else " County")
        if state:
            txt += f", {state}"
        chunks.append(txt)
    out = "; ".join(chunks)
    if total > shown:
        out += f" +{total - shown} more"
    return out


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
        ], help="County codes look like INC097, forecast zones like INZ040. "
                "Find them at alerts.weather.gov or api.weather.gov/zones?area=IN."),
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
              help="Longer lists end with \"+N more\"."),
        Field("link_card", "Attach link card", "bool", default=True),
        Field("link_template", "Link URL", "text",
              default="https://forecast.weather.gov/showsigwx.php?warnzone={zone}&warncounty={county}",
              help="Placeholders: {zone} {county} (your first matching watched codes), {id}, {state}."),
    ]

    def __init__(self, settings, context):
        super().__init__(settings, context)
        contact = (settings.get("contact") or "").strip() or "contact not set"
        self.http = make_session(f"{APP_NAME}/{__version__} ({contact})")
        self.http.headers["Accept"] = "application/geo+json"

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

    def link(self, props: dict) -> str:
        watched = self.codes()
        ugc = (props.get("geocode") or {}).get("UGC") or []
        hits = [c for c in watched if c in ugc] or watched
        zone = next((c for c in hits if c[2] == "Z"), next((c for c in watched if c[2] == "Z"), ""))
        county = next((c for c in hits if c[2] == "C"), next((c for c in watched if c[2] == "C"), ""))
        state = (hits[0][:2] if hits else "")
        try:
            return (self.settings.get("link_template") or "").format(
                zone=zone, county=county, id=props.get("id", ""), state=state)
        except (KeyError, IndexError, ValueError):
            return "https://www.weather.gov/"

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

        def build(short: bool) -> Item:
            ab = (lambda t: abbreviate(t, pairs)) if short else (lambda t: t)
            title = f"{marker} {prefix}{ab(event)}"
            body = [
                Line("📍 " + ab(format_areas(props.get("areaDesc", ""), counties, max_areas, short)),
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
