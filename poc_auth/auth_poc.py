from __future__ import annotations

import argparse
import base64
import csv
import ctypes
import ctypes.wintypes
import http.client
import http.cookiejar
import io
import json
import os
import random
import re
import secrets
import sys
import tempfile
import time
import urllib.parse
import urllib.request
import urllib.error
import webbrowser
from dataclasses import asdict, dataclass
from datetime import datetime, timezone
from pathlib import Path
from typing import Any


APP_ID = "549000912"
DAID = "5"
LOGIN_SUCCESS_URL = "https://qzs.qzone.qq.com/qzone/v5/loginsucc.html?para=izone"
USER_AGENT = (
    "Mozilla/5.0 (Windows NT 10.0; Win64; x64) "
    "AppleWebKit/537.36 (KHTML, like Gecko) Chrome/140.0.0.0 Safari/537.36"
)
QR_SHOW_URL = "https://ssl.ptlogin2.qq.com/ptqrshow"
QR_POLL_URL = "https://ssl.ptlogin2.qq.com/ptqrlogin"
XLOGIN_URL = "https://xui.ptlogin2.qq.com/cgi-bin/xlogin"
LOCAL_UINS_URL = "http://localhost.ptlogin2.qq.com:4301/pt_get_uins"
LOCAL_TICKET_URL = "http://localhost.ptlogin2.qq.com:4301/pt_get_st"
LOCAL_JUMP_URL = "https://ptlogin2.qzone.qq.com/jump"
GROUP_ALBUM_URL = (
    "https://h5.qzone.qq.com/proxy/domain/u.photo.qzone.qq.com/"
    "cgi-bin/upp/qun_list_album_v2"
)
if getattr(sys, "frozen", False):
    _APP_DATA_ROOT = Path(os.environ.get("LOCALAPPDATA", str(Path.home() / "AppData" / "Local"))) / "QQAlbumArchiver"
else:
    _APP_DATA_ROOT = Path(__file__).resolve().parents[3]
DEFAULT_SESSION_PATH = _APP_DATA_ROOT / "work" / "qq-album-session.dpapi"


def hash33(value: str, seed: int = 0) -> int:
    result = seed
    for character in value:
        result += (result << 5) + ord(character)
    return result & 0x7FFFFFFF


def gtk(skey: str) -> int:
    return hash33(skey, 5381)


def parse_ptui_callback(text: str) -> list[str]:
    match = re.search(r"ptuiCB\((.*)\)\s*;?", text, re.S)
    if not match:
        raise ValueError(f"Unexpected ptlogin response: {text[:160]}")
    return next(csv.reader(io.StringIO(match.group(1)), quotechar="'", skipinitialspace=True))


def parse_jsonp(text: str) -> Any:
    text = text.strip().lstrip("\ufeff")
    try:
        return json.loads(text)
    except json.JSONDecodeError:
        pass
    match = re.match(r"^[\w$.]+\((.*)\)\s*;?$", text, re.S)
    if not match:
        raise ValueError(f"Unexpected JSONP response: {text[:160]}")
    return json.loads(match.group(1))


def normalize_uin(value: str) -> str:
    return str(value or "").lstrip("o0") or "0"


@dataclass
class SavedCookie:
    name: str
    value: str
    domain: str
    path: str = "/"
    secure: bool = False
    expires: int | None = None


@dataclass
class SessionBundle:
    schema_version: int
    auth_method: str
    created_at: str
    uin: str
    user_agent: str
    cookies: list[SavedCookie]

    @property
    def cookie_names(self) -> list[str]:
        return sorted({cookie.name for cookie in self.cookies})


class Blob(ctypes.Structure):
    _fields_ = [("cbData", ctypes.wintypes.DWORD), ("pbData", ctypes.POINTER(ctypes.c_ubyte))]


def _blob(data: bytes) -> tuple[Blob, ctypes.Array]:
    buffer = (ctypes.c_ubyte * len(data)).from_buffer_copy(data)
    return Blob(len(data), ctypes.cast(buffer, ctypes.POINTER(ctypes.c_ubyte))), buffer


