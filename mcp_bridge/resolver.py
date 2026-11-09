"""Validate a OneBot ``get_msg`` result and derive the identity to act as.

Identity (sender, chat type, group) comes only from the ``get_msg`` result,
never from the MCP caller. Field names follow NapCat's ``GetMsg`` action
(``packages/napcat-onebot/action/msg/GetMsg.ts`` / ``api/msg.ts``):
``time`` (unix seconds), ``self_id``, ``user_id``, ``message_type``
(``group``/``private``), ``sub_type`` (``normal``/``friend``/``group``),
``group_id``, ``sender`` (``user_id``, ``nickname``, ``card``, ``role``),
``post_type`` (``message`` or ``message_sent`` for the bot's own messages) and
``message_sent_type`` (``self`` for the bot's own messages).
"""

from __future__ import annotations

from dataclasses import dataclass, field
from typing import Any

from .errors import BridgeError
from .ids import MessageRef

# A get_msg "time" further in the future than this is treated as invalid
# (NapCat falls back to Date.now() in milliseconds when msgTime is unparsable).
MAX_FUTURE_SKEW_SECONDS = 300


@dataclass(frozen=True)
class ResolvedMessage:
    message_id: int
    self_id: str
    user_id: str
    message_type: str  # "group" | "private"
    sub_type: str
    group_id: str | None
    sender: dict[str, Any] = field(default_factory=dict)
    time: int = 0
    group_name: str | None = None
    font: int = 14

    @property
    def sender_name(self) -> str:
        return str(self.sender.get("card") or self.sender.get("nickname") or self.user_id)

    @property
    def chat_label(self) -> str:
        if self.message_type == "group":
            return f"group:{self.group_id}"
        return f"private:{self.user_id}"


def _id_str(value: Any) -> str:
    if value is None or isinstance(value, bool):
        return ""
    text = str(value).strip()
    return text if text.lstrip("-").isdigit() else ""


def resolve_message(
    data: Any,
    ref: MessageRef,
    *,
    now: float,
    max_age_seconds: int,
    bot_self_id: str = "",
) -> ResolvedMessage:
    """Check a ``get_msg`` result and return the identity to impersonate.

    Args:
        data: ``data`` field of the ``get_msg`` response.
        ref: The parsed caller reference (message id and optional chat).
        now: Current unix time in seconds.
        max_age_seconds: Refuse messages older than this.
        bot_self_id: Bot account id when known independently (config or
            ``get_login_info``); used when ``get_msg`` omits ``self_id``.
    """
    if not isinstance(data, dict) or not data:
        raise BridgeError("message_not_found", f"无法解析消息 {ref.message_id}（get_msg 无结果）")

    self_id = _id_str(data.get("self_id")) or _id_str(bot_self_id)
    if not self_id:
        raise BridgeError("platform_unavailable", "无法确定机器人自身 QQ 号（self_id）")
    if bot_self_id and _id_str(bot_self_id) and _id_str(bot_self_id) != self_id:
        raise BridgeError(
            "platform_unavailable",
            f"get_msg 返回的 self_id={self_id} 与配置的机器人账号不一致",
        )

    sender = data.get("sender") if isinstance(data.get("sender"), dict) else {}
    user_id = _id_str(data.get("user_id")) or _id_str(sender.get("user_id"))
    if not user_id or user_id == "0":
        raise BridgeError("unsupported_message", "消息缺少发送者 user_id")

    if (
        user_id == self_id
        or data.get("post_type") == "message_sent"
        or data.get("message_sent_type") == "self"
    ):
        raise BridgeError("self_message", "该消息是机器人自己发送的，不能以它的身份执行指令")

    message_type = data.get("message_type")
    if message_type not in ("group", "private"):
        raise BridgeError("unsupported_message", f"不支持的消息类型: {message_type!r}")
    group_id = _id_str(data.get("group_id")) or None
    if message_type == "group" and not group_id:
        raise BridgeError("unsupported_message", "群消息缺少 group_id")

    if ref.chat_type is not None:
        if ref.chat_type != message_type:
            raise BridgeError(
                "chat_mismatch",
                f"message_id 声明的会话类型 {ref.chat_type} 与实际消息（{message_type}）不符",
            )
        actual = group_id if message_type == "group" else user_id
        if ref.chat_id != actual:
            raise BridgeError(
                "chat_mismatch",
                f"message_id 声明的会话 {ref.chat_type}:{ref.chat_id} 与实际消息"
                f"（{message_type}:{actual}）不符",
            )

    try:
        msg_time = int(data.get("time"))
    except (TypeError, ValueError):
        raise BridgeError("unsupported_message", "消息缺少有效的发送时间 time") from None
    if msg_time > now + MAX_FUTURE_SKEW_SECONDS:
        raise BridgeError("unsupported_message", "消息时间戳无效（在未来）")
    age = now - msg_time
    if age > max_age_seconds:
        raise BridgeError(
            "stale_message",
            f"消息已发送 {int(age)} 秒，超过 max_message_age_seconds={max_age_seconds}",
        )

    clean_sender = {
        k: sender[k]
        for k in ("nickname", "card", "role", "sex", "age", "area", "level", "title")
        if k in sender and sender[k] is not None
    }
    clean_sender["user_id"] = int(user_id)

    sub_type = data.get("sub_type")
    if not isinstance(sub_type, str) or not sub_type:
        sub_type = "normal" if message_type == "group" else "friend"
    try:
        font = int(data.get("font", 14))
    except (TypeError, ValueError):
        font = 14
    group_name = data.get("group_name")

    return ResolvedMessage(
        message_id=ref.message_id,
        self_id=self_id,
        user_id=user_id,
        message_type=message_type,
        sub_type=sub_type,
        group_id=group_id,
        sender=clean_sender,
        time=msg_time,
        group_name=group_name if isinstance(group_name, str) else None,
        font=font,
    )
