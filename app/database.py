"""SQLite database operations for Twitch247."""

from __future__ import annotations

import sqlite3
from contextlib import contextmanager
from dataclasses import dataclass
from datetime import datetime, timezone
from pathlib import Path
from typing import Any, Generator, Iterator


def utc_now() -> str:
    return datetime.now(timezone.utc).strftime("%Y-%m-%d %H:%M:%S")


@dataclass
class Video:
    video_id: str
    title: str
    duration: int
    upload_date: str | None
    played_status: str
    current_position_seconds: float
    last_played_timestamp: str | None


@dataclass
class PlaybackState:
    current_video_id: str | None
    current_position_seconds: float
    stream_started_at: str | None
    last_save_at: str | None
    uptime_started_at: str
    last_error: str | None
    last_error_at: str | None
    is_streaming: bool


@dataclass
class DashboardStats:
    total_videos: int
    unplayed: int
    playing: int
    played: int


class Database:
    def __init__(self, db_path: Path, schema_path: Path | None = None) -> None:
        self.db_path = db_path
        self.schema_path = schema_path
        db_path.parent.mkdir(parents=True, exist_ok=True)
        self._init_schema()

    @contextmanager
    def _connect(self) -> Generator[sqlite3.Connection, None, None]:
        conn = sqlite3.connect(self.db_path, timeout=30)
        conn.row_factory = sqlite3.Row
        conn.execute("PRAGMA foreign_keys=ON")
        try:
            yield conn
            conn.commit()
        except Exception:
            conn.rollback()
            raise
        finally:
            conn.close()

    def _init_schema(self) -> None:
        if self.schema_path and self.schema_path.is_file():
            schema = self.schema_path.read_text(encoding="utf-8")
        else:
            default = Path(__file__).resolve().parent.parent / "database" / "schema.sql"
            schema = default.read_text(encoding="utf-8") if default.is_file() else ""

        if not schema:
            raise FileNotFoundError("SQLite schema file not found")

        with self._connect() as conn:
            conn.executescript(schema)

    def upsert_video(
        self,
        video_id: str,
        title: str,
        duration: int,
        upload_date: str | None,
    ) -> bool:
        """Insert new video. Returns True if newly inserted."""
        with self._connect() as conn:
            existing = conn.execute(
                "SELECT video_id FROM videos WHERE video_id = ?",
                (video_id,),
            ).fetchone()
            if existing:
                conn.execute(
                    """
                    UPDATE videos
                    SET title = ?, duration = ?, upload_date = ?, updated_at = ?
                    WHERE video_id = ?
                    """,
                    (title, duration, upload_date, utc_now(), video_id),
                )
                return False

            conn.execute(
                """
                INSERT INTO videos (video_id, title, duration, upload_date)
                VALUES (?, ?, ?, ?)
                """,
                (video_id, title, duration, upload_date),
            )
            return True

    def get_video(self, video_id: str) -> Video | None:
        with self._connect() as conn:
            row = conn.execute(
                "SELECT * FROM videos WHERE video_id = ?", (video_id,)
            ).fetchone()
            return self._row_to_video(row) if row else None

    def get_video_index(self) -> dict[str, Video]:
        """Return all known videos keyed by YouTube video ID."""
        with self._connect() as conn:
            rows = conn.execute("SELECT * FROM videos").fetchall()
            return {row["video_id"]: self._row_to_video(row) for row in rows}

    def prune_videos(self, keep_video_ids: set[str]) -> int:
        """Delete videos not present in keep_video_ids. Returns deleted count."""
        with self._connect() as conn:
            current_row = conn.execute(
                "SELECT current_video_id FROM playback_state WHERE id = 1"
            ).fetchone()
            current_video_id = current_row["current_video_id"] if current_row else None

            ids_to_keep = set(keep_video_ids)
            if current_video_id:
                ids_to_keep.add(current_video_id)

            if not ids_to_keep:
                conn.execute(
                    "UPDATE playback_state SET current_video_id = NULL WHERE id = 1"
                )
                deleted = conn.execute("DELETE FROM videos").rowcount or 0
                return deleted

            placeholders = ",".join("?" for _ in ids_to_keep)
            deleted = conn.execute(
                f"DELETE FROM videos WHERE video_id NOT IN ({placeholders})",
                tuple(ids_to_keep),
            ).rowcount or 0

            if current_video_id and current_video_id not in ids_to_keep:
                conn.execute(
                    """
                    UPDATE playback_state
                    SET current_video_id = NULL,
                        current_position_seconds = 0
                    WHERE id = 1
                    """,
                )

            return deleted

    def get_next_video(self) -> Video | None:
        """Prefer interrupted videos, then unplayed, then oldest by upload_date."""
        with self._connect() as conn:
            row = conn.execute(
                """
                SELECT * FROM videos
                WHERE played_status = 'playing'
                ORDER BY last_played_timestamp DESC
                LIMIT 1
                """
            ).fetchone()
            if row:
                return self._row_to_video(row)

            row = conn.execute(
                """
                SELECT * FROM videos
                WHERE played_status = 'unplayed'
                ORDER BY upload_date ASC NULLS LAST, discovered_at ASC
                LIMIT 1
                """
            ).fetchone()
            if row:
                return self._row_to_video(row)

            count = conn.execute("SELECT COUNT(*) FROM videos").fetchone()[0]
            if count == 0:
                return None

            conn.execute(
                """
                UPDATE videos
                SET played_status = 'unplayed',
                    current_position_seconds = 0,
                    updated_at = ?
                WHERE played_status = 'played'
                """,
                (utc_now(),),
            )

            row = conn.execute(
                """
                SELECT * FROM videos
                ORDER BY upload_date ASC NULLS LAST, discovered_at ASC
                LIMIT 1
                """
            ).fetchone()
            return self._row_to_video(row) if row else None

    def set_video_status(
        self,
        video_id: str,
        played_status: str,
        position: float | None = None,
    ) -> None:
        with self._connect() as conn:
            if position is not None:
                conn.execute(
                    """
                    UPDATE videos
                    SET played_status = ?,
                        current_position_seconds = ?,
                        last_played_timestamp = ?,
                        updated_at = ?
                    WHERE video_id = ?
                    """,
                    (played_status, position, utc_now(), utc_now(), video_id),
                )
            else:
                conn.execute(
                    """
                    UPDATE videos
                    SET played_status = ?,
                        last_played_timestamp = ?,
                        updated_at = ?
                    WHERE video_id = ?
                    """,
                    (played_status, utc_now(), utc_now(), video_id),
                )

    def save_position(self, video_id: str, position: float) -> None:
        now = utc_now()
        with self._connect() as conn:
            conn.execute(
                """
                UPDATE videos
                SET current_position_seconds = ?,
                    last_played_timestamp = ?,
                    updated_at = ?
                WHERE video_id = ?
                """,
                (position, now, now, video_id),
            )
            conn.execute(
                """
                UPDATE playback_state
                SET current_video_id = ?,
                    current_position_seconds = ?,
                    last_save_at = ?
                WHERE id = 1
                """,
                (video_id, position, now),
            )

    def get_playback_state(self) -> PlaybackState:
        with self._connect() as conn:
            row = conn.execute("SELECT * FROM playback_state WHERE id = 1").fetchone()
            return PlaybackState(
                current_video_id=row["current_video_id"],
                current_position_seconds=row["current_position_seconds"],
                stream_started_at=row["stream_started_at"],
                last_save_at=row["last_save_at"],
                uptime_started_at=row["uptime_started_at"],
                last_error=row["last_error"],
                last_error_at=row["last_error_at"],
                is_streaming=bool(row["is_streaming"]),
            )

    def set_streaming(
        self,
        is_streaming: bool,
        video_id: str | None = None,
        reset_stream_timer: bool = False,
    ) -> None:
        now = utc_now()
        with self._connect() as conn:
            if is_streaming:
                if reset_stream_timer:
                    conn.execute(
                        """
                        UPDATE playback_state
                        SET is_streaming = 1,
                            current_video_id = ?,
                            stream_started_at = ?
                        WHERE id = 1
                        """,
                        (video_id, now),
                    )
                else:
                    conn.execute(
                        """
                        UPDATE playback_state
                        SET is_streaming = 1,
                            current_video_id = ?,
                            stream_started_at = COALESCE(stream_started_at, ?)
                        WHERE id = 1
                        """,
                        (video_id, now),
                    )
            else:
                conn.execute(
                    "UPDATE playback_state SET is_streaming = 0 WHERE id = 1"
                )

    def set_error(self, error: str | None) -> None:
        with self._connect() as conn:
            conn.execute(
                """
                UPDATE playback_state
                SET last_error = ?, last_error_at = ?
                WHERE id = 1
                """,
                (error, utc_now() if error else None),
            )

    def log_event(self, event_type: str, message: str) -> None:
        with self._connect() as conn:
            conn.execute(
                "INSERT INTO event_log (event_type, message) VALUES (?, ?)",
                (event_type, message),
            )

    def log_sync(self, new_videos: int, total_videos: int) -> None:
        with self._connect() as conn:
            conn.execute(
                "INSERT INTO sync_log (new_videos, total_videos) VALUES (?, ?)",
                (new_videos, total_videos),
            )

    def get_stats(self) -> DashboardStats:
        with self._connect() as conn:
            total = conn.execute("SELECT COUNT(*) FROM videos").fetchone()[0]
            unplayed = conn.execute(
                "SELECT COUNT(*) FROM videos WHERE played_status = 'unplayed'"
            ).fetchone()[0]
            playing = conn.execute(
                "SELECT COUNT(*) FROM videos WHERE played_status = 'playing'"
            ).fetchone()[0]
            played = conn.execute(
                "SELECT COUNT(*) FROM videos WHERE played_status = 'played'"
            ).fetchone()[0]
            return DashboardStats(
                total_videos=total,
                unplayed=unplayed,
                playing=playing,
                played=played,
            )

    def list_videos(self, limit: int = 50) -> list[Video]:
        with self._connect() as conn:
            rows = conn.execute(
                """
                SELECT * FROM videos
                ORDER BY upload_date DESC NULLS LAST
                LIMIT ?
                """,
                (limit,),
            ).fetchall()
            return [self._row_to_video(r) for r in rows]

    def get_resume_video(self) -> Video | None:
        """Load video to resume after service restart."""
        state = self.get_playback_state()
        if state.current_video_id:
            video = self.get_video(state.current_video_id)
            if video and video.played_status != "played":
                return video
        return None

    @staticmethod
    def _row_to_video(row: sqlite3.Row) -> Video:
        return Video(
            video_id=row["video_id"],
            title=row["title"],
            duration=row["duration"],
            upload_date=row["upload_date"],
            played_status=row["played_status"],
            current_position_seconds=row["current_position_seconds"],
            last_played_timestamp=row["last_played_timestamp"],
        )