def _configure_dpapi():
    crypt32 = ctypes.windll.crypt32
    kernel32 = ctypes.windll.kernel32
    crypt32.CryptProtectData.argtypes = [
        ctypes.POINTER(Blob), ctypes.wintypes.LPCWSTR, ctypes.POINTER(Blob),
        ctypes.c_void_p, ctypes.c_void_p, ctypes.wintypes.DWORD, ctypes.POINTER(Blob),
    ]
    crypt32.CryptProtectData.restype = ctypes.wintypes.BOOL
    crypt32.CryptUnprotectData.argtypes = [
        ctypes.POINTER(Blob), ctypes.POINTER(ctypes.wintypes.LPWSTR), ctypes.POINTER(Blob),
        ctypes.c_void_p, ctypes.c_void_p, ctypes.wintypes.DWORD, ctypes.POINTER(Blob),
    ]
    crypt32.CryptUnprotectData.restype = ctypes.wintypes.BOOL
    kernel32.LocalFree.argtypes = [ctypes.c_void_p]
    kernel32.LocalFree.restype = ctypes.c_void_p
    return crypt32, kernel32


def dpapi_protect(data: bytes, description: str = "QQ Album Archiver session") -> bytes:
    if os.name != "nt":
        raise RuntimeError("DPAPI is available only on Windows")
    input_blob, input_buffer = _blob(data)
    output_blob = Blob()
    crypt32, kernel32 = _configure_dpapi()
    if not crypt32.CryptProtectData(
        ctypes.byref(input_blob), description, None, None, None, 0x1, ctypes.byref(output_blob)
    ):
        raise ctypes.WinError()
    try:
        return ctypes.string_at(output_blob.pbData, output_blob.cbData)
    finally:
        kernel32.LocalFree(output_blob.pbData)
        del input_buffer


def dpapi_unprotect(data: bytes) -> bytes:
    if os.name != "nt":
        raise RuntimeError("DPAPI is available only on Windows")
    input_blob, input_buffer = _blob(data)
    output_blob = Blob()
    crypt32, kernel32 = _configure_dpapi()
    if not crypt32.CryptUnprotectData(
        ctypes.byref(input_blob), None, None, None, None, 0x1, ctypes.byref(output_blob)
    ):
        raise ctypes.WinError()
    try:
        return ctypes.string_at(output_blob.pbData, output_blob.cbData)
    finally:
        kernel32.LocalFree(output_blob.pbData)
        del input_buffer


def save_session(bundle: SessionBundle, path: Path) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    payload = asdict(bundle)
    payload["cookies"] = [asdict(cookie) for cookie in bundle.cookies]
    plaintext = json.dumps(payload, ensure_ascii=False, separators=(",", ":")).encode("utf-8")
    protected = dpapi_protect(plaintext)
    path.write_bytes(b"QQA1" + protected)


def load_session(path: Path) -> SessionBundle:
    raw = path.read_bytes()
    if not raw.startswith(b"QQA1"):
        raise ValueError("Unsupported session file")
    payload = json.loads(dpapi_unprotect(raw[4:]).decode("utf-8"))
    payload["cookies"] = [SavedCookie(**cookie) for cookie in payload["cookies"]]
    return SessionBundle(**payload)


class QQSession:
    def __init__(self):
        self.jar = http.cookiejar.CookieJar()
        self.opener = urllib.request.build_opener(
            urllib.request.ProxyHandler({}),
            urllib.request.HTTPCookieProcessor(self.jar),
        )

    def cookie_value(self, name: str) -> str:
        values = [cookie.value for cookie in self.jar if cookie.name == name]
        return values[-1] if values else ""

    def request(self, url: str, headers: dict[str, str] | None = None, timeout: int = 20) -> bytes:
        request_headers = {"User-Agent": USER_AGENT, **(headers or {})}
        request = urllib.request.Request(url, headers=request_headers)
        with self.opener.open(request, timeout=timeout) as response:
            return response.read()

    def request_text(self, url: str, headers: dict[str, str] | None = None, timeout: int = 20) -> str:
        return self.request(url, headers, timeout).decode("utf-8", "replace")

    def bundle(self, auth_method: str, selected_uin: str = "") -> SessionBundle:
        cookies = [
            SavedCookie(
                name=cookie.name,
                value=cookie.value,
                domain=cookie.domain,
                path=cookie.path,
                secure=cookie.secure,
                expires=cookie.expires,
            )
            for cookie in self.jar
        ]
        cookie_uin = normalize_uin(self.cookie_value("uin"))
        uin = cookie_uin if cookie_uin.isdigit() else selected_uin
        return SessionBundle(
            schema_version=1,
            auth_method=auth_method,
            created_at=datetime.now(timezone.utc).astimezone().isoformat(timespec="seconds"),
            uin=uin,
            user_agent=USER_AGENT,
            cookies=cookies,
        )

    @classmethod
    def from_bundle(cls, bundle: SessionBundle) -> "QQSession":
        session = cls()
        for item in bundle.cookies:
            session.jar.set_cookie(http.cookiejar.Cookie(
                version=0,
                name=item.name,
                value=item.value,
                port=None,
                port_specified=False,
                domain=item.domain,
                domain_specified=bool(item.domain),
                domain_initial_dot=item.domain.startswith("."),
                path=item.path,
                path_specified=True,
                secure=item.secure,
                expires=item.expires,
                discard=item.expires is None,
                comment=None,
                comment_url=None,
                rest={},
                rfc2109=False,
            ))
        return session


