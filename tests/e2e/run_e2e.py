"""End-to-end test against a real AstrBot process.

Boots AstrBot (fresh data root, no LLM provider, no dashboard, outbound network
blocked) with an aiocqhttp platform on a free 127.0.0.1 port, this plugin and a
tiny probe plugin. A fake OneBot implementation connects over reverse
WebSocket, answers ``get_msg`` / ``get_login_info`` and records every
``send_*_msg``. The test then drives the bridge through the real ``mcp`` SDK
client and the plain HTTP endpoints.

Run with a scrubbed environment, e.g.::

    env -i PATH=<venv>/bin:/usr/bin:/bin HOME=<work>/home \\
        <venv>/bin/python tests/e2e/run_e2e.py --astrbot-src <AstrBot> --work <work>
"""

from __future__ import annotations

import argparse
import asyncio
import json
import os
import shutil
import signal
import socket
import subprocess
import sys
import time
from pathlib import Path

import aiohttp
import websockets
from mcp import ClientSession
from mcp.client.streamable_http import streamablehttp_client

HERE = Path(__file__).resolve().parent
PLUGIN_DIR = HERE.parent.parent
BOT, USER, ADMIN, GROUP = 10000, 20001, 30003, 123456789
TOKEN = "e2e-token-0123456789abcdef0123456789"


def free_port() -> int:
    with socket.socket() as s:
        s.bind(("127.0.0.1", 0))
        return s.getsockname()[1]


def port_is_free(port: int) -> bool:
    with socket.socket() as s:
        s.setsockopt(socket.SOL_SOCKET, socket.SO_REUSEADDR, 1)
        try:
            s.bind(("127.0.0.1", port))
            return True
        except OSError:
            return False


class FakeOneBot:
    """Minimal OneBot v11 implementation speaking reverse WebSocket (Universal)."""

    def __init__(self, ws_port: int) -> None:
        self.url = f"ws://127.0.0.1:{ws_port}/ws/"
        self.messages: dict[int, dict] = {}
        self.sent: list[dict] = []
        self.actions: list[str] = []
        self.ws = None
        self._seq = 1000

    def add_message(
        self,
        message_id: int,
        *,
        user: int,
        text: str,
        group: int | None = GROUP,
        age: float = 5,
        role: str = "member",
    ) -> dict:
        data = {
            "self_id": BOT,
            "user_id": user,
            "time": int(time.time() - age),
            "message_id": message_id,
            "real_id": message_id,
            "message_seq": message_id,
            "message_type": "group" if group else "private",
            "sub_type": "normal" if group else "friend",
            "sender": {"user_id": user, "nickname": f"nick{user}", "card": "", "role": role},
            "message": [{"type": "text", "data": {"text": text}}],
            "raw_message": text,
            "font": 14,
            "post_type": "message_sent" if user == BOT else "message",
            "message_format": "array",
        }
        if group:
            data["group_id"] = group
            data["group_name"] = "e2e group"
        self.messages[message_id] = data
        return data

    async def connect(self, timeout: float = 90) -> None:
        deadline = time.monotonic() + timeout
        headers = {"X-Self-ID": str(BOT), "X-Client-Role": "Universal"}
        while True:
            try:
                self.ws = await websockets.connect(self.url, additional_headers=headers)
                break
            except (OSError, websockets.InvalidStatus, websockets.InvalidHandshake):
                if time.monotonic() > deadline:
                    raise
                await asyncio.sleep(0.5)
        asyncio.create_task(self._loop())

    async def push_event(self, message_id: int) -> None:
        payload = dict(self.messages[message_id])
        payload["post_type"] = "message"
        await self.ws.send(json.dumps(payload))

    async def _loop(self) -> None:
        async for raw in self.ws:
            req = json.loads(raw)
            action, params, echo = req.get("action"), req.get("params") or {}, req.get("echo")
            self.actions.append(action)
            resp = {"status": "ok", "retcode": 0, "data": None, "echo": echo}
            if action == "get_msg":
                msg = self.messages.get(int(params.get("message_id")))
                if msg is None:
                    resp = {
                        "status": "failed",
                        "retcode": 1200,
                        "data": None,
                        "message": "消息不存在",
                        "wording": "消息不存在",
                        "echo": echo,
                    }
                else:
                    resp["data"] = msg
            elif action == "get_login_info":
                resp["data"] = {"user_id": BOT, "nickname": "e2e-bot"}
            elif action in ("send_group_msg", "send_private_msg", "send_msg"):
                self._seq += 1
                self.sent.append({"action": action, **params})
                resp["data"] = {"message_id": self._seq}
            await self.ws.send(json.dumps(resp))

    @staticmethod
    def text_of(sent: dict) -> str:
        msg = sent.get("message")
        if isinstance(msg, str):
            return msg
        return "".join(
            seg.get("data", {}).get("text", "") for seg in msg or [] if seg.get("type") == "text"
        )

    async def wait_sent(self, predicate, timeout: float = 15) -> dict:
        deadline = time.monotonic() + timeout
        seen = 0
        while time.monotonic() < deadline:
            for item in self.sent[seen:]:
                if predicate(item):
                    return item
            await asyncio.sleep(0.1)
        raise AssertionError(f"no matching send within {timeout}s; sent={self.sent}")


