"""Text helpers: grapheme counting, hashtags, abbreviations, post fitting."""

from __future__ import annotations

import re
import unicodedata
from dataclasses import replace
from typing import Callable, Iterable

from .models import Item, Line, Rendered

ELLIPSIS = "…"

# ---------------------------------------------------------------- graphemes
# Bluesky's 300 limit counts grapheme clusters (what a reader sees as one
# character, e.g. a flag or a family emoji). The stdlib has no segmenter, so
# this is a close approximation that handles combining marks, ZWJ emoji
# sequences, variation selectors, skin tones, flags and tag sequences.


def _extends(cp: int, ch: str) -> bool:
    return (
        cp == 0x200D
        or 0xFE00 <= cp <= 0xFE0F
        or 0x1F3FB <= cp <= 0x1F3FF
        or 0xE0020 <= cp <= 0xE007F
        or unicodedata.category(ch) in ("Mn", "Me", "Mc")
    )


def graphemes(s: str) -> list[str]:
    out: list[str] = []
    after_zwj = False
    open_flag = False
    for ch in s:
        cp = ord(ch)
        is_ri = 0x1F1E6 <= cp <= 0x1F1FF
        if out and (_extends(cp, ch) or after_zwj):
            out[-1] += ch
        elif out and is_ri and open_flag:
            out[-1] += ch
            open_flag = False
            after_zwj = False
            continue
        elif out and ch == "\n" and out[-1] == "\r":
            out[-1] += ch
        else:
            out.append(ch)
            open_flag = is_ri
        after_zwj = cp == 0x200D
    return out


def grapheme_len(s: str) -> int:
    return len(graphemes(s))


def truncate(s: str, max_len: int, measure: Callable[[str], int] = grapheme_len) -> str:
    """Shorten ``s`` to ``max_len`` (as measured), at a word boundary when
    reasonable, adding an ellipsis."""
    if measure(s) <= max_len:
        return s
    if max_len <= 1:
        return ELLIPSIS if max_len == 1 else ""
    g = graphemes(s)
    # measure() may weigh things differently (Mastodon URLs), so shrink until it fits
    keep = min(len(g), max_len - 1)
    while keep > 0:
        cand = "".join(g[:keep]).rstrip()
        cut = cand.rfind(" ")
        if cut >= keep * 0.6:          # don't throw away too much for a clean break
            cand = cand[:cut]
        cand = cand.rstrip(" ,;:-–—.") + ELLIPSIS
        if measure(cand) <= max_len:
            return cand
        keep -= 1
    return ELLIPSIS


# ----------------------------------------------------------------- hashtags
_TAG_SPLIT = re.compile(r"[^\w]+", re.UNICODE)


def to_hashtag(raw: str) -> str | None:
    """Turn a YouTube tag / user entry into a hashtag body (without '#').

    "severe weather" -> "SevereWeather"; "#INwx" -> "INwx"; "2026" -> None.
    """
    raw = (raw or "").strip().lstrip("#").strip()
    words = [w for w in _TAG_SPLIT.split(raw) if w]
    words = [w.replace("_", "") if len(words) > 1 else w for w in words]
    words = [w for w in words if w]
    if not words:
        return None
    if len(words) == 1:
        tag = words[0]
    else:
        tag = "".join(w[:1].upper() + w[1:] for w in words)
    if tag.isdigit() or grapheme_len(tag) > 64:
        return None
    return tag


def parse_tag_list(value: str | Iterable[str] | None) -> list[str]:
    """Accept "a, b #c" or a list; return normalized, de-duplicated tags."""
    if value is None:
        return []
    if isinstance(value, str):
        if "," in value:
            parts = value.split(",")
        else:
            parts = value.split()
    else:
        parts = list(value)
    return dedupe_tags(t for t in (to_hashtag(p) for p in parts) if t)


def dedupe_tags(tags: Iterable[str], exclude: Iterable[str] = ()) -> list[str]:
    seen = {t.lower() for t in exclude}
    out = []
    for t in tags:
        if t and t.lower() not in seen:
            seen.add(t.lower())
            out.append(t)
    return out


HASHTAG_RE = re.compile(r"(?:^|(?<=\s))#([^\s#​-‏!\"$%&'()*+,./:;<=>?@\[\\\]^`{|}~]+)")
URL_RE = re.compile(r"https?://[^\s<>\"']+[^\s<>\"'.,;:!?)\]]")


