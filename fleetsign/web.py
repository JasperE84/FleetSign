from __future__ import annotations

import os
import secrets
import signal
import threading
import time
from functools import wraps
from pathlib import Path

from flask import (Flask, abort, flash, jsonify, redirect, render_template,
                   request, send_from_directory, session, url_for)
from werkzeug.utils import secure_filename

from .config import AppConfig
from .model import HWDEC_CHOICES, Schedule, is_supported
from .schedule import parse_hhmm
from .store import PlaylistStore
from .sync import FleetTracker, friendly_sync_error, manifest_payload
from .validate import positive_seconds as _positive_seconds

MAX_UPLOAD_BYTES = 4 * 1024**3  # 4 GiB — accept large videos (well over 250 MB)


def _default_restarter() -> None:
    # Respond first, then let systemd (Restart=always) relaunch us in the new role.
    threading.Timer(0.5, lambda: os.kill(os.getpid(), signal.SIGTERM)).start()


def _clock_status() -> dict:
    # The Pi-clock fields the status page polls, shared by master and slave so the
    # format and the clock_ok threshold can't drift between the two app factories.
    from datetime import datetime
    now = datetime.now()
    return {
        "now": now.strftime("%Y-%m-%d %H:%M:%S"),
        "weekday": now.strftime("%A"),
        "tz": now.astimezone().tzname() or "",
        "clock_ok": now.year >= 2024,
    }


def _clamp_hwdec(raw: str) -> str:
    return raw if raw in HWDEC_CHOICES else "auto-copy"


def _fmt_ts(epoch):
    # SyncClient timestamps are raw epoch floats; format them for display. None
    # (never synced / never attempted) and any out-of-range value become None so
    # the template can fall back to 'never'.
    if not epoch:
        return None
    from datetime import datetime
    try:
        return datetime.fromtimestamp(epoch).strftime("%Y-%m-%d %H:%M:%S")
    except (OSError, OverflowError, ValueError):
        return None


def unique_path(media_dir: Path, name: str) -> Path:
    dest = media_dir / name
    stem, suffix = Path(name).stem, Path(name).suffix
    i = 1
    while dest.exists():
        dest = media_dir / f"{stem}-{i}{suffix}"
        i += 1
    return dest


