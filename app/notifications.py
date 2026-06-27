"""Optional Discord webhook notifications."""

from __future__ import annotations

import logging
from enum import Enum

import requests

logger = logging.getLogger("twitch247.notifications")


class NotificationEvent(str, Enum):
    STREAM_START = "stream_start"
    VIDEO_CHANGE = "video_change"
    ERROR = "error"
    SERVICE_RESTART = "service_restart"


COLORS = {
    NotificationEvent.STREAM_START: 0x9146FF,
    NotificationEvent.VIDEO_CHANGE: 0x00FF00,
    NotificationEvent.ERROR: 0xFF0000,
    NotificationEvent.SERVICE_RESTART: 0xFFA500,
}

TITLES = {
    NotificationEvent.STREAM_START: "Stream Started",
    NotificationEvent.VIDEO_CHANGE: "Now Playing",
    NotificationEvent.ERROR: "Error",
    NotificationEvent.SERVICE_RESTART: "Service Restarted",
}


class Notifier:
    def __init__(self, webhook_url: str | None, channel: str = "") -> None:
        self.webhook_url = webhook_url
        self.channel = channel

    @property
    def enabled(self) -> bool:
        return bool(self.webhook_url)

    def send(self, event: NotificationEvent, message: str, fields: dict[str, str] | None = None) -> None:
        if not self.enabled:
            return

        embed_fields = [
            {"name": k, "value": v, "inline": True}
            for k, v in (fields or {}).items()
        ]

        payload = {
            "embeds": [
                {
                    "title": TITLES.get(event, event.value),
                    "description": message,
                    "color": COLORS.get(event, 0x808080),
                    "fields": embed_fields,
                    "footer": {"text": f"Twitch247 • {self.channel}"},
                }
            ]
        }

        try:
            resp = requests.post(self.webhook_url, json=payload, timeout=10)
            resp.raise_for_status()
        except requests.RequestException as exc:
            logger.warning("Discord notification failed: %s", exc)

    def stream_start(self, video_title: str, video_id: str) -> None:
        self.send(
            NotificationEvent.STREAM_START,
            f"24/7 stream is live.",
            {"Video": video_title, "ID": video_id},
        )

    def video_change(self, video_title: str, video_id: str, position: float) -> None:
        self.send(
            NotificationEvent.VIDEO_CHANGE,
            f"Switched to next video.",
            {
                "Video": video_title,
                "ID": video_id,
                "Position": f"{position:.0f}s",
            },
        )

    def error(self, message: str) -> None:
        self.send(NotificationEvent.ERROR, message)

    def service_restart(self, video_title: str, position: float) -> None:
        self.send(
            NotificationEvent.SERVICE_RESTART,
            "Service restarted — resuming playback.",
            {"Video": video_title, "Position": f"{position:.0f}s"},
        )
