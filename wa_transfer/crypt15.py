"""Reading and writing WhatsApp Android backups in .crypt15 format.

File layout:

    varint(header length) | protobuf header | AES-256-GCM(zlib(database)) | GCM tag (16) | md5 (16)

The header is kept as it is, replacing only the IV: it contains fields that vary
between WhatsApp versions and a regenerated backup must preserve them.
The encryption key is HKDF-SHA256 (null salt, info "backup encryption") of the
64-digit key shown by the app.
"""

from __future__ import annotations

import hashlib
import hmac
import os
import re
import zlib
from dataclasses import dataclass

from Cryptodome.Cipher import AES

_INFO = b"backup encryption"
_TAG_LEN = 16
_MD5_LEN = 16
_IV_LEN = 16
_ZLIB_LEVELS = {b"\x78\x01": 1, b"\x78\x5e": 5, b"\x78\x9c": 6, b"\x78\xda": 9}
_DEFAULT_LEVEL = 9


class Crypt15Error(Exception):
    """The file is not a valid crypt15."""


class WrongKeyError(Crypt15Error):
    """The 64-digit key does not match the backup."""


def parse_key(text: str) -> bytes:
    """Converts the 64-digit key typed by the user into the 32 bytes of the root key."""
    clean = re.sub(r"[\s\-]", "", text).lower()
    if len(clean) != 64:
        raise ValueError(f"The key must have 64 characters: you entered {len(clean)}.")
    if not re.fullmatch(r"[0-9a-f]{64}", clean):
        raise ValueError("The key only contains the digits 0-9 and the letters a-f: check that you copied it correctly.")
    return bytes.fromhex(clean)


def _derive(root: bytes) -> bytes:
    prk = hmac.new(b"\x00" * 32, root, hashlib.sha256).digest()
    return hmac.new(prk, _INFO + b"\x01", hashlib.sha256).digest()


def _read_varint(data: bytes, pos: int) -> tuple[int, int]:
    value = 0
    for shift in range(0, 64, 7):
        if pos >= len(data):
            raise Crypt15Error("Truncated file.")
        byte = data[pos]
        pos += 1
        value |= (byte & 0x7F) << shift
        if not byte & 0x80:
            return value, pos
    raise Crypt15Error("Invalid header.")


def _field(data: bytes, wanted: int) -> tuple[int, int] | None:
    """Position (start, end) of the first length-delimited protobuf field numbered `wanted`."""
    pos = 0
    while pos < len(data):
        tag, pos = _read_varint(data, pos)
        number, wire = tag >> 3, tag & 7
        if wire == 0:
            _, pos = _read_varint(data, pos)
        elif wire == 1:
            pos += 8
        elif wire == 5:
            pos += 4
        elif wire == 2:
            length, pos = _read_varint(data, pos)
            if pos + length > len(data):
                raise Crypt15Error("Invalid header.")
            if number == wanted:
                return pos, pos + length
            pos += length
        else:
            raise Crypt15Error("Invalid header.")
    return None


@dataclass
class Backup:
    """An already decrypted crypt15 backup."""

    header: bytes
    iv_start: int
    level: int
    database: bytes

    @property
    def iv(self) -> bytes:
        return self.header[self.iv_start : self.iv_start + _IV_LEN]


def decrypt(blob: bytes, root_key: bytes) -> Backup:
    """Decrypts a msgstore.db.crypt15. Raises WrongKeyError if the key is wrong."""
    size, pos = _read_varint(blob, 0)
    header = blob[pos : pos + size]
    if len(header) != size or len(blob) < pos + size + _TAG_LEN + _MD5_LEN:
        raise Crypt15Error("Truncated file.")

    e2ee = _field(header, 3)
    if e2ee is None:
        raise Crypt15Error(
            "This backup is not encrypted with the 64-digit key (it is a crypt14 or another format). "
            "Turn on the end-to-end encrypted backup with the 64-digit key and make a new one."
        )
    iv_field = _field(header[e2ee[0] : e2ee[1]], 1)
    if iv_field is None or iv_field[1] - iv_field[0] != _IV_LEN:
        raise Crypt15Error("Invalid header: IV missing.")
    iv_start = e2ee[0] + iv_field[0]

    body_end = len(blob) - _MD5_LEN
    if hashlib.md5(blob[:body_end]).digest() != blob[body_end:]:
        raise Crypt15Error("The file is damaged (wrong checksum): make the backup again and retry.")

    cipher = AES.new(_derive(root_key), AES.MODE_GCM, nonce=header[iv_start : iv_start + _IV_LEN])
    try:
        compressed = cipher.decrypt_and_verify(blob[pos + size : body_end - _TAG_LEN], blob[body_end - _TAG_LEN : body_end])
    except ValueError as exc:
        raise WrongKeyError(
            "The key does not open this backup. Check the 64 digits again; if they are right, "
            "the backup on the phone was created with a different key: make it again."
        ) from exc

    return Backup(header, iv_start, _ZLIB_LEVELS.get(compressed[:2], _DEFAULT_LEVEL), zlib.decompress(compressed))


def _encode_varint(value: int) -> bytes:
    out = bytearray()
    while value >= 0x80:
        out.append(value & 0x7F | 0x80)
        value >>= 7
    out.append(value)
    return bytes(out)


def encrypt(database: bytes, root_key: bytes, reference: Backup, iv: bytes | None = None) -> bytes:
    """Encrypts `database` reusing the reference backup's header with a new IV."""
    iv = iv if iv is not None else os.urandom(_IV_LEN)
    header = reference.header[: reference.iv_start] + iv + reference.header[reference.iv_start + _IV_LEN :]
    cipher = AES.new(_derive(root_key), AES.MODE_GCM, nonce=iv)
    body, tag = cipher.encrypt_and_digest(zlib.compress(database, reference.level))
    out = _encode_varint(len(header)) + header + body + tag
    return out + hashlib.md5(out).digest()
