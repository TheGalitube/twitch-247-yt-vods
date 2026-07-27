from __future__ import annotations

import json
import stat
import subprocess
import tempfile
import threading
import unittest
from pathlib import Path
from types import SimpleNamespace
from unittest.mock import Mock, call, patch

import requests

from app.database import Video
from app.main import Twitch247App
from app.notifications import Notifier
from app.streamer import Streamer, StreamResult
from app.youtube_sync import YouTubeSync


class StreamResultRegressionTests(unittest.TestCase):
    def _make_app(self, result: StreamResult) -> Twitch247App:
        app = Twitch247App.__new__(Twitch247App)
        app.config = SimpleNamespace(seek_tolerance=5.0)
        app.db = Mock()
        app.streamer = Mock()
        app.streamer.stream_video.return_value = result
        app.notifier = Mock()
        app._stop_event = threading.Event()
        app._first_start = True
        app._reconnect_delay = 60
        app._allow_restart_seek_tolerance = False
        app._resume_mode = False
        app._reset_stream_timer_on_next_play = False
        return app

    @staticmethod
    def _unknown_duration_video() -> Video:
        return Video(
            video_id="abcdefghijk",
            title="Archive",
            duration=0,
            upload_date="20260727",
            played_status="unplayed",
            current_position_seconds=0.0,
            last_played_timestamp=None,
        )

    def test_completed_result_advances_video_with_unknown_duration(self) -> None:
        app = self._make_app(
            StreamResult(
                success=True,
                final_position=321.0,
                completed=True,
            )
        )

        app._play_video(self._unknown_duration_video())

        self.assertEqual(
            [
                call("abcdefghijk", "playing", 0.0),
                call("abcdefghijk", "played", 0.0),
            ],
            app.db.set_video_status.call_args_list,
        )
        app.db.save_position.assert_called_once_with("abcdefghijk", 0.0)

    def test_noncompleted_success_preserves_unknown_duration_checkpoint(self) -> None:
        app = self._make_app(
            StreamResult(
                success=True,
                final_position=321.0,
            )
        )

        app._play_video(self._unknown_duration_video())

        app.db.set_video_status.assert_called_once_with(
            "abcdefghijk",
            "playing",
            0.0,
        )
        app.db.save_position.assert_called_once_with("abcdefghijk", 321.0)

    def test_stream_url_resolution_failures_return_result_without_process(
        self,
    ) -> None:
        with tempfile.TemporaryDirectory() as temporary_directory:
            streamer = Streamer(
                SimpleNamespace(log_dir=Path(temporary_directory))
            )

            failures = (
                subprocess.TimeoutExpired(["yt-dlp"], timeout=30),
                OSError("resolver unavailable"),
            )
            for failure in failures:
                with self.subTest(failure=type(failure).__name__), patch(
                    "app.streamer.YouTubeSync.get_stream_urls",
                    side_effect=failure,
                ), patch("app.streamer.subprocess.Popen") as popen:
                    with self.assertLogs(
                        "twitch247.streamer",
                        level="ERROR",
                    ):
                        result = streamer.stream_video(
                            video_id="abcdefghijk",
                            title="Archive",
                            start_position=42.0,
                            seek_tolerance_seconds=5.0,
                            duration=0,
                            on_position=Mock(),
                            stop_event=threading.Event(),
                        )

                    self.assertFalse(result.success)
                    self.assertFalse(result.completed)
                    self.assertEqual(42.0, result.final_position)
                    self.assertTrue(result.error)
                    popen.assert_not_called()

    def test_stream_health_callback_exception_is_contained(self) -> None:
        callback = Mock(side_effect=RuntimeError("Discord unavailable"))

        with self.assertLogs("twitch247.streamer", level="ERROR"):
            Streamer._notify_stream_health(
                callback,
                "RTMP recovered",
                "abcdefghijk",
                42.0,
                1,
                5,
                "healthy",
            )

        callback.assert_called_once()

    def test_output_timeline_is_scoped_to_publisher_generation(self) -> None:
        with tempfile.TemporaryDirectory() as temporary_directory:
            streamer = Streamer(
                SimpleNamespace(
                    log_dir=Path(temporary_directory),
                    audio_bitrate="160k",
                    twitch_rtmp_url="rtmp://live.twitch.tv/app/secret",
                )
            )
            first_process = Mock(pid=1001, stderr=[])
            first_process.poll.return_value = None
            second_process = Mock(pid=1002, stderr=[])
            second_process.poll.return_value = None

            with patch.object(streamer, "_ensure_fifo"), patch(
                "app.streamer.subprocess.Popen",
                side_effect=[first_process, second_process],
            ), patch("app.streamer.threading.Thread"), patch(
                "app.streamer.time.sleep"
            ):
                _, first_generation, first_offset = (
                    streamer._ensure_output_process()
                )
                self.assertEqual(0.0, first_offset)

                streamer._record_output_progress(
                    first_generation,
                    first_offset,
                    123.0,
                )
                _, same_generation, next_offset = (
                    streamer._ensure_output_process()
                )
                self.assertEqual(first_generation, same_generation)
                self.assertEqual(123.0, next_offset)

                streamer._stop_output_process()
                _, second_generation, reset_offset = (
                    streamer._ensure_output_process()
                )
                self.assertGreater(second_generation, first_generation)
                self.assertEqual(0.0, reset_offset)
                streamer._stop_output_process()

    def test_input_transport_marks_handover_discontinuity(self) -> None:
        with tempfile.TemporaryDirectory() as temporary_directory:
            streamer = Streamer(
                SimpleNamespace(
                    log_dir=Path(temporary_directory),
                    encoder_preset="superfast",
                    video_bitrate="6000k",
                    maxrate="6000k",
                    bufsize="12000k",
                    audio_bitrate="160k",
                )
            )

            command = streamer._build_input_ffmpeg_cmd(
                "https://video.example/source",
                "https://audio.example/source",
                42.0,
                123.0,
                "/tmp/output.pipe",
            )

            offset_index = command.index("-output_ts_offset")
            flags_index = command.index("-mpegts_flags")
            self.assertEqual("123.0", command[offset_index + 1])
            self.assertEqual(
                "+resend_headers+initial_discontinuity",
                command[flags_index + 1],
            )
            self.assertLess(
                streamer.OUTPUT_MAX_TIMELINE_SECONDS,
                (2**33) / 90_000,
            )

    def test_stream_url_resolution_stops_promptly_on_shutdown(self) -> None:
        process = Mock()
        process.communicate.return_value = ("", "")
        stop_event = threading.Event()
        stop_event.set()

        with patch(
            "app.youtube_sync.subprocess.Popen",
            return_value=process,
        ):
            with self.assertRaises(InterruptedError):
                YouTubeSync.get_stream_urls(
                    "abcdefghijk",
                    stop_event=stop_event,
                )

        process.terminate.assert_called_once()
        process.communicate.assert_called_once_with(timeout=5)


