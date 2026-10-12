"""카카오톡 데이터베이스를 읽고 정규화한다.

이 모듈의 처리는 모두 의도적으로 관대하다. 테이블이 없거나, 컬럼이 없거나, 행
하나를 디코딩할 수 없어도 그 보강만 저하된다. 디코딩할 수 없는 행은 세기만 하고
절대 덤프하지 않는다 — 로그에 대화 데이터가 실려서는 안 된다.

여기에는 프로젝트 결정 두 가지가 담겨 있다.

* 오픈채팅은 폴링하지 않으므로 그 방들은 스냅샷에서 제외한다.
* 이 백엔드가 표현할 수 없는 메시지 타입(이미지, 동영상, 이모티콘 등)은 원래
  타입을 보존한 채 일반 텍스트 자리표시자로 저장한다(ADR-003).
"""

from __future__ import annotations

import json
import logging
import plistlib
from dataclasses import dataclass, field
from datetime import datetime, timezone
from typing import Any, Iterable

from sqlcipher3 import dbapi2 as sqlcipher

from ..models import PLACEHOLDER_TEXT, Message, Room

logger = logging.getLogger("sesametok_backend.kakao.reader")

DIRECT_TYPES = frozenset({0, 2})
TEXT_TYPE = 1
FEED_TYPE = "type_0"
TYPE_FLAG_MASK = 0x3FFF
"""메시지 타입은 이 마스크 위에 플래그 비트를 갖는다.

실제 데이터베이스에는 ``1``/``2`` 옆에 ``16385``/``16386`` 이 있다. 상위 비트
(``0x4000``)는 플래그이고 하위 비트가 실제 메시지 타입이다. 마스킹하지 않으면
플래그가 붙은 *텍스트* 메시지(16385)가 자리표시자로 대체되어 실제 내용이
유실된다.
"""
FEED_KEYS = frozenset({"feedType", "inviter", "member", "leaver", "members"})
REPLY_KEYS = ("src_logId", "srcLogId", "parentLogId", "src_log_id")
REPLY_CONTAINERS = ("reply", "src", "origin", "parent")
MAX_GROUP_NAME_MEMBERS = 8
UNKNOWN_SENDER = "user-0"

CHAT_ID_COLUMNS = ("chatId", "chat_id", "chatID", "chatid")
LINK_NAME_COLUMNS = ("linkName", "link_name")

EPOCH_ZERO = "1970-01-01T00:00:00+00:00"


@dataclass(frozen=True)
class RawRoom:
    chat_id: str
    type_value: int | None
    chat_name: str
    active_members_count: int | None
    direct_member_user_id: int | None
    member_ids: tuple[str, ...]

    @property
    def is_direct(self) -> bool:
        if self.type_value in DIRECT_TYPES:
            return True
        if self.direct_member_user_id is not None and self.direct_member_user_id > 0:
            return True
        return self.active_members_count == 2


@dataclass(frozen=True)
class Snapshot:
    rooms: tuple[Room, ...]
    messages: tuple[Message, ...]
    skipped_rows: int = 0
    open_chat_ids: frozenset[str] = field(default_factory=frozenset)


# ------------------------------------------------------------------- 유틸리티


def _as_text(value: Any) -> str | None:
    """bytes, str, None 중 무엇이든 담을 수 있는 컬럼에서 최선을 다해 텍스트를 얻는다."""

    if value is None:
        return None
    if isinstance(value, str):
        return value
    if isinstance(value, (bytes, bytearray, memoryview)):
        try:
            return bytes(value).decode("utf-8")
        except UnicodeDecodeError:
            return None
    if isinstance(value, (int, float)):
        return str(value)
    return None


def _as_int(value: Any) -> int | None:
    if isinstance(value, bool):
        return None
    if isinstance(value, int):
        return value
    if isinstance(value, float):
        return int(value)
    if isinstance(value, str):
        try:
            return int(float(value))
        except ValueError:
            return None
    return None


def _is_truthy_link(value: Any) -> bool:
    """``linkId`` 가 방을 오픈링크 대화로 표시할 때 True."""

    if value is None:
        return False
    if isinstance(value, bool):
        return value
    if isinstance(value, int):
        return value != 0
    if isinstance(value, (bytes, bytearray, memoryview)):
        return bool(bytes(value).strip(b"\x00"))
    if isinstance(value, str):
        return bool(value.strip())
    return False


