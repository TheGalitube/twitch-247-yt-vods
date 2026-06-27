"""Main orchestration loop for Twitch247."""

from __future__ import annotations

from datetime import datetime, timezone
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
        self.notifier = Notifier(config.discord_webhook_url, config.twitch_channel)
        self._stop_event = threading.Event()
        self._first_start = True
        self._reconnect_delay = self.RECONNECT_DELAY
        self._last_sync = 0.0
        self._stream_clock = 0.0
        self._allow_restart_seek_tolerance = False
        self._resume_mode = False
        self._reset_stream_timer_on_next_play = False

    def run(self) -> None:
        signal.signal(signal.SIGTERM, self._handle_signal)
        signal.signal(signal.SIGINT, self._handle_signal)

        logger.info("Twitch247 starting (v1.0.0)")
        self.db.log_event("startup", "Service started")

        try:
            self.youtube.sync()
        except Exception as exc:
            logger.error("Initial YouTube sync failed: %s", exc)
            self._record_error(str(exc))

        resume = self.db.get_resume_video()
        if resume:
            logger.info(
                "Resuming after restart: %s at %.1fs",
                resume.title,
                resume.current_position_seconds,
            )
            self.notifier.service_restart(resume.title, resume.current_position_seconds)
            self._stream_clock = self._restore_stream_clock()
            self._allow_restart_seek_tolerance = True
            self._resume_mode = True
            self._reset_stream_timer_on_next_play = True
            self._first_start = False
        
        try:
            while not self._stop_event.is_set():
                self._maybe_sync()

                video = self._select_video(resume)
                resume = None

                if not video:
                    logger.warning("No videos available, waiting for sync...")
                    self._interruptible_sleep(60)
                    try:
                        self.youtube.sync()
                    except Exception as exc:
                        logger.error("Sync failed: %s", exc)
                    continue

                self._play_video(video)
        finally:
            self.streamer.close()

        logger.info("Twitch247 shutting down")
        self.db.set_streaming(False)

    def _select_video(self, resume: Video | None) -> Video | None:
        if resume:
            return resume
        return self.db.get_next_video()

    def _play_video(self, video: Video) -> None:
        position = video.current_position_seconds
        if video.played_status == "played":
            position = 0.0

        self.db.set_video_status(video.video_id, "playing", position)
        reset_timer = self._first_start or self._reset_stream_timer_on_next_play
        self.db.set_streaming(True, video.video_id, reset_stream_timer=reset_timer)
        self.db.set_error(None)
        self._reset_stream_timer_on_next_play = False

        if self._first_start:
            self.notifier.stream_start(video.title, video.video_id)
            self._first_start = False
        else:
            self.notifier.video_change(video.title, video.video_id, position)

        logger.info("Playing: %s (%s) from %.1fs", video.title, video.video_id, position)

        def on_position(pos: float) -> None:
            self.db.save_position(video.video_id, pos)

        result = self.streamer.stream_video(
            video_id=video.video_id,
            title=video.title,
            start_position=position,
            stream_offset_seconds=self._stream_clock,
            seek_tolerance_seconds=(
                self.config.seek_tolerance if self._allow_restart_seek_tolerance else 0.0
            ),
            duration=video.duration,
            on_position=on_position,
            stop_event=self._stop_event,
        )

        self._allow_restart_seek_tolerance = False

        played_seconds = max(0.0, result.final_position - position)
        self._stream_clock += played_seconds

        if self._stop_event.is_set():
            self.db.save_position(video.video_id, result.final_position)
            return

        if result.success:
            self._reconnect_delay = self.RECONNECT_DELAY
            finished = (
                video.duration > 0
                and result.final_position >= video.duration - 5
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

    def _maybe_sync(self) -> None:
        now = time.monotonic()
        if now - self._last_sync >= self.config.sync_interval:
            try:
                self.youtube.sync()
            except Exception as exc:
                logger.error("Periodic sync failed: %s", exc)
            self._last_sync = now

    def _record_error(self, error: str) -> None:
        self.db.set_error(error)
        self.db.log_event("error", error)

    def _interruptible_sleep(self, seconds: float) -> None:
        self._stop_event.wait(timeout=seconds)

    def _restore_stream_clock(self) -> float:
        state = self.db.get_playback_state()
        if state.stream_started_at:
            try:
                started_at = datetime.strptime(
                    state.stream_started_at,
                    "%Y-%m-%d %H:%M:%S",
                ).replace(tzinfo=timezone.utc)
                return max(0.0, (datetime.now(timezone.utc) - started_at).total_seconds())
            except ValueError:
                logger.warning(
                    "Invalid stream_started_at value %r, falling back to video position",
                    state.stream_started_at,
                )

        return max(0.0, state.current_position_seconds)

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
