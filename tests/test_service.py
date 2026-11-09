"""BridgeService with a fake AstrBot backend."""

from __future__ import annotations

import asyncio

import pytest

from mcp_bridge.config import BridgeConfig
from mcp_bridge.errors import BridgeError
from mcp_bridge.service import BridgeService

from .fakes import BOT, GROUP, NOW, USER, ApiNotAvailable, FakeBackend, group_msg, make_service


async def refused(service, message_id, command) -> BridgeError:
    with pytest.raises(BridgeError) as ei:
        await service.run_command(message_id, command)
    return ei.value


async def test_run_command_injects_as_resolved_sender():
    service, adapter = make_service()
    result = await service.run_command("ob:group:123456789:-1234567", "  gs体力  ")
    assert result["ok"] and result["status"] == "submitted"
    assert result["as_user"] == {"user_id": str(USER), "name": "明明"}
    assert result["chat"] == f"group:{GROUP}"
    assert adapter.client.calls == [("get_msg", {"message_id": -1234567})]
    (event,) = adapter.converted
    assert event["user_id"] == USER and event["group_id"] == GROUP and event["self_id"] == BOT
    assert event["message"] == [{"type": "text", "data": {"text": "gs体力"}}]
    assert event["time"] == int(NOW)
    assert adapter.handled == [{"abm_for": dict(event)}]


async def test_disallowed_command_never_touches_onebot():
    service, adapter = make_service(allow=["gs体力"])
    err = await refused(service, -1234567, "/reset")
    assert err.code == "not_allowed"
    assert adapter.client.calls == [] and adapter.converted == []


@pytest.mark.parametrize(
    "msg,code",
    [
        (group_msg(user_id=BOT, sender={"user_id": BOT}), "self_message"),
        (group_msg(time=int(NOW) - 3600), "stale_message"),
    ],
)
async def test_refuses_self_and_stale(msg, code):
    service, adapter = make_service(messages={-1234567: msg})
    assert (await refused(service, -1234567, "gs体力")).code == code
    assert adapter.converted == []


async def test_unknown_message():
    service, adapter = make_service(messages={})
    err = await refused(service, 777, "gs体力")
    assert err.code == "message_not_found" and "消息不存在" in err.message
    assert adapter.converted == []


async def test_get_login_info_fallback_when_get_msg_has_no_self_id():
    msg = group_msg()
    msg.pop("self_id")
    service, adapter = make_service(messages={-1234567: msg})
    await service.run_command(-1234567, "gs体力")
    assert [c[0] for c in adapter.client.calls] == ["get_msg", "get_login_info"]
    assert adapter.converted[0]["self_id"] == BOT


async def test_configured_self_id_is_routed_and_checked():
    service, adapter = make_service(self_id=str(BOT))
    await service.run_command(-1234567, "gs体力")
    assert adapter.client.calls == [("get_msg", {"message_id": -1234567, "self_id": BOT})]
    service, adapter = make_service(self_id="424242")
    assert (await refused(service, -1234567, "gs体力")).code == "platform_unavailable"


async def test_onebot_not_connected():
    service, adapter = make_service()
    adapter.client.fail = ApiNotAvailable()
    assert (await refused(service, -1234567, "gs体力")).code == "platform_unavailable"


async def test_onebot_timeout(monkeypatch):
    import mcp_bridge.service as svc

    monkeypatch.setattr(svc, "ACTION_TIMEOUT_SECONDS", 0.01)
    service, adapter = make_service()

    async def hang(action, **params):
        await asyncio.sleep(5)

    adapter.client.call_action = hang
    assert (await refused(service, -1234567, "gs体力")).code == "platform_unavailable"


async def test_no_platform():
    cfg = BridgeConfig.from_dict({"token": "x" * 32, "allow_commands": ["gs体力"]})
    service = BridgeService(cfg, FakeBackend(None), clock=lambda: NOW)
    assert (await refused(service, -1234567, "gs体力")).code == "platform_unavailable"


async def test_rate_limit_counts_every_attempt():
    service, adapter = make_service(rate_limit_per_minute=2)
    await service.run_command(-1234567, "gs体力")
    assert (await refused(service, -1234567, "nope")).code == "not_allowed"
    assert (await refused(service, -1234567, "gs体力")).code == "rate_limited"
    assert len(adapter.converted) == 1


async def test_injector_failure_is_reported():
    service, adapter = make_service()

    async def boom(abm):
        raise RuntimeError("queue closed")

    adapter.handle_msg = boom
    assert (await refused(service, -1234567, "gs体力")).code == "internal_error"


async def test_list_commands_survives_registry_failure():
    service, _ = make_service(allow=["gs体力"], extra_commands=["gs体力 | 体力"])

    async def broken():
        raise RuntimeError("db not ready")

    service.backend.registry = broken
    assert await service.list_commands() == [
        {"command": "gs体力", "description": "体力", "source": "extra"}
    ]


RESET = [
    {
        "effective_command": "reset",
        "aliases": ["重置"],
        "parent_signature": "",
        "description": "重置会话",
        "enabled": True,
        "plugin_activated": True,
        "sub_commands": [],
    }
]


async def test_default_open_runs_any_command():
    service, adapter = make_service(allow=[])
    await service.run_command(-1234567, "/whatever 1 2")
    await service.run_command(-1234567, "gs绑定uid123")
    texts = [e["message"][0]["data"]["text"] for e in adapter.converted]
    assert texts == ["/whatever 1 2", "gs绑定uid123"]


async def test_deny_wins_over_allow():
    service, adapter = make_service(allow=["gs*"], deny_commands=["gs绑定"])
    await service.run_command(-1234567, "gs体力")
    err = await refused(service, -1234567, "GS绑定 123")
    assert err.code == "denied"
    assert len(adapter.converted) == 1 and adapter.client.calls == [
        ("get_msg", {"message_id": -1234567})
    ]


async def test_deny_in_default_open_mode_never_touches_onebot():
    service, adapter = make_service(allow=[], deny_commands=["/reset"], registry=RESET)
    for cmd in ["/reset", "/重置", "重置", "/RESET now"]:
        assert (await refused(service, -1234567, cmd)).code == "denied", cmd
    assert adapter.client.calls == [] and adapter.converted == []
    with pytest.raises(BridgeError):  # deny is a broad prefix match on purpose
        await service.run_command(-1234567, "/resetx")
    await service.run_command(-1234567, "/new")
    assert len(adapter.converted) == 1


async def test_deny_still_applies_textually_when_registry_fails():
    service, adapter = make_service(allow=[], deny_commands=["/reset"], registry=RESET)

    async def broken():
        raise RuntimeError("db not ready")

    service.backend.registry = broken
    assert (await refused(service, -1234567, "/reset")).code == "denied"
    await service.run_command(-1234567, "/重置")  # alias expansion needs the registry
    assert len(adapter.converted) == 1


async def test_list_commands_default_open_minus_denied():
    service, _ = make_service(
        allow=[],
        deny_commands=["/重置"],
        extra_commands=["gs体力 | 体力", "gs绑定 | 绑定"],
        registry=RESET
        + [
            {
                "effective_command": "help",
                "aliases": [],
                "description": "帮助",
                "enabled": True,
                "plugin_activated": True,
                "sub_commands": [],
            }
        ],
    )
    assert [c["command"] for c in await service.list_commands()] == ["/help", "gs体力", "gs绑定"]