def _decode_body(value: Any) -> str:
    """메시지 본문을 디코딩하며, 디코딩할 수 없는 바이트에서는 예외를 발생시킨다.

    디코딩할 수 없는 행은 통째로 건너뛰고 개수만 세므로, 여기서는 그 예외를
    의도적으로 삼키지 않는다(``KakaoReader.messages`` 참고).
    """

    if value is None:
        return ""
    if isinstance(value, str):
        return value
    if isinstance(value, (bytes, bytearray, memoryview)):
        return bytes(value).decode("utf-8")
    return str(value)


def _load_json(value: Any) -> Any:
    text = _as_text(value)
    if text is None:
        return None
    text = text.strip()
    if not text or text[0] not in "{[":
        return None
    try:
        return json.loads(text)
    except ValueError:
        return None


def _walk_for_reply(payload: Any, depth: int = 0) -> int | None:
    if depth > 4 or payload is None:
        return None
    if isinstance(payload, dict):
        for key in REPLY_KEYS:
            if key in payload:
                found = _as_int(payload[key])
                if found is not None:
                    return found
        for container in REPLY_CONTAINERS:
            nested = payload.get(container)
            found = _walk_for_reply(nested, depth + 1)
            if found is not None:
                return found
        return None
    if isinstance(payload, list):
        for item in payload:
            found = _walk_for_reply(item, depth + 1)
            if found is not None:
                return found
    return None


def _reply_target(supplement: Any, attachment: Any, own_log_id: int | None) -> int | None:
    """``attachment`` 가 ``supplement`` 보다 우선한다. 자기 참조는 답글이 아니다."""

    for source in (attachment, supplement):
        payload = _load_json(source)
        found = _walk_for_reply(payload)
        if found is not None and found != own_log_id:
            return found
    return None


def epoch_to_rfc3339(value: Any) -> str:
    """Unix epoch 초(float 허용) → RFC3339 UTC, 초 단위로 절삭."""

    seconds = 0
    if isinstance(value, bool):
        seconds = 0
    elif isinstance(value, (int, float)):
        seconds = int(value)
    elif isinstance(value, str):
        try:
            seconds = int(float(value))
        except ValueError:
            seconds = 0
    try:
        moment = datetime.fromtimestamp(seconds, tz=timezone.utc)
    except (OverflowError, OSError, ValueError):
        moment = datetime.fromtimestamp(0, tz=timezone.utc)
    return moment.isoformat()


def _is_feed(message_type: str, body: str) -> bool:
    if message_type != FEED_TYPE:
        return False
    try:
        payload = json.loads(body)
    except ValueError:
        return False
    if not isinstance(payload, dict):
        return False
    return any(key in payload for key in FEED_KEYS)


# ---------------------------------------------------------------------- 리더


