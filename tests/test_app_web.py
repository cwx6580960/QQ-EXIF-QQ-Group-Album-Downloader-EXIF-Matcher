import sys
import tempfile
import threading
import time
import unittest
from pathlib import Path
from unittest.mock import patch

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

import app_web  # noqa: E402
from qq_album_archiver import PhotoRecord  # noqa: E402


class _FakeDatabase:
    def __init__(self, rows):
        self.rows = rows

    def all(self):
        return list(self.rows)

    def update(self, _row):
        return None

    def export(self, _output):
        return None


class _FakeState:
    def __init__(self, rows):
        self.database = _FakeDatabase(rows)
        self.group_id = ""
        self.cancel_event = threading.Event()
        self.lock = threading.RLock()
        self.records_changed = 0
        self.job_status = ""
        self.progress = 0
        self.job_running = True
        self.library_workers = 0
        self.active_library = 0
        self.peak_library = 0

    def update(self, **values):
        with self.lock:
            for key, value in values.items():
                setattr(self, key, value)


class AppWebTests(unittest.TestCase):
    def test_library_worker_honors_requested_thread_count(self):
        with tempfile.TemporaryDirectory() as temp:
            root = Path(temp)
            rows = []
            for index in range(8):
                raw = root / f"raw-{index}.jpg"
                raw.write_bytes(b"raw")
                rows.append(
                    PhotoRecord(
                        photo_id=f"p{index}",
                        group_id="group",
                        album_name="album",
                        raw_path=str(raw),
                        chosen_time="2020-01-02T12:00:00+08:00",
                        date_source="FILE_EXIF",
                        date_review="ready",
                    )
                )
            fake_state = _FakeState(rows)
            active = 0
            peak = 0
            lock = threading.Lock()

            def fake_copy(record, _output, embed_metadata=False):
                del embed_metadata
                nonlocal active, peak
                with lock:
                    active += 1
                    peak = max(peak, active)
                try:
                    time.sleep(0.05)
                    record.local_path = str(root / f"library-{record.photo_id}.jpg")
                    record.status = "archived"
                    return record, Path(record.local_path)
                finally:
                    with lock:
                        active -= 1

            with (
                patch.object(app_web, "STATE", fake_state),
                patch.object(app_web, "create_library_copy", side_effect=fake_copy),
                patch.object(app_web, "write_reports"),
            ):
                app_web.library_worker(root, embed_metadata=False, workers=4)

            self.assertEqual(peak, 4)
            self.assertEqual(fake_state.peak_library, 4)
            self.assertIn("4 线程", fake_state.job_status)


if __name__ == "__main__":
    unittest.main()
