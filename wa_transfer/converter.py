"""Merges the WhatsApp for iPhone history (ChatStorage.sqlite) into an Android msgstore.db.

The starting point is always the real database taken from the destination phone,
not a reconstructed schema: tables and columns are therefore those of the installed
WhatsApp version, and the columns are read at runtime.

Rules derived from how the app really works (see wa-ios-to-android, tested on
WhatsApp 2.26 with about 89,000 messages):

- jid.type: 0 number, 18 LID, 1 group. Type 7 is the user's own account and must never
  be reused: it makes the app crash at startup.
- Current WhatsApp addresses people through LIDs: the jid_map table links LID and number.
- Message order depends on sort_id, not _id: the whole timeline must be renumbered.
- In backups the message table may have no indexes, so INSERT OR IGNORE does not
  protect against duplicates: the check is explicit.
"""

from __future__ import annotations

import base64
import hashlib
import mimetypes
import os
import re
import shutil
import sqlite3
from collections import Counter
from dataclasses import dataclass, field
from datetime import datetime
from pathlib import Path
from typing import Callable, Mapping

APPLE_EPOCH = 978307200
CHAT_SESSION_TYPES = (0, 1, 4)  # individual, group, community group
GROUP_SESSION_TYPES = (1, 4)
MAX_THUMBNAIL_BYTES = 100_000  # beyond this, rows exceed Android's CursorWindow

# iOS types (ZMESSAGETYPE) -> Android types (message.message_type)
TEXT, IMAGE, AUDIO, VIDEO, CONTACT, LOCATION, DOCUMENT, GIF, CONTACTS, STICKER = 0, 1, 2, 3, 4, 5, 9, 13, 14, 20
IOS_TO_ANDROID = {
    0: TEXT,
    7: TEXT,  # text with link preview
    1: IMAGE,
    2: VIDEO,
    3: AUDIO,
    4: CONTACT,
    5: LOCATION,
    8: DOCUMENT,
    11: GIF,
    15: STICKER,
}
IOS_SYSTEM = 6
IOS_DELETED = 14
MEDIA_TYPES = {IMAGE, AUDIO, VIDEO, DOCUMENT, GIF, STICKER}
KIND_LABELS = {
    TEXT: "Texts and links",
    IMAGE: "Photos",
    AUDIO: "Audio and voice notes",
    VIDEO: "Video",
    CONTACT: "Contacts",
    CONTACTS: "Contacts",
    LOCATION: "Locations",
    DOCUMENT: "Documents",
    GIF: "GIF",
    STICKER: "Stickers",
}

# Columns without which the conversion cannot give a reliable result.
REQUIRED_ANDROID = {
    "jid": {"_id", "user", "server", "type", "raw_string"},
    "chat": {"_id", "jid_row_id", "subject", "sort_timestamp", "last_message_row_id", "display_message_row_id"},
    "message": {
        "_id", "chat_row_id", "from_me", "key_id", "sender_jid_row_id", "status",
        "timestamp", "message_type", "text_data", "sort_id",
    },
    "message_media": {"message_row_id", "chat_row_id", "file_path", "mime_type", "transferred"},
}
REQUIRED_IOS = {
    "ZWACHATSESSION": {"Z_PK", "ZCONTACTJID", "ZSESSIONTYPE"},
    "ZWAMESSAGE": {"Z_PK", "ZCHATSESSION", "ZISFROMME", "ZSTANZAID", "ZMESSAGEDATE", "ZMESSAGETYPE", "ZTEXT"},
    "ZWAMEDIAITEM": {"Z_PK", "ZMESSAGE", "ZMEDIALOCALPATH"},
}

Progress = Callable[[int, int, str], None]


class SchemaError(Exception):
    """The database does not have the expected structure (usually a WhatsApp version that is too new)."""


