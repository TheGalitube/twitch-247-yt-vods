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
  <title>Galitube 24/7 Dashboard</title>
  <style>
    :root {
      color-scheme: dark;
      --bg: #050910;
      --bg-raised: #09111b;
      --card: #101a25;
      --card-hover: #13202c;
      --border: #263646;
      --border-soft: #1d2a37;
      --text: #f1f5f9;
      --muted: #98a6b7;
      --accent: #a855f7;
      --accent-bright: #c084fc;
      --blue: #4ea4ff;
      --green: #18e6a0;
      --red: #ff5d68;
      --orange: #ffad0a;
      --radius: 14px;
      --shadow: 0 18px 45px rgba(0, 0, 0, 0.22);
    }
    * { box-sizing: border-box; margin: 0; padding: 0; }
    html { background: var(--bg); }
    body {
      font-family: Inter, ui-sans-serif, -apple-system, BlinkMacSystemFont, 'Segoe UI', sans-serif;
      background:
        radial-gradient(circle at 50% -10%, rgba(78, 84, 184, 0.18), transparent 34rem),
        linear-gradient(180deg, #070c14 0%, var(--bg) 38%, #060a10 100%);
      color: var(--text);
      min-height: 100vh;
      padding: clamp(1rem, 2.6vw, 2.5rem);
      overflow-x: hidden;
      -webkit-font-smoothing: antialiased;
    }
    a {
      color: var(--accent-bright);
      text-underline-offset: 0.18em;
      text-decoration-color: rgba(192, 132, 252, 0.5);
    }
    a:hover { color: #d8b4fe; }
    .dashboard-shell {
      width: min(100%, 1540px);
      margin: 0 auto;
    }
    .topbar {
      position: relative;
      display: grid;
      grid-template-columns: minmax(0, 1fr) auto minmax(0, 1fr);
      align-items: center;
      min-height: 64px;
      margin-bottom: 1rem;
      padding: 0.55rem 0.7rem;
      background: rgba(11, 19, 29, 0.72);
      border: 1px solid rgba(38, 54, 70, 0.86);
      border-radius: var(--radius);
      box-shadow: 0 14px 38px rgba(0, 0, 0, 0.18);
    }
    .brand {
      grid-column: 2;
      color: var(--accent-bright);
      text-decoration: none;
      line-height: 1;
    }
    .brand:hover { color: #d8b4fe; }
    .brand h1 {
      margin: 0;
      font-size: clamp(1.55rem, 2.3vw, 2rem);
      white-space: nowrap;
    }
    .profile-menu {
      grid-column: 3;
      justify-self: end;
      position: relative;
      z-index: 10;
    }
    .profile-menu summary {
      list-style: none;
      cursor: pointer;
    }
    .profile-menu summary::-webkit-details-marker { display: none; }
    .profile-trigger {
      display: flex;
      align-items: center;
      gap: 0.65rem;
      min-height: 44px;
      padding: 0.3rem 0.45rem 0.3rem 0.35rem;
      border: 1px solid transparent;
      border-radius: 11px;
      transition: background-color 150ms ease, border-color 150ms ease;
    }
    .profile-trigger:hover,
    .profile-menu[open] .profile-trigger {
      background: rgba(168, 85, 247, 0.085);
      border-color: rgba(192, 132, 252, 0.24);
    }
    .profile-avatar {
      position: relative;
      display: grid;
      place-items: center;
      width: 36px;
      height: 36px;
      flex: 0 0 36px;
      overflow: hidden;
      border: 1px solid rgba(192, 132, 252, 0.46);
      border-radius: 50%;
      background: linear-gradient(145deg, #7137b6, #3f237d);
      color: #fff;
      font-size: 0.72rem;
      font-weight: 800;
      letter-spacing: 0.04em;
      box-shadow: 0 0 0 3px rgba(168, 85, 247, 0.08);
    }
    .profile-avatar img {
      position: absolute;
      inset: 0;
      width: 100%;
      height: 100%;
      object-fit: cover;
    }
    .profile-copy {
      min-width: 0;
      text-align: left;
      line-height: 1.25;
    }
    .profile-name {
      display: block;
      max-width: 150px;
      overflow: hidden;
      color: var(--text);
      font-size: 0.78rem;
      font-weight: 700;
      text-overflow: ellipsis;
      white-space: nowrap;
    }
    .profile-handle {
      display: block;
      max-width: 150px;
      overflow: hidden;
      color: var(--muted);
      font-size: 0.67rem;
      text-overflow: ellipsis;
      white-space: nowrap;
    }
    .profile-chevron {
      width: 14px;
      height: 14px;
      color: var(--muted);
      transition: transform 150ms ease;
    }
    .profile-menu[open] .profile-chevron { transform: rotate(180deg); }
    .profile-dropdown {
      position: absolute;
      top: calc(100% + 0.5rem);
      right: 0;
      width: min(240px, calc(100vw - 2rem));
      padding: 0.55rem;
      background: #101a25;
      border: 1px solid var(--border);
      border-radius: 12px;
      box-shadow: 0 20px 55px rgba(0, 0, 0, 0.42);
    }
    .profile-dropdown-meta {
      padding: 0.55rem 0.65rem 0.7rem;
      border-bottom: 1px solid var(--border-soft);
      color: var(--muted);
      font-size: 0.72rem;
      line-height: 1.45;
      word-break: break-word;
    }
    .logout-action {
      display: flex;
      align-items: center;
      gap: 0.55rem;
      margin-top: 0.45rem;
      padding: 0.68rem 0.7rem;
      border-radius: 9px;
      color: #ffc2c7;
      font-size: 0.78rem;
      font-weight: 700;
      text-decoration: none;
    }
    .logout-action:hover {
      background: rgba(255, 93, 104, 0.11);
      color: #ffd7da;
    }
    .logout-action svg { width: 16px; height: 16px; }
    .dashboard-header {
      text-align: center;
      margin: 0 auto clamp(1.5rem, 2.5vw, 2.25rem);
      padding: 0.2rem 1rem 0;
    }
    h1 {
      color: var(--accent-bright);
      font-size: clamp(2rem, 3.5vw, 3rem);
      line-height: 1;
      letter-spacing: -0.045em;
      margin-bottom: 0.7rem;
      text-shadow: 0 0 32px rgba(168, 85, 247, 0.2);
    }
    .subtitle {
      color: var(--muted);
      font-size: 0.94rem;
      line-height: 1.55;
    }
    .grid {
      display: grid;
      gap: 0.9rem;
      margin-bottom: 1rem;
    }
    .operational-grid { grid-template-columns: repeat(5, minmax(0, 1fr)); }
    .summary-grid {
      grid-template-columns: repeat(4, minmax(0, 1fr));
      margin-bottom: 1.25rem;
    }
    .card {
      background:
        linear-gradient(145deg, rgba(18, 30, 42, 0.98), rgba(12, 22, 32, 0.98));
      border: 1px solid var(--border);
      border-radius: var(--radius);
      padding: 1.15rem 1.2rem;
      box-shadow: var(--shadow), inset 0 1px rgba(255, 255, 255, 0.018);
      min-width: 0;
      transition: border-color 160ms ease, background-color 160ms ease;
    }
    .card:hover {
      border-color: #34495e;
    }
    .card h2 {
      font-size: 0.7rem;
      text-transform: uppercase;
      letter-spacing: 0.12em;
      color: #b6c2cf;
      margin-bottom: 0.6rem;
      font-weight: 700;
    }
    .card .value {
      font-size: clamp(1.35rem, 2vw, 1.75rem);
      line-height: 1.12;
      letter-spacing: -0.025em;
      font-weight: 750;
      word-break: break-word;
    }
    .card-copy {
      color: var(--muted);
      font-size: 0.78rem;
      line-height: 1.45;
    }
    .card-copy.before-value { margin-bottom: 0.45rem; }
    .card-copy.after-value { margin-top: 0.55rem; }
    .card-copy.compact { margin-top: 0.25rem; }
    .value.value-small { font-size: 0.96rem; line-height: 1.3; letter-spacing: 0; }
    .value.value-blue { color: var(--blue); }
    .value.value-orange { color: var(--orange); }
    .status-live { color: var(--green); }
    .status-offline { color: var(--red); }
    .status-unknown { color: var(--orange); }
    .progress-bar {
      background: #263241;
      border-radius: 999px;
      height: 7px;
      margin-top: 0.85rem;
      overflow: hidden;
    }
    .progress-fill {
      background: linear-gradient(90deg, #8b5cf6, var(--accent-bright));
      height: 100%;
      border-radius: inherit;
      transition: width 0.3s;
      box-shadow: 0 0 16px rgba(168, 85, 247, 0.35);
    }
    .queue-card {
      padding: 1.15rem 1.2rem 0.9rem;
      overflow: hidden;
    }
    .table-scroll {
      width: 100%;
      overflow-x: auto;
      overscroll-behavior-inline: contain;
      scrollbar-width: thin;
      scrollbar-color: #526173 #111a24;
      border: 1px solid var(--border-soft);
      border-radius: 10px;
      background: rgba(5, 11, 18, 0.32);
    }
    table {
      width: 100%;
      border-collapse: collapse;
      font-size: 0.82rem;
    }
    th, td {
      text-align: left;
      padding: 0.62rem 0.78rem;
      border-bottom: 1px solid var(--border-soft);
    }
    th {
      color: #a9b6c5;
      font-size: 0.72rem;
      letter-spacing: 0.055em;
      font-weight: 650;
      background: rgba(15, 25, 36, 0.94);
    }
    tbody tr { transition: background-color 140ms ease; }
    tbody tr:hover { background: rgba(78, 164, 255, 0.045); }
    tbody tr:last-child td { border-bottom: 0; }
    th:first-child, td:first-child { width: 66%; }
    th:not(:first-child), td:not(:first-child) { white-space: nowrap; }
    .badge {
      display: inline-block;
      padding: 0.22rem 0.55rem;
      border-radius: 6px;
      font-size: 0.7rem;
      line-height: 1;
      font-weight: 700;
      letter-spacing: 0.015em;
    }
    .badge-unplayed { background: #15395e; color: #72b7ff; }
    .badge-playing { background: #0d4937; color: #2df0ad; }
    .badge-played { background: #4b3108; color: #ffbd3c; }
    .error-box {
      background: rgba(67, 22, 29, 0.88);
      border: 1px solid rgba(255, 93, 104, 0.55);
      border-radius: var(--radius);
      padding: 1rem 1.1rem;
      color: #ffc2c7;
      margin-bottom: 1rem;
    }
    footer {
      color: var(--muted);
      font-size: 0.78rem;
      margin-top: 1.25rem;
      text-align: center;
    }
    @media (max-width: 1180px) {
      .operational-grid { grid-template-columns: repeat(3, minmax(0, 1fr)); }
      .summary-grid { grid-template-columns: repeat(2, minmax(0, 1fr)); }
    }
    @media (max-width: 720px) {
      body { padding: 1rem; }
      .topbar { min-height: 58px; padding: 0.45rem 0.5rem; }
      .brand h1 { font-size: 1.35rem; }
      .profile-trigger { gap: 0.35rem; padding-inline: 0.35rem; }
      .profile-copy, .profile-chevron { display: none; }
      .profile-avatar { width: 34px; height: 34px; flex-basis: 34px; }
      .dashboard-header { margin-bottom: 1.35rem; padding-inline: 0.25rem; }
      .subtitle { font-size: 0.82rem; }
      .operational-grid, .summary-grid { grid-template-columns: 1fr; }
      .card { padding: 1rem; border-radius: 12px; }
      .queue-card { padding: 1rem 0.75rem 0.75rem; }
      .queue-card h2 { padding-inline: 0.25rem; }
      .table-scroll { position: relative; }
      table { min-width: 720px; }
      th, td { padding: 0.58rem 0.7rem; }
      footer { margin-top: 1rem; }
    }
    @media (prefers-reduced-motion: reduce) {
      *, *::before, *::after { transition-duration: 0.01ms !important; }
    }
  </style>
</head>
  <body>
  <main class="dashboard-shell">
    <nav class="topbar" aria-label="Dashboard navigation">
      <a class="brand" href="/" aria-label="Galitube 24/7 dashboard home">
        <h1>Galitube 24/7</h1>
      </a>
      {% if authenticated_user %}
      <details class="profile-menu">
        <summary class="profile-trigger" aria-label="Open account menu">
          <span class="profile-avatar" aria-hidden="true">
            <span>{{ authenticated_user.initials }}</span>
            {% if authenticated_user.avatar_url %}
            <img src="{{ authenticated_user.avatar_url }}" alt="" referrerpolicy="no-referrer">
            {% endif %}
          </span>
          <span class="profile-copy">
            <span class="profile-name">{{ authenticated_user.display_name }}</span>
            <span class="profile-handle">@{{ authenticated_user.login }}</span>
          </span>
          <svg class="profile-chevron" viewBox="0 0 20 20" aria-hidden="true">
            <path d="m5.5 7.5 4.5 4.5 4.5-4.5" fill="none" stroke="currentColor" stroke-width="1.8" stroke-linecap="round" stroke-linejoin="round"/>
          </svg>
        </summary>
        <div class="profile-dropdown">
          <div class="profile-dropdown-meta">
            Signed in as {{ authenticated_user.display_name }}<br>
            @{{ authenticated_user.login }}
          </div>
          <a class="logout-action" href="/logout">
            <svg viewBox="0 0 20 20" aria-hidden="true">
              <path d="M8 3.5H5.5a2 2 0 0 0-2 2v9a2 2 0 0 0 2 2H8M12.5 6.5 16 10l-3.5 3.5M7.5 10H16" fill="none" stroke="currentColor" stroke-width="1.7" stroke-linecap="round" stroke-linejoin="round"/>
            </svg>
            Log out
          </a>
        </div>
      </details>
      {% endif %}
    </nav>
    <header class="dashboard-header">
      <p class="subtitle">24/7 YouTube → Twitch Stream Monitor • {{ channel }}</p>
    </header>

  {% if last_error %}
  <div class="error-box">
    <strong>Last Error</strong> ({{ last_error_at }})<br>{{ last_error }}
  </div>
  {% endif %}

  <div class="grid operational-grid">
    <div class="card">
      <h2>Twitch Status</h2>
      <p class="card-copy before-value">Current Twitch live state</p>
      <div class="value {{ twitch_status_class }}">
        {{ twitch_status_label }}
      </div>
      <p class="card-copy after-value">Checked {{ twitch_checked_at or '—' }}</p>
    </div>
    <div class="card">
      <h2>Broadcast Uptime</h2>
      <div class="value">{{ stream_uptime }}</div>
      <p class="card-copy after-value">
        Current Twitch broadcast session
      </p>
      <p class="card-copy compact">Service runtime: {{ service_uptime }}</p>
    </div>
    <div class="card">
      <h2>Local Encoder</h2>
      <p class="card-copy before-value">Local playback/FFmpeg state</p>
      <div class="value {% if local_streaming %}status-live{% else %}status-offline{% endif %}">
        {% if local_streaming %}● RUNNING{% else %}○ STOPPED{% endif %}
      </div>
    </div>
    <div class="card">
      <h2>Current Video</h2>
      <p class="card-copy before-value">Video currently selected for playback</p>
      <div class="value value-small">{{ current_title or '—' }}</div>
      {% if current_video_id %}
      <p class="card-copy after-value">{{ current_video_id }}</p>
      {% endif %}
    </div>
    <div class="card">
      <h2>Position</h2>
      <p class="card-copy before-value">Playback position inside the current VOD</p>
      <div class="value">{{ position_display }}</div>
      {% if progress_pct is not none %}
      <div class="progress-bar"><div class="progress-fill" style="width:{{ progress_pct }}%"></div></div>
      {% endif %}
    </div>
  </div>

  <div class="grid summary-grid">
    <div class="card">
      <h2>Total Videos</h2>
      <p class="card-copy before-value">Known videos in the local queue</p>
      <div class="value">{{ stats.total_videos }}</div>
    </div>
    <div class="card">
      <h2>Unplayed</h2>
      <p class="card-copy before-value">Queued but not yet completed</p>
      <div class="value value-blue">{{ stats.unplayed }}</div>
    </div>
    <div class="card">
      <h2>Played</h2>
      <p class="card-copy before-value">Already completed at least once</p>
      <div class="value value-orange">{{ stats.played }}</div>
    </div>
    <div class="card">
      <h2>Last Save</h2>
      <p class="card-copy before-value">Last persisted playback checkpoint</p>
      <div class="value value-small">{{ last_save_at or '—' }}</div>
    </div>
  </div>

  <div class="card queue-card">
    <h2>Video Queue ({{ videos|length }} total)</h2>
    <div class="table-scroll" tabindex="0" aria-label="Scrollable video queue">
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
  </div>

  <footer>Auto-refreshes every 10s • <a href="/api/status">JSON API</a></footer>
  </main>
</body>
</html>"""


AUTH_HTML = """<!DOCTYPE html>
<html lang="en">
<head>
  <meta charset="UTF-8">
  <meta name="viewport" content="width=device-width, initial-scale=1.0">
  <title>Galitube 24/7 Access</title>
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
    twitch_status_cache: dict[str, object] = {
        "expires_at": datetime.min.replace(tzinfo=timezone.utc),
        "state": "unknown",
        "started_at": None,
        "checked_at": None,
    }

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
        login = session.get("discord_username", user_id)
        display_name = session.get("discord_display_name", login)
        name_parts = [part for part in str(display_name).split() if part]
        initials = "".join(part[0] for part in name_parts[:2]).upper() or str(login)[:2].upper()
        return {
            "login": login,
            "display_name": display_name,
            "user_id": user_id,
            "avatar_url": session.get("discord_avatar_url"),
            "initials": initials,
        }

    def _exchange_code_for_user(code: str) -> dict[str, str | None]:
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
        avatar_hash = payload.get("avatar")
        avatar_url = (
            f"https://cdn.discordapp.com/avatars/{payload['id']}/{avatar_hash}.png?size=128"
            if avatar_hash
            else None
        )
        return {
            "login": payload.get("username") or payload.get("global_name") or payload["id"],
            "display_name": payload.get("global_name") or payload.get("username") or payload["id"],
            "user_id": payload["id"],
            "avatar_url": avatar_url,
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
            if started_at.endswith("Z") or "T" in started_at:
                start = datetime.fromisoformat(started_at.replace("Z", "+00:00"))
            else:
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

    def _format_checked_at(checked_at: datetime | None) -> str | None:
        if not checked_at:
            return None
        return checked_at.strftime("%Y-%m-%d %H:%M:%S UTC")

    def _get_twitch_live_state() -> dict[str, object]:
        now = datetime.now(timezone.utc)
        if now < twitch_status_cache["expires_at"]:
            return twitch_status_cache

        state = "unknown"
        started_at = None
        channel = config.twitch_channel.strip().lstrip("@")

        if channel:
            payload = {
                "query": (
                    "query($login:String!){"
                    "user(login:$login){login stream{type createdAt}}"
                    "}"
                ),
                "variables": {"login": channel},
            }
            try:
                response = requests.post(
                    "https://gql.twitch.tv/gql",
                    headers={
                        "Client-ID": "kimne78kx3ncx6brgo4mv6wki5h1ko",
                        "Content-Type": "application/json",
                    },
                    json=payload,
                    timeout=3,
                )
                response.raise_for_status()
                stream = (
                    ((response.json().get("data") or {}).get("user") or {}).get("stream")
                )
                if stream and stream.get("type") == "live":
                    state = "live"
                    started_at = stream.get("createdAt")
                else:
                    state = "offline"
            except (requests.RequestException, ValueError, KeyError, TypeError):
                state = "unknown"

        twitch_status_cache.update(
            {
                "expires_at": now + timedelta(seconds=8),
                "state": state,
                "started_at": started_at,
                "checked_at": now,
            }
        )
        return twitch_status_cache

    def _build_context() -> dict:
        state = db.get_playback_state()
        stats = db.get_stats()
        videos = db.list_videos(None)
        user = _current_user()
        twitch_state = _get_twitch_live_state()
        twitch_live_state = str(twitch_state["state"])
        twitch_started_at = (
            str(twitch_state["started_at"]) if twitch_state["started_at"] else None
        )
        twitch_checked_at = (
            twitch_state["checked_at"]
            if isinstance(twitch_state["checked_at"], datetime)
            else None
        )

        if twitch_live_state == "live":
            twitch_status_label = "● LIVE"
            twitch_status_class = "status-live"
        elif twitch_live_state == "offline":
            twitch_status_label = "○ OFFLINE"
            twitch_status_class = "status-offline"
        else:
            twitch_status_label = "? UNKNOWN"
            twitch_status_class = "status-unknown"

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
            "is_streaming": twitch_live_state == "live",
            "local_streaming": state.is_streaming,
            "twitch_live_state": twitch_live_state,
            "twitch_status_label": twitch_status_label,
            "twitch_status_class": twitch_status_class,
            "twitch_checked_at": _format_checked_at(twitch_checked_at),
            "stream_uptime": _format_uptime(twitch_started_at if twitch_live_state == "live" else None),
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
        if user["avatar_url"]:
            session["discord_avatar_url"] = user["avatar_url"]
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
            "twitch_live_state": ctx["twitch_live_state"],
            "twitch_checked_at": ctx["twitch_checked_at"],
            "local_streaming": ctx["local_streaming"],
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
