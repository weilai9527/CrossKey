"""Small desktop launcher. Tk stays on the main thread; control shares this process."""
from __future__ import annotations

import argparse
import queue
import re
import signal
import subprocess
import sys
import threading
from typing import Any, Callable

from auth import generate_pairing_code, validate_pairing_code
from configure_ip import validate_host, validate_port
from launcher_support import BoundedLog, LauncherInstance, local_ipv4_addresses
from settings import CONFIG_PATH, load_settings, save_settings
from version import __version__


EDGE_LABELS = {"top": "上边缘", "right": "右边缘", "bottom": "下边缘", "left": "左边缘"}
OPPOSITE = {"top": "下", "right": "左", "bottom": "上", "left": "右"}


class Session:
    """One core worker at a time, with a bounded, non-blocking UI mailbox."""

    def __init__(self) -> None:
        self.events: queue.Queue[dict[str, Any]] = queue.Queue(maxsize=64)
        self.stop_event = threading.Event()
        self.thread: threading.Thread | None = None
        self.generation = 0

    @property
    def running(self) -> bool:
        return self.thread is not None and self.thread.is_alive()

    def publish(self, event: dict[str, Any], generation: int) -> None:
        if generation != self.generation:
            return
        event = dict(event, generation=generation)
        try:
            self.events.put_nowait(event)
        except queue.Full:
            try:
                self.events.get_nowait()
            except queue.Empty:
                pass
            try:
                self.events.put_nowait(event)
            except queue.Full:
                pass

    def start(self, target: Callable[[threading.Event, Callable], None]) -> bool:
        if self.running:
            return False
        self.generation += 1
        generation = self.generation
        self.stop_event = threading.Event()
        while not self.events.empty():
            try:
                self.events.get_nowait()
            except queue.Empty:
                break

        def run() -> None:
            callback = lambda event: self.publish(event, generation)
            try:
                target(self.stop_event, callback)
            except (Exception, SystemExit) as exc:
                if not self.stop_event.is_set():
                    callback({"state": "error", "message": str(exc), "fatal": True})
            finally:
                callback({"state": "stopped"})

        self.thread = threading.Thread(target=run, name="CrossKey-control", daemon=True)
        self.thread.start()
        return True

    def stop(self) -> None:
        self.stop_event.set()


def legacy_mac_processes() -> list[int]:
    """Prevent the old CLI event tap and the new launcher from capturing together."""
    try:
        result = subprocess.run(["ps", "-axo", "pid=,command="], capture_output=True,
                                text=True, timeout=2, check=True)
    except (OSError, subprocess.SubprocessError):
        return []
    found = []
    for line in result.stdout.splitlines():
        pid, _, command = line.strip().partition(" ")
        if re.search(r"(?:^|\s|[/\\])mac_client\.py(?:\s|$)", command):
            if pid.isdigit():
                found.append(int(pid))
    return found


def config_from_form(config: dict[str, Any], values: dict[str, Any], role: str) -> dict[str, Any]:
    updated = dict(config)
    updated["port"] = validate_port(values["port"])
    updated["sync_clipboard"] = bool(values["clipboard"])
    updated["launcher_auto_start"] = bool(values["auto_start"])
    if role == "mac":
        host = str(values["host"]).strip()
        updated["auto_discover"] = bool(values["discover"])
        updated["windows_host"] = validate_host(host) if host else ""
        if not host and not updated["auto_discover"]:
            raise ValueError("请填写 Windows 地址，或开启自动发现。")
        updated["pairing_code"] = validate_pairing_code(str(values["code"]))
        mappings = {}
        for edge, value in values["edges"].items():
            if str(value).strip() not in ("", "关闭"):
                number = int(value)
                if number < 1 or number > 32:
                    raise ValueError("显示器编号需在 1–32 之间。")
                mappings[edge] = number
        if values.get("edges_changed", True):
            updated["edge_monitors"] = mappings
            updated["auto_switch_edge"] = "off"
    return updated


