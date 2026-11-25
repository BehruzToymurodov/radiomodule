"""HM30 Link - send and receive data over the SIYI HM30 datalink (UART side).

    python hm30_link_app.py           # real hardware
    python hm30_link_app.py --demo    # fake STM32 data, no hardware needed
    python hm30_link_app.py --pair    # two windows joined by a simulated radio (try PC-to-PC)

The other side can be an STM32 (with firmware/link.c) or another PC running this app.
HM30 ground unit: Datalink Mode = Type-C, Baud = the same value as the other side.
Only dependency besides the standard library: pyserial (pip install pyserial).
"""

from __future__ import annotations

import argparse
import csv
import json
import math
import os
import queue
import subprocess
import sys
import threading
import time
import tkinter as tk
from collections import deque
from datetime import datetime
from tkinter import filedialog, messagebox, ttk

import link_core as core
import protocol as proto

try:
    import serial
    import serial.tools.list_ports
except ImportError:  # the app still starts in --demo / --pair mode
    serial = None

HM30_VID, HM30_PID = 0x0483, 0x5740  # HM30 ground unit USB serial (as seen on Windows)
DEMO_PORT = "DEMO (fake STM32 data)"
PAIR_PORT = "PAIR (simulated radio to the other window)"
BAUDS = ["9600", "38400", "57600", "115200", "230400"]
PLOT_COLORS = ["#2b7de9", "#e5484d", "#30a46c", "#f5a524", "#8e4ec6", "#12a594", "#d6409f", "#6e6e6e"]
MAX_LOG_LINES = 3000
MAX_POINTS = 5000
MAX_CSV_ROWS = 500_000
BIG_FILE_ASK = 60  # ask before sending files that would take longer than this (s)

SEND_MODES = ["Text", "Command", "Number", "Bytes (hex)", "Files", "Plain text line"]
SEND_HINTS = {
    "Text": "any message  → shown in the other side's Chat",
    "Command": "LED ON  → STM32 gets a CMD packet (link_receive, type LINK_TYPE_CMD)",
    "Number": "speed=1.5, angle=-30  → one VALUE packet per name (Values table on the other side)",
    "Bytes (hex)": "01 02 FF  or  0x01,0x02,0xFF  or  0102FF",
    "Files": "click Browse… (pick one or more files), then Send. Text-like files are compressed automatically.",
    "Plain text line": "hello  → plain text + newline, no packet (like typing in PuTTY)",
}

SETTINGS_PATH = os.path.join(os.environ.get("APPDATA") or os.path.expanduser("~"), "HM30Link", "settings.json")


def load_settings() -> dict:
    try:
        with open(SETTINGS_PATH, encoding="utf-8") as f:
            return json.load(f)
    except (OSError, ValueError):
        return {}


def save_settings(settings: dict) -> None:
    try:
        os.makedirs(os.path.dirname(SETTINGS_PATH), exist_ok=True)
        with open(SETTINGS_PATH, "w", encoding="utf-8") as f:
            json.dump(settings, f, indent=2)
    except OSError:
        pass


def human_size(n: float) -> str:
    for unit in ("B", "KB", "MB", "GB"):
        if n < 1024 or unit == "GB":
            return f"{n:.0f} {unit}" if unit == "B" else f"{n:.1f} {unit}"
        n /= 1024
    return f"{n:.1f} GB"


def human_time(s: float) -> str:
    if not math.isfinite(s):
        return "…"
    s = int(round(s))
    return f"{s} s" if s < 60 else f"{s // 60} min {s % 60:02d} s"


def open_folder(path: str) -> None:
    if sys.platform.startswith("win"):
        os.startfile(path)  # type: ignore[attr-defined]
    elif sys.platform == "darwin":
        subprocess.Popen(["open", path])
    else:
        subprocess.Popen(["xdg-open", path])


class Series:
    def __init__(self, color: str) -> None:
        self.points: deque[tuple[float, float]] = deque(maxlen=MAX_POINTS)
        self.color = color
        self.plot = True
        self.count = 0
        self.vmin = math.inf
        self.vmax = -math.inf