@dataclass
class Report:
    chats: int = 0
    new_chats: int = 0
    inserted: Counter = field(default_factory=Counter)
    duplicates: int = 0
    media_copied: int = 0
    media_missing: int = 0
    media_bytes: int = 0
    quotes: int = 0
    skipped_system: int = 0
    skipped_deleted: int = 0
    skipped_other: int = 0
    skipped_chats: int = 0
    empty_contacts: int = 0

    @property
    def messages(self) -> int:
        return sum(self.inserted.values())


@dataclass
class IosSummary:
    chats: int
    messages: int
    media_files: int
    media_missing: int
    media_bytes: int
    first: datetime | None
    last: datetime | None


def _columns(conn: sqlite3.Connection, table: str) -> dict[str, tuple]:
    return {r[1]: r for r in conn.execute(f'PRAGMA table_info("{table}")')}


def _check_schema(conn: sqlite3.Connection, required: dict[str, set[str]], where: str) -> None:
    problems = []
    for table, cols in required.items():
        have = _columns(conn, table)
        if not have:
            problems.append(f"table {table} missing")
        elif missing := cols - set(have):
            problems.append(f"{table}: missing {', '.join(sorted(missing))}")
    if problems:
        raise SchemaError(f"Unrecognized structure in the {where} database: " + "; ".join(problems))


class _Table:
    """INSERT that writes only the columns existing in this WhatsApp version."""

    def __init__(self, conn: sqlite3.Connection, name: str):
        self.conn = conn
        self.name = name
        self.columns = _columns(conn, name)
        self._must_fill = {
            col: ("" if "TEXT" in (info[2] or "").upper() else 0)
            for col, info in self.columns.items()
            if info[3] and info[4] is None and not info[5]  # NOT NULL, no default, not primary key
        }
        self._sql: dict[tuple[str, ...], str] = {}

    def __bool__(self) -> bool:
        return bool(self.columns)

    def insert(self, **values) -> int:
        row = {**self._must_fill, **{k: v for k, v in values.items() if k in self.columns}}
        keys = tuple(row)
        sql = self._sql.get(keys)
        if sql is None:
            names = ",".join(f'"{k}"' for k in keys)
            sql = f'INSERT INTO "{self.name}" ({names}) VALUES ({",".join("?" * len(keys))})'
            self._sql[keys] = sql
        return self.conn.execute(sql, tuple(row.values())).lastrowid


def _ios_select(conn: sqlite3.Connection) -> str:
    message = _columns(conn, "ZWAMESSAGE")
    media = _columns(conn, "ZWAMEDIAITEM")

    def pick(alias: str, have: Mapping, col: str) -> str:
        return f"{alias}.{col} AS {col}" if col in have else f"NULL AS {col}"

    cols = [pick("m", message, c) for c in (
        "Z_PK", "ZCHATSESSION", "ZISFROMME", "ZSTANZAID", "ZGROUPMEMBER", "ZFROMJID",
        "ZMESSAGEDATE", "ZTEXT", "ZSTARRED", "ZMESSAGETYPE",
    )] + [pick("i", media, c) for c in (
        "ZMEDIALOCALPATH", "ZVCARDSTRING", "ZVCARDNAME", "ZTITLE", "ZMOVIEDURATION",
        "ZLATITUDE", "ZLONGITUDE", "ZMETADATA", "ZTHUMBNAILLOCALPATH", "ZMEDIAURL",
    )]
    return (
        f"SELECT {', '.join(cols)} FROM ZWAMESSAGE m "
        "LEFT JOIN ZWAMEDIAITEM i ON i.ZMESSAGE = m.Z_PK "
        "WHERE m.ZMESSAGEDATE IS NOT NULL ORDER BY m.ZMESSAGEDATE, m.Z_PK"
    )


def media_key(local_path: str | None) -> str | None:
    """Path in the iPhone backup of a file named by ZMEDIALOCALPATH."""
    if not local_path:
        return None
    return "Message/" + local_path.lstrip("/")


