"""JSON settings file with defaults, atomic saves and env-var overrides.

Any setting can be overridden from the environment, which keeps secrets out
of the file if you prefer:  FASTDISSEM__PUBLISHERS__BLUESKY__APP_PASSWORD=...
(double underscores separate the path; values are parsed as JSON when
possible, otherwise used as strings).
"""

from __future__ import annotations

import copy
import json
import os
import secrets
import threading
from pathlib import Path

from .plugins import PUBLISHERS, SOURCES, load_plugins

ENV_PREFIX = "FASTDISSEM__"


def base_defaults() -> dict:
    return {
        "general": {
            "dry_run": True,
            "timezone": "America/Indiana/Indianapolis",
            "log_level": "INFO",
            "max_attempts": 5,
        },
        "web": {
            "host": "127.0.0.1",
            "port": 8787,
            "password": "",
            "secret_key": "",
        },
        "sources": {},
        "publishers": {},
    }


def full_defaults() -> dict:
    load_plugins()
    d = base_defaults()
    for name, cls in SOURCES.items():
        sd = cls.defaults()
        sd["interval_seconds"] = cls.default_interval
        sd["backfill"] = cls.backfill_default
        d["sources"][name] = sd
    for name, cls in PUBLISHERS.items():
        d["publishers"][name] = cls.defaults()
    return d


def _merge(defaults: dict, data: dict) -> dict:
    out = copy.deepcopy(defaults)
    for k, v in (data or {}).items():
        if isinstance(v, dict) and isinstance(out.get(k), dict):
            out[k] = _merge(out[k], v)
        else:
            out[k] = v
    return out


def _apply_env(cfg: dict) -> dict:
    for key, raw in os.environ.items():
        if not key.startswith(ENV_PREFIX):
            continue
        path = [p.lower() for p in key[len(ENV_PREFIX):].split("__") if p]
        if not path:
            continue
        try:
            val = json.loads(raw)
        except ValueError:
            val = raw
        node = cfg
        for p in path[:-1]:
            node = node.setdefault(p, {})
        node[path[-1]] = val
    return cfg


class ConfigStore:
    """Thread-safe holder of the current settings."""

    def __init__(self, path: str | Path):
        self.path = Path(path)
        self._lock = threading.RLock()
        self.version = 0
        self._data = self._load()

    def _load(self) -> dict:
        raw = {}
        if self.path.exists():
            raw = json.loads(self.path.read_text(encoding="utf-8"))
        cfg = _merge(full_defaults(), raw)
        if not cfg["web"].get("secret_key"):
            cfg["web"]["secret_key"] = secrets.token_hex(32)
            self._write(cfg)
        return cfg

    def _write(self, cfg: dict) -> None:
        self.path.parent.mkdir(parents=True, exist_ok=True)
        tmp = self.path.with_suffix(".tmp")
        tmp.write_text(json.dumps(cfg, indent=2), encoding="utf-8")
        try:
            os.chmod(tmp, 0o600)       # it holds passwords
        except OSError:
            pass
        os.replace(tmp, self.path)

    def get(self) -> dict:
        """Effective settings (file + env overrides). Returns a copy."""
        with self._lock:
            return _apply_env(copy.deepcopy(self._data))

    def raw(self) -> dict:
        """Settings as stored in the file (no env overrides)."""
        with self._lock:
            return copy.deepcopy(self._data)

    def save(self, cfg: dict) -> None:
        with self._lock:
            self._data = _merge(full_defaults(), cfg)
            self._write(self._data)
            self.version += 1

    def reload(self) -> None:
        with self._lock:
            self._data = self._load()
            self.version += 1
