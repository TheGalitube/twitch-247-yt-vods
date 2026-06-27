"""Flask monitoring dashboard for Twitch247."""

from __future__ import annotations

import secrets
import sys
from datetime import datetime, timezone, timedelta
from pathlib import Path
from urllib.parse import urlencode

import requests
from flask import Flask, jsonify, redirect, render_template_string, request, session, url_for

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

from app.config import load_config
from app.database import Database

DASHBOARD_HTML = """<!DOCTYPE html>
<html lang="en">
<head>
  <meta charset="UTF-8">
  <meta name="viewport" content="width=device-width, initial-scale=1.0">
  <meta http-equiv="refresh" content="10">
  <title>Twitch247 Dashboard</title>
  <style>
    :root {
      --bg: #0e0e10;
      --card: #18181b;
      --border: #2d2d35;
      --text: #efeff1;
      --muted: #adadb8;
      --accent: #9146ff;
      --green: #00f593;
      --red: #eb0400;
      --orange: #ffa500;
    }
    * { box-sizing: border-box; margin: 0; padding: 0; }
    body {
      font-family: 'Segoe UI', system-ui, sans-serif;
      background: var(--bg);
      color: var(--text);
      min-height: 100vh;
      padding: 2rem;
    }
    h1 { color: var(--accent); margin-bottom: 0.25rem; }
    .subtitle { color: var(--muted); margin-bottom: 2rem; }
    .grid {
      display: grid;
      grid-template-columns: repeat(auto-fit, minmax(280px, 1fr));
      gap: 1rem;
      margin-bottom: 2rem;
    }
    .card {
      background: var(--card);
      border: 1px solid var(--border);
      border-radius: 8px;
      padding: 1.25rem;
    }
    .card h2 {
      font-size: 0.75rem;
      text-transform: uppercase;
      letter-spacing: 0.08em;
      color: var(--muted);
      margin-bottom: 0.5rem;
    }
    .card .value {
      font-size: 1.5rem;
      font-weight: 600;
      word-break: break-word;
    }
    .status-live { color: var(--green); }
    .status-offline { color: var(--red); }
    .progress-bar {
      background: var(--border);
      border-radius: 4px;
      height: 8px;
      margin-top: 0.75rem;
      overflow: hidden;
    }
    .progress-fill {
      background: var(--accent);
      height: 100%;
      border-radius: 4px;
      transition: width 0.3s;
    }
    table {
      width: 100%;
      border-collapse: collapse;
      font-size: 0.875rem;
    }
    th, td {
      text-align: left;
      padding: 0.6rem 0.75rem;
      border-bottom: 1px solid var(--border);
    }
    th { color: var(--muted); font-weight: 500; }
    .badge {
      display: inline-block;
      padding: 0.15rem 0.5rem;
      border-radius: 4px;
      font-size: 0.75rem;
      font-weight: 600;
    }
    .badge-unplayed { background: #1f3a5f; color: #58a6ff; }
    .badge-playing { background: #1a3d2e; color: var(--green); }
    .badge-played { background: #3d2a1a; color: var(--orange); }
    .error-box {
      background: #2d1515;
      border: 1px solid var(--red);
      border-radius: 8px;
      padding: 1rem;
      color: #ffb4b4;
      margin-bottom: 2rem;
    }
    footer { color: var(--muted); font-size: 0.8rem; margin-top: 2rem; }
  </style>
</head>
  <body>
    <h1>Twitch247</h1>
    <p class="subtitle">24/7 YouTube → Twitch Stream Monitor • {{ channel }}</p>
    {% if authenticated_user %}
    <p class="subtitle" style="margin-top:-1rem">
      Signed in as {{ authenticated_user.display_name }} (@{{ authenticated_user.login }}) •
      <a href="/logout" style="color:var(--accent)">Log out</a>
    </p>
    {% endif %}

  {% if last_error %}
  <div class="error-box">
    <strong>Last Error</strong> ({{ last_error_at }})<br>{{ last_error }}
  </div>
  {% endif %}

  <div class="grid">
    <div class="card">
      <h2>Stream Status</h2>
      <p style="color:var(--muted);font-size:0.8rem;margin-bottom:0.35rem">Current Twitch live state</p>
      <div class="value {% if is_streaming %}status-live{% else %}status-offline{% endif %}">
        {% if is_streaming %}● LIVE{% else %}○ OFFLINE{% endif %}
      </div>
    </div>
    <div class="card">
      <h2>Stream Uptime</h2>
      <div class="value">{{ stream_uptime }}</div>
      <p style="color:var(--muted);font-size:0.8rem;margin-top:0.5rem">
        Current Twitch session since the last service resume
      </p>
      <p style="color:var(--muted);font-size:0.8rem;margin-top:0.25rem">Service runtime: {{ service_uptime }}</p>
    </div>
    <div class="card">
      <h2>Current Video</h2>
      <p style="color:var(--muted);font-size:0.8rem;margin-bottom:0.35rem">Video currently selected for playback</p>
      <div class="value" style="font-size:1rem">{{ current_title or '—' }}</div>
      {% if current_video_id %}
      <p style="color:var(--muted);font-size:0.8rem;margin-top:0.5rem">{{ current_video_id }}</p>
      {% endif %}
    </div>
    <div class="card">
      <h2>Position</h2>
      <p style="color:var(--muted);font-size:0.8rem;margin-bottom:0.35rem">Playback position inside the current VOD</p>
      <div class="value">{{ position_display }}</div>
      {% if progress_pct is not none %}
      <div class="progress-bar"><div class="progress-fill" style="width:{{ progress_pct }}%"></div></div>
      {% endif %}
    </div>
  </div>

  <div class="grid">
    <div class="card">
      <h2>Total Videos</h2>
      <p style="color:var(--muted);font-size:0.8rem;margin-bottom:0.35rem">Known videos in the local queue</p>
      <div class="value">{{ stats.total_videos }}</div>
    </div>
    <div class="card">
      <h2>Unplayed</h2>
      <p style="color:var(--muted);font-size:0.8rem;margin-bottom:0.35rem">Queued but not yet completed</p>
      <div class="value" style="color:#58a6ff">{{ stats.unplayed }}</div>
    </div>
    <div class="card">
      <h2>Played</h2>
      <p style="color:var(--muted);font-size:0.8rem;margin-bottom:0.35rem">Already completed at least once</p>
      <div class="value" style="color:var(--orange)">{{ stats.played }}</div>
    </div>
    <div class="card">
      <h2>Last Save</h2>
      <p style="color:var(--muted);font-size:0.8rem;margin-bottom:0.35rem">Last persisted playback checkpoint</p>
      <div class="value" style="font-size:1rem">{{ last_save_at or '—' }}</div>
    </div>
  </div>

  <div class="card">
    <h2>Video Queue (recent)</h2>
    <table>
      <thead>
        <tr><th>Title</th><th>Status</th><th>Duration</th><th>Upload</th></tr>
      </thead>
      <tbody>
        {% for v in videos %}
        <tr>
          <td>{{ v.title[:60] }}{% if v.title|length > 60 %}…{% endif %}</td>
          <td><span class="badge badge-{{ v.played_status }}">{{ v.played_status }}</span></td>
          <td>{{ v.duration_display }}</td>
          <td>{{ v.upload_date or '—' }}</td>
        </tr>
        {% endfor %}
      </tbody>
    </table>
  </div>

  <footer>Auto-refreshes every 10s • <a href="/api/status" style="color:var(--accent)">JSON API</a></footer>
</body>
</html>"""