def create_app(store: PlaylistStore, config: AppConfig, controller,
               restarter=None) -> Flask:
    app = Flask(__name__)
    restarter = restarter or _default_restarter
    app.secret_key = config.session_secret
    app.config["MAX_CONTENT_LENGTH"] = MAX_UPLOAD_BYTES  # don't 413 large uploads
    app.config["SESSION_COOKIE_SAMESITE"] = "Lax"  # mitigate cross-site POST

    fleet = FleetTracker()

    def _check_sync_token():
        sent = request.headers.get("X-Sync-Token", "")
        if not config.sync_token or not secrets.compare_digest(sent, config.sync_token):
            abort(403)

    def login_required(f):
        @wraps(f)
        def wrapper(*a, **kw):
            if not config.is_configured():
                return redirect(url_for("setup"))
            if not session.get("authed"):
                return redirect(url_for("login"))
            return f(*a, **kw)
        return wrapper

    @app.route("/setup", methods=["GET", "POST"])
    def setup():
        if config.is_configured() or config.is_slave():
            return redirect(url_for("login"))
        if request.method == "POST":
            if request.form.get("mode") == "join":
                url = request.form.get("master_url", "").strip()
                tok = request.form.get("sync_token", "").strip()
                if not url or not tok:
                    flash("Master address and token are required.")
                else:
                    config.join_master(url, tok)
                    restarter()
                    return "Joining master — the device will restart as a screen.", 200
            else:
                pw = request.form.get("password", "")
                if len(pw) < 4:
                    flash("Password must be at least 4 characters.")
                else:
                    config.set_password(pw)
                    session["authed"] = True
                    return redirect(url_for("index"))
        return render_template("setup.html")

    @app.route("/login", methods=["GET", "POST"])
    def login():
        if config.is_slave():
            return ("This device is a screen (slave) and will restart shortly to "
                    "mirror its master. It manages no content here."), 200
        if not config.is_configured():
            return redirect(url_for("setup"))
        if request.method == "POST":
            if config.check_password(request.form.get("password", "")):
                session["authed"] = True
                return redirect(url_for("index"))
            time.sleep(1.0)
            flash("Wrong password.")
        return render_template("login.html")

    @app.route("/logout")
    def logout():
        session.clear()
        return redirect(url_for("login"))

    @app.route("/")
    @login_required
    def index():
        return render_template("index.html",
                               media=store.list_media(),
                               settings=store.get_settings(),
                               missing=store.missing_files(),
                               screens=fleet.recent(time.time()),
                               sync_token=config.sync_token)

    @app.route("/upload", methods=["POST"])
    @login_required
    def upload():
        for f in request.files.getlist("files"):
            if not f or not f.filename:
                continue
            name = secure_filename(f.filename)
            if not name or not is_supported(name):
                flash(f"Skipped unsupported file: {f.filename}")
                continue
            dest = unique_path(store.media_dir, name)
            f.save(dest)
            store.add_media(dest.name)
        return redirect(url_for("index"))

    @app.route("/media/<item_id>/enable", methods=["POST"])
    @login_required
    def enable(item_id):
        store.set_enabled(item_id, request.form.get("enabled") == "1")
        return redirect(url_for("index"))

    @app.route("/media/<item_id>/duration", methods=["POST"])
    @login_required
    def duration(item_id):
        raw = request.form.get("duration", "").strip()
        try:
            value = _positive_seconds(raw) if raw else None
        except ValueError:
            flash("Duration must be a positive number of seconds.")
            return redirect(url_for("index"))
        store.set_duration(item_id, value)
        return redirect(url_for("index"))

    @app.route("/media/<item_id>/schedule", methods=["POST"])
    @login_required
    def schedule(item_id):
        if request.form.get("scheduled") != "1":
            store.set_schedule(item_id, None)
        else:
            days = [int(d) for d in request.form.getlist("days") if d.isdigit() and int(d) <= 6]
            start = request.form.get("start", "00:00")
            end = request.form.get("end", "23:59")
            try:
                parse_hhmm(start)
                parse_hhmm(end)
            except ValueError:
                flash("Schedule times must be in HH:MM format.")
                return redirect(url_for("index"))
            store.set_schedule(item_id, Schedule(days=days, start=start, end=end))
        return redirect(url_for("index"))

    @app.route("/media/<item_id>/reorder", methods=["POST"])
    @login_required
    def reorder(item_id):
        store.reorder(item_id, request.form.get("direction", "up"))
        return redirect(url_for("index"))

    @app.route("/media/<item_id>/delete", methods=["POST"])
    @login_required
    def delete(item_id):
        store.remove_media(item_id)
        return redirect(url_for("index"))

    @app.route("/media-file/<path:name>")
    @login_required
    def media_file(name):
        return send_from_directory(store.media_dir, name)

    @app.route("/sync/manifest")
    def sync_manifest():
        _check_sync_token()
        fleet.record(request.remote_addr or "?", time.time())
        payload = manifest_payload(store)
        # The UI password (a hash) rides along so slaves can require the same
        # login. This human-facing credential is distinct from the sync token
        # that guards this endpoint.
        payload["password_hash"] = config.password_hash
        return jsonify(payload)

    @app.route("/sync/media/<path:name>")
    def sync_media(name):
        _check_sync_token()
        fleet.record(request.remote_addr or "?", time.time())
        return send_from_directory(store.media_dir, name)

    @app.route("/settings", methods=["POST"])
    @login_required
    def settings():
        try:
            duration = _positive_seconds(request.form.get("default_image_duration", "20"))
        except ValueError:
            flash("Default image seconds must be a positive number.")
            return redirect(url_for("index"))
        hwdec = _clamp_hwdec(request.form.get("hwdec", "auto-copy"))
        old = store.get_settings()
        store.set_settings(duration, request.form.get("muted") == "1", hwdec)
        if hwdec != old.hwdec:
            controller.restart_playback()  # relaunch mpv with the new decoder
            flash("Video decoder changed — playback restarted.")
        return redirect(url_for("index"))

    @app.route("/control/restart-playback", methods=["POST"])
    @login_required
    def restart_playback():
        controller.restart_playback()
        flash("Playback restarted.")
        return redirect(url_for("index"))

    @app.route("/control/blank", methods=["POST"])
    @login_required
    def blank():
        controller.set_blank(request.form.get("blank") == "1")
        return redirect(url_for("index"))

    @app.route("/control/maintenance", methods=["POST"])
    @login_required
    def maintenance():
        controller.set_maintenance(request.form.get("on") == "1")
        return redirect(url_for("index"))

    @app.route("/status")
    @login_required
    def status():
        return jsonify({
            **_clock_status(),
            "maintenance": controller.is_maintenance(),
            "blank": controller.is_blank(),
        })

    @app.route("/password", methods=["POST"])
    @login_required
    def password():
        pw = request.form.get("password", "")
        if len(pw) >= 4:
            config.set_password(pw)
            flash("Password updated.")
        else:
            flash("Password too short.")
        return redirect(url_for("index"))

    @app.route("/sync-token", methods=["POST"])
    @login_required
    def sync_token_update():
        tok = request.form.get("sync_token", "").strip()
        if tok:
            config.set_sync_token(tok)
            flash("Sync token updated.")
        else:
            flash("Token cannot be empty.")
        return redirect(url_for("index"))

    @app.route("/join-master", methods=["POST"])
    @login_required
    def join_master():
        url = request.form.get("master_url", "").strip()
        tok = request.form.get("sync_token", "").strip()
        if not url or not tok:
            flash("Master address and token are required.")
            return redirect(url_for("index"))
        config.join_master(url, tok)
        restarter()
        return "Joining master — the device will restart as a screen.", 200

    return app