def show_qr_image(data: bytes) -> Path:
    path = Path(tempfile.gettempdir()) / "qq-album-login-qr.png"
    path.write_bytes(data)
    html_path = path.with_suffix(".html")
    encoded = base64.b64encode(data).decode("ascii")
    html_path.write_text(f'''<!doctype html>
<html lang="zh-CN"><meta charset="utf-8"><title>QQ 相册归档器登录</title>
<style>
body {{ margin: 0; min-height: 100vh; display: grid; place-items: center; background: #f4f6f8; color: #202124; font: 16px "Microsoft YaHei UI", sans-serif; }}
main {{ text-align: center; }}
img {{ width: 288px; height: 288px; image-rendering: pixelated; background: white; padding: 16px; border: 1px solid #c8ccd0; border-radius: 6px; }}
h1 {{ font-size: 22px; font-weight: 600; margin: 0 0 18px; }}
p {{ color: #5f6368; margin: 14px 0 0; }}
</style><main><h1>使用手机 QQ 扫码</h1><img src="data:image/png;base64,{encoded}" alt="QQ 登录二维码"><p>在手机上确认登录；本页面不会读取密码。</p></main></html>''', encoding="utf-8")
    opened = False
    if os.name == "nt":
        edge_candidates = [
            Path(os.environ.get("PROGRAMFILES(X86)", "")) / "Microsoft/Edge/Application/msedge.exe",
            Path(os.environ.get("PROGRAMFILES", "")) / "Microsoft/Edge/Application/msedge.exe",
        ]
        for edge in edge_candidates:
            if edge.exists():
                import subprocess
                subprocess.Popen([str(edge), "--app=" + html_path.as_uri()])
                opened = True
                break
    if not opened:
        opened = webbrowser.open(html_path.as_uri())
    if not opened:
        print(f"Open QR page manually: {html_path}")
    return path


def qr_login(
    progress=print,
    timeout_seconds: int = 180,
    qr_callback=None,
) -> tuple[QQSession, str]:
    session = QQSession()
    xlogin_params = {
        "s_url": LOGIN_SUCCESS_URL,
        "appid": APP_ID,
        "style": "22",
        "proxy_url": "https://qzs.qq.com/qzone/v6/portal/proxy.html",
        "target": "self",
        "hide_title_bar": "1",
        "no_verifyimg": "1",
        "link_target": "blank",
        "hide_close_icon": "1",
        "pt_no_auth": "0",
        "daid": DAID,
    }
    session.request_text(f"{XLOGIN_URL}?{urllib.parse.urlencode(xlogin_params)}")
    login_sig = session.cookie_value("pt_login_sig")

    qr_params = {
        "appid": APP_ID,
        "e": "2",
        "l": "M",
        "s": "3",
        "d": "72",
        "v": "4",
        "t": str(random.random()),
        "daid": DAID,
        "pt_3rd_aid": "0",
    }
    qr_data = session.request(f"{QR_SHOW_URL}?{urllib.parse.urlencode(qr_params)}")
    qrsig = session.cookie_value("qrsig")
    if not qrsig:
        raise RuntimeError("QR endpoint did not return qrsig")
    if qr_callback:
        qr_callback(qr_data)
    else:
        qr_path = show_qr_image(qr_data)
        progress(f"QR image opened: {qr_path}")
    progress("Scan it with QQ and confirm login on the phone.")

    started = time.time()
    last_status = ""
    while time.time() - started < timeout_seconds:
        action = f"0-0-{int(time.time() * 1000)}"
        poll_params = {
            "u1": LOGIN_SUCCESS_URL,
            "ptqrtoken": str(hash33(qrsig)),
            "ptredirect": "0",
            "h": "1",
            "t": "1",
            "g": "1",
            "from_ui": "1",
            "ptlang": "2052",
            "action": action,
            "js_ver": "22080914",
            "js_type": "1",
            "login_sig": login_sig,
            "pt_uistyle": "40",
            "aid": APP_ID,
            "daid": DAID,
            "pt_3rd_aid": "0",
            "has_onekey": "1",
        }
        text = session.request_text(
            f"{QR_POLL_URL}?{urllib.parse.urlencode(poll_params)}",
            {"Referer": "https://xui.ptlogin2.qq.com/"},
        )
        values = parse_ptui_callback(text)
        code = values[0] if values else ""
        if code == "0":
            redirect_url = values[2]
            session.request_text(redirect_url, {"Referer": "https://xui.ptlogin2.qq.com/"})
            # The success callback's later fields can contain nickname text. The
            # authenticated numeric account is authoritative in the uin cookie.
            uin = normalize_uin(session.cookie_value("uin"))
            if not uin.isdigit():
                raise RuntimeError("Login succeeded but the numeric uin cookie was not returned")
            return session, uin
        status = {"65": "QR expired", "66": "Waiting for scan", "67": "Scanned; waiting for confirmation"}.get(code, values[-1] if values else code)
        if status != last_status:
            progress(status)
            last_status = status
        if code == "65":
            raise TimeoutError("QR code expired")
        time.sleep(2)
    raise TimeoutError("QR login timed out")


