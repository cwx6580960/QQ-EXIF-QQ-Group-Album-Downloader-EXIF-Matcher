from __future__ import annotations

import argparse
import base64
import csv
import ctypes
import hashlib
import json
import mimetypes
import os
import re
import shutil
import sqlite3
import subprocess
import sys
import threading
import time
import urllib.parse
import urllib.request
from concurrent.futures import ThreadPoolExecutor, as_completed
from dataclasses import asdict, dataclass, fields
from datetime import datetime, timedelta, timezone
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path
from typing import Any, Callable, Iterable

try:
    from PIL import Image, ImageFile
    ImageFile.LOAD_TRUNCATED_IMAGES = False
except ImportError:  # pragma: no cover - the bundled runtime includes Pillow
    Image = None


APP_NAME = "QQ Album Archiver"
SCHEMA_VERSION = 2
FILE_ANALYSIS_VERSION = "2"
CHINA_TZ = timezone(timedelta(hours=8))
MEDIA_EXTENSIONS = {
    ".jpg", ".jpeg", ".png", ".webp", ".gif", ".bmp", ".heic", ".heif",
    ".mp4", ".mov", ".m4v", ".avi", ".mkv",
}
VIDEO_EXTENSIONS = {".mp4", ".mov", ".m4v", ".avi", ".mkv"}


class DownloadCancelled(Exception):
    pass


def application_root() -> Path:
    """Return the source root or PyInstaller's extracted resource directory."""
    if getattr(sys, "frozen", False):
        return Path(getattr(sys, "_MEIPASS", Path(sys.executable).resolve().parent))
    return Path(__file__).resolve().parent


def find_exiftool() -> str | None:
    candidates = [
        shutil.which("exiftool"),
        shutil.which("exiftool.exe"),
        str(application_root() / "tools" / "exiftool" / "exiftool.exe"),
        str(application_root() / "tools" / "exiftool" / "exiftool(-k).exe"),
        str(Path(sys.executable).resolve().parent / "tools" / "exiftool" / "exiftool.exe"),
        str(Path(sys.executable).resolve().parent / "tools" / "exiftool" / "exiftool(-k).exe"),
    ]
    return next((value for value in candidates if value and Path(value).is_file()), None)


def normalized_key(value: str) -> str:
    return re.sub(r"[^a-z0-9]", "", value.lower())


ALIASES = {
    "photo_id": ("photoid", "picid", "pictureid", "lloc", "sloc", "id"),
    "album_id": ("albumid", "topicid", "albumkey"),
    "album_name": ("albumname", "albumtitle", "topicname", "album"),
    "group_id": ("groupid", "groupuin", "qunid", "qunuin", "gc"),
    "uploader": ("uploader", "uploadername", "nickname", "nick", "ownername"),
    "uploader_id": ("uploaderid", "uin", "owneruin", "ownerid"),
    "original_name": ("originalname", "filename", "name", "picname", "title"),
    "upload_time": ("uploadtime", "uploadedat", "uploadtimestamp", "createtime", "addtime"),
    "shoot_time": ("shoottime", "takentime", "takenat", "datetaken", "capturetime"),
    "source_page": ("sourcepage", "pageurl", "referer"),
}

URL_KEY_SCORES = {
    "originurl": 10000,
    "originalurl": 10000,
    "rawurl": 9500,
    "downloadurl": 9000,
    "photourl": 8000,
    "picurl": 7500,
    "url": 5000,
    "src": 3000,
    "thumbnail": -5000,
    "thumburl": -5000,
    "smallurl": -4000,
}


@dataclass
class PhotoRecord:
    record_key: str = ""
    photo_id: str = ""
    album_id: str = ""
    album_name: str = "Unsorted"
    group_id: str = ""
    uploader: str = ""
    uploader_id: str = ""
    original_name: str = ""
    original_url: str = ""
    media_kind: str = ""
    media_mime: str = ""
    upload_time: str = ""
    shoot_time: str = ""
    source_page: str = ""
    capture_source: str = ""
    raw_path: str = ""
    local_path: str = ""
    exif_time: str = ""
    file_secondary_time: str = ""
    file_exif_json: str = ""
    file_camera_make: str = ""
    file_camera_model: str = ""
    file_lens_model: str = ""
    file_gps_available: str = ""
    qq_exif_time: str = ""
    qq_exif_json: str = ""
    qq_camera_make: str = ""
    qq_camera_model: str = ""
    qq_gps_available: str = ""
    camera_make: str = ""
    camera_model: str = ""
    camera_make_source: str = ""
    camera_model_source: str = ""
    gps_available: str = ""
    gps_source: str = ""
    chosen_time: str = ""
    date_source: str = ""
    date_precision: str = ""
    date_confidence: str = ""
    date_review: str = ""
    date_conflict: str = ""
    date_note: str = ""
    date_anchor_ids: str = ""
    manual_reviewed: str = ""
    reviewed_at: str = ""
    reviewed_from_source: str = ""
    review_note: str = ""
    quality_class: str = ""
    quality_width: str = ""
    quality_height: str = ""
    batch_key: str = ""
    sha256: str = ""
    analysis_version: str = ""
    status: str = "pending"
    error: str = ""
    raw_json: str = ""

    def finalize_key(self) -> None:
        if not self.record_key:
            seed = self.photo_id or self.original_url or (
                f"{self.album_id}|{self.original_name}|{self.upload_time}"
            )
            self.record_key = hashlib.sha256(seed.encode("utf-8", "ignore")).hexdigest()[:24]


def parse_datetime(value: Any, default_tz=CHINA_TZ) -> datetime | None:
    if value is None or value == "":
        return None
    if isinstance(value, datetime):
        dt = value
    elif isinstance(value, (int, float)) or (isinstance(value, str) and re.fullmatch(r"\d{10,16}", value.strip())):
        number = float(value)
        if number > 10_000_000_000:
            number /= 1000
        try:
            dt = datetime.fromtimestamp(number, tz=timezone.utc).astimezone(default_tz)
        except (OverflowError, OSError, ValueError):
            return None
    else:
        text = str(value).strip()
        text = text.replace("年", "-").replace("月", "-").replace("日", " ")
        text = re.sub(r"\s+", " ", text).strip()
        candidates = [
            text,
            text.replace("Z", "+00:00"),
            text.replace("/", "-"),
        ]
        dt = None
        for candidate in candidates:
            try:
                dt = datetime.fromisoformat(candidate)
                break
            except ValueError:
                pass
        if dt is None:
            for fmt in (
                "%Y:%m:%d %H:%M:%S",
                "%Y-%m-%d %H:%M:%S",
                "%Y-%m-%d %H:%M",
                "%Y-%m-%d",
                "%Y%m%d%H%M%S",
            ):
                try:
                    dt = datetime.strptime(candidates[-1], fmt)
                    break
                except ValueError:
                    pass
        if dt is None:
            return None
    if dt.tzinfo is None:
        dt = dt.replace(tzinfo=default_tz)
    return dt.astimezone(default_tz)


def iso_time(value: Any) -> str:
    dt = parse_datetime(value)
    return dt.isoformat(timespec="seconds") if dt else ""


def sanitize_component(value: str, fallback: str = "Unsorted", limit: int = 100) -> str:
    value = re.sub(r"[<>:\"/\\|?*\x00-\x1f]", "_", (value or "").strip())
    value = re.sub(r"\s+", " ", value).rstrip(". ")
    if not value:
        value = fallback
    reserved = {"CON", "PRN", "AUX", "NUL", *(f"COM{i}" for i in range(1, 10)), *(f"LPT{i}" for i in range(1, 10))}
    if value.upper() in reserved:
        value = f"_{value}"
    return value[:limit].rstrip(". ") or fallback


def looks_like_url(value: Any) -> bool:
    if not isinstance(value, str):
        return False
    return value.startswith(("http://", "https://", "//"))


def collect_url_candidates(obj: Any, depth: int = 0) -> list[tuple[int, str]]:
    if depth > 4:
        return []
    candidates: list[tuple[int, str]] = []
    if isinstance(obj, dict):
        width = _numeric(obj.get("width") or obj.get("w"))
        height = _numeric(obj.get("height") or obj.get("h"))
        size_bonus = min(int(width * height / 1000), 3000) if width and height else 0
        for key, value in obj.items():
            key_norm = normalized_key(str(key))
            if looks_like_url(value):
                score = URL_KEY_SCORES.get(key_norm, 1000) + size_bonus
                candidates.append((score, normalize_url(str(value))))
            elif isinstance(value, (dict, list)):
                nested = collect_url_candidates(value, depth + 1)
                key_bonus = URL_KEY_SCORES.get(key_norm, 0)
                candidates.extend((score + key_bonus, url) for score, url in nested)
    elif isinstance(obj, list):
        for item in obj[:100]:
            candidates.extend(collect_url_candidates(item, depth + 1))
    return candidates


