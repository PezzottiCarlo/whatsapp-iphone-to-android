"""The transfer operations, independent of the interface."""

from __future__ import annotations

import json
import logging
import shlex
import shutil
from dataclasses import asdict, dataclass
from pathlib import Path
from typing import Callable

from . import android, converter, crypt15, diagnosis, ios_backup
from .android import Android, AndroidError

log = logging.getLogger(__name__)
Progress = Callable[[int, int, str], None]

BACKUP_MAX_AGE_SECONDS = 2 * 60 * 60
SPACE_MARGIN_BYTES = 500 * 1024 * 1024
KEPT_WHEN_CLEANING = ("transfer.log",)
SQLITE_MAGIC = b"SQLite format 3\x00"


class NotEnoughSpaceError(Exception):
    """A message for the user: not enough space on the PC or the phone."""


def human_size(size: float) -> str:
    for unit in ("bytes", "KB", "MB", "GB"):
        if size < 1024 or unit == "GB":
            return f"{size:.0f} {unit}" if unit in ("bytes", "KB") else f"{size:.1f} {unit}"
        size /= 1024
    return ""


@dataclass
class State:
    """How far the transfer got: used to resume if the program is closed. Never contains the key."""

    iphone_read: bool = False
    backup_path: str | None = None
    converted: bool = False
    copied: bool = False
    phone_changed: bool = False  # WhatsApp on the phone has already been reset at least once
    device_saved: bool = False  # the files present on the phone are safe on the PC
    phone_root: str | None = None  # where WhatsApp keeps its files on this phone


@dataclass
class PhoneBackup:
    size: int
    age_seconds: int
    whatsapp_version: str | None


