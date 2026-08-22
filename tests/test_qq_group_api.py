import sys
import unittest
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

from qq_album_archiver import apply_metadata_priority  # noqa: E402
from qq_group_api import AlbumRecord, choose_original_url, normalize_group_photo  # noqa: E402


class QQGroupApiTests(unittest.TestCase):
    def test_zero_dimension_origin_wins(self):
        item = {
            "photourl": {
                "large": {"url": "https://example.test/large.jpg", "width": 4096, "height": 3072},
                "origin": {"url": "https://example.test/origin.jpg", "width": 0, "height": 0},
            }
        }
        self.assertEqual(choose_original_url(item), "https://example.test/origin.jpg")

    def test_largest_image_is_fallback(self):
        item = {
            "photourl": [
                {"url": "https://example.test/small.jpg", "width": 200, "height": 200},
                {"url": "https://example.test/large.jpg", "width": 1920, "height": 1080},
            ]
        }
        self.assertEqual(choose_original_url(item), "https://example.test/large.jpg")

    def test_preserves_upload_time_and_raw_json(self):
        album = AlbumRecord("a1", "Album", 1, {})
        item = {
            "sloc": "photo-key",
            "uploadtime": 1597559528,
            "photourl": {"origin": {"url": "https://example.test/p.jpg", "width": 0, "height": 0}},
            "owner": {"uin": "123", "nickname": "Owner"},
        }
        record = normalize_group_photo(item, "g1", album)
        self.assertIsNotNone(record)
        self.assertEqual(record.upload_time, "2020-08-16T14:32:08+08:00")
        self.assertEqual(record.uploader, "Owner")
        self.assertIn('"uploadtime":1597559528', record.raw_json)

    def test_top_level_uploader_fallback(self):
        album = AlbumRecord("a1", "Album", 1, {})
        item = {
            "sloc": "photo-key",
            "uUploadTime": 1597559528,
            "uploadUin": 123456,
            "nick": "Owner",
            "user": {},
            "photourl": {"origin": {"url": "https://example.test/p.jpg", "width": 0, "height": 0}},
        }
        record = normalize_group_photo(item, "g1", album)
        self.assertEqual(record.uploader_id, "123456")
        self.assertEqual(record.uploader, "Owner")

    def test_preserves_server_exif_as_directly_linked_metadata(self):
        album = AlbumRecord("a1", "Album", 1, {})
        item = {
            "sloc": "photo-key",
            "uUploadTime": 1782277062,
            "uploadUin": 123456,
            "photourl": {"0": {"url": "https://example.test/p/1280", "width": 0, "height": 0}},
            "mapExifInfo": {
                "9": "vivo",
                "10": "vivo X300",
                "11": "2026:06:24 11:41:35",
                "13": "32.1",
                "15": "119.5",
            },
        }
        record = normalize_group_photo(item, "g1", album)
        self.assertEqual(record.qq_exif_time, "2026-06-24T11:41:35+08:00")
        self.assertEqual(record.qq_camera_model, "vivo X300")
        self.assertEqual(record.qq_gps_available, "yes")
        apply_metadata_priority(record)
        self.assertEqual(record.camera_model, "vivo X300")
        self.assertEqual(record.gps_available, "yes")

    def test_video_url_takes_precedence_over_photo_cover(self):
        album = AlbumRecord("a1", "Album", 1, {})
        item = {
            "sloc": "video-key",
            "videoflag": 1,
            "photourl": {"0": {"url": "https://example.test/cover/800", "width": 1920, "height": 1080}},
            "videodata": {"actionurl": "https://groupvideo.photo.qq.com/video-token.f5020.mp4?dis_k=x"},
        }
        record = normalize_group_photo(item, "g1", album)
        self.assertIsNotNone(record)
        self.assertEqual(record.media_kind, "video")
        self.assertTrue(record.original_url.endswith(".mp4?dis_k=x"))
        self.assertTrue(record.original_name.endswith(".mp4"))


if __name__ == "__main__":
    unittest.main()
