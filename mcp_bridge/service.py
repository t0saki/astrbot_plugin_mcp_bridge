"""The two operations exposed over MCP and plain HTTP."""

from __future__ import annotations

import asyncio
import logging
import time
from collections.abc import Awaitable, Callable
from typing import Any, Protocol

from .allowlist import check_permitted, normalize_command
from .commands import build_command_list, registry_denial
from .config import BridgeConfig
from .errors import BridgeError
from .ids import parse_message_id
from .injector import build_payload, inject
from .ratelimit import RateLimiter
from .resolver import resolve_message

ACTION_TIMEOUT_SECONDS = 10.0


class Backend(Protocol):
    """What the service needs from AstrBot (``main.py`` provides the real one)."""

    def get_adapter(self) -> Any:
        """The aiocqhttp platform adapter; raise BridgeError when unavailable."""

    async def registry(self) -> list[dict]:
        """AstrBot command descriptors (``command_management.list_commands()``)."""

    def wake_prefixes(self) -> list[str]: ...


def _classify_action_error(action: str, exc: Exception) -> BridgeError:
    name = type(exc).__name__
    if name == "ActionFailed":
        result = getattr(exc, "result", None) or {}
        detail = ""
        if isinstance(result, dict):
            detail = str(result.get("wording") or result.get("message") or "")
        if action == "get_msg":
            return BridgeError(
                "message_not_found",
                f"OneBot 找不到该消息（可能已撤回或 id 错误）{': ' + detail if detail else ''}",
            )
        return BridgeError("platform_unavailable", f"OneBot 调用 {action} 失败 {detail}".strip())
    if name == "ApiNotAvailable":
        return BridgeError(
            "platform_unavailable", "OneBot 实现未连接到 AstrBot（无可用的反向 WS 连接）"
        )
    if isinstance(exc, asyncio.TimeoutError) or name == "NetworkError":
        return BridgeError("platform_unavailable", f"OneBot 调用 {action} 超时或网络错误")
    return BridgeError("internal_error", f"OneBot 调用 {action} 出错: {name}")


class BridgeService:
    def __init__(
        self,
        config: BridgeConfig,
        backend: Backend,
        *,
        logger: logging.Logger | None = None,
        clock: Callable[[], float] = time.time,
        injector: Callable[[Any, dict], Awaitable[None]] = inject,
    ) -> None:
        self.cfg = config
        self.backend = backend
        self.log = logger or logging.getLogger("astrbot")
        self._clock = clock
        self._inject = injector
        self.limiter = RateLimiter(config.rate_limit_per_minute)

    # -- list_commands --------------------------------------------------------

    async def list_commands(self) -> list[dict]:
        try:
            registry = await self.backend.registry()
        except Exception as e:  # registry is best-effort; extras/allowlist still work
            self.log.warning("[mcp_bridge] reading AstrBot command registry failed: %r", e)
            registry = []
        return build_command_list(
            registry,
            self.backend.wake_prefixes(),
            self.cfg.allow_commands,
            self.cfg.extra_commands,
            self.cfg.deny_commands,
        )

    # -- run_command ----------------------------------------------------------

    async def _call(self, adapter: Any, action: str, **params: Any) -> Any:
        if self.cfg.self_id.isdigit():
            params["self_id"] = int(self.cfg.self_id)
        client = adapter.get_client() if hasattr(adapter, "get_client") else adapter.bot
        try:
            return await asyncio.wait_for(
                client.call_action(action, **params), ACTION_TIMEOUT_SECONDS
            )
        except BridgeError:
            raise
        except Exception as e:
            raise _classify_action_error(action, e) from None

    async def _check_registry_denial(self, text: str) -> None:
        """Refuse aliases / other wake prefixes of a denied AstrBot command."""
        if not self.cfg.deny_commands:
            return
        try:
            registry = await self.backend.registry()
        except Exception as e:  # textual deny already applied; alias expansion is extra
            self.log.warning("[mcp_bridge] reading AstrBot command registry failed: %r", e)
            return
        spelling = registry_denial(
            registry, self.backend.wake_prefixes(), self.cfg.deny_commands, text
        )
        if spelling is not None:
            raise BridgeError(
                "denied",
                f"指令 {text!r} 调用的 {spelling!r} 对应一个被 deny_commands 禁止的指令，拒绝执行",
            )

    async def run_command(self, message_id: Any, command: Any) -> dict:
        if not self.limiter.try_acquire():
            raise BridgeError(
                "rate_limited",
                f"调用过于频繁（每分钟最多 {self.cfg.rate_limit_per_minute} 次），"
                f"请 {int(self.limiter.retry_after()) + 1} 秒后再试",
            )
        ref = parse_message_id(message_id)
        text = normalize_command(command)
        check_permitted(self.cfg.allow_commands, self.cfg.deny_commands, text)
        await self._check_registry_denial(text)

        adapter = self.backend.get_adapter()
        data = await self._call(adapter, "get_msg", message_id=ref.message_id)
        bot_self_id = self.cfg.self_id
        if not bot_self_id and not (isinstance(data, dict) and data.get("self_id")):
            info = await self._call(adapter, "get_login_info")
            if isinstance(info, dict):
                bot_self_id = str(info.get("user_id") or "")

        now = self._clock()
        resolved = resolve_message(
            data,
            ref,
            now=now,
            max_age_seconds=self.cfg.max_message_age_seconds,
            bot_self_id=bot_self_id,
        )
        payload = build_payload(resolved, text, now=now)
        try:
            await self._inject(adapter, payload)
        except BridgeError:
            raise
        except Exception as e:
            self.log.exception("[mcp_bridge] injecting synthetic event failed")
            raise BridgeError(
                "internal_error", f"向 AstrBot 投递消息失败: {type(e).__name__}"
            ) from e

        self.log.info(
            "[mcp_bridge] run_command %r as %s(%s) in %s (source message %s)",
            text,
            resolved.sender_name,
            resolved.user_id,
            resolved.chat_label,
            resolved.message_id,
        )
        where = f"群 {resolved.group_id}" if resolved.message_type == "group" else "私聊"
        return {
            "ok": True,
            "status": "submitted",
            "command": text,
            "as_user": {"user_id": resolved.user_id, "name": resolved.sender_name},
            "chat": resolved.chat_label,
            "message": (
                f"已以 {resolved.sender_name}({resolved.user_id}) 的身份在{where}把指令 "
                f"「{text}」交给 AstrBot。插件的回复会直接发到该聊天里，"
                "本工具不等待也不返回插件输出；"
                "如果指令不存在或无权限，聊天里可能没有任何回复。"
            ),
        }
