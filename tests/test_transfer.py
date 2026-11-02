from __future__ import annotations

import base64
import hashlib
import shutil
import sqlite3
import subprocess
from pathlib import Path
from types import SimpleNamespace

import pytest
from conftest import ALICE, BOB, DOMAIN, GROUP, JPEG, MEDIA_FILES, T0, THUMB, build_backup

from wa_transfer import android, converter, crypt15, ios_backup


def media_index(root: Path) -> dict[str, Path]:
    index = {}
    for relative, data in MEDIA_FILES.items():
        path = root / relative
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_bytes(data)
        index[relative] = path
    return index


@pytest.fixture
def converted(tmp_path, ios_db, template_db):
    output, media = tmp_path / "out.db", tmp_path / "Media"
    report = converter.convert(ios_db, template_db, output, media_index(tmp_path / "src"), media)
    db = sqlite3.connect(output)
    db.row_factory = sqlite3.Row
    yield SimpleNamespace(db=db, report=report, media=media, path=output)
    db.close()


def message(db, key):
    return db.execute("SELECT * FROM message WHERE key_id=?", (key,)).fetchone()


def media_row(db, key):
    return db.execute(
        "SELECT mm.* FROM message_media mm JOIN message m ON m._id=mm.message_row_id WHERE m.key_id=?", (key,)
    ).fetchone()


# ---- crypt15


def test_real_backup_decrypts(real_backup_blob, root_key):
    backup = crypt15.decrypt(real_backup_blob, root_key)
    assert backup.database.startswith(b"SQLite format 3\x00")
    assert backup.level == 9


def test_wrong_key_is_rejected(real_backup_blob):
    with pytest.raises(crypt15.WrongKeyError):
        crypt15.decrypt(real_backup_blob, bytes(32))