def _numeric(value: Any) -> float:
    try:
        return float(value)
    except (TypeError, ValueError):
        return 0


def normalize_url(value: str) -> str:
    value = value.replace("\\/", "/").replace("&amp;", "&")
    return "https:" + value if value.startswith("//") else value


def flat_lookup(obj: dict[str, Any], canonical: str) -> Any:
    wanted = set(ALIASES[canonical])
    for key, value in obj.items():
        if normalized_key(str(key)) in wanted and not isinstance(value, (dict, list)):
            return value
    return ""


def normalize_photo(obj: dict[str, Any], capture_source: str = "json") -> PhotoRecord | None:
    url_candidates = collect_url_candidates(obj)
    url = max(url_candidates, default=(0, ""), key=lambda item: item[0])[1]
    photo_id = str(flat_lookup(obj, "photo_id") or "").strip()
    original_name = str(flat_lookup(obj, "original_name") or "").strip()
    upload = flat_lookup(obj, "upload_time")
    shoot = flat_lookup(obj, "shoot_time")

    # Avoid turning arbitrary API objects into photo rows.
    if not url or not (photo_id or original_name or upload or shoot):
        return None
    parsed = urllib.parse.urlparse(url)
    path_name = urllib.parse.unquote(Path(parsed.path).name)
    if not original_name and Path(path_name).suffix.lower() in MEDIA_EXTENSIONS:
        original_name = path_name
    if original_name and Path(original_name).suffix.lower() not in MEDIA_EXTENSIONS and Path(path_name).suffix:
        original_name += Path(path_name).suffix

    record = PhotoRecord(
        photo_id=photo_id,
        album_id=str(flat_lookup(obj, "album_id") or "").strip(),
        album_name=str(flat_lookup(obj, "album_name") or "Unsorted").strip() or "Unsorted",
        group_id=str(flat_lookup(obj, "group_id") or "").strip(),
        uploader=str(flat_lookup(obj, "uploader") or "").strip(),
        uploader_id=str(flat_lookup(obj, "uploader_id") or "").strip(),
        original_name=original_name,
        original_url=url,
        upload_time=iso_time(upload),
        shoot_time=iso_time(shoot),
        source_page=str(flat_lookup(obj, "source_page") or "").strip(),
        capture_source=capture_source,
        raw_json=json.dumps(obj, ensure_ascii=False, separators=(",", ":")),
    )
    record.finalize_key()
    return record


def walk_dicts(value: Any, max_nodes: int = 200_000) -> Iterable[dict[str, Any]]:
    stack = [value]
    seen = 0
    while stack and seen < max_nodes:
        current = stack.pop()
        seen += 1
        if isinstance(current, dict):
            yield current
            stack.extend(current.values())
        elif isinstance(current, list):
            stack.extend(current)


def extract_records(payload: Any, capture_source: str = "json") -> list[PhotoRecord]:
    if isinstance(payload, dict) and isinstance(payload.get("photos"), list):
        direct: list[PhotoRecord] = []
        canonical_fields = {item.name for item in fields(PhotoRecord)}
        for item in payload["photos"]:
            if not isinstance(item, dict):
                continue
            if "original_url" in item:
                values = {key: item.get(key, "") for key in canonical_fields}
                record = PhotoRecord(**values)
                record.upload_time = iso_time(record.upload_time)
                record.shoot_time = iso_time(record.shoot_time)
                record.capture_source = record.capture_source or capture_source
                record.finalize_key()
            else:
                record = normalize_photo(item, capture_source)
            if record:
                direct.append(record)
        return deduplicate_records(direct)

    records = []
    for obj in walk_dicts(payload):
        record = normalize_photo(obj, capture_source)
        if record:
            records.append(record)
    return deduplicate_records(records)


def deduplicate_records(records: Iterable[PhotoRecord]) -> list[PhotoRecord]:
    merged: dict[str, PhotoRecord] = {}
    for record in records:
        record.finalize_key()
        key = record.photo_id or record.original_url or record.record_key
        if key not in merged:
            merged[key] = record
            continue
        existing = merged[key]
        for field in fields(PhotoRecord):
            name = field.name
            if not getattr(existing, name) and getattr(record, name):
                setattr(existing, name, getattr(record, name))
    return list(merged.values())


def load_manifest(path: Path) -> list[PhotoRecord]:
    suffix = path.suffix.lower()
    if suffix == ".csv":
        with path.open("r", encoding="utf-8-sig", newline="") as handle:
            rows = list(csv.DictReader(handle))
        return extract_records({"photos": rows}, f"csv:{path.name}")

    with path.open("r", encoding="utf-8-sig") as handle:
        payload = json.load(handle)

    if suffix == ".har" or (isinstance(payload, dict) and "log" in payload and "entries" in payload.get("log", {})):
        records: list[PhotoRecord] = []
        for entry in payload.get("log", {}).get("entries", []):
            content = entry.get("response", {}).get("content", {})
            text = content.get("text", "")
            if content.get("encoding") == "base64":
                try:
                    text = base64.b64decode(text).decode("utf-8", "replace")
                except Exception:
                    continue
            try:
                body = json.loads(text)
            except (TypeError, json.JSONDecodeError):
                continue
            url = entry.get("request", {}).get("url", "")
            records.extend(extract_records(body, f"har:{url}"))
        return deduplicate_records(records)

    return extract_records(payload, f"json:{path.name}")


def _exif_text(value: Any) -> str:
    if value is None:
        return ""
    if isinstance(value, bytes):
        return value.decode("utf-8", "replace").strip("\x00 ")
    return str(value).strip()


def _exif_number(value: Any, integer: bool = False) -> str:
    if value in (None, ""):
        return ""
    try:
        number = float(value)
    except (TypeError, ValueError, ZeroDivisionError):
        return _exif_text(value)
    return str(int(round(number))) if integer else f"{number:.10g}"


def _gps_coordinate(value: Any, reference: str) -> str:
    if not isinstance(value, (tuple, list)) or len(value) < 3:
        return ""
    try:
        degrees, minutes, seconds = (float(item) for item in value[:3])
        decimal = abs(degrees) + abs(minutes) / 60 + abs(seconds) / 3600
        if reference.upper() in {"S", "W"}:
            decimal = -decimal
        return f"{decimal:.10f}".rstrip("0").rstrip(".")
    except (TypeError, ValueError, ZeroDivisionError):
        return ""


def read_file_exif_metadata(path: Path) -> tuple[datetime | None, datetime | None, dict[str, str]]:
    if Image is None or path.suffix.lower() not in {".jpg", ".jpeg", ".tif", ".tiff", ".webp", ".png"}:
        return None, None, {}
    try:
        with Image.open(path) as image:
            exif = image.getexif()
            # Pillow exposes DateTimeOriginal/DateTimeDigitized inside the
            # nested Exif IFD for most Apple files; top-level lookup alone
            # silently misses those dates.
            nested = {}
            try:
                nested = exif.get_ifd(34665) or {}
            except Exception:
                nested = {}
            try:
                gps = exif.get_ifd(34853) or {}
            except Exception:
                gps = {}
            original_value = nested.get(36867) or exif.get(36867)
            secondary_value = nested.get(36868) or exif.get(36868) or exif.get(306)
            original = parse_datetime(original_value)
            secondary = parse_datetime(secondary_value)
            max_year = datetime.now(CHINA_TZ).year + 1
            if not original or not (1970 <= original.year <= max_year):
                original = None
            if not secondary or not (1970 <= secondary.year <= max_year):
                secondary = None
            latitude_ref = _exif_text(gps.get(1)).upper()
            longitude_ref = _exif_text(gps.get(3)).upper()
            latitude = _gps_coordinate(gps.get(2), latitude_ref)
            longitude = _gps_coordinate(gps.get(4), longitude_ref)
            metadata = {
                "Make": _exif_text(exif.get(271)),
                "Model": _exif_text(exif.get(272)),
                "LensModel": _exif_text(nested.get(42036)),
                "DateTimeOriginal": _exif_text(original_value),
                "CreateDate": _exif_text(nested.get(36868) or exif.get(36868)),
                "ModifyDate": _exif_text(exif.get(306)),
                "ISO": _exif_number(nested.get(34855), integer=True),
                "ExposureTime": _exif_number(nested.get(33434)),
                "FNumber": _exif_number(nested.get(33437)),
                "FocalLength": _exif_number(nested.get(37386)),
                "FocalLengthIn35mmFormat": _exif_number(nested.get(41989), integer=True),
                "GPSLatitude": latitude,
                "GPSLatitudeRef": latitude_ref if latitude else "",
                "GPSLongitude": longitude,
                "GPSLongitudeRef": longitude_ref if longitude else "",
                "GPSAltitude": _exif_number(gps.get(6)),
                "GPSAltitudeRef": _exif_number(gps.get(5), integer=True),
                "GPSImgDirection": _exif_number(gps.get(17)),
            }
            return original, secondary, {key: value for key, value in metadata.items() if value != ""}
    except Exception:
        return None, None, {}


