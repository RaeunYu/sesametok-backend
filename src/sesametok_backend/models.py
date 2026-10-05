"""Shared DTOs crossing the read → archive → API boundary.

These are plain dataclasses; the API layer renders them to JSON with RFC3339 UTC
timestamps. Personal data lives in ``chat_name`` / ``sender_nickname`` / ``text``,
so they are never logged.
"""

from __future__ import annotations

from dataclasses import asdict, dataclass, field
from typing import Any

PLACEHOLDER_TEXT = "[지원하지 않는 메시지 타입입니다. 원본 메시지를 확인해주세요.]"
"""Body stored for message types this backend does not support (ADR-003)."""


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
    # ADR-003: unsupported types are stored as text with the original type kept.
    original_message_type: str | None = None
    is_placeholder: bool = False

    def to_dict(self) -> dict[str, Any]:
        return asdict(self)

    def fingerprint(self) -> tuple[Any, ...]:
        """Fields whose change must produce ``message.updated``."""

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
