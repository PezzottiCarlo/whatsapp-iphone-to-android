"""Commands sent to the Android phone through adb."""

from __future__ import annotations

import hashlib
import io
import os
import re
import shlex
import shutil
import subprocess
import sys
import time
import urllib.request
import zipfile
from dataclasses import dataclass
from pathlib import Path
from typing import Callable

PACKAGE = "com.whatsapp"
WA_ROOT = f"/sdcard/Android/media/{PACKAGE}/WhatsApp"
LEGACY_ROOT = "/sdcard/WhatsApp"
SCOPED_STORAGE_SDK = 30  # Android 11
PLATFORM_TOOLS_URL = "https://dl.google.com/android/repository/platform-tools-latest-windows.zip"
ADB_FILES = ("adb.exe", "AdbWinApi.dll", "AdbWinUsbApi.dll")
PUSH_ATTEMPTS = 4
PUSH_RETRY_SECONDS = 2
DATABASE_LOG_TAGS = re.compile(r"SQLite|DatabaseUtils|CursorWindow|constraint failed", re.I)
DATABASE_LOG_LINES = 200
OEM_RESTRICTIONS_HINT = (
    "Some brands add a second switch in Developer options: on Xiaomi, Redmi and POCO turn on "
    "“USB debugging (Security settings)”, on OPPO, realme and OnePlus turn on “Disable permission monitoring”."
)

Progress = Callable[[int, int, str], None]
_NO_WINDOW = subprocess.CREATE_NO_WINDOW if sys.platform == "win32" else 0


class AndroidError(Exception):
    """A message for the user about a problem with the phone."""


@dataclass
class DeviceStatus:
    state: str  # "none" | "unauthorized" | "offline" | "ok" | "many"
    serial: str | None = None
    model: str | None = None


@dataclass
class RemoteFile:
    size: int
    modified: int


def backup_file(root: str) -> str:
    return f"{root}/Databases/msgstore.db.crypt15"


def media_dir(root: str) -> str:
    return f"{root}/Media"


def find_adb(cache_dir: Path) -> Path | None:
    cached = cache_dir / "adb.exe"
    if cached.is_file():
        return cached
    found = shutil.which("adb")
    return Path(found) if found else None


def download_adb(cache_dir: Path, progress: Progress | None = None) -> Path:
    """Downloads Google's official Android tools and extracts only adb."""
    notify = progress or (lambda *_: None)
    cache_dir.mkdir(parents=True, exist_ok=True)
    notify(0, 0, "Downloading the Android tools from Google…")
    try:
        with urllib.request.urlopen(PLATFORM_TOOLS_URL, timeout=60) as response:
            total = int(response.headers.get("Content-Length") or 0)
            buffer = io.BytesIO()
            while chunk := response.read(256 * 1024):
                buffer.write(chunk)
                notify(buffer.tell(), total, "Downloading the Android tools from Google…")
    except OSError as exc:
        raise AndroidError(f"Could not download the Android tools: {exc}. Check the PC's connection and try again.") from exc
    with zipfile.ZipFile(buffer) as archive:
        for name in ADB_FILES:
            (cache_dir / name).write_bytes(archive.read(f"platform-tools/{name}"))
    return cache_dir / "adb.exe"


