"""Build a synthetic OneBot v11 message event and feed it to AstrBot.

The event enters AstrBot through the same calls the aiocqhttp adapter makes for
a real group/private message (``aiocqhttp_platform_adapter.py``, the
``@self.bot.on_message`` handlers)::

    abm = await adapter.convert_message(event)   # OneBot Event -> AstrBotMessage
    await adapter.handle_msg(abm)                 # create_event() + commit_event()

so the wake-prefix check, whitelist, admin/member permission (derived from the
real sender id), command filters and catch-all listeners all run as usual.

The original ``message_id`` is reused: AstrBot does not deduplicate on it, and
``reply_with_quote`` then quotes the user's real message (a fresh fake id would
make NapCat reject the quoted reply).
"""

from __future__ import annotations

import time
from typing import Any

from .errors import BridgeError
from .resolver import ResolvedMessage

# Marker key on the raw OneBot event so other plugins can recognise (and e.g.
# skip) bridge-injected messages: ``event.message_obj.raw_message.get(...)``.
SYNTHETIC_MARKER = "astrbot_mcp_bridge"


def cq_escape(text: str) -> str:
    return text.replace("&", "&amp;").replace("[", "&#91;").replace("]", "&#93;")


def build_payload(
    resolved: ResolvedMessage, command: str, *, now: float | None = None
) -> dict[str, Any]:
    """OneBot v11 ``message`` event carrying ``command`` as the resolved sender."""
    payload: dict[str, Any] = {
        "post_type": "message",
        "message_type": resolved.message_type,
        "sub_type": resolved.sub_type,
        "message_id": resolved.message_id,
        "self_id": int(resolved.self_id),
        "user_id": int(resolved.user_id),
        "sender": dict(resolved.sender),
        "message": [{"type": "text", "data": {"text": command}}],
        "message_format": "array",
        "raw_message": cq_escape(command),
        "font": resolved.font,
        "time": int(time.time() if now is None else now),
        SYNTHETIC_MARKER: {"source_message_id": resolved.message_id},
    }
    if resolved.group_id is not None:
        payload["group_id"] = int(resolved.group_id)
    if resolved.group_name:
        payload["group_name"] = resolved.group_name
    return payload


def make_event(payload: dict[str, Any]) -> Any:
    from aiocqhttp import Event  # shipped with AstrBot

    event = Event.from_payload(payload)
    if event is None:
        raise BridgeError("internal_error", "无法构造 OneBot 事件")
    return event


async def inject(adapter: Any, payload: dict[str, Any]) -> None:
    """Push ``payload`` into AstrBot through the aiocqhttp adapter."""
    event = make_event(payload)
    abm = await adapter.convert_message(event)
    if abm is None:
        raise BridgeError("unsupported_message", "AstrBot 适配器忽略了该消息")
    handle_msg = getattr(adapter, "handle_msg", None)
    if callable(handle_msg):
        await handle_msg(abm)
    else:  # pragma: no cover - older/newer adapters without handle_msg
        adapter.commit_event(adapter.create_event(abm))