class Report:
    def __init__(self) -> None:
        self.passed: list[str] = []

    def ok(self, name: str) -> None:
        self.passed.append(name)
        print(f"PASS {name}", flush=True)


def prepare_root(work: Path, ws_port: int, mcp_port: int) -> Path:
    root = work / f"root-{int(time.time())}"
    plugins = root / "data" / "plugins"
    config = root / "data" / "config"
    plugins.mkdir(parents=True)
    config.mkdir(parents=True)
    shutil.copytree(
        PLUGIN_DIR,
        plugins / "astrbot_plugin_mcp_bridge",
        ignore=shutil.ignore_patterns("tests", "__pycache__", ".pytest_cache", ".ruff_cache"),
    )
    shutil.copytree(HERE / "probe_plugin", plugins / "astrbot_plugin_e2e_probe")
    (root / "data" / "cmd_config.json").write_text(
        json.dumps(
            {
                "platform": [
                    {
                        "id": "e2e_onebot",
                        "type": "aiocqhttp",
                        "enable": True,
                        "ws_reverse_host": "127.0.0.1",
                        "ws_reverse_port": ws_port,
                        "ws_reverse_token": "",
                    }
                ],
                "wake_prefix": ["/"],
                "admins_id": [str(ADMIN)],
                "disable_metrics": True,
                "platform_settings": {"reply_with_quote": True},
            },
            ensure_ascii=False,
            indent=2,
        )
    )
    (config / "astrbot_plugin_mcp_bridge_config.json").write_text(
        json.dumps(
            {
                "token": TOKEN,
                "listen_host": "127.0.0.1",
                "listen_port": mcp_port,
                "allow_commands": ["/e2e ping", "/e2eadmin", "gs体力"],
                "extra_commands": ["gs体力 | 查询原神体力"],
                "max_message_age_seconds": 600,
                "rate_limit_per_minute": 100,
            },
            ensure_ascii=False,
            indent=2,
        )
    )
    return root


async def wait_health(mcp_port: int, timeout: float = 120) -> None:
    deadline = time.monotonic() + timeout
    async with aiohttp.ClientSession() as s:
        while time.monotonic() < deadline:
            try:
                async with s.get(f"http://127.0.0.1:{mcp_port}/healthz") as r:
                    if r.status == 200:
                        return
            except aiohttp.ClientError:
                pass
            await asyncio.sleep(0.5)
    raise AssertionError("bridge /healthz never came up")


async def mcp_call(endpoint: str, calls: list[tuple[str, dict]]) -> list:
    out = []
    headers = {"Authorization": f"Bearer {TOKEN}"}
    async with streamablehttp_client(endpoint, headers=headers) as (read, write, _):
        async with ClientSession(read, write) as session:
            await session.initialize()
            for name, args in calls:
                out.append(await session.call_tool(name, args))
    return out


