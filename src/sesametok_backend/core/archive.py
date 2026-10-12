"""암호화된 로컬 아카이브.

이 SQLite 파일은 전적으로 이 프로젝트가 소유하며, 카카오톡 것이 아니다.
SQLCipher(ADR-004)로 암호화하고 32바이트 키를 로우 키 리터럴(``x'<hex>'``)로
전달한다. 이는 *랜덤* 키에 맞는 형식이다. 카카오톡 읽기 경로는 대신 유도된
패스프레이즈를 넘긴다(``kakao/cipher.py`` 참고).

스키마는 여기서 정의한다. ADR-003이 ``original_message_type`` 과
``is_placeholder`` 를 추가한다. 쓰기는 폴러와 발신 워커가 담당하며, API는
읽기만 한다.
"""

from __future__ import annotations

import json
import logging
from collections.abc import Iterable, Sequence
from dataclasses import dataclass
from datetime import datetime, timezone
from pathlib import Path

from sqlcipher3 import dbapi2 as sqlcipher

from ..config import Settings
from ..fsutil import ensure_private_dir, tighten
from ..models import Event, Message, Room
from ..security.archive_key import pragma_literal
from .snapshot import MessageDiff, RoomDiff, diff_messages, diff_rooms, room_fingerprint

logger = logging.getLogger("sesametok_backend.core.archive")

CHUNK = 400
DEFAULT_PAGE_LIMIT = 100
MAX_PAGE_LIMIT = 1000

# 컬럼 순서는 Message.fingerprint()와 정확히 일치해야 한다.
FINGERPRINT_COLUMNS = (
    "chat_name",
    "chat_type",
    "sender_id",
    "sender_nickname",
    "timestamp",
    "text",
    "message_type",
    "reply_to_message_id",
    "is_feed",
    "original_message_type",
    "is_placeholder",
)
SELECT_MESSAGE = "SELECT chat_id, message_id, " + ", ".join(FINGERPRINT_COLUMNS) + " FROM messages"

INSERT_MESSAGE = (
    "INSERT INTO messages (account_hash, chat_id, message_id, chat_name, chat_type, "
    "sender_id, sender_nickname, timestamp, text, message_type, reply_to_message_id, "
    "is_feed, original_message_type, is_placeholder) VALUES (?,?,?,?,?,?,?,?,?,?,?,?,?,?)"
)
UPDATE_MESSAGE = (
    "UPDATE messages SET chat_name=?, chat_type=?, sender_id=?, sender_nickname=?, "
    "timestamp=?, text=?, message_type=?, reply_to_message_id=?, is_feed=?, "
    "original_message_type=?, is_placeholder=? "
    "WHERE account_hash=? AND chat_id=? AND message_id=?"
)

SCHEMA = """
CREATE TABLE IF NOT EXISTS messages (
    account_hash TEXT NOT NULL,
    chat_id TEXT NOT NULL,
    chat_name TEXT NOT NULL,
    chat_type TEXT NOT NULL,
    message_id TEXT NOT NULL,
    sender_id TEXT NOT NULL,
    sender_nickname TEXT NOT NULL,
    timestamp TEXT NOT NULL,
    text TEXT NOT NULL,
    message_type TEXT NOT NULL,
    reply_to_message_id TEXT,
    is_feed INTEGER NOT NULL DEFAULT 0,
    original_message_type TEXT,
    is_placeholder INTEGER NOT NULL DEFAULT 0,
    PRIMARY KEY (account_hash, chat_id, message_id)
);
CREATE TABLE IF NOT EXISTS chats (
    chat_id TEXT PRIMARY KEY,
    chat_name TEXT NOT NULL,
    chat_type TEXT NOT NULL,
    last_message_at TEXT
);
CREATE TABLE IF NOT EXISTS events (
    seq INTEGER PRIMARY KEY AUTOINCREMENT,
    kind TEXT NOT NULL,
    payload TEXT NOT NULL,
    created_at TEXT NOT NULL
);
CREATE TABLE IF NOT EXISTS send_requests (
    client_request_id TEXT PRIMARY KEY,
    chat_id TEXT NOT NULL,
    mode TEXT NOT NULL,
    result TEXT NOT NULL,
    requested_at TEXT NOT NULL,
    completed_at TEXT
);
CREATE TABLE IF NOT EXISTS poll_state (
    id INTEGER PRIMARY KEY CHECK (id = 1),
    cursor TEXT,
    last_success_at TEXT
);
CREATE TABLE IF NOT EXISTS webhooks (
    id TEXT PRIMARY KEY,
    url TEXT NOT NULL,
    secret TEXT NOT NULL,
    kinds TEXT NOT NULL,
    active INTEGER NOT NULL DEFAULT 1,
    cursor_seq INTEGER NOT NULL DEFAULT 0,
    created_at TEXT NOT NULL,
    last_success_at TEXT,
    last_error TEXT
);
CREATE INDEX IF NOT EXISTS idx_messages_chat_ts ON messages(chat_id, timestamp, message_id);
CREATE INDEX IF NOT EXISTS idx_messages_account_ts ON messages(account_hash, timestamp);
CREATE INDEX IF NOT EXISTS idx_events_created ON events(created_at);
"""


