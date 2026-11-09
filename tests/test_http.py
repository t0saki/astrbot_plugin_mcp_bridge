"""HTTP layer: auth, plain JSON endpoints, raw JSON-RPC, lifecycle."""

from __future__ import annotations

import json
import socket

import aiohttp
import pytest

from mcp_bridge.http_server import BridgeServer, check_bearer

from .fakes import USER, make_service

TOKEN = "s3cret-token-0123456789abcdef"


def test_check_bearer():
    assert check_bearer(f"Bearer {TOKEN}", TOKEN)
    assert check_bearer(f"bearer   {TOKEN} ", TOKEN)
    assert not check_bearer(f"Bearer {TOKEN}x", TOKEN)
    assert not check_bearer(f"Basic {TOKEN}", TOKEN)
    assert not check_bearer(TOKEN, TOKEN)
    assert not check_bearer(None, TOKEN)
    assert not check_bearer("Bearer ", "")
    assert not check_bearer("Bearer ", TOKEN)


def test_empty_token_refused():
    service, _ = make_service()
    with pytest.raises(ValueError):
        BridgeServer(service, host="127.0.0.1", port=0, token="")


@pytest.fixture
async def server():
    service, adapter = make_service(allow=["gs体力"], extra_commands=["gs体力 | 查询体力"])
    srv = BridgeServer(service, host="127.0.0.1", port=0, token=TOKEN)
    await srv.start()
    srv.adapter = adapter
    yield srv
    await srv.stop()


def url(srv, path):
    return f"http://127.0.0.1:{srv.bound_port}{path}"


AUTH = {"Authorization": f"Bearer {TOKEN}"}


async def test_auth_required_everywhere_but_healthz(server):
    async with aiohttp.ClientSession() as s:
        async with s.get(url(server, "/healthz")) as r:
            assert r.status == 200
        for method, path in [("GET", "/v1/commands"), ("POST", "/v1/run"), ("POST", "/mcp")]:
            for headers in [{}, {"Authorization": "Bearer wrong"}, {"Authorization": TOKEN}]:
                async with s.request(method, url(server, path), headers=headers, json={}) as r:
                    assert r.status == 401, (method, path, headers)
                    assert r.headers["WWW-Authenticate"].startswith("Bearer")
                    assert TOKEN not in await r.text()
        async with s.get(
            url(server, "/v1/commands"), headers={**AUTH, "Origin": "http://evil"}
        ) as r:
            assert r.status == 403


async def test_plain_http_endpoints(server):
    async with aiohttp.ClientSession(headers=AUTH) as s:
        async with s.get(url(server, "/v1/commands")) as r:
            assert r.status == 200
            body = await r.json()
            assert body["commands"] == [
                {"command": "gs体力", "description": "查询体力", "source": "extra"}
            ]
        async with s.post(
            url(server, "/v1/run"), json={"message_id": "-1234567", "command": "gs体力"}
        ) as r:
            assert r.status == 200
            body = await r.json()
            assert body["ok"] and body["as_user"]["user_id"] == str(USER)
        async with s.post(
            url(server, "/v1/run"), json={"message_id": -1234567, "command": "/reset"}
        ) as r:
            assert r.status == 403 and (await r.json())["error"] == "not_allowed"
        async with s.post(url(server, "/v1/run"), json={"message_id": 5, "command": "gs体力"}) as r:
            assert r.status == 404 and (await r.json())["error"] == "message_not_found"
        async with s.post(url(server, "/v1/run"), data="not json") as r:
            assert r.status == 400
        async with s.post(url(server, "/v1/run"), data=b"x" * (70 * 1024)) as r:
            assert r.status == 413
    assert len(server.adapter.handled) == 1


async def rpc(s, srv, payload, status=200):
    headers = {"Accept": "application/json, text/event-stream"}
    async with s.post(url(srv, "/mcp"), data=json.dumps(payload), headers=headers) as r:
        assert r.status == status
        if status == 202:
            return None
        assert r.content_type == "application/json"
        return await r.json()


async def test_raw_jsonrpc(server):
    async with aiohttp.ClientSession(headers=AUTH) as s:
        init = await rpc(
            s,
            server,
            {
                "jsonrpc": "2.0",
                "id": 1,
                "method": "initialize",
                "params": {"protocolVersion": "2025-03-26"},
            },
        )
        assert init["result"]["protocolVersion"] == "2025-03-26"
        assert "tools" in init["result"]["capabilities"]
        init = await rpc(
            s,
            server,
            {
                "jsonrpc": "2.0",
                "id": 1,
                "method": "initialize",
                "params": {"protocolVersion": "1999-01-01"},
            },
        )
        assert init["result"]["protocolVersion"] == "2025-11-25"
        await rpc(s, server, {"jsonrpc": "2.0", "method": "notifications/initialized"}, 202)
        assert (await rpc(s, server, {"jsonrpc": "2.0", "id": "p", "method": "ping"}))[
            "result"
        ] == {}
        tools = await rpc(s, server, {"jsonrpc": "2.0", "id": 2, "method": "tools/list"})
        assert [t["name"] for t in tools["result"]["tools"]] == ["list_commands", "run_command"]
        err = await rpc(s, server, {"jsonrpc": "2.0", "id": 3, "method": "nope"})
        assert err["error"]["code"] == -32601
        err = await rpc(
            s,
            server,
            {
                "jsonrpc": "2.0",
                "id": 4,
                "method": "tools/call",
                "params": {"name": "nope", "arguments": {}},
            },
        )
        assert err["error"]["code"] == -32602
        batch = await rpc(
            s,
            server,
            [
                {"jsonrpc": "2.0", "id": 5, "method": "ping"},
                {"jsonrpc": "2.0", "method": "notifications/x"},
            ],
        )
        assert batch == [{"jsonrpc": "2.0", "id": 5, "result": {}}]
        async with s.post(url(server, "/mcp"), data="{bad") as r:
            assert r.status == 400 and (await r.json())["error"]["code"] == -32700
        async with s.get(url(server, "/mcp")) as r:
            assert r.status == 405
        async with s.delete(url(server, "/mcp")) as r:
            assert r.status == 405


async def test_stop_releases_port_and_restart_on_same_port():
    service, _ = make_service()
    first = BridgeServer(service, host="127.0.0.1", port=0, token=TOKEN)
    await first.start()
    port = first.bound_port
    await first.stop()
    with socket.socket() as sock:  # port is free again
        sock.setsockopt(socket.SOL_SOCKET, socket.SO_REUSEADDR, 1)
        sock.bind(("127.0.0.1", port))
    second = BridgeServer(service, host="127.0.0.1", port=port, token=TOKEN)
    await second.start()  # simulated plugin reload
    try:
        async with aiohttp.ClientSession() as s:
            async with s.get(f"http://127.0.0.1:{port}/healthz") as r:
                assert r.status == 200
    finally:
        await second.stop()
    await second.stop()  # idempotent