def read_exif_datetimes(path: Path) -> tuple[datetime | None, datetime | None]:
    original, secondary, _ = read_file_exif_metadata(path)
    return original, secondary


def read_exif_datetime(path: Path) -> datetime | None:
    return read_exif_datetimes(path)[0]


def choose_date(record: PhotoRecord, source_path: Path | None = None) -> tuple[datetime, str]:
    exif_dt = read_exif_datetime(source_path) if source_path and source_path.exists() else None
    if exif_dt:
            return exif_dt, "FILE_EXIF"
    secondary_dt = parse_datetime(record.file_secondary_time)
    if secondary_dt:
        return secondary_dt, "FILE_SECONDARY"
    qq_exif_dt = parse_datetime(record.qq_exif_time)
    if qq_exif_dt:
        return qq_exif_dt, "QQ_MAP_EXIF"
    shoot_dt = parse_datetime(record.shoot_time)
    if shoot_dt:
        return shoot_dt, "QQ_SHOOT_TIME"
    preview_dt = parse_datetime(record.chosen_time)
    if preview_dt and (
        record.date_source in {"BATCH_NEIGHBOR", "QQ_UPLOAD_TIME"}
        or (record.date_source == "BATCH_SINGLE_DATE" and record.date_review == "ready")
    ):
        return preview_dt, record.date_source
    upload_dt = parse_datetime(record.upload_time)
    if upload_dt:
        return upload_dt.replace(hour=12, minute=0, second=0, microsecond=0), "QQ_UPLOAD_TIME"
    # Download/creation time is deliberately never a date source. Callers
    # should handle a missing upload time as an explicit unknown value.
    raise ValueError("No trustworthy photo date is available")


def day_at_noon(value: datetime) -> datetime:
    return value.astimezone(CHINA_TZ).replace(hour=12, minute=0, second=0, microsecond=0)


def _record_id(record: PhotoRecord) -> str:
    return record.photo_id or record.record_key


def apply_metadata_priority(record: PhotoRecord) -> PhotoRecord:
    try:
        qq = json.loads(record.qq_exif_json or "{}")
    except (TypeError, ValueError, json.JSONDecodeError):
        qq = {}
    if not isinstance(qq, dict):
        qq = {}

    record.qq_camera_make = record.qq_camera_make or _exif_text(qq.get("9"))
    record.qq_camera_model = record.qq_camera_model or _exif_text(qq.get("10"))
    if not record.qq_gps_available and qq:
        latitude = _gps_decimal(qq.get("13"))
        longitude = _gps_decimal(qq.get("15"))
        record.qq_gps_available = "yes" if (
            latitude is not None and longitude is not None and (latitude != 0 or longitude != 0)
        ) else "no"

    record.camera_make = record.file_camera_make or record.qq_camera_make
    record.camera_model = record.file_camera_model or record.qq_camera_model
    record.camera_make_source = (
        "FILE_EXIF" if record.file_camera_make else "QQ_MAP_EXIF" if record.qq_camera_make else ""
    )
    record.camera_model_source = (
        "FILE_EXIF" if record.file_camera_model else "QQ_MAP_EXIF" if record.qq_camera_model else ""
    )
    if record.file_gps_available == "yes":
        record.gps_available = "yes"
        record.gps_source = "FILE_EXIF"
    elif record.qq_gps_available == "yes":
        record.gps_available = "yes"
        record.gps_source = "QQ_MAP_EXIF"
    else:
        record.gps_available = "no" if record.file_gps_available == "no" or record.qq_gps_available == "no" else ""
        record.gps_source = ""
    return record


def _direct_date(record: PhotoRecord) -> tuple[datetime | None, str]:
    current_year = datetime.now(CHINA_TZ).year
    for value, source in (
        (record.exif_time, "FILE_EXIF"),
        (record.file_secondary_time, "FILE_SECONDARY"),
        (record.qq_exif_time, "QQ_MAP_EXIF"),
        (record.shoot_time, "QQ_SHOOT_TIME"),
    ):
        dt = parse_datetime(value)
        if dt and 1970 <= dt.year <= current_year + 1:
            return dt, source
    return None, ""


def apply_date_preview(records: list[PhotoRecord], session_gap_seconds: int = 10) -> dict[str, Any]:
    """Apply explainable date decisions without touching media files."""
    current_year = datetime.now(CHINA_TZ).year
    for record in records:
        apply_metadata_priority(record)
        manual_dt = parse_datetime(record.chosen_time)
        manual_decision = record.manual_reviewed == "yes" and manual_dt is not None
        direct, source = _direct_date(record)
        record.batch_key = ""
        record.date_anchor_ids = ""
        if not manual_decision:
            record.date_conflict = ""
            record.date_note = ""
        else:
            record.date_source = "MANUAL_REVIEW"
            record.date_precision = "day"
            record.date_confidence = "manual"
            record.date_review = "ready"
            record.date_conflict = ""
            record.date_note = record.review_note or "人工复核确认"
            record.chosen_time = day_at_noon(manual_dt).isoformat(timespec="seconds")
            direct = None
        if manual_decision:
            pass
        elif direct:
            record.chosen_time = direct.isoformat(timespec="seconds")
            record.date_source = source
            record.date_precision = "second"
            record.date_confidence = "medium" if source in {"FILE_SECONDARY", "QQ_SHOOT_TIME"} else "high"
            record.date_review = "ready"
            file_dt = parse_datetime(record.exif_time)
            qq_dt = parse_datetime(record.qq_exif_time)
            if file_dt and qq_dt and file_dt.date() != qq_dt.date():
                record.date_review = "review"
                record.date_conflict = "FILE_QQ_DATE_CONFLICT"
                record.date_note = f"文件 EXIF 为 {file_dt.date()}，QQ 服务端 EXIF 为 {qq_dt.date()}"
        else:
            record.chosen_time = ""
            record.date_source = ""
            record.date_precision = ""
            record.date_confidence = ""
            record.date_review = ""

    grouped: dict[tuple[str, str, str], list[PhotoRecord]] = {}
    for record in records:
        uploader_key = record.uploader_id or record.uploader or "unknown"
        grouped.setdefault((record.group_id, record.album_id or record.album_name, uploader_key), []).append(record)

    sessions: list[list[PhotoRecord]] = []
    for group in grouped.values():
        ordered = sorted(group, key=lambda item: (parse_datetime(item.upload_time) or datetime.max.replace(tzinfo=CHINA_TZ), _record_id(item)))
        session: list[PhotoRecord] = []
        previous: datetime | None = None
        for record in ordered:
            uploaded = parse_datetime(record.upload_time)
            if session and (uploaded is None or previous is None or (uploaded - previous).total_seconds() > session_gap_seconds):
                sessions.append(session)
                session = []
            session.append(record)
            previous = uploaded
        if session:
            sessions.append(session)

    for session in sessions:
        first = parse_datetime(session[0].upload_time)
        seed = f"{session[0].album_id}|{session[0].uploader_id or session[0].uploader}|{first.isoformat() if first else _record_id(session[0])}"
        batch_key = hashlib.sha256(seed.encode("utf-8", "ignore")).hexdigest()[:12]
        anchors: list[tuple[int, PhotoRecord, datetime]] = []
        for index, record in enumerate(session):
            record.batch_key = batch_key
            direct, _ = _direct_date(record)
            if direct:
                anchors.append((index, record, direct))
        anchor_dates = sorted({anchor.date() for _, _, anchor in anchors})

        for index, record in enumerate(session):
            if record.date_source:
                continue
            previous_anchor = next((item for item in reversed(anchors) if item[0] < index), None)
            next_anchor = next((item for item in anchors if item[0] > index), None)
            if previous_anchor and next_anchor and previous_anchor[2].date() == next_anchor[2].date():
                chosen = day_at_noon(previous_anchor[2])
                record.chosen_time = chosen.isoformat(timespec="seconds")
                record.date_source = "BATCH_NEIGHBOR"
                record.date_precision = "day"
                record.date_confidence = "high"
                record.date_review = "ready"
                record.date_anchor_ids = ",".join((_record_id(previous_anchor[1]), _record_id(next_anchor[1])))
                record.date_note = "前后日期锚点一致"
                continue
            if len(anchor_dates) == 1:
                chosen = day_at_noon(anchors[0][2])
                record.chosen_time = chosen.isoformat(timespec="seconds")
                record.date_source = "BATCH_SINGLE_DATE"
                record.date_precision = "day"
                record.date_confidence = "medium"
                record.date_review = "review"
                record.date_anchor_ids = ",".join(_record_id(item[1]) for item in anchors)
                record.date_note = "批次只有单侧或单一日期锚点，需要人工确认"
                continue

            upload = parse_datetime(record.upload_time)
            if upload:
                record.chosen_time = day_at_noon(upload).isoformat(timespec="seconds")
                record.date_source = "QQ_UPLOAD_TIME"
                record.date_precision = "day"
                record.date_confidence = "low"
                record.date_review = "review" if len(anchor_dates) > 1 else "fallback"
                if len(anchor_dates) > 1:
                    record.date_conflict = "BATCH_DATE_CONFLICT"
                    record.date_anchor_ids = ",".join(_record_id(item[1]) for item in anchors)
                    record.date_note = "批次锚点包含多个拍摄日期：" + "、".join(str(value) for value in anchor_dates)
                else:
                    record.date_note = "没有拍摄时间锚点，回退到 QQ 上传日期"
            else:
                record.date_source = "UNKNOWN"
                record.date_precision = ""
                record.date_confidence = "none"
                record.date_review = "review"
                record.date_note = "没有可信拍摄日期或 QQ 上传日期"

    source_counts: dict[str, int] = {}
    review_counts: dict[str, int] = {}
    for record in records:
        source_counts[record.date_source or "UNKNOWN"] = source_counts.get(record.date_source or "UNKNOWN", 0) + 1
        review_counts[record.date_review or "review"] = review_counts.get(record.date_review or "review", 0) + 1
    return {
        "total": len(records),
        "sessions": len(sessions),
        "sources": source_counts,
        "reviews": review_counts,
        "conflicts": sum(bool(record.date_conflict) for record in records),
    }