async def scenario(ob: FakeOneBot, mcp_port: int, log_path: Path, rep: Report) -> None:
    endpoint = f"http://127.0.0.1:{mcp_port}/mcp"

    # list_commands -----------------------------------------------------------
    (listed,) = await mcp_call(endpoint, [("list_commands", {})])
    cmds = {c["command"]: c for c in listed.structuredContent["commands"]}
    assert cmds["/e2e ping"]["source"] == "astrbot", cmds
    assert cmds["/e2e ping"]["description"].startswith("回显发送者身份"), cmds
    assert cmds["/e2eadmin"]["permission"] == "admin", cmds
    assert cmds["gs体力"] == {
        "command": "gs体力",
        "description": "查询原神体力",
        "source": "extra",
    }, cmds
    assert "/e2esecret" not in cmds and "/e2ereload" not in cmds
    rep.ok("list_commands: registry (with descriptions/permission) + extra, filtered by allowlist")

    # a real user message the agent saw ------------------------------------------
    m1 = -1234567
    ob.add_message(m1, user=USER, text="帮我看看体力")
    await ob.push_event(m1)
    await asyncio.sleep(1.0)
    assert not ob.sent, f"plain chat message should not trigger a reply: {ob.sent}"

    (res,) = await mcp_call(
        endpoint, [("run_command", {"message_id": str(m1), "command": "/e2e ping"})]
    )
    assert not res.isError, res.content
    sent = await ob.wait_sent(lambda s: "pong" in ob.text_of(s))
    assert sent["action"] == "send_group_msg" and sent["group_id"] == GROUP, sent
    assert f"pong user={USER} group={GROUP} admin=False mid={m1}" in ob.text_of(sent), sent
    first = sent["message"][0]
    assert first["type"] == "reply" and str(first["data"]["id"]) == str(m1), sent
    rep.ok("run_command /e2e ping -> reply in the right group as the real sender, quoting it")

    n = len(ob.sent)
    (res,) = await mcp_call(
        endpoint, [("run_command", {"message_id": f"ob:group:{GROUP}:{m1}", "command": "gs体力"})]
    )
    assert not res.isError, res.content
    sent = await ob.wait_sent(lambda s: "catchall" in ob.text_of(s))
    assert sent["group_id"] == GROUP
    assert f"catchall text=gs体力 user={USER} group={GROUP} admin=False" in ob.text_of(sent)
    rep.ok("run_command gs体力 (ob:group: id) -> catch-all listener sees real sender")

    # permission comes from the real sender --------------------------------------------
    (res,) = await mcp_call(endpoint, [("run_command", {"message_id": m1, "command": "/e2eadmin"})])
    assert not res.isError, res.content
    await asyncio.sleep(2.0)
    assert not any("admin-ok" in ob.text_of(s) for s in ob.sent[n:]), ob.sent[n:]
    rep.ok("run_command /e2eadmin as a member -> AstrBot permission filter blocks it")

    m2 = -2000002
    ob.add_message(m2, user=ADMIN, text="帮我看看", role="owner")
    (res,) = await mcp_call(endpoint, [("run_command", {"message_id": m2, "command": "/e2eadmin"})])
    assert not res.isError, res.content
    sent = await ob.wait_sent(lambda s: "admin-ok" in ob.text_of(s))
    assert f"admin-ok user={ADMIN} group={GROUP} admin=True" in ob.text_of(sent), sent
    rep.ok("run_command /e2eadmin as an admin -> runs (role derived from admins_id)")

    m3 = 3000003
    ob.add_message(m3, user=USER, text="体力？", group=None)
    (res,) = await mcp_call(endpoint, [("run_command", {"message_id": m3, "command": "/e2e ping"})])
    assert not res.isError, res.content
    sent = await ob.wait_sent(lambda s: s["action"] == "send_private_msg")
    assert sent["user_id"] == USER and f"pong user={USER} group=-" in ob.text_of(sent), sent
    rep.ok("run_command in a private chat -> send_private_msg to the sender")

    # refusals ---------------------------------------------------------------------
    m4, m5 = -4000004, -5000005
    ob.add_message(m4, user=USER, text="old", age=3600)
    ob.add_message(m5, user=BOT, text="bot said this")
    before = len(ob.sent)
    results = await mcp_call(
        endpoint,
        [
            ("run_command", {"message_id": m1, "command": "/e2esecret"}),
            ("run_command", {"message_id": m1, "command": "/e2eopen"}),
            ("run_command", {"message_id": m4, "command": "/e2e ping"}),
            ("run_command", {"message_id": m5, "command": "/e2e ping"}),
            ("run_command", {"message_id": 999999, "command": "/e2e ping"}),
            ("run_command", {"message_id": f"ob:group:1:{m1}", "command": "/e2e ping"}),
            ("run_command", {"message_id": m1, "command": "/e2e ping\n/e2esecret"}),
        ],
    )
    codes = [r.structuredContent.get("error") for r in results]
    assert all(r.isError for r in results), codes
    assert codes == [
        "not_allowed",
        "not_allowed",
        "stale_message",
        "self_message",
        "message_not_found",
        "chat_mismatch",
        "invalid_command",
    ], codes
    await asyncio.sleep(2.0)
    assert len(ob.sent) == before, ob.sent[before:]
    assert not any("SECRET-RAN" in ob.text_of(s) for s in ob.sent)
    rep.ok(
        "refusals: not allowlisted / stale / self / unknown / chat mismatch / multi-line; "
        "nothing sent"
    )

    # plain HTTP endpoints -------------------------------------------------------------
    base = f"http://127.0.0.1:{mcp_port}"
    async with aiohttp.ClientSession() as s:
        async with s.get(f"{base}/v1/commands") as r:
            assert r.status == 401
        auth = {"Authorization": f"Bearer {TOKEN}"}
        async with s.get(f"{base}/v1/commands", headers=auth) as r:
            assert r.status == 200 and any(
                c["command"] == "/e2e ping" for c in (await r.json())["commands"]
            )
        n = len(ob.sent)
        async with s.post(
            f"{base}/v1/run", headers=auth, json={"message_id": m1, "command": "/e2e ping"}
        ) as r:
            assert r.status == 200, await r.text()
        async with s.post(
            f"{base}/v1/run", headers=auth, json={"message_id": m4, "command": "/e2e ping"}
        ) as r:
            assert r.status == 403 and (await r.json())["error"] == "stale_message"
    await ob.wait_sent(lambda s: "pong" in ob.text_of(s) and s in ob.sent[n:])
    rep.ok("plain HTTP: 401 without token, /v1/commands, /v1/run ok + refusal")

    # plugin reload: server stops and comes back on the same port ------------------------
    m6 = -6000006
    ob.add_message(m6, user=ADMIN, text="/e2ereload", role="owner")
    await ob.push_event(m6)
    sent = await ob.wait_sent(lambda s: "reloaded" in ob.text_of(s), timeout=30)
    assert "ok=True" in ob.text_of(sent), sent
    await wait_health(mcp_port, timeout=30)
    n = len(ob.sent)
    (res,) = await mcp_call(endpoint, [("run_command", {"message_id": m1, "command": "/e2e ping"})])
    assert not res.isError, res.content
    await ob.wait_sent(lambda s: "pong" in ob.text_of(s) and s in ob.sent[n:])
    log = log_path.read_text(encoding="utf-8", errors="replace")
    assert log.count("[mcp_bridge] 已启动") == 2, "expected two starts (initial + reload)"
    assert "[mcp_bridge] 服务已停止" in log and "无法监听" not in log
    rep.ok("plugin reload: old server stopped, new one bound the same port, still works")

    # default-open mode with a denylist ----------------------------------------------------
    await reconfigure(
        ob,
        log_path.parent,
        mcp_port,
        -7000007,
        allow_commands=[],
        deny_commands=["/e2esecret", "gs绑定"],
    )
    (listed,) = await mcp_call(endpoint, [("list_commands", {})])
    cmds = {c["command"] for c in listed.structuredContent["commands"]}
    assert {"/e2e ping", "/e2eopen", "/e2eadmin", "/e2ereload", "gs体力"} <= cmds, cmds
    assert "/e2esecret" not in cmds and "/e2ehidden" not in cmds, cmds
    rep.ok("default-open list_commands: whole registry + extras, minus the denied command")

    n = len(ob.sent)
    (res,) = await mcp_call(endpoint, [("run_command", {"message_id": m1, "command": "/e2eopen"})])
    assert not res.isError, res.content
    sent = await ob.wait_sent(lambda s: "open-ok" in ob.text_of(s))
    assert f"open-ok user={USER} group={GROUP}" in ob.text_of(sent), sent
    rep.ok("default-open: /e2eopen (refused under the explicit allowlist) now runs")

    before = len(ob.sent)
    results = await mcp_call(
        endpoint,
        [
            ("run_command", {"message_id": m1, "command": "/e2esecret"}),
            ("run_command", {"message_id": m1, "command": "/e2ehidden"}),  # alias of /e2esecret
            ("run_command", {"message_id": m1, "command": "GS绑定 123"}),
        ],
    )
    codes = [r.structuredContent.get("error") for r in results]
    assert all(r.isError for r in results) and codes == ["denied"] * 3, codes
    await asyncio.sleep(2.0)
    assert len(ob.sent) == before, ob.sent[before:]
    rep.ok(
        "deny: denied command, its alias (via the registry) and a case/space variant of "
        "a GsCore prefix are refused; nothing sent"
    )

    # both lists empty: everything runs, startup warns ------------------------------------
    await reconfigure(ob, log_path.parent, mcp_port, -8000008, allow_commands=[], deny_commands=[])
    log = log_path.read_text(encoding="utf-8", errors="replace")
    assert log.count("allow_commands 与 deny_commands 均为空") == 1, "missing open-mode warning"
    (res,) = await mcp_call(
        endpoint, [("run_command", {"message_id": m1, "command": "/e2ehidden"})]
    )
    assert not res.isError, res.content
    await ob.wait_sent(lambda s: "SECRET-RAN" in ob.text_of(s))
    log = log_path.read_text(encoding="utf-8", errors="replace")
    assert log.count("[mcp_bridge] 已启动") == 4 and "无法监听" not in log
    assert TOKEN not in log, "token leaked into the log"
    rep.ok(
        "both lists empty: startup warning logged once, any command runs; "
        "4 starts on the same port; token never logged"
    )


