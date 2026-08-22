from __future__ import annotations

import json
import random
import time
import urllib.parse
import urllib.request
from dataclasses import dataclass
from typing import Any, Iterable

from poc_auth.auth_poc import QQSession, gtk, parse_jsonp
from qq_album_archiver import PhotoRecord, iso_time, normalize_url


ALBUM_LIST_URL = (
    "https://h5.qzone.qq.com/proxy/domain/u.photo.qzone.qq.com/"
    "cgi-bin/upp/qun_list_album_v2"
)
PHOTO_LIST_URL = "https://h5.qzone.qq.com/groupphoto/inqq"


@dataclass
class AlbumRecord:
    album_id: str
    title: str
    photo_count: int
    raw: dict[str, Any]


def first_value(obj: dict[str, Any], names: Iterable[str]) -> Any:
    for name in names:
        value = obj.get(name)
        if value not in (None, ""):
            return value
    return ""


def _urls_from_photo(item: dict[str, Any]) -> list[dict[str, Any]]:
    value = item.get("photourl") or item.get("photoUrl") or item.get("urls") or []
    if isinstance(value, dict):
        rows = list(value.values())
    elif isinstance(value, list):
        rows = value
    else:
        rows = []
    return [row for row in rows if isinstance(row, dict) and row.get("url")]


def choose_original_url(item: dict[str, Any]) -> str:
    rows = _urls_from_photo(item)
    if not rows:
        return normalize_url(str(first_value(item, ("originUrl", "originalUrl", "rawUrl", "url"))))
    origin = next(
        (
            row for row in rows
            if int(row.get("width") or 0) == 0 and int(row.get("height") or 0) == 0
        ),
        None,
    )
    if origin:
        return normalize_url(str(origin["url"]))
    rows.sort(
        key=lambda row: (
            int(row.get("width") or 0) * int(row.get("height") or 0),
            int(row.get("width") or 0),
            float(row.get("enlarge_rate") or 0),
        ),
        reverse=True,
    )
    return normalize_url(str(rows[0]["url"]))


def _video_url_from_data(video_data: Any) -> str:
    if not isinstance(video_data, dict):
        return ""
    for name in ("actionurl", "videourl", "videoUrl", "downloadurl", "downloadUrl", "playurl", "playUrl"):
        value = video_data.get(name)
        if isinstance(value, str) and value.strip().startswith(("http://", "https://")):
            return normalize_url(value.strip())
    nested = video_data.get("videourls") or video_data.get("videoUrls") or {}
    if isinstance(nested, dict):
        for value in nested.values():
            if isinstance(value, str) and value.strip().startswith(("http://", "https://")):
                return normalize_url(value.strip())
            if isinstance(value, dict):
                found = _video_url_from_data(value)
                if found:
                    return found
    return ""


def _video_flag(value: Any) -> bool:
    return str(value or "").strip().lower() not in {"", "0", "false", "no", "none"}


def _url_suffix(url: str) -> str:
    name = urllib.parse.unquote(urllib.parse.urlparse(url).path).lower()
    return "." + name.rsplit(".", 1)[-1] if "." in name.rsplit("/", 1)[-1] else ""


def server_exif(item: dict[str, Any]) -> dict[str, Any]:
    value = item.get("mapExifInfo") or item.get("mapexifinfo") or {}
    return value if isinstance(value, dict) else {}


def has_server_gps(exif: dict[str, Any]) -> bool:
    def nonzero(value: Any) -> bool:
        text = str(value or "").strip()
        if not text:
            return False
        numbers = []
        for part in text.split(","):
            try:
                if "/" in part:
                    numerator, denominator = part.split("/", 1)
                    numbers.append(float(numerator) / float(denominator))
                else:
                    numbers.append(float(part))
            except (ValueError, ZeroDivisionError):
                continue
        return any(abs(number) > 0.000001 for number in numbers)

    return nonzero(exif.get("13")) and nonzero(exif.get("15"))


