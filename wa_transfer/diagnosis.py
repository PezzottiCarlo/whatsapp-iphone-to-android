"""Measurements on the databases, to understand a failure without seeing the chats.

Only counts and technical names come out: no text, phone numbers or people's names.
"""

from __future__ import annotations

import re
import sqlite3
from pathlib import Path

MEASURES = (
    ("jid (server, type, count)", "SELECT server, type, count(*) FROM jid GROUP BY 1, 2"),
    ("jid_map, rows", "SELECT count(*) FROM jid_map"),
    ("chats per server", "SELECT j.server, count(*) FROM chat c LEFT JOIN jid j ON j._id = c.jid_row_id GROUP BY 1"),
    (
        "individual chats on the number, with no linked LID",
        "SELECT count(*) FROM chat c JOIN jid j ON j._id = c.jid_row_id WHERE j.server = 's.whatsapp.net' "
        "AND NOT EXISTS (SELECT 1 FROM jid_map m WHERE m.jid_row_id = j._id)",
    ),
    (
        "people with two chats, one on the number and one on the LID",
        "SELECT count(*) FROM jid_map m JOIN chat a ON a.jid_row_id = m.lid_row_id JOIN chat b ON b.jid_row_id = m.jid_row_id",
    ),
    ("group participants, rows", "SELECT count(*) FROM group_participant_user"),
    (
        "messages (total, max _id, min sort_id, max sort_id, distinct sort_id)",
        "SELECT count(*), max(_id), min(sort_id), max(sort_id), count(DISTINCT sort_id) FROM message",
    ),
    ("internal row with chat -1 (_id, sort_id)", "SELECT _id, sort_id FROM message WHERE chat_row_id < 0"),
    (
        "chats whose pointer does not indicate the latest message (by _id, by sort_id)",
        "WITH newest AS (SELECT chat_row_id, max(_id) AS id, max(sort_id) AS sort FROM message GROUP BY 1) "
        "SELECT sum(c.last_message_row_id IS NOT n.id), sum(c.last_message_sort_id IS NOT n.sort) "
        "FROM chat c JOIN newest n ON n.chat_row_id = c._id",
    ),
    ("objects in the database", "SELECT type, count(*) FROM sqlite_master GROUP BY 1"),
)

MEASURES_SINCE = (
    (
        "new messages (chat server, from me, status, type, count)",
        "SELECT j.server, m.from_me, m.status, m.message_type, count(*) FROM message m "
        "LEFT JOIN chat c ON c._id = m.chat_row_id LEFT JOIN jid j ON j._id = c.jid_row_id "
        "WHERE m.timestamp > ? GROUP BY 1, 2, 3, 4",
    ),
    (
        "new messages (min _id, max _id, min sort_id, max sort_id)",
        "SELECT min(_id), max(_id), min(sort_id), max(sort_id) FROM message WHERE timestamp > ?",
    ),
    (
        "chats created after the transfer, per server",
        "SELECT j.server, count(*) FROM chat c LEFT JOIN jid j ON j._id = c.jid_row_id WHERE c.created_timestamp > ? GROUP BY 1",
    ),
)


def _measure(db: sqlite3.Connection, label: str, sql: str, args: tuple = ()) -> str:
    try:
        return f"{label}: {db.execute(sql, args).fetchall()}"
    except sqlite3.Error as exc:
        return f"{label}: unavailable ({exc})"


def _props(db: sqlite3.Connection) -> str:
    """The names of the internal settings; of the values, only short ones like 0 or 1."""
    try:
        rows = db.execute("SELECT key, value FROM props ORDER BY key").fetchall()
    except sqlite3.Error as exc:
        return f"props: unavailable ({exc})"
    shown = [f"{re.sub(r'[0-9]{6,}', '#', str(k))}={v if len(str(v)) <= 4 else '…'}" for k, v in rows]
    return "props: " + ", ".join(shown)


def describe(database: Path) -> list[str]:
    db = sqlite3.connect(f"file:{database}?mode=ro", uri=True)
    try:
        return [_measure(db, label, sql) for label, sql in MEASURES] + [_props(db)]
    finally:
        db.close()


def newest_timestamp(database: Path) -> int | None:
    db = sqlite3.connect(f"file:{database}?mode=ro", uri=True)
    try:
        return db.execute("SELECT max(timestamp) FROM message WHERE chat_row_id > 0").fetchone()[0]
    finally:
        db.close()


def describe_since(database: Path, since_ms: int) -> list[str]:
    """Where the messages received or sent after the transfer ended up."""
    db = sqlite3.connect(f"file:{database}?mode=ro", uri=True)
    try:
        return [_measure(db, label, sql, (since_ms,)) for label, sql in MEASURES_SINCE]
    finally:
        db.close()