def summarize_ios(ios_db: Path, media_index: Mapping[str, Path]) -> IosSummary:
    """Counts shown to the user before proceeding."""
    conn = sqlite3.connect(ios_db)
    try:
        _check_schema(conn, REQUIRED_IOS, "iPhone")
        placeholders = ",".join("?" * len(CHAT_SESSION_TYPES))
        where = f"m.ZCHATSESSION IN (SELECT Z_PK FROM ZWACHATSESSION WHERE ZSESSIONTYPE IN ({placeholders}) AND ZCONTACTJID IS NOT NULL)"
        chats = conn.execute(
            f"SELECT count(*) FROM ZWACHATSESSION WHERE ZSESSIONTYPE IN ({placeholders}) AND ZCONTACTJID IS NOT NULL",
            CHAT_SESSION_TYPES,
        ).fetchone()[0]
        count, first, last = conn.execute(
            f"SELECT count(*), min(m.ZMESSAGEDATE), max(m.ZMESSAGEDATE) FROM ZWAMESSAGE m "
            f"WHERE m.ZMESSAGEDATE IS NOT NULL AND {where}",
            CHAT_SESSION_TYPES,
        ).fetchone()
        files = missing = size = 0
        for (path,) in conn.execute(
            f"SELECT i.ZMEDIALOCALPATH FROM ZWAMEDIAITEM i JOIN ZWAMESSAGE m ON i.ZMESSAGE = m.Z_PK "
            f"WHERE i.ZMEDIALOCALPATH IS NOT NULL AND {where}",
            CHAT_SESSION_TYPES,
        ):
            source = media_index.get(media_key(path))
            if source is not None and source.is_file():
                files += 1
                size += source.stat().st_size
            else:
                missing += 1
    finally:
        conn.close()

    def to_dt(value):
        return datetime.fromtimestamp(value + APPLE_EPOCH) if value is not None else None

    return IosSummary(chats, count, files, missing, size, to_dt(first), to_dt(last))


def _quoted_stanza(metadata: bytes | None) -> str | None:
    """The id of the quoted message: field 5 (0x2a) of the protobuf in ZMETADATA."""
    if not metadata or metadata[0] != 0x2A or len(metadata) < 2:
        return None
    length, pos, shift = 0, 1, 0
    while pos < len(metadata):
        byte = metadata[pos]
        pos += 1
        length |= (byte & 0x7F) << shift
        shift += 7
        if not byte & 0x80:
            break
    try:
        stanza = metadata[pos : pos + length].decode("ascii")
    except UnicodeDecodeError:
        return None
    return stanza if stanza and len(metadata) >= pos + length else None


class _MediaPlanner:
    """Gives files the names and folders used by WhatsApp for Android."""

    def __init__(self, media_out: Path):
        self.media_out = media_out
        self.sequence: Counter = Counter()
        self.used: set[str] = set()

    @staticmethod
    def _safe(name: str) -> str:
        name = re.sub(r'[<>:"/\\|?*\x00-\x1f]', "_", name).strip(" .")
        if len(name) > 120:
            stem, ext = os.path.splitext(name)
            name = stem[: 120 - len(ext)] + ext
        return name or "document"

    def relative_path(self, kind: int, voice: bool, from_me: bool, when: datetime, ext: str, title: str | None) -> str:
        day = when.strftime("%Y%m%d")
        sent = "Sent/" if from_me else ""
        if kind == DOCUMENT and title:
            name = self._safe(title)
            if not os.path.splitext(name)[1] and ext:
                name += ext
            folder = f"WhatsApp Documents/{sent}"
            candidate, n = name, 1
            while (folder + candidate).lower() in self.used:
                stem, suffix = os.path.splitext(name)
                candidate, n = f"{stem} ({n}){suffix}", n + 1
            self.used.add((folder + candidate).lower())
            return folder + candidate
        if kind == AUDIO and voice:
            folder, prefix = f"WhatsApp Voice Notes/{when.strftime('%Y%W')}/", "PTT"
        else:
            folder, prefix = {
                IMAGE: (f"WhatsApp Images/{sent}", "IMG"),
                VIDEO: (f"WhatsApp Video/{sent}", "VID"),
                AUDIO: (f"WhatsApp Audio/{sent}", "AUD"),
                DOCUMENT: (f"WhatsApp Documents/{sent}", "DOC"),
                GIF: (f"WhatsApp Animated Gifs/{sent}", "VID"),
                STICKER: ("WhatsApp Stickers/", "STK"),
            }[kind]
        self.sequence[(prefix, day)] += 1
        return f"{folder}{prefix}-{day}-WA{self.sequence[(prefix, day)]:04d}{ext}"

    def copy(self, source: Path, relative: str, timestamp_ms: int) -> tuple[str, int]:
        """Copies the file and returns (base64 sha256, size)."""
        target = self.media_out / relative
        target.parent.mkdir(parents=True, exist_ok=True)
        digest = hashlib.sha256()
        size = 0
        with open(source, "rb") as src, open(target, "wb") as dst:
            while chunk := src.read(1024 * 1024):
                digest.update(chunk)
                dst.write(chunk)
                size += len(chunk)
        # The gallery sorts by file date: keep the message's date.
        seconds = max(timestamp_ms / 1000, 0)
        os.utime(target, (seconds, seconds))
        return base64.b64encode(digest.digest()).decode("ascii"), size