# ------------------------------------------------------------ abbreviations
def parse_abbreviations(text: str) -> list[tuple[str, str]]:
    """Parse "Long = Short" lines (blank lines and # comments ignored)."""
    pairs = []
    for line in (text or "").splitlines():
        line = line.strip()
        if not line or line.startswith("#") or "=" not in line:
            continue
        long, short = line.split("=", 1)
        if long.strip():
            pairs.append((long.strip(), short.strip()))
    # longest first so "Severe Thunderstorm" wins over "Thunderstorm"
    return sorted(pairs, key=lambda p: -len(p[0]))


def abbreviate(text: str, pairs: list[tuple[str, str]]) -> str:
    for long, short in pairs:
        pat = re.compile(r"(?<!\w)" + re.escape(long) + r"(?!\w)", re.IGNORECASE)

        def repl(m, short=short):
            word = m.group(0)
            if word.isupper() and len(word) > 1 and not short.isupper():
                return short.upper()
            return short

        text = pat.sub(repl, text)
    return text


# ------------------------------------------------------------------- compose
def _render(title: str, body: list[Line], url: str | None, tags: list[str]) -> str:
    lines = [title] + [ln.text for ln in body if ln.text]
    text = "\n".join(lines)
    if url:
        text += "\n" + url
    if tags:
        text += "\n\n" + " ".join("#" + t for t in tags)
    return text.strip()


def compose(
    item: Item,
    limit: int,
    measure: Callable[[str], int] = grapheme_len,
    include_url: bool = False,
    min_line: int = 18,
) -> Rendered:
    """Fit ``item`` into ``limit`` for one platform.

    Trimming order: extra (e.g. video) hashtags -> optional body lines ->
    essential body lines (shortened, never removed) -> default hashtags ->
    the title. If the item has a ``compact`` (abbreviated) variant and the
    full version needs any trimming, the compact variant is used instead.
    """
    r = _compose_one(item, limit, measure, include_url, min_line)
    if r.trimmed and item.compact is not None:
        rc = _compose_one(item.compact, limit, measure, include_url, min_line)
        rc.card = item.compact.card or item.card
        return rc
    return r


def _compose_one(item, limit, measure, include_url, min_line) -> Rendered:
    title = item.title.strip()
    body = [replace(ln) for ln in item.body if ln.text.strip()]
    url = item.url if include_url else None
    req = list(item.default_tags)
    opt = dedupe_tags(item.extra_tags, exclude=req)
    trimmed = False

    def size():
        return measure(_render(title, body, url, req + opt))

    while size() > limit and opt:
        opt.pop()
        trimmed = True
    # optional lines: shorten the last one, else drop it
    for i in range(len(body) - 1, -1, -1):
        if size() <= limit:
            break
        if body[i].essential:
            continue
        over = size() - limit
        room = measure(body[i].text) - over
        trimmed = True
        if room >= min_line:
            body[i].text = truncate(body[i].text, room, measure)
        else:
            body.pop(i)
    # essential lines: shorten only
    for ln in reversed(body):
        if size() <= limit:
            break
        over = size() - limit
        room = max(min_line, measure(ln.text) - over)
        if room < measure(ln.text):
            ln.text = truncate(ln.text, room, measure)
            trimmed = True
    # shorten a long title before giving up the default hashtags
    if size() > limit and req:
        over = size() - limit
        room = max(min(60, measure(title)), measure(title) - over)
        if room < measure(title):
            title = truncate(title, room, measure)
            trimmed = True
    while size() > limit and req:
        req.pop()
        trimmed = True
    if size() > limit:
        over = size() - limit
        title = truncate(title, max(1, measure(title) - over), measure)
        trimmed = True
    text = _render(title, body, url, req + opt)
    if measure(text) > limit:  # last resort, should not happen
        text = truncate(text, limit, measure)
        trimmed = True
    return Rendered(text=text, tags=req + opt, length=measure(text), limit=limit,
                    trimmed=trimmed, card=item.card)


def mastodon_measure(text: str) -> int:
    """Mastodon counts every URL as 23 characters."""
    return len(URL_RE.sub("x" * 23, text))