def _local_request(url: str, local_token: int) -> str:
    opener = urllib.request.build_opener(urllib.request.ProxyHandler({}))
    request = urllib.request.Request(
        url,
        headers={
            "User-Agent": USER_AGENT,
            "Cookie": f"pt_local_token={local_token}",
            "Referer": "https://xui.ptlogin2.qq.com/",
        },
    )
    with opener.open(request, timeout=8) as response:
        return response.read().decode("utf-8", "replace")


def local_qq_accounts(local_token: int) -> list[dict[str, Any]]:
    params = {
        "callback": "ptui_getuins_CB",
        "r": str(random.random()),
        "pt_local_tk": str(local_token),
    }
    try:
        text = _local_request(f"{LOCAL_UINS_URL}?{urllib.parse.urlencode(params)}", local_token)
    except (urllib.error.URLError, ConnectionError, http.client.RemoteDisconnected, TimeoutError) as exc:
        raise RuntimeError("QQNT local login service is unavailable. Start QQNT, sign in, then retry.") from exc
    payload = parse_jsonp(text)
    if isinstance(payload, dict):
        for key in ("data", "accounts", "uins"):
            if isinstance(payload.get(key), list):
                return payload[key]
    if isinstance(payload, list):
        return payload
    return []


def local_qq_login(selected_uin: str = "", progress=print) -> tuple[QQSession, str]:
    local_token = secrets.randbelow(900_000_000) + 100_000_000
    accounts = local_qq_accounts(local_token)
    if not accounts:
        raise RuntimeError("No local QQ account found. Start QQNT, sign in, then retry.")
    simplified = []
    for account in accounts:
        uin = str(account.get("uin") or account.get("account") or account.get("clientuin") or "")
        simplified.append((uin, str(account.get("nickname") or account.get("nick") or "")))
    if selected_uin:
        selected = next((item for item in simplified if item[0] == selected_uin), None)
        if not selected:
            raise ValueError(f"QQ account {selected_uin} is not signed into QQNT")
    elif len(simplified) == 1:
        selected = simplified[0]
    else:
        for index, (uin, nickname) in enumerate(simplified, 1):
            print(f"{index}. {uin} {nickname}")
        choice = int(input("Choose account: ").strip())
        selected = simplified[choice - 1]
    uin = selected[0]
    progress(f"Requesting a temporary client ticket for QQ {uin}")

    ticket_params = {
        "clientuin": uin,
        "callback": "ptui_getst_CB",
        "r": str(random.random()),
        "pt_local_tk": str(local_token),
    }
    ticket_payload = parse_jsonp(_local_request(f"{LOCAL_TICKET_URL}?{urllib.parse.urlencode(ticket_params)}", local_token))
    client_key = ""
    if isinstance(ticket_payload, dict):
        client_key = str(ticket_payload.get("clientkey") or ticket_payload.get("key") or ticket_payload.get("data", {}).get("clientkey", ""))
    if not client_key:
        raise RuntimeError(f"QQNT did not issue clientkey: {ticket_payload}")

    session = QQSession()
    jump_params = {
        "clientuin": uin,
        "clientkey": client_key,
        "u1": LOGIN_SUCCESS_URL,
        "keyindex": "9",
        "pt_aid": APP_ID,
        "daid": DAID,
        "pt_local_tk": str(local_token),
        "pt_3rd_aid": "0",
        "ptopt": "1",
        "style": "40",
    }
    session.request_text(
        f"{LOCAL_JUMP_URL}?{urllib.parse.urlencode(jump_params)}",
        {
            "Cookie": f"pt_local_token={local_token}",
            "Referer": "https://xui.ptlogin2.qq.com/",
        },
    )
    return session, uin


