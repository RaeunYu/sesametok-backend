"""테스트용 합성 카카오톡 데이터베이스.

여기서는 실제 계정에 접근하지 않는다. 스키마는 리더가 의존하는 컬럼을 그대로
반영하며, 데이터베이스는 프로덕션 코드가 계산하는 것과 같은 PBKDF2 유도
패스프레이즈로 기록한다. 따라서 카카오톡 설치 없이 읽기 경로를 종단 간 검증할 수
있다.
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
            # 100: 이름 없는 1:1 방 -> 상대 닉네임
            (100, 2, None, 2, 500, member_blob([500]), 0),
            # 200: 이름 없는 그룹 -> 멤버 목록
            (200, 3, None, 3, 0, member_blob([SYNTHETIC_USER_ID, 501, 502, 503]), 0),
            # 300: 이름이 명시된 방
            (300, 3, "Synthetic Team A", 3, 0, member_blob([SYNTHETIC_USER_ID, 501]), 0),
            # 400: 오픈채팅(linkId 설정됨) -> 폴링에서 제외
            (400, 3, None, 5, 0, member_blob([SYNTHETIC_USER_ID, 501]), 9001),
            # 500: 메타 제목만 있는 방
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
            # 플래그가 붙은 텍스트(0x4000 | 1)는 자리표시자가 아니라 텍스트로 남아야 한다
            (300, 35, 501, FLAGGED | TEXT, "flagged text", 1767226380.0, None, None),
            # 플래그가 붙은 사진은 원시 타입을 보존한 채 자리표시자가 된다
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
    """*path* 에 암호화된 합성 데이터베이스를 생성한다."""

    path.parent.mkdir(parents=True, exist_ok=True)
    connection = sqlcipher.connect(str(path))
    # 프로덕션과 같은 규칙: 256-hex secure_key는 패스프레이즈로 쓴다.
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
    """*root* 안에 *유도된* 파일 이름으로 데이터베이스를 생성한다."""

    return write_synthetic_database(
        root / f"{database_name(user_id, uuid)}{suffix}", user_id=user_id, uuid=uuid
    )


def connect_synthetic(
    path: Path,
    *,
    user_id: int = SYNTHETIC_USER_ID,
    uuid: str = SYNTHETIC_UUID,
):
    """프로덕션의 PRAGMA 순서를 그대로 따라 합성 데이터베이스를 직접 연다."""

    connection = sqlcipher.connect(str(path))
    connection.execute(f"PRAGMA key = '{secure_key(user_id, uuid)}'")
    connection.execute("PRAGMA cipher_compatibility = 3")
    connection.execute("SELECT count(*) FROM sqlite_master").fetchone()
    return connection
