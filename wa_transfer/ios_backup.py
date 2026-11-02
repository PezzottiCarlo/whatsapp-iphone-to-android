"""Reading the iPhone's local backups created by iTunes or the Apple Devices app."""

from __future__ import annotations

import os
import plistlib
import shutil
import sqlite3
from dataclasses import dataclass
from datetime import datetime, timezone
from pathlib import Path
from typing import Callable

WHATSAPP_DOMAIN = "AppDomainGroup-group.net.whatsapp.WhatsApp.shared"
BUSINESS_DOMAIN = "AppDomainGroup-group.net.whatsapp.WhatsAppSMB.shared"
DATABASE_FILES = ("ChatStorage.sqlite", "ChatStorage.sqlite-wal", "ChatStorage.sqlite-shm")
MEDIA_PREFIX = "Message/Media/"

Progress = Callable[[int, int, str], None]


class BackupError(Exception):
    """A message for the user about a problem with the backup."""


class WrongPasswordError(BackupError):
    pass


@dataclass
class BackupInfo:
    path: Path
    device_name: str
    product: str
    date: datetime | None
    encrypted: bool
    complete: bool

    def __str__(self) -> str:
        when = self.date.strftime("%Y-%m-%d %H:%M") if self.date else "unknown date"
        lock = " · password protected" if self.encrypted else ""
        return f"{self.device_name} · {when}{lock}"


@dataclass
class IosData:
    database: Path
    media: dict[str, Path]


def backup_roots() -> list[Path]:
    """The folders where classic iTunes and Microsoft Store iTunes/Apple Devices save backups."""
    roots = []
    if appdata := os.environ.get("APPDATA"):
        roots.append(Path(appdata) / "Apple Computer" / "MobileSync" / "Backup")
    if profile := os.environ.get("USERPROFILE"):
        roots.append(Path(profile) / "Apple" / "MobileSync" / "Backup")
    return roots


def read_info(path: Path) -> BackupInfo | None:
    try:
        with open(path / "Manifest.plist", "rb") as f:
            manifest = plistlib.load(f)
    except (OSError, plistlib.InvalidFileException, ValueError):
        return None
    if not (path / "Manifest.db").is_file():
        return None
    lockdown = manifest.get("Lockdown", {})
    date = manifest.get("Date")
    if isinstance(date, datetime):
        date = date.replace(tzinfo=timezone.utc).astimezone().replace(tzinfo=None) if date.tzinfo is None else date.astimezone().replace(tzinfo=None)
    else:
        date = None
    complete = True
    try:
        with open(path / "Status.plist", "rb") as f:
            status = plistlib.load(f)
        complete = status.get("SnapshotState", "finished") == "finished"
        if not date and isinstance(status.get("Date"), datetime):
            date = status["Date"]
    except (OSError, plistlib.InvalidFileException, ValueError):
        pass
    return BackupInfo(
        path=path,
        device_name=str(lockdown.get("DeviceName") or "iPhone"),
        product=str(lockdown.get("ProductType") or ""),
        date=date,
        encrypted=bool(manifest.get("IsEncrypted")),
        complete=complete,
    )


def find_backups(extra_root: Path | None = None) -> list[BackupInfo]:
    found = []
    roots = backup_roots() + ([extra_root] if extra_root else [])
    for root in roots:
        if not root.is_dir():
            continue
        candidates = [root] + [p for p in root.iterdir() if p.is_dir()]
        for candidate in candidates:
            info = read_info(candidate)
            if info is not None:
                found.append(info)
    found.sort(key=lambda b: b.date or datetime.min, reverse=True)
    return found


def _missing_whatsapp(domains: set[str]) -> BackupError:
    if BUSINESS_DOMAIN in domains:
        return BackupError(
            "This backup only contains WhatsApp Business, which this program does not handle: regular WhatsApp is required."
        )
    return BackupError(
        "This backup contains no WhatsApp data. Check that WhatsApp is installed on the iPhone and make the backup again."
    )