def _extension(source_path: str | None, mime: str | None, kind: int) -> str:
    ext = os.path.splitext(source_path or "")[1].lower()
    if ext and ext != ".":
        return ext
    guessed = mimetypes.guess_extension((mime or "").split(";")[0].strip()) if mime else None
    if guessed:
        return guessed
    return {IMAGE: ".jpg", VIDEO: ".mp4", AUDIO: ".opus", GIF: ".mp4", STICKER: ".webp"}.get(kind, "")


def _mime(mime: str | None, target_ext: str, kind: int) -> str:
    if mime and "/" in mime and "\n" not in mime and len(mime) < 128:
        return mime
    guessed = mimetypes.guess_type("x" + target_ext)[0]
    if guessed:
        return guessed
    return {
        IMAGE: "image/jpeg", VIDEO: "video/mp4", AUDIO: "audio/ogg; codecs=opus",
        GIF: "video/mp4", STICKER: "image/webp",
    }.get(kind, "application/octet-stream")


def _learn_lookup_tables(conn: sqlite3.Connection) -> dict[tuple[int, bool], int]:
    """Values of message.lookup_tables already used by WhatsApp on this phone.

    The column's meaning is undocumented. If real messages of a given type exist on
    the phone (the app asks the user to send a test photo and a test reply), their
    value is reused; otherwise 0, which is correct for texts.
    """
    learned: dict[tuple[int, bool], int] = {}
    rows = conn.execute(
        "SELECT m.message_type, m._id IN (SELECT message_row_id FROM message_quoted), m.lookup_tables "
        "FROM message m WHERE m.lookup_tables IS NOT NULL AND m.message_type IS NOT NULL ORDER BY m._id"
    ) if _columns(conn, "message_quoted") and "lookup_tables" in _columns(conn, "message") else []
    for kind, quoted, value in rows:
        learned[(kind, bool(quoted))] = value
    return learned


def convert(
    ios_db: Path,
    template_db: Path,
    output_db: Path,
    media_index: Mapping[str, Path],
    media_out: Path,
    progress: Progress | None = None,
) -> Report:
    """Creates `output_db` from `template_db`, adding the iOS messages; copies the media to `media_out`."""
    report_progress = progress or (lambda *_: None)
    shutil.copyfile(template_db, output_db)
    for suffix in ("-wal", "-shm", "-journal"):
        Path(str(output_db) + suffix).unlink(missing_ok=True)

    ios = sqlite3.connect(ios_db)
    ios.row_factory = sqlite3.Row
    db = sqlite3.connect(output_db)
    try:
        _check_schema(ios, REQUIRED_IOS, "iPhone")
        _check_schema(db, REQUIRED_ANDROID, "Android phone")
        db.execute("PRAGMA foreign_keys=OFF")
        db.execute("PRAGMA journal_mode=DELETE")
        db.execute("PRAGMA synchronous=OFF")
        report = _merge(ios, db, media_index, media_out, report_progress)
        report_progress(0, 0, "Final database check…")
        result = db.execute("PRAGMA integrity_check").fetchone()[0]
        if result != "ok":
            raise SchemaError(f"The resulting database fails the integrity check: {result}")
    finally:
        ios.close()
        db.close()
    return report


