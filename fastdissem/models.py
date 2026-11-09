"""Platform-neutral data passed from sources to publishers."""

from __future__ import annotations

from dataclasses import asdict, dataclass, field
from typing import Any


@dataclass
class Line:
    """One body line of a post.

    ``essential`` lines may be shortened to fit but are never dropped
    (e.g. an alert's expiration time). Non-essential lines are dropped first.
    """

    text: str
    essential: bool = False


@dataclass
class LinkCard:
    """A rich link preview (Bluesky external embed, OpenGraph-ish)."""

    url: str
    title: str = ""
    description: str = ""
    thumb_url: str | None = None


@dataclass
class Item:
    """Something a source found that may be posted.

    ``uid`` must be globally unique and stable; it is the duplicate-detection
    key, so the same YouTube video seen through two channels, or the same NWS
    alert matched by two zones, is only posted once.
    """

    source: str                      # plug-in name, e.g. "youtube"
    key: str                         # id unique within the source
    title: str                       # first line of the post
    body: list[Line] = field(default_factory=list)
    url: str | None = None           # canonical link for the item
    default_tags: list[str] = field(default_factory=list)  # kept preferentially
    extra_tags: list[str] = field(default_factory=list)    # dropped first
    card: LinkCard | None = None
    target: str = ""                 # e.g. channel id / area code that matched
    expires_at: str | None = None    # ISO time after which posting is pointless
    compact: "Item | None" = None    # abbreviated variant, used when full won't fit
    meta: dict[str, Any] = field(default_factory=dict)

    @property
    def uid(self) -> str:
        return f"{self.source}:{self.key}"

    def to_dict(self) -> dict:
        return asdict(self)

    @classmethod
    def from_dict(cls, d: dict) -> "Item":
        d = dict(d)
        d["body"] = [Line(**ln) for ln in d.get("body", [])]
        if d.get("card"):
            d["card"] = LinkCard(**d["card"])
        if d.get("compact"):
            d["compact"] = cls.from_dict(d["compact"])
        return cls(**d)


@dataclass
class Rendered:
    """A post rendered for one specific platform."""

    text: str
    tags: list[str]
    length: int
    limit: int
    trimmed: bool = False
    card: LinkCard | None = None


@dataclass
class PublishResult:
    post_id: str = ""
    post_url: str = ""