class Session:
    def __init__(self, workdir: Path):
        self.workdir = workdir
        self.ios_dir = workdir / "iphone"
        self.android_dir = workdir / "phone"
        self.original_backup = self.android_dir / "original_backup.crypt15"
        self.template_db = self.android_dir / "original_msgstore.db"
        self.device_files = self.android_dir / "existing_files"
        self.output_dir = workdir / "output"
        self.output_db = self.output_dir / "msgstore.db"
        self.output_backup = self.output_dir / "msgstore.db.crypt15"
        self.output_media = self.output_dir / "Media"
        self.report_file = self.output_dir / "summary.json"
        self.state_file = workdir / "state.json"
        self.state = State()
        self.ios_data: ios_backup.IosData | None = None
        self.summary: converter.IosSummary | None = None
        self.report: converter.Report | None = None
        self.root_key: bytes | None = None
        self._reference: crypt15.Backup | None = None
        workdir.mkdir(parents=True, exist_ok=True)
        self._load_state()

    # ---- state

    def _load_state(self) -> None:
        try:
            self.state = State(**json.loads(self.state_file.read_text("utf-8")))
        except (OSError, ValueError, TypeError):
            self.state = State()

    def _save_state(self) -> None:
        self.state_file.write_text(json.dumps(asdict(self.state)), "utf-8")

    def saved_report(self) -> dict | None:
        try:
            return json.loads(self.report_file.read_text("utf-8"))
        except (OSError, ValueError):
            return None

    def phone_root(self, device: Android) -> str:
        # Once found it is kept: after a reset the folder may be gone and can't be detected again.
        if self.state.phone_root is None:
            self.state.phone_root = device.whatsapp_root()
            self._save_state()
        return self.state.phone_root

    # ---- iPhone

    def read_iphone(self, info: ios_backup.BackupInfo, password: str | None, progress: Progress) -> converter.IosSummary:
        self._require_pc_space(_tree_size(info.path) if info.encrypted else 0, "to decrypt the backup")
        self.ios_data = ios_backup.extract(info, self.ios_dir, password, progress)
        progress(0, 0, "Counting chats and messages…")
        self.summary = converter.summarize_ios(self.ios_data.database, self.ios_data.media)
        self.state.iphone_read = True
        self.state.backup_path = str(info.path)
        self.state.converted = self.state.copied = False
        self._save_state()
        return self.summary

    # ---- phone: starting backup

    def read_phone_backup(self, device: Android, key_text: str, progress: Progress) -> PhoneBackup:
        """Downloads the backup WhatsApp made on the phone and checks that the key opens it."""
        root_key = crypt15.parse_key(key_text)
        progress(0, 0, "Looking for the backup on the phone…")
        if not self.state.phone_changed:
            self.state.phone_root = None  # nothing has been touched yet: it may be a different phone
        remote_backup = android.backup_file(self.phone_root(device))
        remote = device.remote_file(remote_backup)
        if remote is None:
            raise AndroidError(
                "There is no backup on the phone yet. In WhatsApp open Settings → Chats → Chat backup, "
                "tap “Back up”, wait for it to finish and try again."
            )
        age = device.now() - remote.modified
        progress(0, 0, "Copying the backup to the PC…")
        target = self.android_dir / "downloaded_backup.crypt15"
        device.pull(remote_backup, target, timeout=None)
        backup = crypt15.decrypt(target.read_bytes(), root_key)
        if backup.database[:16] != SQLITE_MAGIC:
            raise crypt15.Crypt15Error("The backup was decrypted but does not contain a valid database: make it again and retry.")
        self.template_db.write_bytes(backup.database)
        # After the first reset the backup on the phone is ours: the true original,
        # needed to undo everything, must not be replaced any more.
        if self.state.phone_changed and self.original_backup.is_file():
            target.unlink()
        else:
            target.replace(self.original_backup)
        self.root_key, self._reference = root_key, backup
        # The backup is copied last: if it is there, everything else on the phone is complete
        # and can be saved again.
        self.state.device_saved = False
        self.state.converted = False  # the conversion must be redone on this new backup
        self._save_state()
        return PhoneBackup(remote.size, age, device.whatsapp_version())

    # ---- resuming after the backups

    def saved_iphone_backup(self) -> ios_backup.BackupInfo | None:
        return ios_backup.read_info(Path(self.state.backup_path)) if self.state.backup_path else None

    def can_resume(self) -> bool:
        """Both backups are already on the PC: they can be read again without redoing them."""
        return self.state.iphone_read and self.original_backup.is_file() and self.saved_iphone_backup() is not None

    def resume_after_backups(self, key_text: str, password: str | None, progress: Progress) -> converter.IosSummary:
        """Restarts from the backups already made, so only conversion and copy need redoing.

        The base is the backup taken from the phone before it was reset, not the current
        one: the current one already contains the chats merged in an earlier round.
        """
        root_key = crypt15.parse_key(key_text)
        info = self.saved_iphone_backup()
        if info is None:
            raise ios_backup.BackupError("The iPhone backup is no longer on this PC: make it again and restart from step 2.")
        progress(0, 0, "Opening the phone's original backup…")
        backup = crypt15.decrypt(self.original_backup.read_bytes(), root_key)
        if backup.database[:16] != SQLITE_MAGIC:
            raise crypt15.Crypt15Error("The original backup does not contain a valid database.")
        self.template_db.write_bytes(backup.database)
        self.root_key, self._reference = root_key, backup
        summary = self.read_iphone(info, password, progress)
        log.info("Resumed from the backups already made")
        return summary

    # ---- conversion

    def convert(self, progress: Progress) -> converter.Report:
        if self.ios_data is None or self.summary is None:
            raise RuntimeError("iPhone backup not read")
        if self.root_key is None or self._reference is None:
            raise RuntimeError("Phone backup not read")
        self._require_pc_space(self.summary.media_bytes, "to prepare photos and videos")
        if self.output_dir.exists():
            shutil.rmtree(self.output_dir)
        self.output_media.mkdir(parents=True)
        report = converter.convert(
            self.ios_data.database, self.template_db, self.output_db, self.ios_data.media, self.output_media, progress
        )
        progress(0, 0, "Encrypting the new backup…")
        self.output_backup.write_bytes(crypt15.encrypt(self.output_db.read_bytes(), self.root_key, self._reference))
        check = crypt15.decrypt(self.output_backup.read_bytes(), self.root_key)
        if check.database != self.output_db.read_bytes():
            raise crypt15.Crypt15Error("The new backup cannot be read back correctly: try again.")
        self.report = report
        self.report_file.write_text(json.dumps({**asdict(report), "inserted": dict(report.inserted)}), "utf-8")
        self.state.converted, self.state.copied = True, False
        self._save_state()
        log.info("Conversion: %d messages, %d files, %d files missing", report.messages, report.media_copied, report.media_missing)
        return report

    # ---- phone: applying

    def _existing_folders(self, device: Android, root: str) -> list[str]:
        listing = device.shell(f"ls -A {shlex.quote(root)} 2>/dev/null", check=False)
        return [name for name in listing.splitlines() if name.strip() and name.strip() != "Databases"]

    def apply_to_phone(self, device: Android, progress: Progress) -> None:
        """Resets WhatsApp on the phone and copies the new backup onto it.

        The reset also deletes WhatsApp's Android/media folder, so the files already
        there are first saved on the PC and then put back.
        """
        if not self.output_backup.is_file():
            raise RuntimeError("No converted backup to copy")
        needed = _tree_size(self.output_media) + self.output_backup.stat().st_size + SPACE_MARGIN_BYTES
        free = device.free_bytes()
        if free < needed:
            raise NotEnoughSpaceError(
                f"The phone needs {human_size(needed)} free, it has {human_size(free)}. "
                "Free up some space on the phone and try again."
            )

        root = self.phone_root(device)
        self._save_device_files(device, root, progress)

        progress(0, 0, "Resetting WhatsApp on the phone…")
        self.state.copied = False
        self.state.phone_changed = True
        self._save_state()
        device.clear_whatsapp()

        if self.device_files.is_dir():
            device.push_tree(self.device_files, root, _labelled(progress, "existing files"))
        device.push_tree(self.output_media, android.media_dir(root), progress)
        progress(0, 0, "Copying the chat backup to the phone…")
        device.push_verified(self.output_backup, android.backup_file(root))

        progress(0, 0, "Final check…")
        self._verify_phone(device, root)
        self.state.copied = True
        self._save_state()
        log.info("Copy to the phone completed")

    def _save_device_files(self, device: Android, root: str, progress: Progress) -> None:
        # Saved by an earlier attempt: don't overwrite it with a folder that has
        # been reset in the meantime.
        if self.state.device_saved:
            return
        folders = self._existing_folders(device, root)
        if folders:
            self._require_pc_space(device.tree_bytes(root), "to keep the files already on the phone safe")
            if self.device_files.exists():
                shutil.rmtree(self.device_files)
            self.device_files.mkdir(parents=True)
            for index, folder in enumerate(folders):
                progress(index, len(folders), f"Saving the files already on the phone to the PC: {folder}")
                device.pull(f"{root}/{folder}", self.device_files / folder, timeout=None)
        self.state.device_saved = True
        self._save_state()

    def _verify_phone(self, device: Android, root: str) -> None:
        expected = sum(1 for p in self.output_media.rglob("*") if p.is_file())
        found = device.count_files(android.media_dir(root))
        if found < expected:
            raise AndroidError(
                f"The phone has {found} files instead of at least {expected}: the copy is not complete. Run the copy again."
            )
        remote = device.remote_file(android.backup_file(root))
        if remote is None or remote.size != self.output_backup.stat().st_size:
            raise AndroidError("The chat backup on the phone does not look complete. Run the copy again.")

    def undo_on_phone(self, device: Android, progress: Progress) -> None:
        """Returns the phone to its state before the transfer."""
        if not self.original_backup.is_file():
            raise AndroidError("The phone's original backup is not on the PC: it cannot be restored.")
        root = self.phone_root(device)
        progress(0, 0, "Resetting WhatsApp on the phone…")
        device.clear_whatsapp()
        if self.device_files.is_dir():
            device.push_tree(self.device_files, root, _labelled(progress, "existing files"))
        progress(0, 0, "Putting the original backup back…")
        device.push_verified(self.original_backup, android.backup_file(root))
        self.state.copied = False
        self._save_state()

    # ---- diagnostics

    def inspect_phone_backup(self, device: Android, key_text: str) -> list[str]:
        """Measures the latest backup WhatsApp made after the restore, leaving no copies on the PC."""
        if not key_text.strip():
            return ["Key not entered: phone backup not read."]
        remote_backup = android.backup_file(self.phone_root(device))
        if device.remote_file(remote_backup) is None:
            return ["There is no backup on the phone."]
        folder = self.workdir / "diagnostics"
        folder.mkdir(exist_ok=True)
        try:
            blob_path = folder / "backup.crypt15"
            device.pull(remote_backup, blob_path, timeout=None)
            blob = blob_path.read_bytes()
            if self.output_backup.is_file() and blob == self.output_backup.read_bytes():
                return ["This is still the backup copied by the program: no new backup was made in WhatsApp."]
            try:
                database = crypt15.decrypt(blob, crypt15.parse_key(key_text)).database
            except (crypt15.Crypt15Error, ValueError) as exc:
                return [f"The backup does not open with this key: {exc}"]
            current = folder / "msgstore.db"
            current.write_bytes(database)
            lines = diagnosis.describe(current)
            since = diagnosis.newest_timestamp(self.output_db) if self.output_db.is_file() else None
            if since is not None:
                lines += diagnosis.describe_since(current, since)
            return lines
        finally:
            shutil.rmtree(folder, ignore_errors=True)

    # ---- cleanup

    def clean(self) -> None:
        """Deletes every copy of the chats from the PC. The iPhone backup is not touched."""
        for child in self.workdir.iterdir():
            if child.name in KEPT_WHEN_CLEANING:
                continue
            if child.is_dir():
                shutil.rmtree(child, ignore_errors=True)
            else:
                child.unlink(missing_ok=True)
        self.state = State()
        self.ios_data = self.summary = self.report = self.root_key = self._reference = None

    def _require_pc_space(self, bytes_needed: int, purpose: str) -> None:
        needed = bytes_needed + SPACE_MARGIN_BYTES
        free = shutil.disk_usage(self.workdir).free
        if free < needed:
            raise NotEnoughSpaceError(
                f"The PC needs {human_size(needed)} free {purpose}, it has {human_size(free)}. "
                "Free up some space or choose a work folder on another disk."
            )


def backup_is_stale(backup: PhoneBackup) -> bool:
    return backup.age_seconds > BACKUP_MAX_AGE_SECONDS


def _tree_size(root: Path) -> int:
    return sum(p.stat().st_size for p in root.rglob("*") if p.is_file())


def _labelled(progress: Progress, label: str) -> Progress:
    return lambda done, total, _message: progress(done, total, f"Putting back on the phone: {label}")