def _merge(
    ios: sqlite3.Connection,
    db: sqlite3.Connection,
    media_index: Mapping[str, Path],
    media_out: Path,
    progress: Progress,
) -> Report:
    report = Report()
    cur = db.cursor()
    jid_table, chat_table = _Table(db, "jid"), _Table(db, "chat")
    message_table, media_table = _Table(db, "message"), _Table(db, "message_media")
    location_table, vcard_table = _Table(db, "message_location"), _Table(db, "message_vcard")
    quoted_table, thumb_table = _Table(db, "message_quoted"), _Table(db, "message_thumbnail")
    hash_thumb_table = _Table(db, "media_hash_thumbnail")
    has_jid_map = bool(_columns(db, "jid_map"))
    lookup = _learn_lookup_tables(db)

    server_type = {"s.whatsapp.net": 0, "lid": 18, "g.us": 1}
    jid_cache: dict[str, int] = {}

    def jid_row(raw: str) -> int:
        if raw in jid_cache:
            return jid_cache[raw]
        row = cur.execute("SELECT _id FROM jid WHERE raw_string=?", (raw,)).fetchone()
        if row is None:
            user, _, server = raw.partition("@")
            row_id = jid_table.insert(user=user, server=server, agent=0, device=0, type=server_type.get(server, 0), raw_string=raw)
        else:
            row_id = row[0]
        jid_cache[raw] = row_id
        return row_id

    def linked_rows(row_id: int) -> set[int]:
        rows = {row_id}
        if has_jid_map:
            rows |= {r[0] for r in cur.execute("SELECT lid_row_id FROM jid_map WHERE jid_row_id=?", (row_id,))}
            rows |= {r[0] for r in cur.execute("SELECT jid_row_id FROM jid_map WHERE lid_row_id=?", (row_id,))}
        return rows

    def lid_of(row_id: int) -> int:
        if not has_jid_map:
            return row_id
        row = cur.execute("SELECT lid_row_id FROM jid_map WHERE jid_row_id=?", (row_id,)).fetchone()
        return row[0] if row else row_id

    # ---- chats
    sessions = {}
    chat_for_session: dict[int, int] = {}
    created: set[int] = set()
    session_cols = _columns(ios, "ZWACHATSESSION")
    name_col = "ZPARTNERNAME" if "ZPARTNERNAME" in session_cols else "NULL"
    archived_col = "ZARCHIVED" if "ZARCHIVED" in session_cols else "0"
    placeholders = ",".join("?" * len(CHAT_SESSION_TYPES))
    for pk, raw, name, stype, archived in ios.execute(
        f"SELECT Z_PK, ZCONTACTJID, {name_col}, ZSESSIONTYPE, {archived_col} FROM ZWACHATSESSION "
        f"WHERE ZSESSIONTYPE IN ({placeholders}) AND ZCONTACTJID IS NOT NULL",
        CHAT_SESSION_TYPES,
    ):
        sessions[pk] = stype
        row = jid_row(raw)
        candidates = linked_rows(row)
        marks = ",".join("?" * len(candidates))
        existing = cur.execute(f"SELECT _id FROM chat WHERE jid_row_id IN ({marks})", tuple(candidates)).fetchone()
        if existing:
            chat_for_session[pk] = existing[0]
            continue
        target = row
        for candidate in candidates:
            if cur.execute("SELECT server FROM jid WHERE _id=?", (candidate,)).fetchone()[0] == "lid":
                target = candidate
        chat_id = chat_table.insert(
            jid_row_id=target, hidden=0, subject=name if stype in GROUP_SESSION_TYPES else None,
            archived=int(archived or 0), created_timestamp=0, sort_timestamp=0,
        )
        chat_for_session[pk] = chat_id
        created.add(chat_id)
    report.chats, report.new_chats = len(chat_for_session), len(created)

    member_cols = _columns(ios, "ZWAGROUPMEMBER")
    member_jid = (
        dict(ios.execute("SELECT Z_PK, ZMEMBERJID FROM ZWAGROUPMEMBER")) if "ZMEMBERJID" in member_cols else {}
    )

    # ---- messages
    had_messages = {r[0] for r in cur.execute("SELECT DISTINCT chat_row_id FROM message")}
    seen = {tuple(r) for r in cur.execute("SELECT chat_row_id, from_me, key_id FROM message")}
    rows = ios.execute(_ios_select(ios)).fetchall()
    total = len(rows)

    # Room for the old messages: they take sort_id 1..N, the existing (newer) ones move up.
    offset = total + 10
    cur.execute("UPDATE message SET sort_id = sort_id + ? WHERE sort_id > 0", (offset,))
    chat_cols = _columns(db, "chat")
    all_pointers = (
        ("last_message_row_id", "last_message_sort_id"),
        ("display_message_row_id", "display_message_sort_id"),
        ("last_read_message_row_id", "last_read_message_sort_id"),
        ("last_read_receipt_sent_message_row_id", "last_read_receipt_sent_message_sort_id"),
    )
    pointer_pairs = [p for p in all_pointers if p[0] in chat_cols and p[1] in chat_cols]
    for _, sort_col in pointer_pairs:
        cur.execute(f"UPDATE chat SET {sort_col} = {sort_col} + ? WHERE {sort_col} > 0", (offset,))

    planner = _MediaPlanner(media_out)
    newest: dict[int, tuple[int, int, int]] = {}
    oldest: dict[int, int] = {}
    pending_quotes: list[tuple[int, int, str]] = []
    done_pk: set[int] = set()
    sort_id = 0

    for index, row in enumerate(rows):
        if index % 200 == 0:
            progress(index, total, "Converting messages…")
        pk = row["Z_PK"]
        if pk in done_pk:
            continue
        done_pk.add(pk)

        session = row["ZCHATSESSION"]
        chat = chat_for_session.get(session)
        ios_type = row["ZMESSAGETYPE"]
        text = row["ZTEXT"] or None
        if chat is None:
            report.skipped_chats += 1
            continue
        if ios_type == IOS_SYSTEM:
            report.skipped_system += 1
            continue
        if ios_type == IOS_DELETED:
            report.skipped_deleted += 1
            continue
        kind = IOS_TO_ANDROID.get(ios_type)
        if kind is None and text:
            kind = TEXT  # unknown type with some text: better to keep the text than lose it
        if kind is None or (kind == TEXT and not text):
            report.skipped_other += 1
            continue

        from_me = int(row["ZISFROMME"] or 0)
        key = row["ZSTANZAID"] or f"MIGRATED{pk:012d}"
        if (chat, from_me, key) in seen:
            report.duplicates += 1
            continue
        seen.add((chat, from_me, key))

        timestamp = int((row["ZMESSAGEDATE"] + APPLE_EPOCH) * 1000)
        sender = 0
        if not from_me and sessions[session] in GROUP_SESSION_TYPES:
            from_jid = row["ZFROMJID"]
            raw = member_jid.get(row["ZGROUPMEMBER"]) or (from_jid if from_jid and not from_jid.endswith("@g.us") else None)
            sender = lid_of(jid_row(raw)) if raw else 0

        title = row["ZTITLE"] or None
        vcard_strings: list[str] = []
        if kind == CONTACT:
            vcard_strings = [v for v in (row["ZVCARDSTRING"] or "").split("_$!<VCard-Separator>!$_") if v.strip()]
            names = (row["ZVCARDNAME"] or "").split("_$!<Name-Separator>!$_")
            if not vcard_strings:
                report.empty_contacts += 1
                continue
            if len(vcard_strings) > 1:
                kind = CONTACTS
            text = names[0] if names and names[0] else text
        elif kind in MEDIA_TYPES and kind != DOCUMENT:
            text = text or title
        elif kind == LOCATION:
            text = None

        source_path = row["ZMEDIALOCALPATH"]
        voice = kind == AUDIO and (
            (source_path or "").lower().endswith(".opus") or "opus" in (row["ZVCARDSTRING"] or "").lower()
        )
        quoted_stanza = _quoted_stanza(row["ZMETADATA"])
        sort_id += 1
        message_id = message_table.insert(
            chat_row_id=chat, from_me=from_me, key_id=key, sender_jid_row_id=sender,
            status=13 if from_me else 0, broadcast=0, origin=1 if voice else 0,
            timestamp=timestamp, received_timestamp=timestamp, receipt_server_timestamp=-1,
            message_type=kind, text_data=text, starred=int(row["ZSTARRED"] or 0),
            lookup_tables=lookup.get((kind, bool(quoted_stanza)), 0), sort_id=sort_id,
        )
        report.inserted[KIND_LABELS[kind]] += 1
        newest[chat] = (timestamp, message_id, sort_id)
        oldest.setdefault(chat, timestamp)
        if quoted_stanza:
            pending_quotes.append((message_id, chat, quoted_stanza))

        if kind in MEDIA_TYPES:
            _store_media(row, kind, voice, from_me, chat, message_id, timestamp, title, media_index, planner,
                         media_table, thumb_table, hash_thumb_table, report)
        elif kind == LOCATION and row["ZLATITUDE"] is not None and location_table:
            location_table.insert(
                message_row_id=message_id, chat_row_id=chat, latitude=row["ZLATITUDE"],
                longitude=row["ZLONGITUDE"], place_name=title,
            )
        elif kind in (CONTACT, CONTACTS) and vcard_table:
            for vcard in vcard_strings:
                vcard_table.insert(message_row_id=message_id, vcard=vcard)

    # ---- quotes: the quoted message must really exist
    progress(0, 0, "Linking replies…")
    if quoted_table and pending_quotes:
        cur.execute("CREATE INDEX wa_transfer_tmp_key ON message(key_id)")
        for message_id, chat, stanza in pending_quotes:
            parent = cur.execute(
                "SELECT chat_row_id, from_me, sender_jid_row_id, key_id, timestamp, message_type, origin, text_data "
                "FROM message WHERE key_id=? AND chat_row_id=?",
                (stanza, chat),
            ).fetchone()
            if parent is None:
                continue
            quoted_table.insert(
                message_row_id=message_id, chat_row_id=chat, parent_message_chat_row_id=parent[0],
                from_me=parent[1], sender_jid_row_id=parent[2], key_id=parent[3], timestamp=parent[4],
                message_type=parent[5], origin=parent[6], text_data=parent[7], lookup_tables=0,
            )
            report.quotes += 1
        cur.execute("DROP INDEX wa_transfer_tmp_key")

    # ---- pointers of chats that had no messages
    progress(0, 0, "Reordering conversations…")
    for chat, (timestamp, message_id, sid) in newest.items():
        if chat in had_messages:
            continue
        sets = ["sort_timestamp=?"]
        args: list = [timestamp]
        for col in ("unseen_message_count", "unseen_row_count"):
            if col in chat_cols:
                sets.append(f"{col}=0")
        for row_col, sort_col in all_pointers[:3]:
            if row_col in chat_cols:
                sets.append(f"{row_col}=?")
                args.append(message_id)
            if sort_col in chat_cols:
                sets.append(f"{sort_col}=?")
                args.append(sid)
        cur.execute(f"UPDATE chat SET {', '.join(sets)} WHERE _id=?", (*args, chat))
        if "created_timestamp" in chat_cols:
            cur.execute("UPDATE chat SET created_timestamp=? WHERE _id=? AND created_timestamp=0", (oldest[chat], chat))

    # ---- renumber the whole timeline chronologically (the internal row with chat -1 stays as it is)
    cur.execute("CREATE TEMP TABLE wa_transfer_order (_id INTEGER PRIMARY KEY, n INTEGER)")
    cur.execute(
        "INSERT INTO wa_transfer_order SELECT _id, row_number() OVER (ORDER BY timestamp, _id) "
        "FROM message WHERE chat_row_id > 0"
    )
    cur.execute(
        "UPDATE message SET sort_id = (SELECT n FROM wa_transfer_order o WHERE o._id = message._id) "
        "WHERE chat_row_id > 0"
    )
    for row_col, sort_col in pointer_pairs:
        cur.execute(
            f"UPDATE chat SET {sort_col} = (SELECT sort_id FROM message WHERE _id = chat.{row_col}) "
            f"WHERE {row_col} > 0 AND EXISTS (SELECT 1 FROM message WHERE _id = chat.{row_col})"
        )
    cur.execute("DROP TABLE wa_transfer_order")
    db.commit()
    return report


