"""YouTube channel discovery via yt-dlp."""

from __future__ import annotations

import json
import os
import subprocess
import sys
import threading
import time
from concurrent.futures import ThreadPoolExecutor, as_completed
from dataclasses import dataclass

from app.database import Database
from app.logging_setup import get_logger

logger = get_logger("youtube")


@dataclass
class YouTubeVideo:
    video_id: str
    title: str
    duration: int
    upload_date: str | None


class UpcomingLiveEvent(RuntimeError):
    """Raised when YouTube reports that a live event is scheduled for the future."""


class UnplayableYouTubeVideo(RuntimeError):
    """Raised when YouTube refuses a video that should not block playback."""


class YouTubeSync:
    DEFAULT_METADATA_WORKERS = 4
    DEFAULT_SYNC_LIMIT = 15
    YT_DLP_BASE = [
        sys.executable,
        "-m",
        "yt_dlp",
        "--no-warnings",
        "--no-playlist",
        "--force-ipv4",
        "--socket-timeout",
        "30",
    ]

    def __init__(self, channel_url: str, db: Database) -> None:
        self.channel_url = channel_url
        self.db = db
        self.metadata_workers = max(
            1,
            int(os.getenv("YOUTUBE_METADATA_WORKERS", str(self.DEFAULT_METADATA_WORKERS))),
        )
        self.sync_limit = max(
            0,
            int(os.getenv("YOUTUBE_SYNC_LIMIT", str(self.DEFAULT_SYNC_LIMIT))),
        )

    @classmethod
    def _yt_dlp_cmd(cls) -> list[str]:
        cmd = [*cls.YT_DLP_BASE]
        cookies_file = os.getenv("YOUTUBE_COOKIES_FILE", "").strip()
        cookies_from_browser = os.getenv("YOUTUBE_COOKIES_FROM_BROWSER", "").strip()
        if cookies_file:
            cmd.extend(["--cookies", cookies_file])
        if cookies_from_browser:
            cmd.extend(["--cookies-from-browser", cookies_from_browser])
        return cmd

    def sync(self) -> int:
        """Discover videos and update database. Returns count of new videos."""
        logger.info("Syncing YouTube channel: %s", self.channel_url)
        new_count = 0

        try:
            videos = self._fetch_channel_videos()
        except subprocess.CalledProcessError as exc:
            logger.error("yt-dlp channel sync failed: %s", exc.stderr or exc)
            raise

        for video in videos:
            if self.db.upsert_video(
                video.video_id,
                video.title,
                video.duration,
                video.upload_date,
            ):
                new_count += 1
                logger.info("New video discovered: %s (%s)", video.title, video.video_id)

        if videos:
            deleted_count = self.db.prune_videos(
                {video.video_id for video in videos}
            )
        else:
            # An empty yt-dlp response is often a transient extraction/API
            # failure. Never erase the known playback queue on that signal.
            deleted_count = 0
            logger.warning("YouTube sync returned no usable videos; keeping local queue")
        stats = self.db.get_stats()
        self.db.log_sync(new_count, stats.total_videos)
        logger.info(
            "Sync complete: %d new, %d deleted, %d total videos",
            new_count,
            deleted_count,
            stats.total_videos,
        )
        return new_count

    def _fetch_channel_videos(self) -> list[YouTubeVideo]:
        cmd = [
            *self._yt_dlp_cmd(),
            "--flat-playlist",
            "--dump-single-json",
            self.channel_url,
        ]
        result = subprocess.run(
            cmd,
            capture_output=True,
            text=True,
            check=True,
            timeout=600,
        )
        data = json.loads(result.stdout)

        entries = data.get("entries") or []
        if not entries and data.get("id"):
            entries = [data]

        if self.sync_limit > 0:
            entries = entries[: self.sync_limit]

        known_videos = self.db.get_video_index()
        videos: list[YouTubeVideo] = []
        metadata_needed: dict[str, YouTubeVideo] = {}

        for entry in entries:
            if not entry:
                continue
            video_id = entry.get("id") or entry.get("url", "").split("=")[-1]
            if not video_id or len(video_id) != 11:
                continue

            known = known_videos.get(video_id)
            title = entry.get("title") or "Untitled"
            duration = self._parse_duration(entry.get("duration"))
            duration = duration or (known.duration if known else 0)
            upload_date = entry.get("upload_date") or (known.upload_date if known else None)

            video = YouTubeVideo(
                video_id=video_id,
                title=title,
                duration=duration,
                upload_date=upload_date,
            )
            videos.append(video)

            if known and known.duration > 0 and known.upload_date:
                continue

            if duration == 0 or not upload_date:
                metadata_needed[video_id] = video

        if metadata_needed:
            logger.info(
                "Fetching detailed metadata for %d videos with %d workers",
                len(metadata_needed),
                self.metadata_workers,
            )
            videos_by_id = {video.video_id: video for video in videos}
            with ThreadPoolExecutor(max_workers=self.metadata_workers) as executor:
                futures = {
                    executor.submit(self._fetch_video_metadata, video_id): video_id
                    for video_id in metadata_needed
                }
                for future in as_completed(futures):
                    video_id = futures[future]
                    try:
                        meta = future.result()
                    except UpcomingLiveEvent as exc:
                        logger.info("Skipping unavailable YouTube video %s: %s", video_id, exc)
                        videos_by_id.pop(video_id, None)
                        continue
                    except UnplayableYouTubeVideo as exc:
                        logger.warning(
                            "Keeping YouTube video %s despite metadata auth error: %s",
                            video_id,
                            exc,
                        )
                        continue
                    except Exception as exc:
                        logger.warning("Could not fetch metadata for %s: %s", video_id, exc)
                        continue
                    if not meta:
                        continue

                    current = videos_by_id[video_id]
                    videos_by_id[video_id] = YouTubeVideo(
                        video_id=video_id,
                        title=meta.title or current.title,
                        duration=meta.duration or current.duration,
                        upload_date=meta.upload_date or current.upload_date,
                    )
            videos = [
                videos_by_id[video.video_id]
                for video in videos
                if video.video_id in videos_by_id
            ]

        return videos

    def _fetch_video_metadata(self, video_id: str) -> YouTubeVideo | None:
        url = f"https://www.youtube.com/watch?v={video_id}"
        cmd = [
            *self._yt_dlp_cmd(),
            "--skip-download",
            "--print",
            "%(title)s\t%(duration)s\t%(upload_date)s",
            url,
        ]
        try:
            result = subprocess.run(
                cmd,
                capture_output=True,
                text=True,
                check=True,
                timeout=120,
            )
            parts = result.stdout.strip().split("\t")
            if len(parts) >= 3:
                return YouTubeVideo(
                    video_id=video_id,
                    title=parts[0],
                    duration=self._parse_duration(parts[1]),
                    upload_date=parts[2] or None,
                )
        except subprocess.CalledProcessError as exc:
            err = exc.stderr or exc.stdout or str(exc)
            if YouTubeSync.is_upcoming_live_error(err):
                raise UpcomingLiveEvent(err.strip()) from exc
            if YouTubeSync.is_youtube_auth_error(err):
                raise UnplayableYouTubeVideo(err.strip()) from exc
            logger.warning("Could not fetch metadata for %s: %s", video_id, exc)
        except (ValueError, subprocess.TimeoutExpired) as exc:
            logger.warning("Could not fetch metadata for %s: %s", video_id, exc)
        return None

    @staticmethod
    def _parse_duration(value: object) -> int:
        if value is None:
            return 0
        normalized = str(value).strip()
        if not normalized or normalized.upper() in {"NA", "N/A", "NONE", "NULL"}:
            return 0
        try:
            return max(0, int(float(normalized)))
        except (TypeError, ValueError):
            return 0

    @staticmethod
    def is_upcoming_live_error(error: str) -> bool:
        normalized = error.lower()
        return (
            "this live event will begin" in normalized
            or "premieres in" in normalized
            or "premiere will begin" in normalized
        )

    @staticmethod
    def is_youtube_auth_error(error: str) -> bool:
        normalized = error.lower()
        return (
            "sign in to confirm" in normalized
            and "not a bot" in normalized
        ) or "use --cookies-from-browser or --cookies" in normalized

    @staticmethod
    def get_stream_urls(
        video_id: str,
        stop_event: threading.Event | None = None,
        timeout: float = 120.0,
    ) -> tuple[str, str | None]:
        """
        Resolve direct stream URL(s) via yt-dlp without downloading.
        Returns (primary_url, audio_url_or_none).
        """
        url = f"https://www.youtube.com/watch?v={video_id}"
        cmd = [
            *YouTubeSync._yt_dlp_cmd(),
            "-g",
            "-f",
            "bestvideo[height<=1080][ext=mp4]+bestaudio[ext=m4a]/"
            "bestvideo[height<=1080]+bestaudio/best[height<=1080]/best",
            url,
        ]
        proc = subprocess.Popen(
            cmd,
            stdout=subprocess.PIPE,
            stderr=subprocess.PIPE,
            text=True,
        )
        deadline = time.monotonic() + timeout
        while True:
            if stop_event and stop_event.is_set():
                proc.terminate()
                try:
                    proc.communicate(timeout=5)
                except subprocess.TimeoutExpired:
                    proc.kill()
                    proc.communicate()
                raise InterruptedError("Stream URL resolution interrupted")

            remaining = deadline - time.monotonic()
            if remaining <= 0:
                proc.kill()
                stdout, stderr = proc.communicate()
                raise subprocess.TimeoutExpired(
                    cmd,
                    timeout,
                    output=stdout,
                    stderr=stderr,
                )

            try:
                stdout, stderr = proc.communicate(timeout=min(1.0, remaining))
                break
            except subprocess.TimeoutExpired:
                continue

        if proc.returncode:
            raise subprocess.CalledProcessError(
                proc.returncode,
                cmd,
                output=stdout,
                stderr=stderr,
            )

        lines = [ln.strip() for ln in stdout.strip().split("\n") if ln.strip()]
        if not lines:
            raise RuntimeError(f"No stream URL returned for {video_id}")

        if len(lines) >= 2:
            return lines[0], lines[1]
        return lines[0], None