class KakaoReader:
    """한 계정의 카카오톡 데이터베이스에 대한 읽기 전용 뷰."""

    def __init__(self, connection, *, user_id: int) -> None:
        self._con = connection
        self._user_id = user_id
        self._tables: set[str] | None = None
        self._columns: dict[str, set[str]] = {}
        self._nicknames: dict[str, str] | None = None
        self._titles: dict[str, str] | None = None
        self._link_names: dict[str, str] | None = None
        self._open_chats: set[str] | None = None

    # ------------------------------------------------------------- 스키마 도우미

    def tables(self) -> set[str]:
        if self._tables is None:
            try:
                rows = self._con.execute(
                    "SELECT name FROM sqlite_master WHERE type IN ('table','view')"
                ).fetchall()
            except sqlcipher.Error:
                rows = []
            self._tables = {str(row[0]) for row in rows}
        return self._tables

    def columns(self, table: str) -> set[str]:
        if table not in self._columns:
            try:
                rows = self._con.execute(f'PRAGMA table_info("{table}")').fetchall()
            except sqlcipher.Error:
                rows = []
            self._columns[table] = {str(row[1]) for row in rows}
        return self._columns[table]

    def has(self, table: str) -> bool:
        return table in self.tables()

    def _select(self, table: str, wanted: Iterable[str], where: str = "") -> list[tuple]:
        """*wanted* 와 실제 컬럼의 교집합을 관대하게 선택한다."""

        if not self.has(table):
            return []
        present = [column for column in wanted if column in self.columns(table)]
        if not present:
            return []
        columns = ", ".join(f'"{column}"' for column in present)
        query = f'SELECT {columns} FROM "{table}"'
        if where:
            query += f" WHERE {where}"
        try:
            rows = self._con.execute(query).fetchall()
        except sqlcipher.Error as exc:
            logger.warning("%s 를 읽을 수 없다 (%s)", table, type(exc).__name__)
            return []
        return [tuple(row) for row in rows]

    # -------------------------------------------------------------- 보강 정보

    def nicknames(self) -> dict[str, str]:
        if self._nicknames is None:
            names: dict[str, str] = {}
            for row in self._select(
                "NTUser", ("userId", "friendNickName", "displayName", "nickName")
            ):
                if len(row) < 2:
                    continue
                user_id = _as_int(row[0])
                if user_id is None:
                    continue
                for value in row[1:]:
                    text = _as_text(value)
                    if text:
                        names[str(user_id)] = text
                        break
            self._nicknames = names
        return self._nicknames

    def room_titles(self) -> dict[str, str]:
        if self._titles is None:
            titles: dict[str, str] = {}
            if self.has("NTChatMeta"):
                for row in self._select(
                    "NTChatMeta",
                    ("chatId", "content", "revision", "type"),
                ):
                    values = dict(zip(("chatId", "content", "revision", "type"), row))
                    if _as_int(values.get("type")) != 3:
                        continue
                    chat_id = _as_int(values.get("chatId"))
                    content = _as_text(values.get("content"))
                    if chat_id is None or not content:
                        continue
                    titles[str(chat_id)] = content
            self._titles = titles
        return self._titles

    def link_names(self) -> dict[str, str]:
        if self._link_names is None:
            names: dict[str, str] = {}
            if self.has("NTOpenLink"):
                columns = self.columns("NTOpenLink")
                chat_column = next((c for c in CHAT_ID_COLUMNS if c in columns), None)
                name_column = next((c for c in LINK_NAME_COLUMNS if c in columns), None)
                if chat_column and name_column:
                    for chat_raw, name_raw in self._select(
                        "NTOpenLink", (chat_column, name_column)
                    ):
                        chat_id = _as_int(chat_raw)
                        name = _as_text(name_raw)
                        if chat_id is not None and name:
                            names[str(chat_id)] = name
            self._link_names = names
        return self._link_names

    def open_chat_ids(self) -> set[str]:
        """오픈채팅에 속한 chat id들. 이 백엔드는 오픈채팅을 폴링하지 않는다.

        오픈링크 방은 ``NTChatRoom.linkId`` 에 표시된다. ``NTOpenLink`` 자체에는
        ``chatId`` 컬럼이 없으므로 ``linkId`` 로 연결한다. chat id를 노출하는
        스키마를 위해 ``NTOpenLink`` 스캔을 대체 경로로 남겨 둔다.
        """

        if self._open_chats is None:
            ids: set[str] = set()

            if self.has("NTChatRoom") and "linkId" in self.columns("NTChatRoom"):
                for chat_raw, link_raw in self._select("NTChatRoom", ("chatId", "linkId")):
                    chat_id = _as_int(chat_raw)
                    if chat_id is None:
                        continue
                    if _is_truthy_link(link_raw):
                        ids.add(str(chat_id))

            if self.has("NTOpenLink"):
                columns = self.columns("NTOpenLink")
                chat_column = next((c for c in CHAT_ID_COLUMNS if c in columns), None)
                if chat_column:
                    for row in self._select("NTOpenLink", (chat_column,)):
                        chat_id = _as_int(row[0])
                        if chat_id is not None:
                            ids.add(str(chat_id))

            self._open_chats = ids
        return self._open_chats

    # ------------------------------------------------------------------- 방

    def raw_rooms(self) -> list[RawRoom]:
        rows = self._select(
            "NTChatRoom",
            (
                "chatId",
                "type",
                "chatName",
                "activeMembersCount",
                "directChatMemberUserId",
                "displayMemberIds",
            ),
        )
        columns = [
            column
            for column in (
                "chatId",
                "type",
                "chatName",
                "activeMembersCount",
                "directChatMemberUserId",
                "displayMemberIds",
            )
            if column in self.columns("NTChatRoom")
        ]

        rooms: list[RawRoom] = []
        for row in rows:
            values = dict(zip(columns, row))
            chat_id = _as_int(values.get("chatId"))
            if chat_id is None:
                continue
            rooms.append(
                RawRoom(
                    chat_id=str(chat_id),
                    type_value=_as_int(values.get("type")),
                    chat_name=_as_text(values.get("chatName")) or "",
                    active_members_count=_as_int(values.get("activeMembersCount")),
                    direct_member_user_id=_as_int(values.get("directChatMemberUserId")),
                    member_ids=self._member_ids(values.get("displayMemberIds")),
                )
            )
        return rooms

    @staticmethod
    def _member_ids(raw: Any) -> tuple[str, ...]:
        if raw is None:
            return ()
        if isinstance(raw, (bytes, bytearray, memoryview)):
            try:
                payload = plistlib.loads(bytes(raw))
            except Exception:
                return ()
        else:
            payload = raw
        items: Iterable[Any]
        if isinstance(payload, dict):
            items = next(
                (value for value in payload.values() if isinstance(value, (list, tuple))),
                (),
            )
        elif isinstance(payload, (list, tuple)):
            items = payload
        else:
            return ()
        result: list[str] = []
        for item in items:
            user_id = _as_int(item)
            if user_id is not None:
                result.append(str(user_id))
        return tuple(result)

    def resolve_room_name(self, room: RawRoom) -> str:
        if room.chat_name:
            return room.chat_name

        title = self.room_titles().get(room.chat_id)
        if title:
            return title

        link = self.link_names().get(room.chat_id)
        if link:
            return link

        nicknames = self.nicknames()
        others = [user_id for user_id in room.member_ids if user_id != str(self._user_id)]

        if room.is_direct:
            peer = None
            if room.direct_member_user_id is not None:
                peer = str(room.direct_member_user_id)
            elif others:
                peer = others[0]
            if peer and peer != str(self._user_id):
                return nicknames.get(peer, f"user-{peer}")

        if others:
            names = [nicknames.get(user_id, f"user-{user_id}") for user_id in others]
            return ", ".join(names[:MAX_GROUP_NAME_MEMBERS])

        return f"chat-{room.chat_id}"

    def rooms(self) -> list[Room]:
        excluded = self.open_chat_ids()
        rooms: list[Room] = []
        for raw in self.raw_rooms():
            if raw.chat_id in excluded:
                continue
            rooms.append(
                Room(
                    chat_id=raw.chat_id,
                    chat_name=self.resolve_room_name(raw),
                    chat_type="direct" if raw.is_direct else "group",
                )
            )
        rooms.sort(key=lambda room: room.chat_id)
        return rooms

    # ---------------------------------------------------------------- 메시지

    def _message_rows(self, since_epoch: int | None) -> list[tuple]:
        wanted = (
            "chatId",
            "logId",
            "authorId",
            "type",
            "message",
            "sentAt",
            "supplement",
            "attachment",
        )
        where = ""
        params: tuple = ()
        if since_epoch is not None and "sentAt" in self.columns("NTChatMessage"):
            where = "sentAt >= ?"
            params = (since_epoch,)

        if not self.has("NTChatMessage"):
            return []
        present = [column for column in wanted if column in self.columns("NTChatMessage")]
        if not present:
            return []
        columns = ", ".join(f'"{column}"' for column in present)
        query = f'SELECT {columns} FROM "NTChatMessage"'
        if where:
            query += f" WHERE {where}"
        try:
            rows = self._con.execute(query, params).fetchall()
        except sqlcipher.Error as exc:
            logger.warning("NTChatMessage를 읽을 수 없다 (%s)", type(exc).__name__)
            return []
        return [(present, tuple(row)) for row in rows]

    def messages(
        self,
        *,
        since_epoch: int | None = None,
        room_index: dict[str, Room] | None = None,
    ) -> tuple[list[Message], int]:
        """정규화된 메시지들과 디코딩 오류로 건너뛴 행 수."""

        index = room_index if room_index is not None else {
            room.chat_id: room for room in self.rooms()
        }
        nicknames = self.nicknames()
        collected: list[Message] = []
        skipped = 0

        for present, row in self._message_rows(since_epoch):
            values = dict(zip(present, row))
            try:
                message = self._normalise(values, index, nicknames)
            except (UnicodeDecodeError, ValueError, TypeError):
                skipped += 1
                continue
            if message is not None:
                collected.append(message)

        collected.sort(key=lambda item: (item.timestamp, item.message_id))

        deduped: list[Message] = []
        seen: set[tuple[str, str]] = set()
        for message in collected:
            key = (message.chat_id, message.message_id)
            if key in seen:
                continue
            seen.add(key)
            deduped.append(message)

        if skipped:
            logger.warning("디코딩할 수 없는 메시지 행 %d개를 건너뛰었다", skipped)
        return deduped, skipped

    def _normalise(
        self,
        values: dict[str, Any],
        index: dict[str, Room],
        nicknames: dict[str, str],
    ) -> Message | None:
        chat_id_value = _as_int(values.get("chatId"))
        if chat_id_value is None:
            return None
        chat_id = str(chat_id_value)
        room = index.get(chat_id)
        if room is None:
            # 오픈채팅이거나 걸러진 방 — 인터페이스에 절대 노출하지 않는다.
            return None

        body = _decode_body(values.get("message"))
        log_id = _as_int(values.get("logId"))
        raw_type = _as_int(values.get("type"))
        base_type = None if raw_type is None else raw_type & TYPE_FLAG_MASK
        message_type = "text" if base_type == TEXT_TYPE else (
            f"type_{base_type}" if base_type is not None else "type_unknown"
        )

        is_feed = _is_feed(message_type, body)
        original_type: str | None = None
        is_placeholder = False

        if not is_feed and message_type != "text":
            # 아무것도 숨기지 않도록 원시 값(플래그 비트 포함)을 보존한다.
            original_type = f"type_{raw_type}" if raw_type is not None else "type_unknown"
            is_placeholder = True
            message_type = "text"
            body = PLACEHOLDER_TEXT
        elif not body:
            # 본문이 없는 텍스트 행은 보여줄 내용이 없다.
            return None

        author_id = _as_int(values.get("authorId"))
        sender_id = str(author_id) if author_id is not None else "0"
        if author_id is not None and author_id == self._user_id:
            sender_nickname = nicknames.get(sender_id, "나")
        elif sender_id in nicknames:
            sender_nickname = nicknames[sender_id]
        else:
            sender_nickname = f"user-{sender_id}" if sender_id != "0" else UNKNOWN_SENDER

        log_suffix = str(log_id) if log_id is not None else "0"
        message_id = f"{chat_id}-{log_suffix}"

        reply_log_id = _reply_target(
            values.get("supplement"), values.get("attachment"), log_id
        )
        reply_to = f"{chat_id}-{reply_log_id}" if reply_log_id is not None else None

        return Message(
            message_id=message_id,
            chat_id=chat_id,
            chat_name=room.chat_name,
            chat_type=room.chat_type,
            sender_id=sender_id,
            sender_nickname=sender_nickname,
            timestamp=epoch_to_rfc3339(values.get("sentAt")),
            text=body if not is_feed else body,
            message_type=message_type,
            reply_to_message_id=reply_to,
            is_feed=is_feed,
            original_message_type=original_type,
            is_placeholder=is_placeholder,
        )

    # ---------------------------------------------------------------- 스냅샷

    def snapshot(self, *, since_epoch: int | None = None) -> Snapshot:
        rooms = self.rooms()
        index = {room.chat_id: room for room in rooms}
        messages, skipped = self.messages(since_epoch=since_epoch, room_index=index)
        last_by_room: dict[str, str] = {}
        for message in messages:
            current = last_by_room.get(message.chat_id)
            if current is None or message.timestamp > current:
                last_by_room[message.chat_id] = message.timestamp
        enriched = [
            Room(
                chat_id=room.chat_id,
                chat_name=room.chat_name,
                chat_type=room.chat_type,
                last_message_at=last_by_room.get(room.chat_id),
                message_count=room.message_count,
            )
            for room in rooms
        ]
        return Snapshot(
            rooms=tuple(enriched),
            messages=tuple(messages),
            skipped_rows=skipped,
            open_chat_ids=frozenset(self.open_chat_ids()),
        )
