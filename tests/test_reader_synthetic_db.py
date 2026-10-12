"""합성 암호화 데이터베이스에 대한 리더 테스트.

픽스처에는 지어낸 방, 사용자, 메시지만 들어 있다. 단언은 정렬, 분류, 정규화를
검사하며 실제 대화 데이터는 절대 다루지 않는다.
"""

from __future__ import annotations

import pytest

from sesametok_backend.kakao.reader import KakaoReader, epoch_to_rfc3339
from sesametok_backend.models import PLACEHOLDER_TEXT
from support import (
    SYNTHETIC_USER_ID,
    connect_synthetic,
    write_synthetic_database,
)


@pytest.fixture
def connection(tmp_path):
    conn = connect_synthetic(write_synthetic_database(tmp_path / "synthetic.db"))
    yield conn
    conn.close()


@pytest.fixture
def reader(connection) -> KakaoReader:
    return KakaoReader(connection, user_id=SYNTHETIC_USER_ID)


def test_rooms_exclude_open_chats_and_sort(reader: KakaoReader) -> None:
    rooms = reader.rooms()
    assert [room.chat_id for room in rooms] == ["100", "200", "300", "500"]
    # 오픈채팅은 NTChatRoom.linkId -> NTOpenLink.linkId로 식별한다
    assert reader.open_chat_ids() == {"400"}


def test_room_name_priority(reader: KakaoReader) -> None:
    names = {room.chat_id: room.chat_name for room in reader.rooms()}

    # 명시된 chatName이 메타 제목보다 우선한다
    assert names["300"] == "Synthetic Team A"
    # chatName이 비어 있으면 리비전이 가장 높은 메타 제목이 이긴다
    assert names["500"] == "Meta Title v2"
    # 1:1 방은 상대 닉네임으로 대체한다
    assert names["100"] == "Peer"
    # 그룹 방은 자신을 제외한 멤버 닉네임을 이어 붙인다
    assert names["200"] == "Member A, Member B, Member C"


def test_chat_type_classification(reader: KakaoReader) -> None:
    types = {room.chat_id: room.chat_type for room in reader.rooms()}
    assert types["100"] == "direct"
    assert types["200"] == "group"
    assert types["500"] == "group"


def test_nickname_priority(reader: KakaoReader) -> None:
    nicknames = reader.nicknames()
    assert nicknames["500"] == "Peer"       # friendNickName 우선
    assert nicknames["501"] == "Member A"   # 다음 displayName
    assert nicknames["502"] == "Member B"   # 다음 nickName
    assert nicknames[str(SYNTHETIC_USER_ID)] == "Me"


def test_message_count_and_skipped_rows(reader: KakaoReader) -> None:
    messages, skipped = reader.messages()

    assert skipped == 1  # UTF-8이 아닌 행
    assert len(messages) == 12
    assert all(message.chat_id != "400" for message in messages)


def test_flagged_text_type_stays_text(reader: KakaoReader) -> None:
    by_id = {message.message_id: message for message in reader.messages()[0]}
    flagged = by_id["300-35"]  # 원시 타입 0x4000 | 1

    assert flagged.message_type == "text"
    assert flagged.text == "flagged text"
    assert flagged.is_placeholder is False
    assert flagged.original_message_type is None


def test_flagged_photo_keeps_the_raw_type(reader: KakaoReader) -> None:
    by_id = {message.message_id: message for message in reader.messages()[0]}
    flagged = by_id["300-36"]  # 원시 타입 0x4000 | 2

    assert flagged.message_type == "text"
    assert flagged.original_message_type == "type_16386"
    assert flagged.is_placeholder is True
    assert flagged.text == PLACEHOLDER_TEXT


def test_unsupported_type_becomes_placeholder(reader: KakaoReader) -> None:
    by_id = {message.message_id: message for message in reader.messages()[0]}
    photo = by_id["100-12"]

    assert photo.message_type == "text"
    assert photo.original_message_type == "type_2"
    assert photo.is_placeholder is True
    assert photo.text == PLACEHOLDER_TEXT
    assert photo.is_feed is False


def test_text_message_is_not_a_placeholder(reader: KakaoReader) -> None:
    by_id = {message.message_id: message for message in reader.messages()[0]}
    assert by_id["100-10"].is_placeholder is False
    assert by_id["100-10"].original_message_type is None
    assert by_id["100-10"].text == "hello"


def test_feed_classification(reader: KakaoReader) -> None:
    by_id = {message.message_id: message for message in reader.messages()[0]}
    feed = by_id["100-13"]

    assert feed.is_feed is True
    assert feed.message_type == "type_0"
    assert feed.is_placeholder is False
    assert feed.text.startswith("{")  # 본문은 대체되지 않고 보존된다


def test_reply_parsing(reader: KakaoReader) -> None:
    by_id = {message.message_id: message for message in reader.messages()[0]}

    assert by_id["100-11"].reply_to_message_id == "100-10"   # attachment 키
    assert by_id["300-32"].reply_to_message_id == "300-30"   # supplement 컨테이너
    assert by_id["300-33"].reply_to_message_id == "300-30"   # attachment가 supplement를 이긴다
    assert by_id["300-34"].reply_to_message_id is None       # 자기 참조
    assert by_id["200-20"].reply_to_message_id is None       # 평범한 logId는 무시한다


def test_sender_resolution(reader: KakaoReader) -> None:
    by_id = {message.message_id: message for message in reader.messages()[0]}

    assert by_id["100-10"].sender_nickname == "Peer"
    assert by_id["100-11"].sender_nickname == "Me"
    assert by_id["500-50"].sender_nickname == "Member B"
    assert by_id["100-11"].sender_id == str(SYNTHETIC_USER_ID)


def test_timestamp_conversion(reader: KakaoReader) -> None:
    by_id = {message.message_id: message for message in reader.messages()[0]}
    assert by_id["100-10"].timestamp == "2026-01-01T00:00:00+00:00"


def test_messages_are_sorted_by_timestamp_then_id(reader: KakaoReader) -> None:
    messages = reader.messages()[0]
    keys = [(message.timestamp, message.message_id) for message in messages]
    assert keys == sorted(keys)


def test_since_epoch_filters(reader: KakaoReader) -> None:
    threshold = 1_767_226_000
    messages, _ = reader.messages(since_epoch=threshold)

    cutoff = epoch_to_rfc3339(threshold)
    assert messages
    assert all(message.timestamp >= cutoff for message in messages)
    assert "100-10" not in {message.message_id for message in messages}


def test_snapshot_sets_last_message_at(reader: KakaoReader) -> None:
    snapshot = reader.snapshot()

    by_id = {room.chat_id: room for room in snapshot.rooms}
    assert by_id["100"].last_message_at == "2026-01-01T00:03:00+00:00"
    assert by_id["200"].last_message_at == "2026-01-01T00:04:00+00:00"
    assert snapshot.skipped_rows == 1
    assert "400" in snapshot.open_chat_ids