class App:
    def __init__(self, root, *, demo: bool = False, pair_port=None, title: str = "HM30 Link",
                 save_dir: str | None = None, persist: bool = True) -> None:
        self.root = root
        self.pair_port = pair_port
        self.persist = persist  # False in --pair / tests: do not touch the user's settings file
        self.settings = load_settings() if persist else {}
        if save_dir:
            self.settings["save_dir"] = save_dir
        self.events: queue.Queue = queue.Queue()
        self.worker: core.LinkWorker | None = None
        self.series: dict[str, Series] = {}
        self.csv_rows: list[tuple] = []
        self.rx_bytes = 0
        self.last_rx = 0.0
        self.rate_hist: deque[tuple[float, int]] = deque()
        self.paused = False
        self.history: list[str] = []
        self.history_pos = 0
        self.file_paths: list[str] = []
        self.file_cancel: threading.Event | None = None
        self.incoming: dict[int, tuple[proto.IncomingFile, float, dict]] = {}
        self.chat_marks: dict[int, dict] = {}  # token -> {"mark", "left", "failed"}
        self.session_file = None
        self.session_csv = None

        root.title(title)
        root.geometry("1240x800")
        root.minsize(980, 600)
        self._build_ui()
        self.refresh_ports(select=DEMO_PORT if demo else PAIR_PORT if pair_port else None)
        root.protocol("WM_DELETE_WINDOW", self.on_close)
        root.after(50, self.poll_events)
        root.after(500, self.update_status)
        root.after(100, self.redraw_plot)
        if demo or pair_port:
            self.connect()
        elif not self._save_dir_ok():
            root.after(400, lambda: self.choose_save_dir(first_time=True))

    # ---- layout -------------------------------------------------------------

    def _build_ui(self) -> None:
        style = ttk.Style(self.root)
        if "vista" in style.theme_names():
            style.theme_use("vista")

        top = ttk.Frame(self.root, padding=(8, 8, 8, 2))
        top.pack(fill="x")
        ttk.Label(top, text="Port").pack(side="left")
        self.port_var = tk.StringVar()
        self.port_box = ttk.Combobox(top, textvariable=self.port_var, width=46, state="readonly")
        self.port_box.pack(side="left", padx=(4, 4))
        ttk.Button(top, text="⟳", width=3, command=self.refresh_ports).pack(side="left")
        ttk.Label(top, text="Baud").pack(side="left", padx=(12, 0))
        self.baud_var = tk.StringVar(value=str(self.settings.get("baud", "115200")))
        baud_box = ttk.Combobox(top, textvariable=self.baud_var, values=BAUDS, width=8, state="readonly")
        baud_box.pack(side="left", padx=4)
        baud_box.bind("<<ComboboxSelected>>", lambda _e: self._on_baud_change())
        self.connect_btn = ttk.Button(top, text="Connect", command=self.toggle_connect)
        self.connect_btn.pack(side="left", padx=(12, 0))
        self.link_lbl = tk.Label(top, text="●  disconnected", fg="#888", font=("Segoe UI", 10, "bold"))
        self.link_lbl.pack(side="left", padx=16)
        ttk.Button(top, text="Save CSV…", command=self.save_csv).pack(side="right")
        ttk.Button(top, text="Clear", command=self.clear_all).pack(side="right", padx=4)
        self.pause_btn = ttk.Button(top, text="Pause", command=self.toggle_pause)
        self.pause_btn.pack(side="right")

        folder = ttk.Frame(self.root, padding=(8, 0, 8, 2))
        folder.pack(fill="x")
        ttk.Label(folder, text="Received files and logs go to:").pack(side="left")
        self.folder_lbl = ttk.Label(folder, text="", foreground="#2b5fb4")
        self.folder_lbl.pack(side="left", padx=6)
        ttk.Button(folder, text="Change…", command=self.choose_save_dir).pack(side="left")
        ttk.Button(folder, text="📂 Open", command=self.open_save_dir).pack(side="left", padx=4)
        self._show_save_dir()

        main = ttk.PanedWindow(self.root, orient="horizontal")
        main.pack(fill="both", expand=True, padx=8, pady=4)

        # left: Chat / Log tabs
        left = ttk.Frame(main)
        main.add(left, weight=3)
        self.tabs = ttk.Notebook(left)
        self.tabs.pack(fill="both", expand=True)
        self.chat = self._text_tab("Chat", wrap="word", font=("Segoe UI", 10))
        self.log = self._text_tab("Log", wrap="none", font=("Consolas", 10))
        for widget in (self.chat, self.log):
            widget.tag_configure("time", foreground="#777")
        self.chat.tag_configure("me", foreground="#6fd18f")
        self.chat.tag_configure("them", foreground="#e6e6e6")
        self.chat.tag_configure("who_me", foreground="#3fae6a", font=("Segoe UI", 10, "bold"))
        self.chat.tag_configure("who_them", foreground="#7fb8ff", font=("Segoe UI", 10, "bold"))
        self.chat.tag_configure("file", foreground="#f5c26b")
        self.chat.tag_configure("ok", foreground="#3fae6a")
        self.chat.tag_configure("bad", foreground="#ff6b6b")
        self.chat.tag_configure("info", foreground="#c9a0ff")
        for tag, color in (("TEXT", "#e6e6e6"), ("RAW", "#b5b5b5"), ("VALUE", "#7fb8ff"), ("BYTES", "#f5c26b"),
                           ("SENT", "#6fd18f"), ("ACKED", "#3fae6a"), ("INFO", "#c9a0ff"), ("ERROR", "#ff6b6b")):
            self.log.tag_configure(tag, foreground=color)
        opts = ttk.Frame(left)
        opts.pack(fill="x", pady=(4, 0))
        self.show_values_var = tk.BooleanVar(value=False)
        ttk.Checkbutton(opts, text="Show value packets in Log", variable=self.show_values_var).pack(side="left")
        self.autoscroll_var = tk.BooleanVar(value=True)
        ttk.Checkbutton(opts, text="Auto-scroll", variable=self.autoscroll_var).pack(side="left", padx=12)

        # right: values table + graph
        right = ttk.PanedWindow(main, orient="vertical")
        main.add(right, weight=4)
        table_frame = ttk.Frame(right)
        right.add(table_frame, weight=2)
        ttk.Label(table_frame, text="Values  (click a row to show/hide it on the graph)").pack(anchor="w")
        cols = ("plot", "value", "min", "max", "count", "age")
        self.table = ttk.Treeview(table_frame, columns=cols, height=7)
        self.table.heading("#0", text="Name")
        self.table.column("#0", width=140)
        for c, w, title in (("plot", 50, "Graph"), ("value", 110, "Value"), ("min", 90, "Min"),
                            ("max", 90, "Max"), ("count", 70, "Count"), ("age", 70, "Age (s)")):
            self.table.heading(c, text=title)
            self.table.column(c, width=w, anchor="e" if c != "plot" else "center")
        self.table.pack(fill="both", expand=True)
        self.table.bind("<ButtonRelease-1>", self.on_table_click)

        plot_frame = ttk.Frame(right)
        right.add(plot_frame, weight=3)
        bar = ttk.Frame(plot_frame)
        bar.pack(fill="x")
        ttk.Label(bar, text="Graph window").pack(side="left")
        self.window_var = tk.StringVar(value="30 s")
        ttk.Combobox(bar, textvariable=self.window_var, values=["10 s", "30 s", "60 s", "5 min"], width=7,
                     state="readonly").pack(side="left", padx=4)
        self.canvas = tk.Canvas(plot_frame, bg="#111", highlightthickness=0)
        self.canvas.pack(fill="both", expand=True, pady=(4, 0))

        # bottom: send box
        send = ttk.Frame(self.root, padding=(8, 4, 8, 2))
        send.pack(fill="x")
        ttk.Label(send, text="Send").pack(side="left")
        self.send_mode = tk.StringVar(value=SEND_MODES[0])
        mode_box = ttk.Combobox(send, textvariable=self.send_mode, values=SEND_MODES, width=15, state="readonly")
        mode_box.pack(side="left", padx=(6, 0))
        mode_box.bind("<<ComboboxSelected>>", lambda _e: self._on_mode_change())
        self.send_var = tk.StringVar()
        self.entry = ttk.Entry(send, textvariable=self.send_var)
        self.entry.pack(side="left", fill="x", expand=True, padx=6)
        self.entry.bind("<Return>", lambda _e: self.send())
        self.entry.bind("<Up>", lambda _e: self._history_step(-1))
        self.entry.bind("<Down>", lambda _e: self._history_step(1))
        self.entry.bind("<Key>", lambda _e: self.file_paths.clear() if self.send_mode.get() == "Files" else None)
        self.browse_btn = ttk.Button(send, text="Browse…", command=self.browse_files)
        self.browse_btn.pack(side="left")
        self.send_btn = ttk.Button(send, text="Send", command=self.send)
        self.send_btn.pack(side="left", padx=(6, 0))

        prog = ttk.Frame(self.root, padding=(8, 0, 8, 0))
        prog.pack(fill="x")
        self.hint = ttk.Label(prog, foreground="#666", anchor="w")
        self.hint.pack(side="left", fill="x", expand=True)
        self.progress_lbl = ttk.Label(prog, text="", anchor="e")
        self.progress_lbl.pack(side="right", padx=(8, 0))
        self.progress = ttk.Progressbar(prog, length=260, mode="determinate", maximum=1000)
        self.progress.pack(side="right")
        self._on_mode_change()

        self.status = ttk.Label(self.root, padding=(8, 2, 8, 6), anchor="w")
        self.status.pack(fill="x")

    def _text_tab(self, title: str, **kw) -> tk.Text:
        frame = ttk.Frame(self.tabs)
        self.tabs.add(frame, text=title)
        text = tk.Text(frame, state="disabled", bg="#111", fg="#ddd", insertbackground="#ddd",
                       padx=6, pady=4, **kw)
        sb = ttk.Scrollbar(frame, command=text.yview)
        text.configure(yscrollcommand=sb.set)
        sb.pack(side="right", fill="y")
        text.pack(side="left", fill="both", expand=True)
        return text

    # ---- save folder / settings ---------------------------------------------

    def save_dir(self) -> str:
        return self.settings.get("save_dir") or os.path.join(os.path.expanduser("~"), "Documents", "HM30 Received")

    def _save_dir_ok(self) -> bool:
        d = self.settings.get("save_dir")
        return bool(d) and os.path.isdir(d)

    def _show_save_dir(self) -> None:
        self.folder_lbl.configure(text=self.save_dir() + ("" if self._save_dir_ok() else "   (not chosen yet)"))

    def choose_save_dir(self, first_time: bool = False) -> None:
        if first_time:
            messagebox.showinfo("HM30 Link", "Choose the folder where received files and logs will be saved.\n\n"
                                             "You can change it later with the \"Change…\" button.")
        d = filedialog.askdirectory(title="Folder for received files and logs", initialdir=self.save_dir(),
                                    mustexist=False)
        if not d:
            if first_time:
                d = self.save_dir()
            else:
                return
        try:
            os.makedirs(d, exist_ok=True)
        except OSError as exc:
            messagebox.showerror("HM30 Link", f"Cannot use this folder:\n{exc}")
            return
        self.settings["save_dir"] = d = os.path.normpath(d)
        self._persist()
        self._show_save_dir()
        self.log_line("INFO", f"Received files and logs will be saved to {d}")
        if self.worker:  # start a new session log in the new place
            self._open_session_log()

    def open_save_dir(self) -> None:
        d = self.save_dir()
        os.makedirs(d, exist_ok=True)
        open_folder(d)

    def _persist(self) -> None:
        if self.persist:
            save_settings(self.settings)

    def _on_baud_change(self) -> None:
        self.settings["baud"] = self.baud_var.get()
        self._persist()
        if self.worker:
            self.log_line("INFO", "Baud changed: disconnect and connect again to use it. "
                                  "The HM30 menu and the other side must use the same baud.")

    def _ensure_dir(self, sub: str = "") -> str | None:
        d = os.path.join(self.save_dir(), sub) if sub else self.save_dir()
        try:
            os.makedirs(d, exist_ok=True)
            return d
        except OSError as exc:
            self.log_line("ERROR", f"Cannot write to {d}: {exc}. Click \"Change…\" to pick another folder.")
            return None

    def _open_session_log(self) -> None:
        self._close_session_log()
        d = self._ensure_dir("logs")
        if not d:
            return
        path = os.path.join(d, f"session_{datetime.now():%Y-%m-%d_%H-%M-%S}.csv")
        try:
            self.session_file = open(path, "w", newline="", encoding="utf-8")
            self.session_csv = csv.writer(self.session_file)
            self.session_csv.writerow(["time", "direction", "kind", "name", "value"])
            self.log_line("INFO", f"Logging everything to {path}")
        except OSError as exc:
            self.log_line("ERROR", f"Cannot create the log file: {exc}")
            self.session_file = self.session_csv = None

    def _close_session_log(self) -> None:
        if self.session_file:
            try:
                self.session_file.close()
            except OSError:
                pass
        self.session_file = self.session_csv = None

    # ---- connection ---------------------------------------------------------

    def refresh_ports(self, select: str | None = None) -> None:
        items, best = [], None
        if serial is not None:
            for p in sorted(serial.tools.list_ports.comports(), key=lambda p: p.device):
                is_hm30 = p.vid == HM30_VID and p.pid == HM30_PID
                label = f"{p.device} — {'HM30 ground unit' if is_hm30 else (p.description or 'serial port')}"
                items.append(label)
                if is_hm30 and best is None:
                    best = label
        if self.pair_port is not None:
            items.append(PAIR_PORT)
        items.append(DEMO_PORT)
        self.port_box["values"] = items
        if select:
            self.port_var.set(select)
        elif self.port_var.get() not in items:
            self.port_var.set(best or items[0])

    def toggle_connect(self) -> None:
        if self.worker:
            self.disconnect()
        else:
            self.connect()

    def connect(self) -> None:
        label = self.port_var.get()
        try:
            if label == DEMO_PORT:
                port = core.DemoSerial()
            elif label == PAIR_PORT and self.pair_port is not None:
                port = self.pair_port
                port.is_open = True
            else:
                if serial is None:
                    messagebox.showerror("HM30 Link", "pyserial is not installed.\n\npip install pyserial")
                    return
                device = label.split(" — ")[0]
                port = serial.Serial(device, int(self.baud_var.get()), timeout=0.1, write_timeout=2)
        except Exception as exc:
            messagebox.showerror("HM30 Link", f"Could not open {label}:\n\n{exc}\n\n"
                                 "Close PuTTY or any other program using the port.")
            return
        self.worker = core.LinkWorker(port, self.events)
        self.worker.start()
        self.connect_btn.configure(text="Disconnect")
        self.log_line("INFO", f"Connected to {label} @ {self.baud_var.get()} baud")
        self._open_session_log()

    def disconnect(self) -> None:
        if self.file_cancel:
            self.file_cancel.set()
        if self.worker:
            self.worker.stop()
            self.worker = None
        self.connect_btn.configure(text="Connect")
        self.incoming.clear()
        self._close_session_log()

    def on_close(self) -> None:
        self.disconnect()
        self.root.after(150, self.root.destroy)

    # ---- receiving ----------------------------------------------------------

    def poll_events(self) -> None:
        try:
            for _ in range(800):
                ev = self.events.get_nowait()
                kind = ev[0]
                if kind == "rx":
                    _, ts, nbytes, items = ev
                    self.rx_bytes += nbytes
                    self.last_rx = time.monotonic()
                    for item in items:
                        self.handle_item(ts, item)
                elif kind == "error":
                    self.log_line("ERROR", f"Connection lost: {ev[1]}")
                    self.chat_line("info", "Connection lost: " + ev[1])
                    self.disconnect()
                elif kind == "closed":
                    self.log_line("INFO", "Port closed")
                elif kind == "info":
                    self.log_line("INFO", ev[1])
                elif kind == "peer":
                    if ev[1]:
                        self.log_line("INFO", "The other side is the HM30 Link app: messages and files are confirmed")
                        self.chat_line("info", "Other app connected")
                elif kind in ("delivered", "failed"):
                    self.on_delivery(kind == "delivered", *ev[1:])
                elif kind == "file_begin":
                    self.on_file_begin(ev[1])
                elif kind == "progress":
                    self.show_progress(ev[1])
                elif kind == "file_sent":
                    self.on_file_sent(ev[1])
                elif kind == "files_done":
                    self.file_cancel = None
                    self.send_btn.configure(text="Send")
        except queue.Empty:
            pass
        self.root.after(50, self.poll_events)

    def handle_item(self, ts: float, item) -> None:
        stamp = datetime.fromtimestamp(ts).strftime("%H:%M:%S.%f")[:-3]
        peer_app = bool(self.worker and self.worker.peer_is_app)
        if isinstance(item, proto.RawLine):
            if item.text.strip():
                self.log_line("RAW", item.text, ts)
                self.chat_line("them", item.text, ts)
                self._record(stamp, "in", "raw", "", item.text)
            return
        t = item.ftype
        if t == proto.TYPE_VALUE:
            try:
                name, value = item.value()
            except ValueError:
                return
            s = self.series.get(name)
            if s is None:
                s = self.series[name] = Series(PLOT_COLORS[len(self.series) % len(PLOT_COLORS)])
            s.points.append((time.monotonic(), value))
            s.count += 1
            s.vmin = min(s.vmin, value)
            s.vmax = max(s.vmax, value)
            self._record(stamp, "in", "value", name, f"{value:.6g}")
            if self.show_values_var.get():
                self.log_line("VALUE", f"{name} = {value:.6g}", ts)
            if peer_app:  # an STM32 streams values all the time: keep those out of the chat
                self.chat_line("them", f"{name} = {value:.6g}", ts)
        elif t == proto.TYPE_BYTES:
            hexs = item.payload.hex(" ")
            self.log_line("BYTES", f"[{len(item.payload)} bytes] {hexs}", ts)
            self._record(stamp, "in", "bytes", "", hexs)
            if peer_app:
                self.chat_line("them", f"[{len(item.payload)} bytes] {hexs[:120]}", ts)
        elif t == proto.TYPE_FILE_START:
            self.on_file_start(item.payload)
        elif t == proto.TYPE_FILE_DATA:
            self.on_file_data(item.payload)
        elif t == proto.TYPE_FILE_END:
            self.on_file_end(item.payload, stamp)
        else:  # TEXT, CMD, unknown types
            text = item.text()
            prefix = "" if t == proto.TYPE_TEXT else f"[{item.type_name}] "
            self.log_line("TEXT", prefix + text, ts)
            self.chat_line("them", prefix + text, ts)
            self._record(stamp, "in", "text" if t == proto.TYPE_TEXT else item.type_name.lower(), "", text)

    def _record(self, stamp: str, direction: str, kind: str, name: str, value: str) -> None:
        if len(self.csv_rows) < MAX_CSV_ROWS:
            self.csv_rows.append((stamp, direction, kind, name, value))
        if self.session_csv:
            try:
                self.session_csv.writerow([stamp, direction, kind, name, value])
            except OSError:
                pass

    # ---- receiving files ----------------------------------------------------

    def on_file_start(self, payload: bytes) -> None:
        try:
            f = proto.IncomingFile.from_start(payload)
        except proto.FileError as exc:
            self.log_line("ERROR", f"Bad file header: {exc}")
            return
        info = {"name": f.name, "index": 1, "count": 1, "size": f.original_size, "sent_size": f.sent_size,
                "compressed": bool(f.flags & proto.FILE_ZLIB)}
        self.incoming[f.fid] = (f, time.monotonic(), info)
        extra = f", compressed to {human_size(f.sent_size)}" if info["compressed"] else ""
        self.log_line("INFO", f"Receiving file {f.name} ({human_size(f.original_size)}{extra})")
        self.show_progress(core.receive_progress(info, 0.0, time.monotonic()))

    def on_file_data(self, payload: bytes) -> None:
        entry = self.incoming.get(proto.file_data_id(payload))
        if not entry:
            return
        f, t0, info = entry
        try:
            f.add(payload)
        except proto.FileError as exc:
            self.log_line("ERROR", f"File {f.name}: {exc}")
            return
        now = time.monotonic()
        if now - info.get("_shown", 0) > 0.2:
            info["_shown"] = now
            self.show_progress(core.receive_progress(info, f.fraction, t0))

    def on_file_end(self, payload: bytes, stamp: str) -> None:
        entry = self.incoming.pop(payload[0] if payload else -1, None)
        if not entry:
            return
        f, t0, info = entry
        try:
            data = f.finish()
        except proto.FileError as exc:
            self.log_line("ERROR", f"File {f.name} NOT saved: {exc}")
            self.chat_line("bad", f"✗ File {f.name} arrived damaged and was not saved ({exc})")
            self.show_progress(None)
            return
        folder = self._ensure_dir()
        if not folder:
            return
        path = os.path.normpath(proto.unique_path(folder, f.name))
        try:
            with open(path, "wb") as out:
                out.write(data)
        except OSError as exc:
            self.log_line("ERROR", f"Cannot save {path}: {exc}")
            return
        secs = max(time.monotonic() - t0, 1e-6)
        self.show_progress(core.receive_progress(info, 1.0, t0), done_text=f"Received {f.name} ✓")
        self.log_line("INFO", f"File received ✓ {path} ({human_size(len(data))} in {secs:.1f} s)")
        self.chat_line("file", f"📁 File received: {os.path.basename(path)} ({human_size(len(data))}) → {path}")
        self._record(stamp, "in", "file", os.path.basename(path), path)

    # ---- chat / log ---------------------------------------------------------

    def chat_line(self, tag: str, text: str, ts: float | None = None, token: int | None = None,
                  parts: int = 1) -> None:
        if self.paused and tag in ("them", "me"):
            return
        stamp = datetime.fromtimestamp(ts or time.time()).strftime("%H:%M:%S")
        c = self.chat
        c.configure(state="normal")
        c.insert("end", stamp + "  ", "time")
        if tag == "me":
            c.insert("end", "You: ", "who_me")
        elif tag == "them":
            c.insert("end", "Them: ", "who_them")
        c.insert("end", text, tag)
        if token is not None:
            mark = f"tok{token}"
            c.mark_set(mark, "end-1c")
            c.mark_gravity(mark, "left")
            self.chat_marks[token] = {"mark": mark, "left": parts, "failed": 0}
        c.insert("end", "\n")
        lines = int(c.index("end-1c").split(".")[0])
        if lines > MAX_LOG_LINES:
            c.delete("1.0", f"{lines - MAX_LOG_LINES}.0")
        c.configure(state="disabled")
        if self.autoscroll_var.get():
            c.see("end")

    def _chat_mark(self, token: int, ok: bool) -> None:
        info = self.chat_marks.get(token)
        if not info:
            return
        info["left"] -= 1
        if not ok:
            info["failed"] += 1
        if info["left"] > 0:
            return
        self.chat_marks.pop(token, None)
        c = self.chat
        if info["mark"] not in c.mark_names():
            return
        c.configure(state="normal")
        if info["failed"]:
            c.insert(info["mark"], "  ✗ not delivered", "bad")
        else:
            c.insert(info["mark"], "  ✓", "ok")
        c.configure(state="disabled")

    def log_line(self, tag: str, text: str, ts: float | None = None) -> None:
        if self.paused and tag not in ("INFO", "ERROR"):
            return
        stamp = datetime.fromtimestamp(ts or time.time()).strftime("%H:%M:%S.%f")[:-3]
        self.log.configure(state="normal")
        self.log.insert("end", stamp + "  ", "time")
        self.log.insert("end", text + "\n", tag)
        lines = int(self.log.index("end-1c").split(".")[0])
        if lines > MAX_LOG_LINES:
            self.log.delete("1.0", f"{lines - MAX_LOG_LINES}.0")
        self.log.configure(state="disabled")
        if self.autoscroll_var.get():
            self.log.see("end")

    # ---- sending ------------------------------------------------------------

    def _on_mode_change(self) -> None:
        mode = self.send_mode.get()
        self.hint.configure(text="Example:  " + SEND_HINTS[mode])
        self.browse_btn.configure(state="normal" if mode == "Files" else "disabled")

    def _history_step(self, step: int) -> str:
        if self.history:
            self.history_pos = max(0, min(len(self.history), self.history_pos + step))
            self.send_var.set(self.history[self.history_pos] if self.history_pos < len(self.history) else "")
            self.entry.icursor("end")
        return "break"

    def browse_files(self) -> None:
        paths = filedialog.askopenfilenames(title="Files to send (you can pick several)")
        if paths:
            self.file_paths = list(paths)
            if len(paths) == 1:
                self.send_var.set(paths[0])
            else:
                names = ", ".join(os.path.basename(p) for p in paths[:4])
                self.send_var.set(f"{len(paths)} files: {names}{' …' if len(paths) > 4 else ''}")

    def _remember(self, text: str) -> None:
        if not self.history or self.history[-1] != text:
            self.history.append(text)
        self.history_pos = len(self.history)

    def send(self) -> None:
        if self.file_cancel is not None:  # files are being sent: the button means "Cancel"
            self.file_cancel.set()
            self.log_line("INFO", "Cancelling the file transfer…")
            return
        text = self.send_var.get().strip()
        mode = self.send_mode.get()
        if not text:
            return
        if not self.worker:
            self.log_line("ERROR", "Not connected")
            return
        w = self.worker
        stamp = datetime.now().strftime("%H:%M:%S.%f")[:-3]
        try:
            if mode == "Files":
                self.send_files(self.file_paths or [text])
                return
            if mode == "Plain text line":  # no packet, no confirmation (like typing in PuTTY)
                w.send((text + "\r\n").encode("utf-8"))
                self.log_line("SENT", f"→ [RAW] {text}")
                self.chat_line("me", text)
                self._record(stamp, "out", "raw", "", text)
            elif mode == "Number":
                values = proto.parse_values(text)
                token = w.new_token()
                shown = ", ".join(f"{n} = {v:g}" for n, v in values)
                self.chat_line("me", shown, token=token, parts=len(values))
                for name, v in values:
                    w.send_reliable(proto.TYPE_VALUE, proto.value_payload(name, v), f"[NUMBER] {name} = {v:g}",
                                    token=token)
                    self._record(stamp, "out", "value", name, f"{v:g}")
                self.log_line("SENT", f"→ [NUMBER] {shown}")
            else:
                if mode == "Bytes (hex)":
                    data, ftype = proto.parse_hex(text), proto.TYPE_BYTES
                    shown = f"[{len(data)} bytes] {data[:32].hex(' ')}{' …' if len(data) > 32 else ''}"
                    kind, value = "bytes", data.hex(" ")
                else:
                    data = text.encode("utf-8")
                    ftype = proto.TYPE_CMD if mode == "Command" else proto.TYPE_TEXT
                    shown = f"[CMD] {text}" if mode == "Command" else text
                    kind, value = ("cmd" if mode == "Command" else "text"), text
                parts = proto.chunk(data)
                token = w.new_token()
                self.chat_line("me", shown, token=token, parts=len(parts))
                for part in parts:
                    w.send_reliable(ftype, part, shown, token=token)
                self.log_line("SENT", f"→ {shown}")
                self._record(stamp, "out", kind, "", value)
        except ValueError as exc:
            self.log_line("ERROR", f"Not sent: {exc}")
            self.chat_line("bad", f"Not sent: {exc}")
            return
        except Exception as exc:
            self.log_line("ERROR", f"Send failed: {exc}")
            return
        self._remember(text)
        self.send_var.set("")

    def on_delivery(self, ok: bool, desc: str, tries: int, report: bool, token: int | None) -> None:
        if desc == "hello":
            if ok:
                self.log_line("INFO", "The other side answered: everything you send is confirmed (✓) "
                                      "and re-sent if lost")
            elif not (self.worker and self.worker.peer_is_app):
                self.log_line("ERROR", "No answer from the other side yet. STM32: is the new firmware flashed? "
                                       "PC: is the other app connected? Air unit powered and linked?")
            return
        if token is not None:
            self._chat_mark(token, ok)
        if not report:
            return
        if ok:
            self.log_line("ACKED", f"✓ delivered {desc}" + (f"  ({tries} tries)" if tries > 1 else ""))
        else:
            self.log_line("ERROR", f"✗ NOT delivered {desc}  (no answer after {tries} tries)")

    # ---- sending files ------------------------------------------------------

    def send_files(self, paths: list[str]) -> None:
        paths = [p for p in paths if p]
        missing = [p for p in paths if not os.path.isfile(p)]
        if missing or not paths:
            self.log_line("ERROR", f"File not found: {missing[0] if missing else '(none)'} — use Browse…")
            return
        total = sum(os.path.getsize(p) for p in paths)
        bps = int(self.baud_var.get()) / 10 * 0.7  # rough, before compression
        if total / bps > BIG_FILE_ASK and not messagebox.askyesno(
            "HM30 Link", f"{len(paths)} file(s), {human_size(total)}.\nThis can take about "
                         f"{human_time(total / bps)} at {self.baud_var.get()} baud (less if it compresses well)."
                         "\n\nSend?"):
            return
        for p in paths:
            self._remember(p)
        worker = self.worker
        cancel = self.file_cancel = threading.Event()
        self.send_btn.configure(text="Cancel")
        self.log_line("SENT", f"→ sending {len(paths)} file(s), {human_size(total)}")
        threading.Thread(target=core.send_files,
                         args=(worker, paths, self.events, cancel, lambda: worker is self.worker and not
                               worker.stopped), daemon=True).start()
        self.send_var.set("")
        self.file_paths = []

    def on_file_begin(self, info: dict) -> None:
        extra = (f", compressed {human_size(info['size'])} → {human_size(info['sent_size'])}"
                 if info["compressed"] else "")
        self.log_line("SENT", f"→ [FILE {info['index']}/{info['count']}] {info['name']} "
                              f"({human_size(info['size'])}{extra})")
        info["token"] = token = self.worker.new_token() if self.worker else None
        self._file_token = token
        self.chat_line("me", f"📎 {info['name']} ({human_size(info['size'])}{' · compressed' if info['compressed'] else ''})",
                       token=token)
        self.show_progress({**info, "dir": "send", "frac": 0.0, "speed": 0.0, "eta": math.inf})

    def on_file_sent(self, info: dict) -> None:
        token = getattr(self, "_file_token", None)
        if info.get("ok"):
            self.log_line("ACKED", f"✓ file delivered {info['name']}: {human_size(info['size'])} in "
                                   f"{info['secs']:.1f} s ({human_size(info['speed'])}/s)")
            if token is not None:
                self._chat_mark(token, True)
            self.show_progress({**info, "dir": "send", "frac": 1.0, "speed": info["speed"], "eta": 0},
                               done_text=f"Sent {info['name']} ✓")
            self._record(datetime.now().strftime("%H:%M:%S.%f")[:-3], "out", "file", info["name"],
                         info.get("path", ""))
        else:
            self.log_line("ERROR", f"✗ file {info['name']}: {info.get('error', 'failed')}")
            if token is not None:
                self._chat_mark(token, False)
            self.show_progress(None)

    def show_progress(self, p: dict | None, done_text: str | None = None) -> None:
        if p is None:
            self.progress["value"] = 0
            self.progress_lbl.configure(text="")
            return
        self.progress["value"] = int(1000 * p["frac"])
        if done_text:
            self.progress_lbl.configure(text=done_text)
            return
        arrow = "Sending" if p["dir"] == "send" else "Receiving"
        count = f" ({p['index']}/{p['count']})" if p.get("count", 1) > 1 else ""
        speed = f"{human_size(p['speed'])}/s" if p["speed"] > 0 else "…"
        left = human_time(p["eta"]) + " left" if math.isfinite(p["eta"]) else "…"
        self.progress_lbl.configure(text=f"{arrow} {p['name']}{count}  {p['frac'] * 100:.0f}% · {speed} · {left}")

    # ---- table / graph ------------------------------------------------------

    def on_table_click(self, event) -> None:
        name = self.table.identify_row(event.y)
        if name in self.series:
            s = self.series[name]
            s.plot = not s.plot

    def update_status(self) -> None:
        now = time.monotonic()
        for name, s in self.series.items():
            if not s.points:
                continue
            t, v = s.points[-1]
            vals = ("■ on" if s.plot else "off", f"{v:.4g}", f"{s.vmin:.4g}", f"{s.vmax:.4g}", s.count,
                    f"{now - t:.1f}")
            if self.table.exists(name):
                self.table.item(name, values=vals)
            else:
                self.table.insert("", "end", iid=name, text=name, values=vals, tags=(name,))
            self.table.tag_configure(name, foreground=s.color)

        w = self.worker
        st = w.parser.stats if w else proto.ParserStats()
        tx = w.tx if w else core.TxStats()
        self.rate_hist.append((now, st.frames))
        while self.rate_hist and now - self.rate_hist[0][0] > 3:
            self.rate_hist.popleft()
        rate = 0.0
        if len(self.rate_hist) > 1:
            (t0, f0), (t1, f1) = self.rate_hist[0], self.rate_hist[-1]
            rate = (f1 - f0) / max(t1 - t0, 1e-6)

        who = "app" if w and w.peer_is_app else "STM32" if w and w.hello_ok else ""
        if not w:
            self.link_lbl.configure(text="●  disconnected", fg="#888")
        elif self.last_rx and now - self.last_rx < 2:
            self.link_lbl.configure(text=f"●  receiving{' from ' + who if who else ''}", fg="#30a46c")
        else:
            self.link_lbl.configure(text="●  connected, no data", fg="#f5a524")

        total = st.frames + st.crc_errors + st.lost
        quality = f"{100 * st.frames / total:.1f}%" if total else "–"
        rtt = f"  delay {w.srtt * 1000:.0f} ms" if w and w.srtt else ""
        self.status.configure(
            text=f"IN  ok {st.frames}  damaged {st.crc_errors}  lost {st.lost}  quality {quality}  "
                 f"{rate:.1f} pkt/s      OUT  sent {tx.sent}  confirmed {tx.delivered}  re-sent {tx.retries}  "
                 f"failed {tx.failed}{rtt}"
        )
        if self.session_file:
            try:
                self.session_file.flush()
            except OSError:
                pass
        self.root.after(500, self.update_status)

    def redraw_plot(self) -> None:
        c = self.canvas
        c.delete("all")
        w, h = c.winfo_width(), c.winfo_height()
        if w > 50 and h > 50:
            self._draw_plot(c, w, h)
        self.root.after(100, self.redraw_plot)

    def _draw_plot(self, c: tk.Canvas, w: int, h: int) -> None:
        span = {"10 s": 10, "30 s": 30, "60 s": 60, "5 min": 300}.get(self.window_var.get(), 30)
        now = time.monotonic()
        t_start = now - span
        visible = {n: [(t, v) for t, v in s.points if t >= t_start] for n, s in self.series.items() if s.plot}
        visible = {n: p for n, p in visible.items() if p}
        left, right, top, bottom = 60, 12, 12, 26
        pw, ph = w - left - right, h - top - bottom
        c.create_rectangle(left, top, left + pw, top + ph, outline="#333")
        if not visible:
            c.create_text(w / 2, h / 2, text="No values yet — numbers sent by the other side appear here",
                          fill="#666", font=("Segoe UI", 10))
            return
        lo = min(v for p in visible.values() for _, v in p)
        hi = max(v for p in visible.values() for _, v in p)
        if hi - lo < 1e-9:
            lo, hi = lo - 1, hi + 1
        pad = (hi - lo) * 0.08
        lo, hi = lo - pad, hi + pad
        for i in range(5):  # y grid
            y = top + ph * i / 4
            val = hi - (hi - lo) * i / 4
            c.create_line(left, y, left + pw, y, fill="#262626")
            c.create_text(left - 6, y, text=f"{val:.4g}", anchor="e", fill="#888", font=("Segoe UI", 8))
        for i in range(6):  # x grid
            x = left + pw * i / 5
            c.create_line(x, top, x, top + ph, fill="#262626")
            c.create_text(x, top + ph + 12, text=f"-{span * (5 - i) / 5:.0f}s" if i < 5 else "now",
                          fill="#888", font=("Segoe UI", 8))
        lx = left + 8
        for name, pts in visible.items():
            s = self.series[name]
            step = max(1, len(pts) // pw)  # at most ~1 point per pixel
            coords = []
            for t, v in pts[::step]:
                coords += [left + pw * (t - t_start) / span, top + ph * (hi - v) / (hi - lo)]
            if len(coords) >= 4:
                c.create_line(*coords, fill=s.color, width=2)
            else:
                c.create_oval(coords[0] - 2, coords[1] - 2, coords[0] + 2, coords[1] + 2, fill=s.color, outline="")
            label = f"{name} {pts[-1][1]:.4g}"
            c.create_text(lx, top + 10, text=label, anchor="w", fill=s.color, font=("Segoe UI", 9, "bold"))
            lx += 14 + 8 * len(label)

    # ---- misc ---------------------------------------------------------------

    def toggle_pause(self) -> None:
        self.paused = not self.paused
        self.pause_btn.configure(text="Resume" if self.paused else "Pause")

    def clear_all(self) -> None:
        for widget in (self.log, self.chat):
            widget.configure(state="normal")
            widget.delete("1.0", "end")
            widget.configure(state="disabled")
        self.chat_marks.clear()
        self.series.clear()
        self.table.delete(*self.table.get_children())
        self.csv_rows.clear()
        self.rx_bytes = 0
        self.rate_hist.clear()
        if self.worker:  # start the IN / OUT counters from zero too
            self.worker.parser.stats = proto.ParserStats()
            self.worker.tx = core.TxStats()

    def save_csv(self) -> None:
        if not self.csv_rows:
            messagebox.showinfo("HM30 Link", "Nothing received or sent yet.")
            return
        path = filedialog.asksaveasfilename(defaultextension=".csv", initialdir=self.save_dir(),
                                            initialfile=f"hm30_{datetime.now():%Y%m%d_%H%M%S}.csv",
                                            filetypes=[("CSV", "*.csv")])
        if not path:
            return
        with open(path, "w", newline="", encoding="utf-8") as f:
            wr = csv.writer(f)
            wr.writerow(["time", "direction", "kind", "name", "value"])
            wr.writerows(self.csv_rows)
        self.log_line("INFO", f"Saved {len(self.csv_rows)} rows to {path}")


def main() -> None:
    ap = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    ap.add_argument("--demo", action="store_true", help="start with fake STM32 data (no hardware)")
    ap.add_argument("--pair", action="store_true", help="two windows joined by a simulated radio")
    ap.add_argument("--loss", type=float, default=0.05, help="--pair: packet loss per direction (0..1)")
    args = ap.parse_args()
    root = tk.Tk()
    if args.pair:
        a, b = core.make_pair(loss=args.loss)
        base = os.path.join(os.path.expanduser("~"), "Documents", "HM30 Received (pair test)")
        App(root, pair_port=a, title="HM30 Link — SENDER (pair test)", save_dir=os.path.join(base, "sender"),
            persist=False)
        second = tk.Toplevel(root)
        App(second, pair_port=b, title="HM30 Link — RECEIVER (pair test)", save_dir=os.path.join(base, "receiver"),
            persist=False)
        second.geometry("+80+60")
    else:
        App(root, demo=args.demo)
    root.mainloop()


if __name__ == "__main__":
    main()
