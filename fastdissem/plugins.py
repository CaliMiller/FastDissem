"""Plug-in base classes and registries for sources and publishers.

Adding a new social network = one new file in ``fastdissem/publishers/``
that subclasses :class:`Publisher` and is decorated with
``@register_publisher``. Its ``fields`` drive the settings form in the web UI
automatically. Sources work the same way in ``fastdissem/sources/``.
"""

from __future__ import annotations

import copy
import importlib
import logging
import pkgutil
from dataclasses import dataclass, field
from typing import Any, Callable

from .models import Item, PublishResult, Rendered
from .text import compose, grapheme_len

log = logging.getLogger(__name__)


@dataclass
class Field:
    """A setting shown in the web UI.

    type: text | password | int | bool | textarea | tags | select | rows
      * tags: hashtags, entered comma/space separated, stored as a list
      * rows: a small editable table; ``columns`` is a list of Field
    """

    key: str
    label: str
    type: str = "text"
    default: Any = ""
    help: str = ""
    secret: bool = False
    options: list[str] = field(default_factory=list)
    columns: list["Field"] = field(default_factory=list)


class Plugin:
    name: str = ""
    label: str = ""
    description: str = ""
    fields: list[Field] = []

    def __init__(self, settings: dict, context: "Context"):
        self.settings = settings
        self.ctx = context

    @classmethod
    def defaults(cls) -> dict:
        d = {"enabled": False}
        for f in cls.fields:
            d[f.key] = copy.deepcopy(f.default)
        return d


@dataclass
class Context:
    """Shared services handed to plug-ins."""

    timezone: str = "America/New_York"
    http_get: Callable[..., Any] | None = None
    storage: Any = None
    general: dict = field(default_factory=dict)


class Source(Plugin):
    """Produces Items. ``poll`` must be safe to call repeatedly; the engine
    handles duplicate detection by Item.uid."""

    default_interval = 300
    backfill_default = False

    def poll(self) -> list[Item]:
        raise NotImplementedError

    def targets(self) -> list[str]:
        """Keys of the things being watched (channels, areas). A target seen
        for the first time is 'seeded' (existing items marked as already
        posted) unless the source's backfill setting is on."""
        return []

    def prepare(self, config_section: dict) -> bool:
        """Called after settings are saved in the UI; may fill derived values
        (e.g. resolve @handles to channel ids). Return True if changed."""
        return False


class Publisher(Plugin):
    """Posts Items to one network."""

    char_limit = 500
    include_url_in_text = True   # False when the platform shows a link card

    def measure(self, text: str) -> int:
        return grapheme_len(text)

    def render(self, item: Item) -> Rendered:
        return compose(item, self.char_limit, self.measure, self.include_url_in_text)

    def publish(self, item: Item, rendered: Rendered) -> PublishResult:
        raise NotImplementedError

    def check(self) -> str:
        """Verify credentials; return a human message or raise."""
        return "OK"


SOURCES: dict[str, type[Source]] = {}
PUBLISHERS: dict[str, type[Publisher]] = {}


def register_source(cls):
    SOURCES[cls.name] = cls
    return cls


def register_publisher(cls):
    PUBLISHERS[cls.name] = cls
    return cls


_loaded = False


def load_plugins() -> None:
    global _loaded
    if _loaded:
        return
    for pkg_name in ("fastdissem.sources", "fastdissem.publishers"):
        pkg = importlib.import_module(pkg_name)
        for mod in pkgutil.iter_modules(pkg.__path__):
            if mod.name.startswith("_"):
                continue
            try:
                importlib.import_module(f"{pkg_name}.{mod.name}")
            except Exception:  # a broken plug-in must not take down the app
                log.exception("Failed to load plug-in %s.%s", pkg_name, mod.name)
    _loaded = True
