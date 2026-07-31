from __future__ import annotations

import json
import subprocess
import unittest
from types import SimpleNamespace
from unittest.mock import Mock, patch

from app.youtube_sync import UpcomingLiveEvent, YouTubeSync


class YouTubeDurationRegressionTests(unittest.TestCase):
    def setUp(self) -> None:
        self.db = Mock()
        self.db.get_video_index.return_value = {}
        with patch.dict(
            "os.environ",
            {
                "YOUTUBE_METADATA_WORKERS": "1",
                "YOUTUBE_SYNC_LIMIT": "15",
            },
        ):
            self.sync = YouTubeSync("https://example.test/channel", self.db)

    def test_parse_duration_treats_unavailable_values_as_zero(self) -> None:
        for value in (None, "", "NA", "N/A", "none", "NULL", "not-a-number", -3):
            with self.subTest(value=value):
                self.assertEqual(0, self.sync._parse_duration(value))

        self.assertEqual(123, self.sync._parse_duration("123.9"))
        self.assertEqual(456, self.sync._parse_duration(456))

    def test_sync_excludes_24_7_title_from_upsert_and_prune_keep_set(self) -> None:
        included = SimpleNamespace(
            video_id="included001",
            title="Regular archive",
            duration=120,
            upload_date="20260730",
        )
        excluded = SimpleNamespace(
            video_id="excluded001",
            title="TheGalitube 24/7 Live",
            duration=0,
            upload_date="20260731",
        )
        self.db.upsert_video.return_value = True
        self.db.get_stats.return_value = SimpleNamespace(total_videos=1)

        with patch.object(
            self.sync,
            "_fetch_channel_videos",
            return_value=[included, excluded],
        ):
            new_count = self.sync.sync()

        self.assertEqual(1, new_count)
        self.db.upsert_video.assert_called_once_with(
            included.video_id,
            included.title,
            included.duration,
            included.upload_date,
        )
        self.db.prune_videos.assert_called_once_with({included.video_id})

    def test_sync_prunes_existing_queue_when_every_title_is_excluded(self) -> None:
        excluded = SimpleNamespace(
            video_id="excluded001",
            title="２４／７ rebroadcast",
            duration=0,
            upload_date="20260731",
        )
        self.db.get_stats.return_value = SimpleNamespace(total_videos=0)

        with patch.object(
            self.sync,
            "_fetch_channel_videos",
            return_value=[excluded],
        ):
            new_count = self.sync.sync()

        self.assertEqual(0, new_count)
        self.db.upsert_video.assert_not_called()
        self.db.prune_videos.assert_called_once_with(set())

    def test_title_filter_is_unicode_normalized_and_case_insensitive(self) -> None:
        with patch.object(
            YouTubeSync,
            "EXCLUDED_TITLE_SUBSTRINGS",
            ("Live 24/7",),
        ):
            self.assertTrue(
                YouTubeSync._title_is_excluded("LIVE ２４／７ rebroadcast")
            )

    @patch("app.youtube_sync.subprocess.run")
    def test_fetch_backfills_limit_after_flat_title_exclusion(self, run: Mock) -> None:
        entries = [
            {
                "id": f"video{index:06d}",
                "title": "Channel 24/7 live" if index == 0 else f"Archive {index}",
                "duration": 120,
                "upload_date": "20260730",
            }
            for index in range(16)
        ]
        run.return_value = subprocess.CompletedProcess(
            args=["yt-dlp"],
            returncode=0,
            stdout=json.dumps({"entries": entries}),
            stderr="",
        )

        videos = self.sync._fetch_channel_videos()

        regular = [
            video for video in videos if not self.sync._title_is_excluded(video.title)
        ]
        self.assertEqual(15, len(regular))
        self.assertEqual("video000015", regular[-1].video_id)

    @patch("app.youtube_sync.subprocess.run")
    def test_fetch_stops_with_fewer_regular_videos_when_source_is_exhausted(
        self,
        run: Mock,
    ) -> None:
        entries = [
            {
                "id": f"video{index:06d}",
                "title": "Channel 24/7 live" if index == 0 else f"Archive {index}",
                "duration": 120,
                "upload_date": "20260730",
            }
            for index in range(10)
        ]
        run.return_value = subprocess.CompletedProcess(
            args=["yt-dlp"],
            returncode=0,
            stdout=json.dumps({"entries": entries}),
            stderr="",
        )

        videos = self.sync._fetch_channel_videos()

        regular = [
            video for video in videos if not self.sync._title_is_excluded(video.title)
        ]
        self.assertEqual(9, len(regular))

    @patch("app.youtube_sync.subprocess.run")
    def test_metadata_title_exclusion_also_backfills_an_older_video(
        self,
        run: Mock,
    ) -> None:
        entries = [
            {
                "id": f"video{index:06d}",
                "title": f"Archive {index}",
                "duration": 0 if index == 0 else 120,
                "upload_date": None if index == 0 else "20260730",
            }
            for index in range(16)
        ]
        run.return_value = subprocess.CompletedProcess(
            args=["yt-dlp"],
            returncode=0,
            stdout=json.dumps({"entries": entries}),
            stderr="",
        )
        detailed = SimpleNamespace(
            title="Updated 24/7 live title",
            duration=0,
            upload_date="20260731",
        )

        with patch.object(self.sync, "_fetch_video_metadata", return_value=detailed):
            videos = self.sync._fetch_channel_videos()

        regular = [
            video for video in videos if not self.sync._title_is_excluded(video.title)
        ]
        self.assertEqual(15, len(regular))
        self.assertEqual("video000015", regular[-1].video_id)

    @patch("app.youtube_sync.subprocess.run")
    def test_upcoming_live_does_not_trigger_unrelated_backfill(self, run: Mock) -> None:
        entries = [
            {
                "id": f"video{index:06d}",
                "title": f"Archive {index}",
                "duration": 0 if index == 0 else 120,
                "upload_date": None if index == 0 else "20260730",
            }
            for index in range(16)
        ]
        run.return_value = subprocess.CompletedProcess(
            args=["yt-dlp"],
            returncode=0,
            stdout=json.dumps({"entries": entries}),
            stderr="",
        )

        with patch.object(
            self.sync,
            "_fetch_video_metadata",
            side_effect=UpcomingLiveEvent("not started"),
        ):
            videos = self.sync._fetch_channel_videos()

        self.assertEqual(14, len(videos))
        self.assertNotIn("video000015", {video.video_id for video in videos})

    @patch("app.youtube_sync.subprocess.run")
    def test_flat_playlist_duration_na_does_not_abort_discovery(
        self,
        run: Mock,
    ) -> None:
        run.return_value = subprocess.CompletedProcess(
            args=["yt-dlp"],
            returncode=0,
            stdout=json.dumps(
                {
                    "entries": [
                        {
                            "id": "abcdefghijk",
                            "title": "Live archive",
                            "duration": "NA",
                            "upload_date": "20260727",
                        }
                    ]
                }
            ),
            stderr="",
        )

        with patch.object(self.sync, "_fetch_video_metadata", return_value=None):
            videos = self.sync._fetch_channel_videos()

        self.assertEqual(1, len(videos))
        self.assertEqual("abcdefghijk", videos[0].video_id)
        self.assertEqual(0, videos[0].duration)

    @patch("app.youtube_sync.subprocess.run")
    def test_detailed_metadata_duration_na_returns_usable_video(
        self,
        run: Mock,
    ) -> None:
        run.return_value = subprocess.CompletedProcess(
            args=["yt-dlp"],
            returncode=0,
            stdout="Archive title\tNA\t20260727\n",
            stderr="",
        )

        video = self.sync._fetch_video_metadata("abcdefghijk")

        self.assertIsNotNone(video)
        self.assertEqual("Archive title", video.title)
        self.assertEqual(0, video.duration)
        self.assertEqual("20260727", video.upload_date)


if __name__ == "__main__":
    unittest.main()
