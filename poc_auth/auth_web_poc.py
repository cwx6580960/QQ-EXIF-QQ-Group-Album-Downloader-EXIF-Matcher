from __future__ import annotations

import argparse
import base64
import json
import threading
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer

from auth_poc import (
    DEFAULT_SESSION_PATH,
    QQSession,
    cookie_summary,
    load_session,
    local_qq_login,
    probe_group_albums,
    qr_login,
    save_session,
)


PAGE = r'''<!doctype html>
<html lang="zh-CN">
<head>
<meta charset="utf-8">
<meta name="viewport" content="width=device-width,initial-scale=1">
<title>QQ 群相册登录 POC</title>
<style>
:root { color-scheme: light; --line:#d9dde3; --text:#202124; --muted:#67707b; --bg:#f4f6f8; --panel:#fff; --accent:#166534; --warn:#9a6700; --danger:#b42318; }
* { box-sizing:border-box; }
body { margin:0; background:var(--bg); color:var(--text); font:14px "Microsoft YaHei UI","Segoe UI",sans-serif; letter-spacing:0; }
header { background:#fff; border-bottom:1px solid var(--line); }
.bar { max-width:1040px; margin:auto; min-height:64px; display:flex; align-items:center; justify-content:space-between; padding:0 22px; }
h1 { margin:0; font-size:20px; font-weight:650; }
.privacy { color:var(--muted); font-size:13px; }
main { max-width:1040px; margin:auto; padding:24px 22px 40px; display:grid; grid-template-columns:minmax(340px,1fr) minmax(300px,0.8fr); gap:34px; }
section { min-width:0; }
h2 { margin:0 0 12px; font-size:15px; }
.mode { display:flex; gap:8px; margin-bottom:18px; }
button { min-height:36px; border:1px solid #b8bec6; border-radius:5px; background:#fff; color:var(--text); padding:7px 13px; cursor:pointer; font:inherit; }
button:hover { background:#f3f4f6; }
button:disabled { color:#9aa0a6; background:#f5f6f7; cursor:default; }
button.primary { background:#1f2937; border-color:#1f2937; color:#fff; }
button.primary:hover { background:#111827; }
.qr-frame { width:min(100%,360px); aspect-ratio:1; border:1px solid var(--line); background:#fff; display:grid; place-items:center; margin-bottom:18px; }
.qr-frame img { width:88%; height:88%; object-fit:contain; image-rendering:pixelated; }
.placeholder { color:var(--muted); text-align:center; padding:30px; line-height:1.8; }
.status { border-top:1px solid var(--line); border-bottom:1px solid var(--line); padding:14px 0; }
.status strong { display:block; font-size:16px; margin-bottom:5px; }
.status span { color:var(--muted); overflow-wrap:anywhere; }
.probe { margin-top:24px; }
.input-row { display:grid; grid-template-columns:1fr auto; gap:8px; }
input { min-width:0; min-height:36px; border:1px solid #b8bec6; border-radius:5px; padding:7px 10px; font:inherit; }
.result { margin-top:12px; min-height:44px; padding:10px 12px; background:#eef2f5; border-left:3px solid #667085; overflow-wrap:anywhere; }
.facts { margin:0; border-top:1px solid var(--line); }
.facts div { display:grid; grid-template-columns:120px 1fr; gap:12px; padding:12px 0; border-bottom:1px solid var(--line); }
.facts dt { color:var(--muted); }
.facts dd { margin:0; overflow-wrap:anywhere; }
.ok { color:var(--accent); } .waiting { color:var(--warn); } .error { color:var(--danger); }
@media (max-width:760px) { .privacy{display:none} main{grid-template-columns:1fr;padding:18px 16px}.qr-frame{margin-inline:auto}.mode{flex-wrap:wrap}.bar{padding:0 16px} }
</style>
</head>
<body>
<header><div class="bar"><h1>QQ 群相册登录 POC</h1><div class="privacy">不读取密码，不上传 Cookie</div></div></header>
<main>
  <section>
    <h2>建立腾讯会话</h2>
    <div class="mode">
      <button id="qrBtn" class="primary">二维码登录</button>
      <button id="qqBtn">QQNT 快速登录</button>
    </div>
    <div class="qr-frame" id="qrFrame"><div class="placeholder">点击“二维码登录”<br>二维码过期后会自动刷新</div></div>
    <div class="status"><strong id="status">等待操作</strong><span id="detail">会话仅保存在本机内存或 Windows 加密存储中</span></div>
    <div class="probe">
      <h2>群相册验票</h2>
      <div class="input-row"><input id="group" inputmode="numeric" placeholder="输入你有权访问的 QQ 群号"><button id="probeBtn">验证群相册</button></div>
      <div class="result" id="probeResult">登录成功后，用真实群号验证相册接口。</div>
    </div>
  </section>
  <section>
    <h2>POC 状态</h2>
    <dl class="facts">
      <div><dt>登录方式</dt><dd id="method">尚未登录</dd></div>
      <div><dt>会话状态</dt><dd id="session">未建立</dd></div>
      <div><dt>Cookie 摘要</dt><dd id="cookies">不会显示 Cookie 值</dd></div>
      <div><dt>保存方式</dt><dd id="storage">优先 DPAPI；失败则仅驻留内存</dd></div>
      <div><dt>判定标准</dt><dd>必须能访问目标群的 <code>qun_list_album_v2</code>，仅登录成功不算完成。</dd></div>
    </dl>
  </section>
</main>
<script>
const $ = (id) => document.getElementById(id);
let lastQr = "";
async function post(path, body={}) {
  const response = await fetch(path,{method:"POST",headers:{"Content-Type":"application/json"},body:JSON.stringify(body)});
  return response.json();
}
async function refresh() {
  try {
    const s = await fetch("/api/status",{cache:"no-store"}).then(r=>r.json());
    $("status").textContent=s.status; $("detail").textContent=s.detail;
    $("method").textContent=s.method||"尚未登录"; $("session").textContent=s.authenticated?"已建立":"未建立";
    $("cookies").textContent=s.cookie_summary||"不会显示 Cookie 值"; $("storage").textContent=s.storage||"尚未保存";
    $("qrBtn").disabled=s.running; $("qqBtn").disabled=s.running;
    if(s.qr && s.qr!==lastQr){ lastQr=s.qr; $("qrFrame").innerHTML=`<img src="${s.qr}" alt="QQ 登录二维码">`; }
    $("status").className=s.level||"";
  } catch(e) { $("status").textContent="本地服务连接中断"; $("status").className="error"; }
}
$("qrBtn").onclick=async()=>{await post("/api/login/qr");refresh()};
$("qqBtn").onclick=async()=>{await post("/api/login/local");refresh()};
$("probeBtn").onclick=async()=>{
  const group=$("group").value.trim();
  $("probeResult").textContent="正在验证…";
  const r=await post("/api/probe",{group});
  $("probeResult").textContent=r.message;
};
setInterval(refresh,800); refresh();
</script>
</body></html>'''


