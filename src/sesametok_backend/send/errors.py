"""전송 실패 코드와 사용자에게 보이는 메시지.

문구는 가능한 한 원문에 가깝게 재현한다. 이 메시지들은 무엇이 전송되었고
무엇이 전송되지 않았는지 설명하며, 전송이 되돌릴 수 없기 때문에 이 점이 중요하다.
대화 데이터는 전혀 담기지 않는다.
"""

from __future__ import annotations

from dataclasses import dataclass

TEMPLATES: dict[str, str] = {
    "accessibility_denied": (
        "손쉬운 사용 권한이 허용되지 않았습니다. 시스템 설정 > 개인정보 보호 및 보안 > "
        "손쉬운 사용에서 이 터미널에 권한을 허용하세요"
    ),
    "not_running": "카카오톡이 실행되고 있지 않습니다",
    "window_not_open": (
        "발신 대상 채팅 창 '{room}'이(가) 열려 있지 않습니다. 발신은 이미 열려 있는 "
        "창에만 할 수 있습니다. 카카오톡에서 해당 방을 연 뒤 다시 시도하세요."
    ),
    "compose_box_not_found": "'{room}' 창에서 메시지 입력 상자를 찾을 수 없습니다",
    "set_value_failed": "입력 상자에 메시지를 쓰지 못했습니다 (AXError {n})",
    "background_compose_not_empty": (
        "엄격한 백그라운드 전용 모드에서는 '{room}'의 입력 상자에 이미 있는 텍스트를 "
        "덮어쓰지 않았습니다"
    ),
    "background_compose_unreadable": (
        "엄격한 백그라운드 전용 모드에서는 '{room}'의 입력 상자가 비어 있는지 확인할 수 "
        "없었고, 아무것도 쓰지 않았습니다"
    ),
    "background_compose_changed": (
        "비활성 Enter를 보내기 직전에 '{room}'의 입력 내용이 바뀌어 Enter를 보내지 "
        "않았습니다"
    ),
    "not_sent": "메시지가 입력 상자에 남아 있습니다. 카카오톡이 Enter를 받아들이지 않았습니다",
    "background_send_not_accepted": (
        "카카오톡이 비활성 Enter를 확인해 주지 않았습니다. 엄격한 백그라운드 전용 모드는 "
        "눈에 보이는 대체 경로 없이 중단했습니다 (텍스트가 입력 상자에 남아 있을 수 있습니다)"
    ),
    "chat_list_unavailable": (
        "카카오톡의 채팅 목록을 찾을 수 없습니다. 기본 창이 열려 있나요?"
    ),
    "room_not_in_chat_list": (
        "'{room}'이(가) 채팅 목록에 없습니다 ({n}개 방 확인). 실시간 방 목록에서 정확한 "
        "이름을 확인하세요"
    ),
    "room_open_failed": "채팅 목록에서 '{room}'을(를) 선택했지만 그 창이 열리지 않았습니다",
    "search_unavailable": "카카오톡의 채팅 검색 상자에 접근할 수 없습니다",
    "ambiguous_room": (
        "이름이 '{room}'인 채팅이 {count}개인데 화면에서 서로를 구분할 단서가 없습니다. "
        "아무것도 열지 않았습니다. 잘못된 방으로 보내면 되돌릴 수 없습니다. 방을 id로 "
        "지정해 마지막 메시지로 고르게 하거나, 둘 중 하나의 이름을 바꾸세요."
    ),
    "room_name_not_typable": (
        "방 이름에 채팅 이름으로 쓸 수 없는 문자가 있습니다: {offenders}. {detail}"
    ),
    "value_mismatch": (
        "{what}이(가) 기록된 값을 그대로 유지하지 않아, 다음 단계가 잘못된 값에 대해 "
        "동작할 뻔했습니다."
    ),
    "room_not_found": "목록이나 검색에서 '{room}'이라는 채팅을 찾지 못했습니다.",
    "image_not_sent": (
        "이미지를 붙여넣었지만 카카오톡이 전송 확인을 보여주지 않았고, 붙여넣기 내용도 "
        "비워지지 않았습니다"
    ),
    "user_busy": (
        "{n}초 동안 키보드나 마우스를 계속 사용하셨는데, 이 전송은 카카오톡을 앞으로 "
        "가져와야 합니다. 아무것도 전송되지 않았고 어디에도 입력되지 않았습니다. 잠시 "
        "손을 떼고 다시 시도하거나, 지금 포커스를 가져가 스스로를 중단시키세요."
    ),
    "focus_lost": (
        "키 입력을 보내려던 순간 카카오톡이 최전면에 있지 않아 전혀 보내지 않았습니다. "
        "보냈다면 전환하신 다른 앱에 입력되었을 것입니다. 아무것도 전송되지 않았습니다. "
        "다시 시도하세요."
    ),
    "chat_list_moved": (
        "'{room}'을(를) 여는 동안 채팅 목록 순서가 바뀌어 클릭을 보내지 않았습니다. 그 "
        "자리를 차지한 방에 클릭이 들어갔을 것입니다. 아무것도 열지 않았습니다. 다시 "
        "시도하세요."
    ),
    "draft_not_staged": (
        "임시 내용이 '{room}'의 입력 상자에 없습니다. 붙여넣어지지 않았거나 이미 전송된 "
        "것입니다. 대기 중이라고 단정하지 말고 채팅을 확인하세요."
    ),
    "cancelled": "아무것도 전송되지 않았습니다",
    "cancelled_after_paste": (
        "이미지를 붙여넣은 뒤 취소되었으므로 전송되었을 수도, 되지 않았을 수도 있습니다"
    ),
    "no_consent": (
        "전송에는 명시적 동의가 필요합니다: accept_use_policy=true로 설정하세요 "
        "(dry_run에는 필요하지 않습니다)"
    ),
    "rate_limited": "전송 속도 제한에 도달했습니다. 나중에 다시 시도하세요",
    "target_ambiguous": (
        "이 방의 이름이 둘 이상의 채팅을 가리키는데 아카이브에서 구분할 수 없습니다. "
        "아무것도 전송되지 않았습니다"
    ),
    "window_not_discoverable": "{detail}",
    "background_only_unsupported": (
        "이 요청에는 백그라운드 전용 모드를 사용할 수 없습니다. 아무것도 전송되지 않았습니다"
    ),
    "unconfirmed": (
        "전송 결과를 확인할 수 없었습니다. 전달되었을 수도, 되지 않았을 수도 있습니다. "
        "무턱대고 다시 시도하지 마세요. 재시도하면 중복 전송될 수 있습니다."
    ),
}


class SendError(RuntimeError):
    """카카오톡이 받아들이기 전에 실패한 전송."""

    def __init__(
        self,
        code: str,
        message: str | None = None,
        *,
        retryable: bool = False,
        **details: object,
    ) -> None:
        self.code = code
        self.details = details
        self.retryable = retryable
        super().__init__(message or format_message(code, **details))


def format_message(code: str, **details: object) -> str:
    template = TEMPLATES.get(code)
    if template is None:
        return code
    try:
        return template.format(**details)
    except (KeyError, IndexError):
        return template


def error_payload(code: str, message: str | None = None, *, retryable: bool = False) -> dict:
    return {
        "error": {
            "code": code,
            "message": message if message is not None else format_message(code),
            "retryable": retryable,
        }
    }
