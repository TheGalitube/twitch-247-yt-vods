"""Configuration loader for Twitch247."""

from __future__ import annotations

import os
import re
from dataclasses import dataclass
from pathlib import Path

from dotenv import load_dotenv


def _find_config_path() -> Path:
    candidates = [
        Path(os.environ.get("TWITCH247_CONFIG", "")),
        Path("/opt/twitch247/config/config.env"),
        Path(__file__).resolve().parent.parent / "config" / "config.env",
    ]
    for path in candidates:
        if path and path.is_file():
            return path
    return candidates[1]


@dataclass(frozen=True)
class Config:
    twitch_stream_key: str
    twitch_channel: str
    youtube_channel_url: str
    log_level: str
    save_interval: int
    sync_interval: int
    seek_tolerance: float
    video_bitrate: str
    maxrate: str
    bufsize: str
    audio_bitrate: str
    dashboard_host: str
    dashboard_port: int
    discord_webhook_url: str | None
    dashboard_secret_key: str | None
    discord_client_id: str | None
    discord_client_secret: str | None
    discord_oauth_redirect_uri: str | None
    dashboard_allowed_discord_user_ids: list[str]
    app_root: Path
    db_path: Path
    log_dir: Path

    @property
    def twitch_rtmp_url(self) -> str:
        return f"rtmp://live.twitch.tv/app/{self.twitch_stream_key}"


def load_config() -> Config:
    config_path = _find_config_path()
    if config_path.is_file():
        load_dotenv(config_path)

    app_root = Path(os.getenv("APP_ROOT", "/opt/twitch247"))

    allowed_users = [
        part.strip().lstrip("@")
        for part in re.split(r"[,\s]+", os.getenv("DASHBOARD_ALLOWED_DISCORD_USER_IDS", ""))
        if part.strip()
    ]

    return Config(
        twitch_stream_key=os.environ["TWITCH_STREAM_KEY"],
        twitch_channel=os.getenv("TWITCH_CHANNEL", ""),
        youtube_channel_url=os.environ["YOUTUBE_CHANNEL_URL"],
        log_level=os.getenv("LOG_LEVEL", "INFO").upper(),
        save_interval=int(os.getenv("SAVE_INTERVAL", "15")),
        sync_interval=int(os.getenv("SYNC_INTERVAL", "3600")),
        seek_tolerance=float(os.getenv("SEEK_TOLERANCE", "5")),
        video_bitrate=os.getenv("VIDEO_BITRATE", "6000k"),
        maxrate=os.getenv("MAXRATE", "6000k"),
        bufsize=os.getenv("BUFSIZE", "12000k"),
        audio_bitrate=os.getenv("AUDIO_BITRATE", "160k"),
        dashboard_host=os.getenv("DASHBOARD_HOST", "127.0.0.1"),
        dashboard_port=int(os.getenv("DASHBOARD_PORT", "8080")),
        discord_webhook_url=os.getenv("DISCORD_WEBHOOK_URL") or None,
        dashboard_secret_key=os.getenv("DASHBOARD_SECRET_KEY") or None,
        discord_client_id=os.getenv("DISCORD_CLIENT_ID") or None,
        discord_client_secret=os.getenv("DISCORD_CLIENT_SECRET") or None,
        discord_oauth_redirect_uri=os.getenv("DISCORD_OAUTH_REDIRECT_URI") or None,
        dashboard_allowed_discord_user_ids=allowed_users,
        app_root=app_root,
        db_path=Path(os.getenv("DB_PATH", str(app_root / "database" / "twitch247.db"))),
        log_dir=Path(os.getenv("LOG_DIR", str(app_root / "logs"))),
    )
