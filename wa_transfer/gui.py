"""The guided wizard: explains every manual step and does the rest on its own."""

from __future__ import annotations

import logging
import os
import platform
import queue
import shutil
import sqlite3
import tempfile
import threading
import tkinter as tk
import traceback
import webbrowser
from datetime import datetime
from pathlib import Path
from tkinter import filedialog, font as tkfont, messagebox, ttk
from typing import Callable

from . import __version__, android, converter, crypt15, diagnosis, ios_backup, session
from .android import Android, AndroidError
from .session import NotEnoughSpaceError, Session, human_size

log = logging.getLogger(__name__)

PAPER = "#F7F7F2"
CARD = "#FFFFFF"
INK = "#1D2925"
MUTED = "#5A6862"
LINE = "#DADDD4"
ACCENT = "#0B6E55"
ACCENT_DARK = "#08553F"
RAIL = "#10302A"
RAIL_TEXT = "#B9CCC5"
RAIL_ACTIVE = "#FFFFFF"
RAIL_DONE = "#6FD3AE"
TONES = {
    "info": ("#E8F1EE", "#0B4F3E"),
    "ok": ("#DFF3E9", "#0A5B41"),
    "warn": ("#FFF3D1", "#6B4B00"),
    "error": ("#FBE4DF", "#8A2412"),
}

APPLE_DEVICES_URL = "https://apps.microsoft.com/detail/9NP83LWLPZ9K"
USB_DRIVER_URL = "https://developer.android.com/studio/run/oem-usb"
GOOGLE_BACKUPS_URL = "https://drive.google.com/drive/backups"
LOG_LINES_IN_REPORT = 300

USER_ERRORS = (
    AndroidError,
    ios_backup.BackupError,
    crypt15.Crypt15Error,
    converter.SchemaError,
    NotEnoughSpaceError,
    ValueError,
)

PAGES = [
    "Before you start",
    "iPhone backup",
    "Connect the Android phone",
    "Prepare WhatsApp",
    "Convert the chats",
    "Copy to the phone",
    "Restore in WhatsApp",
    "Final check",
]
(P_WELCOME, P_IPHONE, P_CONNECT, P_WHATSAPP, P_CONVERT, P_COPY, P_RESTORE, P_END) = range(len(PAGES))


def counted(count: int, one: str, many: str) -> str:
    return f"{count:,} " + (one if count == 1 else many)


def default_workdir() -> Path:
    return Path(os.environ.get("USERPROFILE") or Path.home()) / "Documents" / "WhatsApp Transfer"


def tool_dir() -> Path:
    return Path(os.environ.get("LOCALAPPDATA") or Path.home()) / "WhatsAppTransfer" / "platform-tools"