class State:
    def __init__(self):
        self.lock = threading.RLock()
        self.running = False
        self.status = "等待操作"
        self.detail = "选择二维码登录或 QQNT 快速登录"
        self.level = ""
        self.qr = ""
        self.authenticated = False
        self.method = ""
        self.cookie_summary = ""
        self.storage = "尚未保存"
        self.session: QQSession | None = None
        self.uin = ""

    def snapshot(self):
        with self.lock:
            return {
                "running": self.running,
                "status": self.status,
                "detail": self.detail,
                "level": self.level,
                "qr": self.qr,
                "authenticated": self.authenticated,
                "method": self.method,
                "cookie_summary": self.cookie_summary,
                "storage": self.storage,
            }

    def update(self, **values):
        with self.lock:
            for key, value in values.items():
                setattr(self, key, value)


STATE = State()


def translated_progress(message: str):
    translations = {
        "Waiting for scan": ("等待扫码", "请使用手机 QQ 扫描二维码", "waiting"),
        "Scanned; waiting for confirmation": ("已扫码，等待手机确认", "请在手机 QQ 上点击确认登录", "waiting"),
        "QR expired": ("二维码已过期", "正在自动刷新", "waiting"),
    }
    status, detail, level = translations.get(message, (message, "", ""))
    STATE.update(status=status, detail=detail, level=level)


def qr_worker():
    try:
        while True:
            try:
                def show_qr(data: bytes):
                    encoded = base64.b64encode(data).decode("ascii")
                    STATE.update(qr=f"data:image/png;base64,{encoded}")

                session, uin = qr_login(translated_progress, timeout_seconds=180, qr_callback=show_qr)
                bundle = session.bundle("qr", uin)
                storage = "仅驻留当前进程内存"
                try:
                    save_session(bundle, DEFAULT_SESSION_PATH)
                    storage = "Windows DPAPI 加密文件"
                except Exception:
                    pass
                STATE.update(
                    authenticated=True, method="腾讯二维码登录", session=session, uin=uin,
                    cookie_summary=cookie_summary(bundle), storage=storage,
                    status="二维码登录成功", detail="请输入群号继续验票", level="ok",
                )
                return
            except TimeoutError:
                STATE.update(status="二维码已过期", detail="正在自动刷新", level="waiting")
                continue
    except Exception as exc:
        STATE.update(status="二维码登录失败", detail=str(exc), level="error")
    finally:
        STATE.update(running=False)