class Launcher:
    def __init__(self, root: Any, role: str, instance: LauncherInstance, log: BoundedLog) -> None:
        import tkinter as tk
        from tkinter import messagebox, ttk
        self.tk, self.ttk, self.messagebox = tk, ttk, messagebox
        self.root, self.role, self.instance, self.log = root, role, instance, log
        self.session = Session()
        self.hidden = self.quitting = False
        self.phase = "idle"
        self.was_running = False
        self.connected_target: tuple[str, int] | None = None
        self.message = ""
        self.last_error = ""
        self.monitors: list[dict[str, Any]] = []
        self.settings_window = self.details_window = None
        self.details_text = None
        self.log_revision = -1
        self.ips: list[str] = []
        self.ip_busy = False
        self.network_events: queue.Queue = queue.Queue(maxsize=1)
        self.config = load_settings()
        if role == "windows" and not self.config["pairing_code"]:
            self.config["pairing_code"] = generate_pairing_code()
            save_settings(self.config)
        self.colors = {"bg": "#F8FAFC", "panel": "#EEF2F7", "fg": "#202A37",
                       "muted": "#5D6B7D", "accent": "#2864CE", "line": "#D5DDE8"}
        self.font = "Segoe UI" if role == "windows" else "Helvetica Neue"
        self.root.title(f"CrossKey {__version__} · {'Mac' if role == 'mac' else 'Windows'}")
        self.root.configure(bg=self.colors["bg"])
        self.root.minsize(430, 560)
        self.root.resizable(False, False)
        self.root.protocol("WM_DELETE_WINDOW", self.close_window)
        self.root.option_add("*Font", (self.font, 11))
        self.root.report_callback_exception = self.callback_error
        style = ttk.Style(root)
        style.configure("TButton", padding=(10, 6))
        style.configure("TCheckbutton", background=self.colors["bg"])
        menu = tk.Menu(root)
        app_menu = tk.Menu(menu, tearoff=False)
        app_menu.add_command(label="显示窗口", command=self.show_window)
        app_menu.add_command(label="连接设置", command=self.open_settings)
        app_menu.add_separator()
        app_menu.add_command(label="退出 CrossKey", command=self.quit)
        menu.add_cascade(label="CrossKey", menu=app_menu)
        root.config(menu=menu)
        if sys.platform == "darwin":
            root.createcommand("tk::mac::ReopenApplication", self.show_window)
            root.createcommand("tk::mac::Quit", self.quit)
        self.build_view()
        self.refresh_network()
        self.root.after(200, self.tick)
        self.root.after(30000, self.network_tick)
        if self.config.get("launcher_auto_start", False):
            self.root.after(300, self.start_stop)

    def label(self, parent: Any, text: str = "", size: int = 11, bold: bool = False, **kw: Any) -> Any:
        return self.tk.Label(parent, text=text, bg=self.colors["bg"], fg=self.colors["fg"],
                             font=(self.font, size, "bold" if bold else "normal"), **kw)

    def build_view(self) -> None:
        tk, ttk = self.tk, self.ttk
        c = self.colors
        self.body = tk.Frame(self.root, bg=c["bg"], padx=22, pady=18)
        self.body.pack(fill="both", expand=True)
        header = tk.Frame(self.body, bg=c["bg"])
        header.pack(fill="x")
        self.label(header, "CrossKey", 17, True).pack(side="left")
        self.label(header, f"v{__version__}", 10).pack(side="left", padx=8)
        ttk.Button(header, text="设置", command=self.open_settings).pack(side="right")
        self.heading = self.label(self.body, size=18, bold=True, anchor="w")
        self.heading.pack(fill="x", pady=(20, 3))
        self.subtitle = self.label(self.body, size=10, anchor="w", justify="left", wraplength=410)
        self.subtitle.pack(fill="x", pady=(0, 14))
        device = tk.Frame(self.body, bg=c["panel"], padx=14, pady=12)
        device.pack(fill="x")
        self.device_title = tk.Label(device, bg=c["panel"], fg=c["fg"], anchor="w", font=(self.font, 12, "bold"))
        self.device_title.pack(fill="x")
        self.address = tk.Label(device, bg=c["panel"], fg=c["fg"], anchor="w", justify="left", font=(self.font, 12))
        self.address.pack(fill="x", pady=(5, 0))
        if self.role == "windows":
            ttk.Button(device, text="复制地址", command=self.copy_address).pack(anchor="e", pady=(7, 0))
        layout_header = tk.Frame(self.body, bg=c["bg"])
        layout_header.pack(fill="x", pady=(16, 3))
        self.label(layout_header, "屏幕布局" if self.role == "mac" else "Windows 显示器", 11, True).pack(side="left")
        ttk.Button(layout_header, text="调整布局" if self.role == "mac" else "刷新检测",
                   command=self.open_settings if self.role == "mac" else self.refresh_network).pack(side="right")
        self.canvas = tk.Canvas(self.body, width=410, height=158, bg=c["bg"], highlightthickness=0)
        self.canvas.pack(fill="x")
        self.route = self.label(self.body, size=10, justify="center", wraplength=410)
        self.route.pack(fill="x", pady=(2, 12))
        if self.role == "windows":
            pairing = tk.Frame(self.body, bg=c["panel"], padx=14, pady=10)
            pairing.pack(fill="x", pady=(0, 12))
            tk.Label(pairing, text="配对码  ·  在 Mac 上输入", bg=c["panel"], fg=c["muted"], font=(self.font, 10)).pack(side="left")
            tk.Label(pairing, text=self.config["pairing_code"], bg=c["panel"], fg=c["fg"], font=(self.font, 18, "bold")).pack(side="right")
        self.action = ttk.Button(self.body, text="", command=self.start_stop)
        self.action.pack(fill="x", pady=(0, 7))
        if self.role == "mac":
            self.label(self.body, "F8 / ⌃⌥⌘8 切换控制 · 从目标屏幕对应边缘返回", 10).pack(pady=(0, 8))
        bottom = tk.Frame(self.body, bg=c["bg"])
        bottom.pack(fill="x", pady=(6, 0))
        ttk.Button(bottom, text="连接详情", command=self.open_details).pack(side="left")
        ttk.Button(bottom, text="退出程序", command=self.quit).pack(side="right")
        self.label(self.body, "运行时关闭窗口后继续服务 · 再次双击可打开", 9).pack(pady=(12, 0))
        self.render()

    def render(self) -> None:
        if self.hidden:
            return
        mac = self.role == "mac"
        titles = {"idle": "准备连接" if mac else "准备接收", "connecting": "正在连接 Windows",
                  "starting": "正在启动", "connected": "已连接，可跨屏" if mac else "Mac 已连接",
                  "listening": "等待 Mac 连接", "controlling": "正在控制 Windows" if mac else "Mac 正在控制",
                  "local": "已返回 Mac" if mac else "Mac 已暂停控制", "disconnected": "连接已断开",
                  "stopping": "正在停止", "stopped": "已停止", "error": "需要处理"}
        self.heading.configure(text=titles.get(self.phase, "CrossKey"))
        descriptions = {"idle": "沿用保存的电脑和布局。" if mac else "开启后即可接受 Mac 的配对连接。",
                        "connected": "推动鼠标越过设置的边缘，或按 F8。" if mac else "可以通过 Mac 的鼠标和键盘操作。",
                        "listening": "在 Mac 打开 CrossKey，填写配对码并连接。",
                        "local": "仍保持连接，可再次穿屏进入。",
                        "stopped": "连接和键鼠监听已停止。",
                        "disconnected": "Mac 会自动尝试重新连接。" if mac else "等待 Mac 重新连接。",
                        "stopping": "正在释放连接和键鼠监听…"}
        detail = self.last_error or descriptions.get(self.phase, self.message)
        self.subtitle.configure(text=detail[:260])
        self.device_title.configure(text="Windows 电脑" if mac else "这台 Windows · 本机局域网 IP")
        if mac:
            host, port = self.connected_target or (self.config.get("windows_host") or "自动发现", self.config["port"])
            self.address.configure(text=f"{host}  ·  端口 {port}")
        else:
            self.address.configure(text=(" / ".join(self.ips) or "未检测到局域网 IPv4，请检查网络") + f"\n端口 {self.config['port']}", wraplength=370)
        busy = self.phase == "stopping" or self.quitting
        self.action.configure(state="disabled" if busy else "normal", text=("停止并返回 Mac" if mac else "停止接收") if self.session.running else ("连接 Windows" if mac else "开启接收"))
        self.draw_layout()

    def draw_layout(self) -> None:
        canvas, c = self.canvas, self.colors
        canvas.delete("all")
        def screen(x: float, y: float, w: float, h: float, title: str, caption: str, active: bool = False) -> None:
            canvas.create_rectangle(x, y, x+w, y+h, fill="#E6EEFC" if active else c["panel"], outline=c["accent"] if active else c["line"], width=1)
            canvas.create_text(x+w/2, y+h/2-9, text=title, fill=c["fg"], font=(self.font, 14, "bold"))
            canvas.create_text(x+w/2, y+h/2+14, text=caption, fill=c["muted"], font=(self.font, 9))
        if self.role == "windows":
            if not self.monitors:
                canvas.create_text(205, 70, text="正在检测显示器…" if self.ip_busy else "未读取到显示器，点击刷新检测", fill=c["muted"], font=(self.font, 11))
            else:
                monitors = self.monitors[:16]
                min_x = min(m["x"] for m in monitors)
                min_y = min(m["y"] for m in monitors)
                width = max(m["x"]+m["width"] for m in monitors)-min_x
                height = max(m["y"]+m["height"] for m in monitors)-min_y
                scale = min(390/max(width, 1), 140/max(height, 1))
                for m in monitors:
                    screen(10+(m["x"]-min_x)*scale, 6+(m["y"]-min_y)*scale,
                           m["width"]*scale-3, m["height"]*scale-3, str(m["number"]), "主显示器" if m.get("primary") else "显示器")
            self.route.configure(text="Mac 的穿屏方向在 Mac 端设置。")
            return
        mappings = self.config.get("edge_monitors", {})
        if not mappings and self.config.get("auto_switch_edge") != "off":
            edge = self.config["auto_switch_edge"]
            mappings = {edge: 0}
        if not mappings:
            screen(138, 44, 134, 65, "Mac", "使用 F8 切换", True)
            self.route.configure(text="尚未设置穿屏方向，点击「调整布局」。")
        elif len(mappings) == 1:
            edge, number = next(iter(mappings.items()))
            title = str(number) if number else "Windows"
            if edge in ("top", "bottom"):
                win_y, mac_y = (4, 94) if edge == "top" else (94, 4)
                x = 235 if number == 2 else 140
                screen(x, win_y, 140, 58, title, "Windows 显示器", True)
                screen(x, mac_y, 140, 58, "3 · Mac", "这台电脑")
                canvas.create_text(x+70, 78, text="↕", fill=c["accent"], font=(self.font, 17))
                if number == 2:
                    screen(40, win_y, 140, 58, "1", "Windows 笔记本")
            else:
                x1, x2 = (25, 245) if edge == "right" else (245, 25)
                screen(x1, 48, 140, 62, "3 · Mac", "这台电脑")
                screen(x2, 48, 140, 62, title, "Windows 显示器", True)
                canvas.create_text(205, 78, text="↔", fill=c["accent"], font=(self.font, 17))
            self.route.configure(text=f"Mac {EDGE_LABELS[edge]}进入屏幕 {title} · 从其{OPPOSITE[edge]}边缘返回\n布局为已保存设置，进入时由 Windows 确认。")
        else:
            screen(143, 49, 124, 62, "3 · Mac", "这台电脑")
            positions = {"top": (143, 0), "bottom": (143, 120), "left": (0, 49), "right": (285, 49)}
            for edge, number in mappings.items():
                x, y = positions[edge]
                canvas.create_text(x+62, y+18, text=f"{EDGE_LABELS[edge]} → 屏幕 {number}", fill=c["accent"], font=(self.font, 10))
            self.route.configure(text="从目标显示器的对应边缘返回 Mac。")

    def refresh_network(self) -> None:
        if self.ip_busy:
            return
        self.ip_busy = True
        def read() -> None:
            monitors = []
            try:
                ips = local_ipv4_addresses() if self.role == "windows" else []
                if self.role == "windows":
                    from windows_monitors import list_monitors
                    for m in list_monitors().values():
                        b = m.bounds
                        monitors.append(dict(number=m.number, x=b.left, y=b.top, width=b.right-b.left,
                                             height=b.bottom-b.top, primary=m.primary))
                self.network_events.put_nowait((ips, monitors, ""))
            except Exception as exc:
                self.network_events.put_nowait((locals().get("ips", []), monitors, str(exc)))
        threading.Thread(target=read, name="CrossKey-address", daemon=True).start()

    def network_tick(self) -> None:
        if not self.hidden and not self.quitting:
            self.refresh_network()
        if not self.quitting:
            self.root.after(30000, self.network_tick)

    def start_stop(self) -> None:
        if self.session.running:
            self.session.stop()
            self.phase, self.last_error = "stopping", ""
            self.render()
            return
        if self.role == "mac":
            old = legacy_mac_processes()
            if old:
                self.last_error = "旧命令行客户端仍在运行（PID " + ", ".join(map(str, old)) + "）。请先退出旧客户端，再点击连接。"
                self.phase = "error"
                self.render()
                return
            if not self.config.get("pairing_code"):
                self.open_settings()
                return
        self.last_error = ""
        self.phase = "connecting" if self.role == "mac" else "starting"
        config = dict(self.config)
        def run(stop_event: threading.Event, on_status: Callable) -> None:
            if self.role == "mac":
                from mac_client import main
                main([], stop_event=stop_event, on_status=on_status)
            else:
                from win_server import serve
                serve(config["host"], config["port"], config["sync_clipboard"],
                      config["clipboard_only_control_mode"], config["clipboard_poll_interval"],
                      config["clipboard_max_bytes"], config["discovery_port"], config["pairing_code"],
                      config["heartbeat_timeout"], stop_event=stop_event, on_status=on_status)
        self.session.start(run)
        self.render()

    def tick(self) -> None:
        running = self.session.running
        changed = running != self.was_running
        self.was_running = running
        if self.instance.poll_show():
            self.show_window()
        try:
            ips, monitors, error = self.network_events.get_nowait()
            self.ip_busy = False
            self.ips, self.monitors = ips, monitors
            if error:
                self.log.write("Display/network detection: " + error + "\n")
            changed = True
        except queue.Empty:
            pass
        for _ in range(64):
            try:
                event = self.session.events.get_nowait()
            except queue.Empty:
                break
            if event.get("generation") != self.session.generation:
                continue
            state = event.get("state", "")
            if state == "connected" and event.get("host") and event.get("port"):
                self.connected_target = (str(event["host"]), int(event["port"]))
            elif state in ("stopped", "disconnected"):
                self.connected_target = None
            if state == "error":
                self.last_error = str(event.get("message", "连接失败，请查看详情。"))
            elif state not in ("stopped", "stopping"):
                self.last_error = ""
            self.phase = "error" if state == "stopped" and self.last_error else state
            self.message = str(event.get("message", ""))
            if event.get("monitors"):
                self.monitors = event["monitors"]
            changed = True
        if self.quitting and not self.session.running:
            self.instance.close()
            self.root.destroy()
            return
        if changed:
            self.render()
        if self.details_text is not None and not self.hidden:
            revision = self.log.revision
            if revision != self.log_revision:
                self.log_revision = revision
                content = self.log.getvalue()
                self.details_text.configure(state="normal")
                self.details_text.delete("1.0", "end")
                self.details_text.insert("1.0", content)
                self.details_text.configure(state="disabled")
                self.details_text.see("end")
        self.root.after(1000 if self.hidden else 250, self.tick)

    def open_settings(self) -> None:
        if self.session.running:
            self.messagebox.showinfo("连接设置", "请先停止当前连接，再修改设置。", parent=self.root)
            return
        if self.settings_window is not None:
            self.settings_window.lift()
            return
        tk, ttk = self.tk, self.ttk
        window = self.settings_window = tk.Toplevel(self.root)
        window.title("CrossKey · 连接与布局")
        window.configure(bg=self.colors["bg"])
        window.resizable(False, False)
        frame = tk.Frame(window, bg=self.colors["bg"], padx=22, pady=18)
        frame.pack(fill="both", expand=True)
        fields: dict[str, Any] = {}
        def entry(key: str, label: str, value: Any, secret: bool = False) -> None:
            self.label(frame, label, 10).pack(anchor="w", pady=(8, 3))
            var = tk.StringVar(value=str(value))
            ttk.Entry(frame, textvariable=var, width=35, show="•" if secret else "").pack(fill="x")
            fields[key] = var
        if self.role == "mac":
            entry("host", "Windows 局域网 IP / 主机名", self.config["windows_host"])
            entry("code", "Windows 窗口中的六位配对码", self.config["pairing_code"], True)
        entry("port", "连接端口", self.config["port"])
        for key, label, value in (("clipboard", "同步剪贴板", self.config["sync_clipboard"]),
                                  ("auto_start", "打开窗口后自动启动", self.config.get("launcher_auto_start", False))):
            fields[key] = tk.BooleanVar(value=value)
            ttk.Checkbutton(frame, text=label, variable=fields[key]).pack(anchor="w", pady=(9, 0))
        edge_vars = {}
        if self.role == "mac":
            fields["discover"] = tk.BooleanVar(value=self.config["auto_discover"])
            ttk.Checkbutton(frame, text="优先自动发现 Windows", variable=fields["discover"]).pack(anchor="w", pady=(9, 0))
            self.label(frame, "Mac 边缘 → Windows 显示器编号", 11, True).pack(anchor="w", pady=(16, 5))
            mapping = self.config.get("edge_monitors", {})
            if not mapping and self.config.get("auto_switch_edge", "off") != "off":
                old_edge = self.config["auto_switch_edge"]
                self.label(frame, "当前使用旧穿屏方式：" + EDGE_LABELS[old_edge] + "。\n修改下方编号后才会切换为显示器映射。", 9, justify="left").pack(anchor="w", pady=(0, 5))
            for edge, label in EDGE_LABELS.items():
                row = tk.Frame(frame, bg=self.colors["bg"])
                row.pack(fill="x", pady=3)
                self.label(row, label, 10).pack(side="left")
                var = tk.StringVar(value=str(mapping.get(edge, "关闭")))
                ttk.Combobox(row, textvariable=var, width=12, values=("关闭", *map(str, range(1, 9)))).pack(side="right")
                edge_vars[edge] = var
            self.label(frame, "按 Windows 端 CrossKey 检测到的编号填写；关闭表示不从此边缘穿屏。", 9, wraplength=350, justify="left").pack(pady=(7, 0))
        error = self.label(frame, "", 10, wraplength=350, justify="left")
        error.pack(fill="x", pady=8)
        def close() -> None:
            window.destroy()
            self.settings_window = None
        def save() -> None:
            try:
                values = {key: var.get() for key, var in fields.items()}
                values["edges"] = {key: var.get() for key, var in edge_vars.items()}
                values["edges_changed"] = any(var.get() != str(self.config.get("edge_monitors", {}).get(edge, "关闭")) for edge, var in edge_vars.items())
                updated = config_from_form(self.config, values, self.role)
                save_settings(updated)
            except (OSError, ValueError) as exc:
                error.configure(text=str(exc))
                return
            self.config = updated
            close()
            self.render()
        ttk.Button(frame, text="保存设置", command=save).pack(fill="x")
        window.protocol("WM_DELETE_WINDOW", close)
        window.transient(self.root)

    def open_details(self) -> None:
        if self.details_window is not None:
            self.details_window.lift()
            return
        window = self.details_window = self.tk.Toplevel(self.root)
        window.title("CrossKey · 最近日志（最多 200 行）")
        self.details_text = self.tk.Text(window, width=75, height=20, wrap="word", font=("Courier", 10))
        self.details_text.pack(fill="both", expand=True)
        self.details_text.insert("1.0", self.log.getvalue())
        self.details_text.configure(state="disabled")
        def close() -> None:
            window.destroy()
            self.details_window = self.details_text = None
        window.protocol("WM_DELETE_WINDOW", close)

    def copy_address(self) -> None:
        def copy(ip: str) -> None:
            self.root.clipboard_clear()
            self.root.clipboard_append(ip)
        if len(self.ips) == 1:
            copy(self.ips[0])
        elif self.ips:
            menu = self.tk.Menu(self.root, tearoff=False)
            menu.add_command(label="选择与 Mac 在同一网络的地址", state="disabled")
            menu.add_separator()
            for ip in self.ips:
                menu.add_command(label=ip, command=lambda value=ip: copy(value))
            try:
                menu.tk_popup(self.root.winfo_pointerx(), self.root.winfo_pointery())
            finally:
                menu.grab_release()

    def close_window(self) -> None:
        if not self.session.running:
            self.quit()
            return
        for window in (self.settings_window, self.details_window):
            if window is not None:
                window.destroy()
        self.settings_window = self.details_window = self.details_text = None
        self.hidden = True
        self.body.destroy()
        self.root.withdraw()

    def show_window(self) -> None:
        if self.hidden:
            self.hidden = False
            self.build_view()
            self.refresh_network()
        self.root.deiconify()
        self.root.lift()
        self.root.focus_force()

    def quit(self) -> None:
        self.quitting = True
        self.session.stop()
        self.phase = "stopping"
        self.render()

    def callback_error(self, kind: type, value: BaseException, traceback: Any) -> None:
        import traceback as tb
        self.log.write("".join(tb.format_exception(kind, value, traceback)))
        self.messagebox.showerror("CrossKey", str(value), parent=self.root)