def apply_manual_review(
    record: PhotoRecord,
    chosen_date: Any,
    note: str = "",
    accept_suggestion: bool = False,
) -> PhotoRecord:
    """Persist a human date decision that survives later automatic previews."""
    chosen = parse_datetime(chosen_date)
    if chosen is None or not (1970 <= chosen.year <= datetime.now(CHINA_TZ).year + 1):
        raise ValueError("复核日期无效，请使用 YYYY-MM-DD 格式")
    previous_source = record.date_source or "UNKNOWN"
    record.chosen_time = day_at_noon(chosen).isoformat(timespec="seconds")
    record.reviewed_from_source = previous_source
    record.reviewed_at = datetime.now(CHINA_TZ).isoformat(timespec="seconds")
    record.manual_reviewed = "yes"
    record.date_source = "MANUAL_REVIEW"
    record.date_precision = "day"
    record.date_confidence = "manual"
    record.date_review = "ready"
    record.date_conflict = ""
    record.review_note = (note or "").strip() or (
        "人工确认当前建议日期" if accept_suggestion else "人工指定日期"
    )
    record.date_note = record.review_note
    record.error = ""
    return record


def inspect_media_quality(record: PhotoRecord, path: Path | None = None) -> str:
    if path is None or not path.exists() or Image is None:
        return record.quality_class or "MAX_AVAILABLE"
    try:
        with Image.open(path) as image:
            width, height = image.size
        record.quality_width = str(width)
        record.quality_height = str(height)
        raw = json.loads(record.raw_json or "{}")
        origin_width = int(raw.get("origin_width") or 0)
        origin_height = int(raw.get("origin_height") or 0)
        if origin_width and origin_height:
            if (width, height) == (origin_width, origin_height):
                record.quality_class = "ORIGINAL_CONFIRMED"
            elif width < origin_width or height < origin_height:
                record.quality_class = "DERIVATIVE"
            else:
                record.quality_class = "MAX_AVAILABLE"
        else:
            record.quality_class = "MAX_AVAILABLE"
    except Exception:
        record.quality_class = record.quality_class or "MAX_AVAILABLE"
    return record.quality_class


def media_suffix_for_file(path: Path) -> str:
    """Return the suffix matching the detected media format, not only the URL suffix."""
    fallback = path.suffix.lower()
    if Image is None or not path.exists():
        return fallback
    try:
        with Image.open(path) as image:
            format_name = (image.format or "").upper()
        detected = {
            "JPEG": ".jpg",
            "PNG": ".png",
            "WEBP": ".webp",
            "GIF": ".gif",
            "BMP": ".bmp",
            "TIFF": ".tif",
        }.get(format_name)
        return detected or fallback
    except Exception:
        return fallback


def media_suffix_from_content_type(content_type: str) -> str:
    value = (content_type or "").split(";", 1)[0].strip().lower()
    return {
        "image/jpeg": ".jpg", "image/jpg": ".jpg", "image/png": ".png", "image/gif": ".gif",
        "image/webp": ".webp", "image/bmp": ".bmp", "image/tiff": ".tif", "image/heic": ".heic",
        "image/heif": ".heif", "video/mp4": ".mp4", "video/quicktime": ".mov", "video/x-m4v": ".m4v",
        "video/x-msvideo": ".avi", "video/x-matroska": ".mkv",
    }.get(value, "")


def detect_media_suffix(path: Path) -> str:
    """Detect common image/video containers when QQ's URL has no extension."""
    fallback = path.suffix.lower()
    try:
        with path.open("rb") as handle:
            header = handle.read(64)
    except OSError:
        return fallback
    if header.startswith(b"\xff\xd8\xff"):
        return ".jpg"
    if header.startswith(b"\x89PNG\r\n\x1a\n"):
        return ".png"
    if header.startswith((b"GIF87a", b"GIF89a")):
        return ".gif"
    if header.startswith(b"RIFF") and header[8:12] == b"WEBP":
        return ".webp"
    if header.startswith(b"RIFF") and header[8:12] == b"AVI ":
        return ".avi"
    if len(header) >= 12 and header[4:8] == b"ftyp":
        brand = header[8:12].lower()
        return ".mov" if brand.startswith(b"qt") else ".mp4"
    if header.startswith(b"\x1a\x45\xdf\xa3"):
        return ".mkv"
    return fallback


def jpeg_contains_eoi(path: Path) -> bool:
    """Find a JPEG EOI marker even when QQ appends data after the image."""
    previous = b""
    with path.open("rb") as handle:
        while chunk := handle.read(256 * 1024):
            if b"\xff\xd9" in previous + chunk:
                return True
            previous = chunk[-1:]
    return False


def validate_download_file(path: Path, expected_suffix: str = "") -> None:
    """Reject incomplete media before replacing the destination file."""
    if not path.exists() or path.stat().st_size <= 0:
        raise ValueError("下载结果为空")
    detected = detect_media_suffix(path)
    suffix = detected or expected_suffix.lower()
    if suffix in MEDIA_EXTENSIONS - VIDEO_EXTENSIONS:
        if Image is not None:
            try:
                with Image.open(path) as image:
                    image.load()
            except Exception as exc:
                raise ValueError("图片文件不完整或已损坏") from exc
        if suffix == ".jpg" and not jpeg_contains_eoi(path):
            raise ValueError("JPEG 缺少结束标记，下载可能被截断")


def media_file_is_complete(path: Path) -> bool:
    try:
        validate_download_file(path, path.suffix.lower())
        return True
    except (OSError, ValueError):
        return False


def media_suffix_for_record(record: PhotoRecord) -> str:
    name_suffix = Path(record.original_name or "").suffix.lower()
    url_suffix = Path(urllib.parse.urlparse(record.original_url).path).suffix.lower()
    if record.media_kind == "video":
        return name_suffix if name_suffix in VIDEO_EXTENSIONS else url_suffix if url_suffix in VIDEO_EXTENSIONS else ".mp4"
    if record.media_kind == "image":
        return name_suffix if name_suffix in MEDIA_EXTENSIONS - VIDEO_EXTENSIONS else url_suffix if url_suffix in MEDIA_EXTENSIONS - VIDEO_EXTENSIONS else ".jpg"
    if name_suffix in MEDIA_EXTENSIONS:
        return name_suffix
    if url_suffix in MEDIA_EXTENSIONS:
        return url_suffix
    return ".jpg"