def normalize_group_photo(
    item: dict[str, Any],
    group_id: str,
    album: AlbumRecord,
) -> PhotoRecord | None:
    video_data = item.get("videodata") or item.get("videoData") or {}
    video_url = _video_url_from_data(video_data)
    is_video = _video_flag(item.get("videoflag") or item.get("videoFlag")) or bool(video_url)
    original_url = video_url if is_video and video_url else choose_original_url(item)
    if not original_url and not video_url:
        return None

    photo_id = str(first_value(item, ("sloc", "lloc", "photoId", "photoid", "id")))
    original_name = str(first_value(item, ("name", "picname", "photoName", "title")))
    if not original_name:
        original_name = photo_id
    if is_video and not _url_suffix(original_name):
        original_name += _url_suffix(video_url) or ".mp4"
    upload_time = first_value(item, (
        "uUploadTime", "uploadtime", "uploadTime", "upload_time", "phototime", "photoTime",
        "createTime", "createtime", "create_time", "addTime", "addtime",
    ))
    shoot_time = first_value(item, (
        "shootTime", "shoottime", "shoot_time", "takenTime", "takenAt",
        "captureTime", "exifTime",
    ))
    uploader = item.get("owner") or item.get("uploader") or item.get("user") or {}
    if isinstance(uploader, dict):
        uploader_name = str(first_value(uploader, ("nickname", "nick", "name")))
        uploader_id = str(first_value(uploader, ("uin", "id", "qq")))
        if not uploader_name:
            uploader_name = str(first_value(item, ("nickname", "nick", "pic_host_nick", "uploaderName")))
        if not uploader_id:
            uploader_id = str(first_value(item, ("uploadUin", "uin", "ownerUin", "uploaderId")))
    else:
        uploader_name = str(first_value(item, ("nickname", "nick", "pic_host_nick", "uploaderName")))
        uploader_id = str(first_value(item, ("uploadUin", "uin", "ownerUin", "uploaderId")))

    raw = dict(item)
    if video_url:
        raw["_qq_album_archiver_video_url"] = normalize_url(str(video_url))
    exif = server_exif(item)
    record = PhotoRecord(
        photo_id=photo_id,
        album_id=album.album_id,
        album_name=album.title,
        group_id=group_id,
        uploader=uploader_name,
        uploader_id=uploader_id,
        original_name=original_name,
        original_url=original_url or normalize_url(str(video_url)),
        media_kind="video" if is_video else "image",
        media_mime=("video/" + (_url_suffix(video_url).lstrip(".") or "mp4")) if is_video else "image/*",
        upload_time=iso_time(upload_time),
        shoot_time=iso_time(shoot_time),
        qq_exif_time=iso_time(exif.get("11")),
        qq_exif_json=json.dumps(exif, ensure_ascii=False, separators=(",", ":")) if exif else "",
        qq_camera_make=str(exif.get("9") or "").strip(),
        qq_camera_model=str(exif.get("10") or "").strip(),
        qq_gps_available="yes" if has_server_gps(exif) else "no",
        source_page=f"https://h5.qzone.qq.com/groupphoto/index?groupId={group_id}",
        capture_source="qq-group-api",
        raw_json=json.dumps(raw, ensure_ascii=False, separators=(",", ":")),
    )
    record.finalize_key()
    return record


