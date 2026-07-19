"""Optional Discord webhook notifications."""

from __future__ import annotations

import json
import logging
from datetime import datetime, timezone
from enum import Enum
from pathlib import Path

import requests

logger = logging.getLogger("twitch247.notifications")


class NotificationEvent(str, Enum):
    STREAM_START = "stream_start"
    VIDEO_CHANGE = "video_change"
    STREAM_HEALTH = "stream_health"
    ERROR = "error"
    SERVICE_RESTART = "service_restart"


COLORS = {
    NotificationEvent.STREAM_START: 0x9146FF,
    NotificationEvent.VIDEO_CHANGE: 0x00FF00,
    NotificationEvent.STREAM_HEALTH: 0x3498DB,
    NotificationEvent.ERROR: 0xFF0000,
    NotificationEvent.SERVICE_RESTART: 0xFFA500,
}

TITLES = {
    NotificationEvent.STREAM_START: "Stream Started",
    NotificationEvent.VIDEO_CHANGE: "Now Playing",
    NotificationEvent.STREAM_HEALTH: "Stream Health",
    NotificationEvent.ERROR: "Error",
    NotificationEvent.SERVICE_RESTART: "Service Restarted",
}


class Notifier:
    STATUS_EVENTS = {
        NotificationEvent.STREAM_START,
        NotificationEvent.VIDEO_CHANGE,
        NotificationEvent.SERVICE_RESTART,
    }

    def __init__(
        self,
        webhook_url: str | None,
        channel: str = "",
        state_path: Path | None = None,
    ) -> None:
        self.webhook_url = webhook_url
        self.channel = channel
        self.state_path = state_path

    @property
    def enabled(self) -> bool:
        return bool(self.webhook_url)

    def send(self, event: NotificationEvent, message: str, fields: dict[str, str] | None = None) -> None:
        if not self.enabled:
            return
        if event in self.STATUS_EVENTS:
            bucket = "status"
        elif event == NotificationEvent.STREAM_HEALTH:
            bucket = "stream_health"
        elif event == NotificationEvent.ERROR:
            bucket = "error"
        else:
            bucket = event.value

        embed_fields = [
            {"name": k, "value": self._truncate(v, 1024), "inline": True}
            for k, v in (fields or {}).items()
        ]
        embed_fields.append(
            {
                "name": "Last update",
                "value": datetime.now(timezone.utc).strftime("%Y-%m-%d %H:%M:%S UTC"),
                "inline": True,
            }
        )

        payload = {
            "embeds": [
                {
                    "title": TITLES.get(event, event.value),
                    "description": self._truncate(message, 4096),
                    "color": COLORS.get(event, 0x808080),
                    "fields": embed_fields,
                    "footer": {"text": f"Twitch247 • {self.channel}"},
                }
            ]
        }

        try:
            self._upsert_message(bucket, payload)
            if bucket == "status" and "error" not in self._load_state():
                self._upsert_message("error", self._empty_error_payload())
            if bucket == "status" and "stream_health" not in self._load_state():
                self._upsert_message("stream_health", self._empty_stream_health_payload())
        except requests.RequestException as exc:
            logger.warning("Discord notification failed: %s", exc)
        except OSError as exc:
            logger.warning("Discord notification state update failed: %s", exc)

    def _upsert_message(self, bucket: str, payload: dict[str, object]) -> None:
        message_id = self._load_state().get(bucket)
        if message_id:
            resp = requests.patch(
                f"{self.webhook_url}/messages/{message_id}",
                json=payload,
                timeout=10,
            )
            if resp.status_code != 404:
                resp.raise_for_status()
                return

            logger.info("Discord %s message %s no longer exists; creating a new one", bucket, message_id)

        resp = requests.post(f"{self.webhook_url}?wait=true", json=payload, timeout=10)
        resp.raise_for_status()
        new_message_id = str(resp.json()["id"])
        state = self._load_state()
        state[bucket] = new_message_id
        self._save_state(state)

    def _load_state(self) -> dict[str, str]:
        if not self.state_path or not self.state_path.is_file():
            return {}
        try:
            data = json.loads(self.state_path.read_text(encoding="utf-8"))
        except (json.JSONDecodeError, OSError) as exc:
            logger.warning("Could not read Discord notification state: %s", exc)
            return {}
        return {
            str(key): str(value)
            for key, value in data.items()
            if isinstance(value, str) and value
        }

    def _save_state(self, state: dict[str, str]) -> None:
        if not self.state_path:
            return
        self.state_path.parent.mkdir(parents=True, exist_ok=True)
        self.state_path.write_text(
            json.dumps(state, indent=2, sort_keys=True) + "\n",
            encoding="utf-8",
        )

    @staticmethod
    def _truncate(value: str, limit: int) -> str:
        if len(value) <= limit:
            return value
        return value[: max(0, limit - 1)] + "…"

    def _empty_error_payload(self) -> dict[str, object]:
        return {
            "embeds": [
                {
                    "title": "Errors",
                    "description": "No active errors.",
                    "color": 0x00AA00,
                    "fields": [
                        {
                            "name": "Last update",
                            "value": datetime.now(timezone.utc).strftime("%Y-%m-%d %H:%M:%S UTC"),
                            "inline": True,
                        }
                    ],
                    "footer": {"text": f"Twitch247 • {self.channel}"},
                }
            ]
        }

    def _empty_stream_health_payload(self) -> dict[str, object]:
        return {
            "embeds": [
                {
                    "title": "Stream Health",
                    "description": "No RTMP drops recorded since message setup.",
                    "color": 0x00AA00,
                    "fields": [
                        {
                            "name": "Last update",
                            "value": datetime.now(timezone.utc).strftime("%Y-%m-%d %H:%M:%S UTC"),
                            "inline": True,
                        }
                    ],
                    "footer": {"text": f"Twitch247 • {self.channel}"},
                }
            ]
        }

    def stream_start(self, video_title: str, video_id: str) -> None:
        self.send(
            NotificationEvent.STREAM_START,
            f"24/7 stream is live.",
            {"Video": video_title, "ID": video_id},
        )

    def video_change(
        self,
        video_title: str,
        video_id: str,
        position: float,
        message: str = "Switched to next video.",
    ) -> None:
        self.send(
            NotificationEvent.VIDEO_CHANGE,
            message,
            {
                "Video": video_title,
                "ID": video_id,
                "Position": f"{position:.0f}s",
            },
        )

    def error(self, message: str) -> None:
        self.send(NotificationEvent.ERROR, message)

    def stream_health(self, message: str, fields: dict[str, str] | None = None) -> None:
        self.send(NotificationEvent.STREAM_HEALTH, message, fields)

    def service_restart(self, video_title: str, position: float) -> None:
        self.send(
            NotificationEvent.SERVICE_RESTART,
            "Service restarted — resuming playback.",
            {"Video": video_title, "Position": f"{position:.0f}s"},
        )