def local_worker():
    try:
        session, uin = local_qq_login(progress=translated_progress)
        bundle = session.bundle("local-qq", uin)
        storage = "仅驻留当前进程内存"
        try:
            save_session(bundle, DEFAULT_SESSION_PATH)
            storage = "Windows DPAPI 加密文件"
        except Exception:
            pass
        STATE.update(
            authenticated=True, method="QQNT 本地快速登录", session=session, uin=uin,
            cookie_summary=cookie_summary(bundle), storage=storage,
            status="QQNT 快速登录成功", detail="请输入群号继续验票", level="ok",
        )
    except Exception as exc:
        STATE.update(status="QQNT 快速登录失败", detail=str(exc), level="error")
    finally:
        STATE.update(running=False)


class Handler(BaseHTTPRequestHandler):
    def send_json(self, payload, status=200):
        data = json.dumps(payload, ensure_ascii=False).encode("utf-8")
        self.send_response(status)
        self.send_header("Content-Type", "application/json; charset=utf-8")
        self.send_header("Content-Length", str(len(data)))
        self.send_header("Cache-Control", "no-store")
        self.end_headers()
        self.wfile.write(data)

    def read_json(self):
        size = int(self.headers.get("Content-Length", "0"))
        return json.loads(self.rfile.read(size) or b"{}")

    def do_GET(self):  # noqa: N802
        if self.path == "/" or self.path.startswith("/?"):
            data = PAGE.encode("utf-8")
            self.send_response(200)
            self.send_header("Content-Type", "text/html; charset=utf-8")
            self.send_header("Content-Length", str(len(data)))
            self.send_header("Cache-Control", "no-store")
            self.end_headers()
            self.wfile.write(data)
        elif self.path == "/api/status":
            self.send_json(STATE.snapshot())
        else:
            self.send_error(404)

    def do_POST(self):  # noqa: N802
        if self.path == "/api/login/qr":
            with STATE.lock:
                if STATE.running:
                    self.send_json({"ok": False, "message": "已有登录任务正在运行"}, 409)
                    return
                STATE.running = True
                STATE.status = "正在请求二维码"
                STATE.detail = "二维码过期后会自动刷新"
                STATE.level = "waiting"
            threading.Thread(target=qr_worker, daemon=True).start()
            self.send_json({"ok": True})
        elif self.path == "/api/login/local":
            with STATE.lock:
                if STATE.running:
                    self.send_json({"ok": False, "message": "已有登录任务正在运行"}, 409)
                    return
                STATE.running = True
                STATE.status = "正在连接 QQNT"
                STATE.detail = "请确保 QQNT 已启动并登录"
                STATE.level = "waiting"
            threading.Thread(target=local_worker, daemon=True).start()
            self.send_json({"ok": True})
        elif self.path == "/api/probe":
            body = self.read_json()
            group = str(body.get("group", "")).strip()
            if not group.isdigit():
                self.send_json({"ok": False, "message": "QQ群号应当只包含数字。"}, 400)
                return
            with STATE.lock:
                session = STATE.session
                uin = STATE.uin
            if session is None and DEFAULT_SESSION_PATH.exists():
                try:
                    bundle = load_session(DEFAULT_SESSION_PATH)
                    session = QQSession.from_bundle(bundle)
                    uin = bundle.uin
                except Exception:
                    session = None
            if session is None:
                self.send_json({"ok": False, "message": "请先完成登录。"}, 401)
                return
            try:
                result = probe_group_albums(session, uin, group)
                data = result.get("data", {}) if isinstance(result, dict) else {}
                albums = data.get("album") or data.get("albums") or data.get("albumList") or []
                code = result.get("code") if isinstance(result, dict) else None
                if data:
                    message = f"验票成功：接口 code={code}，响应包含 {len(albums) if isinstance(albums, list) else '未知'} 个相册。"
                    self.send_json({"ok": True, "message": message})
                else:
                    detail = result.get("message", "没有返回相册数据") if isinstance(result, dict) else "响应格式无效"
                    self.send_json({"ok": False, "message": f"验票未通过：code={code}，{detail}"})
            except Exception as exc:
                self.send_json({"ok": False, "message": f"验票请求失败：{exc}"}, 500)
        else:
            self.send_error(404)

    def log_message(self, *_args):
        return


def main():
    parser = argparse.ArgumentParser()
    parser.add_argument("--port", type=int, default=8766)
    args = parser.parse_args()
    server = ThreadingHTTPServer(("127.0.0.1", args.port), Handler)
    print(f"QQ auth POC: http://127.0.0.1:{args.port}/", flush=True)
    try:
        server.serve_forever()
    except KeyboardInterrupt:
        pass
    finally:
        server.server_close()


if __name__ == "__main__":
    main()
