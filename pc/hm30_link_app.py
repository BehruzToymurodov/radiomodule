"""HM30 Link - send and receive data over the SIYI HM30 datalink (UART side).

    python hm30_link_app.py           # real hardware
    python hm30_link_app.py --demo    # fake STM32 data, no hardware needed
    python hm30_link_app.py --pair    # two windows joined by a simulated radio (try PC-to-PC)

The other side can be an STM32 (with firmware/link.c) or another PC running this app.
HM30 ground unit: Datalink Mode = Type-C, Baud = the same value as the other side.
Needs pyserial (pip install pyserial). Optional: Pillow (pip install pillow) for photo previews.
"""

from __future__ import annotations

import argparse
import csv
import io
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

try:  # photo previews in the chat (optional)
    from PIL import Image, ImageTk
except ImportError:
    Image = ImageTk = None

try:  # sound for notifications (Windows)
    import winsound
except ImportError:
    winsound = None

HM30_VID, HM30_PID = 0x0483, 0x5740  # HM30 ground unit USB serial (as seen on Windows)
DEMO_PORT = "DEMO (fake STM32 data)"
PAIR_PORT = "PAIR (simulated radio to the other window)"
BAUDS = ["9600", "38400", "57600", "115200", "230400"]
MAX_LINES = 3000
MAX_LINE_CHARS = 300
MAX_POINTS = 5000
MAX_CSV_ROWS = 500_000
BIG_FILE_ASK = 60      # ask before sending files that would take longer than this (s)
EVENT_BUDGET_S = 0.03  # handle incoming data in short slices so the window never freezes
RECONNECT_EVERY_MS = 2000
RESUME_WAIT_S = 12     # after reconnecting, wait this long for the other side before re-sending files
IMAGE_EXT = {".jpg", ".jpeg", ".png", ".gif", ".bmp", ".webp"}
THUMB_SIZE = (260, 180)
MAX_THUMBS = 60
MAX_TRANSFERS = 500

SEND_MODES = ["Text", "Command", "Number", "Bytes (hex)", "Files", "Plain text line"]
SEND_HINTS = {
    "Text": "Type a message and press Enter. It appears in the other side's Chat.",
    "Command": "e.g. LED ON — for an STM32 (it arrives as LINK_TYPE_CMD in link_receive).",
    "Number": "e.g. speed=1.5, angle=-30 — shows up in the other side's Values table and graph.",
    "Bytes (hex)": "e.g. 01 02 FF  or  0x01,0x02,0xFF  or  0102FF",
    "Files": "Click Browse… (you can pick several files), then Send. Text files are compressed automatically.",
    "Plain text line": "Sent as plain text + newline, without confirmation (like typing in PuTTY).",
}

# ---- look --------------------------------------------------------------------------
C = {
    "bg": "#f3f5f9", "card": "#ffffff", "border": "#dde2ea", "text": "#1f2937", "muted": "#6b7280",
    "header": "#1e293b", "header_text": "#f8fafc", "accent": "#2563eb", "accent_dark": "#1d4ed8",
    "ok": "#16a34a", "warn": "#d97706", "err": "#dc2626", "info": "#475569", "in": "#2563eb", "out": "#0f766e",
    "me_bg": "#dcfce7", "me_fg": "#14532d", "them_bg": "#eef2f7", "them_fg": "#1f2937", "grid": "#eef1f5",
}
PLOT_COLORS = ["#2563eb", "#dc2626", "#16a34a", "#d97706", "#7c3aed", "#0891b2", "#db2777", "#4b5563"]
FONT = ("Segoe UI", 10)
FONT_BOLD = ("Segoe UI", 10, "bold")
LEVEL_ICON = {"info": "ℹ", "ok": "✔", "warn": "⚠", "err": "✖", "in": "⬇", "out": "⬆", "tech": "·"}

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


def resource(name: str) -> str:
    """A file next to this script, or inside the .exe (PyInstaller unpacks to sys._MEIPASS)."""
    base = getattr(sys, "_MEIPASS", os.path.dirname(os.path.abspath(__file__)))
    return os.path.join(base, name)


def set_icon(window) -> None:
    try:
        if sys.platform.startswith("win") and os.path.exists(resource("hm30_link.ico")):
            window.iconbitmap(resource("hm30_link.ico"))
        elif os.path.exists(resource("hm30_link_icon.png")):
            window.iconphoto(True, tk.PhotoImage(file=resource("hm30_link_icon.png")))
    except tk.TclError:
        pass


def open_file(path: str) -> None:
    if sys.platform.startswith("win"):
        os.startfile(path)  # type: ignore[attr-defined]
    else:
        open_folder(path)


def is_image(name: str) -> bool:
    return os.path.splitext(name)[1].lower() in IMAGE_EXT


def make_thumbnail(source) -> "ImageTk.PhotoImage | None":
    """source: a path or bytes. Returns a small Tk image, or None (no Pillow / not an image)."""
    if Image is None:
        return None
    try:
        img = Image.open(io.BytesIO(source) if isinstance(source, (bytes, bytearray)) else source)
        img.draft("RGB", (THUMB_SIZE[0] * 2, THUMB_SIZE[1] * 2))  # fast JPEG decode at reduced size
        img = img.convert("RGB")
        img.thumbnail(THUMB_SIZE)
        return ImageTk.PhotoImage(img)
    except Exception:
        return None


def flash_taskbar(window) -> None:
    """Make the taskbar button blink until the window is focused (Windows only)."""
    if not sys.platform.startswith("win"):
        return
    try:
        import ctypes
        from ctypes import wintypes

        class FLASHWINFO(ctypes.Structure):
            _fields_ = [("cbSize", wintypes.UINT), ("hwnd", wintypes.HWND), ("dwFlags", wintypes.DWORD),
                        ("uCount", wintypes.UINT), ("dwTimeout", wintypes.DWORD)]

        hwnd = ctypes.windll.user32.GetParent(window.winfo_id())
        info = FLASHWINFO(ctypes.sizeof(FLASHWINFO), hwnd, 0x3 | 0xC, 0, 0)  # FLASHW_ALL | FLASHW_TIMERNOFG
        ctypes.windll.user32.FlashWindowEx(ctypes.byref(info))
    except Exception:
        pass


def readable(text: str) -> bool:
    """True for normal text (e.g. printf from an STM32), False for radio noise."""
    if not text:
        return False
    good = sum(ch.isprintable() and ch != "�" for ch in text)
    return good / len(text) >= 0.9


def clip(text: str) -> str:
    text = "".join(ch if ch.isprintable() else "·" for ch in text)
    return text if len(text) <= MAX_LINE_CHARS else text[:MAX_LINE_CHARS] + " …"


