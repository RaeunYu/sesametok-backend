"""Synthetic KakaoTalk databases for tests.

Nothing here touches a real account. The schema mirrors the columns the reader
depends on, and the database is written with the same PBKDF2-derived passphrase
the production code computes, so the read path is exercised end to end without a
KakaoTalk installation.
"""

from __future__ import annotations

import plistlib
from pathlib import Path

from sqlcipher3 import dbapi2 as sqlcipher

from sesametok_backend.kakao.derive import database_name, secure_key

SYNTHETIC_UUID = "00000000-1111-2222-3333-444444444444"
SYNTHETIC_USER_ID = 1000000001

TEXT = 1
PHOTO = 2
FEED = 0
FLAGGED = 0x4000

SCHEMA = """
CREATE TABLE NTChatRoom (
    chatId INTEGER PRIMARY KEY,
    type INTEGER,
    chatName TEXT,
    activeMembersCount INTEGER,
    directChatMemberUserId INTEGER,
    displayMemberIds BLOB,
    linkId INTEGER
);
CREATE TABLE NTChatMeta (
    chatId INTEGER, type INTEGER, content TEXT, revision INTEGER
);
CREATE TABLE NTUser (
    userId INTEGER PRIMARY KEY,
    friendNickName TEXT,
    displayName TEXT,
    nickName TEXT
);
CREATE TABLE NTChatMessage (
    chatId INTEGER, logId INTEGER, authorId INTEGER, type INTEGER,
    message TEXT, sentAt REAL, supplement TEXT, attachment TEXT
);
CREATE TABLE NTOpenLink (linkId INTEGER, linkName TEXT);
"""


def member_blob(user_ids: list[int]) -> bytes:
    return plistlib.dumps(user_ids, fmt=plistlib.FMT_BINARY)


def _seed(connection) -> None:
    connection.executemany(
        "INSERT INTO NTChatRoom (chatId, type, chatName, activeMembersCount, "
        "directChatMemberUserId, displayMemberIds, linkId) VALUES (?,?,?,?,?,?,?)",
        [
            # 100: 1:1 room with no name -> peer nickname
            (100, 2, None, 2, 500, member_blob([500]), 0),
            # 200: group with no name -> member list
            (200, 3, None, 3, 0, member_blob([SYNTHETIC_USER_ID, 501, 502, 503]), 0),
            # 300: explicitly named
            (300, 3, "Synthetic Team A", 3, 0, member_blob([SYNTHETIC_USER_ID, 501]), 0),
            # 400: open chat (linkId set) -> excluded from polling
            (400, 3, None, 5, 0, member_blob([SYNTHETIC_USER_ID, 501]), 9001),
            # 500: meta title only
            (500, 3, None, 4, 0, member_blob([SYNTHETIC_USER_ID, 502, 503]), 0),
        ],
    )
    connection.executemany(
        "INSERT INTO NTUser (userId, friendNickName, displayName, nickName) VALUES (?,?,?,?)",
        [
            (500, "Peer", "PeerDisplay", "PeerNick"),
            (501, None, "Member A", "A"),
            (502, None, None, "Member B"),
            (503, "Member C", None, None),
            (SYNTHETIC_USER_ID, None, None, "Me"),
        ],
    )
    connection.executemany(
        "INSERT INTO NTChatMeta (chatId, type, content, revision) VALUES (?,?,?,?)",
        [
            (500, 3, "Meta Title v1", 1),
            (500, 3, "Meta Title v2", 2),
            (500, 1, "ignored wrong type", 9),
            (300, 3, "ignored because chatName wins", 5),
        ],
    )
    connection.executemany(
        "INSERT INTO NTOpenLink (linkId, linkName) VALUES (?,?)",
        [(9001, "Synthetic Open Room")],
    )
    connection.executemany(
        "INSERT INTO NTChatMessage (chatId, logId, authorId, type, message, sentAt, "
        "supplement, attachment) VALUES (?,?,?,?,?,?,?,?)",
        [
            (100, 10, 500, TEXT, "hello", 1767225600.0, None, None),
            (100, 11, SYNTHETIC_USER_ID, TEXT, "answer", 1767225660.0,
             None, '{"src_logId": 10}'),
            (100, 12, 500, PHOTO, None, 1767225720.0, None, '{"path":"x"}'),
            (100, 13, 500, FEED, '{"feedType":"invite","members":[501]}',
             1767225780.0, None, None),
            (200, 20, 501, TEXT, "group message", 1767225840.0,
             None, '{"logId": 99}'),
            (200, 21, 502, TEXT, b"\xff\xfe not utf8", 1767225900.0, None, None),
            (300, 30, 501, TEXT, "team message", 1767225960.0, None, None),
            (300, 31, 501, TEXT, None, 1767226020.0, None, None),
            (300, 32, 501, TEXT, "via supplement", 1767226080.0,
             '{"reply":{"srcLogId":30}}', None),
            (300, 33, 501, TEXT, "attachment wins", 1767226140.0,
             '{"src_log_id": 31}', '{"parentLogId": 30}'),
            (300, 34, 501, TEXT, "self reference", 1767226200.0,
             None, '{"srcLogId": 34}'),
            # flagged text (0x4000 | 1) must stay text, not become a placeholder
            (300, 35, 501, FLAGGED | TEXT, "flagged text", 1767226380.0, None, None),
            # flagged photo becomes a placeholder with the raw type preserved
            (300, 36, 501, FLAGGED | PHOTO, None, 1767226440.0, None, None),
            (400, 40, 501, TEXT, "open chat message", 1767226260.0, None, None),
            (500, 50, 502, TEXT, "meta room message", 1767226320.0, None, None),
        ],
    )


def write_synthetic_database(
    path: Path,
    *,
    user_id: int = SYNTHETIC_USER_ID,
    uuid: str = SYNTHETIC_UUID,
) -> Path:
    """Create an encrypted synthetic database at *path*."""

    path.parent.mkdir(parents=True, exist_ok=True)
    connection = sqlcipher.connect(str(path))
    # Same convention as production: the 256-hex secure_key is a passphrase.
    connection.execute(f"PRAGMA key = '{secure_key(user_id, uuid)}'")
    connection.execute("PRAGMA cipher_compatibility = 3")
    connection.executescript(SCHEMA)
    _seed(connection)
    connection.commit()
    connection.close()
    return path


def write_synthetic_store(
    root: Path,
    *,
    user_id: int = SYNTHETIC_USER_ID,
    uuid: str = SYNTHETIC_UUID,
    suffix: str = "",
) -> Path:
    """Create the database under the *derived* file name inside *root*."""

    return write_synthetic_database(
        root / f"{database_name(user_id, uuid)}{suffix}", user_id=user_id, uuid=uuid
    )


def connect_synthetic(
    path: Path,
    *,
    user_id: int = SYNTHETIC_USER_ID,
    uuid: str = SYNTHETIC_UUID,
):
    """Open a synthetic database directly, mirroring the production PRAGMA order."""

    connection = sqlcipher.connect(str(path))
    connection.execute(f"PRAGMA key = '{secure_key(user_id, uuid)}'")
    connection.execute("PRAGMA cipher_compatibility = 3")
    connection.execute("SELECT count(*) FROM sqlite_master").fetchone()
    return connection