class Android:
    def __init__(self, adb: Path):
        self.adb = adb
        self.serial: str | None = None

    def _run(self, *args: str, timeout: float | None = 60, check: bool = True, serial: bool = True) -> str:
        cmd = [str(self.adb)]
        if serial and self.serial:
            cmd += ["-s", self.serial]
        cmd += list(args)
        try:
            done = subprocess.run(
                cmd, capture_output=True, timeout=timeout, creationflags=_NO_WINDOW, stdin=subprocess.DEVNULL
            )
        except subprocess.TimeoutExpired as exc:
            raise AndroidError("The phone is not responding. Check the cable and that the screen is unlocked, then try again.") from exc
        output = (done.stdout + done.stderr).decode("utf-8", "replace").strip()
        if check and done.returncode != 0:
            raise AndroidError(f"Command failed on the phone ({args[0]}): {output or done.returncode}")
        return output

    def _push(self, *args: str) -> None:
        # Right after an app is installed, Android may briefly refuse to create files in
        # its folder (seen on Android 16): trying again is enough.
        for attempt in range(PUSH_ATTEMPTS):
            try:
                self._run("push", *args, timeout=None)
                return
            except AndroidError as exc:
                if attempt == PUSH_ATTEMPTS - 1 or "Operation not permitted" not in str(exc):
                    raise
                time.sleep(PUSH_RETRY_SECONDS)

    def shell(self, command: str, *, timeout: float | None = 60, check: bool = True) -> str:
        return self._run("shell", command, timeout=timeout, check=check)

    def status(self) -> DeviceStatus:
        output = self._run("devices", "-l", timeout=20, check=False, serial=False)
        # On first start adb prints "* daemon ... *" messages before and after the header.
        lines = output.splitlines()
        after_header = lines[next((i + 1 for i, line in enumerate(lines) if line.startswith("List of devices")), len(lines)) :]
        devices = [line.split() for line in after_header if line.strip() and not line.startswith("*")]
        if not devices:
            self.serial = None
            return DeviceStatus("none")
        if len(devices) > 1:
            self.serial = None
            return DeviceStatus("many")
        parts = devices[0]
        serial, state = parts[0], parts[1] if len(parts) > 1 else "offline"
        if state == "unauthorized":
            return DeviceStatus("unauthorized", serial)
        if state != "device":
            return DeviceStatus("offline", serial)
        self.serial = serial
        model = next((p.split(":", 1)[1].replace("_", " ") for p in parts if p.startswith("model:")), None)
        return DeviceStatus("ok", serial, model)

    def sdk_level(self) -> int | None:
        output = self.shell("getprop ro.build.version.sdk", check=False).strip()
        return int(output) if output.isdigit() else None

    def whatsapp_root(self) -> str:
        """The folder holding WhatsApp's backups and media on this phone.

        Android 11 and later always use Android/media. Older versions may still use
        /sdcard/WhatsApp, so there the most recent backup decides.
        """
        sdk = self.sdk_level()
        if sdk is None or sdk >= SCOPED_STORAGE_SDK:
            return WA_ROOT
        backups = [(found.modified, root) for root in (WA_ROOT, LEGACY_ROOT) if (found := self.remote_file(backup_file(root)))]
        return max(backups)[1] if backups else LEGACY_ROOT

    def whatsapp_version(self) -> str | None:
        """The installed WhatsApp version, or None if it is not installed."""
        if PACKAGE not in self.shell(f"pm list packages {PACKAGE}", check=False).split("package:")[-1].split():
            return None
        info = self.shell(f"dumpsys package {PACKAGE}", timeout=30, check=False)
        match = re.search(r"versionName=(\S+)", info)
        return match.group(1) if match else "?"

    def open_store_page(self) -> None:
        self.shell(f"am start -a android.intent.action.VIEW -d market://details?id={PACKAGE}", check=False)

    def open_whatsapp(self) -> None:
        self.shell(f"monkey -p {PACKAGE} -c android.intent.category.LAUNCHER 1", check=False)

    def stop_whatsapp(self) -> None:
        self.shell(f"am force-stop {PACKAGE}", check=False)

    def remote_file(self, path: str) -> RemoteFile | None:
        output = self.shell(f"stat -c '%s %Y' {shlex.quote(path)} 2>/dev/null", check=False)
        match = re.fullmatch(r"(\d+) (\d+)", output.strip())
        return RemoteFile(int(match.group(1)), int(match.group(2))) if match else None

    def now(self) -> int:
        return int(self.shell("date +%s"))

    def free_bytes(self) -> int:
        # The trailing slash follows the symlink; counting from the end survives wrapped lines.
        lines = self.shell("df -k /sdcard/").splitlines()
        return int(lines[-1].split()[-3]) * 1024

    def tree_bytes(self, path: str) -> int:
        output = self.shell(f"du -sk {shlex.quote(path)} 2>/dev/null", timeout=180, check=False)
        match = re.match(r"(\d+)", output)
        return int(match.group(1)) * 1024 if match else 0

    def pull(self, remote: str, local: Path, timeout: float | None = None) -> None:
        local.parent.mkdir(parents=True, exist_ok=True)
        self._run("pull", remote, str(local), timeout=timeout)

    def _same_on_phone(self, remote: str, data: bytes) -> bool:
        # Older Android versions may not have sha256sum, and a few have neither tool.
        for tool, algorithm in (("sha256sum", hashlib.sha256), ("md5sum", hashlib.md5)):
            output = self.shell(f"{tool} {shlex.quote(remote)} 2>/dev/null", timeout=300, check=False).split()
            if output and re.fullmatch(r"[0-9a-f]{32}|[0-9a-f]{64}", output[0]):
                return output[0] == algorithm(data).hexdigest()
        found = self.remote_file(remote)
        return found is not None and found.size == len(data)

    def push_verified(self, local: Path, remote: str) -> None:
        """Copies a file atomically and verifies its digest on the phone."""
        temp = remote + ".partial"
        self.shell(f"mkdir -p {shlex.quote(os.path.dirname(remote))}")
        self._push(str(local), temp)
        if not self._same_on_phone(temp, local.read_bytes()):
            self.shell(f"rm -f {shlex.quote(temp)}", check=False)
            raise AndroidError("The file copied to the phone is not identical to the original: check the cable and try again.")
        self.shell(f"mv -f {shlex.quote(temp)} {shlex.quote(remote)}")

    def push_tree(self, local_root: Path, remote_root: str, progress: Progress | None = None) -> None:
        """Copies a folder in blocks, so progress is visible and it can resume if interrupted."""
        notify = progress or (lambda *_: None)
        groups = _media_groups(local_root)
        total = sum(size for _, _, size in groups) or 1
        done = 0
        for folder, relative, size in groups:
            notify(done, total, f"Copying to the phone: {folder.name}")
            target = remote_root if relative == "." else f"{remote_root}/{relative}"
            self.shell(f"mkdir -p {shlex.quote(target)}")
            # --sync skips files already present and identical: running it again resumes where it stopped.
            files = [p for p in folder.iterdir() if p.is_file()]
            for start in range(0, len(files), 200):
                chunk = files[start : start + 200]
                self._push("--sync", *[str(p) for p in chunk], target)
                done += sum(p.stat().st_size for p in chunk)
                notify(done, total, f"Copying to the phone: {folder.name}")

    def count_files(self, path: str) -> int:
        output = self.shell(f"find {shlex.quote(path)} -type f 2>/dev/null | wc -l", timeout=180, check=False)
        match = re.search(r"\d+", output)
        return int(match.group(0)) if match else 0

    def clear_whatsapp(self) -> None:
        """Returns WhatsApp to its freshly installed state, without uninstalling it."""
        self.stop_whatsapp()
        output = self.shell(f"pm clear {PACKAGE}", timeout=120, check=False)
        if "Success" not in output:
            raise AndroidError(f"The phone did not allow WhatsApp to be reset: {output}\n\n{OEM_RESTRICTIONS_HINT}")

    def crash_log(self) -> str:
        """Only WhatsApp's startup errors: no message content."""
        log = self._run("logcat", "-d", "-b", "crash", timeout=30, check=False)
        return "\n".join(line for line in log.splitlines() if PACKAGE in line or "FATAL" in line or "\tat " in line or "Caused by" in line)

    def database_errors(self) -> str:
        """Android's errors about the WhatsApp database, with every value removed."""
        match = re.search(r"uid:(\d+)", self.shell(f"pm list packages -U {PACKAGE}", check=False))
        if match is None:
            return ""
        log = self._run("logcat", "-d", f"--uid={match.group(1)}", "-v", "threadtime", timeout=30, check=False)
        lines = [_scrub(line) for line in log.splitlines() if DATABASE_LOG_TAGS.search(line)]
        return "\n".join(lines[-DATABASE_LOG_LINES:])


def _scrub(line: str) -> str:
    line = re.sub(r"'[^']*'", "'…'", line)
    line = re.sub(r"\S+@\S+", "…@…", line)
    return re.sub(r"\d{6,}", "#", line)  # long numbers may be phone numbers


def _media_groups(root: Path) -> list[tuple[Path, str, int]]:
    """All folders that contain files, with their relative path and size."""
    groups = []
    for folder in sorted({p.parent for p in root.rglob("*") if p.is_file()}):
        size = sum(p.stat().st_size for p in folder.iterdir() if p.is_file())
        groups.append((folder, folder.relative_to(root).as_posix(), size))
    return groups