class NotifierRegressionTests(unittest.TestCase):
    def test_non_object_state_is_treated_as_empty(self) -> None:
        with tempfile.TemporaryDirectory() as temporary_directory:
            state_path = Path(temporary_directory) / "discord-state.json"
            state_path.write_text("[]\n", encoding="utf-8")
            notifier = Notifier(
                "https://discord.com/api/webhooks/123/secret",
                state_path=state_path,
            )

            with self.assertLogs(
                "twitch247.notifications",
                level="WARNING",
            ):
                self.assertEqual({}, notifier._load_state())

    def test_state_save_is_atomic_and_private(self) -> None:
        with tempfile.TemporaryDirectory() as temporary_directory:
            state_path = Path(temporary_directory) / "discord-state.json"
            notifier = Notifier(
                "https://discord.com/api/webhooks/123/secret",
                state_path=state_path,
            )

            notifier._save_state({"status": "message-id"})

            self.assertEqual(
                {"status": "message-id"},
                json.loads(state_path.read_text(encoding="utf-8")),
            )
            self.assertEqual(0o600, stat.S_IMODE(state_path.stat().st_mode))
            self.assertFalse(
                state_path.with_name(f".{state_path.name}.tmp").exists()
            )

    def test_worker_contains_failures_and_does_not_log_webhook_secret(self) -> None:
        webhook_secret = "super-secret-webhook-token"
        notifier = Notifier(
            f"https://discord.com/api/webhooks/123/{webhook_secret}"
        )
        failures = [
            requests.ConnectionError(
                f"request failed for {notifier.webhook_url}"
            ),
            RuntimeError("invalid state"),
        ]

        with patch.object(
            notifier,
            "_upsert_message",
            side_effect=failures,
        ), self.assertLogs(
            "twitch247.notifications",
            level="WARNING",
        ) as captured:
            notifier.error("first")
            notifier.error("second")
            notifier._queue.join()

        output = "\n".join(captured.output)
        self.assertIn("ConnectionError", output)
        self.assertIn("RuntimeError", output)
        self.assertNotIn(webhook_secret, output)
        self.assertIsNotNone(notifier._worker)
        self.assertTrue(notifier._worker.is_alive())


if __name__ == "__main__":
    unittest.main()
