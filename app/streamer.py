"""FFmpeg-based streamer: YouTube URL -> Twitch RTMP without permanent download."""

from __future__ import annotations

import os
import re
import stat
import subprocess
import threading
import time
from collections import deque
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
    completed: bool = False


@dataclass
class TcpSocketInfo:
    table: str
    local: str
    remote: str
    state: str
    inode: str
    tx_queue: str
    rx_queue: str


class Streamer:
    TIME_RE = re.compile(r"out_time_ms=(\d+)")
    RTMP_URL_RE = re.compile(r"(rtmps?://[^/\s]+/app/)[^\s]+", re.IGNORECASE)
    HTTP_URL_RE = re.compile(r"https?://[^\s]+", re.IGNORECASE)
    OUTPUT_WIDTH = 1920
    OUTPUT_HEIGHT = 1080
    OUTPUT_FPS = 30
    OUTPUT_RESTART_ATTEMPTS = 5
    OUTPUT_HEALTH_CHECK_INTERVAL = 5.0
    OUTPUT_STARTUP_GRACE_SECONDS = 30.0
    OUTPUT_UNHEALTHY_CHECKS = 2
    OUTPUT_MAX_TIMELINE_SECONDS = 20 * 60 * 60
    INPUT_STARTUP_TIMEOUT_SECONDS = 90.0
    INPUT_STALL_TIMEOUT_SECONDS = 60.0
    TCP_CLOSED_STATES = {
        "04",  # FIN_WAIT1
        "05",  # FIN_WAIT2
        "07",  # CLOSE
        "08",  # CLOSE_WAIT
        "09",  # LAST_ACK
        "0B",  # CLOSING
    }
    TCP_STATE_NAMES = {
        "01": "ESTABLISHED",
        "02": "SYN_SENT",
        "03": "SYN_RECV",
        "04": "FIN_WAIT1",
        "05": "FIN_WAIT2",
        "06": "TIME_WAIT",
        "07": "CLOSE",
        "08": "CLOSE_WAIT",
        "09": "LAST_ACK",
        "0A": "LISTEN",
        "0B": "CLOSING",
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
        self._output_generation = 0
        self._output_timeline_seconds = 0.0
        self._output_stderr_tail: deque[str] = deque(maxlen=20)
        self._output_stderr_lock = threading.Lock()

    def stream_video(
        self,
        video_id: str,
        title: str,
        start_position: float,
        seek_tolerance_seconds: float,
        duration: int,
        on_position: Callable[[float], None],
        stop_event: threading.Event,
        on_stream_health: Callable[[str, dict[str, str]], None] | None = None,
    ) -> StreamResult:
        """Stream a YouTube video to Twitch starting at start_position."""
        current_position = start_position
        max_output_retries = self.OUTPUT_RESTART_ATTEMPTS
        output_recovery_pending = False

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
                video_url, audio_url = YouTubeSync.get_stream_urls(
                    video_id,
                    stop_event=stop_event,
                )
            except (
                subprocess.CalledProcessError,
                subprocess.TimeoutExpired,
                OSError,
            ) as exc:
                err = self._subprocess_error_text(exc)
                self._stop_output_process()
                if stop_event.is_set():
                    return StreamResult(
                        success=True,
                        final_position=current_position,
                    )
                logger.error("Failed to resolve stream URL for %s: %s", video_id, err)
                return StreamResult(success=False, final_position=current_position, error=err)
            except RuntimeError as exc:
                self._stop_output_process()
                return StreamResult(success=False, final_position=current_position, error=str(exc))

            try:
                output_pipe, output_generation, output_offset = (
                    self._ensure_output_process()
                )
                cmd = self._build_input_ffmpeg_cmd(
                    video_url,
                    audio_url,
                    seek_pos,
                    output_offset,
                    output_pipe,
                )
            except (OSError, RuntimeError) as exc:
                logger.error("Failed to start RTMP output: %s", exc)
                self._stop_output_process()
                return StreamResult(
                    success=False,
                    final_position=current_position,
                    error=str(exc),
                )

            logger.debug(
                "FFmpeg input command: %s",
                self._redact_sensitive_text(" ".join(cmd)),
            )

            proc: subprocess.Popen[bytes] | None = None
            wall_start = time.monotonic()
            last_save = wall_start
            last_position = seek_pos
            saved_position = current_position
            last_progress_at = wall_start
            progress_seen = False
            position_lock = threading.Lock()
            stderr_tail: list[str] = []
            finished_naturally = False
            output_dropped = False
            input_stalled = False
            planned_output_cycle = False
            recovery_notified = False
            last_output_health_check = time.monotonic()
            unhealthy_output_checks = 0
            progress_thread: threading.Thread | None = None

            def set_position(position: float) -> None:
                nonlocal last_position, last_progress_at, progress_seen
                with position_lock:
                    if position > last_position + 0.05:
                        last_progress_at = time.monotonic()
                        progress_seen = True
                    last_position = max(last_position, position)

            def get_progress() -> tuple[float, float, bool]:
                with position_lock:
                    return last_position, last_progress_at, progress_seen

            def read_progress() -> None:
                if proc is None or proc.stderr is None:
                    return
                for raw_line in proc.stderr:
                    line = self._redact_sensitive_text(
                        raw_line.decode("utf-8", errors="replace").strip()
                    )
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

                while not stop_event.is_set():
                    if proc.poll() is not None:
                        break

                    now = time.monotonic()
                    progress_position, progress_at, has_progress = get_progress()

                    if self._output_dead.is_set():
                        output_dropped = True
                        diagnostic = self._output_process_summary()
                        health_position = max(
                            progress_position,
                            saved_position,
                            current_position,
                        )
                        logger.warning(
                            "RTMP output went away, restarting stream (%s)",
                            diagnostic,
                        )
                        self._notify_stream_health(
                            on_stream_health,
                            "RTMP output process exited unexpectedly.",
                            video_id,
                            health_position,
                            attempt,
                            max_output_retries,
                            diagnostic,
                        )
                        break

                    stalled_for = (
                        now - progress_at if has_progress else now - wall_start
                    )
                    stall_limit = (
                        self.INPUT_STALL_TIMEOUT_SECONDS
                        if has_progress
                        else self.INPUT_STARTUP_TIMEOUT_SECONDS
                    )
                    if stalled_for >= stall_limit:
                        input_stalled = True
                        output_dropped = True
                        diagnostic = (
                            f"no media progress for {stalled_for:.1f}s; "
                            f"{self._output_process_summary()}"
                        )
                        logger.warning(
                            "FFmpeg media progress stalled, restarting pipeline (%s)",
                            diagnostic,
                        )
                        self._notify_stream_health(
                            on_stream_health,
                            "FFmpeg media progress stalled.",
                            video_id,
                            max(progress_position, saved_position, current_position),
                            attempt,
                            max_output_retries,
                            diagnostic,
                        )
                        break

                    emitted_seconds = max(0.0, progress_position - seek_pos)
                    if (
                        output_offset + emitted_seconds
                        >= self.OUTPUT_MAX_TIMELINE_SECONDS
                    ):
                        planned_output_cycle = True
                        output_dropped = True
                        diagnostic = (
                            f"output timeline reached "
                            f"{output_offset + emitted_seconds:.1f}s"
                        )
                        logger.info(
                            "Cycling RTMP output before MPEG-TS timestamp wrap (%s)",
                            diagnostic,
                        )
                        self._notify_stream_health(
                            on_stream_health,
                            "RTMP output cycled before timestamp wrap.",
                            video_id,
                            max(progress_position, saved_position, current_position),
                            attempt,
                            max_output_retries,
                            diagnostic,
                        )
                        break

                    if (
                        now - last_output_health_check
                        >= self.OUTPUT_HEALTH_CHECK_INTERVAL
                    ):
                        last_output_health_check = now
                        health_position = max(
                            progress_position,
                            saved_position,
                            current_position,
                        )
                        if self._output_tcp_connection_closed():
                            unhealthy_output_checks += 1
                            if (
                                unhealthy_output_checks
                                >= self.OUTPUT_UNHEALTHY_CHECKS
                            ):
                                output_dropped = True
                                self._output_dead.set()
                                diagnostic = self._output_process_summary()
                                logger.warning(
                                    "RTMP output TCP connection unhealthy, "
                                    "restarting stream (%s)",
                                    diagnostic,
                                )
                                self._notify_stream_health(
                                    on_stream_health,
                                    "RTMP TCP connection unhealthy.",
                                    video_id,
                                    health_position,
                                    attempt,
                                    max_output_retries,
                                    diagnostic,
                                )
                                break
                        else:
                            unhealthy_output_checks = 0

                        if (
                            output_recovery_pending
                            and not recovery_notified
                            and self._output_started_at
                            and now - self._output_started_at
                            >= self.OUTPUT_STARTUP_GRACE_SECONDS
                        ):
                            self._notify_stream_health(
                                on_stream_health,
                                "RTMP connection recovered.",
                                video_id,
                                health_position,
                                attempt,
                                max_output_retries,
                                self._output_process_summary(),
                            )
                            output_recovery_pending = False
                            recovery_notified = True

                    if now - last_save >= self.config.save_interval:
                        position = progress_position
                        if duration > 0:
                            position = min(position, float(duration))
                        if position > saved_position + 0.05:
                            saved_position = position
                            on_position(position)
                        last_save = now
                        playback_logger.debug(
                            "Position saved: %.1fs / %ds",
                            position,
                            duration,
                        )

                    if duration > 0 and progress_position >= duration - 2:
                        playback_logger.info(
                            "Video near end (%.1fs), finishing",
                            progress_position,
                        )
                        finished_naturally = True
                        break

                    stop_event.wait(0.5)

                if proc.poll() is None:
                    if output_dropped or input_stalled or stop_event.is_set():
                        proc.kill()
                        proc.wait()
                    else:
                        proc.terminate()
                        try:
                            proc.wait(timeout=30)
                        except subprocess.TimeoutExpired:
                            proc.kill()
                            proc.wait()

                if progress_thread:
                    progress_thread.join(timeout=2)
                returncode = proc.returncode or 0
                progress_position, _, _ = get_progress()
                final_position = max(
                    progress_position,
                    saved_position,
                    current_position,
                )
                self._record_output_progress(
                    output_generation,
                    output_offset,
                    max(0.0, progress_position - seek_pos),
                )

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
                    return StreamResult(
                        success=True,
                        final_position=final_position,
                        completed=True,
                    )

                stderr_joined = " ".join(stderr_tail[-10:])
                output_failure = output_dropped or (
                    "Broken pipe" in stderr_joined
                    or "Input/output error" in stderr_joined
                    or "Connection reset by peer" in stderr_joined
                )
                premature_eof = (
                    returncode == 0
                    and duration > 0
                    and final_position < duration - 5
                )
                transient_input_failure = returncode in (-9, 255) or premature_eof or (
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
                    output_recovery_pending = not planned_output_cycle
                    # Every retry gets a fresh publisher generation. Otherwise the
                    # new input would reset PTS inside an already-running MPEG-TS
                    # timeline and the output would drop packets indefinitely.
                    self._stop_output_process()
                    if stop_event.wait(2):
                        return StreamResult(
                            success=True,
                            final_position=current_position,
                        )
                    continue

                if output_failure or transient_input_failure or returncode != 0:
                    err_tail = f": {' | '.join(stderr_tail[-5:])}" if stderr_tail else ""
                    logger.warning("FFmpeg exited with code %d", returncode)
                    self._stop_output_process()
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
                return StreamResult(
                    success=True,
                    final_position=final_position,
                    completed=True,
                )

            except Exception as exc:
                logger.exception("Stream error for %s", video_id)
                if proc and proc.poll() is None:
                    proc.kill()
                    proc.wait()
                if progress_thread:
                    progress_thread.join(timeout=2)
                progress_position, _, _ = get_progress()
                current_position = max(progress_position, current_position)
                # An arbitrary callback/DB/OS exception may have interrupted the
                # writer after it emitted media. Reusing the publisher with the
                # old generation offset would make the next writer jump backward.
                self._stop_output_process()
                if attempt < max_output_retries and (
                    "RTMP output process" in str(exc)
                    or self._output_dead.is_set()
                ):
                    logger.warning(
                        "Retrying %s after output failure at %.1fs",
                        video_id,
                        current_position,
                    )
                    if stop_event.wait(2):
                        return StreamResult(
                            success=True,
                            final_position=current_position,
                        )
                    continue
                return StreamResult(
                    success=False,
                    final_position=current_position,
                    error=str(exc),
                )

        return StreamResult(
            success=False,
            final_position=current_position,
            error="RTMP output dropped repeatedly",
        )

    def close(self) -> None:
        self._stop_output_process()

    def _ensure_output_process(self) -> tuple[str, int, float]:
        with self._output_lock:
            if self._output_proc and self._output_proc.poll() is None:
                return (
                    str(self._fifo_path),
                    self._output_generation,
                    self._output_timeline_seconds,
                )

            self._ensure_fifo()
            self._closing.clear()
            self._output_dead.clear()
            with self._output_stderr_lock:
                self._output_stderr_tail.clear()
            self._output_proc = subprocess.Popen(
                self._build_output_ffmpeg_cmd(),
                stderr=subprocess.PIPE,
                bufsize=0,
            )
            self._output_started_at = time.monotonic()
            self._output_generation += 1
            self._output_timeline_seconds = 0.0
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

            playback_logger.info(
                "Persistent RTMP output started (pid=%s)",
                self._output_proc.pid,
            )
            return (
                str(self._fifo_path),
                self._output_generation,
                self._output_timeline_seconds,
            )

    def _stop_output_process(self) -> None:
        self._closing.set()
        with self._output_lock:
            proc = self._output_proc
            self._output_proc = None
            self._output_timeline_seconds = 0.0
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

    def _record_output_progress(
        self,
        generation: int,
        starting_offset: float,
        emitted_seconds: float,
    ) -> None:
        """Advance only the timeline belonging to the active RTMP process."""
        with self._output_lock:
            if generation != self._output_generation or not self._output_proc:
                return
            if self._output_proc.poll() is not None:
                return
            self._output_timeline_seconds = max(
                self._output_timeline_seconds,
                starting_offset + max(0.0, emitted_seconds),
            )

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
            line = self._redact_sensitive_text(
                raw_line.decode("utf-8", errors="replace").strip()
            )
            if line:
                with self._output_stderr_lock:
                    self._output_stderr_tail.append(line)
                logger.warning("RTMP output: %s", line)

        if self._output_proc is proc and not self._closing.is_set():
            self._output_dead.set()
            logger.error(
                "RTMP output process exited unexpectedly (%s)",
                self._output_process_summary(proc),
            )

    def _output_tcp_connection_closed(self) -> bool:
        proc = self._output_proc
        if not proc:
            logger.warning("RTMP output process missing")
            return True
        if proc.poll() is not None:
            logger.warning(
                "RTMP output process already exited during TCP health check (%s)",
                self._output_process_summary(proc),
            )
            return True
        if (
            self._output_started_at
            and time.monotonic() - self._output_started_at
            < self.OUTPUT_STARTUP_GRACE_SECONDS
        ):
            return False

        socket_inodes = self._output_socket_inodes(proc)
        if socket_inodes is None:
            logger.warning(
                "Could not inspect RTMP output process sockets (%s)",
                self._output_process_summary(proc),
            )
            return True

        if not socket_inodes:
            logger.warning(
                "RTMP output process has no socket file descriptors (%s)",
                self._output_process_summary(proc),
            )
            return True

        sockets = self._output_tcp_sockets(socket_inodes)
        rtmp_sockets = [
            socket_info
            for socket_info in sockets
            if socket_info.remote.rsplit(":", 1)[-1] == "1935"
        ]
        for socket_info in rtmp_sockets:
            if socket_info.state in self.TCP_CLOSED_STATES:
                logger.warning(
                    "RTMP output socket entered closed TCP state: %s (%s)",
                    self._format_socket_info(socket_info),
                    self._output_process_summary(proc),
                )
                return True

        if not rtmp_sockets:
            logger.warning(
                "RTMP output has no Twitch TCP socket (inodes=%s, %s)",
                ",".join(sorted(socket_inodes)),
                self._output_process_summary(proc),
            )
            return True
        if not any(socket_info.state == "01" for socket_info in rtmp_sockets):
            logger.warning(
                "RTMP output has no established Twitch TCP socket (%s)",
                self._output_process_summary(proc),
            )
            return True

        return False

    def _output_socket_inodes(self, proc: subprocess.Popen[bytes]) -> set[str] | None:
        socket_inodes: set[str] = set()
        fd_dir = f"/proc/{proc.pid}/fd"
        try:
            fd_names = os.listdir(fd_dir)
        except OSError:
            return None

        for fd_name in fd_names:
            try:
                target = os.readlink(os.path.join(fd_dir, fd_name))
            except OSError:
                continue
            match = re.fullmatch(r"socket:\[(\d+)\]", target)
            if match:
                socket_inodes.add(match.group(1))

        return socket_inodes

    def _output_tcp_sockets(self, socket_inodes: set[str]) -> list[TcpSocketInfo]:
        sockets: list[TcpSocketInfo] = []
        for table in ("/proc/net/tcp", "/proc/net/tcp6"):
            try:
                with open(table, "r", encoding="utf-8") as handle:
                    next(handle, None)
                    for line in handle:
                        fields = line.split()
                        if len(fields) <= 9 or fields[9] not in socket_inodes:
                            continue

                        tx_queue, _, rx_queue = fields[4].partition(":")
                        sockets.append(
                            TcpSocketInfo(
                                table=table.rsplit("/", 1)[-1],
                                local=self._decode_proc_net_address(fields[1], table),
                                remote=self._decode_proc_net_address(fields[2], table),
                                state=fields[3],
                                inode=fields[9],
                                tx_queue=tx_queue,
                                rx_queue=rx_queue,
                            )
                        )
            except OSError:
                continue

        return sockets

    def _output_process_summary(
        self,
        proc: subprocess.Popen[bytes] | None = None,
    ) -> str:
        proc = proc or self._output_proc
        if not proc:
            return "pid=none"

        returncode = proc.poll()
        runtime = (
            time.monotonic() - self._output_started_at
            if self._output_started_at
            else 0.0
        )
        socket_summary = self._current_output_socket_summary(proc)
        stderr_tail = self._output_stderr_tail_summary()
        return (
            f"pid={proc.pid}, returncode={returncode}, runtime={runtime:.1f}s, "
            f"sockets={socket_summary}, stderr_tail={stderr_tail}"
        )

    def _current_output_socket_summary(self, proc: subprocess.Popen[bytes]) -> str:
        socket_inodes = self._output_socket_inodes(proc)
        if socket_inodes is None:
            return "uninspectable"
        if not socket_inodes:
            return "none"

        sockets = self._output_tcp_sockets(socket_inodes)
        if not sockets:
            return f"not-in-tcp-table(inodes={','.join(sorted(socket_inodes))})"

        return "; ".join(self._format_socket_info(socket) for socket in sockets)

    def _output_stderr_tail_summary(self) -> str:
        with self._output_stderr_lock:
            tail = list(self._output_stderr_tail)[-5:]
        if not tail:
            return "none"
        return " | ".join(tail)

    def _format_socket_info(self, socket_info: TcpSocketInfo) -> str:
        state = self.TCP_STATE_NAMES.get(socket_info.state, socket_info.state)
        return (
            f"{socket_info.table}:{socket_info.local}->{socket_info.remote} "
            f"state={state} inode={socket_info.inode} "
            f"tx={socket_info.tx_queue} rx={socket_info.rx_queue}"
        )

    @staticmethod
    def _decode_proc_net_address(value: str, table: str) -> str:
        address_hex, _, port_hex = value.partition(":")
        port = int(port_hex, 16)

        if table.endswith("tcp"):
            octets = [
                str(int(address_hex[index : index + 2], 16))
                for index in range(6, -1, -2)
            ]
            return f"{'.'.join(octets)}:{port}"

        if table.endswith("tcp6"):
            groups = [
                address_hex[index : index + 4]
                for index in range(0, len(address_hex), 4)
            ]
            return f"{':'.join(groups)}:{port}"

        return value

    @staticmethod
    def _stop_event_set(stop_event: threading.Event) -> bool:
        return stop_event.is_set()

    @classmethod
    def _redact_sensitive_text(cls, value: str) -> str:
        value = cls.RTMP_URL_RE.sub(r"\1[REDACTED]", value)
        return cls.HTTP_URL_RE.sub("[source-url]", value)

    @classmethod
    def _subprocess_error_text(cls, exc: BaseException) -> str:
        for attribute in ("stderr", "stdout"):
            value = getattr(exc, attribute, None)
            if value:
                if isinstance(value, bytes):
                    value = value.decode("utf-8", errors="replace")
                return cls._redact_sensitive_text(str(value).strip())
        return cls._redact_sensitive_text(str(exc))

    @staticmethod
    def _notify_stream_health(
        callback: Callable[[str, dict[str, str]], None] | None,
        message: str,
        video_id: str,
        position: float,
        attempt: int,
        max_attempts: int,
        diagnostic: str,
    ) -> None:
        if not callback:
            return

        try:
            callback(
                message,
                {
                    "Video ID": video_id,
                    "Position": f"{position:.1f}s",
                    "Attempt": f"{attempt}/{max_attempts}",
                    "Diagnostic": diagnostic,
                },
            )
        except Exception:
            # Notifications are optional and must never interrupt RTMP recovery.
            logger.exception("Stream-health notification failed")

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
            "-probesize",
            "10M",
            "-analyzeduration",
            "10M",
            "-f",
            "mpegts",
            "-i",
            str(self._fifo_path),
            "-map",
            "0:v:0",
            "-map",
            "0:a:0?",
            "-c:v",
            "copy",
            "-c:a",
            "aac",
            "-b:a",
            self.config.audio_bitrate,
            "-ar",
            "48000",
            "-ac",
            "2",
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
        output_offset_seconds: float,
        output_pipe: str,
    ) -> list[str]:
        cfg = self.config
        video_filter = (
            f"scale={self.OUTPUT_WIDTH}:{self.OUTPUT_HEIGHT}:"
            "force_original_aspect_ratio=decrease,"
            f"pad={self.OUTPUT_WIDTH}:{self.OUTPUT_HEIGHT}:(ow-iw)/2:(oh-ih)/2,"
            f"fps={self.OUTPUT_FPS},setpts=PTS-STARTPTS,format=yuv420p"
        )
        ts_offset = max(0.0, output_offset_seconds)

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
                cfg.encoder_preset,
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
                "+resend_headers+initial_discontinuity",
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
            cfg.encoder_preset,
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
            "+resend_headers+initial_discontinuity",
            "-muxdelay",
            "0",
            "-muxpreload",
            "0",
            output_pipe,
        ]
