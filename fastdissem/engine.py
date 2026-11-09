"""The polling loop: ask sources for items, skip duplicates, deliver."""

from __future__ import annotations

import logging
import threading
import time
from datetime import datetime, timezone

from .config import ConfigStore
from .models import Item
from .plugins import PUBLISHERS, SOURCES, Context, Publisher, Source, load_plugins
from .storage import Storage

log = logging.getLogger(__name__)


def _ctx(cfg: dict, storage: Storage | None) -> Context:
    return Context(timezone=cfg["general"].get("timezone") or "UTC", storage=storage,
                   general=cfg["general"])


def build_sources(cfg: dict, storage=None, only_enabled=True) -> dict[str, Source]:
    load_plugins()
    ctx = _ctx(cfg, storage)
    return {n: SOURCES[n](s, ctx) for n, s in cfg["sources"].items()
            if n in SOURCES and (s.get("enabled") or not only_enabled)}


def build_publishers(cfg: dict, storage=None, only_enabled=True) -> dict[str, Publisher]:
    load_plugins()
    ctx = _ctx(cfg, storage)
    return {n: PUBLISHERS[n](s, ctx) for n, s in cfg["publishers"].items()
            if n in PUBLISHERS and (s.get("enabled") or not only_enabled)}


class Engine:
    def __init__(self, config: ConfigStore, storage: Storage, dry_run_override: bool | None = None):
        self.config = config
        self.storage = storage
        self.dry_run_override = dry_run_override
        self._built_version = -1
        self.sources: dict[str, Source] = {}
        self.publishers: dict[str, Publisher] = {}
        self.cfg: dict = {}
        self.status: dict[str, dict] = {}
        self._wake = threading.Event()
        self._force: set[str] = set()
        self._run_lock = threading.Lock()
        self.started_at = datetime.now(timezone.utc)

    # ------------------------------------------------------------- set-up
    def _rebuild_if_needed(self) -> None:
        if self._built_version == self.config.version and self.cfg:
            return
        self.cfg = self.config.get()
        self.sources = build_sources(self.cfg, self.storage)
        self.publishers = build_publishers(self.cfg, self.storage)
        self._built_version = self.config.version
        log.info("Active sources: %s | networks: %s | dry run: %s",
                 ", ".join(self.sources) or "none", ", ".join(self.publishers) or "none", self.dry_run)

    @property
    def dry_run(self) -> bool:
        if self.dry_run_override is not None:
            return self.dry_run_override
        return bool(self.cfg.get("general", {}).get("dry_run", True))

    def trigger(self, source: str | None = None) -> None:
        """Ask the loop to check now (all sources, or one)."""
        self._force.update([source] if source else list(self.sources) or ["*"])
        self._wake.set()

    # --------------------------------------------------------------- loop
    def run_forever(self, stop: threading.Event) -> None:
        log.info("Engine started")
        while not stop.is_set():
            try:
                self.tick()
            except Exception:
                log.exception("Engine tick failed")
            self._wake.wait(timeout=2)
            self._wake.clear()
        log.info("Engine stopped")

    def tick(self) -> None:
        with self._run_lock:
            self._rebuild_if_needed()
            now = time.time()
            for name, src in self.sources.items():
                st = self.status.setdefault(name, {"last_run": 0.0})
                interval = max(30, int(self.cfg["sources"][name].get("interval_seconds") or src.default_interval))
                if name in self._force or "*" in self._force or now - st["last_run"] >= interval:
                    self._force.discard(name)
                    self.run_source(name, src)
                    st["next_run"] = st["last_run"] + interval
            self._force.discard("*")
            self.deliver_due()

    def run_once(self) -> None:
        self._rebuild_if_needed()
        for name, src in self.sources.items():
            self.run_source(name, src)
        self.deliver_due()

    # ------------------------------------------------------------ sources
    def run_source(self, name: str, src: Source) -> int:
        st = self.status.setdefault(name, {})
        st["last_run"] = time.time()
        try:
            items = src.poll()
        except Exception as e:
            st["last_error"] = f"{type(e).__name__}: {e}"
            st["ok"] = False
            log.warning("%s poll failed: %s", name, e)
            self.storage.event("error", f"{src.label}: check failed – {e}")
            return 0
        st.update(ok=True, last_error="", last_count=len(items))
        return self.ingest(name, src, items)

    def ingest(self, name: str, src: Source, items: list[Item]) -> int:
        backfill = bool(self.cfg["sources"][name].get("backfill"))
        all_targets = set(src.targets())
        new_targets = {t for t in all_targets if not self.storage.target_initialized(f"{name}:{t}")}
        queued = 0
        for item in items:
            if self.storage.seen(item.uid):
                continue
            item_targets = {t for t in item.target.split(",") if t}
            first_look = (item_targets and item_targets <= new_targets) or \
                         (not item_targets and new_targets and new_targets == all_targets)
            if first_look and not backfill:
                # Already existed when we started watching this channel/area.
                self.storage.add_item(item, "seeded")
                continue
            if not self.storage.add_item(item, "queued"):
                continue
            queued += 1
            for pub in self.publishers:
                self.storage.queue(item.uid, pub)
            log.info("New %s item: %s", name, item.title)
        for t in new_targets:
            self.storage.mark_target(f"{name}:{t}")
        if new_targets and not backfill:
            self.storage.event("info", f"{src.label}: started watching {', '.join(sorted(new_targets))} "
                                       f"(existing items marked as already posted)")
        return queued

    # ---------------------------------------------------------- delivery
    def deliver_due(self) -> None:
        max_attempts = int(self.cfg["general"].get("max_attempts") or 5)
        for d in self.storage.due():
            uid, pname = d["uid"], d["publisher"]
            pub = self.publishers.get(pname)
            if pub is None:            # network disabled: check again later
                self.storage.record(uid, pname, d["status"], error="network disabled",
                                    backoff_s=600)
                continue
            item = self.storage.get_item(uid)
            if item is None:
                continue
            if item.expires_at:
                try:
                    if datetime.fromisoformat(item.expires_at) <= datetime.now(timezone.utc):
                        self.storage.record(uid, pname, "expired", error="expired before it could be sent")
                        continue
                except ValueError:
                    pass
            try:
                rendered = pub.render(item)
            except Exception as e:
                self.storage.record(uid, pname, "gave_up", error=f"render failed: {e}")
                log.exception("Render failed for %s", uid)
                continue
            if self.dry_run:
                self.storage.record(uid, pname, "dry_run", text=rendered.text)
                log.info("[dry run] %s → %s:\n%s", pname, uid, rendered.text)
                continue
            attempts = d["attempts"] + 1
            try:
                res = pub.publish(item, rendered)
            except Exception as e:
                status = "gave_up" if attempts >= max_attempts else "failed"
                backoff = min(3600, 60 * 2 ** (attempts - 1))
                self.storage.record(uid, pname, status, error=str(e)[:500], attempts=attempts,
                                    text=rendered.text, backoff_s=backoff if status == "failed" else None)
                self.storage.event("error", f"{pub.label}: could not post “{item.title}” – {e}")
                log.warning("%s post failed (%d/%d): %s", pname, attempts, max_attempts, e)
                continue
            self.storage.record(uid, pname, "sent", post_url=res.post_url, text=rendered.text,
                                attempts=attempts)
            self.storage.event("info", f"{pub.label}: posted “{item.title}”")
            log.info("Posted %s to %s %s", uid, pname, res.post_url)

    # ----------------------------------------------------------- preview
    def preview(self) -> list[dict]:
        """Current items of every enabled source, rendered for every network
        (enabled ones, or all if none are enabled). Posts nothing."""
        cfg = self.config.get()
        sources = build_sources(cfg)
        pubs = build_publishers(cfg) or build_publishers(cfg, only_enabled=False)
        pubs.pop("console", None) if len(pubs) > 1 else None
        out = []
        for name, src in sources.items():
            try:
                items = src.poll()
            except Exception as e:
                out.append({"source": src.label, "error": str(e), "items": []})
                continue
            rows = []
            for item in items[-10:]:
                rows.append({
                    "item": item, "seen": self.storage.seen(item.uid),
                    "renders": {p.label: p.render(item) for p in pubs.values()},
                })
            out.append({"source": src.label, "error": "", "items": rows})
        return out
