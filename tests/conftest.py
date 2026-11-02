"""Test data: a synthetic iPhone backup and the real (empty) WhatsApp 2.26 database."""

from __future__ import annotations

import hashlib
import plistlib
import sqlite3
import sys
from datetime import datetime
from pathlib import Path

import pytest

sys.path.insert(0, str(Path(__file__).parent.parent))

from wa_transfer import crypt15  # noqa: E402

FIXTURES = Path(__file__).parent / "fixtures"
DOMAIN = "AppDomainGroup-group.net.whatsapp.WhatsApp.shared"
ALICE = "15555550100@s.whatsapp.net"
BOB = "15555550111@s.whatsapp.net"
GROUP = "120363000000000001@g.us"
# Seconds since 2001: 2024-01-15 onwards.
T0 = 727_000_000.0
JPEG = b"\xff\xd8\xff\xe0" + b"pict" * 50
THUMB = b"\xff\xd8\xff\xe0" + b"mini" * 10

IOS_SCHEMA = """
CREATE TABLE ZWACHATSESSION (Z_PK INTEGER PRIMARY KEY, ZCONTACTJID TEXT, ZPARTNERNAME TEXT, ZSESSIONTYPE INTEGER, ZARCHIVED INTEGER, ZGROUPINFO INTEGER);
CREATE TABLE ZWAGROUPMEMBER (Z_PK INTEGER PRIMARY KEY, ZCHATSESSION INTEGER, ZMEMBERJID TEXT, ZCONTACTNAME TEXT);
CREATE TABLE ZWAMESSAGE (Z_PK INTEGER PRIMARY KEY, ZCHATSESSION INTEGER, ZISFROMME INTEGER, ZSTANZAID TEXT, ZGROUPMEMBER INTEGER, ZFROMJID TEXT, ZTOJID TEXT, ZMESSAGEDATE REAL, ZSORT INTEGER, ZTEXT TEXT, ZSTARRED INTEGER, ZMESSAGETYPE INTEGER, ZMEDIAITEM INTEGER);
CREATE TABLE ZWAMEDIAITEM (Z_PK INTEGER PRIMARY KEY, ZMESSAGE INTEGER, ZMEDIALOCALPATH TEXT, ZMEDIAURL TEXT, ZVCARDSTRING TEXT, ZVCARDNAME TEXT, ZTITLE TEXT, ZFILESIZE INTEGER, ZMOVIEDURATION REAL, ZLATITUDE REAL, ZLONGITUDE REAL, ZMETADATA BLOB, ZTHUMBNAILLOCALPATH TEXT);
"""


def build_ios_db(path: Path) -> None:
    """Conversations with every message type handled."""
    c = sqlite3.connect(path)
    c.executescript(IOS_SCHEMA)
    c.executemany(
        "INSERT INTO ZWACHATSESSION (Z_PK, ZCONTACTJID, ZPARTNERNAME, ZSESSIONTYPE, ZARCHIVED) VALUES (?,?,?,?,?)",
        [
            (1, ALICE, "Alice", 0, 0),
            (2, GROUP, "Family", 1, 1),
            (3, "status@broadcast", None, 2, 0),
            (4, BOB, "Bob", 0, 0),
        ],
    )
    c.execute("INSERT INTO ZWAGROUPMEMBER VALUES (1, 2, ?, 'Bob')", (BOB,))

    def msg(pk, chat, mine, kind, text=None, offset=0.0, member=None, starred=0, stanza=None):
        c.execute(
            "INSERT INTO ZWAMESSAGE (Z_PK, ZCHATSESSION, ZISFROMME, ZSTANZAID, ZGROUPMEMBER, ZFROMJID, ZMESSAGEDATE, ZTEXT, ZSTARRED, ZMESSAGETYPE) "
            "VALUES (?,?,?,?,?,?,?,?,?,?)",
            (pk, chat, mine, stanza or f"STANZA{pk:04d}", member, None if mine else (GROUP if chat == 2 else ALICE),
             T0 + offset, text, starred, kind),
        )

    def item(pk, message, **cols):
        names = ",".join(cols)
        c.execute(
            f"INSERT INTO ZWAMEDIAITEM (Z_PK, ZMESSAGE, {names}) VALUES (?,?,{','.join('?' * len(cols))})",
            (pk, message, *cols.values()),
        )

    msg(1, 1, 0, 0, "Hello!", 0, stanza="ABC1")
    msg(2, 1, 1, 0, "Hello Alice", 10, starred=1)
    msg(3, 1, 0, 7, "look https://example.com", 20)
    msg(4, 1, 1, 1, None, 30)
    item(1, 4, ZMEDIALOCALPATH="Media/15555550100@s.whatsapp.net/a/b/photo.jpg", ZVCARDSTRING="image/jpeg",
         ZTITLE="At the beach", ZTHUMBNAILLOCALPATH="Media/15555550100@s.whatsapp.net/a/b/photo.thumb")
    msg(5, 1, 0, 2, None, 40)
    item(2, 5, ZMEDIALOCALPATH="Media/15555550100@s.whatsapp.net/a/b/video.mp4", ZVCARDSTRING="video/mp4", ZMOVIEDURATION=12.0)
    msg(6, 1, 0, 3, None, 50)
    item(3, 6, ZMEDIALOCALPATH="Media/15555550100@s.whatsapp.net/a/b/voice.opus", ZVCARDSTRING="audio/ogg; codecs=opus", ZMOVIEDURATION=4.0)
    msg(7, 1, 0, 3, None, 55)
    item(4, 7, ZMEDIALOCALPATH="Media/15555550100@s.whatsapp.net/a/b/track.mp3", ZVCARDSTRING="audio/mpeg", ZMOVIEDURATION=180.0)
    msg(8, 1, 1, 8, None, 60)
    item(5, 8, ZMEDIALOCALPATH="Media/15555550100@s.whatsapp.net/a/b/uuid.pdf", ZVCARDSTRING="application/pdf", ZTITLE="Contract: final?.pdf")
    msg(9, 1, 0, 4, "John", 70)
    item(6, 9, ZVCARDNAME="John Smith", ZVCARDSTRING="BEGIN:VCARD\nFN:John Smith\nEND:VCARD")
    msg(10, 1, 0, 5, None, 80)
    item(7, 10, ZLATITUDE=45.46, ZLONGITUDE=9.19, ZTITLE="Cathedral")
    # Reply to "Hello!": the metadata contains the quoted id in protobuf field 5.
    msg(11, 1, 1, 0, "Replying", 90)
    item(8, 11, ZMETADATA=b"\x2a\x04ABC1")
    msg(12, 1, 0, 11, None, 100)
    item(9, 12, ZMEDIALOCALPATH="Media/15555550100@s.whatsapp.net/a/b/anim.mp4", ZVCARDSTRING="video/mp4")
    msg(13, 1, 0, 15, None, 110)
    item(10, 13, ZMEDIALOCALPATH="Media/15555550100@s.whatsapp.net/a/b/st.webp", ZVCARDSTRING="image/webp")
    # File never downloaded on the iPhone: it is not in the backup.
    msg(14, 1, 0, 1, None, 120)
    item(11, 14, ZMEDIALOCALPATH="Media/15555550100@s.whatsapp.net/a/b/absent.jpg", ZVCARDSTRING="image/jpeg")
    msg(15, 1, 0, 14, None, 130)  # deleted
    msg(16, 2, 0, 6, None, 5)  # group event
    msg(17, 2, 0, 0, "Hello everyone", 15, member=1)
    msg(18, 2, 1, 0, "Hello!", 25)
    msg(19, 3, 0, 0, "status", 35)  # status update: not a conversation
    msg(20, 4, 1, 0, "Hello Bob", 45)
    msg(21, 1, 0, 99, "new type with text", 140)
    msg(22, 1, 0, 99, None, 150)  # new type with no content
    c.commit()
    c.close()


