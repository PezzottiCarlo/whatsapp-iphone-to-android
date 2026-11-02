"""Program entry point. The options are only for testing."""

import argparse
from pathlib import Path


def self_check(report: Path) -> None:
    """Checks that the exe contains everything: cryptography, SQLite FTS, HTTPS."""
    import sqlite3
    import ssl
    import urllib.request

    from Cryptodome.Cipher import AES  # noqa: F401
    from iphone_backup_decrypt import EncryptedBackup  # noqa: F401

    from wa_transfer import crypt15, android

    lines = []
    fake = crypt15.Backup(b"\x1a\x12\x0a\x10" + bytes(16), 4, 9, b"")
    blob = crypt15.encrypt(b"SQLite", bytes(32), fake)
    lines.append(f"crypt15 ok: {crypt15.decrypt(blob, bytes(32)).database == b'SQLite'}")
    connection = sqlite3.connect(":memory:")
    connection.execute("CREATE VIRTUAL TABLE t USING fts4(a)")
    lines.append("sqlite fts4 ok")
    lines.append(f"ssl: {ssl.OPENSSL_VERSION}")
    request = urllib.request.Request(android.PLATFORM_TOOLS_URL, method="HEAD")
    with urllib.request.urlopen(request, timeout=30) as response:
        lines.append(f"adb download reachable: {response.status}")
    report.write_text("\n".join(lines), "utf-8")


if __name__ == "__main__":
    parser = argparse.ArgumentParser()
    parser.add_argument("--workdir", type=Path, help="work folder")
    parser.add_argument("--page", type=int, help="starting page (from 0)")
    parser.add_argument("--self-check", type=Path, help="checks the exe and writes the result to this file")
    args = parser.parse_args()
    if args.self_check:
        self_check(args.self_check)
    else:
        from wa_transfer.gui import main

        main(args.workdir, args.page)