def friendly_error(msg: str) -> str:
    low = msg.lower()
    if any(k in low for k in ("access is denied", "permissionerror", "clearcommerror", "device", "could not")):
        return "the USB adapter / ground unit was unplugged or stopped responding"
    return msg


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
        self.title = title
        self.pair_port = pair_port
        self.persist = persist  # False in --pair / tests: do not touch the user's settings file
        self.settings = load_settings() if persist else {}
        if save_dir:
            self.settings["save_dir"] = save_dir
        self.events: queue.Queue = queue.Queue()
        self.worker: core.LinkWorker | None = None
        self.series: dict[str, Series] = {}
        self.csv_rows: list[tuple] = []
        self.last_rx = 0.0
        self.rate_hist: deque[tuple[float, int]] = deque()
        self.paused = False
        self.history: list[str] = []
        self.history_pos = 0
        self.file_paths: list[str] = []
        self.file_cancel: threading.Event | None = None
        self._file_token: int | None = None
        self.incoming: dict[int, tuple[proto.IncomingFile, float, dict]] = {}
        self.chat_marks: dict[int, dict] = {}  # token -> {"mark", "left", "failed"}
        self.session_file = None
        self.session_csv = None
        self.noise_bytes = 0
        self._noise_reported = 0.0
        self._quality_mark: tuple[float, int, int, int] | None = None
        self._scroll: set = set()
        self.lost_connection = False
        # auto-reconnect
        self.port_label: str | None = None        # what we are connected to (real serial ports only)
        self.port_ids: tuple | None = None        # (vid, pid, serial number) to find the adapter again
        self.reconnecting = False
        self.reconnect_tries = 0
        self.files_left: list[str] = []           # files not yet confirmed (re-sent after a reconnect)
        self.files_user_cancelled = False
        # transfers, notifications, previews
        self.transfers: list[dict] = []
        self.unread = 0
        self._last_sound = 0.0
        self._thumbs: deque = deque(maxlen=MAX_THUMBS)  # keep Tk images alive

        root.title(title)
        set_icon(root)
        root.geometry("1280x820")
        root.minsize(1000, 640)
        root.configure(bg=C["bg"])
        self._style()
        self._build_ui()
        self.refresh_ports(select=DEMO_PORT if demo else PAIR_PORT if pair_port else None)
        root.protocol("WM_DELETE_WINDOW", self.on_close)
        root.bind("<FocusIn>", self._on_focus, add="+")
        self._load_transfers()
        root.after(50, self.poll_events)
        root.after(500, self.update_status)
        root.after(150, self.redraw_plot)
        self.note("info", "Welcome! Choose the port, set the same baud on both sides, then click Connect.")
        if demo or pair_port:
            self.connect()
        elif not self._save_dir_ok():
            root.after(400, lambda: self.choose_save_dir(first_time=True))

    # ---- look ---------------------------------------------------------------

    def _style(self) -> None:
        s = ttk.Style(self.root)
        s.theme_use("clam")
        s.configure(".", background=C["bg"], foreground=C["text"], font=FONT, bordercolor=C["border"],
                    lightcolor=C["border"], darkcolor=C["border"], focuscolor=C["accent"])
        s.configure("TFrame", background=C["bg"])
        s.configure("Card.TFrame", background=C["card"], relief="flat", borderwidth=0)
        s.configure("Header.TFrame", background=C["header"])
        s.configure("Header.TLabel", background=C["header"], foreground=C["header_text"], font=("Segoe UI", 14, "bold"))
        s.configure("Sub.TLabel", background=C["header"], foreground="#94a3b8", font=("Segoe UI", 9))
        s.configure("TLabel", background=C["bg"])
        s.configure("Card.TLabel", background=C["card"])
        s.configure("Muted.TLabel", background=C["bg"], foreground=C["muted"], font=("Segoe UI", 9))
        s.configure("CardMuted.TLabel", background=C["card"], foreground=C["muted"], font=("Segoe UI", 9))
        s.configure("Path.TLabel", background=C["card"], foreground=C["accent"])
        s.configure("Title.TLabel", background=C["card"], font=("Segoe UI", 10, "bold"))
        s.configure("TButton", padding=(10, 4), background="#e9edf3", relief="flat")
        s.map("TButton", background=[("active", "#dbe2ec"), ("disabled", "#f1f3f6")],
              foreground=[("disabled", "#9ca3af")])
        s.configure("Accent.TButton", background=C["accent"], foreground="white", font=FONT_BOLD, padding=(14, 5))
        s.map("Accent.TButton", background=[("active", C["accent_dark"]), ("disabled", "#93c5fd")])
        s.configure("Danger.TButton", background="#fee2e2", foreground=C["err"], font=FONT_BOLD, padding=(14, 5))
        s.map("Danger.TButton", background=[("active", "#fecaca")])
        s.configure("TCombobox", fieldbackground="white", background="white", arrowsize=14, padding=3)
        s.map("TCombobox", fieldbackground=[("readonly", "white")], selectbackground=[("readonly", "white")],
              selectforeground=[("readonly", C["text"])], background=[("readonly", "white")])
        s.configure("TEntry", fieldbackground="white", padding=5)
        s.configure("TCheckbutton", background=C["card"])
        s.configure("TNotebook", background=C["card"], borderwidth=0, tabmargins=(4, 4, 4, 0))
        s.configure("TNotebook.Tab", padding=(16, 6), font=FONT, background="#e9edf3")
        s.map("TNotebook.Tab", background=[("selected", C["card"])], font=[("selected", FONT_BOLD)])
        s.configure("Treeview", rowheight=26, fieldbackground="white", background="white", borderwidth=0)
        s.configure("Treeview.Heading", font=FONT_BOLD, background="#eef1f5", relief="flat")
        s.configure("Green.Horizontal.TProgressbar", troughcolor="#e5e7eb", background=C["ok"],
                    bordercolor="#e5e7eb", lightcolor=C["ok"], darkcolor=C["ok"], thickness=12)
        s.configure("TPanedwindow", background=C["bg"])

    def _card(self, parent, **pack) -> ttk.Frame:
        outer = tk.Frame(parent, bg=C["border"], bd=0)
        outer.pack(**pack)
        inner = ttk.Frame(outer, style="Card.TFrame", padding=10)
        inner.pack(fill="both", expand=True, padx=1, pady=1)
        return inner

    # ---- layout -------------------------------------------------------------

    def _build_ui(self) -> None:
        header = ttk.Frame(self.root, style="Header.TFrame", padding=(14, 10))
        header.pack(fill="x")
        ttk.Label(header, text="HM30 Link", style="Header.TLabel").pack(side="left")
        ttk.Label(header, text="  data over the SIYI HM30 radio", style="Sub.TLabel").pack(side="left", pady=(4, 0))
        self.badge = tk.Label(header, text="● Disconnected", font=FONT_BOLD, fg="white", bg="#64748b",
                              padx=12, pady=3)
        self.badge.pack(side="right")

        self.status = ttk.Label(self.root, padding=(14, 4, 14, 6), anchor="w", style="Muted.TLabel")
        self.status.pack(fill="x", side="bottom")

        body = ttk.Frame(self.root, padding=(10, 8, 10, 0))
        body.pack(fill="both", expand=True)

        # connection card
        conn = self._card(body, fill="x")
        row = ttk.Frame(conn, style="Card.TFrame")
        row.pack(fill="x")
        ttk.Label(row, text="Port", style="Card.TLabel").pack(side="left")
        self.port_var = tk.StringVar()
        self.port_box = ttk.Combobox(row, textvariable=self.port_var, width=48, state="readonly")
        self.port_box.pack(side="left", padx=(6, 4))
        ttk.Button(row, text="⟳", width=3, command=self.refresh_ports).pack(side="left")
        ttk.Label(row, text="Baud", style="Card.TLabel").pack(side="left", padx=(16, 0))
        self.baud_var = tk.StringVar(value=str(self.settings.get("baud", "115200")))
        baud_box = ttk.Combobox(row, textvariable=self.baud_var, values=BAUDS, width=8, state="readonly")
        baud_box.pack(side="left", padx=6)
        baud_box.bind("<<ComboboxSelected>>", lambda _e: self._on_baud_change())
        self.connect_btn = ttk.Button(row, text="Connect", style="Accent.TButton", command=self.toggle_connect)
        self.connect_btn.pack(side="left", padx=(10, 0))
        ttk.Button(row, text="Save CSV…", command=self.save_csv).pack(side="right")
        ttk.Button(row, text="Clear", command=self.clear_all).pack(side="right", padx=6)
        self.pause_btn = ttk.Button(row, text="Pause", command=self.toggle_pause)
        self.pause_btn.pack(side="right")
        row2 = ttk.Frame(conn, style="Card.TFrame")
        row2.pack(fill="x", pady=(8, 0))
        ttk.Label(row2, text="Received files and logs:", style="CardMuted.TLabel").pack(side="left")
        self.folder_lbl = ttk.Label(row2, text="", style="Path.TLabel")
        self.folder_lbl.pack(side="left", padx=6)
        ttk.Button(row2, text="Change…", command=self.choose_save_dir).pack(side="left")
        ttk.Button(row2, text="Open folder", command=self.open_save_dir).pack(side="left", padx=6)
        self._show_save_dir()

        # send card
        send_card = self._card(body, fill="x", side="bottom", pady=(0, 2))
        send = ttk.Frame(send_card, style="Card.TFrame")
        send.pack(fill="x")
        ttk.Label(send, text="Send", style="Title.TLabel").pack(side="left")
        self.send_mode = tk.StringVar(value=SEND_MODES[0])
        mode_box = ttk.Combobox(send, textvariable=self.send_mode, values=SEND_MODES, width=15, state="readonly")
        mode_box.pack(side="left", padx=(8, 0))
        mode_box.bind("<<ComboboxSelected>>", lambda _e: self._on_mode_change())
        self.send_var = tk.StringVar()
        self.entry = ttk.Entry(send, textvariable=self.send_var, font=("Segoe UI", 11))
        self.entry.pack(side="left", fill="x", expand=True, padx=8)
        self.entry.bind("<Return>", lambda _e: self.send())
        self.entry.bind("<Up>", lambda _e: self._history_step(-1))
        self.entry.bind("<Down>", lambda _e: self._history_step(1))
        self.entry.bind("<Key>", lambda _e: self.file_paths.clear() if self.send_mode.get() == "Files" else None)
        self.browse_btn = ttk.Button(send, text="Browse…", command=self.browse_files)
        self.browse_btn.pack(side="left")
        self.send_btn = ttk.Button(send, text="Send  ➤", style="Accent.TButton", command=self.send)
        self.send_btn.pack(side="left", padx=(8, 0))
        prog = ttk.Frame(send_card, style="Card.TFrame")
        prog.pack(fill="x", pady=(8, 0))
        self.hint = ttk.Label(prog, style="CardMuted.TLabel", anchor="w")
        self.hint.pack(side="left", fill="x", expand=True)
        self.progress_lbl = ttk.Label(prog, text="", style="Card.TLabel", anchor="e")
        self.progress_lbl.pack(side="right", padx=(10, 0))
        self.progress = ttk.Progressbar(prog, length=280, mode="determinate", maximum=1000,
                                        style="Green.Horizontal.TProgressbar")
        self.progress.pack(side="right")
        self._on_mode_change()

        main = ttk.PanedWindow(body, orient="horizontal")
        main.pack(fill="both", expand=True, pady=8)

        # left: Chat / Log
        left_outer = tk.Frame(main, bg=C["border"])
        main.add(left_outer, weight=5)
        left = ttk.Frame(left_outer, style="Card.TFrame", padding=(6, 6, 6, 6))
        left.pack(fill="both", expand=True, padx=1, pady=1)
        self.tabs = ttk.Notebook(left)
        self.tabs.pack(fill="both", expand=True)
        self.chat = self._text_tab("  Chat  ", font=("Segoe UI", 10), bg="white", wrap="word")
        self.log_text = self._text_tab("  Log  ", font=("Segoe UI", 9), bg="#fbfcfe", wrap="word")
        self._build_transfers_tab()
        self._chat_tags()
        self._log_tags()
        opts = ttk.Frame(left, style="Card.TFrame")
        opts.pack(fill="x", pady=(6, 0))
        self.tech_var = tk.BooleanVar(value=False)
        ttk.Checkbutton(opts, text="Technical details in Log", variable=self.tech_var,
                        command=self._apply_tech).pack(side="left")
        self.autoscroll_var = tk.BooleanVar(value=True)
        ttk.Checkbutton(opts, text="Auto-scroll", variable=self.autoscroll_var).pack(side="left", padx=12)
        self.notify_var = tk.BooleanVar(value=self.settings.get("notify", True))
        ttk.Checkbutton(opts, text="Sound + blink when something arrives", variable=self.notify_var,
                        command=self._on_notify_change).pack(side="left")

        # right: values + graph
        right = ttk.PanedWindow(main, orient="vertical")
        main.add(right, weight=4)
        vals_outer = tk.Frame(right, bg=C["border"])
        right.add(vals_outer, weight=2)
        vals = ttk.Frame(vals_outer, style="Card.TFrame", padding=8)
        vals.pack(fill="both", expand=True, padx=1, pady=1)
        ttk.Label(vals, text="Values", style="Title.TLabel").pack(anchor="w")
        ttk.Label(vals, text="Numbers the other side sends. Click a row to show or hide it on the graph.",
                  style="CardMuted.TLabel").pack(anchor="w", pady=(0, 4))
        cols = ("plot", "value", "min", "max", "count", "age")
        self.table = ttk.Treeview(vals, columns=cols, height=6)
        self.table.heading("#0", text="Name")
        self.table.column("#0", width=150)
        for c, w, title in (("plot", 60, "Graph"), ("value", 110, "Value"), ("min", 90, "Min"),
                            ("max", 90, "Max"), ("count", 70, "Count"), ("age", 70, "Age (s)")):
            self.table.heading(c, text=title)
            self.table.column(c, width=w, anchor="e" if c != "plot" else "center")
        self.table.pack(fill="both", expand=True)
        self.table.bind("<ButtonRelease-1>", self.on_table_click)

        plot_outer = tk.Frame(right, bg=C["border"])
        right.add(plot_outer, weight=3)
        plot = ttk.Frame(plot_outer, style="Card.TFrame", padding=8)
        plot.pack(fill="both", expand=True, padx=1, pady=1)
        bar = ttk.Frame(plot, style="Card.TFrame")
        bar.pack(fill="x")
        ttk.Label(bar, text="Graph", style="Title.TLabel").pack(side="left")
        self.window_var = tk.StringVar(value="30 s")
        ttk.Combobox(bar, textvariable=self.window_var, values=["10 s", "30 s", "60 s", "5 min"], width=7,
                     state="readonly").pack(side="right")
        ttk.Label(bar, text="last", style="CardMuted.TLabel").pack(side="right", padx=4)
        self.canvas = tk.Canvas(plot, bg="white", highlightthickness=0)
        self.canvas.pack(fill="both", expand=True, pady=(6, 0))

    def _build_transfers_tab(self) -> None:
        frame = ttk.Frame(self.tabs, style="Card.TFrame", padding=(4, 6, 4, 4))
        self.tabs.add(frame, text="  Transfers  ")
        bar = ttk.Frame(frame, style="Card.TFrame")
        bar.pack(fill="x", pady=(0, 6))
        ttk.Label(bar, text="Every file sent and received. Double-click a row to open its folder.",
                  style="CardMuted.TLabel").pack(side="left")
        self.tr_folder_btn = ttk.Button(bar, text="Open folder", command=lambda: self._open_transfer(False),
                                        state="disabled")
        self.tr_folder_btn.pack(side="right")
        self.tr_open_btn = ttk.Button(bar, text="Open file", command=lambda: self._open_transfer(True),
                                      state="disabled")
        self.tr_open_btn.pack(side="right", padx=6)
        cols = ("dir", "name", "size", "dur", "speed", "status")
        box = ttk.Frame(frame, style="Card.TFrame")
        box.pack(fill="both", expand=True)
        self.tr_table = ttk.Treeview(box, columns=cols, show="tree headings", selectmode="browse")
        self.tr_table.heading("#0", text="When")
        self.tr_table.column("#0", width=92, stretch=False)
        for c, w, title, anchor in (("dir", 82, "", "w"), ("name", 150, "File", "w"), ("size", 66, "Size", "e"),
                                    ("dur", 50, "Time", "e"), ("speed", 78, "Speed", "e"),
                                    ("status", 175, "Status", "w")):
            self.tr_table.heading(c, text=title, anchor=anchor)
            self.tr_table.column(c, width=w, anchor=anchor, stretch=(c in ("name", "status")))
        sb = ttk.Scrollbar(box, command=self.tr_table.yview)
        self.tr_table.configure(yscrollcommand=sb.set)
        sb.pack(side="right", fill="y")
        self.tr_table.pack(side="left", fill="both", expand=True)
        self.tr_table.tag_configure("ok", foreground=C["ok"])
        self.tr_table.tag_configure("bad", foreground=C["err"])
        self.tr_table.tag_configure("warn", foreground=C["warn"])
        self.tr_table.bind("<<TreeviewSelect>>", lambda _e: self._on_transfer_select())
        self.tr_table.bind("<Double-1>", lambda _e: self._open_transfer(False))

    def _text_tab(self, title: str, **kw) -> tk.Text:
        frame = ttk.Frame(self.tabs, style="Card.TFrame")
        self.tabs.add(frame, text=title)
        text = tk.Text(frame, state="disabled", fg=C["text"], relief="flat", padx=10, pady=8,
                       highlightthickness=0, spacing1=2, spacing3=2, cursor="arrow", **kw)
        sb = ttk.Scrollbar(frame, command=text.yview)
        text.configure(yscrollcommand=sb.set)
        sb.pack(side="right", fill="y")
        text.pack(side="left", fill="both", expand=True)
        return text

    def _chat_tags(self) -> None:
        c = self.chat
        c.tag_configure("time", foreground="#9ca3af", font=("Segoe UI", 8))
        c.tag_configure("me", background=C["me_bg"], foreground=C["me_fg"], justify="right",
                        lmargin1=140, lmargin2=140, rmargin=10, spacing1=4, spacing3=4)
        c.tag_configure("them", background=C["them_bg"], foreground=C["them_fg"], justify="left",
                        lmargin1=10, lmargin2=10, rmargin=140, spacing1=4, spacing3=4)
        c.tag_configure("who", font=("Segoe UI", 8, "bold"), foreground="#9ca3af", spacing1=8)
        c.tag_configure("me_head", justify="right", rmargin=10)
        c.tag_configure("them_head", justify="left", lmargin1=10)
        c.tag_configure("link", foreground=C["accent"], underline=True)
        c.tag_bind("link", "<Enter>", lambda _e: c.configure(cursor="hand2"))
        c.tag_bind("link", "<Leave>", lambda _e: c.configure(cursor="arrow"))
        c.tag_configure("center", justify="center", foreground=C["muted"], font=("Segoe UI", 9, "italic"),
                        spacing1=8, spacing3=8)
        c.tag_configure("file", foreground="#92400e")
        c.tag_configure("ok", foreground=C["ok"], font=FONT_BOLD)
        c.tag_configure("bad", foreground=C["err"], font=FONT_BOLD)

    def _log_tags(self) -> None:
        lg = self.log_text
        lg.tag_configure("time", foreground="#9ca3af", font=("Consolas", 9))
        for level in ("info", "ok", "warn", "err", "in", "out"):
            lg.tag_configure(level, foreground=C[level])
        lg.tag_configure("icon", font=("Segoe UI Symbol", 10, "bold"))
        lg.tag_configure("tech", foreground="#9ca3af", font=("Consolas", 9), elide=True)

    def _apply_tech(self) -> None:
        self.log_text.tag_configure("tech", elide=not self.tech_var.get())

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
        self.note("info", f"Received files and logs will be saved to {d}")
        self._load_transfers()
        if self.worker:
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
            self.note("warn", "Baud changed. Click Disconnect, then Connect to use it. Both apps and the "
                              "ground unit menu (Baud) must use the same value.")

    def _ensure_dir(self, sub: str = "") -> str | None:
        d = os.path.join(self.save_dir(), sub) if sub else self.save_dir()
        try:
            os.makedirs(d, exist_ok=True)
            return d
        except OSError as exc:
            self.note("err", f"Cannot write to {d}: {exc}. Click \"Change…\" to pick another folder.")
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
            self.note("info", f"Everything in this session is saved to {path}", tech=True)
        except OSError as exc:
            self.note("err", f"Cannot create the session log: {exc}")
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
                if "bluetooth" in label.lower():
                    continue  # Bluetooth COM ports are never the HM30 link
                items.append(label)
                if best is None and (is_hm30 or p.vid is not None):
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
        if self.worker or self.reconnecting:
            was_reconnecting = self.reconnecting
            self.disconnect()
            self.note("info", "Stopped reconnecting." if was_reconnecting else "Disconnected.")
        else:
            self.connect()

    @staticmethod
    def _port_info(device: str):
        if serial is None:
            return None
        return next((p for p in serial.tools.list_ports.comports() if p.device == device), None)

    def connect(self, auto: bool = False) -> bool:
        """Open the selected port. auto=True: a reconnect attempt (no pop-ups, quiet failures)."""
        label = self.port_var.get()
        device = label.split(" — ")[0]
        ids = None
        try:
            if label == DEMO_PORT:
                port = core.DemoSerial()
            elif label == PAIR_PORT and self.pair_port is not None:
                port = self.pair_port
                port.is_open = True
            else:
                if serial is None:
                    messagebox.showerror("HM30 Link", "pyserial is not installed.\n\npip install pyserial")
                    return False
                info = self._port_info(device)
                ids = (info.vid, info.pid, info.serial_number) if info and info.vid is not None else None
                port = serial.Serial(device, int(self.baud_var.get()), timeout=0.1, write_timeout=2)
        except Exception as exc:
            if not auto:
                self.note("err", f"Could not open {device}: {exc}")
                messagebox.showerror("HM30 Link", f"Could not open {label}:\n\n{exc}\n\n"
                                     "Close PuTTY or any other program using the port, check the USB cable, "
                                     "then try again.")
            return False
        is_real = label not in (DEMO_PORT, PAIR_PORT)
        self.port_label = label if is_real else None
        self.port_ids = ids
        self.worker = core.LinkWorker(port, self.events)
        self.worker.start()
        self.lost_connection = False
        self.reconnecting = False
        self.connect_btn.configure(text="Disconnect", style="Danger.TButton")
        if auto:
            self.note("ok", f"Reconnected to {device}. Everything continues.")
            self.chat_center("Reconnected")
            if self.files_left and not self.files_user_cancelled:
                self._resume_files(time.monotonic())
        else:
            self.note("info", f"Connected to {device} at {self.baud_var.get()} baud. "
                              "Waiting for the other side to answer…")
        self._quality_mark = None
        self._open_session_log()
        return True

    def disconnect(self, keep_files: bool = False) -> None:
        """Close the link. keep_files=True when the link broke (files will be re-sent after reconnecting)."""
        if self.file_cancel:
            if not keep_files:
                self.files_user_cancelled = True
                self.files_left = []
            self.file_cancel.set()
        if self.worker:
            self.worker.stop()
            self.worker = None
        if not keep_files:
            self.reconnecting = False
        self.connect_btn.configure(text="Stop reconnecting" if self.reconnecting else "Connect",
                                   style="Danger.TButton" if self.reconnecting else "Accent.TButton")
        self.incoming.clear()
        self._close_session_log()

    # ---- auto-reconnect -------------------------------------------------------

    def _connection_lost(self, error: str) -> None:
        msg = friendly_error(error)
        self.note("tech", f"error: {error}", tech=True)
        if self.port_label is None:  # demo / pair: nothing to reconnect to
            self.note("err", f"Connection lost: {msg}.")
            self.chat_center(f"Connection lost — {msg}", bad=True)
            self.lost_connection = True
            self.disconnect()
            return
        self.reconnecting = True
        self.reconnect_tries = 0
        self.disconnect(keep_files=True)
        self.note("warn", f"Connection lost: {msg}. Reconnecting automatically — plug it back in, "
                          "or click \"Stop reconnecting\".")
        self.chat_center("Connection lost — reconnecting…", bad=True)
        self.root.after(RECONNECT_EVERY_MS, self._try_reconnect)

    def _find_port_again(self) -> str | None:
        """The label of our adapter, even if Windows gave it another COM number."""
        if serial is None or self.port_label is None:
            return None
        device = self.port_label.split(" — ")[0]
        ports = list(serial.tools.list_ports.comports())
        if self.port_ids:
            for p in ports:
                if (p.vid, p.pid, p.serial_number) == self.port_ids:
                    device = p.device
                    break
            else:
                return None
        elif not any(p.device == device for p in ports):
            return None
        self.refresh_ports()
        return next((v for v in self.port_box["values"] if v.split(" — ")[0] == device), None)

    def _try_reconnect(self) -> None:
        if not self.reconnecting or self.worker:
            return
        self.reconnect_tries += 1
        label = self._find_port_again()
        if label:
            self.port_var.set(label)
            if self.connect(auto=True):
                return
        if self.reconnect_tries % 15 == 0:  # remind every ~30 s
            self.note("warn", "Still trying to reconnect… Check that the USB cable is plugged in.")
        self.root.after(RECONNECT_EVERY_MS, self._try_reconnect)

    def _resume_files(self, started: float) -> None:
        """After a reconnect: wait until the other side answers, then send the unfinished files again."""
        w = self.worker
        if not w or not self.files_left or self.files_user_cancelled or self.file_cancel:
            return
        if w.peer_is_app or w.hello_ok:
            files = list(self.files_left)
            self.note("out", f"Sending {len(files)} unfinished file(s) again after the reconnect…")
            self.send_files(files, resumed=True)
        elif time.monotonic() - started < RESUME_WAIT_S:
            self.root.after(500, self._resume_files, started)
        else:
            self.note("warn", f"The other side did not answer after the reconnect. {len(self.files_left)} "
                              "file(s) were not sent — send them again when it is back.")
            self.files_left = []

    def on_close(self) -> None:
        self.disconnect()
        self.root.after(150, self.root.destroy)

    # ---- receiving ----------------------------------------------------------

    def poll_events(self) -> None:
        start = time.perf_counter()
        try:
            while time.perf_counter() - start < EVENT_BUDGET_S:
                ev = self.events.get_nowait()
                self._handle_event(ev)
        except queue.Empty:
            pass
        for widget in self._scroll:  # scroll once per slice, not once per line
            widget.see("end")
        self._scroll.clear()
        more = not self.events.empty()
        self.root.after(5 if more else 50, self.poll_events)

    def _handle_event(self, ev) -> None:
        kind = ev[0]
        if kind == "rx":
            _, ts, _nbytes, items = ev
            self.last_rx = time.monotonic()
            for item in items:
                self.handle_item(ts, item)
        elif kind == "error":
            self._connection_lost(ev[1])
        elif kind == "closed":
            self.note("tech", "port closed", tech=True)
        elif kind == "info":
            self.note("tech", ev[1], tech=True)
        elif kind == "peer":
            if ev[1]:
                self.note("ok", "The other app answered — you can send and receive messages and files.")
                self.chat_center("Other app connected")
            else:
                self.note("info", "The other app reconnected.", tech=True)
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
            self.send_btn.configure(text="Send  ➤", style="Accent.TButton")
            if not self.reconnecting:
                self.files_left = []

    def handle_item(self, ts: float, item) -> None:
        stamp = datetime.fromtimestamp(ts).strftime("%H:%M:%S.%f")[:-3]
        peer_app = bool(self.worker and self.worker.peer_is_app)
        if isinstance(item, proto.RawLine):
            text = item.text.strip()
            if not text:
                return
            if readable(text):  # e.g. printf from an STM32
                self.note("in", f"Text: {clip(text)}")
                self.chat_bubble("them", clip(text), ts)
                self.alert("message")
                self._record(stamp, "in", "raw", "", text)
            else:  # radio noise / wrong baud / two devices on one UART: count it, do not draw it
                self.noise_bytes += len(text)
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
            if peer_app:  # an STM32 streams values all the time: keep those out of chat and log
                self.note("in", f"Number: {clip(name)} = {value:.6g}")
                self.chat_bubble("them", f"{clip(name)} = {value:.6g}", ts)
                self.alert("number")
            else:
                self.note("tech", f"value {clip(name)} = {value:.6g}", tech=True)
        elif t == proto.TYPE_BYTES:
            hexs = item.payload.hex(" ")
            self.note("in", f"Bytes ({len(item.payload)}): {clip(hexs)}")
            self._record(stamp, "in", "bytes", "", hexs)
            if peer_app:
                self.chat_bubble("them", f"[{len(item.payload)} bytes] {clip(hexs)}", ts)
        elif t == proto.TYPE_FILE_START:
            self.on_file_start(item.payload)
        elif t == proto.TYPE_FILE_DATA:
            self.on_file_data(item.payload)
        elif t == proto.TYPE_FILE_END:
            self.on_file_end(item.payload, stamp)
        else:  # TEXT, CMD, unknown types
            text = clip(item.text())
            what = "Message" if t == proto.TYPE_TEXT else "Command" if t == proto.TYPE_CMD else item.type_name
            self.note("in", f"{what}: {text}")
            self.chat_bubble("them", text if t == proto.TYPE_TEXT else f"[{what}] {text}", ts)
            self.alert("message")
            self._record(stamp, "in", "text" if t == proto.TYPE_TEXT else item.type_name.lower(), "", item.text())

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
            self.note("err", f"A file could not be started: {exc}")
            return
        info = {"name": f.name, "index": 1, "count": 1, "size": f.original_size, "sent_size": f.sent_size,
                "compressed": bool(f.flags & proto.FILE_ZLIB)}
        self.incoming[f.fid] = (f, time.monotonic(), info)
        extra = f" (compressed to {human_size(f.sent_size)} for the radio)" if info["compressed"] else ""
        self.note("in", f"Receiving file {f.name} — {human_size(f.original_size)}{extra}…")
        self.show_progress(core.receive_progress(info, 0.0, time.monotonic()))

    def on_file_data(self, payload: bytes) -> None:
        entry = self.incoming.get(proto.file_data_id(payload))
        if not entry:
            return
        f, t0, info = entry
        try:
            f.add(payload)
        except proto.FileError as exc:
            self.note("tech", f"file {f.name}: {exc}", tech=True)
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
            self.note("err", f"File {f.name} arrived damaged and was NOT saved ({exc}). "
                             "Ask the sender to send it again.")
            self.chat_center(f"✖ {f.name} arrived damaged and was not saved", bad=True)
            self.show_progress(None)
            self.add_transfer("in", f.name, f.original_size, time.monotonic() - t0, "✖ damaged, not saved", "",
                              "bad")
            self.alert(f"{f.name} arrived damaged")
            return
        folder = self._ensure_dir()
        if not folder:
            return
        path = os.path.normpath(proto.unique_path(folder, f.name))
        try:
            with open(path, "wb") as out:
                out.write(data)
        except OSError as exc:
            self.note("err", f"Cannot save {path}: {exc}")
            return
        secs = max(time.monotonic() - t0, 1e-6)
        self.show_progress(core.receive_progress(info, 1.0, t0), done_text=f"Received {f.name}  ✔")
        self.note("ok", f"Saved {os.path.basename(path)} — {human_size(len(data))} in {human_time(secs)} "
                        f"({human_size(len(data) / secs)}/s), checked OK → {path}")
        self.chat_bubble("them", f"📁 {os.path.basename(path)}  ({human_size(len(data))})  ✔ saved",
                         tag2="file", link=(" open folder ", os.path.dirname(path)),
                         image=path if is_image(path) else None)
        self._record(stamp, "in", "file", os.path.basename(path), path)
        self.add_transfer("in", os.path.basename(path), len(data), secs, "✔ received, checked", path, "ok")
        self.alert(f"File received: {os.path.basename(path)}")

    # ---- chat / log ---------------------------------------------------------

    def _append(self, widget: tk.Text, parts: list[tuple[str, tuple]]) -> None:
        widget.configure(state="normal")
        for text, tags in parts:
            widget.insert("end", text, tags)
        lines = int(widget.index("end-1c").split(".")[0])
        if lines > MAX_LINES:
            widget.delete("1.0", f"{lines - MAX_LINES}.0")
        widget.configure(state="disabled")
        if self.autoscroll_var.get():
            self._scroll.add(widget)

    def chat_bubble(self, who: str, text: str, ts: float | None = None, token: int | None = None,
                    parts: int = 1, tag2: str | None = None, link: tuple[str, str] | None = None,
                    image: str | None = None) -> None:
        if self.paused:
            return
        stamp = datetime.fromtimestamp(ts or time.time()).strftime("%H:%M")
        c = self.chat
        tags = (who,) + ((tag2,) if tag2 else ())
        c.configure(state="normal")
        c.insert("end", f"{'You' if who == 'me' else 'Them'} · {stamp}\n", (f"{who}_head", "who"))
        c.insert("end", f"  {text}  ", tags)
        if link:
            self._links = getattr(self, "_links", 0) + 1
            ltag = f"link{self._links}"
            c.insert("end", link[0], (who, "link", ltag))
            c.tag_bind(ltag, "<Button-1>", lambda _e, d=link[1]: open_folder(d))
        if token is not None:
            mark = f"tok{token}"
            c.mark_set(mark, "end-1c")
            c.mark_gravity(mark, "left")
            self.chat_marks[token] = {"mark": mark, "left": parts, "failed": 0}
        c.insert("end", "\n")
        thumb = make_thumbnail(image) if image else None
        if thumb is not None:  # photo preview; click it to open the photo
            self._thumbs.append(thumb)
            self._links = getattr(self, "_links", 0) + 1
            ltag = f"img{self._links}"
            start = c.index("end-1c")
            c.image_create("end", image=thumb, padx=4, pady=4)
            c.tag_add(who, start)
            c.tag_add(ltag, start)
            c.tag_add("link", start)
            c.tag_bind(ltag, "<Button-1>", lambda _e, p=image: open_file(p))
            c.insert("end", "\n", (who,))
        c.configure(state="disabled")
        self._append(c, [])

    # ---- notifications ------------------------------------------------------

    def _has_focus(self) -> bool:
        try:
            w = self.root.focus_get()
        except (KeyError, tk.TclError):  # focus is in a dialog / another window
            return False
        return w is not None and w.winfo_toplevel() is self.root

    def alert(self, what: str) -> None:
        """Something arrived: if the window is in the background, count it, blink and beep."""
        if self._has_focus():
            return
        self.unread += 1
        self.root.title(f"({self.unread}) {self.title}")
        if not self.notify_var.get():
            return
        flash_taskbar(self.root)
        now = time.monotonic()
        if winsound and now - self._last_sound > 3:
            self._last_sound = now
            try:
                winsound.MessageBeep(winsound.MB_ICONASTERISK)
            except Exception:
                pass

    def _on_focus(self, _event=None) -> None:
        if self.unread:
            self.unread = 0
            self.root.title(self.title)

    def _on_notify_change(self) -> None:
        self.settings["notify"] = self.notify_var.get()
        self._persist()

    # ---- transfers history ----------------------------------------------------

    def _transfers_csv(self) -> str:
        return os.path.join(self.save_dir(), "logs", "transfers.csv")

    def _load_transfers(self) -> None:
        self.transfers.clear()
        self.tr_table.delete(*self.tr_table.get_children())
        try:
            with open(self._transfers_csv(), newline="", encoding="utf-8") as f:
                rows = list(csv.DictReader(f))[-MAX_TRANSFERS:]
        except OSError:
            return
        for r in rows:
            try:
                r["size"] = int(r["size"])
                r["secs"] = float(r["secs"]) if r.get("secs") else None
            except (KeyError, ValueError):
                continue
            self._show_transfer(r)

    def add_transfer(self, direction: str, name: str, size: int, secs: float | None, status: str, path: str,
                     tag: str) -> None:
        r = {"when": datetime.now().strftime("%Y-%m-%d %H:%M:%S"), "dir": direction, "name": name,
             "size": size, "secs": secs, "status": status, "path": path, "tag": tag}
        self._show_transfer(r)
        d = self._ensure_dir("logs")
        if not d:
            return
        path_csv = self._transfers_csv()
        new = not os.path.exists(path_csv)
        try:
            with open(path_csv, "a", newline="", encoding="utf-8") as f:
                wr = csv.DictWriter(f, fieldnames=list(r))
                if new:
                    wr.writeheader()
                wr.writerow({**r, "secs": "" if secs is None else f"{secs:.2f}"})
        except OSError:
            pass

    def _show_transfer(self, r: dict) -> None:
        self.transfers.append(r)
        secs, size = r.get("secs"), r["size"]
        speed = f"{human_size(size / secs)}/s" if secs and secs > 0 and r.get("tag") == "ok" else ""
        when = r["when"][5:16].replace("-", ".") if len(r["when"]) >= 16 else r["when"]
        iid = str(len(self.transfers) - 1)
        self.tr_table.insert("", 0, iid=iid, text=when, tags=(r.get("tag", ""),), values=(
            "⬆ sent" if r["dir"] == "out" else "⬇ received", r["name"], human_size(size),
            human_time(secs) if secs else "", speed, r["status"]))
        kids = self.tr_table.get_children()
        if len(kids) > MAX_TRANSFERS:
            self.tr_table.delete(*kids[MAX_TRANSFERS:])

    def _selected_transfer(self) -> dict | None:
        sel = self.tr_table.selection()
        return self.transfers[int(sel[0])] if sel else None

    def _on_transfer_select(self) -> None:
        r = self._selected_transfer()
        ok = bool(r and r.get("path") and os.path.exists(r["path"]))
        self.tr_open_btn.configure(state="normal" if ok else "disabled")
        self.tr_folder_btn.configure(state="normal" if ok else "disabled")

    def _open_transfer(self, the_file: bool) -> None:
        r = self._selected_transfer()
        if not r or not r.get("path") or not os.path.exists(r["path"]):
            self.note("warn", "This file is not on this PC (any more).")
            return
        open_file(r["path"]) if the_file else open_folder(os.path.dirname(r["path"]))

    def chat_center(self, text: str, bad: bool = False) -> None:
        self._append(self.chat, [(f"{text}\n", ("center", "bad") if bad else ("center",))])

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
            c.insert(info["mark"], " ✖ not delivered ", ("me", "bad"))
        else:
            c.insert(info["mark"], "✔ ", ("me", "ok"))
        c.configure(state="disabled")

    def note(self, level: str, text: str, tech: bool = False, ts: float | None = None) -> None:
        """One line in the Log tab. tech=True lines only show with "Show technical details"."""
        if self.paused and level in ("in", "tech"):
            return
        stamp = datetime.fromtimestamp(ts or time.time()).strftime("%H:%M:%S")
        if tech:
            parts = [(f"{stamp}  {text}\n", ("tech",))]
        else:
            parts = [(f"{stamp}  ", ("time",)), (LEVEL_ICON.get(level, "·") + "  ", ("icon", level)),
                     (text + "\n", (level,))]
        self._append(self.log_text, parts)

    # ---- sending ------------------------------------------------------------

    def _on_mode_change(self) -> None:
        mode = self.send_mode.get()
        self.hint.configure(text=SEND_HINTS[mode])
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
            self.note("warn", "Cancelling the file transfer…")
            return
        text = self.send_var.get().strip()
        mode = self.send_mode.get()
        if not text:
            return
        if not self.worker:
            self.note("warn", "Not connected. Choose the port and click Connect first.")
            return
        w = self.worker
        stamp = datetime.now().strftime("%H:%M:%S.%f")[:-3]
        try:
            if mode == "Files":
                self.send_files(self.file_paths or [text])
                return
            if mode == "Plain text line":  # no packet, no confirmation (like typing in PuTTY)
                w.send((text + "\r\n").encode("utf-8"))
                self.note("out", f"Plain text sent (no confirmation): {clip(text)}")
                self.chat_bubble("me", text)
                self._record(stamp, "out", "raw", "", text)
            elif mode == "Number":
                values = proto.parse_values(text)
                token = w.new_token()
                shown = ", ".join(f"{n} = {v:g}" for n, v in values)
                self.chat_bubble("me", shown, token=token, parts=len(values))
                for name, v in values:
                    w.send_reliable(proto.TYPE_VALUE, proto.value_payload(name, v), f"number {name} = {v:g}",
                                    token=token)
                    self._record(stamp, "out", "value", name, f"{v:g}")
            else:
                if mode == "Bytes (hex)":
                    data, ftype = proto.parse_hex(text), proto.TYPE_BYTES
                    shown = f"[{len(data)} bytes] {data[:32].hex(' ')}{' …' if len(data) > 32 else ''}"
                    desc, kind, value = f"bytes ({len(data)})", "bytes", data.hex(" ")
                else:
                    data = text.encode("utf-8")
                    ftype = proto.TYPE_CMD if mode == "Command" else proto.TYPE_TEXT
                    shown = f"[Command] {text}" if mode == "Command" else text
                    desc = f"{'command' if mode == 'Command' else 'message'} “{clip(text)}”"
                    kind, value = ("cmd" if mode == "Command" else "text"), text
                parts = proto.chunk(data)
                token = w.new_token()
                self.chat_bubble("me", shown, token=token, parts=len(parts))
                for part in parts:
                    w.send_reliable(ftype, part, desc, token=token)
                self._record(stamp, "out", kind, "", value)
        except ValueError as exc:
            self.note("warn", f"Not sent: {exc}")
            return
        except Exception as exc:
            self.note("err", f"Sending failed: {friendly_error(str(exc))}")
            return
        self._remember(text)
        self.send_var.set("")

    def on_delivery(self, ok: bool, desc: str, tries: int, report: bool, token: int | None) -> None:
        if desc == "hello":
            if ok and not (self.worker and self.worker.peer_is_app):
                self.root.after(800, self._hello_stm_check)
            elif not ok and not (self.worker and self.worker.peer_is_app):
                self.note("warn", "No answer from the other side yet. Check: is the other app connected? "
                                  "Same baud on both sides and in the ground unit menu? Air unit powered and linked?")
            return
        if token is not None:
            self._chat_mark(token, ok)
        if not report:
            return
        if ok:
            self.note("out", f"Delivered {desc}" + (f" (needed {tries} tries)" if tries > 1 else ""))
        else:
            self.note("err", f"NOT delivered: {desc} — no answer after {tries} tries. Is the link still up?")

    def _hello_stm_check(self) -> None:
        w = self.worker
        if w and w.hello_ok and not w.peer_is_app:
            self.note("ok", "An STM32 answered — commands and messages you send are confirmed.")

    # ---- sending files ------------------------------------------------------

    def send_files(self, paths: list[str], resumed: bool = False) -> None:
        paths = [p for p in paths if p]
        missing = [p for p in paths if not os.path.isfile(p)]
        if missing or not paths:
            self.note("warn", f"File not found: {missing[0] if missing else '(none)'} — use Browse…")
            return
        total = sum(os.path.getsize(p) for p in paths)
        bps = int(self.baud_var.get()) / 10 * 0.7  # rough, before compression
        if not resumed and total / bps > BIG_FILE_ASK and not messagebox.askyesno(
            "HM30 Link", f"{len(paths)} file(s), {human_size(total)}.\nThis can take about "
                         f"{human_time(total / bps)} at {self.baud_var.get()} baud (less if it compresses well)."
                         "\n\nSend?"):
            return
        for p in paths:
            self._remember(p)
        self.files_left = list(paths)
        self.files_user_cancelled = False
        worker = self.worker
        cancel = self.file_cancel = threading.Event()
        self.send_btn.configure(text="Cancel", style="Danger.TButton")
        if len(paths) > 1:
            self.note("out", f"Sending {len(paths)} files ({human_size(total)}) one after another…")
        threading.Thread(target=core.send_files,
                         args=(worker, paths, self.events, cancel,
                               lambda: worker is self.worker and not worker.stopped), daemon=True).start()
        self.send_var.set("")
        self.file_paths = []

    def on_file_begin(self, info: dict) -> None:
        extra = (f", compressed to {human_size(info['sent_size'])} for the radio" if info["compressed"] else "")
        count = f" ({info['index']}/{info['count']})" if info["count"] > 1 else ""
        self.note("out", f"Sending file {info['name']}{count} — {human_size(info['size'])}{extra}…")
        token = self.worker.new_token() if self.worker else None
        self._file_token = token
        self.chat_bubble("me", f"📎 {info['name']}  ({human_size(info['size'])})", token=token, tag2="file",
                         image=info.get("path") if is_image(info["name"]) else None)
        self.show_progress({**info, "dir": "send", "frac": 0.0, "speed": 0.0, "eta": math.inf})

    def on_file_sent(self, info: dict) -> None:
        token = self._file_token
        path = info.get("path", "")
        if info.get("ok"):
            self.note("ok", f"Sent {info['name']} — {human_size(info['size'])} in {human_time(info['secs'])} "
                            f"({human_size(info['speed'])}/s). The other side confirmed every piece.")
            if token is not None:
                self._chat_mark(token, True)
            self.show_progress({**info, "dir": "send", "frac": 1.0, "speed": info["speed"], "eta": 0},
                               done_text=f"Sent {info['name']}  ✔")
            self._record(datetime.now().strftime("%H:%M:%S.%f")[:-3], "out", "file", info["name"], path)
            if path in self.files_left:
                self.files_left.remove(path)
            self.add_transfer("out", info["name"], info["size"], info["secs"], "✔ sent", path, "ok")
        elif self.reconnecting:  # the link broke: it is sent again automatically after the reconnect
            self.note("warn", f"Sending {info['name']} was interrupted by the connection loss. "
                              "It will be sent again automatically after reconnecting.")
            if token is not None:
                self._chat_mark(token, False)
            self.show_progress(None)
            self.add_transfer("out", info["name"], info["size"], None, "⟳ interrupted, retried", path, "warn")
        elif info.get("cancelled"):
            self.note("warn", f"Sending {info['name']} was cancelled.")
            if token is not None:
                self._chat_mark(token, False)
            self.show_progress(None)
            self.add_transfer("out", info["name"], info["size"], None, "cancelled", path, "warn")
        else:
            self.note("err", f"File {info['name']} was NOT delivered: {info.get('error', 'failed')}")
            if token is not None:
                self._chat_mark(token, False)
            self.show_progress(None)
            if path in self.files_left:
                self.files_left.remove(path)
            self.add_transfer("out", info["name"], info.get("size", 0), None, "✖ not delivered", path, "bad")

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
        self.progress_lbl.configure(text=f"{arrow} {p['name']}{count}   {p['frac'] * 100:.0f}%  ·  {speed}  ·  {left}")

    # ---- status / table / graph ---------------------------------------------

    def on_table_click(self, event) -> None:
        name = self.table.identify_row(event.y)
        if name in self.series:
            s = self.series[name]
            s.plot = not s.plot

    def _set_badge(self, text: str, color: str) -> None:
        if self.badge.cget("text") != text:
            self.badge.configure(text=text, bg=color)

    def update_status(self) -> None:
        now = time.monotonic()
        for name, s in self.series.items():
            if not s.points:
                continue
            t, v = s.points[-1]
            vals = ("● on" if s.plot else "off", f"{v:.4g}", f"{s.vmin:.4g}", f"{s.vmax:.4g}", s.count,
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

        if not w and self.reconnecting:
            self._set_badge(f"⟳ Reconnecting… (try {self.reconnect_tries})", C["warn"])
        elif not w:
            if self.lost_connection:
                self._set_badge("✖ Connection lost", C["err"])
            else:
                self._set_badge("● Disconnected", "#64748b")
        elif w.peer_is_app:
            self._set_badge("● Connected to the other app", C["ok"])
        elif w.hello_ok:
            self._set_badge("● Connected to STM32", C["ok"])
        elif self.last_rx and now - self.last_rx < 2:
            self._set_badge("● Receiving data", C["ok"])
        else:
            self._set_badge("● Waiting for the other side…", C["warn"])

        self._watch_quality(now, st)

        if w and w.peer_is_app:  # app to app: quality = share of packets that needed no re-send
            quality = f"{100 * tx.sent / (tx.sent + tx.retries):.0f}%" if tx.sent else "–"
        else:
            total = st.frames + st.crc_errors + st.lost
            quality = f"{100 * st.frames / total:.0f}%" if total else "–"
        delay = f"{w.srtt * 1000:.0f} ms" if w and w.srtt else "–"
        self.status.configure(
            text=f"Link quality {quality}   ·   delay {delay}   ·   received {st.frames} packets "
                 f"({st.crc_errors} damaged, {st.lost} lost)   ·   sent {tx.sent}, confirmed {tx.delivered}, "
                 f"re-sent {tx.retries}, failed {tx.failed}")
        if self.session_file:
            try:
                self.session_file.flush()
            except OSError:
                pass
        self.root.after(500, self.update_status)

    def _watch_quality(self, now: float, st: proto.ParserStats) -> None:
        """Explain problems in plain words, at most every 10 s."""
        if self.noise_bytes and now - self._noise_reported > 10:
            self._noise_reported = now
            self.note("warn", f"Radio noise: {self.noise_bytes:,} unreadable bytes were ignored. If this keeps "
                              "happening: same baud on both sides and in the ground unit menu? Only ONE device "
                              "connected to the air unit UART? GND wire connected?")
            self.noise_bytes = 0
        tx = self.worker.tx if self.worker else core.TxStats()
        mark = (now, st.frames, st.crc_errors, st.lost, tx.sent, tx.retries)
        if self._quality_mark is None:
            self._quality_mark = mark
            return
        t0, f0, d0, l0, s0, r0 = self._quality_mark
        if now - t0 < 10:
            return
        self._quality_mark = mark
        frames, damaged, lost = st.frames - f0, st.crc_errors - d0, st.lost - l0
        sent, resent = tx.sent - s0, tx.retries - r0
        bad = damaged + lost
        if bad >= 5 and bad > 0.1 * (frames + bad):
            self.note("warn", f"Weak or noisy link: {damaged} damaged and {lost} lost packets in the last 10 s. "
                              "Try moving the units or the antennas.")
        elif resent >= 5 and resent > 0.25 * max(sent, 1):
            self.note("warn", f"Weak link: {resent} packets had to be re-sent in the last 10 s (they still arrive, "
                              "just slower). Try moving the units or the antennas.")

    def redraw_plot(self) -> None:
        c = self.canvas
        c.delete("all")
        w, h = c.winfo_width(), c.winfo_height()
        if w > 50 and h > 50:
            self._draw_plot(c, w, h)
        self.root.after(150, self.redraw_plot)

    def _draw_plot(self, c: tk.Canvas, w: int, h: int) -> None:
        span = {"10 s": 10, "30 s": 30, "60 s": 60, "5 min": 300}.get(self.window_var.get(), 30)
        now = time.monotonic()
        t_start = now - span
        visible = {n: [(t, v) for t, v in s.points if t >= t_start] for n, s in self.series.items() if s.plot}
        visible = {n: p for n, p in visible.items() if p}
        left, right, top, bottom = 60, 12, 26, 24
        pw, ph = w - left - right, h - top - bottom
        if not visible:
            c.create_text(w / 2, h / 2, text="No numbers yet — values sent by the other side appear here",
                          fill="#9ca3af", font=("Segoe UI", 10))
            return
        lo = min(v for p in visible.values() for _, v in p)
        hi = max(v for p in visible.values() for _, v in p)
        if hi - lo < 1e-9:
            lo, hi = lo - 1, hi + 1
        pad = (hi - lo) * 0.08
        lo, hi = lo - pad, hi + pad
        for i in range(5):
            y = top + ph * i / 4
            c.create_line(left, y, left + pw, y, fill=C["grid"])
            c.create_text(left - 8, y, text=f"{hi - (hi - lo) * i / 4:.4g}", anchor="e", fill=C["muted"],
                          font=("Segoe UI", 8))
        for i in range(6):
            x = left + pw * i / 5
            c.create_line(x, top, x, top + ph, fill=C["grid"])
            c.create_text(x, top + ph + 12, text=f"-{span * (5 - i) / 5:.0f}s" if i < 5 else "now",
                          fill=C["muted"], font=("Segoe UI", 8))
        lx = left
        for name, pts in visible.items():
            s = self.series[name]
            step = max(1, len(pts) // pw)
            coords = []
            for t, v in pts[::step]:
                coords += [left + pw * (t - t_start) / span, top + ph * (hi - v) / (hi - lo)]
            if len(coords) >= 4:
                c.create_line(*coords, fill=s.color, width=2, smooth=False)
            else:
                c.create_oval(coords[0] - 3, coords[1] - 3, coords[0] + 3, coords[1] + 3, fill=s.color, outline="")
            label = f"{name}  {pts[-1][1]:.4g}"
            c.create_oval(lx, 8, lx + 9, 17, fill=s.color, outline="")
            c.create_text(lx + 14, 12, text=label, anchor="w", fill=C["text"], font=("Segoe UI", 9, "bold"))
            lx += 34 + 7 * len(label)

    # ---- misc ---------------------------------------------------------------

    def toggle_pause(self) -> None:
        self.paused = not self.paused
        self.pause_btn.configure(text="Resume" if self.paused else "Pause")
        self.note("info", "Paused: new messages are not shown (they are still received and saved)."
                  if self.paused else "Resumed.")

    def clear_all(self) -> None:
        for widget in (self.log_text, self.chat):
            widget.configure(state="normal")
            widget.delete("1.0", "end")
            widget.configure(state="disabled")
        self.chat_marks.clear()
        self.series.clear()
        self.table.delete(*self.table.get_children())
        self.csv_rows.clear()
        self.rate_hist.clear()
        self.noise_bytes = 0
        self._quality_mark = None
        if self.worker:  # start the counters from zero too
            self.worker.parser.stats = proto.ParserStats()
            self.worker.tx = core.TxStats()
        self.note("info", "Cleared.")

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
        self.note("ok", f"Saved {len(self.csv_rows)} rows to {path}")


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
