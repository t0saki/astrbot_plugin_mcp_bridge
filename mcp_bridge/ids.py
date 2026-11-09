"""Parse the ``message_id`` argument of ``run_command``.

Accepted forms:

* a bare OneBot message id, as an integer or a string (may be negative):
  ``-1234567``, ``"-1234567"``;
* a chat-qualified id ``ob:<chat>:<integer>``, e.g.
  ``ob:group:123456789:-1234567`` or ``ob:private:10001:42``. Only the trailing
  integer is sent to ``get_msg``; when the chat part is ``group:<id>`` or
  ``private:<id>`` it is additionally checked against the resolved message so
  an id copied from the wrong chat is refused instead of posting elsewhere.
"""

from __future__ import annotations

import re
from dataclasses import dataclass

from .errors import BridgeError

_INT_RE = re.compile(r"-?\d{1,19}")
_CHAT_RE = re.compile(r"(group|private):(\d{1,20})")
_INT64_MAX = 2**63 - 1


@dataclass(frozen=True)
class MessageRef:
    message_id: int
    chat_type: str | None = None  # "group" | "private" | None (not stated)
    chat_id: str | None = None


def _to_int(text: str) -> int:
    if not _INT_RE.fullmatch(text):
        raise BridgeError(
            "invalid_message_id",
            f"message_id 必须是整数或 ob:<chat>:<整数> 形式，收到: {text[:64]!r}",
        )
    value = int(text)
    if abs(value) > _INT64_MAX or value == 0:
        raise BridgeError("invalid_message_id", f"message_id 超出范围: {text[:64]!r}")
    return value


def parse_message_id(raw: object) -> MessageRef:
    """Turn the caller-supplied value into a :class:`MessageRef` or raise."""
    if isinstance(raw, bool) or raw is None:
        raise BridgeError("invalid_message_id", "缺少 message_id")
    if isinstance(raw, int):
        return MessageRef(_to_int(str(raw)))
    if isinstance(raw, float):
        if not raw.is_integer():
            raise BridgeError("invalid_message_id", f"message_id 不是整数: {raw!r}")
        return MessageRef(_to_int(str(int(raw))))
    if not isinstance(raw, str):
        raise BridgeError("invalid_message_id", "message_id 必须是字符串或整数")

    text = raw.strip()
    if not text.startswith("ob:"):
        return MessageRef(_to_int(text))

    head, sep, tail = text[3:].rpartition(":")
    if not sep or not head:
        raise BridgeError(
            "invalid_message_id",
            f"message_id 应为 ob:<chat>:<整数> 形式，收到: {text[:64]!r}",
        )
    message_id = _to_int(tail)
    chat = _CHAT_RE.fullmatch(head)
    if chat:
        return MessageRef(message_id, chat.group(1), chat.group(2))
    return MessageRef(message_id)