def analyze_raw_file(record: PhotoRecord, path: Path) -> PhotoRecord:
    detected_suffix = detect_media_suffix(path)
    if detected_suffix in VIDEO_EXTENSIONS:
        record.media_kind = "video"
        record.media_mime = record.media_mime or {".mp4": "video/mp4", ".mov": "video/quicktime", ".m4v": "video/x-m4v", ".avi": "video/x-msvideo", ".mkv": "video/x-matroska"}.get(detected_suffix, "video/*")
    elif detected_suffix in MEDIA_EXTENSIONS - VIDEO_EXTENSIONS:
        record.media_kind = record.media_kind or "image"
        record.media_mime = record.media_mime or "image/*"
    original, secondary, file_exif = read_file_exif_metadata(path)
    record.raw_path = str(path)
    record.exif_time = iso_time(original)
    record.file_secondary_time = iso_time(secondary)
    record.file_exif_json = json.dumps(file_exif, ensure_ascii=False, separators=(",", ":")) if file_exif else ""
    record.file_camera_make = file_exif.get("Make", "")
    record.file_camera_model = file_exif.get("Model", "")
    record.file_lens_model = file_exif.get("LensModel", "")
    latitude = _rational_float(file_exif.get("GPSLatitude"))
    longitude = _rational_float(file_exif.get("GPSLongitude"))
    is_image = detected_suffix in MEDIA_EXTENSIONS - VIDEO_EXTENSIONS
    record.file_gps_available = "yes" if (
        latitude is not None and longitude is not None and (latitude != 0 or longitude != 0)
    ) else "no" if is_image else ""
    apply_metadata_priority(record)
    record.sha256 = sha256_file(path)
    inspect_media_quality(record, path)
    record.analysis_version = FILE_ANALYSIS_VERSION
    record.status = "analyzed"
    record.error = ""
    return record


def raw_destination_for(record: PhotoRecord, archive_root: Path, source_suffix: str = "") -> Path:
    record.finalize_key()
    group = sanitize_component(record.group_id or "QQ_Group")
    album = sanitize_component(record.album_name)
    suffix = source_suffix.lower() or media_suffix_for_record(record)
    if record.media_kind == "video" and suffix not in VIDEO_EXTENSIONS:
        suffix = media_suffix_for_record(record)
    if suffix not in MEDIA_EXTENSIONS:
        suffix = media_suffix_for_record(record)
    identifier = sanitize_component(record.record_key, "photo", 48)
    filename = f"QQ_{identifier}{suffix}"
    return archive_root / "00_raw" / group / album / filename


def stage_raw_file(record: PhotoRecord, source: Path, archive_root: Path) -> tuple[PhotoRecord, Path]:
    destination = raw_destination_for(record, archive_root, source.suffix)
    destination.parent.mkdir(parents=True, exist_ok=True)
    if destination.exists() and destination.stat().st_size:
        if sha256_file(destination) != sha256_file(source):
            destination = unique_destination(destination.parent, destination.stem, destination.suffix)
    if not destination.exists():
        shutil.copy2(source, destination)
    return analyze_raw_file(record, destination), destination


def create_library_copy(
    record: PhotoRecord,
    archive_root: Path,
    embed_metadata: bool = False,
) -> tuple[PhotoRecord, Path]:
    source = Path(record.raw_path)
    if not record.raw_path or not source.exists():
        raise ValueError("Raw file is missing; download and analyze it first")
    if not record.chosen_time or record.date_source in {"UNKNOWN", "BATCH_SINGLE_DATE"} or record.date_review == "review":
        raise ValueError("Photo date still requires review")
    chosen = parse_datetime(record.chosen_time)
    if chosen is None:
        raise ValueError("Chosen photo date is invalid")

    group = sanitize_component(record.group_id or "QQ_Group")
    album = sanitize_component(record.album_name)
    library_dir = archive_root / "01_library" / group / album
    library_dir.mkdir(parents=True, exist_ok=True)
    suffix = media_suffix_for_file(source)
    if suffix not in MEDIA_EXTENSIONS:
        suffix = ".jpg"
    record.finalize_key()
    identifier = sanitize_component(record.record_key, "photo", 48)
    library_stem = f"{chosen.strftime('%Y-%m-%d')}__QQ_{identifier}"
    library_destination = library_dir / f"{library_stem}{suffix}"
    shutil.copy2(source, library_destination)
    if embed_metadata:
        if not embed_metadata_with_exiftool(library_destination, chosen, record.date_source, record):
            library_destination.unlink(missing_ok=True)
            raise RuntimeError("ExifTool failed to write metadata")
    set_windows_file_times(library_destination, chosen)
    record.local_path = str(library_destination)
    record.status = "archived"
    record.error = ""
    return record, library_destination


def sha256_file(path: Path) -> str:
    digest = hashlib.sha256()
    with path.open("rb") as handle:
        for chunk in iter(lambda: handle.read(1024 * 1024), b""):
            digest.update(chunk)
    return digest.hexdigest()


def set_windows_file_times(path: Path, dt: datetime) -> None:
    timestamp = dt.timestamp()
    os.utime(path, (timestamp, timestamp))
    if os.name != "nt":
        return
    try:
        from ctypes import wintypes

        access = 0x0100 | 0x0080  # FILE_WRITE_ATTRIBUTES | FILE_READ_ATTRIBUTES
        share = 0x00000001 | 0x00000002 | 0x00000004
        handle = ctypes.windll.kernel32.CreateFileW(
            str(path), access, share, None, 3, 0x02000000, None
        )
        if handle == wintypes.HANDLE(-1).value:
            return
        filetime_value = int((timestamp + 11644473600) * 10_000_000)
        filetime = wintypes.FILETIME(filetime_value & 0xFFFFFFFF, filetime_value >> 32)
        ctypes.windll.kernel32.SetFileTime(handle, ctypes.byref(filetime), None, ctypes.byref(filetime))
        ctypes.windll.kernel32.CloseHandle(handle)
    except Exception:
        pass


def _rational_float(value: Any) -> float | None:
    text = str(value or "").strip()
    if not text:
        return None
    try:
        if "/" in text and "," not in text:
            numerator, denominator = text.split("/", 1)
            divisor = float(denominator)
            return float(numerator) / divisor if divisor else None
        return float(text)
    except (TypeError, ValueError):
        return None


def _gps_decimal(value: Any) -> float | None:
    text = str(value or "").strip()
    if not text:
        return None
    parts = [part.strip() for part in text.split(",")]
    values = [_rational_float(part) for part in parts]
    if any(item is None for item in values):
        return None
    if len(values) == 1:
        return values[0]
    degrees = values[0]
    minutes = values[1] if len(values) > 1 else 0
    seconds = values[2] if len(values) > 2 else 0
    sign = -1 if degrees < 0 else 1
    return sign * (abs(degrees) + abs(minutes) / 60 + abs(seconds) / 3600)


def qq_exif_fallback_tags(record: PhotoRecord) -> dict[str, str]:
    try:
        qq = json.loads(record.qq_exif_json or "{}")
    except (TypeError, ValueError, json.JSONDecodeError):
        qq = {}
    if not isinstance(qq, dict):
        qq = {}

    tags: dict[str, str] = {}
    make = str(record.qq_camera_make or qq.get("9") or "").strip()
    model = str(record.qq_camera_model or qq.get("10") or "").strip()
    if make:
        tags["EXIF:Make"] = make
    if model:
        tags["EXIF:Model"] = model

    numeric_fields = (
        ("1", "EXIF:ISO", True),
        ("2", "EXIF:ExposureTime", False),
        ("6", "EXIF:FNumber", False),
        ("7", "EXIF:FocalLengthIn35mmFormat", True),
        ("8", "EXIF:FocalLength", False),
    )
    for key, tag, integer in numeric_fields:
        number = _rational_float(qq.get(key))
        if number is None or number <= 0:
            continue
        tags[tag] = str(int(round(number))) if integer else f"{number:.10g}"

    latitude = _gps_decimal(qq.get("13"))
    longitude = _gps_decimal(qq.get("15"))
    latitude_ref = str(qq.get("12") or "").strip().upper()
    longitude_ref = str(qq.get("14") or "").strip().upper()
    if (
        latitude is not None and longitude is not None
        and (latitude != 0 or longitude != 0)
        and latitude_ref in {"N", "S"} and longitude_ref in {"E", "W"}
    ):
        tags["EXIF:GPSLatitude"] = f"{abs(latitude):.10f}".rstrip("0").rstrip(".")
        tags["EXIF:GPSLatitudeRef"] = latitude_ref
        tags["EXIF:GPSLongitude"] = f"{abs(longitude):.10f}".rstrip("0").rstrip(".")
        tags["EXIF:GPSLongitudeRef"] = longitude_ref
        altitude = _rational_float(qq.get("17"))
        altitude_ref = str(qq.get("16") or "").strip()
        if altitude is not None and altitude >= 0 and altitude_ref in {"0", "1"}:
            tags["EXIF:GPSAltitude"] = f"{altitude:.10g}"
            tags["EXIF:GPSAltitudeRef"] = altitude_ref
    return tags


