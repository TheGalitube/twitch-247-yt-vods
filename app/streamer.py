"""FFmpeg-based streamer: YouTube URL -> Twitch RTMP without permanent download."""

from __future__ import annotations

import os
import re
import stat
import subprocess
import threading
import time
from dataclasses import dataclass
from typing import Callable

from app.config import Config
from app.logging_setup import get_logger
from app.youtube_sync import YouTubeSync

logger = get_logger("streamer")
playback_logger = get_logger("playback")


@dataclass
class StreamResult:
    success: bool
    final_position: float
    error: str | None = None


class Streamer:
    TIME_RE = re.compile(r"out_time_ms=(\d+)")
    OUTPUT_WIDTH = 1920
    OUTPUT_HEIGHT = 1080
    OUTPUT_FPS = 60
    OUTPUT_HEALTH_CHECK_INTERVAL = 5.0
    OUTPUT_STARTUP_GRACE_SECONDS = 30.0
    TCP_CLOSED_STATES = {
        "04",  # FIN_WAIT1
        "05",  # FIN_WAIT2
        "07",  # CLOSE
        "08",  # CLOSE_WAIT
        "09",  # LAST_ACK
        "0B",  # CLOSING
    }

    def __init__(self, config: Config) -> None:
        self.config = config
        self._output_proc: subprocess.Popen[bytes] | None = None
        self._output_lock = threading.Lock()
        self._output_stderr_thread: threading.Thread | None = None
        self._output_dead = threading.Event()
        self._closing = threading.Event()
        self._fifo_path = self.config.log_dir / "twitch247-rtmp.pipe"
        self._fifo_keepalive_fd: int | None = None
        self._output_started_at = 0.0

    def stream_video(
        self,
        video_id: str,
        title: str,
        start_position: float,
        stream_offset_seconds: float,
        seek_tolerance_seconds: float,
        duration: int,
        on_position: Callable[[float], None],
        stop_event: threading.Event,
    ) -> StreamResult:
        """Stream a YouTube video to Twitch starting at start_position."""
        current_position = start_position
        max_output_retries = 2

        for attempt in range(1, max_output_retries + 1):
            seek_pos = max(0.0, current_position - max(0.0, seek_tolerance_seconds))
            if attempt == 1:
                playback_logger.info(
                    "Starting stream: %s (%s) at %.1fs (seek %.1fs)",
                    title,
                    video_id,
                    current_position,
                    seek_pos,
                )
            else:
                playback_logger.info(
                    "Restarting stream after RTMP drop: %s (%s) at %.1fs (seek %.1fs)",
                    title,
                    video_id,
                    current_position,
                    seek_pos,
                )

            try:
                video_url, audio_url = YouTubeSync.get_stream_urls(video_id)
            except subprocess.CalledProcessError as exc:
                err = exc.stderr or str(exc)
                logger.error("Failed to resolve stream URL for %s: %s", video_id, err)
                return StreamResult(success=False, final_position=current_position, error=err)
            except RuntimeError as exc:
                return StreamResult(success=False, final_position=current_position, error=str(exc))

            try:
                output_pipe = self._ensure_output_process()
            except RuntimeError as exc:
                logger.error("Failed to start RTMP output: %s", exc)
                return StreamResult(
                    success=False,
                    final_position=current_position,
                    error=str(exc),
                )

            try:
                cmd = self._build_input_ffmpeg_cmd(
                    video_url,
                    audio_url,
                    seek_pos,
                    stream_offset_seconds,
                    output_pipe,
                )
            except RuntimeError as exc:
                logger.error("Failed to build FFmpeg command: %s", exc)
                return StreamResult(
                    success=False,
                    final_position=current_position,
                    error=str(exc),
                )
            logger.debug("FFmpeg command: %s", " ".join(cmd))

            proc: subprocess.Popen[bytes] | None = None
            wall_start = time.monotonic()
            last_save = 0.0
            last_position = seek_pos
            position_lock = threading.Lock()
            stderr_tail: list[str] = []
            finished_naturally = False
            output_dropped = False
            last_output_health_check = time.monotonic()

            def set_position(position: float) -> None:
                nonlocal last_position
                with position_lock:
                    last_position = position

            def get_position() -> float:
                with position_lock:
                    return last_position

            def read_progress() -> None:
                if proc is None or proc.stderr is None:
                    return
                for raw_line in proc.stderr:
                    line = raw_line.decode("utf-8", errors="replace").strip()
                    if line:
                        stderr_tail.append(line)
                        del stderr_tail[:-20]

                    match = self.TIME_RE.search(line)
                    if match:
                        ffmpeg_ms = int(match.group(1))
                        set_position(seek_pos + (ffmpeg_ms / 1_000_000))

            try:
                proc = subprocess.Popen(
                    cmd,
                    stdout=subprocess.DEVNULL,
                    stderr=subprocess.PIPE,
                    bufsize=0,
                )

                progress_thread = threading.Thread(
                    target=read_progress,
                    name=f"ffmpeg-progress-{video_id}",
                    daemon=True,
                )
                progress_thread.start()

                while not self._stop_event_set(stop_event):
                    if stop_event.is_set():
                        break
                    if proc.poll() is not None:
                        break
                    if self._output_dead.is_set():
                        output_dropped = True
                        logger.warning("RTMP output went away, restarting stream")
                        break

                    now = time.monotonic()
                    if (
                        now - last_output_health_check
                        >= self.OUTPUT_HEALTH_CHECK_INTERVAL
                    ):
                        last_output_health_check = now
                        if self._output_tcp_connection_closed():
                            output_dropped = True
                            self._output_dead.set()
                            logger.warning(
                                "RTMP output TCP connection closed, restarting stream"
                            )
                            break

                    if now - last_save >= self.config.save_interval:
                        elapsed = now - wall_start
                        position = seek_pos + elapsed
                        if duration > 0:
                            position = min(position, float(duration))
                        on_position(position)
                        last_save = now
                        playback_logger.debug(
                            "Position saved: %.1fs / %ds",
                            position,
                            duration,
                        )

                    if duration > 0 and last_position >= duration - 2:
                        playback_logger.info(
                            "Video near end (%.1fs), finishing",
                            last_position,
                        )
                        finished_naturally = True
                        break

                    time.sleep(0.5)

                if proc.poll() is None:
                    proc.terminate()
                    try:
                        proc.wait(timeout=30)
                    except subprocess.TimeoutExpired:
                        proc.kill()
                        proc.wait()

                progress_thread.join(timeout=2)
                returncode = proc.returncode or 0
                final_position = get_position()

                if stop_event.is_set():
                    return StreamResult(success=True, final_position=final_position)

                if finished_naturally:
                    if duration > 0:
                        final_position = float(duration)
                    playback_logger.info(
                        "Stream finished: %s at %.1fs",
                        video_id,
                        final_position,
                    )
                    return StreamResult(success=True, final_position=final_position)

                stderr_joined = " ".join(stderr_tail[-10:])
                output_failure = output_dropped or (
                    "Broken pipe" in stderr_joined
                    or "Input/output error" in stderr_joined
                    or "Connection reset by peer" in stderr_joined
                )
                transient_input_failure = returncode in (-9, 255) or (
                    "Invalid data found when processing input" in stderr_joined
                    or "HTTP error 403 Forbidden" in stderr_joined
                )
                if (output_failure or transient_input_failure) and attempt < max_output_retries:
                    current_position = max(final_position, current_position)
                    logger.warning(
                        "%s dropped, retrying %s at %.1fs (attempt %d/%d)",
                        "RTMP output" if output_failure else "Input stream",
                        video_id,
                        current_position,
                        attempt,
                        max_output_retries,
                    )
                    if output_failure:
                        self._stop_output_process()
                    time.sleep(2)
                    continue

                if returncode != 0 and returncode != 255:
                    err_tail = f": {' | '.join(stderr_tail[-5:])}" if stderr_tail else ""
                    logger.warning("FFmpeg exited with code %d", returncode)
                    return StreamResult(
                        success=False,
                        final_position=final_position,
                        error=f"FFmpeg exit code {returncode}{err_tail}",
                    )

                if duration > 0 and final_position >= duration - 5:
                    final_position = float(duration)

                playback_logger.info(
                    "Stream finished: %s at %.1fs",
                    video_id,
                    final_position,
                )
                return StreamResult(success=True, final_position=final_position)

            except Exception as exc:
                logger.exception("Stream error for %s", video_id)
                if proc and proc.poll() is None:
                    proc.kill()
                if attempt < max_output_retries and (
                    "RTMP output process" in str(exc)
                    or self._output_dead.is_set()
                ):
                    current_position = max(get_position(), current_position)
                    logger.warning(
                        "Retrying %s after output failure at %.1fs",
                        video_id,
                        current_position,
                    )
                    self._stop_output_process()
                    time.sleep(2)
                    continue
                return StreamResult(
                    success=False,
                    final_position=get_position(),
                    error=str(exc),
                )

        return StreamResult(
            success=False,
            final_position=current_position,
            error="RTMP output dropped repeatedly",
        )

    def close(self) -> None:
        self._stop_output_process()

    def _ensure_output_process(self) -> str:
        with self._output_lock:
            if self._output_proc and self._output_proc.poll() is None:
                return str(self._fifo_path)

            self._ensure_fifo()
            self._closing.clear()
            self._output_dead.clear()
            self._output_proc = subprocess.Popen(
                self._build_output_ffmpeg_cmd(),
                stderr=subprocess.PIPE,
                bufsize=0,
            )
            self._output_started_at = time.monotonic()
            self._output_stderr_thread = threading.Thread(
                target=self._drain_output_stderr,
                name="ffmpeg-rtmp-output",
                daemon=True,
            )
            self._output_stderr_thread.start()

            time.sleep(0.5)
            if self._output_proc.poll() is not None:
                raise RuntimeError(
                    f"RTMP output exited with code {self._output_proc.returncode}"
                )

            playback_logger.info("Persistent RTMP output started")
            return str(self._fifo_path)

    def _stop_output_process(self) -> None:
        self._closing.set()
        with self._output_lock:
            proc = self._output_proc
            self._output_proc = None
            keepalive_fd = self._fifo_keepalive_fd
            self._fifo_keepalive_fd = None

        if not proc:
            if keepalive_fd is not None:
                try:
                    os.close(keepalive_fd)
                except OSError:
                    pass
            self._cleanup_fifo()
            return

        if proc.poll() is None:
            proc.terminate()
            try:
                proc.wait(timeout=10)
            except subprocess.TimeoutExpired:
                proc.kill()
                proc.wait()

        if keepalive_fd is not None:
            try:
                os.close(keepalive_fd)
            except OSError:
                pass

        self._cleanup_fifo()

    def _cleanup_fifo(self) -> None:
        try:
            if self._fifo_path.exists():
                self._fifo_path.unlink()
        except OSError:
            pass

    def _ensure_fifo(self) -> None:
        self.config.log_dir.mkdir(parents=True, exist_ok=True)
        if self._fifo_path.exists():
            if not stat.S_ISFIFO(self._fifo_path.stat().st_mode):
                raise RuntimeError(f"{self._fifo_path} exists and is not a FIFO")
        else:
            os.mkfifo(self._fifo_path, 0o660)

        if self._fifo_keepalive_fd is None:
            self._fifo_keepalive_fd = os.open(
                self._fifo_path,
                os.O_RDWR | os.O_NONBLOCK,
            )

    def _drain_output_stderr(self) -> None:
        proc = self._output_proc
        if not proc or not proc.stderr:
            return

        for raw_line in proc.stderr:
            line = raw_line.decode("utf-8", errors="replace").strip()
            if line:
                logger.warning("RTMP output: %s", line)

        if self._output_proc is proc and not self._closing.is_set():
            self._output_dead.set()
            logger.error("RTMP output process exited unexpectedly")

    def _output_tcp_connection_closed(self) -> bool:
        proc = self._output_proc
        if not proc or proc.poll() is not None:
            return True
        if (
            self._output_started_at
            and time.monotonic() - self._output_started_at
            < self.OUTPUT_STARTUP_GRACE_SECONDS
        ):
            return False

        socket_inodes: set[str] = set()
        fd_dir = f"/proc/{proc.pid}/fd"
        try:
            for fd_name in os.listdir(fd_dir):
                try:
                    target = os.readlink(os.path.join(fd_dir, fd_name))
                except OSError:
                    continue
                match = re.fullmatch(r"socket:\[(\d+)\]", target)
                if match:
                    socket_inodes.add(match.group(1))
        except OSError:
            return True

        if not socket_inodes:
            logger.warning("RTMP output process has no TCP socket")
            return True

        matched_socket = False
        for table in ("/proc/net/tcp", "/proc/net/tcp6"):
            try:
                with open(table, "r", encoding="utf-8") as handle:
                    next(handle, None)
                    for line in handle:
                        fields = line.split()
                        if len(fields) > 9 and fields[9] in socket_inodes:
                            matched_socket = True
                            if fields[3] in self.TCP_CLOSED_STATES:
                                return True
            except OSError:
                continue

        if not matched_socket:
            logger.warning("RTMP output socket disappeared from TCP table")
            return True

        return False

    @staticmethod
    def _stop_event_set(stop_event: threading.Event) -> bool:
        return stop_event.is_set()

    def _build_output_ffmpeg_cmd(self) -> list[str]:
        return [
            "ffmpeg",
            "-hide_banner",
            "-loglevel",
            "warning",
            "-fflags",
            "+genpts+discardcorrupt",
            "-err_detect",
            "ignore_err",
            "-f",
            "mpegts",
            "-i",
            str(self._fifo_path),
            "-c",
            "copy",
            "-flvflags",
            "no_duration_filesize",
            "-flush_packets",
            "1",
            "-f",
            "flv",
            self.config.twitch_rtmp_url,
        ]

    def _build_input_ffmpeg_cmd(
        self,
        video_url: str,
        audio_url: str | None,
        seek_pos: float,
        stream_offset_seconds: float,
        output_pipe: str,
    ) -> list[str]:
        cfg = self.config
        video_filter = (
            f"scale={self.OUTPUT_WIDTH}:{self.OUTPUT_HEIGHT}:"
            "force_original_aspect_ratio=decrease,"
            f"pad={self.OUTPUT_WIDTH}:{self.OUTPUT_HEIGHT}:(ow-iw)/2:(oh-ih)/2,"
            f"fps={self.OUTPUT_FPS},setpts=PTS-STARTPTS,format=yuv420p"
        )
        ts_offset = max(0.0, stream_offset_seconds)

        common_prefix = [
            "ffmpeg",
            "-y",
            "-hide_banner",
            "-loglevel",
            "warning",
            "-fflags",
            "+genpts",
            "-progress",
            "pipe:2",
            "-ss",
            str(seek_pos),
            "-re",
            "-reconnect",
            "1",
            "-reconnect_streamed",
            "1",
            "-reconnect_delay_max",
            "30",
        ]

        if audio_url:
            return [
                *common_prefix,
                "-i",
                video_url,
                "-ss",
                str(seek_pos),
                "-re",
                "-reconnect",
                "1",
                "-reconnect_streamed",
                "1",
                "-reconnect_delay_max",
                "30",
                "-i",
                audio_url,
                "-map",
                "0:v:0",
                "-map",
                "1:a:0",
                "-vf",
                video_filter,
                "-af",
                "aresample=async=1:first_pts=0,asetpts=PTS-STARTPTS",
                "-c:v",
                "libx264",
                "-preset",
                "veryfast",
                "-r",
                str(self.OUTPUT_FPS),
                "-b:v",
                cfg.video_bitrate,
                "-maxrate",
                cfg.maxrate,
                "-bufsize",
                cfg.bufsize,
                "-g",
                str(self.OUTPUT_FPS * 2),
                "-keyint_min",
                str(self.OUTPUT_FPS * 2),
                "-sc_threshold",
                "0",
                "-output_ts_offset",
                str(ts_offset),
                "-c:a",
                "aac",
                "-b:a",
                cfg.audio_bitrate,
                "-ar",
                "48000",
                "-ac",
                "2",
                "-flush_packets",
                "1",
                "-f",
                "mpegts",
                "-mpegts_flags",
                "+resend_headers",
                "-muxdelay",
                "0",
                "-muxpreload",
                "0",
                output_pipe,
            ]

        return [
            *common_prefix,
            "-i",
            video_url,
            "-map",
            "0:v:0",
            "-map",
            "0:a:0?",
            "-vf",
            video_filter,
            "-af",
            "aresample=async=1:first_pts=0,asetpts=PTS-STARTPTS",
            "-c:v",
            "libx264",
            "-preset",
            "veryfast",
            "-r",
            str(self.OUTPUT_FPS),
            "-b:v",
            cfg.video_bitrate,
            "-maxrate",
            cfg.maxrate,
            "-bufsize",
            cfg.bufsize,
            "-g",
            str(self.OUTPUT_FPS * 2),
            "-keyint_min",
            str(self.OUTPUT_FPS * 2),
            "-sc_threshold",
            "0",
            "-output_ts_offset",
            str(ts_offset),
            "-c:a",
            "aac",
            "-b:a",
            cfg.audio_bitrate,
            "-ar",
            "48000",
            "-ac",
            "2",
            "-flush_packets",
            "1",
            "-f",
            "mpegts",
            "-mpegts_flags",
            "+resend_headers",
            "-muxdelay",
            "0",
            "-muxpreload",
            "0",
            output_pipe,
        ]