def _store_media(row, kind, voice, from_me, chat, message_id, timestamp, title, media_index, planner,
                 media_table, thumb_table, hash_thumb_table, report: Report) -> None:
    source_path = row["ZMEDIALOCALPATH"]
    source = media_index.get(media_key(source_path)) if source_path else None
    mime_hint = row["ZVCARDSTRING"]
    ext = _extension(source_path, mime_hint, kind)
    mime = _mime(mime_hint, ext, kind)
    duration = int(row["ZMOVIEDURATION"] or 0)

    if source is None or not source.is_file():
        # The file is not in the backup (never downloaded on the iPhone): the message stays, without the file.
        media_table.insert(
            message_row_id=message_id, chat_row_id=chat, transferred=0, file_path=None, file_size=0,
            mime_type=mime, media_name=title if kind == DOCUMENT else None, media_duration=duration,
            message_url=row["ZMEDIAURL"], autotransfer_retry_enabled=0,
        )
        report.media_missing += 1
        return

    when = datetime.fromtimestamp(timestamp / 1000)
    relative = planner.relative_path(kind, voice, bool(from_me), when, ext, title)
    file_hash, size = planner.copy(source, relative, timestamp)
    media_table.insert(
        message_row_id=message_id, chat_row_id=chat, autotransfer_retry_enabled=0, transferred=1,
        transcoded=0, file_path=f"Media/{relative}", file_size=size, file_length=size, mime_type=mime,
        media_name=title if kind == DOCUMENT else os.path.basename(relative), file_hash=file_hash,
        media_duration=duration, message_url=row["ZMEDIAURL"], width=0, height=0,
        has_streaming_sidecar=0, suspicious_content=0, is_animated_sticker=0,
    )
    report.media_copied += 1
    report.media_bytes += size

    thumb = media_index.get(media_key(row["ZTHUMBNAILLOCALPATH"])) if row["ZTHUMBNAILLOCALPATH"] else None
    if thumb is None and source_path:
        thumb = media_index.get(media_key(os.path.splitext(source_path)[0] + ".thumb"))
    if thumb is not None and thumb.is_file() and thumb.stat().st_size <= MAX_THUMBNAIL_BYTES:
        data = thumb.read_bytes()
        if data[:3] == b"\xff\xd8\xff":  # JPEG only
            if thumb_table:
                thumb_table.insert(message_row_id=message_id, thumbnail=data)
            if hash_thumb_table:
                hash_thumb_table.conn.execute(
                    f'INSERT OR IGNORE INTO "{hash_thumb_table.name}" (media_hash, thumbnail) VALUES (?, ?)',
                    (file_hash, data),
                )