class App(tk.Tk):
    def __init__(self, workdir: Path | None = None, start_page: int | None = None):
        super().__init__()
        self.title(f"WhatsApp Transfer · iPhone → Android · v{__version__}")
        self.geometry("1040x760")
        self.minsize(860, 600)
        self.configure(bg=PAPER)

        self.workdir = workdir or default_workdir()
        self.session = self._open_session()
        self.device: Android | None = None
        self.device_status: android.DeviceStatus | None = None
        self.backups: list[ios_backup.BackupInfo] = []
        self.key_text = ""
        self.phone_backup: session.PhoneBackup | None = None
        self.page = P_WELCOME
        self.reached = P_WELCOME
        self.trouble = False
        self.busy = False
        self._events: queue.Queue = queue.Queue()
        self._wrapped: list[tuple[tk.Label, int]] = []
        self._device_label: tk.Label | None = None
        self._device_refresh: Callable[[], None] | None = None
        self._polling = False

        self._fonts()
        self._layout()
        self.protocol("WM_DELETE_WINDOW", self._on_close)
        self.after(100, self._drain_events)
        self.after(1500, self._poll_device)

        resume = self._resume_page()
        self.reached = max(self.reached, resume)
        self.show(start_page if start_page is not None else resume)

    def _open_session(self) -> Session:
        session_obj = Session(self.workdir)
        root = logging.getLogger()
        root.handlers.clear()
        handler = logging.FileHandler(self.workdir / "transfer.log", encoding="utf-8")
        handler.setFormatter(logging.Formatter("%(asctime)s %(levelname)s %(name)s: %(message)s"))
        root.addHandler(handler)
        root.setLevel(logging.INFO)
        log.info("Start: version %s, %s", __version__, platform.platform())
        return session_obj

    def report_callback_exception(self, exc, val, tb) -> None:
        # With no console Tk would write the error to nowhere and the button would seem to do nothing.
        log.error("Unexpected error in the interface\n%s", "".join(traceback.format_exception(exc, val, tb)))
        messagebox.showerror(
            "Unexpected error",
            f"Something went wrong. No data was lost.\n\nThe details are in the file transfer.log, in the folder:\n{self.workdir}",
        )

    def _resume_page(self) -> int:
        state = self.session.state
        if state.copied:
            return P_RESTORE
        if state.converted and self.session.output_backup.is_file():
            return P_COPY
        if state.phone_changed:
            # The converted backup is missing or from an earlier round: it must not be copied.
            return P_CONVERT
        return P_WELCOME

    # ---- window structure

    def _fonts(self) -> None:
        family = "Segoe UI" if "Segoe UI" in tkfont.families() else "TkDefaultFont"
        self.f_body = tkfont.Font(family=family, size=11)
        self.f_small = tkfont.Font(family=family, size=10)
        self.f_bold = tkfont.Font(family=family, size=11, weight="bold")
        self.f_title = tkfont.Font(family=family, size=22, weight="bold")
        self.f_section = tkfont.Font(family=family, size=13, weight="bold")
        self.f_rail = tkfont.Font(family=family, size=10)
        self.f_rail_on = tkfont.Font(family=family, size=10, weight="bold")
        self.f_mono = tkfont.Font(family="Consolas", size=12)

    def _layout(self) -> None:
        self.columnconfigure(1, weight=1)
        self.rowconfigure(0, weight=1)

        self.rail = tk.Frame(self, bg=RAIL, width=236)
        self.rail.grid(row=0, column=0, rowspan=2, sticky="ns")
        self.rail.grid_propagate(False)
        self.rail.pack_propagate(False)
        tk.Label(
            self.rail, text="WhatsApp\niPhone → Android", bg=RAIL, fg=RAIL_ACTIVE, font=self.f_section,
            justify="left", anchor="w",
        ).pack(fill="x", padx=22, pady=(26, 24))
        self.rail_items = []
        for index, name in enumerate(PAGES):
            label = tk.Label(self.rail, text=f"{index + 1}   {name}", bg=RAIL, fg=RAIL_TEXT, font=self.f_rail, anchor="w")
            label.pack(fill="x", padx=22, pady=6)
            self.rail_items.append(label)

        holder = tk.Frame(self, bg=PAPER)
        holder.grid(row=0, column=1, sticky="nsew")
        holder.rowconfigure(0, weight=1)
        holder.columnconfigure(0, weight=1)
        self.canvas = tk.Canvas(holder, bg=PAPER, highlightthickness=0)
        self.canvas.grid(row=0, column=0, sticky="nsew")
        scroll = ttk.Scrollbar(holder, orient="vertical", command=self.canvas.yview)
        scroll.grid(row=0, column=1, sticky="ns")
        self.canvas.configure(yscrollcommand=scroll.set)
        self.body = tk.Frame(self.canvas, bg=PAPER)
        self._body_window = self.canvas.create_window((0, 0), window=self.body, anchor="nw")
        self.body.bind("<Configure>", lambda _e: self.canvas.configure(scrollregion=self.canvas.bbox("all")))
        self.canvas.bind("<Configure>", self._on_resize)
        self.bind_all("<MouseWheel>", lambda e: self.canvas.yview_scroll(int(-e.delta / 120), "units"))

        bar = tk.Frame(self, bg=CARD, highlightbackground=LINE, highlightthickness=1)
        bar.grid(row=1, column=1, sticky="ew")
        bar.columnconfigure(1, weight=1)
        self.back_button = self._button(bar, "← Back", self._go_back, secondary=True)
        self.back_button.grid(row=0, column=0, padx=16, pady=12)
        self.nav_hint = tk.Label(bar, text="", bg=CARD, fg=MUTED, font=self.f_small, anchor="e")
        self.nav_hint.grid(row=0, column=1, sticky="ew", padx=8)
        self.next_button = self._button(bar, "Next →", self._go_next)
        self.next_button.grid(row=0, column=2, padx=16, pady=12)

    def _on_resize(self, event) -> None:
        self.canvas.itemconfigure(self._body_window, width=event.width)
        for label, inset in self._wrapped:
            if label.winfo_exists():
                label.configure(wraplength=max(event.width - inset, 200))

    def _button(self, parent, text, command, secondary=False, danger=False) -> tk.Button:
        bg = CARD if secondary else ("#A63A25" if danger else ACCENT)
        fg = INK if secondary else "#FFFFFF"
        return tk.Button(
            parent, text=text, command=command, bg=bg, fg=fg, activebackground=LINE if secondary else ACCENT_DARK,
            activeforeground=fg, disabledforeground="#9AA39F", relief="flat", bd=0, padx=16, pady=8, font=self.f_bold,
            cursor="hand2", highlightbackground=LINE, highlightthickness=1 if secondary else 0,
        )

    # ---- page building blocks

    def _text(self, parent, text, *, font=None, fg=INK, bg=PAPER, inset=96, pady=(0, 8), padx=0) -> tk.Label:
        label = tk.Label(parent, text=text, font=font or self.f_body, fg=fg, bg=bg, justify="left", anchor="w")
        label.pack(fill="x", pady=pady, padx=padx)
        self._wrapped.append((label, inset))
        label.configure(wraplength=max(self.canvas.winfo_width() - inset, 640))
        return label

    def _title(self, parent, text, subtitle=None) -> None:
        self._text(parent, text, font=self.f_title, pady=(0, 6))
        if subtitle:
            self._text(parent, subtitle, fg=MUTED, pady=(0, 16))

    def _section(self, parent, text) -> None:
        self._text(parent, text, font=self.f_section, pady=(14, 8))

    def _callout(self, parent, tone, text, title=None) -> tk.Frame:
        bg, fg = TONES[tone]
        box = tk.Frame(parent, bg=bg)
        box.pack(fill="x", pady=(4, 12))
        if title:
            self._text(box, title, font=self.f_bold, fg=fg, bg=bg, inset=128, pady=(12, 2), padx=16)
        self._text(box, text, fg=fg, bg=bg, inset=128, pady=(2 if title else 12, 12), padx=16)
        return box

    def _steps(self, parent, items: list[str], start: int = 1) -> None:
        for number, item in enumerate(items, start):
            row = tk.Frame(parent, bg=PAPER)
            row.pack(fill="x", pady=4)
            tk.Label(row, text=str(number), width=2, bg=ACCENT, fg="#FFFFFF", font=self.f_bold).pack(side="left", anchor="n", padx=(0, 12), ipady=2)
            holder = tk.Frame(row, bg=PAPER)
            holder.pack(side="left", fill="x", expand=True)
            self._text(holder, item, inset=138, pady=(1, 0))

    def _bullets(self, parent, items: list[str], bg=PAPER, inset=120) -> None:
        for item in items:
            self._text(parent, f"•  {item}", bg=bg, inset=inset, pady=(0, 3))

    def _status(self, parent) -> tk.Frame:
        """Area showing the outcome of the page's last operation."""
        frame = tk.Frame(parent, bg=PAPER)
        frame.pack(fill="x")
        self._status_frame = frame
        return frame

    def _show_status(self, tone, text, title=None) -> None:
        for child in self._status_frame.winfo_children():
            child.destroy()
        self._callout(self._status_frame, tone, text, title)
        self.update_idletasks()
        self.canvas.configure(scrollregion=self.canvas.bbox("all"))

    def _progress_area(self, parent) -> None:
        self._progress_holder = tk.Frame(parent, bg=PAPER, height=1)
        self._progress_holder.pack(fill="x")
        frame = tk.Frame(self._progress_holder, bg=PAPER)
        self._progress_label = tk.Label(frame, text="", bg=PAPER, fg=MUTED, font=self.f_small, anchor="w")
        self._progress_label.pack(fill="x")
        style = ttk.Style(self)
        style.theme_use("clam")
        style.configure("Green.Horizontal.TProgressbar", troughcolor=LINE, background=ACCENT, bordercolor=LINE, lightcolor=ACCENT, darkcolor=ACCENT)
        self._progress_bar = ttk.Progressbar(frame, style="Green.Horizontal.TProgressbar", mode="determinate", maximum=1000)
        self._progress_bar.pack(fill="x", pady=(3, 0))
        self._progress_frame = frame

    def _key_entry(self, parent) -> tk.Entry:
        row = tk.Frame(parent, bg=PAPER)
        row.pack(fill="x", pady=(2, 6))
        value = tk.StringVar(value=self.key_text)
        entry = tk.Entry(row, textvariable=value, font=self.f_mono, relief="solid", bd=1)
        entry.pack(fill="x", ipady=6)
        counter = tk.Label(row, bg=PAPER, fg=MUTED, font=self.f_small, anchor="w")
        counter.pack(fill="x", pady=(4, 0))

        def update(*_):
            clean = "".join(value.get().split()).replace("-", "")
            self.key_text = value.get()
            counter.configure(text=f"{len(clean)} of 64 characters" + ("  ✓" if len(clean) == 64 else ""))

        value.trace_add("write", update)
        update()
        return entry

    # ---- navigation

    def show(self, page: int) -> None:
        log.info("Page %d: %s", page + 1, PAGES[page])
        self.page = page
        self.reached = max(self.reached, page)
        self._wrapped.clear()
        self._device_label = None
        self._device_refresh = None
        for child in self.body.winfo_children():
            child.destroy()
        content = tk.Frame(self.body, bg=PAPER)
        content.pack(fill="both", expand=True, padx=40, pady=(30, 30))
        getattr(self, f"_page_{page}")(content)
        self.canvas.yview_moveto(0)
        self._update_rail()
        self._update_nav()

    def _update_rail(self) -> None:
        for index, label in enumerate(self.rail_items):
            if index == self.page:
                label.configure(fg=RAIL_ACTIVE, font=self.f_rail_on, text=f"{index + 1}   {PAGES[index]}")
            elif index < self.reached:
                label.configure(fg=RAIL_DONE, font=self.f_rail, text=f"✓   {PAGES[index]}")
            else:
                label.configure(fg=RAIL_TEXT, font=self.f_rail, text=f"{index + 1}   {PAGES[index]}")

    def _can_continue(self) -> tuple[bool, str]:
        s = self.session
        if self.page == P_IPHONE and s.summary is None:
            return False, "Read the iPhone backup first"
        if self.page == P_CONNECT and (self.device_status is None or self.device_status.state != "ok"):
            return False, "Waiting for the phone"
        if self.page == P_WHATSAPP and self.phone_backup is None:
            return False, "Check the backup on the phone first"
        if self.page == P_CONVERT and not s.state.converted:
            return False, "Start the conversion first"
        if self.page == P_COPY and not s.state.copied:
            return False, "Copy the data to the phone first"
        return True, ""

    def _update_nav(self) -> None:
        allowed, hint = self._can_continue()
        last = self.page in (P_RESTORE, P_END)
        self.next_button.configure(state="normal" if allowed and not self.busy else "disabled")
        self.back_button.configure(state="normal" if self.page > P_WELCOME and not self.busy else "disabled")
        if last:
            self.next_button.grid_remove()
        else:
            self.next_button.grid()
        self.nav_hint.configure(text="Operation in progress: do not disconnect the phones" if self.busy else ("" if last else hint))

    def _go_next(self) -> None:
        if self.page < P_END:
            self.show(self.page + 1)

    def _go_back(self) -> None:
        if self.page > P_WELCOME:
            self.show(self.page - 1)

    def _on_close(self) -> None:
        if self.busy and not messagebox.askyesno(
            "Operation in progress",
            "An operation is in progress. If you close now you will have to run it again, but no data will be lost.\n\nClose anyway?",
        ):
            return
        self.destroy()

    # ---- background operations

    def _run(self, work: Callable, on_done: Callable | None = None, buttons: tuple = ()) -> None:
        if self.busy:
            return
        self.busy = True
        self._task_buttons = buttons
        for button in buttons:
            button.configure(state="disabled")
        self._update_nav()
        if getattr(self, "_progress_frame", None) is not None and self._progress_frame.winfo_exists():
            self._progress_frame.pack(fill="x", pady=(6, 6))
            self._set_progress(0, 0, "Starting…")

        def progress(done: int, total: int, message: str) -> None:
            self._events.put(("progress", done, total, message))

        def target() -> None:
            try:
                result = work(progress)
                self._events.put(("done", on_done, result))
            except USER_ERRORS as exc:
                log.warning("Error shown to the user: %s", exc)
                self._events.put(("error", str(exc)))
            except Exception:
                log.error("Unexpected error\n%s", traceback.format_exc())
                self._events.put(("error", f"Unexpected error. The details are in the file transfer.log, in the folder {self.workdir}"))

        threading.Thread(target=target, daemon=True).start()

    def _set_progress(self, done: int, total: int, message: str) -> None:
        if not self._progress_frame.winfo_exists():
            return
        self._progress_label.configure(text=message)
        if total > 0:
            self._progress_bar.stop()
            self._progress_bar.configure(mode="determinate", value=min(done / total, 1) * 1000)
        elif str(self._progress_bar.cget("mode")) != "indeterminate":
            self._progress_bar.configure(mode="indeterminate")
            self._progress_bar.start(18)

    def _drain_events(self) -> None:
        try:
            while True:
                event = self._events.get_nowait()
                if event[0] == "progress":
                    if getattr(self, "_progress_frame", None) is not None:
                        self._set_progress(*event[1:])
                elif event[0] == "device":
                    self._apply_device_status(event[1])
                else:
                    self._finish_task(event)
        except queue.Empty:
            pass
        self.after(80, self._drain_events)

    def _finish_task(self, event) -> None:
        self.busy = False
        for button in self._task_buttons:
            if button.winfo_exists():
                button.configure(state="normal")
        if getattr(self, "_progress_frame", None) is not None and self._progress_frame.winfo_exists():
            self._progress_bar.stop()
            self._progress_frame.pack_forget()
            self._progress_holder.configure(height=1)
        if event[0] == "error":
            self._show_status("error", event[1], "That didn't work")
        elif event[1] is not None:
            event[1](event[2])
        self._update_nav()

    # ---- phone status

    def _ensure_adb(self, progress=None) -> Android:
        if self.device is None:
            adb = android.find_adb(tool_dir()) or android.download_adb(tool_dir(), progress)
            self.device = Android(adb)
        return self.device

    def _poll_device(self) -> None:
        if not self._polling and not self.busy and self.device is not None and self.page >= P_CONNECT:
            self._polling = True

            def check() -> None:
                try:
                    self._events.put(("device", self.device.status()))
                except Exception:
                    log.exception("Phone check failed")
                    self._events.put(("device", None))

            threading.Thread(target=check, daemon=True).start()
        self.after(2000, self._poll_device)

    def _apply_device_status(self, status) -> None:
        self._polling = False
        self.device_status = status
        if self._device_refresh is not None:
            self._device_refresh()
        if not self.busy:
            self._update_nav()

    def _require_device(self) -> Android:
        device = self._ensure_adb()
        status = device.status()
        self.device_status = status
        if status.state != "ok":
            raise AndroidError("The phone can't be seen. Check the cable, unlock the screen and make sure USB debugging is authorized.")
        return device

    # ---- pages

    def _page_0(self, p) -> None:
        self._title(
            p, "Move your WhatsApp chats from iPhone to Android",
            "I'll guide you step by step. The program does the conversion and copies on its own; a few steps on the phones are up to you, and each one is explained.",
        )
        self._callout(
            p, "warn",
            "This program has been through one full transfer with real phones so far, and it worked. Right after the restore, though, WhatsApp took a while to recover: it could not send messages and received ones did not appear in the chat. "
            "If this happens to you, wait, then restart the phone. Do not delete WhatsApp from the iPhone until sending and receiving work.",
            "Still early: read this first",
        )
        self._callout(
            p, "info",
            "If your Android phone is brand new, or you can restore it to factory settings, use WhatsApp's official transfer instead: it is done during the phone's first setup, "
            "by connecting the two phones with a USB-C to Lightning cable and choosing WhatsApp → Settings → Chats → Move chats to Android on the iPhone. "
            "This program is for when that route isn't possible or didn't work.",
            "First of all: have you tried the official method?",
        )
        self._section(p, "What you need")
        self._bullets(p, [
            "The iPhone and its cable, the Android phone and its cable, both charged.",
            "The SIM with your WhatsApp number: you will need to receive an SMS on that number.",
            "Free space on the PC and the phone: at least as much as your WhatsApp photos and videos, you'll be told later on.",
            "An Internet connection on the PC and some time: from 30 minutes to a few hours if you have many videos.",
        ])
        columns = tk.Frame(p, bg=PAPER)
        columns.pack(fill="x", pady=(10, 4))
        columns.columnconfigure((0, 1), weight=1, uniform="c")
        yes = tk.Frame(columns, bg=TONES["ok"][0])
        yes.grid(row=0, column=0, sticky="nsew", padx=(0, 8))
        no = tk.Frame(columns, bg=TONES["warn"][0])
        no.grid(row=0, column=1, sticky="nsew", padx=(8, 0))
        half = 560
        self._text(yes, "What gets transferred", font=self.f_bold, fg=TONES["ok"][1], bg=TONES["ok"][0], inset=half, pady=(12, 6), padx=14)
        for item in ("Individual and group chats, in order and with the right dates", "Texts, links, starred messages, replies",
                     "Photos, videos, audio, voice notes, documents", "GIFs, stickers, contacts and locations"):
            self._text(yes, f"•  {item}", fg=TONES["ok"][1], bg=TONES["ok"][0], inset=half, pady=(0, 3), padx=14)
        tk.Frame(yes, height=8, bg=TONES["ok"][0]).pack()
        self._text(no, "What does not get transferred", font=self.f_bold, fg=TONES["warn"][1], bg=TONES["warn"][0], inset=half, pady=(12, 6), padx=14)
        for item in ("Reactions, polls and view-once photos", "Call history and status updates",
                     "System notices (e.g. “Alex left the group”)", "Wallpapers and per-chat settings"):
            self._text(no, f"•  {item}", fg=TONES["warn"][1], bg=TONES["warn"][0], inset=half, pady=(0, 3), padx=14)
        tk.Frame(no, height=8, bg=TONES["warn"][0]).pack()

        self._section(p, "Good to know")
        self._bullets(p, [
            "This is an unofficial program, not affiliated with WhatsApp. Use it only with your own account and your own phones.",
            "The iPhone is not modified: the program only reads the backup. Do not delete WhatsApp from the iPhone until you have checked that everything is on the Android phone.",
            "What is already in WhatsApp on the Android phone stays: it is merged with the iPhone chats and saved to the PC first. If something goes wrong you can put the phone back as it was.",
            "The chats stay on your PC: they are not sent anywhere. At the end you can delete them with one click.",
            "WhatsApp Business is not supported, nor is a second copy of WhatsApp in a dual-app or work profile.",
        ])
        self._section(p, "Where to work")
        row = tk.Frame(p, bg=PAPER)
        row.pack(fill="x")
        folder = tk.Label(row, text=str(self.workdir), bg=PAPER, fg=MUTED, font=self.f_small, anchor="w")
        folder.pack(side="left", fill="x", expand=True)

        def choose() -> None:
            chosen = filedialog.askdirectory(title="Choose a folder on a disk with plenty of free space")
            if chosen:
                self.workdir = Path(chosen) / "WhatsApp Transfer"
                self.session = self._open_session()
                resume = self._resume_page()
                if resume != P_WELCOME:  # a transfer has already been started in that folder
                    self.reached = max(self.reached, resume)
                    self.show(resume)
                    return
                folder.configure(text=str(self.workdir))
                free.configure(text=self._free_text())

        self._button(row, "Change folder", choose, secondary=True).pack(side="right")
        free = tk.Label(p, text=self._free_text(), bg=PAPER, fg=MUTED, font=self.f_small, anchor="w")
        free.pack(fill="x", pady=(4, 0))

    def _free_text(self) -> str:
        return f"Free space on that disk: {human_size(shutil.disk_usage(self.workdir).free)}"

    def _page_1(self, p) -> None:
        self._title(p, "Back up the iPhone to the PC", "An iCloud backup won't work: you need one saved on this computer.")
        self._steps(p, [
            "On the iPhone open the App Store and update WhatsApp to the latest version.",
            "Turn on airplane mode on the iPhone and from now on stop using WhatsApp there. Messages sent to you wait on the server and will arrive on the Android phone, so you won't lose any.",
            "Connect the iPhone to the PC, unlock it, tap “Trust” and enter the passcode.",
            "Open the “Apple Devices” app or iTunes. If you don't have it, install it from the Microsoft Store with the button below.",
            "Click your iPhone. In the Backups section choose “Back up all of the data on your iPhone to this computer” (in iTunes: “This computer”), then “Back Up Now”. Wait for it to finish: it can take a while.",
        ])
        self._callout(
            p, "info",
            "If “Encrypt local backup” is on, that's not a problem: leave it on, but you must remember the password, you'll be asked for it. If you don't remember it: on the iPhone go to Settings → General → Transfer or Reset iPhone → Reset → Reset All Settings. This doesn't delete your data, but it clears the backup password and your settings.",
            "Password-protected backup",
        )
        tools = tk.Frame(p, bg=PAPER)
        tools.pack(fill="x", pady=(0, 10))
        self._button(tools, "Download Apple Devices", lambda: webbrowser.open(APPLE_DEVICES_URL), secondary=True).pack(side="left")

        self._section(p, "When the backup has finished, choose it here")
        listbox = tk.Listbox(p, height=4, font=self.f_body, relief="solid", bd=1, activestyle="none", exportselection=False,
                             selectbackground=ACCENT, selectforeground="#FFFFFF")
        listbox.pack(fill="x")
        password_row = tk.Frame(p, bg=PAPER)
        tk.Label(password_row, text="Backup password:", bg=PAPER, font=self.f_body).pack(side="left", padx=(0, 8))
        password = tk.StringVar()
        tk.Entry(password_row, textvariable=password, show="•", font=self.f_body, relief="solid", bd=1, width=28).pack(side="left", ipady=3)

        def selected() -> ios_backup.BackupInfo | None:
            picked = listbox.curselection()
            # With no backups the list's only row is the “No backup found” notice.
            return self.backups[picked[0]] if picked and picked[0] < len(self.backups) else None

        def on_select(_e=None) -> None:
            info = selected()
            if info and info.encrypted:
                password_row.pack(fill="x", pady=(8, 0), after=listbox)
            else:
                password_row.pack_forget()

        listbox.bind("<<ListboxSelect>>", on_select)

        def refresh(extra: Path | None = None) -> None:
            self.backups = ios_backup.find_backups(extra)
            listbox.delete(0, "end")
            for backup in self.backups:
                listbox.insert("end", f"  {backup}")
            if self.backups:
                listbox.selection_set(0)
                on_select()
            else:
                listbox.insert("end", "  No backup found: when it has finished click “Refresh the list”.")

        actions = tk.Frame(p, bg=PAPER)
        actions.pack(fill="x", pady=(10, 4))

        def read() -> None:
            info = selected()
            if info is None:
                self._show_status("warn", "Choose a backup from the list first.")
                return
            word = password.get() or None
            self._run(lambda progress: self.session.read_iphone(info, word, progress), lambda summary: done(info, summary),
                      buttons=(read_button,))

        def done(info, summary: converter.IosSummary) -> None:
            first = summary.first.strftime("%Y-%m-%d") if summary.first else "?"
            last = summary.last.strftime("%Y-%m-%d %H:%M") if summary.last else "?"
            text = (
                f"{counted(summary.chats, 'chat', 'chats')} and {counted(summary.messages, 'message', 'messages')}, from {first} to {last}.\n"
                f"{counted(summary.media_files, 'file', 'files')} to copy ({human_size(summary.media_bytes)})."
            )
            if summary.media_missing:
                text += (
                    "\n" + counted(summary.media_missing, "file is not in the backup", "files are not in the backup")
                    + ": photos or videos never downloaded on the iPhone. The message will be there, the file won't."
                )
            if info.date and (datetime.now() - info.date).days >= 1:
                self._show_status("warn", text + f"\n\nWarning: this backup is from {info.date:%Y-%m-%d}. Later messages are not in it: you should make it again.", "Backup read, but it isn't recent")
            else:
                self._show_status("ok", text, "Backup read correctly")

        read_button = self._button(actions, "Read this backup", read)
        read_button.pack(side="left")

        def other_folder() -> None:
            chosen = filedialog.askdirectory(title="Choose the backup folder")
            if chosen:
                refresh(Path(chosen))

        self._button(actions, "Refresh the list", refresh, secondary=True).pack(side="left", padx=8)
        self._button(actions, "Look in another folder", other_folder, secondary=True).pack(side="left")
        self._progress_area(p)
        self._status(p)
        refresh()

    def _page_2(self, p) -> None:
        self._title(p, "Connect the Android phone", "To copy files the PC must be able to talk to the phone. This needs a hidden option to be turned on once.")
        self._steps(p, [
            "On the phone find “Build number” in Settings → About phone (use the Settings search if you can't see it) and tap it 7 times in a row. Enter your PIN if asked. The list below shows where brands put it.",
            "Open Developer options (usually in Settings → System, or use the Settings search) and turn on “USB debugging”.",
            "Connect the phone to the PC with the cable. If a USB notification appears, choose “File transfer”.",
            "The phone shows “Allow USB debugging?”: tick “Always allow from this computer” and tap “Allow”.",
        ])
        self._section(p, "Where to find it, by brand")
        self._bullets(p, [
            "Samsung: Settings → About phone → Software information → Build number.",
            "Xiaomi, Redmi, POCO: Settings → About phone → tap “OS version” (or “MIUI version”). Developer options are in Additional settings. Also turn on “USB debugging (Security settings)”.",
            "OPPO, realme, OnePlus: Settings → About device → Version → Build number. If the copy is refused later, turn on “Disable permission monitoring” in Developer options.",
            "vivo: Settings → All specs (or About phone) → Software version.",
            "Huawei, Honor: Settings → About phone → Build number.",
            "Google Pixel, Nothing, Motorola and others: Settings → About phone → Build number.",
        ])
        self._text(p, "Names change with brand, Android version and language: if a label doesn't match exactly, look for the closest one.", fg=MUTED, font=self.f_small, pady=(6, 0))
        self._section(p, "Connection status")
        box = tk.Frame(p, bg=PAPER)
        box.pack(fill="x")
        self._status(p)
        self._progress_area(p)

        def refresh() -> None:
            for child in box.winfo_children():
                child.destroy()
            status = self.device_status
            if self.device is None:
                self._callout(box, "info", "Preparing the Android tools…")
            elif status is None or status.state == "none":
                self._callout(
                    box, "warn",
                    "No phone can be seen yet. Check that USB debugging is on, try another USB port or another cable (some cables are for charging only).",
                    "Waiting for the phone…",
                )
            elif status.state == "unauthorized":
                self._callout(box, "warn", "Unlock the phone and tap “Allow” in the USB debugging window. If it doesn't appear, unplug and replug the cable.", "Authorization missing")
            elif status.state == "offline":
                self._callout(box, "warn", "The phone is connected but not responding. Unplug and replug the cable.", "Phone not reachable")
            elif status.state == "many":
                self._callout(box, "warn", "More than one Android phone can be seen. Leave only the destination phone connected.", "Too many phones connected")
            else:
                self._callout(box, "ok", f"Phone connected: {status.model or status.serial}. You can go on.", "All good")
            self.canvas.configure(scrollregion=self.canvas.bbox("all"))

        self._device_refresh = refresh
        refresh()
        self._text(p, "If Windows doesn't recognize the phone: open Device Manager; if you see an entry with a yellow triangle, you need your brand's USB driver.", fg=MUTED, font=self.f_small)
        self._button(p, "Find your brand's USB driver", lambda: webbrowser.open(USB_DRIVER_URL), secondary=True).pack(anchor="w")
        if self.device is None:
            def ready(_):
                if box.winfo_exists():  # the page may have changed in the meantime
                    refresh()
            self._run(self._ensure_adb, ready)

    def _page_3(self, p) -> None:
        self._title(p, "Prepare WhatsApp on the Android phone", "Here we take a real backup from the phone: it's the base the iPhone chats will be added to.")
        self._callout(
            p, "warn",
            "When you register the number on the Android phone, WhatsApp on the iPhone gets logged out. That's normal: the chats are already safe in the backup you've just read.",
            "Before you start",
        )
        tools = tk.Frame(p, bg=PAPER)
        tools.pack(fill="x", pady=(0, 8))

        def on_phone(action):
            self._run(lambda _p: action(self._require_device()), None)

        self._button(tools, "Open the Play Store on the phone", lambda: on_phone(lambda d: d.open_store_page()), secondary=True).pack(side="left")
        self._button(tools, "Open WhatsApp on the phone", lambda: on_phone(lambda d: d.open_whatsapp()), secondary=True).pack(side="left", padx=8)
        self._steps(p, [
            "Install WhatsApp from the Play Store or update it. If you already use it on this phone with the same number, go to point 3.",
            "Put the SIM in the phone, open WhatsApp and register the same number as on the iPhone. When it offers to restore a backup, choose “Skip”.",
            "Do a test: open the chat with yourself (search for your name in the contacts), send a photo and then reply to that photo with a text. The program needs this to learn how this WhatsApp version stores photos and replies.",
            "Go to Settings → Chats → Chat backup → End-to-end encrypted backup → Turn on. Choose “Use 64-digit encryption key instead” and generate the key. (If the encrypted backup was already on with a password, turn it off and back on this way.)",
            "WhatsApp shows the key: a grid of 16 groups of 4 characters. Type it in the box below and keep it: you'll need it again at the end. Then confirm on the phone with “I saved my 64-digit key” and complete the activation.",
            "Go back to Chat backup. If there is “Back up to Google Account”, set it to “Never”. Then tap “Back up” and wait for it to finish.",
            "Turn on airplane mode on the Android phone and leave it connected: until the end new messages wait on the server, so you won't lose any.",
        ])
        self._text(p, "WhatsApp's labels vary with version and language: if you can't find an exact label, look for the closest one.", fg=MUTED, font=self.f_small, pady=(8, 0))
        self._section(p, "The 64-digit key")
        self._text(p, "Type it as you see it: spaces and capitals don't matter. It stays in memory only and is never saved to disk.", fg=MUTED, font=self.f_small)
        self._key_entry(p)
        actions = tk.Frame(p, bg=PAPER)
        actions.pack(fill="x", pady=(4, 4))

        def done(backup: session.PhoneBackup) -> None:
            self.phone_backup = backup
            minutes = backup.age_seconds // 60
            version = backup.whatsapp_version or "?"
            summary = f"WhatsApp {version}. Backup of {human_size(backup.size)}, made {minutes} minutes ago."
            if session.backup_is_stale(backup):
                self._show_status("warn", summary + " It's quite old: if you've received messages on this phone in the meantime, make the backup again and recheck.", "The key is right, but the backup isn't recent")
            else:
                self._show_status("ok", summary + " The key is the right one.", "Backup found and opened")

        def check() -> None:
            self.phone_backup = None
            self._update_nav()
            self._run(
                lambda progress: self.session.read_phone_backup(self._require_device(), self.key_text, progress),
                done, buttons=(check_button,),
            )

        check_button = self._button(actions, "Check backup and key", check)
        check_button.pack(side="left")
        self._progress_area(p)
        self._status(p)

    def _page_4(self, p) -> None:
        self._title(p, "Convert the chats", "The program turns the chats from iPhone format into Android format. You can leave it working: the phones aren't touched.")
        s = self.session
        ready = s.summary is not None and s.root_key is not None
        if not ready:
            if s.can_resume():
                self._resume_form(p)
            elif not s.state.converted:
                self._callout(p, "warn", "The iPhone backup or the key is missing (this happens if the program was closed). Go back to steps 2 and 4 and read them again: a few clicks are enough, you don't have to redo the backups.", "One step back needed")
                return
        if s.summary is not None:
            self._text(p, f"To convert: {s.summary.messages:,} messages and {s.summary.media_files:,} files ({human_size(s.summary.media_bytes)}).", font=self.f_bold)

        def start() -> None:
            self._run(s.convert, lambda _r: self._show_report(result_box), buttons=(go,))

        go = self._button(p, "Start the conversion" if not s.state.converted else "Convert again", start)
        go.pack(anchor="w", pady=(6, 6))
        if not ready:
            go.configure(state="disabled")
        self._progress_area(p)
        self._status(p)
        result_box = tk.Frame(p, bg=PAPER)
        result_box.pack(fill="x")
        if s.state.converted:
            self._show_report(result_box)

    def _resume_form(self, p) -> None:
        s = self.session
        self._callout(
            p, "info",
            "The iPhone backup and the one made by the Android phone at step 4 are already on this PC: you don't have to redo them and the phones aren't touched. "
            "Enter the 64-digit key and the program reads them again on its own.",
            "Resume from the backups already made",
        )
        self._key_entry(p)
        password = tk.StringVar()
        info = s.saved_iphone_backup()
        if info is not None and info.encrypted:
            row = tk.Frame(p, bg=PAPER)
            row.pack(fill="x", pady=(0, 6))
            tk.Label(row, text="iPhone backup password:", bg=PAPER, font=self.f_body).pack(side="left", padx=(0, 8))
            tk.Entry(row, textvariable=password, show="•", font=self.f_body, relief="solid", bd=1, width=28).pack(side="left", ipady=3)

        def resume() -> None:
            word = password.get() or None
            self._run(lambda progress: s.resume_after_backups(self.key_text, word, progress), lambda _r: self.show(P_CONVERT), buttons=(button,))

        button = self._button(p, "Read the backups again", resume)
        button.pack(anchor="w", pady=(0, 12))

    def _show_report(self, box: tk.Frame) -> None:
        data = self.session.saved_report()
        for child in box.winfo_children():
            child.destroy()
        if data is None:
            return
        self._callout(box, "ok", f"The Android backup is ready: {sum(data['inserted'].values()):,} messages in {data['chats']} chats.", "Conversion completed")
        table = tk.Frame(box, bg=CARD, highlightbackground=LINE, highlightthickness=1)
        table.pack(fill="x", pady=(0, 10))
        table.columnconfigure(0, weight=1)
        rows = sorted(data["inserted"].items(), key=lambda item: -item[1])
        rows.append(("Files copied", data["media_copied"]))
        rows.append(("Size of the files", human_size(data["media_bytes"])))
        if data["quotes"]:
            rows.append(("Replies linked to the original message", data["quotes"]))
        for index, (name, value) in enumerate(rows):
            tk.Label(table, text=name, bg=CARD, fg=INK, font=self.f_body, anchor="w").grid(row=index, column=0, sticky="ew", padx=14, pady=4)
            shown = f"{value:,}" if isinstance(value, int) else value
            tk.Label(table, text=shown, bg=CARD, fg=INK, font=self.f_bold, anchor="e").grid(row=index, column=1, sticky="e", padx=14)
        notes = []
        if data["media_missing"]:
            notes.append(counted(data["media_missing"], "file was not in the iPhone backup", "files were not in the iPhone backup") + ": the message is there, the file isn't.")
        if data["skipped_system"]:
            notes.append(counted(data["skipped_system"], "system notice not transferred.", "system notices not transferred."))
        if data["skipped_deleted"]:
            notes.append(counted(data["skipped_deleted"], "deleted message not transferred (it had no content).", "deleted messages not transferred (they had no content)."))
        if data["skipped_other"]:
            notes.append(counted(data["skipped_other"], "item with no content (calls, reactions and the like) not transferred.", "items with no content (calls, reactions and the like) not transferred."))
        if data["duplicates"]:
            notes.append(counted(data["duplicates"], "message was already on the phone and was not duplicated.", "messages were already on the phone and were not duplicated."))
        if notes:
            self._bullets(box, notes)

    def _page_5(self, p) -> None:
        self._title(p, "Copy everything to the phone", "This is the most delicate step, but it's all reversible: what's on the phone now is saved to the PC first.")
        self._section(p, "What the program will do")
        self._steps(p, [
            "Save to the PC the WhatsApp files already on the phone.",
            "Reset WhatsApp on the phone, as if freshly installed: it's the only way to make it offer the restore. The app and your gallery photos stay.",
            "Put the saved files back, add those from the iPhone and finally copy the new chat backup, checking that it's identical.",
        ])
        self._callout(
            p, "warn",
            "Keep the phone connected and don't close the program. If it gets interrupted that's not a problem: reopen the program and click “Start the copy” again, it resumes from the missing files. "
            "Until you do the restore, the phone must stay in airplane mode.",
            "During the copy",
        )
        understood = tk.BooleanVar(value=self.session.state.copied)
        check = tk.Checkbutton(
            p, text="I understand that WhatsApp on the phone will be reset and that I'll have to restore it", variable=understood, bg=PAPER,
            font=self.f_body, activebackground=PAPER, anchor="w", command=lambda: go.configure(state="normal" if understood.get() else "disabled"),
        )
        check.pack(fill="x", pady=(4, 6))

        def done(_r) -> None:
            self._show_status("ok", "The backup and all the files are on the phone and have been verified. Go on to the restore in WhatsApp.", "Copy completed")

        def start() -> None:
            if not messagebox.askyesno("Confirm", "WhatsApp on the phone will now be reset.\nThe files already there are saved to the PC first.\n\nProceed?"):
                return
            self._run(lambda progress: self.session.apply_to_phone(self._require_device(), progress), done, buttons=(go,))

        go = self._button(p, "Start the copy", start)
        go.configure(state="normal" if understood.get() else "disabled")
        go.pack(anchor="w", pady=(0, 6))
        self._progress_area(p)
        self._status(p)
        if self.session.state.copied:
            done(None)
        elif self.session.state.phone_changed:
            self._show_status("warn", "An earlier copy didn't reach the end: WhatsApp on the phone has already been reset and the files are safe on the PC. Click “Start the copy” to start again.", "Copy to complete")

    def _page_6(self, p) -> None:
        self._title(p, "Restore the chats in WhatsApp", "The last step, all on the phone. Now WhatsApp will find the backup and import it.")
        self._button(p, "Open WhatsApp on the phone", lambda: self._run(lambda _p: self._require_device().open_whatsapp()), secondary=True).pack(anchor="w", pady=(0, 10))
        self._steps(p, [
            "Turn off airplane mode on the phone and connect to Wi-Fi. Don't open other apps yet.",
            "Open WhatsApp, accept the terms and enter the same number. Confirm with the code received by SMS (and your two-step verification PIN if you have one).",
            "When it asks for access to contacts and files, tap “Allow”.",
            "On the “Restore backup” screen tap “Restore”. It should show a backup from today.",
            "Enter the 64-digit key, the one below, and confirm.",
            "Wait for the restore to finish, without closing the app. With many chats it takes a few minutes. Then enter your name.",
            "Check a few old chats: texts, photos, voice notes and groups. The first opening can be slow: WhatsApp is arranging the data.",
            "If you can't send yet, or new messages don't appear in the chat, give WhatsApp some time, then restart the phone.",
        ])
        self._section(p, "Your key")
        if self.key_text.strip():
            clean = "".join(self.key_text.split()).replace("-", "").lower()
            grid = tk.Frame(p, bg=TONES["ok"][0])
            grid.pack(anchor="w", pady=(0, 8))
            for index in range(16):
                tk.Label(grid, text=clean[index * 4 : index * 4 + 4], bg=TONES["ok"][0], fg=INK, font=self.f_mono, padx=12, pady=5).grid(row=index // 4, column=index % 4)
        else:
            self._text(p, "Use the 64-digit key you wrote down earlier.", fg=MUTED)
        self._section(p, "How did it go?")
        actions = tk.Frame(p, bg=PAPER)
        actions.pack(fill="x", pady=(0, 8))

        def finish(trouble: bool) -> None:
            self.trouble = trouble
            self.show(P_END)

        self._button(actions, "The chats are there", lambda: finish(False)).pack(side="left")
        self._button(actions, "Something's wrong", lambda: finish(True), secondary=True).pack(side="left", padx=8)
        self._progress_area(p)
        self._status(p)

    def _page_7(self, p) -> None:
        if self.trouble:
            self._page_trouble(p)
        else:
            self._page_success(p)

    def _page_success(self, p) -> None:
        self._title(p, "Done!", "The iPhone chats are now on the Android phone.")
        self._section(p, "To finish")
        self._steps(p, [
            "Send and receive a few messages, and calmly check your most important chats, before deleting anything from the iPhone.",
            "If an old photo won't open in WhatsApp, check whether it's in the Gallery; if so, you can still find it there.",
            "Turn off “USB debugging” in Developer options: it's no longer needed.",
            "Keep the 64-digit key or, in WhatsApp, turn off the encrypted backup and turn it back on as you prefer.",
        ])
        self._section(p, "Delete the copies from the PC")
        self._text(p, f"In the folder {self.workdir} there are unencrypted copies of your chats and files. When you're sure, delete them. The iPhone backup isn't touched: you can remove it from Apple Devices → Manage Backups.")
        self._status(p)

        def clean() -> None:
            if messagebox.askyesno("Delete the copies", "Afterwards you won't be able to put the phone back as it was through this program.\n\nDelete?"):
                self.session.clean()
                self._show_status("ok", "Copies deleted from the PC. You can close the program.")

        self._button(p, "Delete the copies from the PC", clean, danger=True).pack(anchor="w", pady=(4, 8))
        self._button(p, "Actually something's wrong", lambda: self._to_trouble(), secondary=True).pack(anchor="w")

    def _to_trouble(self) -> None:
        self.trouble = True
        self.show(P_END)

    def _page_trouble(self, p) -> None:
        self._title(p, "Let's fix it", "Nothing is lost: the iPhone is intact and the phone can be put back as it was.")
        problems = [
            ("The chats are there, but you can't send, or received messages don't appear in the chat",
             "WhatsApp can take a while to recover after the restore. Don't run the copy again: leave the phone on Wi-Fi with WhatsApp open for a while, then restart the phone. If it still doesn't work, send a test message, then in WhatsApp tap Settings → Chats → Chat backup → Back up. Connect the phone, type the key below and use “Save the error report”. It only contains technical counts, no messages."),
            ("WhatsApp doesn't offer the restore, or says there's no backup",
             "The phone may have run a check before the files were readable. Use “Run the copy again” below, then retry the registration. Make sure you're using the same number."),
            ("It offers a Google backup, not today's one",
             "Open the link below on the PC with the same Google account, delete today's WhatsApp backup and use “Run the copy again”. If there was already an old backup you want to keep, you can refuse access to the Google account on WhatsApp's screen."),
            ("It rejects the key",
             "Check the digits: they are only 0-9 and a-f. If you're sure they're right, use “Put the phone back as it was” and redo from step 4."),
            ("WhatsApp closes on its own or stays stuck",
             "Save the error report below: it only contains the technical error, no messages. Then put the phone back as it was."),
            ("Photos are missing, or won't open",
             "Check the free space on the phone and use “Run the copy again”. Files reported as missing during the conversion weren't in the iPhone backup."),
            ("Searches don't find old messages",
             "WhatsApp rebuilds the index on its own over time: leave it on and charging. The chats are all still there anyway."),
        ]
        for title, advice in problems:
            box = tk.Frame(p, bg=CARD, highlightbackground=LINE, highlightthickness=1)
            box.pack(fill="x", pady=4)
            self._text(box, title, font=self.f_bold, bg=CARD, inset=128, pady=(10, 2), padx=14)
            self._text(box, advice, bg=CARD, fg=MUTED, inset=128, pady=(0, 10), padx=14)

        self._section(p, "The 64-digit key")
        self._text(p, "The error report needs it to read the phone's latest backup. It stays in memory only.", fg=MUTED, font=self.f_small)
        self._key_entry(p)
        self._section(p, "Actions")
        actions = tk.Frame(p, bg=PAPER)
        actions.pack(fill="x", pady=(0, 8))

        def recopy() -> None:
            if messagebox.askyesno("Run the copy again", "WhatsApp on the phone will be reset again and will receive the backup.\n\nProceed?"):
                self._run(lambda progress: self.session.apply_to_phone(self._require_device(), progress),
                          lambda _r: self._show_status("ok", "Copy redone. Retry the restore from point 1 of the previous step.", "Done"),
                          buttons=(recopy_button, undo_button, log_button))

        def undo() -> None:
            if messagebox.askyesno("Put it back as it was", "WhatsApp on the phone will go back to the backup made at step 4, with its files. You'll have to register the number again and restore that backup with the same key.\n\nProceed?"):
                self._run(lambda progress: self.session.undo_on_phone(self._require_device(), progress),
                          lambda _r: self._show_status("ok", "The phone is as it was. Open WhatsApp, register the number and restore with the same key. To go back to the iPhone: turn off airplane mode, open WhatsApp and register the number again there.", "Done"),
                          buttons=(recopy_button, undo_button, log_button))

        def save_log() -> None:
            def work(_progress):
                try:
                    device = self._require_device()
                except AndroidError:
                    device = None  # the report is also, and above all, needed when the phone doesn't respond
                target = self.workdir / f"error_report_{datetime.now():%Y%m%d_%H%M%S}.txt"
                target.write_text(_diagnostic(device, self.session, self.key_text), "utf-8")
                return target

            self._run(work, lambda target: self._show_status("info", f"Report saved in:\n{target}", "Error report"),
                      buttons=(recopy_button, undo_button, log_button))

        recopy_button = self._button(actions, "Run the copy again", recopy)
        recopy_button.pack(side="left")
        undo_button = self._button(actions, "Put the phone back as it was", undo, secondary=True)
        undo_button.pack(side="left", padx=8)
        log_button = self._button(actions, "Save the error report", save_log, secondary=True)
        log_button.pack(side="left")
        self._button(p, "Open the backups on Google Drive", lambda: webbrowser.open(GOOGLE_BACKUPS_URL), secondary=True).pack(anchor="w", pady=(0, 8))
        self._button(p, "Redo conversion and copy without redoing the backups", lambda: self.show(P_CONVERT), secondary=True).pack(anchor="w", pady=(0, 8))
        self._progress_area(p)
        self._status(p)
        self._button(p, "It works now", lambda: self._to_success(), secondary=True).pack(anchor="w", pady=(10, 0))

    def _to_success(self) -> None:
        self.trouble = False
        self.show(P_END)


def _diagnostic(device: Android | None, s: Session, key_text: str = "") -> str:
    """Technical information to understand an error: no messages, numbers or keys."""
    lines = [f"Program version: {__version__}", f"System: {platform.platform()}"]
    lines.append(f"State: {s.state.iphone_read=} {s.state.converted=} {s.state.copied=} {s.state.phone_changed=}")
    if device is None:
        lines.append("Phone: not connected")
    else:
        lines.append(f"Phone: {device.shell('getprop ro.product.manufacturer', check=False)} {device.status().model}")
        lines.append(f"Android: {device.shell('getprop ro.build.version.release', check=False)} (SDK {device.sdk_level()})")
        lines.append(f"WhatsApp: {device.whatsapp_version()}")
        root = s.phone_root(device)
        lines.append(f"WhatsApp folder: {root}")
        lines.append(f"Backup on the phone: {device.remote_file(android.backup_file(root))}")
    if s.output_backup.is_file():
        lines.append(f"Backup produced: {s.output_backup.stat().st_size} bytes")
    report = s.saved_report()
    if report:
        lines.append(f"Summary: {report}")
    if s.output_db.is_file():
        try:
            db = sqlite3.connect(s.output_db)
            lines.append("message columns: " + ",".join(r[1] for r in db.execute("PRAGMA table_info(message)")))
            lines.append("message_media columns: " + ",".join(r[1] for r in db.execute("PRAGMA table_info(message_media)")))
            lines.append(f"Message types: {db.execute('SELECT message_type, lookup_tables, count(*) FROM message GROUP BY 1,2').fetchall()}")
            db.close()
        except sqlite3.Error as exc:
            lines.append(f"Database: {exc}")
    for title, database in (("Database taken from the phone", s.template_db), ("Database produced by the program", s.output_db)):
        if database.is_file():
            lines.append(f"\n{title}:")
            lines.extend(diagnosis.describe(database))
    if device is not None:
        lines.append("\nThe phone's latest backup:")
        lines.extend(s.inspect_phone_backup(device, key_text))
        lines.append("\nWhatsApp database errors on the phone:")
        lines.append(device.database_errors() or "(none)")
        lines.append("\nWhatsApp errors on the phone:")
        lines.append(device.crash_log() or "(none)")
    lines.append("\nLast lines of the program's log:")
    try:
        lines.extend((s.workdir / "transfer.log").read_text("utf-8", "replace").splitlines()[-LOG_LINES_IN_REPORT:])
    except OSError as exc:
        lines.append(f"(log not readable: {exc})")
    return "\n".join(lines)


def main(workdir: Path | None = None, start_page: int | None = None) -> None:
    try:
        import ctypes

        ctypes.windll.shcore.SetProcessDpiAwareness(1)
    except (AttributeError, OSError, ImportError):
        pass
    try:
        App(workdir, start_page).mainloop()
    except Exception:
        # This can happen before the log exists in the work folder (e.g. folder not writable).
        fallback = Path(tempfile.gettempdir()) / "WhatsAppTransfer_error.txt"
        fallback.write_text(traceback.format_exc(), "utf-8")
        messagebox.showerror("Startup error", f"The program could not start.\n\nThe details are in the file:\n{fallback}")
