"""SQLite store: seen items, per-network deliveries, activity log.

Duplicate prevention works at two levels:
  * items      - every Item.uid ever seen. A uid already here is never
                 queued again, across restarts.
  * deliveries - one row per (item, network). A network only gets an item
                 once; failed sends are retried with back-off.
"""

from __future__ import annotations

import json
import sqlite3
import threading
from datetime import datetime, timedelta, timezone
from pathlib import Path

from .models import Item

SCHEMA = """
CREATE TABLE IF NOT EXISTS items (
    uid TEXT PRIMARY KEY,
    source TEXT NOT NULL,
    target TEXT,
    title TEXT,
    url TEXT,
    status TEXT NOT NULL,          -- queued | seeded | skipped
    payload TEXT,
    first_seen TEXT NOT NULL
);
CREATE TABLE IF NOT EXISTS deliveries (
    uid TEXT NOT NULL,
    publisher TEXT NOT NULL,
    status TEXT NOT NULL,          -- pending | sent | failed | dry_run | expired | gave_up
    attempts INTEGER NOT NULL DEFAULT 0,
    next_try TEXT,
    last_error TEXT,
    post_url TEXT,
    text TEXT,
    updated_at TEXT NOT NULL,
    PRIMARY KEY (uid, publisher)
);
CREATE TABLE IF NOT EXISTS targets (
    key TEXT PRIMARY KEY,
    initialized_at TEXT NOT NULL
);
CREATE TABLE IF NOT EXISTS events (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    ts TEXT NOT NULL,
    level TEXT NOT NULL,
    message TEXT NOT NULL
);
CREATE INDEX IF NOT EXISTS ix_deliv_status ON deliveries(status);
"""


def now() -> datetime:
    return datetime.now(timezone.utc)


def iso(dt: datetime) -> str:
    return dt.astimezone(timezone.utc).isoformat(timespec="seconds")


class Storage:
    def __init__(self, path: str | Path):
        self.path = str(path)
        Path(self.path).parent.mkdir(parents=True, exist_ok=True)
        self._lock = threading.RLock()
        self._db = sqlite3.connect(self.path, check_same_thread=False)
        self._db.row_factory = sqlite3.Row
        self._db.execute("PRAGMA journal_mode=WAL")
        self._db.executescript(SCHEMA)
        self._db.commit()

    def _q(self, sql, args=()):
        with self._lock:
            cur = self._db.execute(sql, args)
            self._db.commit()
            return cur

    def _all(self, sql, args=()):
        with self._lock:
            return [dict(r) for r in self._db.execute(sql, args).fetchall()]

    # items -------------------------------------------------------------
    def seen(self, uid: str) -> bool:
        return bool(self._all("SELECT 1 FROM items WHERE uid=?", (uid,)))

    def add_item(self, item: Item, status: str) -> bool:
        """Insert; returns False if it already existed (race-safe)."""
        cur = self._q(
            "INSERT OR IGNORE INTO items(uid,source,target,title,url,status,payload,first_seen)"
            " VALUES (?,?,?,?,?,?,?,?)",
            (item.uid, item.source, item.target, item.title, item.url, status,
             json.dumps(item.to_dict()), iso(now())),
        )
        return cur.rowcount == 1

    def get_item(self, uid: str) -> Item | None:
        rows = self._all("SELECT payload FROM items WHERE uid=?", (uid,))
        return Item.from_dict(json.loads(rows[0]["payload"])) if rows else None

    def forget(self, uid: str) -> None:
        """Remove an item so it can be posted again on the next check."""
        self._q("DELETE FROM deliveries WHERE uid=?", (uid,))
        self._q("DELETE FROM items WHERE uid=?", (uid,))

    # targets -----------------------------------------------------------
    def target_initialized(self, key: str) -> bool:
        return bool(self._all("SELECT 1 FROM targets WHERE key=?", (key,)))

    def mark_target(self, key: str) -> None:
        self._q("INSERT OR IGNORE INTO targets(key,initialized_at) VALUES (?,?)", (key, iso(now())))

    # deliveries --------------------------------------------------------
    def queue(self, uid: str, publisher: str) -> None:
        self._q(
            "INSERT OR IGNORE INTO deliveries(uid,publisher,status,attempts,next_try,updated_at)"
            " VALUES (?,?,?,?,?,?)",
            (uid, publisher, "pending", 0, iso(now()), iso(now())),
        )

    def due(self) -> list[dict]:
        return self._all(
            "SELECT * FROM deliveries WHERE status IN ('pending','failed') AND next_try<=?"
            " ORDER BY updated_at",
            (iso(now()),),
        )

    def record(self, uid, publisher, status, *, error=None, post_url=None, text=None,
               attempts=None, backoff_s=None) -> None:
        nxt = iso(now() + timedelta(seconds=backoff_s)) if backoff_s else None
        self._q(
            "UPDATE deliveries SET status=?, last_error=?, post_url=COALESCE(?,post_url),"
            " text=COALESCE(?,text), attempts=COALESCE(?,attempts), next_try=?, updated_at=?"
            " WHERE uid=? AND publisher=?",
            (status, error, post_url, text, attempts, nxt, iso(now()), uid, publisher),
        )

    def recent(self, limit: int = 50) -> list[dict]:
        return self._all(
            "SELECT d.*, i.title, i.url, i.source, i.target FROM deliveries d"
            " JOIN items i ON i.uid=d.uid ORDER BY d.updated_at DESC LIMIT ?",
            (limit,),
        )

    def counts(self) -> dict:
        rows = self._all("SELECT status, COUNT(*) n FROM deliveries GROUP BY status")
        out = {r["status"]: r["n"] for r in rows}
        out["seen_items"] = self._all("SELECT COUNT(*) n FROM items")[0]["n"]
        return out

    # events ------------------------------------------------------------
    def event(self, level: str, message: str) -> None:
        self._q("INSERT INTO events(ts,level,message) VALUES (?,?,?)", (iso(now()), level, message[:2000]))
        self._q("DELETE FROM events WHERE id < (SELECT MAX(id) - 500 FROM events)")

    def events(self, limit: int = 30) -> list[dict]:
        return self._all("SELECT * FROM events ORDER BY id DESC LIMIT ?", (limit,))
