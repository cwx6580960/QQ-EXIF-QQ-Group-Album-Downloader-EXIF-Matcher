from __future__ import annotations

import io
import threading
import tkinter as tk
from pathlib import Path
from tkinter import messagebox, ttk

from PIL import Image, ImageTk

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


class LoginPocApp:
    def __init__(self, root: tk.Tk):
        self.root = root
        self.root.title("QQ 群相册登录 POC")
        self.root.geometry("560x660")
        self.root.minsize(520, 620)
        self.running = False
        self.photo_image = None
        self.session_path = DEFAULT_SESSION_PATH
        self.live_session = None
        self.live_uin = ""

        self.status_var = tk.StringVar(value="选择一种登录方式")
        self.detail_var = tk.StringVar(value="会话只在本机加密保存")
        self.group_var = tk.StringVar()

        style = ttk.Style()
        try:
            style.theme_use("vista")
        except Exception:
            pass
        style.configure("TButton", padding=(11, 7), font=("Microsoft YaHei UI", 9))
        style.configure("TLabel", font=("Microsoft YaHei UI", 9))

        frame = ttk.Frame(root, padding=20)
        frame.pack(fill="both", expand=True)

        ttk.Label(frame, text="QQ 群相册登录 POC", font=("Microsoft YaHei UI", 18, "bold")).pack(anchor="w")
        ttk.Label(frame, text="验证登录会话能否真正访问群相册接口", foreground="#5f6368").pack(anchor="w", pady=(4, 18))

        actions = ttk.Frame(frame)
        actions.pack(fill="x")
        self.qr_button = ttk.Button(actions, text="二维码登录", command=self.start_qr)
        self.qr_button.pack(side="left", padx=(0, 8))
        self.qq_button = ttk.Button(actions, text="QQNT 快速登录", command=self.start_local_qq)
        self.qq_button.pack(side="left")

        self.qr_area = tk.Frame(frame, width=340, height=340, bg="#f4f6f8", highlightthickness=1, highlightbackground="#c8ccd0")
        self.qr_area.pack(pady=20)
        self.qr_area.pack_propagate(False)
        self.qr_label = tk.Label(
            self.qr_area,
            text="二维码会显示在这里",
            bg="#f4f6f8",
            fg="#5f6368",
            font=("Microsoft YaHei UI", 11),
        )
        self.qr_label.pack(fill="both", expand=True)

        ttk.Label(frame, textvariable=self.status_var, font=("Microsoft YaHei UI", 11, "bold")).pack()
        ttk.Label(frame, textvariable=self.detail_var, foreground="#5f6368", wraplength=500, justify="center").pack(pady=(5, 18))

        probe = ttk.Frame(frame)
        probe.pack(fill="x", pady=(5, 0))
        ttk.Label(probe, text="QQ群号").pack(side="left")
        ttk.Entry(probe, textvariable=self.group_var).pack(side="left", fill="x", expand=True, padx=8)
        self.probe_button = ttk.Button(probe, text="验证群相册", command=self.start_probe)
        self.probe_button.pack(side="right")

        ttk.Separator(frame).pack(fill="x", pady=16)
        ttk.Label(frame, text=f"加密会话：{self.session_path}", foreground="#6b7280", wraplength=500).pack(anchor="w")

    def set_running(self, running: bool):
        self.running = running
        state = "disabled" if running else "normal"
        self.qr_button.configure(state=state)
        self.qq_button.configure(state=state)
        self.probe_button.configure(state=state)

    def set_status(self, status: str, detail: str = ""):
        self.root.after(0, lambda: (self.status_var.set(status), self.detail_var.set(detail or self.detail_var.get())))

    def show_qr(self, data: bytes):
        def update():
            image = Image.open(io.BytesIO(data)).convert("RGB").resize((300, 300), Image.Resampling.NEAREST)
            self.photo_image = ImageTk.PhotoImage(image)
            self.qr_label.configure(image=self.photo_image, text="", bg="white")
        self.root.after(0, update)

    def progress(self, message: str):
        translations = {
            "Waiting for scan": ("等待扫码", "请使用手机 QQ 扫描二维码"),
            "Scanned; waiting for confirmation": ("已扫码，等待确认", "请在手机 QQ 上点击确认登录"),
            "QR expired": ("二维码已过期", "正在自动刷新二维码"),
        }
        status, detail = translations.get(message, (message, ""))
        self.set_status(status, detail)

    def start_qr(self):
        if self.running:
            return
        self.set_running(True)
        self.set_status("正在请求二维码", "腾讯登录页面不会把密码交给本程序")

        def worker():
            try:
                while True:
                    try:
                        session, uin = qr_login(self.progress, timeout_seconds=180, qr_callback=self.show_qr)
                        bundle = session.bundle("qr", uin)
                        self.live_session = session
                        self.live_uin = uin
                        try:
                            save_session(bundle, self.session_path)
                            storage = "会话已用 DPAPI 加密保存"
                        except Exception:
                            storage = "会话仅保存在当前程序内存中"
                        self.set_status("二维码登录成功", f"{cookie_summary(bundle)}；{storage}")
                        break
                    except TimeoutError:
                        self.set_status("二维码已过期", "正在自动刷新二维码")
                        continue
            except Exception as exc:
                self.set_status("二维码登录失败", str(exc))
            finally:
                self.root.after(0, lambda: self.set_running(False))

        threading.Thread(target=worker, daemon=True).start()

    def start_local_qq(self):
        if self.running:
            return
        self.set_running(True)
        self.set_status("正在连接 QQNT", "请确保 QQNT 已启动并登录")

        def worker():
            try:
                session, uin = local_qq_login(progress=self.progress)
                bundle = session.bundle("local-qq", uin)
                self.live_session = session
                self.live_uin = uin
                try:
                    save_session(bundle, self.session_path)
                    storage = "会话已用 DPAPI 加密保存"
                except Exception:
                    storage = "会话仅保存在当前程序内存中"
                self.set_status("QQNT 快速登录成功", f"{cookie_summary(bundle)}；{storage}")
            except Exception as exc:
                self.set_status("QQNT 快速登录失败", str(exc))
            finally:
                self.root.after(0, lambda: self.set_running(False))

        threading.Thread(target=worker, daemon=True).start()

    def start_probe(self):
        group = self.group_var.get().strip()
        if not group.isdigit():
            messagebox.showwarning("请输入群号", "QQ群号应当只包含数字。")
            return
        if self.live_session is None and not Path(self.session_path).exists():
            messagebox.showwarning("尚未登录", "请先完成二维码登录或 QQNT 快速登录。")
            return
        self.set_running(True)
        self.set_status("正在验证群相册", "只读取相册数量和接口状态")

        def worker():
            try:
                if self.live_session is not None:
                    session = self.live_session
                    uin = self.live_uin
                else:
                    bundle = load_session(self.session_path)
                    session = QQSession.from_bundle(bundle)
                    uin = bundle.uin
                result = probe_group_albums(session, uin, group)
                data = result.get("data", {}) if isinstance(result, dict) else {}
                albums = data.get("album") or data.get("albums") or data.get("albumList") or []
                code = result.get("code") if isinstance(result, dict) else None
                if data:
                    self.set_status("群相册验票成功", f"接口 code={code}，本次响应包含 {len(albums) if isinstance(albums, list) else '未知'} 个相册")
                else:
                    message = result.get("message", "No album data") if isinstance(result, dict) else "Invalid response"
                    self.set_status("群相册验票未通过", f"code={code}，{message}")
            except Exception as exc:
                self.set_status("群相册验票失败", str(exc))
            finally:
                self.root.after(0, lambda: self.set_running(False))

        threading.Thread(target=worker, daemon=True).start()


def main():
    root = tk.Tk()
    LoginPocApp(root)
    root.mainloop()


if __name__ == "__main__":
    main()