def embed_metadata_with_exiftool(
    path: Path,
    chosen: datetime,
    source: str,
    record: PhotoRecord | None = None,
) -> bool:
    executable = find_exiftool()
    if not executable:
        return False
    exif_value = chosen.strftime("%Y:%m:%d %H:%M:%S")
    iso_value = chosen.isoformat(timespec="seconds")
    suffix = media_suffix_for_file(path)
    # ExifTool write mode "cg" creates missing tags/groups without changing existing values.
    args = [executable, "-overwrite_original", "-P", "-wm", "cg"]
    if suffix in {".jpg", ".jpeg", ".tif", ".tiff", ".png", ".webp", ".gif"}:
        args.extend([
            f"-EXIF:DateTimeOriginal={exif_value}",
            f"-EXIF:CreateDate={exif_value}",
            f"-XMP-photoshop:DateCreated={iso_value}",
        ])
        if record is not None:
            args.extend(f"-{tag}={value}" for tag, value in qq_exif_fallback_tags(record).items())
    elif suffix in {".mp4", ".mov", ".m4v"}:
        args.extend([f"-QuickTime:CreateDate={exif_value}", f"-QuickTime:MediaCreateDate={exif_value}"])
    else:
        return False
    args.extend([f"-XMP-dc:Description=Date source: {source}", str(path)])
    env = os.environ.copy()
    env.update({"LC_ALL": "C", "LC_CTYPE": "C", "LANG": "C"})
    completed = subprocess.run(args, capture_output=True, text=True, timeout=120, env=env)
    return completed.returncode == 0


CAPTURE_PRESERVE_FIELDS = {
    "raw_path", "local_path", "exif_time", "file_secondary_time", "file_exif_json",
    "file_camera_make", "file_camera_model", "file_lens_model", "file_gps_available",
    "camera_make", "camera_model", "camera_make_source", "camera_model_source",
    "gps_available", "gps_source", "chosen_time", "date_source", "date_precision",
    "date_confidence", "date_review", "date_conflict", "date_note", "date_anchor_ids",
    "manual_reviewed", "reviewed_at", "reviewed_from_source", "review_note",
    "quality_class", "quality_width", "quality_height", "batch_key", "sha256",
    "analysis_version", "status", "error",
}


class ArchiveDatabase:
    def __init__(self, path: Path):
        self.path = path
        self.path.parent.mkdir(parents=True, exist_ok=True)
        self.lock = threading.RLock()
        self.conn = sqlite3.connect(path, check_same_thread=False)
        self.conn.row_factory = sqlite3.Row
        columns = ",\n".join(f"{field.name} TEXT NOT NULL DEFAULT ''" for field in fields(PhotoRecord))
        self.conn.execute(f"CREATE TABLE IF NOT EXISTS photos ({columns}, PRIMARY KEY(record_key))")
        existing = {row[1] for row in self.conn.execute("PRAGMA table_info(photos)")}
        for field in fields(PhotoRecord):
            if field.name not in existing:
                self.conn.execute(f"ALTER TABLE photos ADD COLUMN {field.name} TEXT NOT NULL DEFAULT ''")
        self.conn.execute("CREATE INDEX IF NOT EXISTS idx_photo_id ON photos(photo_id)")
        self.conn.commit()

    def upsert(self, records: Iterable[PhotoRecord]) -> int:
        names = [field.name for field in fields(PhotoRecord)]
        placeholders = ",".join("?" for _ in names)
        updates = ",".join(
            (
                f"{name}=CASE WHEN photos.{name}<>'' THEN photos.{name} ELSE excluded.{name} END"
                if name in CAPTURE_PRESERVE_FIELDS
                else f"{name}=CASE WHEN excluded.{name}<>'' THEN excluded.{name} ELSE photos.{name} END"
            )
            for name in names if name != "record_key"
        )
        sql = f"INSERT INTO photos ({','.join(names)}) VALUES ({placeholders}) ON CONFLICT(record_key) DO UPDATE SET {updates}"
        count = 0
        with self.lock:
            for record in records:
                record.finalize_key()
                self.conn.execute(sql, [str(getattr(record, name) or "") for name in names])
                count += 1
            self.conn.commit()
        return count

    def replace(self, records: Iterable[PhotoRecord]) -> int:
        names = [field.name for field in fields(PhotoRecord)]
        placeholders = ",".join("?" for _ in names)
        updates = ",".join(f"{name}=excluded.{name}" for name in names if name != "record_key")
        sql = f"INSERT INTO photos ({','.join(names)}) VALUES ({placeholders}) ON CONFLICT(record_key) DO UPDATE SET {updates}"
        count = 0
        with self.lock:
            for record in records:
                record.finalize_key()
                self.conn.execute(sql, [str(getattr(record, name) or "") for name in names])
                count += 1
            self.conn.commit()
        return count

    def all(self, include_raw_json: bool = True) -> list[PhotoRecord]:
        names = [field.name for field in fields(PhotoRecord) if include_raw_json or field.name != "raw_json"]
        with self.lock:
            rows = self.conn.execute(
                f"SELECT {','.join(names)} FROM photos ORDER BY album_name, upload_time, original_name"
            ).fetchall()
        return [PhotoRecord(**dict(row)) for row in rows]

    def get(self, record_key: str) -> PhotoRecord | None:
        with self.lock:
            row = self.conn.execute("SELECT * FROM photos WHERE record_key=?", (record_key,)).fetchone()
        return PhotoRecord(**dict(row)) if row else None

    def update(self, record: PhotoRecord) -> None:
        self.replace([record])

    def close(self) -> None:
        with self.lock:
            self.conn.close()

    def export(self, metadata_dir: Path) -> None:
        metadata_dir.mkdir(parents=True, exist_ok=True)
        records = self.all()
        payload = {
            "schemaVersion": SCHEMA_VERSION,
            "exportedAt": datetime.now(CHINA_TZ).isoformat(timespec="seconds"),
            "photos": [asdict(record) for record in records],
        }
        (metadata_dir / "photos.json").write_text(json.dumps(payload, ensure_ascii=False, indent=2), encoding="utf-8")
        with (metadata_dir / "photos.csv").open("w", encoding="utf-8-sig", newline="") as handle:
            writer = csv.DictWriter(handle, fieldnames=[field.name for field in fields(PhotoRecord)])
            writer.writeheader()
            writer.writerows(asdict(record) for record in records)


class FileMatcher:
    def __init__(self, source_dir: Path):
        self.source_dir = source_dir
        self.files = [path for path in source_dir.rglob("*") if path.is_file() and path.suffix.lower() in MEDIA_EXTENSIONS]
        self.by_name: dict[str, list[Path]] = {}
        self.by_stem: dict[str, list[Path]] = {}
        for path in self.files:
            self.by_name.setdefault(path.name.lower(), []).append(path)
            self.by_stem.setdefault(path.stem.lower(), []).append(path)

    def match(self, record: PhotoRecord) -> Path | None:
        if record.raw_path and Path(record.raw_path).exists():
            return Path(record.raw_path)
        if record.local_path and Path(record.local_path).exists():
            return Path(record.local_path)
        names = []
        if record.original_name:
            names.append(Path(record.original_name).name.lower())
        if record.original_url:
            url_name = urllib.parse.unquote(Path(urllib.parse.urlparse(record.original_url).path).name).lower()
            if url_name:
                names.append(url_name)
        for name in names:
            matches = self.by_name.get(name, [])
            if len(matches) == 1:
                return matches[0]
        if record.photo_id:
            token = record.photo_id.lower()
            candidates = [path for path in self.files if token in path.name.lower()]
            if len(candidates) == 1:
                return candidates[0]
        return None