def now_utc() -> str:
    return datetime.now(tz=timezone.utc).isoformat()


def _chunks(items: Sequence[str], size: int = CHUNK) -> Iterable[Sequence[str]]:
    for start in range(0, len(items), size):
        yield items[start : start + size]


@dataclass(frozen=True)
class MessagesPage:
    messages: tuple[Message, ...]
    has_more: bool
    next_before: str | None


def row_to_message(row: Sequence) -> Message:
    return Message(
        message_id=str(row[1]),
        chat_id=str(row[0]),
        chat_name=str(row[2]),
        chat_type=str(row[3]),
        sender_id=str(row[4]),
        sender_nickname=str(row[5]),
        timestamp=str(row[6]),
        text=str(row[7]),
        message_type=str(row[8]),
        reply_to_message_id=row[9],
        is_feed=bool(row[10]),
        original_message_type=row[11],
        is_placeholder=bool(row[12]),
    )


class Archive:
    """암호화된 아카이브에 대한 연결 하나.

    ``Archive`` 인스턴스는 스레드 간에 공유하지 않는다. 각 스레드가 자신의 연결을
    연다. WAL 모드 덕분에 폴러가 쓰는 동안 API가 읽을 수 있다.
    """

    def __init__(self, connection, *, account_hash: str, path: Path) -> None:
        self._con = connection
        self.account_hash = account_hash
        self.path = path

    # ------------------------------------------------------------- 생명주기

    @classmethod
    def open(
        cls,
        settings: Settings,
        key: bytes,
        *,
        account_hash: str,
        initialize: bool = True,
    ) -> "Archive":
        """아카이브를 연다. ``initialize=False`` 면 스키마/WAL 설정을 건너뛴다.

        모든 스레드가 자신의 연결을 연다. 쓰기는 폴러와 발신 워커만 한다.
        ``initialize`` 가 False면 데이터베이스 파일이 이미 존재해야 한다.
        """

        path = settings.archive_path
        ensure_private_dir(path.parent)
        connection = sqlcipher.connect(str(path), timeout=5.0)
        connection.execute(f'PRAGMA key = "{pragma_literal(key)}"')
        connection.execute("PRAGMA busy_timeout = 5000")
        connection.execute("SELECT count(*) FROM sqlite_master").fetchone()
        if initialize:
            connection.execute("PRAGMA journal_mode = WAL")
            connection.executescript(SCHEMA)
            connection.commit()
            tighten(path)
        return cls(connection, account_hash=account_hash, path=path)

    def close(self) -> None:
        try:
            self._con.close()
        except sqlcipher.Error:  # pragma: no cover - 닫기는 최선 노력이다
            pass

    def commit(self) -> None:
        self._con.commit()

    # ------------------------------------------------------------------ 방

    def _existing_room_fingerprints(self) -> dict[str, tuple]:
        rows = self._con.execute("SELECT chat_id, chat_name, chat_type FROM chats").fetchall()
        return {str(row[0]): (str(row[1]), str(row[2])) for row in rows}

    def upsert_rooms(self, rooms: Sequence[Room]) -> RoomDiff:
        existing = self._existing_room_fingerprints()
        diff = diff_rooms(existing, rooms)
        if not rooms:
            return diff

        current_last = {
            str(row[0]): row[1]
            for row in self._con.execute("SELECT chat_id, last_message_at FROM chats").fetchall()
        }
        for room in rooms:
            previous = current_last.get(room.chat_id)
            last = room.last_message_at
            if previous is not None and last is not None:
                last = max(previous, last)
            elif last is None:
                last = previous
            self._con.execute(
                "INSERT INTO chats (chat_id, chat_name, chat_type, last_message_at) "
                "VALUES (?,?,?,?) ON CONFLICT(chat_id) DO UPDATE SET "
                "chat_name=excluded.chat_name, chat_type=excluded.chat_type, "
                "last_message_at=excluded.last_message_at",
                (room.chat_id, room.chat_name, room.chat_type, last),
            )
        return diff

    def rooms(self) -> list[Room]:
        rows = self._con.execute(
            "SELECT c.chat_id, c.chat_name, c.chat_type, c.last_message_at, "
            "(SELECT count(*) FROM messages m "
            " WHERE m.account_hash = ? AND m.chat_id = c.chat_id) "
            "FROM chats c ORDER BY c.chat_id",
            (self.account_hash,),
        ).fetchall()
        return [
            Room(
                chat_id=str(row[0]),
                chat_name=str(row[1]),
                chat_type=str(row[2]),
                last_message_at=row[3],
                message_count=int(row[4]),
            )
            for row in rows
        ]

    def room(self, chat_id: str) -> Room | None:
        row = self._con.execute(
            "SELECT c.chat_id, c.chat_name, c.chat_type, c.last_message_at, "
            "(SELECT count(*) FROM messages m "
            " WHERE m.account_hash = ? AND m.chat_id = c.chat_id) "
            "FROM chats c WHERE c.chat_id = ?",
            (self.account_hash, chat_id),
        ).fetchone()
        if row is None:
            return None
        return Room(
            chat_id=str(row[0]),
            chat_name=str(row[1]),
            chat_type=str(row[2]),
            last_message_at=row[3],
            message_count=int(row[4]),
        )

    def count_rooms(self) -> int:
        return int(self._con.execute("SELECT count(*) FROM chats").fetchone()[0])

    # --------------------------------------------------------------- 메시지

    def existing_fingerprints(
        self, messages: Sequence[Message]
    ) -> dict[tuple[str, str], tuple]:
        result: dict[tuple[str, str], tuple] = {}
        by_chat: dict[str, list[str]] = {}
        for message in messages:
            by_chat.setdefault(message.chat_id, []).append(message.message_id)

        for chat_id, message_ids in by_chat.items():
            for chunk in _chunks(message_ids):
                placeholders = ",".join("?" * len(chunk))
                rows = self._con.execute(
                    f"{SELECT_MESSAGE} WHERE account_hash = ? AND chat_id = ? "
                    f"AND message_id IN ({placeholders})",
                    (self.account_hash, chat_id, *chunk),
                ).fetchall()
                for row in rows:
                    result[(str(row[0]), str(row[1]))] = tuple(row[2:])
        return result

    def apply_message_diff(self, messages: Sequence[Message]) -> MessageDiff:
        """새 메시지를 삽입하고 변경된 메시지를 갱신한다. 변경된 내용을 반환한다."""

        if not messages:
            return MessageDiff()

        diff = diff_messages(self.existing_fingerprints(messages), messages)

        for message in diff.created:
            self._con.execute(
                INSERT_MESSAGE,
                (
                    self.account_hash,
                    message.chat_id,
                    message.message_id,
                    message.chat_name,
                    message.chat_type,
                    message.sender_id,
                    message.sender_nickname,
                    message.timestamp,
                    message.text,
                    message.message_type,
                    message.reply_to_message_id,
                    int(message.is_feed),
                    message.original_message_type,
                    int(message.is_placeholder),
                ),
            )

        for message in diff.updated:
            self._con.execute(
                UPDATE_MESSAGE,
                (
                    message.chat_name,
                    message.chat_type,
                    message.sender_id,
                    message.sender_nickname,
                    message.timestamp,
                    message.text,
                    message.message_type,
                    message.reply_to_message_id,
                    int(message.is_feed),
                    message.original_message_type,
                    int(message.is_placeholder),
                    self.account_hash,
                    message.chat_id,
                    message.message_id,
                ),
            )

        return diff

    def count_messages(self, chat_id: str | None = None) -> int:
        if chat_id is None:
            row = self._con.execute(
                "SELECT count(*) FROM messages WHERE account_hash = ?", (self.account_hash,)
            ).fetchone()
        else:
            row = self._con.execute(
                "SELECT count(*) FROM messages WHERE account_hash = ? AND chat_id = ?",
                (self.account_hash, chat_id),
            ).fetchone()
        return int(row[0])

    def messages(
        self,
        chat_id: str,
        *,
        since: str | None = None,
        until: str | None = None,
        limit: int = DEFAULT_PAGE_LIMIT,
        before: str | None = None,
    ) -> MessagesPage:
        """가장 최신 페이지부터. 페이지 안에서는 오래된 것에서 최신 순으로 반환한다.

        ``before`` 는 과거로 더 거슬러 올라가며, ``next_before`` 는 그다음 오래된
        페이지를 위한 커서다.
        """

        bounded = max(1, min(limit, MAX_PAGE_LIMIT))
        where = ["account_hash = ?", "chat_id = ?"]
        params: list[object] = [self.account_hash, chat_id]

        if since is not None:
            where.append("timestamp >= ?")
            params.append(since)
        if until is not None:
            where.append("timestamp < ?")
            params.append(until)
        if before is not None:
            anchor = self._con.execute(
                "SELECT timestamp, message_id FROM messages "
                "WHERE account_hash = ? AND chat_id = ? AND message_id = ?",
                (self.account_hash, chat_id, before),
            ).fetchone()
            if anchor is None:
                return MessagesPage(messages=(), has_more=False, next_before=None)
            where.append("(timestamp, message_id) < (?, ?)")
            params.extend([anchor[0], anchor[1]])

        sql = (
            f"{SELECT_MESSAGE} WHERE {' AND '.join(where)} "
            "ORDER BY timestamp DESC, message_id DESC LIMIT ?"
        )
        params.append(bounded + 1)
        rows = self._con.execute(sql, tuple(params)).fetchall()

        has_more = len(rows) > bounded
        rows = rows[:bounded]
        rows.reverse()
        messages = tuple(row_to_message(row) for row in rows)
        next_before = messages[0].message_id if has_more and messages else None
        return MessagesPage(messages=messages, has_more=has_more, next_before=next_before)

    def message(self, message_id: str) -> Message | None:
        chat_id, _, _ = message_id.rpartition("-")
        if not chat_id:
            return None
        row = self._con.execute(
            f"{SELECT_MESSAGE} WHERE account_hash = ? AND chat_id = ? AND message_id = ?",
            (self.account_hash, chat_id, message_id),
        ).fetchone()
        return None if row is None else row_to_message(row)

    # ----------------------------------------------------------------- 이벤트

    def append_event(self, kind: str, payload: dict, *, created_at: str | None = None) -> Event:
        moment = created_at or now_utc()
        cursor = self._con.execute(
            "INSERT INTO events (kind, payload, created_at) VALUES (?,?,?)",
            (kind, json.dumps(payload, ensure_ascii=False), moment),
        )
        return Event(seq=int(cursor.lastrowid), kind=kind, payload=payload, created_at=moment)

    def _event_row(self, row: Sequence) -> Event:
        try:
            payload = json.loads(row[2])
        except ValueError:
            payload = {}
        return Event(
            seq=int(row[0]),
            kind=str(row[1]),
            payload=payload if isinstance(payload, dict) else {},
            created_at=str(row[3]),
        )

    def events_since(self, seq: int, *, limit: int = 500) -> list[Event]:
        rows = self._con.execute(
            "SELECT seq, kind, payload, created_at FROM events WHERE seq > ? "
            "ORDER BY seq ASC LIMIT ?",
            (seq, limit),
        ).fetchall()
        return [self._event_row(row) for row in rows]

    def latest_seq(self) -> int:
        row = self._con.execute("SELECT COALESCE(max(seq), 0) FROM events").fetchone()
        return int(row[0])

    def oldest_seq(self) -> int:
        row = self._con.execute("SELECT COALESCE(min(seq), 0) FROM events").fetchone()
        return int(row[0])

    def prune_events(self, *, days: int, max_count: int, now: str | None = None) -> int:
        """*days* 보다 오래되었거나 최신 *max_count* 를 넘어선 이벤트를 삭제한다."""

        removed = 0
        if days > 0:
            cutoff = datetime.fromtimestamp(
                datetime.fromisoformat(now or now_utc()).timestamp() - days * 86_400,
                tz=timezone.utc,
            ).isoformat()
            cursor = self._con.execute("DELETE FROM events WHERE created_at < ?", (cutoff,))
            removed += max(cursor.rowcount, 0)

        if max_count > 0:
            cursor = self._con.execute(
                "DELETE FROM events WHERE seq <= "
                "(SELECT seq FROM events ORDER BY seq DESC LIMIT 1 OFFSET ?)",
                (max_count,),
            )
            removed += max(cursor.rowcount, 0)

        self.commit()
        return removed

    # ------------------------------------------------------------- 폴링 상태

    def poll_cursor(self) -> int | None:
        row = self._con.execute("SELECT cursor FROM poll_state WHERE id = 1").fetchone()
        if row is None or row[0] is None:
            return None
        try:
            return int(row[0])
        except (TypeError, ValueError):
            return None

    def last_success_at(self) -> str | None:
        row = self._con.execute("SELECT last_success_at FROM poll_state WHERE id = 1").fetchone()
        return None if row is None else row[0]

    def set_poll_state(self, *, cursor: int, last_success_at: str) -> None:
        self._con.execute(
            "INSERT INTO poll_state (id, cursor, last_success_at) VALUES (1, ?, ?) "
            "ON CONFLICT(id) DO UPDATE SET cursor=excluded.cursor, "
            "last_success_at=excluded.last_success_at",
            (str(cursor), last_success_at),
        )

    # -------------------------------------------------------------- 웹훅

    def add_webhook(self, *, webhook_id: str, url: str, secret: str, kinds: Sequence[str]) -> dict:
        self._con.execute(
            "INSERT INTO webhooks (id, url, secret, kinds, active, cursor_seq, created_at) "
            "VALUES (?,?,?,?,1,?,?)",
            (webhook_id, url, secret, ",".join(kinds), self.latest_seq(), now_utc()),
        )
        return self.get_webhook(webhook_id, include_secret=True)  # type: ignore[return-value]

    def _webhook_row(self, row: Sequence, *, include_secret: bool) -> dict:
        record = {
            "id": str(row[0]),
            "url": str(row[1]),
            "kinds": [k for k in str(row[3]).split(",") if k],
            "active": bool(row[4]),
            "cursor_seq": int(row[5]),
            "created_at": str(row[6]),
            "last_success_at": row[7],
            "last_error": row[8],
        }
        if include_secret:
            record["secret"] = str(row[2])
        return record

    def list_webhooks(self, *, include_secret: bool = False) -> list[dict]:
        rows = self._con.execute(
            "SELECT id, url, secret, kinds, active, cursor_seq, created_at, "
            "last_success_at, last_error FROM webhooks ORDER BY created_at"
        ).fetchall()
        return [self._webhook_row(row, include_secret=include_secret) for row in rows]

    def get_webhook(self, webhook_id: str, *, include_secret: bool = False) -> dict | None:
        row = self._con.execute(
            "SELECT id, url, secret, kinds, active, cursor_seq, created_at, "
            "last_success_at, last_error FROM webhooks WHERE id = ?",
            (webhook_id,),
        ).fetchone()
        return None if row is None else self._webhook_row(row, include_secret=include_secret)

    def set_webhook_active(self, webhook_id: str, active: bool) -> bool:
        cursor = self._con.execute(
            "UPDATE webhooks SET active = ? WHERE id = ?", (int(active), webhook_id)
        )
        return cursor.rowcount > 0

    def delete_webhook(self, webhook_id: str) -> bool:
        cursor = self._con.execute("DELETE FROM webhooks WHERE id = ?", (webhook_id,))
        return cursor.rowcount > 0

    def set_webhook_cursor(self, webhook_id: str, seq: int) -> None:
        self._con.execute("UPDATE webhooks SET cursor_seq = ? WHERE id = ?", (seq, webhook_id))

    def record_webhook_result(
        self, webhook_id: str, *, success: bool, error: str | None = None
    ) -> None:
        if success:
            self._con.execute(
                "UPDATE webhooks SET last_success_at = ?, last_error = NULL WHERE id = ?",
                (now_utc(), webhook_id),
            )
        else:
            self._con.execute(
                "UPDATE webhooks SET last_error = ? WHERE id = ?", (error, webhook_id)
            )

    # ---------------------------------------------------------- 발신 요청

    def get_send_request(self, client_request_id: str) -> dict | None:
        row = self._con.execute(
            "SELECT client_request_id, chat_id, mode, result, requested_at, completed_at "
            "FROM send_requests WHERE client_request_id = ?",
            (client_request_id,),
        ).fetchone()
        if row is None:
            return None
        return {
            "client_request_id": str(row[0]),
            "chat_id": str(row[1]),
            "mode": str(row[2]),
            "result": str(row[3]),
            "requested_at": str(row[4]),
            "completed_at": row[5],
        }

    def record_send_request(
        self, *, client_request_id: str, chat_id: str, mode: str, result: str
    ) -> None:
        self._con.execute(
            "INSERT INTO send_requests (client_request_id, chat_id, mode, result, requested_at) "
            "VALUES (?,?,?,?,?) ON CONFLICT(client_request_id) DO UPDATE SET "
            "result=excluded.result, completed_at=excluded.requested_at",
            (client_request_id, chat_id, mode, result, now_utc()),
        )

    def complete_send_request(self, client_request_id: str, result: str) -> None:
        self._con.execute(
            "UPDATE send_requests SET result = ?, completed_at = ? WHERE client_request_id = ?",
            (result, now_utc(), client_request_id),
        )