AUTH_HTML = """<!DOCTYPE html>
<html lang="en">
<head>
  <meta charset="UTF-8">
  <meta name="viewport" content="width=device-width, initial-scale=1.0">
  <title>Twitch247 Access</title>
  <style>
    :root {
      --bg: #0e0e10;
      --card: #18181b;
      --border: #2d2d35;
      --text: #efeff1;
      --muted: #adadb8;
      --accent: #9146ff;
      --red: #eb0400;
      --green: #00f593;
    }
    * { box-sizing: border-box; margin: 0; padding: 0; }
    body {
      font-family: 'Segoe UI', system-ui, sans-serif;
      background: radial-gradient(circle at top, #1a1a22 0%, var(--bg) 60%);
      color: var(--text);
      min-height: 100vh;
      display: grid;
      place-items: center;
      padding: 2rem;
    }
    .panel {
      width: min(680px, 100%);
      background: var(--card);
      border: 1px solid var(--border);
      border-radius: 16px;
      padding: 2rem;
      box-shadow: 0 18px 60px rgba(0, 0, 0, 0.35);
    }
    h1 { color: var(--accent); margin-bottom: 0.5rem; }
    p { color: var(--muted); line-height: 1.5; }
    .notice {
      margin-top: 1.25rem;
      padding: 1rem;
      border-radius: 12px;
      border: 1px solid var(--border);
      background: #111114;
    }
    .notice strong { color: var(--text); }
    .allowed {
      margin-top: 1rem;
      display: flex;
      flex-wrap: wrap;
      gap: 0.5rem;
    }
    .chip {
      padding: 0.35rem 0.65rem;
      border-radius: 999px;
      background: rgba(145, 70, 255, 0.15);
      color: var(--text);
      border: 1px solid rgba(145, 70, 255, 0.35);
      font-size: 0.9rem;
    }
    .button {
      display: inline-block;
      margin-top: 1.5rem;
      padding: 0.8rem 1.1rem;
      border-radius: 10px;
      background: var(--accent);
      color: white;
      text-decoration: none;
      font-weight: 700;
    }
    .button.secondary {
      background: transparent;
      border: 1px solid var(--border);
      color: var(--text);
      margin-left: 0.75rem;
    }
    .error {
      color: #ffb4b4;
      border-color: rgba(235, 4, 0, 0.5);
      background: rgba(45, 21, 21, 0.7);
    }
    .ok {
      color: #d8ffd9;
      border-color: rgba(0, 245, 147, 0.35);
      background: rgba(10, 35, 24, 0.85);
    }
  </style>
</head>
<body>
  <div class="panel">
    <h1>{{ title }}</h1>
    <p>{{ message }}</p>
    <div class="notice {% if kind == 'error' %}error{% elif kind == 'ok' %}ok{% endif %}">
      <strong>{{ notice_title }}</strong>
      <p style="margin-top:0.35rem">{{ notice_body }}</p>
    </div>
    {% if allowed_users %}
    <div class="allowed">
      {% for user in allowed_users %}
      <span class="chip">{{ user }}</span>
      {% endfor %}
    </div>
    {% endif %}
    {% if auth_url %}
    <a class="button" href="{{ auth_url }}">Sign in with Discord</a>
    {% endif %}
    {% if logout_url %}
    <a class="button secondary" href="{{ logout_url }}">Log out</a>
    {% endif %}
  </div>
</body>
</html>"""


