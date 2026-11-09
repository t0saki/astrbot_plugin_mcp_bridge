"""Test doubles for the AstrBot side of the bridge."""

from __future__ import annotations

from typing import Any

from mcp_bridge.config import BridgeConfig
from mcp_bridge.errors import BridgeError
from mcp_bridge.service import BridgeService

BOT = 10000
USER = 20001
GROUP = 123456789
NOW = 1_800_000_000.0


class ActionFailed(Exception):
    """Same class name as aiocqhttp.exceptions.ActionFailed."""

    def __init__(self, result: dict) -> None:
        super().__init__(result)
        self.result = result


class ApiNotAvailable(Exception):
    pass


def group_msg(message_id: int = -1234567, **over: Any) -> dict:
    data = {
        "self_id": BOT,
        "user_id": USER,
        "time": int(NOW) - 30,
        "message_id": message_id,
        "real_id": message_id,
        "message_seq": message_id,
        "message_type": "group",
        "sub_type": "normal",
        "group_id": GROUP,
        "group_name": "测试群",
        "sender": {"user_id": USER, "nickname": "小明", "card": "明明", "role": "member"},
        "message": [{"type": "text", "data": {"text": "帮我看看体力"}}],
        "raw_message": "帮我看看体力",
        "font": 14,
        "post_type": "message",
    }
    data.update(over)
    return data


class FakeClient:
    def __init__(self, messages: dict[int, dict] | None = None, login: int = BOT) -> None:
        self.messages = messages or {}
        self.login = login
        self.calls: list[tuple[str, dict]] = []
        self.fail: Exception | None = None

    async def call_action(self, action: str, **params: Any) -> Any:
        self.calls.append((action, params))
        if self.fail is not None:
            raise self.fail
        if action == "get_msg":
            msg = self.messages.get(params["message_id"])
            if msg is None:
                raise ActionFailed({"status": "failed", "retcode": 1200, "wording": "消息不存在"})
            return msg
        if action == "get_login_info":
            return {"user_id": self.login, "nickname": "bot"}
        raise AssertionError(f"unexpected action {action}")


class FakeAdapter:
    def __init__(self, client: FakeClient) -> None:
        self.client = client
        self.converted: list[Any] = []
        self.handled: list[Any] = []

    def get_client(self) -> FakeClient:
        return self.client

    async def convert_message(self, event: Any) -> Any:
        self.converted.append(event)
        return {"abm_for": dict(event)}

    async def handle_msg(self, abm: Any) -> None:
        self.handled.append(abm)


class FakeBackend:
    def __init__(
        self,
        adapter: FakeAdapter | None,
        registry: list[dict] | None = None,
        prefixes: list[str] | None = None,
    ) -> None:
        self.adapter = adapter
        self._registry = registry or []
        self._prefixes = prefixes if prefixes is not None else ["/"]

    def get_adapter(self) -> FakeAdapter:
        if self.adapter is None:
            raise BridgeError("platform_unavailable", "no aiocqhttp platform")
        return self.adapter

    async def registry(self) -> list[dict]:
        return self._registry

    def wake_prefixes(self) -> list[str]:
        return self._prefixes


def make_service(
    allow: list[str] | None = None,
    messages: dict[int, dict] | None = None,
    **cfg: Any,
) -> tuple[BridgeService, FakeAdapter]:
    raw = {"token": "t" * 32, "allow_commands": allow if allow is not None else ["gs体力"]}
    raw.update(cfg)
    client = FakeClient(messages if messages is not None else {-1234567: group_msg()})
    adapter = FakeAdapter(client)
    service = BridgeService(BridgeConfig.from_dict(raw), FakeBackend(adapter), clock=lambda: NOW)
    return service, adapter
