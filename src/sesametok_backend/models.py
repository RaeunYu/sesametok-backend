"""읽기 → 아카이브 → API 경계를 넘는 공유 DTO.

평범한 dataclass이며, API 계층이 RFC3339 UTC 타임스탬프로 JSON으로 렌더링한다.
개인정보는 ``chat_name`` / ``sender_nickname`` / ``text``에 있으므로 절대 로그에
남기지 않는다.
"""

from __future__ import annotations

from dataclasses import asdict, dataclass, field
from typing import Any

PLACEHOLDER_TEXT = "[지원하지 않는 메시지 타입입니다. 원본 메시지를 확인해주세요.]"
"""이 백엔드가 지원하지 않는 메시지 타입에 대해 저장하는 본문 (ADR-003)."""


@dataclass(frozen=True)
class Room:
    chat_id: str
    chat_name: str
    chat_type: str  # "direct" | "group"
    last_message_at: str | None = None
    message_count: int | None = None

    def to_dict(self) -> dict[str, Any]:
        return asdict(self)


@dataclass(frozen=True)
class Message:
    message_id: str
    chat_id: str
    chat_name: str
    chat_type: str
    sender_id: str
    sender_nickname: str
    timestamp: str  # RFC3339 UTC
    text: str
    message_type: str
    reply_to_message_id: str | None = None
    is_feed: bool = False
    # ADR-003: 지원하지 않는 타입은 원래 타입을 유지한 채 text로 저장한다.
    original_message_type: str | None = None
    is_placeholder: bool = False

    def to_dict(self) -> dict[str, Any]:
        return asdict(self)

    def fingerprint(self) -> tuple[Any, ...]:
        """변경되면 ``message.updated``를 만들어야 하는 필드."""

        return (
            self.chat_name,
            self.chat_type,
            self.sender_id,
            self.sender_nickname,
            self.timestamp,
            self.text,
            self.message_type,
            self.reply_to_message_id,
            self.is_feed,
            self.original_message_type,
            self.is_placeholder,
        )


@dataclass(frozen=True)
class Event:
    seq: int
    kind: str  # message.created | message.updated | room.updated | send.result
    payload: dict[str, Any] = field(default_factory=dict)
    created_at: str | None = None

    def to_dict(self) -> dict[str, Any]:
        data: dict[str, Any] = {"seq": self.seq, "kind": self.kind, "payload": self.payload}
        if self.created_at is not None:
            data["created_at"] = self.created_at
        return data


EVENT_KINDS = frozenset(
    {"message.created", "message.updated", "room.updated", "send.result"}
)