class Downloader:
    def __init__(self, target: Path, concurrency: int = 3):
        self.target = target
        self.target.mkdir(parents=True, exist_ok=True)
        self.concurrency = max(1, min(concurrency, 6))

    def download_one(
        self,
        record: PhotoRecord,
        destination: Path | None = None,
        cancel_event: threading.Event | None = None,
    ) -> Path:
        if not record.original_url:
            raise ValueError("No original URL")
        parsed = urllib.parse.urlparse(record.original_url)
        url_name = urllib.parse.unquote(Path(parsed.path).name)
        suffix = media_suffix_for_record(record)
        original = sanitize_component(record.original_name or f"{record.photo_id or record.record_key}{suffix}")
        original_path = Path(original)
        actual_suffix = original_path.suffix.lower() if original_path.suffix.lower() in MEDIA_EXTENSIONS else suffix
        if record.media_kind == "video" and actual_suffix not in VIDEO_EXTENSIONS:
            actual_suffix = suffix
        token = sanitize_component(record.photo_id or record.record_key, "photo", 36)
        name = f"{original_path.stem}__{token}{actual_suffix}"
        album = sanitize_component(record.album_name)
        destination = destination or self.target / album / name
        if destination.suffix.lower() not in MEDIA_EXTENSIONS or (record.media_kind == "video" and destination.suffix.lower() not in VIDEO_EXTENSIONS):
            destination = destination.with_suffix(actual_suffix)
        destination.parent.mkdir(parents=True, exist_ok=True)
        if destination.exists() and destination.stat().st_size > 0:
            return destination
        part = destination.with_suffix(destination.suffix + ".part")
        headers = {
            "User-Agent": "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 Chrome/140 Safari/537.36",
            "Accept": "image/avif,image/webp,image/apng,image/svg+xml,image/*,video/*,application/octet-stream,*/*;q=0.8",
        }
        if record.source_page:
            headers["Referer"] = record.source_page
        error = None
        for attempt in range(3):
            if cancel_event and cancel_event.is_set():
                part.unlink(missing_ok=True)
                raise DownloadCancelled("Download cancelled")
            try:
                request = urllib.request.Request(record.original_url, headers=headers)
                with urllib.request.urlopen(request, timeout=45) as response, part.open("wb") as handle:
                    response_headers = getattr(response, "headers", None)
                    content_type = response_headers.get("Content-Type", "") if response_headers is not None else ""
                    content_length = response_headers.get("Content-Length", "") if response_headers is not None else ""
                    try:
                        expected_length = int(content_length) if str(content_length).strip() else None
                    except (TypeError, ValueError):
                        expected_length = None
                    received_length = 0
                    while True:
                        if cancel_event and cancel_event.is_set():
                            raise DownloadCancelled("Download cancelled")
                        chunk = response.read(256 * 1024)
                        if not chunk:
                            break
                        handle.write(chunk)
                        received_length += len(chunk)
                if expected_length is not None and received_length != expected_length:
                    raise IOError(f"下载响应不完整：收到 {received_length} / {expected_length} 字节")
                if cancel_event and cancel_event.is_set():
                    raise DownloadCancelled("Download cancelled")
                detected_suffix = media_suffix_from_content_type(content_type) or detect_media_suffix(part)
                validate_download_file(part, detected_suffix)
                final_destination = destination
                if detected_suffix in MEDIA_EXTENSIONS and detected_suffix != destination.suffix.lower():
                    final_destination = destination.with_suffix(detected_suffix)
                final_destination.parent.mkdir(parents=True, exist_ok=True)
                if final_destination.exists() and final_destination.stat().st_size > 0:
                    part.unlink(missing_ok=True)
                    return final_destination
                part.replace(final_destination)
                return final_destination
            except DownloadCancelled:
                part.unlink(missing_ok=True)
                raise
            except Exception as exc:
                error = exc
                time.sleep(1.5 * (attempt + 1))
        part.unlink(missing_ok=True)
        raise RuntimeError(str(error))


def unique_destination(base: Path, stem: str, suffix: str) -> Path:
    candidate = base / f"{stem}{suffix}"
    counter = 2
    while candidate.exists():
        candidate = base / f"{stem}__{counter}{suffix}"
        counter += 1
    return candidate


def archive_record(
    record: PhotoRecord,
    source: Path,
    archive_root: Path,
    embed_metadata: bool = False,
) -> tuple[PhotoRecord, Path]:
    record, _ = stage_raw_file(record, source, archive_root)
    apply_date_preview([record])
    return create_library_copy(record, archive_root, embed_metadata)


def write_reports(database: ArchiveDatabase, archive_root: Path) -> None:
    reports = archive_root / "reports"
    reports.mkdir(parents=True, exist_ok=True)
    records = database.all()
    categories = {
        "missing_capture_time.csv": [r for r in records if r.date_source in {"QQ_UPLOAD_TIME", "UNKNOWN"}],
        "needs_review.csv": [r for r in records if r.date_review == "review"],
        "date_conflicts.csv": [r for r in records if r.date_conflict],
        "failed.csv": [r for r in records if r.status in {"failed", "download_failed", "organize_failed"}],
        "download_failed.csv": [r for r in records if r.status == "download_failed"],
        "organize_failed.csv": [r for r in records if r.status == "organize_failed"],
        "unmatched.csv": [r for r in records if r.status in {"pending", "unmatched"}],
    }
    names = [field.name for field in fields(PhotoRecord)]
    for filename, rows in categories.items():
        with (reports / filename).open("w", encoding="utf-8-sig", newline="") as handle:
            writer = csv.DictWriter(handle, fieldnames=names)
            writer.writeheader()
            writer.writerows(asdict(row) for row in rows)
    media_counts: dict[tuple[str, str], int] = {}
    for row in records:
        key = (row.media_kind or "unknown", row.media_mime or "")
        media_counts[key] = media_counts.get(key, 0) + 1
    with (reports / "media_summary.csv").open("w", encoding="utf-8-sig", newline="") as handle:
        writer = csv.DictWriter(handle, fieldnames=["media_kind", "media_mime", "count"])
        writer.writeheader()
        writer.writerows({"media_kind": kind, "media_mime": mime, "count": count} for (kind, mime), count in sorted(media_counts.items()))



class ImportServer:
    def __init__(self, callback: Callable[[list[PhotoRecord]], None], port: int = 8765):
        self.callback = callback
        self.port = port
        self.server: ThreadingHTTPServer | None = None
        self.thread: threading.Thread | None = None

    def start(self) -> None:
        callback = self.callback

        class Handler(BaseHTTPRequestHandler):
            def _headers(self, status=200):
                self.send_response(status)
                self.send_header("Content-Type", "application/json; charset=utf-8")
                self.send_header("Access-Control-Allow-Origin", "*")
                self.send_header("Access-Control-Allow-Headers", "Content-Type")
                self.end_headers()

            def do_OPTIONS(self):  # noqa: N802
                self._headers(204)

            def do_GET(self):  # noqa: N802
                self._headers()
                self.wfile.write(json.dumps({"ok": True, "app": APP_NAME}).encode())

            def do_POST(self):  # noqa: N802
                if self.path != "/import":
                    self._headers(404)
                    return
                try:
                    size = int(self.headers.get("Content-Length", "0"))
                    if size > 100 * 1024 * 1024:
                        raise ValueError("Payload too large")
                    payload = json.loads(self.rfile.read(size).decode("utf-8"))
                    records = extract_records(payload, "userscript")
                    callback(records)
                    self._headers()
                    self.wfile.write(json.dumps({"ok": True, "count": len(records)}).encode())
                except Exception as exc:
                    self._headers(400)
                    self.wfile.write(json.dumps({"ok": False, "error": str(exc)}).encode())

            def log_message(self, *_args):
                return

        self.server = ThreadingHTTPServer(("127.0.0.1", self.port), Handler)
        self.thread = threading.Thread(target=self.server.serve_forever, daemon=True)
        self.thread.start()

    def stop(self) -> None:
        if self.server:
            self.server.shutdown()
            self.server.server_close()