def main() -> None:
    parser = argparse.ArgumentParser(description="CrossKey lightweight desktop launcher")
    parser.add_argument("--version", action="version", version=f"CrossKey {__version__}")
    args = parser.parse_args()
    del args
    if sys.platform not in ("darwin", "win32"):
        raise SystemExit("CrossKey launcher supports macOS and Windows.")
    try:
        import tkinter as tk
        from tkinter import messagebox
    except ImportError as exc:
        if sys.platform == "win32":
            import ctypes
            ctypes.windll.user32.MessageBoxW(None, "此 Python 未安装 Tcl/Tk。请安装包含 Tcl/Tk 的 Python，或使用 win_server.py 命令行入口。", "CrossKey 启动失败", 0x10)
        raise SystemExit("This Python is missing Tk. Install Python with Tcl/Tk support, or use mac_client.py / win_server.py.") from exc
    role = "mac" if sys.platform == "darwin" else "windows"
    instance = LauncherInstance(role)
    # The incumbent needs no worker thread: its Tk event loop acknowledges this request.
    try:
        if not instance.acquire_or_notify():
            return
    except OSError as exc:
        notice = tk.Tk()
        notice.withdraw()
        messagebox.showerror("CrossKey 启动失败", str(exc), parent=notice)
        notice.destroy()
        return
    original_out, original_err = sys.stdout, sys.stderr
    log = BoundedLog()
    root = None
    app = None
    try:
        sys.stdout = sys.stderr = log
        root = tk.Tk()
        app = Launcher(root, role, instance, log)
        for signum in (signal.SIGINT, signal.SIGTERM):
            signal.signal(signum, lambda _signum, _frame: app.quit())
        root.mainloop()
    except Exception as exc:
        if root is not None:
            messagebox.showerror("CrossKey 启动失败", str(exc), parent=root)
        elif original_err is not None:
            original_err.write(str(exc) + "\n")
        elif sys.platform == "win32":
            import ctypes
            ctypes.windll.user32.MessageBoxW(None, str(exc), "CrossKey 启动失败", 0x10)
        raise
    finally:
        if app is not None:
            app.session.stop()
            if app.session.thread is not None:
                app.session.thread.join(timeout=6)
        sys.stdout, sys.stderr = original_out, original_err
        instance.close()


if __name__ == "__main__":
    main()
