"""Round trip with the official ``mcp`` SDK streamable-HTTP client."""

from __future__ import annotations

import httpx
import pytest

mcp = pytest.importorskip("mcp")

from mcp import ClientSession  # noqa: E402
from mcp.client.streamable_http import streamablehttp_client  # noqa: E402

from mcp_bridge.http_server import BridgeServer  # noqa: E402

from .fakes import GROUP, USER, make_service  # noqa: E402

TOKEN = "roundtrip-token-0123456789abcdef"


@pytest.fixture
async def server():
    service, adapter = make_service(
        allow=["gs体力", "gs查询*"], extra_commands=["gs体力 | 查询原神体力"]
    )
    srv = BridgeServer(service, host="127.0.0.1", port=0, token=TOKEN)
    await srv.start()
    srv.adapter = adapter
    yield srv
    await srv.stop()


async def test_mcp_sdk_round_trip(server):
    endpoint = f"http://127.0.0.1:{server.bound_port}/mcp"
    headers = {"Authorization": f"Bearer {TOKEN}"}
    async with streamablehttp_client(endpoint, headers=headers) as (read, write, _):
        async with ClientSession(read, write) as session:
            init = await session.initialize()
            assert init.serverInfo.name == "astrbot-mcp-bridge"

            tools = await session.list_tools()
            assert {t.name for t in tools.tools} == {"list_commands", "run_command"}

            listed = await session.call_tool("list_commands", {})
            assert not listed.isError
            commands = listed.structuredContent["commands"]
            assert {c["command"] for c in commands} == {"gs体力", "gs查询*"}
            assert "gs体力 — 查询原神体力" in listed.content[0].text

            ok = await session.call_tool(
                "run_command",
                {"message_id": f"ob:group:{GROUP}:-1234567", "command": "gs查询123456789"},
            )
            assert not ok.isError, ok.content
            assert ok.structuredContent["as_user"]["user_id"] == str(USER)
            assert "不返回插件输出" in ok.content[0].text

            denied = await session.call_tool(
                "run_command", {"message_id": "-1234567", "command": "/reset"}
            )
            assert denied.isError
            assert denied.structuredContent["error"] == "not_allowed"

            # integer id passes through the SDK too
            ok2 = await session.call_tool(
                "run_command", {"message_id": -1234567, "command": "gs体力"}
            )
            assert not ok2.isError

            await session.send_ping()

    injected = server.adapter.converted
    assert [e["message"][0]["data"]["text"] for e in injected] == ["gs查询123456789", "gs体力"]
    assert all(e["user_id"] == USER and e["group_id"] == GROUP for e in injected)


async def test_mcp_sdk_bad_token(server):
    endpoint = f"http://127.0.0.1:{server.bound_port}/mcp"
    with pytest.raises(BaseException) as ei:
        async with streamablehttp_client(endpoint, headers={"Authorization": "Bearer wrong"}) as (
            read,
            write,
            _,
        ):
            async with ClientSession(read, write) as session:
                await session.initialize()

    def flatten(exc):
        yield exc
        for sub in getattr(exc, "exceptions", ()) or ():
            yield from flatten(sub)

    found = [e for e in flatten(ei.value) if isinstance(e, httpx.HTTPStatusError)]
    assert found and found[0].response.status_code == 401
    assert server.adapter.converted == []


async def test_mcp_sdk_default_open_and_deny():
    service, adapter = make_service(
        allow=[], deny_commands=["gs绑定"], extra_commands=["gs体力 | 体力", "gs绑定 | 绑定"]
    )
    srv = BridgeServer(service, host="127.0.0.1", port=0, token=TOKEN)
    await srv.start()
    try:
        endpoint = f"http://127.0.0.1:{srv.bound_port}/mcp"
        headers = {"Authorization": f"Bearer {TOKEN}"}
        async with streamablehttp_client(endpoint, headers=headers) as (read, write, _):
            async with ClientSession(read, write) as session:
                await session.initialize()
                listed = await session.call_tool("list_commands", {})
                assert [c["command"] for c in listed.structuredContent["commands"]] == ["gs体力"]
                ok = await session.call_tool(
                    "run_command", {"message_id": "-1234567", "command": "gs深渊"}
                )
                assert not ok.isError, ok.content
                denied = await session.call_tool(
                    "run_command", {"message_id": "-1234567", "command": "gs绑定uid1"}
                )
                assert denied.isError and denied.structuredContent["error"] == "denied"
    finally:
        await srv.stop()
    assert [e["message"][0]["data"]["text"] for e in adapter.converted] == ["gs深渊"]
