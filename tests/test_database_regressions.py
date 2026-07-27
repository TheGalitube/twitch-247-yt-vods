from __future__ import annotations

import tempfile
import unittest
from pathlib import Path

from app.database import Database


SCHEMA_PATH = Path(__file__).resolve().parents[1] / "database" / "schema.sql"


class DatabaseResumeRegressionTests(unittest.TestCase):
    def setUp(self) -> None:
        self.temporary_directory = tempfile.TemporaryDirectory()
        self.addCleanup(self.temporary_directory.cleanup)
        self.db = Database(
            Path(self.temporary_directory.name) / "twitch247-test.db",
            SCHEMA_PATH,
        )

    def _add_video(
        self,
        video_id: str,
        *,
        upload_date: str | None,
        status: str = "unplayed",
        position: float = 0.0,
    ) -> None:
        self.db.upsert_video(
            video_id=video_id,
            title=video_id,
            duration=3_600,
            upload_date=upload_date,
        )
        self.db.set_video_status(video_id, status, position)

    def test_undated_unplayed_video_does_not_discard_dated_resume(self) -> None:
        current_id = "current00001"
        self._add_video(
            current_id,
            upload_date="20260720",
            status="playing",
            position=123.5,
        )
        self.db.save_position(current_id, 123.5)
        self._add_video("undated0001", upload_date=None)

        resumed = self.db.get_resume_video()
        selected = self.db.get_next_video()

        self.assertIsNotNone(resumed)
        self.assertEqual(current_id, resumed.video_id)
        self.assertEqual(123.5, resumed.current_position_seconds)
        self.assertIsNotNone(selected)
        self.assertEqual(current_id, selected.video_id)
        self.assertEqual(123.5, selected.current_position_seconds)

        persisted = self.db.get_video(current_id)
        self.assertIsNotNone(persisted)
        self.assertEqual("playing", persisted.played_status)
        self.assertEqual(123.5, persisted.current_position_seconds)

    def test_preempted_resume_keeps_its_checkpoint(self) -> None:
        current_id = "current00001"
        older_id = "older000001"
        self._add_video(
            current_id,
            upload_date="20260720",
            status="playing",
            position=987.25,
        )
        self.db.save_position(current_id, 987.25)
        self._add_video(older_id, upload_date="20260719")

        self.assertIsNone(self.db.get_resume_video())
        selected = self.db.get_next_video()

        self.assertIsNotNone(selected)
        self.assertEqual(older_id, selected.video_id)

        interrupted = self.db.get_video(current_id)
        self.assertIsNotNone(interrupted)
        self.assertEqual("unplayed", interrupted.played_status)
        self.assertEqual(987.25, interrupted.current_position_seconds)

    def test_prune_never_deletes_playing_row_before_state_update(self) -> None:
        playing_id = "playing0001"
        retained_id = "retained001"
        self._add_video(
            playing_id,
            upload_date="20260720",
            status="playing",
            position=12.0,
        )
        self._add_video(retained_id, upload_date="20260721")

        self.db.prune_videos({retained_id})

        playing = self.db.get_video(playing_id)
        self.assertIsNotNone(playing)
        self.assertEqual("playing", playing.played_status)
        self.assertEqual(12.0, playing.current_position_seconds)


if __name__ == "__main__":
    unittest.main()