class QQGroupAlbumAPI:
    def __init__(self, session: QQSession, uin: str):
        self.session = session
        self.uin = uin

    @property
    def token(self) -> int:
        skey = self.session.cookie_value("p_skey") or self.session.cookie_value("skey")
        if not skey:
            raise RuntimeError("QQ session has neither p_skey nor skey")
        return gtk(skey)

    def list_albums(self, group_id: str) -> list[AlbumRecord]:
        params = {
            "g_tk": str(self.token),
            "callback": "shine2_Callback",
            "qunId": group_id,
            "uin": self.uin,
            "start": "0",
            "num": "1000",
            "getMemberRole": "1",
            "inCharset": "utf-8",
            "outCharset": "utf-8",
            "source": "qzone",
            "attach_info": "",
            "callbackFun": "shine2",
            "t": str(random.random()),
        }
        text = self.session.request_text(
            f"{ALBUM_LIST_URL}?{urllib.parse.urlencode(params)}",
            {"Referer": f"https://h5.qzone.qq.com/groupphoto/index?groupId={group_id}"},
        )
        if "对不起，您" in text:
            raise PermissionError("The current QQ account cannot access this group album")
        payload = parse_jsonp(text)
        data = payload.get("data", {}) if isinstance(payload, dict) else {}
        rows = data.get("album") or []
        albums = []
        for row in rows:
            if not isinstance(row, dict):
                continue
            album_id = str(first_value(row, ("id", "albumId", "topicId")))
            title = str(first_value(row, ("title", "name", "albumName"))) or "Unnamed album"
            try:
                count = int(first_value(row, ("photocnt", "photoCount", "total")) or 0)
            except (TypeError, ValueError):
                count = 0
            albums.append(AlbumRecord(album_id, title, count, row))
        return albums

    def photo_page(self, group_id: str, album: AlbumRecord, start: int = 0, page_size: int = 36) -> list[PhotoRecord]:
        params = {"g_tk": str(self.token)}
        form = {
            "qunId": group_id,
            "albumId": album.album_id,
            "uin": self.uin,
            "start": str(start),
            "num": str(page_size),
            "getCommentCnt": "0",
            "getMemberRole": "0",
            "hostUin": self.uin,
            "getalbum": "0",
            "platform": "qzone",
            "inCharset": "utf-8",
            "outCharset": "utf-8",
            "source": "qzone",
            "cmd": "qunGetPhotoList",
            "qunid": group_id,
            "albumid": album.album_id,
            "attach_info": f"start_count={start}",
        }
        encoded_form = urllib.parse.urlencode(form)

        def fetch(body: str) -> dict[str, Any]:
            request = urllib.request.Request(
                f"{PHOTO_LIST_URL}?{urllib.parse.urlencode(params)}",
                data=body.encode("utf-8"),
                headers={
                    "User-Agent": self.session.bundle("runtime", self.uin).user_agent,
                    "Referer": f"https://h5.qzone.qq.com/groupphoto/index?groupId={group_id}",
                    "Accept": "application/json, text/javascript, */*; q=0.01",
                    "Content-Type": "application/x-www-form-urlencoded; charset=UTF-8",
                    "X-Requested-With": "XMLHttpRequest",
                },
                method="POST",
            )
            with self.session.opener.open(request, timeout=30) as response:
                return json.loads(response.read().decode("utf-8", "replace"))

        payload = fetch(encoded_form)
        data = payload.get("data", {}) if isinstance(payload, dict) else {}
        rows = data.get("photolist") or data.get("photoList") or []
        if not rows:
            # The reference Electron implementation sends the form string with
            # an extra pair of quotes. Keep it as a compatibility fallback.
            quoted_payload = fetch(f'"{encoded_form}"')
            quoted_data = quoted_payload.get("data", {}) if isinstance(quoted_payload, dict) else {}
            rows = quoted_data.get("photolist") or quoted_data.get("photoList") or []
        records = [normalize_group_photo(row, group_id, album) for row in rows if isinstance(row, dict)]
        return [record for record in records if record]

    def all_photos(
        self,
        group_id: str,
        album: AlbumRecord,
        progress=None,
        delay_seconds: float = 0.25,
    ) -> list[PhotoRecord]:
        records: list[PhotoRecord] = []
        seen: set[str] = set()
        start = 0
        max_pages = max(3, (album.photo_count // 30) + 10)
        for _ in range(max_pages):
            page = self.photo_page(group_id, album, start)
            new_rows = []
            for record in page:
                key = record.photo_id or record.original_url
                if key not in seen:
                    seen.add(key)
                    new_rows.append(record)
            records.extend(new_rows)
            if progress:
                progress(album, len(records), album.photo_count)
            if not page or not new_rows or (album.photo_count and len(records) >= album.photo_count):
                break
            start += 40
            time.sleep(delay_seconds)
        return records
