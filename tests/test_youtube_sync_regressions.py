from __future__ import annotations

import json
import subprocess
import unittest
from unittest.mock import Mock, patch

from app.youtube_sync import YouTubeSync


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