def extract(info: BackupInfo, workdir: Path, password: str | None = None, progress: Progress | None = None) -> IosData:
    """Prepares the chat database in `workdir` and indexes the media. The backup is never modified."""
    notify = progress or (lambda *_: None)
    if not info.complete:
        raise BackupError("This backup is incomplete (it was interrupted). Make it again until it finishes and retry.")
    workdir.mkdir(parents=True, exist_ok=True)
    for name in DATABASE_FILES:
        (workdir / name).unlink(missing_ok=True)
    if info.encrypted:
        return _extract_encrypted(info, workdir, password, notify)
    return _extract_plain(info, workdir, notify)


def _extract_plain(info: BackupInfo, workdir: Path, notify: Progress) -> IosData:
    notify(0, 0, "Reading the backup…")
    manifest = sqlite3.connect(f"file:{info.path / 'Manifest.db'}?mode=ro", uri=True)
    try:
        domains = {r[0] for r in manifest.execute("SELECT DISTINCT domain FROM Files WHERE domain LIKE '%net.whatsapp.%'")}
        rows = manifest.execute(
            "SELECT fileID, relativePath FROM Files WHERE domain=? AND flags=1", (WHATSAPP_DOMAIN,)
        ).fetchall()
    except sqlite3.DatabaseError as exc:
        raise BackupError(f"Could not read the backup: {exc}. Make it again and retry.") from exc
    finally:
        manifest.close()

    media: dict[str, Path] = {}
    found_db = False
    for file_id, relative in rows:
        source = info.path / file_id[:2] / file_id
        if relative in DATABASE_FILES:
            if source.is_file():
                shutil.copyfile(source, workdir / relative)
                found_db = found_db or relative == DATABASE_FILES[0]
        elif relative.startswith(MEDIA_PREFIX):
            media[relative] = source
    if not found_db:
        raise _missing_whatsapp(domains)
    return IosData(workdir / DATABASE_FILES[0], media)


def _extract_encrypted(info: BackupInfo, workdir: Path, password: str | None, notify: Progress) -> IosData:
    from iphone_backup_decrypt import EncryptedBackup
    from iphone_backup_decrypt.exceptions import IncorrectPassphraseError

    if not password:
        raise WrongPasswordError("This backup is protected: enter the password you chose for the iPhone's backups.")
    notify(0, 0, "Unlocking the backup with the password (this may take a minute)…")
    try:
        backup = EncryptedBackup(backup_directory=str(info.path), passphrase=password)
        backup.test_decryption()
    except IncorrectPassphraseError as exc:
        raise WrongPasswordError("The backup password is not correct.") from exc

    found_db = False
    for name in DATABASE_FILES:
        try:
            backup.extract_file(relative_path=name, domain_like=WHATSAPP_DOMAIN, output_filename=str(workdir / name))
            found_db = found_db or name == DATABASE_FILES[0]
        except FileNotFoundError:
            continue
    if not found_db:
        with backup.manifest_db_cursor() as cursor:
            cursor.execute("SELECT DISTINCT domain FROM Files WHERE domain LIKE '%net.whatsapp.%'")
            raise _missing_whatsapp({r[0] for r in cursor.fetchall()})

    media_root = workdir / "decrypted_media"

    def on_file(*, n, total_files, **_):
        if n % 25 == 0:
            notify(n, total_files, "Decrypting photos, videos and documents…")
        return True

    backup.extract_files(
        relative_paths_like=MEDIA_PREFIX + "%",
        domain_like=WHATSAPP_DOMAIN,
        output_folder=str(media_root),
        preserve_folders=True,
        incremental=True,
        filter_callback=on_file,
    )
    media = {}
    for path in media_root.rglob("*") if media_root.is_dir() else ():
        if path.is_file():
            media[path.relative_to(media_root).as_posix()] = path
    return IosData(workdir / DATABASE_FILES[0], media)