def probe_group_albums(session: QQSession, uin: str, group_id: str) -> dict[str, Any]:
    skey = session.cookie_value("p_skey") or session.cookie_value("skey")
    if not skey:
        raise RuntimeError("Session has neither p_skey nor skey")
    params = {
        "g_tk": str(gtk(skey)),
        "callback": "shine0_Callback",
        "t": str(random.random()),
        "uin": uin,
        "qunId": group_id,
        "start": "0",
        "num": "20",
        "inCharset": "utf-8",
        "outCharset": "utf-8",
        "source": "qzone",
        "platform": "qzone",
        "format": "jsonp",
        "appid": "4",
    }
    text = session.request_text(
        f"{GROUP_ALBUM_URL}?{urllib.parse.urlencode(params)}",
        {"Referer": f"https://h5.qzone.qq.com/groupphoto/index?groupId={group_id}"},
    )
    return parse_jsonp(text)


def cookie_summary(bundle: SessionBundle) -> str:
    important = {"uin", "skey", "p_skey", "pt4_token", "ptcz", "RK"}
    present = sorted(important.intersection(bundle.cookie_names))
    if len(bundle.uin) > 4:
        masked_uin = f"{bundle.uin[:2]}{'*' * max(4, len(bundle.uin) - 4)}{bundle.uin[-2:]}"
    else:
        masked_uin = "****"
    return f"uin={masked_uin}; cookies={', '.join(present) or '(none of the expected names)'}"


def main() -> int:
    parser = argparse.ArgumentParser(description="QQ group album authentication proof of concept")
    parser.add_argument("method", choices=["qr", "local-qq", "probe", "inspect"])
    parser.add_argument("--uin", default="", help="Choose a QQ account for local-qq")
    parser.add_argument("--group", default="", help="Group number used to validate album access")
    parser.add_argument("--session", default=str(DEFAULT_SESSION_PATH))
    parser.add_argument("--timeout", type=int, default=180)
    args = parser.parse_args()
    session_path = Path(args.session)

    if args.method == "qr":
        session, uin = qr_login(timeout_seconds=args.timeout)
        bundle = session.bundle("qr", uin)
        save_session(bundle, session_path)
        print(f"Login succeeded; {cookie_summary(bundle)}")
        print(f"Encrypted session saved to {session_path}")
    elif args.method == "local-qq":
        session, uin = local_qq_login(args.uin)
        bundle = session.bundle("local-qq", uin)
        save_session(bundle, session_path)
        print(f"Login succeeded; {cookie_summary(bundle)}")
        print(f"Encrypted session saved to {session_path}")
    elif args.method == "inspect":
        bundle = load_session(session_path)
        print(f"Session: {cookie_summary(bundle)}")
        print(f"Method: {bundle.auth_method}; created: {bundle.created_at}")
    else:
        if not args.group:
            parser.error("probe requires --group")
        bundle = load_session(session_path)
        session = QQSession.from_bundle(bundle)
        result = probe_group_albums(session, bundle.uin, args.group)
        data = result.get("data", {}) if isinstance(result, dict) else {}
        albums = data.get("album") or data.get("albums") or data.get("albumList") or []
        print(json.dumps({
            "ok": bool(data),
            "code": result.get("code") if isinstance(result, dict) else None,
            "message": result.get("message") if isinstance(result, dict) else "",
            "album_count_in_probe": len(albums) if isinstance(albums, list) else None,
            "response_keys": sorted(result.keys()) if isinstance(result, dict) else [],
        }, ensure_ascii=False, indent=2))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
