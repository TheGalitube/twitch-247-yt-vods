"""Optional Discord webhook notifications."""

from __future__ import annotations

import json
import logging
import os
import queue
import threading
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
        self._queue: queue.Queue[tuple[str, dict[str, object]]] = queue.Queue(
            maxsize=50
        )
        self._worker: threading.Thread | None = None
        self._worker_lock = threading.Lock()

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

        self._ensure_worker()
        try:
            self._queue.put_nowait((bucket, payload))
        except queue.Full:
            logger.warning("Discord notification queue full; dropping %s update", bucket)

    def _ensure_worker(self) -> None:
        with self._worker_lock:
            if self._worker and self._worker.is_alive():
                return
            self._worker = threading.Thread(
                target=self._worker_loop,
                name="discord-notifications",
                daemon=True,
            )
            self._worker.start()

    def _worker_loop(self) -> None:
        while True:
            bucket, payload = self._queue.get()
            try:
                self._upsert_message(bucket, payload)
                state = self._load_state()
                if bucket == "status" and "error" not in state:
                    self._upsert_message("error", self._empty_error_payload())
                    state = self._load_state()
                if bucket == "status" and "stream_health" not in state:
                    self._upsert_message(
                        "stream_health",
                        self._empty_stream_health_payload(),
                    )
            except requests.RequestException as exc:
                # requests exception strings contain the full webhook URL/token.
                logger.warning(
                    "Discord notification failed (%s)",
                    type(exc).__name__,
                )
            except Exception as exc:
                # Notifications are optional and may never stop playback.
                logger.warning(
                    "Discord notification update failed (%s)",
                    type(exc).__name__,
                )
            finally:
                self._queue.task_done()

    def _upsert_message(self, bucket: str, payload: dict[str, object]) -> None:
        message_id = self._load_state().get(bucket)
        if message_id:
            resp = requests.patch(
                f"{self.webhook_url}/messages/{message_id}",
                json=payload,
                timeout=5,
            )
            if resp.status_code != 404:
                resp.raise_for_status()
                return

            logger.info("Discord %s message %s no longer exists; creating a new one", bucket, message_id)

        resp = requests.post(f"{self.webhook_url}?wait=true", json=payload, timeout=5)
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
            logger.warning(
                "Could not read Discord notification state (%s)",
                type(exc).__name__,
            )
            return {}
        if not isinstance(data, dict):
            logger.warning("Discord notification state is not an object; resetting")
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
        temporary_path = self.state_path.with_name(f".{self.state_path.name}.tmp")
        temporary_path.write_text(
            json.dumps(state, indent=2, sort_keys=True) + "\n",
            encoding="utf-8",
        )
        os.chmod(temporary_path, 0o600)
        os.replace(temporary_path, self.state_path)

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