def create_app() -> Flask:
    config = load_config()
    schema_path = config.app_root / "database" / "schema.sql"
    if not schema_path.is_file():
        schema_path = Path(__file__).resolve().parent.parent / "database" / "schema.sql"

    db = Database(config.db_path, schema_path)
    flask_app = Flask(__name__)
    flask_app.secret_key = config.dashboard_secret_key or "twitch247-dashboard-dev"
    flask_app.permanent_session_lifetime = timedelta(days=7)
    allowed_user_ids = {user.strip() for user in config.dashboard_allowed_discord_user_ids if user.strip()}

    auth_ready = all(
        [
            config.dashboard_secret_key,
            config.discord_client_id,
            config.discord_client_secret,
            config.discord_oauth_redirect_uri,
            allowed_user_ids,
        ]
    )

    def _normalize_user_id(user_id: str) -> str:
        return user_id.strip()

    def _is_allowed_user_id(user_id: str) -> bool:
        return _normalize_user_id(user_id) in allowed_user_ids

    def _render_auth_page(
        *,
        title: str,
        message: str,
        notice_title: str,
        notice_body: str,
        kind: str = "",
        auth_url: str | None = None,
        logout_url: str | None = None,
        status_code: int = 200,
    ):
        return (
            render_template_string(
                AUTH_HTML,
                title=title,
                message=message,
                notice_title=notice_title,
                notice_body=notice_body,
                kind=kind,
                auth_url=auth_url,
                logout_url=logout_url,
                allowed_users=config.dashboard_allowed_discord_user_ids,
            ),
            status_code,
        )

    def _auth_setup_response():
        return _render_auth_page(
            title="Dashboard Access Not Configured",
            message="Discord login is required, but the dashboard OAuth settings are missing.",
            notice_title="Required config",
            notice_body=(
                "Set DASHBOARD_SECRET_KEY, DISCORD_CLIENT_ID, DISCORD_CLIENT_SECRET, "
                "DISCORD_OAUTH_REDIRECT_URI, and DASHBOARD_ALLOWED_DISCORD_USER_IDS in config.env."
            ),
            kind="error",
            status_code=503,
        )

    def _login_url(next_url: str) -> str:
        state = secrets.token_urlsafe(32)
        session["oauth_state"] = state
        session["oauth_next"] = next_url
        params = {
            "response_type": "code",
            "client_id": config.discord_client_id,
            "redirect_uri": config.discord_oauth_redirect_uri,
            "scope": "identify",
            "state": state,
            "prompt": "consent",
        }
        return f"https://discord.com/oauth2/authorize?{urlencode(params)}"

    def _safe_next_url(raw_next: str | None) -> str:
        if not raw_next:
            return url_for("index")
        if raw_next.startswith("/"):
            return raw_next
        return url_for("index")

    def _current_user():
        user_id = session.get("discord_user_id")
        if not user_id:
            return None
        if not _is_allowed_user_id(user_id):
            session.clear()
            return None
        return {
            "login": session.get("discord_username", user_id),
            "display_name": session.get("discord_display_name", session.get("discord_username", user_id)),
            "user_id": user_id,
        }

    def _exchange_code_for_user(code: str) -> dict[str, str]:
        token_resp = requests.post(
            "https://discord.com/api/oauth2/token",
            data={
                "code": code,
                "grant_type": "authorization_code",
                "redirect_uri": config.discord_oauth_redirect_uri,
            },
            auth=(config.discord_client_id, config.discord_client_secret),
            timeout=15,
        )
        token_resp.raise_for_status()
        access_token = token_resp.json()["access_token"]

        user_resp = requests.get(
            "https://discord.com/api/users/@me",
            headers={
                "Authorization": f"Bearer {access_token}",
            },
            timeout=15,
        )
        user_resp.raise_for_status()
        payload = user_resp.json()
        return {
            "login": payload.get("username") or payload.get("global_name") or payload["id"],
            "display_name": payload.get("global_name") or payload.get("username") or payload["id"],
            "user_id": payload["id"],
        }

    @flask_app.before_request
    def _require_auth():
        if request.endpoint in {"health", "login", "oauth_callback", "logout", "favicon"}:
            return None
        if request.path == "/favicon.ico":
            return None
        if request.endpoint == "static":
            return None
        if not auth_ready:
            if request.path.startswith("/api/"):
                return jsonify({"error": "dashboard auth not configured"}), 503
            return _auth_setup_response()
        if _current_user() is not None:
            return None
        if request.path.startswith("/api/"):
            return jsonify({"error": "unauthorized"}), 401
        return redirect(url_for("login", next=request.path))

    def _format_duration(seconds: int) -> str:
        if seconds <= 0:
            return "—"
        h, rem = divmod(seconds, 3600)
        m, s = divmod(rem, 60)
        if h:
            return f"{h}:{m:02d}:{s:02d}"
        return f"{m}:{s:02d}"

    def _format_uptime(started_at: str | None) -> str:
        if not started_at:
            return "—"
        try:
            start = datetime.strptime(started_at, "%Y-%m-%d %H:%M:%S").replace(
                tzinfo=timezone.utc
            )
            delta = datetime.now(timezone.utc) - start
            days = delta.days
            hours, rem = divmod(delta.seconds, 3600)
            minutes, _ = divmod(rem, 60)
            parts = []
            if days:
                parts.append(f"{days}d")
            parts.append(f"{hours}h {minutes}m")
            return " ".join(parts)
        except ValueError:
            return started_at

    def _build_context() -> dict:
        state = db.get_playback_state()
        stats = db.get_stats()
        videos = db.list_videos(20)
        user = _current_user()

        current_title = None
        current_duration = 0
        if state.current_video_id:
            current = db.get_video(state.current_video_id)
            if current:
                current_title = current.title
                current_duration = current.duration

        position = state.current_position_seconds
        progress_pct = None
        if current_duration > 0:
            progress_pct = min(100, round(position / current_duration * 100, 1))

        return {
            "channel": config.twitch_channel,
            "authenticated_user": user,
            "auth_enabled": auth_ready,
            "is_streaming": state.is_streaming,
            "stream_uptime": _format_uptime(state.stream_started_at if state.is_streaming else None),
            "service_uptime": _format_uptime(state.uptime_started_at),
            "current_title": current_title,
            "current_video_id": state.current_video_id,
            "position_display": _format_duration(int(position)),
            "progress_pct": progress_pct,
            "stats": stats,
            "last_save_at": state.last_save_at,
            "last_error": state.last_error,
            "last_error_at": state.last_error_at,
            "videos": [
                {
                    "title": v.title,
                    "played_status": v.played_status,
                    "duration_display": _format_duration(v.duration),
                    "upload_date": v.upload_date,
                }
                for v in videos
            ],
        }

    @flask_app.route("/")
    def index():
        return render_template_string(DASHBOARD_HTML, **_build_context())

    @flask_app.route("/login")
    def login():
        if not auth_ready:
            return _auth_setup_response()
        current_user = _current_user()
        if current_user:
            return redirect(url_for("index"))
        next_url = _safe_next_url(request.args.get("next"))
        auth_url = _login_url(next_url)
        return _render_auth_page(
            title="Sign in with Discord",
            message="Use one of the approved Discord accounts to view the dashboard.",
            notice_title="Allowed Discord user IDs",
            notice_body=(
                "Only users on the access list can enter. After Discord sign-in, we check the Discord user ID."
            ),
            kind="ok",
            auth_url=auth_url,
            status_code=200,
        )

    @flask_app.route("/oauth/callback")
    def oauth_callback():
        if not auth_ready:
            return _auth_setup_response()

        if request.args.get("error"):
            return _render_auth_page(
                title="Discord sign-in failed",
                message="Discord did not complete the login flow.",
                notice_title="Error",
                notice_body=request.args.get("error_description") or request.args["error"],
                kind="error",
                auth_url=url_for("login"),
                status_code=400,
            )

        expected_state = session.pop("oauth_state", None)
        if not expected_state or request.args.get("state") != expected_state:
            return _render_auth_page(
                title="Invalid login attempt",
                message="The Discord login state did not match.",
                notice_title="Security check failed",
                notice_body="Please start the login process again.",
                kind="error",
                auth_url=url_for("login"),
                status_code=400,
            )

        code = request.args.get("code")
        if not code:
            return _render_auth_page(
                title="Login missing code",
                message="Discord did not return an authorization code.",
                notice_title="Missing code",
                notice_body="Please start the login flow again.",
                kind="error",
                auth_url=url_for("login"),
                status_code=400,
            )

        try:
            user = _exchange_code_for_user(code)
        except (requests.RequestException, KeyError, RuntimeError) as exc:
            return _render_auth_page(
                title="Discord login error",
                message="We could not verify your Discord account.",
                notice_title="Login failed",
                notice_body=str(exc),
                kind="error",
                auth_url=url_for("login"),
                status_code=400,
            )

        if not _is_allowed_user_id(user["user_id"]):
            session.clear()
            return _render_auth_page(
                title="Access denied",
                message="Your Discord account authenticated successfully, but it is not on the dashboard allowlist.",
                notice_title="Not allowed",
                notice_body=f"Signed in as {user['display_name']} (Discord ID {user['user_id']}).",
                kind="error",
                logout_url=url_for("logout"),
                status_code=403,
            )

        next_url = _safe_next_url(session.pop("oauth_next", None))
        session.clear()
        session.permanent = True
        session["discord_username"] = user["login"]
        session["discord_display_name"] = user["display_name"]
        session["discord_user_id"] = user["user_id"]
        session["authenticated_at"] = datetime.now(timezone.utc).isoformat()
        return redirect(next_url)

    @flask_app.route("/logout")
    def logout():
        session.clear()
        return redirect(url_for("login"))

    @flask_app.route("/favicon.ico")
    def favicon():
        return ("", 204)

    @flask_app.route("/api/status")
    def api_status():
        ctx = _build_context()
        return jsonify({
            "streaming": ctx["is_streaming"],
            "uptime": ctx["stream_uptime"],
            "service_uptime": ctx["service_uptime"],
            "current_video_id": ctx["current_video_id"],
            "current_title": ctx["current_title"],
            "position_seconds": db.get_playback_state().current_position_seconds,
            "stats": {
                "total": ctx["stats"].total_videos,
                "unplayed": ctx["stats"].unplayed,
                "playing": ctx["stats"].playing,
                "played": ctx["stats"].played,
            },
            "last_error": ctx["last_error"],
            "last_error_at": ctx["last_error_at"],
            "last_save_at": ctx["last_save_at"],
        })

    @flask_app.route("/health")
    def health():
        return jsonify({"status": "ok"})

    return flask_app


def main() -> None:
    config = load_config()
    app = create_app()
    app.run(
        host=config.dashboard_host,
        port=config.dashboard_port,
        debug=False,
    )


if __name__ == "__main__":
    main()
