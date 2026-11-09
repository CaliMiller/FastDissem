"""Web UI for settings, previews and recent activity (Flask)."""

from __future__ import annotations

import hmac
import re
import logging
import secrets
from datetime import datetime, timezone
from functools import wraps

from markupsafe import Markup
from flask import (Flask, abort, flash, redirect, render_template, request, session,
                   url_for)

from .. import __version__
from ..engine import Engine, build_publishers, build_sources
from ..plugins import PUBLISHERS, SOURCES, Field, load_plugins
from ..text import parse_tag_list

log = logging.getLogger(__name__)

GENERAL_FIELDS = [
    Field("dry_run", "Dry run (compose posts but don't send them)", "bool", default=True,
          help="Turn off when your previews look right. Items handled during a dry run are not sent later."),
    Field("timezone", "Time zone for alert times", default="America/Indiana/Indianapolis"),
    Field("max_attempts", "Retries before giving up on a post", "int", default=5),
    Field("log_level", "Log level", "select", default="INFO", options=["DEBUG", "INFO", "WARNING"]),
]
WEB_FIELDS = [
    Field("password", "Web UI password", "password", secret=True, default="",
          help="Required whenever the UI is reachable from other computers."),
    Field("host", "Listen address", default="127.0.0.1",
          help="127.0.0.1 = this computer only. 0.0.0.0 = your network (set a password!). Restart to apply."),
    Field("port", "Port", "int", default=8787),
]
SOURCE_COMMON = [
    Field("interval_seconds", "Check every (seconds)", "int", default=300),
    Field("backfill", "Post items that already exist when a channel/area is first added", "bool",
          default=False),
]


def _read_field(f: Field, prefix: str, form, old):
    name = f"{prefix}.{f.key}"
    if f.type == "bool":
        return name in form
    if f.type == "rows":
        idx = sorted({int(k[len(name) + 1:].split(".")[0]) for k in form
                      if k.startswith(name + ".") and k[len(name) + 1:].split(".")[0].isdigit()})
        rows = []
        for i in idx:
            row = {c.key: _read_field(c, f"{name}.{i}", form, None) for c in f.columns}
            if any(v for k, v in row.items() if k != "enabled" and isinstance(v, str) and v.strip()):
                rows.append(row)
        return rows
    raw = form.get(name, "")
    if f.secret:
        if form.get(f"__clear__{name}"):
            return ""
        return raw if raw.strip() else (old if old is not None else "")
    if f.type == "int":
        try:
            return int(raw)
        except ValueError:
            return old if old is not None else f.default
    if f.type == "tags":
        return parse_tag_list(raw)
    if f.type == "textarea":
        return raw.replace("\r\n", "\n")
    return raw.strip()


def _read_section(fields, prefix, form, old: dict) -> dict:
    out = dict(old)
    for f in fields:
        out[f.key] = _read_field(f, prefix, form, old.get(f.key))
    return out


