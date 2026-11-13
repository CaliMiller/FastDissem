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

DEFAULT_PORT = 8787


def port_problem(host: str, port: int) -> str | None:
    """Why the web UI can't listen on host:port, or None if it can."""
    import socket
    fam = socket.AF_INET6 if ":" in host else socket.AF_INET
    try:
        with socket.socket(fam, socket.SOCK_STREAM) as s:
            s.setsockopt(socket.SOL_SOCKET, socket.SO_REUSEADDR, 1)
            s.bind((host, port))
    except OSError as e:
        return e.strerror or str(e)
    return None


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
    def serve_on(p: int) -> None:
        try:
            from waitress import serve  # optional, nicer production server
        except ImportError:
            serve = None
        log.info("Web UI on http://%s:%s%s", host, p, " (waitress)" if serve else "")
        if serve:
            serve(app, host=host, port=p, threads=4)
        else:
            app.run(host=host, port=p, debug=False, use_reloader=False, threaded=True)

    problem = port_problem(host, port)
    if problem and port != DEFAULT_PORT:
        # A bad port must not take the posting engine down with it.
        hint = (" Ports below 1024 (like 80) need permission: update Fast Dissem with the "
                "installer, which grants it." if port < 1024 else "")
        log.error("Can't open the web UI on port %s: %s.%s", port, problem, hint)
        log.warning("Falling back to port %s – change the port in Settings → Web UI.", DEFAULT_PORT)
        storage.event("error", f"Web UI couldn't use port {port} ({problem}), so it is on port "
                               f"{DEFAULT_PORT} instead.{hint}")
        port = DEFAULT_PORT
    serve_on(port)
    return 0


if __name__ == "__main__":
    sys.exit(main())
