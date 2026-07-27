"""Main orchestration loop for Twitch247."""

from __future__ import annotations

import signal
import sys
import threading
import time
from pathlib import Path

from app.config import Config, load_config
from app.database import Database, Video
from app.logging_setup import get_logger, setup_logging
from app.notifications import Notifier
from app.streamer import Streamer
from app.youtube_sync import YouTubeSync

logger = get_logger("main")


class Twitch247App:
    RECONNECT_DELAY = 5
    MAX_RECONNECT_DELAY = 120
    ERROR_RETRY_DELAY = 30

    def __init__(self, config: Config) -> None:
        self.config = config
        schema_path = config.app_root / "database" / "schema.sql"
        if not schema_path.is_file():
            schema_path = Path(__file__).resolve().parent.parent / "database" / "schema.sql"

        self.db = Database(config.db_path, schema_path)
        self.youtube = YouTubeSync(config.youtube_channel_url, self.db)
        self.streamer = Streamer(config)
        self.notifier = Notifier(
            config.discord_webhook_url,
            config.twitch_channel,
            config.log_dir / "discord_messages.json",
        )
        self._stop_event = threading.Event()
        self._first_start = True
        self._reconnect_delay = self.RECONNECT_DELAY
        self._last_sync = 0.0
        self._sync_lock = threading.Lock()
        self._sync_thread: threading.Thread | None = None
        self._allow_restart_seek_tolerance = False
        self._resume_mode = False
        self._reset_stream_timer_on_next_play = False

    def run(self) -> None:
        signal.signal(signal.SIGTERM, self._handle_signal)
        signal.signal(signal.SIGINT, self._handle_signal)

        logger.info("Twitch247 starting (v1.0.0)")
        self.db.log_event("startup", "Service started")

        resume = self.db.get_resume_video()
        if resume:
            logger.info(
                "Resuming after restart: %s at %.1fs",
                resume.title,
                resume.current_position_seconds,
            )
            self.notifier.service_restart(resume.title, resume.current_position_seconds)
            self._allow_restart_seek_tolerance = True
            self._resume_mode = True
            self._reset_stream_timer_on_next_play = True
            self._first_start = False
            # A restart must resume media immediately. Channel discovery can take
            # several minutes and therefore runs in the background.
            self._maybe_sync(force=True)
        else:
            try:
                self.youtube.sync()
            except Exception as exc:
                logger.error("Initial YouTube sync failed: %s", exc)
                self._record_error(str(exc))
            finally:
                self._last_sync = time.monotonic()

        try:
            while not self._stop_event.is_set():
                self._maybe_sync()

                video = self._select_video(resume)
                resume = None

                if not video:
                    logger.warning("No videos available, waiting for sync...")
                    self._interruptible_sleep(60)
                    self._maybe_sync(force=True)
                    continue

                try:
                    self._play_video(video)
                except Exception as exc:
                    logger.exception(
                        "Unexpected playback error for %s; resetting pipeline",
                        video.video_id,
                    )
                    try:
                        self._record_error(str(exc))
                        self.db.set_streaming(False)
                    except Exception:
                        logger.exception("Could not persist unexpected playback error")
                    self.streamer.close()
                    self._interruptible_sleep(self.ERROR_RETRY_DELAY)
        finally:
            self.streamer.close()
            try:
                self.db.set_streaming(False)
            except Exception:
                logger.exception("Could not persist shutdown state")
            logger.info("Twitch247 shutting down")

    def _select_video(self, resume: Video | None) -> Video | None:
        if resume:
            return resume
        return self.db.get_next_video()

    def _play_video(self, video: Video) -> None:
        position = video.current_position_seconds
        if video.played_status == "played":
            position = 0.0

        if video.duration > 0 and position >= video.duration - 5:
            self.db.set_video_status(video.video_id, "played", 0.0)
            self.db.save_position(video.video_id, 0.0)
            logger.info("Video already at end, marking completed: %s", video.title)
            self._resume_mode = False
            return

        self.db.set_video_status(video.video_id, "playing", position)
        reset_timer = self._first_start or self._reset_stream_timer_on_next_play
        self.db.set_streaming(True, video.video_id, reset_stream_timer=reset_timer)
        self.db.set_error(None)
        self._reset_stream_timer_on_next_play = False

        if self._first_start:
            self.notifier.stream_start(video.title, video.video_id)
            self._first_start = False
        else:
            status_message = (
                "Resumed playback after service restart."
                if self._resume_mode
                else "Switched to next video."
            )
            self.notifier.video_change(
                video.title,
                video.video_id,
                position,
                message=status_message,
            )
        self._resume_mode = False

        logger.info("Playing: %s (%s) from %.1fs", video.title, video.video_id, position)

        def on_position(pos: float) -> None:
            self.db.save_position(video.video_id, pos)

        def on_stream_health(message: str, fields: dict[str, str]) -> None:
            self.notifier.stream_health(message, fields)

        result = self.streamer.stream_video(
            video_id=video.video_id,
            title=video.title,
            start_position=position,
            seek_tolerance_seconds=(
                self.config.seek_tolerance if self._allow_restart_seek_tolerance else 0.0
            ),
            duration=video.duration,
            on_position=on_position,
            stop_event=self._stop_event,
            on_stream_health=on_stream_health,
        )

        self._allow_restart_seek_tolerance = False

        if self._stop_event.is_set():
            self.db.save_position(video.video_id, result.final_position)
            return

        if result.success:
            self._reconnect_delay = self.RECONNECT_DELAY
            finished = (
                result.completed
                or (
                    video.duration > 0
                    and result.final_position >= video.duration - 5
                )
            )
            if finished:
                self.db.set_video_status(video.video_id, "played", 0.0)
                self.db.save_position(video.video_id, 0.0)
                logger.info("Video completed: %s", video.title)
            else:
                self.db.save_position(video.video_id, result.final_position)
        else:
            self._handle_stream_failure(video, result.error, result.final_position)

    def _handle_stream_failure(
        self,
        video: Video,
        error: str | None,
        position: float,
    ) -> None:
        msg = error or "Unknown stream error"
        logger.error("Stream failed for %s: %s", video.video_id, msg)
        if YouTubeSync.is_upcoming_live_error(msg):
            logger.info(
                "Skipping unavailable YouTube video %s until next sync",
                video.video_id,
            )
            self.db.delete_video(video.video_id)
            self.db.set_streaming(False)
            self._reconnect_delay = self.RECONNECT_DELAY
            return

        if YouTubeSync.is_youtube_auth_error(msg):
            self._record_error(msg)
            self.db.set_video_status(video.video_id, "played", 0.0)
            self.db.save_position(video.video_id, 0.0)
            self.db.set_streaming(False)
            logger.warning(
                "YouTube auth/bot check for %s; marking video played and continuing with next queued video",
                video.video_id,
            )
            self.notifier.stream_health(
                "Skipped YouTube video after auth/bot-check failure.",
                {
                    "Video": video.title,
                    "ID": video.video_id,
                    "Reason": "YouTube auth/bot check",
                },
            )
            self._reconnect_delay = self.RECONNECT_DELAY
            return

        self._record_error(msg)
        self.notifier.error(f"{video.title}: {msg}")
        self.db.save_position(video.video_id, position)
        self.db.set_streaming(False)

        logger.info("Reconnecting in %ds...", self._reconnect_delay)
        self._interruptible_sleep(self._reconnect_delay)
        self._reconnect_delay = min(
            self._reconnect_delay * 2,
            self.MAX_RECONNECT_DELAY,
        )

    def _maybe_sync(self, force: bool = False) -> None:
        now = time.monotonic()
        with self._sync_lock:
            if self._sync_thread and self._sync_thread.is_alive():
                return
            if not force and now - self._last_sync < self.config.sync_interval:
                return
            self._last_sync = now
            self._sync_thread = threading.Thread(
                target=self._run_sync,
                name="youtube-channel-sync",
                daemon=True,
            )
            self._sync_thread.start()

    def _run_sync(self) -> None:
        try:
            self.youtube.sync()
        except Exception as exc:
            logger.error("Periodic sync failed: %s", exc)
            try:
                self._record_error(str(exc))
            except Exception:
                logger.exception("Could not persist sync error")

    def _record_error(self, error: str) -> None:
        self.db.set_error(error)
        self.db.log_event("error", error)

    def _interruptible_sleep(self, seconds: float) -> None:
        self._stop_event.wait(timeout=seconds)

    def _handle_signal(self, signum: int, _frame: object) -> None:
        logger.info("Received signal %d, stopping...", signum)
        self._stop_event.set()


def main() -> None:
    try:
        config = load_config()
    except KeyError as exc:
        print(f"Missing required config: {exc}", file=sys.stderr)
        sys.exit(1)

    setup_logging(config.log_dir, config.log_level)
    app = Twitch247App(config)
    app.run()


if __name__ == "__main__":
    main()