def create_slave_app(store: PlaylistStore, config: AppConfig, controller,
                     sync_client, restarter=None) -> Flask:
    restarter = restarter or _default_restarter
    app = Flask(__name__)
    app.secret_key = config.session_secret
    app.config["SESSION_COOKIE_SAMESITE"] = "Lax"

    def _render_waiting():
        # Built once and shared by both render sites (login_required + login) so
        # the pre-sync page always surfaces WHY it can't connect (refused/timeout/
        # bad token) instead of a blank "waiting", with the raw detail kept too.
        return render_template(
            "slave_waiting.html",
            master_url=config.master_url,
            last_error=sync_client.last_error,
            last_error_friendly=friendly_sync_error(sync_client.last_error),
            last_attempt=_fmt_ts(sync_client.last_attempt))

    def login_required(f):
        @wraps(f)
        def wrapper(*a, **kw):
            # The UI password is synced from the master; until the first sync the
            # slave has none, so show the waiting page (which itself offers the
            # recovery controls below, but no content and no sync token).
            if not config.is_configured():
                return _render_waiting()
            if not session.get("authed"):
                return redirect(url_for("login"))
            return f(*a, **kw)
        return wrapper

    def recovery_or_login(f):
        # Reachable BEFORE the first sync (no password yet) so a slave joined
        # with a wrong master URL/token can be repaired or demoted from its own
        # UI instead of needing SSH/SD-card access. Once a password is synced
        # these revert to requiring login. Pre-sync the device holds no content
        # or secrets, and /setup-join is already open, so this is consistent.
        @wraps(f)
        def wrapper(*a, **kw):
            if config.is_configured() and not session.get("authed"):
                return redirect(url_for("login"))
            return f(*a, **kw)
        return wrapper

    @app.route("/login", methods=["GET", "POST"])
    def login():
        if not config.is_configured():
            return _render_waiting()
        if request.method == "POST":
            if config.check_password(request.form.get("password", "")):
                session["authed"] = True
                return redirect(url_for("index"))
            time.sleep(1.0)
            flash("Wrong password.")
        return render_template("login.html")

    @app.route("/logout")
    def logout():
        session.clear()
        return redirect(url_for("login"))

    @app.route("/")
    @login_required
    def index():
        return render_template("slave_status.html",
                               master_url=config.master_url,
                               sync_token=config.sync_token,
                               settings=store.get_settings(),
                               item_count=len(store.list_media()),
                               last_sync=_fmt_ts(sync_client.last_sync),
                               last_attempt=_fmt_ts(sync_client.last_attempt),
                               last_error=sync_client.last_error,
                               last_error_friendly=friendly_sync_error(
                                   sync_client.last_error))

    @app.route("/local/hwdec", methods=["POST"])
    @login_required
    def local_hwdec():
        hwdec = _clamp_hwdec(request.form.get("hwdec", "auto-copy"))
        s = store.get_settings()
        store.set_settings(s.default_image_duration, s.muted, hwdec)
        controller.restart_playback()
        flash("Decoder changed — playback restarted.")
        return redirect(url_for("index"))

    @app.route("/local/connection", methods=["POST"])
    @recovery_or_login
    def local_connection():
        url = request.form.get("master_url", "").strip()
        tok = request.form.get("sync_token", "").strip()
        if url and tok:
            config.join_master(url, tok)  # still a slave; SyncClient re-reads
            flash("Master connection updated — applies on the next sync.")
        else:
            flash("Master address and token are required.")
        return redirect(url_for("index"))

    @app.route("/become-master", methods=["POST"])
    @recovery_or_login
    def become_master():
        config.become_master()
        restarter()
        return ("Becoming master — the device will restart. "
                "Browse to it to manage content."), 200

    @app.route("/status")
    @login_required
    def status():
        return jsonify({
            **_clock_status(),
            "item_count": len(store.list_media()),
            "last_sync": sync_client.last_sync,
            "last_sync_text": _fmt_ts(sync_client.last_sync),
            "last_error": sync_client.last_error,
            "last_error_friendly": friendly_sync_error(sync_client.last_error),
        })

    return app
