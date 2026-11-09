"""Command line entry point.

  python -m fastdissem run            # engine + web UI (default)
  python -m fastdissem run --no-web   # headless engine only
  python -m fastdissem once           # one check-and-post pass, then exit (cron friendly)
  python -m fastdissem preview        # print what would be posted, post nothing
  python -m fastdissem web            # web UI only
"""

from __future__ import annotations

import argparse
import logging
import os
import signal
import sys
import threading
from pathlib import Path

from . import __version__
from .config import ConfigStore
from .engine import Engine
from .storage import Storage


def main(argv=None) -> int:
    ap = argparse.ArgumentParser(prog="fastdissem", description="Fast Dissem – fast dissemination")
    ap.add_argument("command", nargs="?", default="run", choices=["run", "once", "preview", "web"])
    ap.add_argument("--data-dir", default=os.environ.get("FASTDISSEM_DATA", "data"),
                    help="Folder for config.json and the database (default: ./data)")
    ap.add_argument("--no-web", action="store_true", help="Don't start the web UI")
    ap.add_argument("--host", help="Override web UI listen address")
    ap.add_argument("--port", type=int, help="Override web UI port")
    dr = ap.add_mutually_exclusive_group()
    dr.add_argument("--dry-run", dest="dry_run", action="store_true", default=None,
                    help="Compose but don't send, regardless of settings")
    dr.add_argument("--live", dest="dry_run", action="store_false",
                    help="Send for real, regardless of settings")
    ap.add_argument("--version", action="version", version=__version__)
    args = ap.parse_args(argv)

    data = Path(args.data_dir)
    config = ConfigStore(data / "config.json")
    cfg = config.get()
    logging.basicConfig(level=cfg["general"].get("log_level", "INFO"),
                        format="%(asctime)s %(levelname)-7s %(name)s: %(message)s")
    logging.getLogger("werkzeug").setLevel(logging.WARNING)
    log = logging.getLogger("fastdissem")
    storage = Storage(data / "fastdissem.db")
    engine = Engine(config, storage, dry_run_override=args.dry_run)

    if args.command == "once":
        engine.run_once()
        return 0

    if args.command == "preview":
        for g in engine.preview():
            print(f"\n===== {g['source']} =====")
            if g["error"]:
                print("  error:", g["error"])
            for row in g["items"]:
                print(f"\n--- {row['item'].uid} {'(already handled)' if row['seen'] else '(NEW)'}")
                for label, r in row["renders"].items():
                    print(f"[{label} {r.length}/{r.limit}{' trimmed' if r.trimmed else ''}]")
                    print(r.text)
                    if r.card:
                        print(f"  [card] {r.card.title} – {r.card.url}")
        return 0

    stop = threading.Event()

    def _stop(*_):
        log.info("Shutting down…")
        stop.set()
        if args.command == "web" or not args.no_web:
            os._exit(0)  # the dev web server has no clean stop hook

    signal.signal(signal.SIGTERM, _stop)
    signal.signal(signal.SIGINT, _stop)

    if args.command == "run":
        t = threading.Thread(target=engine.run_forever, args=(stop,), name="engine", daemon=True)
        t.start()
        if args.no_web:
            while not stop.is_set():
                stop.wait(1)
            t.join(timeout=10)
            return 0

    from .web import create_app
    app = create_app(config, storage, engine)
    host = args.host or cfg["web"].get("host", "127.0.0.1")
    port = args.port or int(cfg["web"].get("port", 8787))
    if host not in ("127.0.0.1", "localhost", "::1") and not cfg["web"].get("password"):
        log.warning("Web UI is reachable from the network WITHOUT a password. Set one in Settings → Web UI.")
    try:
        from waitress import serve  # optional, nicer production server
        log.info("Web UI on http://%s:%s (waitress)", host, port)
        serve(app, host=host, port=port, threads=4)
    except ImportError:
        log.info("Web UI on http://%s:%s", host, port)
        app.run(host=host, port=port, debug=False, use_reloader=False, threaded=True)
    return 0


if __name__ == "__main__":
    sys.exit(main())