async def reconfigure(ob: FakeOneBot, root: Path, mcp_port: int, mid: int, **changes) -> None:
    """Rewrite the bridge config and reload the plugin through a real admin message."""
    path = root / "data" / "config" / "astrbot_plugin_mcp_bridge_config.json"
    cfg = json.loads(path.read_text(encoding="utf-8-sig"))  # AstrBot writes a BOM
    cfg.update(changes)
    path.write_text(json.dumps(cfg, ensure_ascii=False, indent=2), encoding="utf-8")
    n = len(ob.sent)
    ob.add_message(mid, user=ADMIN, text="/e2ereload", role="owner")
    await ob.push_event(mid)
    sent = await ob.wait_sent(lambda s: "reloaded" in ob.text_of(s) and s in ob.sent[n:], 30)
    assert "ok=True" in ob.text_of(sent), sent
    await wait_health(mcp_port, timeout=30)


async def main_async(args) -> int:
    work = Path(args.work).resolve()
    (work / "home").mkdir(parents=True, exist_ok=True)
    ws_port, mcp_port = free_port(), free_port()
    root = prepare_root(work, ws_port, mcp_port)
    log_path = root / "astrbot.log"
    venv_bin = str(Path(sys.executable).parent)
    env = {
        "PATH": f"{venv_bin}:/usr/bin:/bin",
        "HOME": str(work / "home"),
        "ASTRBOT_ROOT": str(root),
        "ASTRBOT_DISABLE_METRICS": "1",
        "PYTHONUNBUFFERED": "1",
        "PYTHONDONTWRITEBYTECODE": "1",
    }
    log_file = open(log_path, "w", encoding="utf-8")
    proc = subprocess.Popen(
        [sys.executable, str(HERE / "boot_astrbot.py")],
        cwd=args.astrbot_src,
        env=env,
        stdout=log_file,
        stderr=subprocess.STDOUT,
        start_new_session=True,
    )
    print(f"AstrBot pid={proc.pid} root={root} ws={ws_port} mcp={mcp_port}", flush=True)
    rep = Report()
    rc = 1
    try:
        await wait_health(mcp_port)
        rep.ok("AstrBot booted; bridge plugin serving /healthz")
        ob = FakeOneBot(ws_port)
        await ob.connect()
        await asyncio.sleep(1.0)
        rep.ok("fake OneBot connected over reverse WebSocket")
        await scenario(ob, mcp_port, log_path, rep)
        rc = 0
    except Exception as e:
        import traceback

        traceback.print_exc()
        print(f"FAIL: {e!r}", flush=True)
    finally:
        try:
            os.killpg(proc.pid, signal.SIGTERM)
            proc.wait(timeout=15)
        except subprocess.TimeoutExpired:
            os.killpg(proc.pid, signal.SIGKILL)
            proc.wait(timeout=5)
        except ProcessLookupError:
            pass
        log_file.close()
        freed = port_is_free(mcp_port) and port_is_free(ws_port)
        print(f"AstrBot exited rc={proc.returncode}; ports freed={freed}", flush=True)
        print(f"{len(rep.passed)} e2e checks passed; log: {log_path}", flush=True)
    return rc


def main() -> None:
    parser = argparse.ArgumentParser()
    parser.add_argument("--astrbot-src", required=True)
    parser.add_argument("--work", required=True)
    sys.exit(asyncio.run(main_async(parser.parse_args())))


if __name__ == "__main__":
    main()