def create_app(config, storage, engine: Engine) -> Flask:
    load_plugins()
    app = Flask(__name__)
    app.secret_key = config.get()["web"]["secret_key"]
    app.config.update(SESSION_COOKIE_HTTPONLY=True, SESSION_COOKIE_SAMESITE="Lax")

    def needs_login():
        return bool(config.get()["web"].get("password"))

    def login_required(fn):
        @wraps(fn)
        def wrapper(*a, **kw):
            if needs_login() and not session.get("authed"):
                return redirect(url_for("login", next=request.path))
            return fn(*a, **kw)
        return wrapper

    @app.before_request
    def csrf():
        if "csrf" not in session:
            session["csrf"] = secrets.token_hex(16)
        if request.method == "POST":
            tok = request.form.get("_csrf", "")
            if not hmac.compare_digest(tok, session["csrf"]):
                abort(400, "Form expired – reload the page and try again.")

    @app.context_processor
    def inject():
        cfg = config.get()
        return {"csrf": session.get("csrf", ""), "version": __version__, "cfg": cfg,
                "dry_run": engine.dry_run if engine.cfg else cfg["general"].get("dry_run"),
                "needs_login": needs_login()}

    @app.template_filter("regex_tags")
    def regex_tags(s):
        return Markup(re.sub(r"(?<![\w&])(#\w+)", r'<span class="tag">\1</span>', str(s)))

    @app.template_filter("ago")
    def ago(ts):
        if not ts:
            return "never"
        if isinstance(ts, (int, float)):
            dt = datetime.fromtimestamp(ts, timezone.utc)
        else:
            dt = datetime.fromisoformat(ts)
        s = int((datetime.now(timezone.utc) - dt).total_seconds())
        if s < 0:
            s = -s
            return f"in {s // 60} min" if s >= 60 else f"in {s}s"
        if s < 60:
            return f"{s}s ago"
        if s < 3600:
            return f"{s // 60} min ago"
        if s < 86400:
            return f"{s // 3600} h ago"
        return f"{s // 86400} d ago"

    # ---------------------------------------------------------------- auth
    @app.route("/login", methods=["GET", "POST"])
    def login():
        if request.method == "POST":
            pw = config.get()["web"].get("password", "")
            if pw and hmac.compare_digest(request.form.get("password", ""), pw):
                session["authed"] = True
                nxt = request.args.get("next", "/")
                return redirect(nxt if nxt.startswith("/") and not nxt.startswith("//") else "/")
            flash("Wrong password.", "error")
        return render_template("login.html")

    @app.post("/logout")
    def logout():
        session.pop("authed", None)
        return redirect(url_for("login"))

    # ----------------------------------------------------------- dashboard
    @app.get("/")
    @login_required
    def dashboard():
        cfg = config.get()
        srcs = []
        for name, cls in SOURCES.items():
            s = cfg["sources"].get(name, {})
            st = engine.status.get(name, {})
            n = len([r for r in s.get("channels", s.get("areas", [])) if r.get("enabled", True)])
            srcs.append({"name": name, "label": cls.label, "enabled": s.get("enabled"), "st": st, "targets": n})
        pubs = [{"name": n, "label": c.label, "enabled": cfg["publishers"].get(n, {}).get("enabled")}
                for n, c in PUBLISHERS.items()]
        return render_template("dashboard.html", sources=srcs, pubs=pubs,
                               recent=storage.recent(60), events=storage.events(15),
                               counts=storage.counts())

    @app.post("/check")
    @login_required
    def check_now():
        engine.trigger(request.form.get("source") or None)
        flash("Checking now – refresh in a few seconds.", "ok")
        return redirect(url_for("dashboard"))

    @app.post("/forget")
    @login_required
    def forget():
        uid = request.form.get("uid", "")
        storage.forget(uid)
        flash("Forgotten. If it's still current it will be posted on the next check "
              "(unless its channel/area is new).", "ok")
        return redirect(url_for("dashboard"))

    # ------------------------------------------------------------ settings
    def plugin_sections(kind: str):
        reg = SOURCES if kind == "sources" else PUBLISHERS
        return [(n, c) for n, c in reg.items()]

    @app.get("/settings")
    @login_required
    def settings():
        return render_template("settings.html", raw=config.raw(),
                               general_fields=GENERAL_FIELDS, web_fields=WEB_FIELDS,
                               source_common=SOURCE_COMMON,
                               sources=plugin_sections("sources"),
                               publishers=plugin_sections("publishers"))

    @app.post("/settings")
    @login_required
    def save_settings():
        form = request.form
        cfg = config.raw()
        cfg["general"] = _read_section(GENERAL_FIELDS, "general", form, cfg["general"])
        cfg["web"] = _read_section(WEB_FIELDS, "web", form, cfg["web"])
        enabled = Field("enabled", "", "bool")
        for name, cls in SOURCES.items():
            old = cfg["sources"].get(name, {})
            cfg["sources"][name] = _read_section([enabled] + SOURCE_COMMON + cls.fields,
                                                 f"sources.{name}", form, old)
        for name, cls in PUBLISHERS.items():
            old = cfg["publishers"].get(name, {})
            cfg["publishers"][name] = _read_section([enabled] + cls.fields, f"publishers.{name}", form, old)
        # let sources fill derived values (YouTube @handle -> channel id)
        notes = []
        for name, src in build_sources(cfg, only_enabled=False).items():
            try:
                src.prepare(cfg["sources"][name])
            except Exception as e:
                notes.append(f"{src.label}: {e}")
        for row in cfg["sources"].get("youtube", {}).get("channels", []):
            if row.get("channel") and not row.get("channel_id"):
                notes.append(f"Couldn't find the channel ID for {row['channel']} yet – will retry.")
        config.save(cfg)
        logging.getLogger().setLevel(cfg["general"].get("log_level", "INFO"))
        engine.trigger()
        flash("Settings saved." + (" " + " ".join(notes) if notes else ""), "warn" if notes else "ok")
        return redirect(url_for("settings") + (request.form.get("_anchor") or ""))

    @app.post("/test/<name>")
    @login_required
    def test_publisher(name):
        cfg = config.get()
        if name not in PUBLISHERS:
            abort(404)
        try:
            pub = build_publishers(cfg, only_enabled=False)[name]
            flash(f"{pub.label}: {pub.check()}", "ok")
        except Exception as e:
            flash(f"{PUBLISHERS[name].label}: {e}", "error")
        return redirect(url_for("settings") + f"#pub-{name}")

    # ------------------------------------------------------------- preview
    @app.get("/preview")
    @login_required
    def preview():
        return render_template("preview.html", groups=engine.preview())

    return app
