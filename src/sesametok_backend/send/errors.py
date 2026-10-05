"""Send failure codes and their user-facing messages.

The wording should be reproduced as closely as possible: the messages explain
what was and was not done, which
matters because sending is irreversible. They contain no conversation data.
"""

from __future__ import annotations

from dataclasses import dataclass

TEMPLATES: dict[str, str] = {
    "accessibility_denied": (
        "accessibility permission is not granted; grant it to this terminal in "
        "System Settings > Privacy & Security > Accessibility"
    ),
    "not_running": "KakaoTalk is not running",
    "window_not_open": (
        "chat window '{room}' is not open. This command only sends into an already "
        "open window; open the room in KakaoTalk first. Open windows: {open}"
    ),
    "compose_box_not_found": "could not find the message input box in window '{room}'",
    "set_value_failed": "failed to write the message into the input box (AXError {n})",
    "background_compose_not_empty": (
        "strict background-only mode refused to overwrite the existing text in "
        "'{room}'s compose box"
    ),
    "background_compose_unreadable": (
        "strict background-only mode could not verify that '{room}'s compose box was "
        "empty; nothing was written"
    ),
    "background_compose_changed": (
        "'{room}'s compose text changed before the non-activating Enter; Enter was not posted"
    ),
    "not_sent": "message stayed in the input box; KakaoTalk did not accept Enter",
    "background_send_not_accepted": (
        "KakaoTalk did not confirm the non-activating Enter; strict background-only mode "
        "stopped without any visible fallback (the text may remain in the compose box)"
    ),
    "chat_list_unavailable": (
        "could not find KakaoTalk's chat list; is the main window open?"
    ),
    "room_not_in_chat_list": (
        "'{room}' is not in the chat list ({n} rooms checked). Check the exact name with "
        "the live room list"
    ),
    "room_open_failed": "selected '{room}' in the chat list but its window did not open",
    "search_unavailable": "could not reach KakaoTalk's chat search box",
    "ambiguous_room": (
        "{count} chats are named '{room}', and nothing on screen tells them apart. "
        "Nothing was opened: sending to the wrong one cannot be undone. Address the room "
        "by its id so the last message can be used to pick, or rename one of them."
    ),
    "room_name_not_typable": (
        "the room name contains characters a chat name cannot: {offenders}. {detail}"
    ),
    "value_mismatch": (
        "{what} did not hold what was written to it, so the next step would have acted "
        "on the wrong value."
    ),
    "room_not_found": "no chat named '{room}' found in the list or by search.",
    "image_not_sent": (
        "pasted the image but KakaoTalk never showed the send confirmation, and the "
        "paste did not clear"
    ),
    "user_busy": (
        "you kept using the keyboard or mouse for {n}s, and this send needs to bring "
        "KakaoTalk forward. Nothing was sent and nothing was typed anywhere. Retry when "
        "idle, or take focus now to interrupt yourself."
    ),
    "focus_lost": (
        "KakaoTalk was not frontmost at the moment the keystroke would have been sent, "
        "so it was not sent at all — posting it would have typed into whatever app you "
        "switched to. Nothing was sent; retry."
    ),
    "chat_list_moved": (
        "the chat list reordered while '{room}' was being opened, so the click was not "
        "sent — it would have landed on whichever room took that position. Nothing was "
        "opened; retry."
    ),
    "draft_not_staged": (
        "the draft is not sitting in '{room}'s compose box. It was either not pasted or "
        "already sent — check the chat rather than assuming it is waiting."
    ),
    "cancelled": "nothing was sent",
    "cancelled_after_paste": (
        "the image was pasted and then cancelled, so it may or may not have been sent"
    ),
    "no_consent": (
        "sending requires explicit consent: set accept_use_policy=true (dry_run does not need it)"
    ),
    "rate_limited": "send rate limit reached; retry later",
    "target_ambiguous": (
        "this room's name maps to more than one chat and the archive cannot tell them "
        "apart; nothing was sent"
    ),
    "window_not_discoverable": "{detail}",
    "background_only_unsupported": (
        "background-only mode is not available for this request; nothing was sent"
    ),
    "unconfirmed": (
        "the send outcome could not be confirmed; it may or may not have been delivered. "
        "Do not retry blindly: retrying can double-send."
    ),
}


class SendError(RuntimeError):
    """A send that failed before KakaoTalk accepted it."""

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