MEDIA_FILES = {
    "Message/Media/15555550100@s.whatsapp.net/a/b/photo.jpg": JPEG,
    "Message/Media/15555550100@s.whatsapp.net/a/b/photo.thumb": THUMB,
    "Message/Media/15555550100@s.whatsapp.net/a/b/video.mp4": b"video" * 40,
    "Message/Media/15555550100@s.whatsapp.net/a/b/voice.opus": b"opus" * 30,
    "Message/Media/15555550100@s.whatsapp.net/a/b/track.mp3": b"mp3" * 30,
    "Message/Media/15555550100@s.whatsapp.net/a/b/uuid.pdf": b"%PDF" * 10,
    "Message/Media/15555550100@s.whatsapp.net/a/b/anim.mp4": b"gif" * 10,
    "Message/Media/15555550100@s.whatsapp.net/a/b/st.webp": b"RIFF" * 10,
}


def build_backup(root: Path, database: Path, *, encrypted=False, state="finished", domain=DOMAIN) -> Path:
    """An iPhone backup with the same structure as real ones (Manifest.db and SHA-1 named files)."""
    root.mkdir(parents=True)
    with open(root / "Manifest.plist", "wb") as f:
        plistlib.dump(
            {
                "IsEncrypted": encrypted,
                "Date": datetime(2026, 11, 1, 10, 0),
                "Lockdown": {"DeviceName": "Sam's iPhone", "ProductType": "iPhone10,4"},
            },
            f,
        )
    with open(root / "Status.plist", "wb") as f:
        plistlib.dump({"SnapshotState": state, "IsFullBackup": True}, f)
    manifest = sqlite3.connect(root / "Manifest.db")
    manifest.execute("CREATE TABLE Files (fileID TEXT PRIMARY KEY, domain TEXT, relativePath TEXT, flags INTEGER, file BLOB)")

    def add(relative, data, flags=1):
        file_id = hashlib.sha1(f"{domain}-{relative}".encode()).hexdigest()
        manifest.execute("INSERT INTO Files VALUES (?,?,?,?,?)", (file_id, domain, relative, flags, b""))
        if flags == 1:
            (root / file_id[:2]).mkdir(exist_ok=True)
            (root / file_id[:2] / file_id).write_bytes(data)

    add("ChatStorage.sqlite", database.read_bytes())
    add("Message", b"", flags=2)
    for relative, data in MEDIA_FILES.items():
        add(relative, data)
    manifest.commit()
    manifest.close()
    return root


@pytest.fixture
def ios_db(tmp_path) -> Path:
    path = tmp_path / "source" / "ChatStorage.sqlite"
    path.parent.mkdir()
    build_ios_db(path)
    return path


@pytest.fixture
def backup_dir(tmp_path, ios_db) -> Path:
    return build_backup(tmp_path / "Backup" / "00008000-AAAA", ios_db)


@pytest.fixture
def root_key() -> bytes:
    return (FIXTURES / "encrypted_backup.key").read_bytes()[-32:]


@pytest.fixture
def real_backup_blob() -> bytes:
    return (FIXTURES / "msgstore-2.26.db.crypt15").read_bytes()


@pytest.fixture
def template_db(tmp_path, real_backup_blob, root_key) -> Path:
    """The real WhatsApp 2.26 database taken from the wa-crypt-tools samples."""
    path = tmp_path / "template.db"
    path.write_bytes(crypt15.decrypt(real_backup_blob, root_key).database)
    return path