class ArchiverGUI:
    def __init__(self, root, workspace: Path):
        import tkinter as tk
        from tkinter import filedialog, messagebox, ttk

        self.tk = tk
        self.ttk = ttk
        self.filedialog = filedialog
        self.messagebox = messagebox
        self.root = root
        self.workspace = workspace
        self.workspace.mkdir(parents=True, exist_ok=True)
        self.database = ArchiveDatabase(self.workspace / "metadata" / "archive.sqlite3")
        self.server: ImportServer | None = None
        self.worker: threading.Thread | None = None

        root.title("QQ 群相册归档器")
        root.geometry("1120x720")
        root.minsize(900, 600)
        self._configure_style()

        self.source_var = tk.StringVar(value=str(self.workspace / "incoming"))
        self.archive_var = tk.StringVar(value=str(self.workspace / "archive"))
        self.download_var = tk.BooleanVar(value=True)
        self.embed_var = tk.BooleanVar(value=False)
        self.status_var = tk.StringVar(value="就绪")
        self.count_var = tk.StringVar(value="0 张照片")

        self._build_ui()
        self.refresh_table()
        root.protocol("WM_DELETE_WINDOW", self.on_close)

    def _configure_style(self):
        style = self.ttk.Style()
        try:
            style.theme_use("vista")
        except Exception:
            pass
        style.configure("Treeview", rowheight=27, font=("Microsoft YaHei UI", 9))
        style.configure("Treeview.Heading", font=("Microsoft YaHei UI", 9, "bold"))
        style.configure("TButton", padding=(10, 6), font=("Microsoft YaHei UI", 9))
        style.configure("TLabel", font=("Microsoft YaHei UI", 9))

    def _build_ui(self):
        ttk = self.ttk
        outer = ttk.Frame(self.root, padding=14)
        outer.pack(fill="both", expand=True)

        header = ttk.Frame(outer)
        header.pack(fill="x", pady=(0, 12))
        ttk.Label(header, text="QQ 群相册归档器", font=("Microsoft YaHei UI", 17, "bold")).pack(side="left")
        ttk.Label(header, textvariable=self.count_var).pack(side="right")

        paths = ttk.Frame(outer)
        paths.pack(fill="x", pady=(0, 10))
        ttk.Label(paths, text="下载目录", width=10).grid(row=0, column=0, sticky="w", pady=4)
        ttk.Entry(paths, textvariable=self.source_var).grid(row=0, column=1, sticky="ew", padx=6)
        ttk.Button(paths, text="选择", command=self.choose_source).grid(row=0, column=2)
        ttk.Label(paths, text="归档目录", width=10).grid(row=1, column=0, sticky="w", pady=4)
        ttk.Entry(paths, textvariable=self.archive_var).grid(row=1, column=1, sticky="ew", padx=6)
        ttk.Button(paths, text="选择", command=self.choose_archive).grid(row=1, column=2)
        paths.columnconfigure(1, weight=1)

        commands = ttk.Frame(outer)
        commands.pack(fill="x", pady=(0, 10))
        ttk.Button(commands, text="导入 JSON / CSV / HAR", command=self.import_file).pack(side="left", padx=(0, 6))
        ttk.Button(commands, text="启动浏览器接收", command=self.start_server).pack(side="left", padx=6)
        ttk.Button(commands, text="匹配本地文件", command=self.match_files).pack(side="left", padx=6)
        ttk.Button(commands, text="开始归档", command=self.process).pack(side="left", padx=6)
        ttk.Checkbutton(commands, text="缺失文件时下载", variable=self.download_var).pack(side="right", padx=8)
        ttk.Checkbutton(commands, text="ExifTool 写入副本", variable=self.embed_var).pack(side="right", padx=8)

        columns = ("album", "name", "upload", "chosen", "source", "match", "status")
        self.tree = ttk.Treeview(outer, columns=columns, show="headings", selectmode="browse")
        headings = {
            "album": "相册", "name": "文件 / 照片 ID", "upload": "QQ 上传时间",
            "chosen": "采用时间", "source": "日期来源", "match": "本地匹配", "status": "状态",
        }
        widths = {"album": 150, "name": 220, "upload": 145, "chosen": 145, "source": 120, "match": 85, "status": 90}
        for key in columns:
            self.tree.heading(key, text=headings[key])
            self.tree.column(key, width=widths[key], minwidth=70, stretch=key in {"album", "name"})
        scroll = ttk.Scrollbar(outer, orient="vertical", command=self.tree.yview)
        self.tree.configure(yscrollcommand=scroll.set)
        self.tree.pack(side="left", fill="both", expand=True)
        scroll.pack(side="right", fill="y")

        status = ttk.Frame(self.root, padding=(14, 6))
        status.pack(fill="x", side="bottom")
        ttk.Label(status, textvariable=self.status_var).pack(side="left")
        ttk.Label(status, text="接收地址：http://127.0.0.1:8765/import").pack(side="right")

    def choose_source(self):
        value = self.filedialog.askdirectory(initialdir=self.source_var.get())
        if value:
            self.source_var.set(value)

    def choose_archive(self):
        value = self.filedialog.askdirectory(initialdir=self.archive_var.get())
        if value:
            self.archive_var.set(value)

    def import_file(self):
        path = self.filedialog.askopenfilename(filetypes=[("Metadata", "*.json *.csv *.har"), ("All files", "*.*")])
        if not path:
            return
        try:
            records = load_manifest(Path(path))
            count = self.database.upsert(records)
            self.status_var.set(f"已导入 {count} 条记录")
            self.refresh_table()
        except Exception as exc:
            self.messagebox.showerror("导入失败", str(exc))

    def start_server(self):
        if self.server:
            self.status_var.set("浏览器接收服务已在运行")
            return

        def callback(records):
            self.database.upsert(records)
            self.root.after(0, lambda: (self.status_var.set(f"从浏览器收到 {len(records)} 条记录"), self.refresh_table()))

        try:
            self.server = ImportServer(callback)
            self.server.start()
            self.status_var.set("浏览器接收服务已启动")
        except OSError as exc:
            self.messagebox.showerror("无法启动接收服务", str(exc))

    def refresh_table(self):
        for item in self.tree.get_children():
            self.tree.delete(item)
        records = self.database.all()
        for record in records[:20_000]:
            name = record.original_name or record.photo_id or record.record_key
            self.tree.insert("", "end", iid=record.record_key, values=(
                record.album_name, name, record.upload_time, record.chosen_time,
                record.date_source, "是" if record.local_path and Path(record.local_path).exists() else "否", record.status,
            ))
        self.count_var.set(f"{len(records)} 张照片")

    def match_files(self):
        source = Path(self.source_var.get())
        if not source.exists():
            self.messagebox.showwarning("目录不存在", "请先选择现有批量下载目录。")
            return
        matcher = FileMatcher(source)
        matched = 0
        for record in self.database.all():
            path = matcher.match(record)
            if path:
                record.local_path = str(path)
                record.status = "matched"
                record.error = ""
                self.database.update(record)
                matched += 1
            elif record.status != "archived":
                record.status = "unmatched"
                self.database.update(record)
        self.status_var.set(f"匹配完成：{matched}/{len(self.database.all())}")
        self.refresh_table()

    def process(self):
        if self.worker and self.worker.is_alive():
            return
        records = self.database.all()
        if not records:
            self.messagebox.showwarning("没有元数据", "请先导入元数据或启动浏览器接收。")
            return
        source_dir = Path(self.source_var.get())
        archive_root = Path(self.archive_var.get())
        source_dir.mkdir(parents=True, exist_ok=True)
        archive_root.mkdir(parents=True, exist_ok=True)

        def work():
            matcher = FileMatcher(source_dir)
            downloader = Downloader(source_dir / "_downloaded")
            completed = 0
            for index, record in enumerate(records, 1):
                try:
                    source = matcher.match(record)
                    if source is None and self.download_var.get() and record.original_url:
                        source = downloader.download_one(record)
                    if source is None:
                        record.status = "unmatched"
                        record.error = "No matching local file and no downloadable URL"
                    else:
                        record, _ = archive_record(record, source, archive_root, self.embed_var.get())
                        completed += 1
                except Exception as exc:
                    record.status = "failed"
                    record.error = str(exc)
                self.database.update(record)
                if index % 5 == 0 or index == len(records):
                    self.root.after(0, lambda i=index, c=completed: self.status_var.set(f"归档中 {i}/{len(records)}，成功 {c}"))
            self.database.export(archive_root / "metadata")
            write_reports(self.database, archive_root)
            self.root.after(0, lambda: (self.status_var.set(f"归档完成：成功 {completed}/{len(records)}"), self.refresh_table()))

        self.worker = threading.Thread(target=work, daemon=True)
        self.worker.start()

    def on_close(self):
        if self.server:
            self.server.stop()
        self.root.destroy()


def run_cli(args) -> int:
    workspace = Path(args.workspace).resolve()
    database = ArchiveDatabase(workspace / "metadata" / "archive.sqlite3")
    for path in args.import_files:
        records = load_manifest(Path(path))
        print(f"Imported {database.upsert(records)} records from {path}")
    if args.process:
        if not args.source or not args.archive:
            raise SystemExit("--process requires --source and --archive")
        source_dir = Path(args.source)
        archive_root = Path(args.archive)
        matcher = FileMatcher(source_dir)
        downloader = Downloader(source_dir / "_downloaded", args.concurrency)
        for record in database.all():
            try:
                source = matcher.match(record)
                if source is None and args.download and record.original_url:
                    source = downloader.download_one(record)
                if source is None:
                    record.status = "unmatched"
                else:
                    record, destination = archive_record(record, source, archive_root, args.embed)
                    print(destination)
            except Exception as exc:
                record.status = "failed"
                record.error = str(exc)
                print(f"FAILED {record.record_key}: {exc}", file=sys.stderr)
            database.update(record)
        database.export(archive_root / "metadata")
        write_reports(database, archive_root)
    return 0


def build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(description="Archive QQ group albums with provenance-aware dates.")
    parser.add_argument("--workspace", default=str(Path.home() / "Documents" / "QQAlbumArchiver"))
    parser.add_argument("--import", dest="import_files", action="append", default=[], help="JSON, CSV or HAR metadata file")
    parser.add_argument("--source", help="Existing downloaded media directory")
    parser.add_argument("--archive", help="Archive output directory")
    parser.add_argument("--process", action="store_true")
    parser.add_argument("--download", action="store_true", help="Download missing files from captured original URLs")
    parser.add_argument("--embed", action="store_true", help="Use ExifTool when installed; library copies only")
    parser.add_argument("--concurrency", type=int, default=3)
    parser.add_argument("--no-gui", action="store_true")
    return parser


def main() -> int:
    parser = build_parser()
    args = parser.parse_args()
    if args.no_gui or args.import_files or args.process:
        return run_cli(args)
    import tkinter as tk

    root = tk.Tk()
    ArchiverGUI(root, Path(args.workspace))
    root.mainloop()
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