def test_corrupted_file_is_rejected(real_backup_blob, root_key):
    damaged = bytearray(real_backup_blob)
    damaged[len(damaged) // 2] ^= 0xFF
    with pytest.raises(crypt15.Crypt15Error):
        crypt15.decrypt(bytes(damaged), root_key)


def test_reencrypt_keeps_header_and_changes_only_iv(real_backup_blob, root_key):
    original = crypt15.decrypt(real_backup_blob, root_key)
    blob = crypt15.encrypt(b"new content", root_key, original)
    again = crypt15.decrypt(blob, root_key)
    assert again.database == b"new content"
    assert again.iv != original.iv
    assert again.header[: again.iv_start] == original.header[: original.iv_start]
    assert again.header[again.iv_start + 16 :] == original.header[original.iv_start + 16 :]


@pytest.mark.parametrize("text", ["a" * 64, "AAAA " * 16, "aaaa-" * 15 + "aaaa", "aaaa\naaaa " * 8])
def test_key_formats_accepted(text):
    assert crypt15.parse_key(text) == bytes.fromhex("a" * 64)


@pytest.mark.parametrize("text", ["", "abc", "g" * 64, "a" * 63, "a" * 65])
def test_bad_keys_refused(text):
    with pytest.raises(ValueError):
        crypt15.parse_key(text)


# ---- conversion on the real 2.26 schema


def test_counts(converted):
    r = converted.report
    assert r.chats == 3 and r.new_chats == 3
    assert r.messages == 18
    assert r.skipped_system == 1 and r.skipped_deleted == 1 and r.skipped_other == 1 and r.skipped_chats == 1
    assert r.media_copied == 7 and r.media_missing == 1
    assert r.quotes == 1


def test_text_and_direction(converted):
    received, sent = message(converted.db, "ABC1"), message(converted.db, "STANZA0002")
    assert received["text_data"] == "Hello!" and received["from_me"] == 0 and received["status"] == 0
    assert sent["from_me"] == 1 and sent["status"] == 13 and sent["starred"] == 1


def test_timestamps_are_preserved(converted):
    assert message(converted.db, "ABC1")["timestamp"] == int((T0 + 978307200) * 1000)


def test_link_becomes_text(converted):
    row = message(converted.db, "STANZA0003")
    assert row["message_type"] == 0 and "https://example.com" in row["text_data"]


def test_unknown_type_with_text_is_kept(converted):
    assert message(converted.db, "STANZA0021")["text_data"] == "new type with text"


def test_chats_and_jid_types(converted):
    db = converted.db
    rows = {r["raw_string"]: r for r in db.execute("SELECT * FROM jid")}
    assert rows[ALICE]["type"] == 0 and rows[GROUP]["type"] == 1
    assert rows[ALICE]["user"] == "15555550100" and rows[ALICE]["server"] == "s.whatsapp.net"
    assert 7 not in {r["type"] for r in rows.values()}, "type 7 makes WhatsApp crash"
    group = db.execute("SELECT * FROM chat WHERE jid_row_id=?", (rows[GROUP]["_id"],)).fetchone()
    assert group["subject"] == "Family" and group["archived"] == 1
    assert db.execute("SELECT subject FROM chat WHERE jid_row_id=?", (rows[ALICE]["_id"],)).fetchone()[0] is None
    assert "status@broadcast" not in rows


def test_group_sender(converted):
    db = converted.db
    sender = message(db, "STANZA0017")["sender_jid_row_id"]
    assert db.execute("SELECT raw_string FROM jid WHERE _id=?", (sender,)).fetchone()[0] == BOB
    assert message(db, "STANZA0018")["sender_jid_row_id"] == 0


def test_chat_pointers(converted):
    db = converted.db
    for chat in db.execute("SELECT * FROM chat"):
        newest = db.execute(
            "SELECT _id, sort_id, timestamp FROM message WHERE chat_row_id=? ORDER BY timestamp DESC LIMIT 1", (chat["_id"],)
        ).fetchone()
        assert chat["last_message_row_id"] == newest["_id"] == chat["display_message_row_id"]
        assert chat["last_message_sort_id"] == newest["sort_id"] == chat["display_message_sort_id"]
        assert chat["sort_timestamp"] == newest["timestamp"]
        assert chat["unseen_message_count"] == 0


def test_timeline_is_chronological(converted):
    rows = converted.db.execute("SELECT timestamp, sort_id FROM message WHERE chat_row_id>0 ORDER BY sort_id").fetchall()
    stamps = [r["timestamp"] for r in rows]
    assert stamps == sorted(stamps)
    assert [r["sort_id"] for r in rows] == list(range(1, len(rows) + 1))


@pytest.mark.parametrize(
    "key,kind,folder,prefix,ext",
    [
        ("STANZA0004", 1, "Media/WhatsApp Images/Sent/", "IMG-", ".jpg"),
        ("STANZA0005", 3, "Media/WhatsApp Video/", "VID-", ".mp4"),
        ("STANZA0006", 2, "Media/WhatsApp Voice Notes/", "PTT-", ".opus"),
        ("STANZA0007", 2, "Media/WhatsApp Audio/", "AUD-", ".mp3"),
        ("STANZA0012", 13, "Media/WhatsApp Animated Gifs/", "VID-", ".mp4"),
        ("STANZA0013", 20, "Media/WhatsApp Stickers/", "STK-", ".webp"),
    ],
)
def test_media_layout(converted, key, kind, folder, prefix, ext):
    row = media_row(converted.db, key)
    assert message(converted.db, key)["message_type"] == kind
    assert row["file_path"].startswith(folder)
    name = row["file_path"].rsplit("/", 1)[1]
    assert name.startswith(prefix) and name.endswith(ext)
    assert row["transferred"] == 1
    file = converted.media / row["file_path"].removeprefix("Media/")
    data = file.read_bytes()
    assert row["file_size"] == len(data)
    assert row["file_hash"] == base64.b64encode(hashlib.sha256(data).digest()).decode()


def test_message_time_set_on_file(converted):
    row = media_row(converted.db, "STANZA0005")
    file = converted.media / row["file_path"].removeprefix("Media/")
    assert file.stat().st_mtime == pytest.approx(message(converted.db, "STANZA0005")["timestamp"] / 1000, abs=2)


def test_image_caption_and_thumbnail(converted):
    db = converted.db
    row = message(db, "STANZA0004")
    assert row["text_data"] == "At the beach"
    assert db.execute("SELECT thumbnail FROM message_thumbnail WHERE message_row_id=?", (row["_id"],)).fetchone()[0] == THUMB
    assert media_row(db, "STANZA0004")["file_hash"] == base64.b64encode(hashlib.sha256(JPEG).digest()).decode()


def test_voice_note_flag(converted):
    assert message(converted.db, "STANZA0006")["origin"] == 1
    assert message(converted.db, "STANZA0007")["origin"] == 0


def test_document_keeps_a_safe_name(converted):
    row = media_row(converted.db, "STANZA0008")
    assert row["media_name"] == "Contract: final?.pdf"
    assert row["file_path"] == "Media/WhatsApp Documents/Sent/Contract_ final_.pdf"
    assert row["mime_type"] == "application/pdf"


def test_missing_media_keeps_message(converted):
    row = media_row(converted.db, "STANZA0014")
    assert row["transferred"] == 0 and row["file_path"] is None


def test_contact_card(converted):
    row = message(converted.db, "STANZA0009")
    assert row["message_type"] == 4 and row["text_data"] == "John Smith"
    vcard = converted.db.execute("SELECT vcard FROM message_vcard WHERE message_row_id=?", (row["_id"],)).fetchone()[0]
    assert "FN:John Smith" in vcard


def test_location(converted):
    row = message(converted.db, "STANZA0010")
    place = converted.db.execute("SELECT * FROM message_location WHERE message_row_id=?", (row["_id"],)).fetchone()
    assert row["message_type"] == 5
    assert place["latitude"] == 45.46 and place["longitude"] == 9.19 and place["place_name"] == "Cathedral"


def test_reply_points_to_original(converted):
    db = converted.db
    reply = message(db, "STANZA0011")
    quoted = db.execute("SELECT * FROM message_quoted WHERE message_row_id=?", (reply["_id"],)).fetchone()
    assert quoted["key_id"] == "ABC1" and quoted["text_data"] == "Hello!" and quoted["from_me"] == 0
    assert quoted["parent_message_chat_row_id"] == reply["chat_row_id"]


def test_whatsapp_views_still_work(converted):
    db = converted.db
    views = [r[0] for r in db.execute("SELECT name FROM sqlite_master WHERE type='view'")]
    assert views
    for view in views:
        db.execute(f'SELECT * FROM "{view}" LIMIT 5').fetchall()


def test_no_leftovers(converted):
    db = converted.db
    names = {r[0] for r in db.execute("SELECT name FROM sqlite_master")}
    assert not {n for n in names if "wa_transfer" in n}
    assert db.execute("PRAGMA integrity_check").fetchone()[0] == "ok"
    assert not Path(str(converted.path) + "-wal").exists()


def test_template_is_not_modified(tmp_path, ios_db, template_db):
    before = template_db.read_bytes()
    converter.convert(ios_db, template_db, tmp_path / "out.db", {}, tmp_path / "m")
    assert template_db.read_bytes() == before


# ---- merging with a WhatsApp already in use


def prepare_used_phone(template: Path) -> None:
    """Phone already in use: a chat with Alice via LID, a recent message, and an old one already saved from the iPhone."""
    db = sqlite3.connect(template)
    db.execute("INSERT INTO jid (_id,user,server,agent,device,type,raw_string) VALUES (1,'123','lid',0,0,18,'123@lid')")
    db.execute("INSERT INTO jid (_id,user,server,agent,device,type,raw_string) VALUES (2,'15555550100','s.whatsapp.net',0,0,0,?)", (ALICE,))
    db.execute("INSERT INTO jid_map (lid_row_id,jid_row_id) VALUES (1,2)")
    db.execute("INSERT INTO jid (_id,user,server,agent,device,type,raw_string) VALUES (3,'15555550199','s.whatsapp.net',0,0,7,'15555550199@s.whatsapp.net')")
    db.execute(
        "INSERT INTO chat (_id,jid_row_id,hidden,last_message_row_id,display_message_row_id,last_message_sort_id,display_message_sort_id,sort_timestamp) "
        "VALUES (1,1,0,2,2,2,2,9999999999999)"
    )
    db.execute(
        "INSERT INTO message (_id,chat_row_id,from_me,key_id,sender_jid_row_id,status,timestamp,message_type,text_data,sort_id,lookup_tables) "
        "VALUES (1,1,0,'ABC1',0,0,1,0,'Hello!',1,0)"
    )
    db.execute(
        "INSERT INTO message (_id,chat_row_id,from_me,key_id,sender_jid_row_id,status,timestamp,message_type,text_data,sort_id,lookup_tables) "
        "VALUES (2,1,0,'NEW',0,0,9999999999999,0,'recent message',2,0)"
    )
    # Test photo sent by the user on the phone: it teaches the lookup_tables value.
    db.execute(
        "INSERT INTO message (_id,chat_row_id,from_me,key_id,sender_jid_row_id,status,timestamp,message_type,sort_id,lookup_tables) "
        "VALUES (3,1,1,'TEST',0,13,9999999999998,1,3,6)"
    )
    db.commit()
    db.close()


def test_merge_into_used_phone(tmp_path, ios_db, template_db):
    prepare_used_phone(template_db)
    output = tmp_path / "out.db"
    report = converter.convert(ios_db, template_db, output, media_index(tmp_path / "s"), tmp_path / "m")
    db = sqlite3.connect(output)
    db.row_factory = sqlite3.Row

    # The chat with Alice is the existing one via LID, not a duplicate.
    assert db.execute("SELECT count(*) FROM chat WHERE jid_row_id=1").fetchone()[0] == 1
    assert message(db, "STANZA0002")["chat_row_id"] == 1
    assert report.new_chats == 2

    # A message already present is not duplicated.
    assert report.duplicates == 1
    assert db.execute("SELECT count(*) FROM message WHERE key_id='ABC1'").fetchone()[0] == 1

    # The recent message stays the latest, with the right pointers.
    chat = db.execute("SELECT * FROM chat WHERE _id=1").fetchone()
    newest = message(db, "NEW")
    assert chat["last_message_row_id"] == newest["_id"] and chat["last_message_sort_id"] == newest["sort_id"]
    assert newest["sort_id"] == db.execute("SELECT max(sort_id) FROM message").fetchone()[0]

    # The value seen on the phone is reused for the imported photos.
    assert message(db, "STANZA0004")["lookup_tables"] == 6
    assert message(db, "STANZA0002")["lookup_tables"] == 0
    db.close()


def test_schema_change_gives_clear_error(tmp_path, ios_db, template_db):
    db = sqlite3.connect(template_db)
    db.execute("ALTER TABLE message_media RENAME COLUMN file_path TO new_path")
    db.commit()
    db.close()
    with pytest.raises(converter.SchemaError, match="file_path"):
        converter.convert(ios_db, template_db, tmp_path / "out.db", {}, tmp_path / "m")


# ---- iPhone backup


def test_find_and_read_backup(tmp_path, backup_dir):
    found = ios_backup.find_backups(backup_dir.parent)
    assert [b.path for b in found] == [backup_dir]
    assert found[0].device_name == "Sam's iPhone" and not found[0].encrypted and found[0].complete
    assert "Sam's iPhone" in str(found[0])


def test_extract_plain_backup(tmp_path, backup_dir):
    before = sorted((p.as_posix(), p.stat().st_mtime_ns) for p in backup_dir.rglob("*") if p.is_file())
    data = ios_backup.extract(ios_backup.read_info(backup_dir), tmp_path / "work")
    assert sqlite3.connect(data.database).execute("SELECT count(*) FROM ZWAMESSAGE").fetchone()[0] == 22
    assert set(data.media) == set(MEDIA_FILES)
    assert all(p.read_bytes() == MEDIA_FILES[k] for k, p in data.media.items())
    summary = converter.summarize_ios(data.database, data.media)
    assert summary.chats == 3 and summary.messages == 21 and summary.media_files == 7 and summary.media_missing == 1
    assert sorted((p.as_posix(), p.stat().st_mtime_ns) for p in backup_dir.rglob("*") if p.is_file()) == before


def test_incomplete_backup_refused(tmp_path, ios_db):
    info = ios_backup.read_info(build_backup(tmp_path / "b", ios_db, state="new"))
    with pytest.raises(ios_backup.BackupError, match="incomplete"):
        ios_backup.extract(info, tmp_path / "w")


def test_business_backup_explained(tmp_path, ios_db):
    info = ios_backup.read_info(build_backup(tmp_path / "b", ios_db, domain=ios_backup.BUSINESS_DOMAIN))
    with pytest.raises(ios_backup.BackupError, match="Business"):
        ios_backup.extract(info, tmp_path / "w")


def test_encrypted_backup_asks_password(tmp_path, ios_db):
    info = ios_backup.read_info(build_backup(tmp_path / "b", ios_db, encrypted=True))
    assert info.encrypted
    with pytest.raises(ios_backup.WrongPasswordError):
        ios_backup.extract(info, tmp_path / "w")


def test_not_a_backup_is_ignored(tmp_path):
    (tmp_path / "x").mkdir()
    assert ios_backup.read_info(tmp_path / "x") is None


# ---- full chain, no phone


def test_full_chain(tmp_path, backup_dir, real_backup_blob, root_key):
    reference = crypt15.decrypt(real_backup_blob, root_key)
    template = tmp_path / "template.db"
    template.write_bytes(reference.database)

    data = ios_backup.extract(ios_backup.read_info(backup_dir), tmp_path / "ios")
    output, media = tmp_path / "out.db", tmp_path / "Media"
    report = converter.convert(data.database, template, output, data.media, media)
    blob = crypt15.encrypt(output.read_bytes(), root_key, reference)

    final = tmp_path / "final.db"
    final.write_bytes(crypt15.decrypt(blob, root_key).database)
    db = sqlite3.connect(final)
    assert db.execute("SELECT count(*) FROM message WHERE chat_row_id>0").fetchone()[0] == report.messages
    paths = [r[0] for r in db.execute("SELECT file_path FROM message_media WHERE file_path IS NOT NULL")]
    assert len(paths) == report.media_copied
    assert all((media / p.removeprefix("Media/")).is_file() for p in paths)
    db.close()


# ---- adb


class FakeAdb:
    def __init__(self, monkeypatch, responder):
        self.calls = []

        def run(cmd, **kwargs):
            args = cmd[1:]
            if args[:1] == ["-s"]:
                args = args[2:]
            self.calls.append(args)
            out, code = responder(args)
            return subprocess.CompletedProcess(cmd, code, out.encode(), b"")

        monkeypatch.setattr(android.subprocess, "run", run)


@pytest.mark.parametrize(
    "listing,state,model",
    [
        ("List of devices attached\n", "none", None),
        ("List of devices attached\nABC unauthorized usb:1\n", "unauthorized", None),
        ("List of devices attached\nABC offline\n", "offline", None),
        ("List of devices attached\nABC device product:x model:A024_Phone device:y\n", "ok", "A024 Phone"),
        ("List of devices attached\nA device\nB device\n", "many", None),
        ("* daemon started *\nList of devices attached\nABC device model:M\n", "ok", "M"),
    ],
)
def test_device_states(monkeypatch, listing, state, model):
    FakeAdb(monkeypatch, lambda args: (listing, 0))
    status = android.Android(Path("adb")).status()
    assert status.state == state and status.model == model


def test_push_verified_ok(monkeypatch, tmp_path):
    local = tmp_path / "f"
    local.write_bytes(b"data")
    digest = hashlib.sha256(b"data").hexdigest()
    fake = FakeAdb(monkeypatch, lambda a: (f"{digest}  x", 0) if "sha256sum" in a[-1] else ("", 0))
    android.Android(Path("adb")).push_verified(local, android.backup_file(android.WA_ROOT))
    assert any(a[0] == "shell" and a[1].startswith("mv -f") for a in fake.calls)


def test_push_verified_detects_corruption(monkeypatch, tmp_path):
    local = tmp_path / "f"
    local.write_bytes(b"data")
    fake = FakeAdb(monkeypatch, lambda a: ("0" * 64 + "  x", 0) if "sha256sum" in a[-1] else ("", 0))
    with pytest.raises(android.AndroidError, match="not identical"):
        android.Android(Path("adb")).push_verified(local, android.backup_file(android.WA_ROOT))
    assert not any(a[0] == "shell" and a[1].startswith("mv -f") for a in fake.calls)


def test_push_tree_handles_spaces_and_chunks(monkeypatch, tmp_path):
    folder = tmp_path / "WhatsApp Images" / "Sent"
    folder.mkdir(parents=True)
    for i in range(450):
        (folder / f"IMG-{i}.jpg").write_bytes(b"x")
    fake = FakeAdb(monkeypatch, lambda a: ("", 0))
    seen = []
    android.Android(Path("adb")).push_tree(tmp_path, android.media_dir(android.WA_ROOT), lambda d, t, m: seen.append((d, t)))
    pushes = [a for a in fake.calls if a[0] == "push"]
    assert len(pushes) == 3
    assert all(a[1] == "--sync" and a[-1] == f"{android.media_dir(android.WA_ROOT)}/WhatsApp Images/Sent" for a in pushes)
    assert seen[-1] == (450, 450)
    assert any("'" in a[1] and "WhatsApp Images" in a[1] for a in fake.calls if a[0] == "shell")


def test_push_tree_root_files_go_to_root(monkeypatch, tmp_path):
    (tmp_path / "a.txt").write_bytes(b"x")
    fake = FakeAdb(monkeypatch, lambda a: ("", 0))
    android.Android(Path("adb")).push_tree(tmp_path, android.WA_ROOT)
    assert [a[-1] for a in fake.calls if a[0] == "push"] == [android.WA_ROOT]


def test_push_retries_transient_refusal(monkeypatch, tmp_path):
    monkeypatch.setattr(android, "PUSH_RETRY_SECONDS", 0)
    (tmp_path / "a.txt").write_bytes(b"x")
    outcomes = iter([("remote couldn't create file: Operation not permitted", 1), ("ok", 0)])
    fake = FakeAdb(monkeypatch, lambda a: next(outcomes) if a[0] == "push" else ("", 0))
    android.Android(Path("adb")).push_tree(tmp_path, android.WA_ROOT)
    assert sum(1 for a in fake.calls if a[0] == "push") == 2


def test_clear_refused_is_reported(monkeypatch):
    FakeAdb(monkeypatch, lambda a: ("Failed", 0))
    with pytest.raises(android.AndroidError, match="reset"):
        android.Android(Path("adb")).clear_whatsapp()


def test_whatsapp_version_absent(monkeypatch):
    FakeAdb(monkeypatch, lambda a: ("", 0))
    assert android.Android(Path("adb")).whatsapp_version() is None


def test_whatsapp_version_ignores_business(monkeypatch):
    FakeAdb(monkeypatch, lambda a: ("package:com.whatsapp.w4b", 0))
    assert android.Android(Path("adb")).whatsapp_version() is None


def test_whatsapp_version_present(monkeypatch):
    def respond(args):
        if "pm list" in args[1]:
            return "package:com.whatsapp.w4b\npackage:com.whatsapp", 0
        return "    versionName=2.26.44.7\n", 0

    FakeAdb(monkeypatch, respond)
    assert android.Android(Path("adb")).whatsapp_version() == "2.26.44.7"


@pytest.mark.parametrize("sdk", ["30", "36", ""])
def test_modern_android_uses_android_media(monkeypatch, sdk):
    FakeAdb(monkeypatch, lambda a: (sdk, 0) if "getprop" in a[-1] else ("", 0))
    assert android.Android(Path("adb")).whatsapp_root() == android.WA_ROOT


def test_older_android_uses_the_folder_with_the_newest_backup(monkeypatch):
    def respond(args):
        if "getprop" in args[-1]:
            return "29", 0
        if android.LEGACY_ROOT in args[-1]:
            return "100 2000", 0
        return ("100 1000", 0) if android.WA_ROOT in args[-1] else ("", 0)

    FakeAdb(monkeypatch, respond)
    assert android.Android(Path("adb")).whatsapp_root() == android.LEGACY_ROOT


def test_older_android_without_any_backup_defaults_to_the_legacy_folder(monkeypatch):
    FakeAdb(monkeypatch, lambda a: ("28", 0) if "getprop" in a[-1] else ("", 0))
    assert android.Android(Path("adb")).whatsapp_root() == android.LEGACY_ROOT


def test_push_verified_falls_back_to_md5(monkeypatch, tmp_path):
    local = tmp_path / "f"
    local.write_bytes(b"data")
    digest = hashlib.md5(b"data").hexdigest()

    def respond(args):
        if "sha256sum" in args[-1]:
            return "sha256sum: inaccessible or not found", 127
        return (f"{digest}  x", 0) if "md5sum" in args[-1] else ("", 0)

    fake = FakeAdb(monkeypatch, respond)
    android.Android(Path("adb")).push_verified(local, android.backup_file(android.WA_ROOT))
    assert any(a[0] == "shell" and a[1].startswith("mv -f") for a in fake.calls)


def test_push_verified_falls_back_to_size_when_no_digest_tool_exists(monkeypatch, tmp_path):
    local = tmp_path / "f"
    local.write_bytes(b"data")

    def respond(args):
        if "sum " in args[-1]:
            return "not found", 127
        return ("4 1000", 0) if "stat -c" in args[-1] else ("", 0)

    fake = FakeAdb(monkeypatch, respond)
    android.Android(Path("adb")).push_verified(local, android.backup_file(android.WA_ROOT))
    assert any(a[0] == "shell" and a[1].startswith("mv -f") for a in fake.calls)


@pytest.mark.parametrize(
    "output",
    [
        "Filesystem 1K-blocks Used Available Use% Mounted on\n/dev/fuse 1000 400 600 40% /storage/emulated",
        "Filesystem 1K-blocks Used Available Use% Mounted on\n/a/very/long/device/name\n 1000 400 600 40% /storage/emulated",
    ],
)
def test_free_space_is_read_on_any_df_layout(monkeypatch, output):
    FakeAdb(monkeypatch, lambda a: (output, 0))
    assert android.Android(Path("adb")).free_bytes() == 600 * 1024


def test_refused_reset_explains_brand_restrictions(monkeypatch):
    FakeAdb(monkeypatch, lambda a: ("java.lang.SecurityException: PERMISSION_DENIED", 0))
    with pytest.raises(android.AndroidError, match="Xiaomi"):
        android.Android(Path("adb")).clear_whatsapp()
