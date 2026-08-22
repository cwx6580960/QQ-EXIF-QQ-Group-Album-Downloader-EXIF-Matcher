import json
import tempfile
import threading
import unittest
from datetime import datetime
from pathlib import Path
from unittest.mock import patch

import sys

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

from qq_album_archiver import (  # noqa: E402
    ArchiveDatabase,
    CHINA_TZ,
    DownloadCancelled,
    Downloader,
    PhotoRecord,
    analyze_raw_file,
    apply_metadata_priority,
    archive_record,
    apply_date_preview,
    apply_manual_review,
    choose_date,
    create_library_copy,
    extract_records,
    load_manifest,
    parse_datetime,
    qq_exif_fallback_tags,
    sanitize_component,
    stage_raw_file,
    media_suffix_for_file,
    detect_media_suffix,
    media_file_is_complete,
    validate_download_file,
)


class CoreTests(unittest.TestCase):
    def test_parse_unix_milliseconds(self):
        dt = parse_datetime(1597559528000)
        self.assertEqual(dt.isoformat(timespec="seconds"), "2020-08-16T14:32:08+08:00")

    def test_extracts_largest_original_url(self):
        payload = {
            "data": {
                "photos": [
                    {
                        "photoId": "p1",
                        "albumName": "Trip",
                        "uploadTime": 1597559528,
                        "photoUrl": [
                            {"url": "https://example.test/thumb.jpg", "width": 200, "height": 100},
                            {"originUrl": "https://example.test/original.jpg", "width": 4000, "height": 3000},
                        ],
                    }
                ]
            }
        }
        records = extract_records(payload)
        self.assertEqual(len(records), 1)
        self.assertEqual(records[0].photo_id, "p1")
        self.assertEqual(records[0].original_url, "https://example.test/original.jpg")
        self.assertEqual(records[0].upload_time, "2020-08-16T14:32:08+08:00")

    def test_upload_time_is_fallback(self):
        record = PhotoRecord(upload_time="2020-08-16T14:32:08+08:00")
        dt, source = choose_date(record)
        self.assertEqual(source, "QQ_UPLOAD_TIME")
        self.assertEqual(dt.year, 2020)

    def test_manual_review_survives_date_preview(self):
        record = PhotoRecord(
            photo_id="reviewed",
            album_id="a",
            uploader_id="u",
            upload_time="2026-02-13T10:00:00+08:00",
            qq_exif_time="2026-02-13T08:00:00+08:00",
        )
        apply_date_preview([record])
        apply_manual_review(record, "2026-02-14", "与同批照片核对")
        apply_date_preview([record])
        self.assertEqual(record.date_source, "MANUAL_REVIEW")
        self.assertEqual(record.date_review, "ready")
        self.assertEqual(record.chosen_time[:10], "2026-02-14")
        self.assertEqual(record.review_note, "与同批照片核对")

    def test_detects_mp4_container_without_url_extension(self):
        with tempfile.TemporaryDirectory() as directory:
            path = Path(directory) / "download.jpg"
            path.write_bytes(b"\x00\x00\x00\x18ftypisom\x00\x00\x00\x00")
            self.assertEqual(detect_media_suffix(path), ".mp4")

    def test_shoot_time_precedes_upload_time(self):
        record = PhotoRecord(
            shoot_time="2019-01-02T03:04:05+08:00",
            upload_time="2020-08-16T14:32:08+08:00",
        )
        dt, source = choose_date(record)
        self.assertEqual(source, "QQ_SHOOT_TIME")
        self.assertEqual(dt.year, 2019)

    def test_original_file_secondary_date_precedes_qq_map_exif(self):
        record = PhotoRecord(
            file_secondary_time="2018-01-02T03:04:05+08:00",
            qq_exif_time="2019-02-03T04:05:06+08:00",
        )
        dt, source = choose_date(record)
        self.assertEqual(source, "FILE_SECONDARY")
        self.assertEqual(dt.year, 2018)

    def test_file_camera_metadata_precedes_qq_map_exif(self):
        from PIL import Image

        with tempfile.TemporaryDirectory() as temp:
            path = Path(temp) / "canon.jpg"
            exif = Image.Exif()
            exif[271] = "Canon"
            exif[272] = "Canon EOS 80D"
            Image.new("RGB", (16, 16), "white").save(path, exif=exif)
            record = PhotoRecord(
                photo_id="canon",
                qq_camera_make="vivo",
                qq_camera_model="vivo X300",
            )
            analyze_raw_file(record, path)
            self.assertEqual(record.file_camera_make, "Canon")
            self.assertEqual(record.file_camera_model, "Canon EOS 80D")
            self.assertEqual(record.camera_make, "Canon")
            self.assertEqual(record.camera_model, "Canon EOS 80D")
            self.assertEqual(record.camera_model_source, "FILE_EXIF")

    def test_qq_camera_is_used_only_when_file_camera_is_missing(self):
        record = PhotoRecord(qq_camera_make="vivo", qq_camera_model="vivo X300")
        apply_metadata_priority(record)
        self.assertEqual(record.camera_make, "vivo")
        self.assertEqual(record.camera_model, "vivo X300")
        self.assertEqual(record.camera_model_source, "QQ_MAP_EXIF")

    def test_database_update_can_clear_stale_derived_values(self):
        with tempfile.TemporaryDirectory() as temp:
            database = ArchiveDatabase(Path(temp) / "archive.sqlite3")
            record = PhotoRecord(photo_id="p1", date_note="stale", date_conflict="OLD")
            database.upsert([record])
            record.date_note = ""
            record.date_conflict = ""
            database.update(record)
            saved = database.get(record.record_key)
            self.assertEqual(saved.date_note, "")
            self.assertEqual(saved.date_conflict, "")
            database.conn.close()

    def test_detects_actual_png_format_when_filename_says_jpg(self):
        from PIL import Image

        with tempfile.TemporaryDirectory() as temp:
            path = Path(temp) / "mislabeled.jpg"
            Image.new("RGB", (8, 8), "white").save(path, format="PNG")
            self.assertEqual(media_suffix_for_file(path), ".png")

    def test_qq_exif_fields_are_available_as_missing_tag_fallbacks(self):
        record = PhotoRecord(
            qq_exif_json=json.dumps({
                "1": "212", "2": "0.01", "6": "1.68", "7": "16", "8": "6250/1000",
                "9": "vivo", "10": "vivo X300", "12": "N", "13": "32/1,0/1,10/1",
                "14": "E", "15": "119/1,33/1,20/1", "16": "0", "17": "48/1",
            }),
        )
        tags = qq_exif_fallback_tags(record)
        self.assertEqual(tags["EXIF:Make"], "vivo")
        self.assertEqual(tags["EXIF:Model"], "vivo X300")
        self.assertEqual(tags["EXIF:ISO"], "212")
        self.assertEqual(tags["EXIF:FocalLength"], "6.25")
        self.assertEqual(tags["EXIF:FocalLengthIn35mmFormat"], "16")
        self.assertEqual(tags["EXIF:GPSLatitudeRef"], "N")
        self.assertEqual(tags["EXIF:GPSLongitudeRef"], "E")

    def test_zero_qq_gps_is_not_written(self):
        record = PhotoRecord(qq_exif_json=json.dumps({
            "12": "N", "13": "0/1,0/1,0/1", "14": "E", "15": "0/1,0/1,0/1",
        }))
        tags = qq_exif_fallback_tags(record)
        self.assertNotIn("EXIF:GPSLatitude", tags)
        self.assertNotIn("EXIF:GPSLongitude", tags)

    def test_downloader_cancels_mid_stream_and_removes_partial_file(self):
        class CancellingResponse:
            def __init__(self, event):
                self.event = event
                self.read_count = 0

            def __enter__(self):
                return self

            def __exit__(self, *_args):
                return False

            def read(self, _size):
                self.read_count += 1
                if self.read_count == 1:
                    self.event.set()
                    return b"partial"
                return b""

        with tempfile.TemporaryDirectory() as temp:
            root = Path(temp)
            destination = root / "photo.jpg"
            event = threading.Event()
            record = PhotoRecord(photo_id="p1", original_url="https://example.test/photo.jpg")
            with patch("urllib.request.urlopen", return_value=CancellingResponse(event)):
                with self.assertRaises(DownloadCancelled):
                    Downloader(root).download_one(record, destination, cancel_event=event)
            self.assertFalse(destination.exists())
            self.assertFalse(destination.with_suffix(".jpg.part").exists())

    def test_downloader_rejects_short_content_length(self):
        class ShortResponse:
            headers = {"Content-Type": "image/jpeg", "Content-Length": "100"}

            def __enter__(self):
                return self

            def __exit__(self, *_args):
                return False

            def read(self, _size):
                if getattr(self, "done", False):
                    return b""
                self.done = True
                return b"short"

        with tempfile.TemporaryDirectory() as temp:
            root = Path(temp)
            destination = root / "photo.jpg"
            record = PhotoRecord(photo_id="p1", original_url="https://example.test/photo.jpg")
            with patch("urllib.request.urlopen", side_effect=lambda *_args, **_kwargs: ShortResponse()), patch("time.sleep"):
                with self.assertRaisesRegex(RuntimeError, "下载响应不完整"):
                    Downloader(root).download_one(record, destination)
            self.assertFalse(destination.exists())
            self.assertFalse(destination.with_suffix(".jpg.part").exists())

    def test_validate_download_file_rejects_truncated_jpeg(self):
        from PIL import Image

        with tempfile.TemporaryDirectory() as temp:
            root = Path(temp)
            complete = root / "complete.jpg"
            Image.new("RGB", (32, 32), "white").save(complete, format="JPEG")
            truncated = root / "truncated.jpg"
            truncated.write_bytes(complete.read_bytes()[:-2])
            self.assertTrue(media_file_is_complete(complete))
            self.assertFalse(media_file_is_complete(truncated))
            with self.assertRaisesRegex(ValueError, "不完整|截断"):
                validate_download_file(truncated, ".jpg")

    def test_preview_infers_only_between_matching_anchors(self):
        records = [
            PhotoRecord(photo_id="before", album_id="a", uploader_id="u", upload_time="2026-02-13T10:00:00+08:00", qq_exif_time="2026-02-13T08:00:00+08:00"),
            PhotoRecord(photo_id="middle", album_id="a", uploader_id="u", upload_time="2026-02-13T10:00:05+08:00"),
            PhotoRecord(photo_id="after", album_id="a", uploader_id="u", upload_time="2026-02-13T10:00:09+08:00", qq_exif_time="2026-02-13T09:00:00+08:00"),
        ]
        apply_date_preview(records)
        self.assertEqual(records[1].date_source, "BATCH_NEIGHBOR")
        self.assertEqual(records[1].date_confidence, "high")
        self.assertEqual(records[1].chosen_time, "2026-02-13T12:00:00+08:00")

    def test_preview_marks_single_date_batch_for_review(self):
        records = [
            PhotoRecord(photo_id="anchor", album_id="a", uploader_id="u", upload_time="2026-02-13T10:00:00+08:00", qq_exif_time="2026-02-13T08:00:00+08:00"),
            PhotoRecord(photo_id="candidate", album_id="a", uploader_id="u", upload_time="2026-02-13T10:00:05+08:00"),
        ]
        apply_date_preview(records)
        self.assertEqual(records[1].date_source, "BATCH_SINGLE_DATE")
        self.assertEqual(records[1].date_review, "review")

    def test_preview_conflict_falls_back_to_upload_date(self):
        records = [
            PhotoRecord(photo_id="one", album_id="a", uploader_id="u", upload_time="2026-02-13T10:00:00+08:00", qq_exif_time="2026-02-12T08:00:00+08:00"),
            PhotoRecord(photo_id="candidate", album_id="a", uploader_id="u", upload_time="2026-02-13T10:00:04+08:00"),
            PhotoRecord(photo_id="two", album_id="a", uploader_id="u", upload_time="2026-02-13T10:00:08+08:00", qq_exif_time="2026-02-14T08:00:00+08:00"),
        ]
        apply_date_preview(records)
        self.assertEqual(records[1].date_source, "QQ_UPLOAD_TIME")
        self.assertEqual(records[1].date_conflict, "BATCH_DATE_CONFLICT")
        self.assertEqual(records[1].chosen_time, "2026-02-13T12:00:00+08:00")

    def test_sanitize_windows_name(self):
        self.assertEqual(sanitize_component('a:b*c?d"e'), "a_b_c_d_e")
        self.assertEqual(sanitize_component("CON"), "_CON")

    def test_load_canonical_manifest(self):
        with tempfile.TemporaryDirectory() as temp:
            path = Path(temp) / "manifest.json"
            path.write_text(json.dumps({
                "photos": [{
                    "photo_id": "abc",
                    "album_name": "Album",
                    "original_url": "https://example.test/a.jpg",
                    "upload_time": "2020-08-16 14:32:08",
                }]
            }), encoding="utf-8")
            records = load_manifest(path)
            self.assertEqual(len(records), 1)
            self.assertEqual(records[0].photo_id, "abc")

    def test_archive_preserves_raw_without_sidecar(self):
        with tempfile.TemporaryDirectory() as temp:
            root = Path(temp)
            source = root / "download.jpg"
            source.write_bytes(b"not-a-real-jpeg-but-an-original-byte-stream")
            record = PhotoRecord(
                photo_id="p1",
                album_name="Album",
                group_id="Group",
                original_name="download.jpg",
                upload_time="2020-08-16T14:32:08+08:00",
            )
            updated, library = archive_record(record, source, root / "archive")
            raw = Path(updated.raw_path)
            self.assertEqual(raw.read_bytes(), source.read_bytes())
            self.assertEqual(raw.name, f"QQ_{updated.record_key}.jpg")
            self.assertTrue(library.exists())
            self.assertEqual(library.name, f"2020-08-16__QQ_{updated.record_key}.jpg")
            self.assertFalse(library.with_suffix(".jpg.xmp").exists())
            self.assertEqual(updated.date_source, "QQ_UPLOAD_TIME")

    def test_download_stage_does_not_create_library_copy(self):
        with tempfile.TemporaryDirectory() as temp:
            root = Path(temp)
            source = root / "download.jpg"
            source.write_bytes(b"downloaded-original-bytes")
            record = PhotoRecord(photo_id="p1", album_name="Album", group_id="Group", original_name="download.jpg")
            updated, raw = stage_raw_file(record, source, root / "archive")
            self.assertTrue(raw.exists())
            self.assertEqual(raw.name, f"QQ_{updated.record_key}.jpg")
            self.assertEqual(updated.status, "analyzed")
            self.assertFalse((root / "archive" / "01_library").exists())

    def test_library_stage_requires_download_and_reviewed_date(self):
        with tempfile.TemporaryDirectory() as temp:
            root = Path(temp)
            record = PhotoRecord(photo_id="p1", chosen_time="2020-01-02T12:00:00+08:00", date_source="QQ_UPLOAD_TIME", date_review="fallback")
            with self.assertRaisesRegex(ValueError, "Raw file is missing"):
                create_library_copy(record, root)

            raw = root / "raw.jpg"
            raw.write_bytes(b"raw")
            analyze_raw_file(record, raw)
            record.chosen_time = "2020-01-02T12:00:00+08:00"
            record.date_source = "BATCH_SINGLE_DATE"
            record.date_review = "review"
            with self.assertRaisesRegex(ValueError, "requires review"):
                create_library_copy(record, root)

    def test_library_embeds_each_accepted_inferred_date_source(self):
        with tempfile.TemporaryDirectory() as temp:
            root = Path(temp)
            raw = root / "raw.jpg"
            raw.write_bytes(b"raw")
            sources = ("QQ_MAP_EXIF", "FILE_SECONDARY", "QQ_SHOOT_TIME", "BATCH_NEIGHBOR", "QQ_UPLOAD_TIME")
            with patch("qq_album_archiver.embed_metadata_with_exiftool", return_value=True) as embed:
                for index, source in enumerate(sources):
                    record = PhotoRecord(
                        photo_id=f"p{index}",
                        group_id="Group",
                        album_name="Album",
                        raw_path=str(raw),
                        chosen_time="2020-01-02T12:00:00+08:00",
                        date_source=source,
                        date_review="fallback" if source == "QQ_UPLOAD_TIME" else "ready",
                    )
                    create_library_copy(record, root / source, embed_metadata=True)
            self.assertEqual(embed.call_count, len(sources))

    def test_library_runs_create_only_metadata_fill_for_file_exif(self):
        with tempfile.TemporaryDirectory() as temp:
            root = Path(temp)
            raw = root / "raw.jpg"
            raw.write_bytes(b"raw")
            record = PhotoRecord(
                photo_id="p1",
                group_id="Group",
                album_name="Album",
                raw_path=str(raw),
                chosen_time="2020-01-02T03:04:05+08:00",
                date_source="FILE_EXIF",
                date_review="ready",
            )
            with patch("qq_album_archiver.embed_metadata_with_exiftool", return_value=True) as embed:
                create_library_copy(record, root, embed_metadata=True)
            embed.assert_called_once()
            self.assertEqual(embed.call_args.args[2], "FILE_EXIF")
            self.assertIs(embed.call_args.args[3], record)


if __name__ == "__main__":
    unittest.main()
