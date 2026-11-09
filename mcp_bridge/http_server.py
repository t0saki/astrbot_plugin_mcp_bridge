"""HTTP server: MCP (Streamable HTTP, stateless, JSON responses) + plain JSON.

Implements the subset of the MCP Streamable HTTP transport a tools-only server
needs: ``POST /mcp`` carrying JSON-RPC ``initialize``,
``notifications/initialized`` (and any other notification), ``ping``,
``tools/list`` and ``tools/call``, always answered with ``application/json``.
No session id is issued, so clients do not open the optional GET/SSE stream;
``GET``/``DELETE /mcp`` return 405 as the spec allows.

Plain endpoints for clients without MCP:

* ``GET  /v1/commands``                         -> ``{"ok": true, "commands": [...]}``
* ``POST /v1/run`` ``{"message_id", "command"}`` -> ``{"ok": true, ...}`` or error
* ``GET  /healthz`` (no auth)                   -> ``{"ok": true}``

Every endpoint except ``/healthz`` requires ``Authorization: Bearer <token>``.
"""

from __future__ import annotations

import hmac
import json
import logging
from typing import Any

from aiohttp import web

from . import __version__
from .commands import format_command_list
from .errors import BridgeError
from .service import BridgeService

SERVER_NAME = "astrbot-mcp-bridge"
SUPPORTED_PROTOCOL_VERSIONS = ("2024-11-05", "2025-03-26", "2025-06-18", "2025-11-25")
LATEST_PROTOCOL_VERSION = SUPPORTED_PROTOCOL_VERSIONS[-1]
MAX_BODY_BYTES = 64 * 1024

INSTRUCTIONS = (
    "Runs AstrBot plugin commands (game queries, rss, ...) on behalf of a chat user. "
    "Call list_commands to see what may be run. When someone asks in natural language "
    "for something a command does, call run_command with the OneBot message id of that "
    "person's own recent message and the command text exactly as a user would type it. "
    "The plugin answers directly in that chat; the tool only confirms hand-off."
)

TOOLS: list[dict[str, Any]] = [
    {
        "name": "list_commands",
        "title": "List runnable AstrBot commands",
        "description": (
            "List the AstrBot plugin commands that run_command may execute, each with a "
            "one-line description. Commands are written exactly as a chat user types them "
            "(including any prefix such as '/' or 'gs'). An entry ending in '*' accepts an "
            "argument glued directly onto it (e.g. 'gs查询*' -> 'gs查询123456789')."
        ),
        "inputSchema": {"type": "object", "properties": {}, "additionalProperties": False},
        "annotations": {"readOnlyHint": True, "openWorldHint": False},
    },
    {
        "name": "run_command",
        "title": "Run an AstrBot command as a chat user",
        "description": (
            "Run an AstrBot plugin command as the author of the referenced message, in that "
            "message's chat. AstrBot processes it exactly as if that person had typed it "
            "(same permissions, same chat); the plugin's answer is posted straight into the "
            "chat. This tool returns as soon as the command is handed over and does NOT "
            "return the plugin's output. Refused if the message is unknown, sent by the bot, "
            "too old, or the command is not on the allowlist (see list_commands)."
        ),
        "inputSchema": {
            "type": "object",
            "properties": {
                "message_id": {
                    "type": "string",
                    "description": (
                        "OneBot message id of a recent message from the person who asked: the "
                        "bare integer (may be negative, e.g. '-1234567') or 'ob:<chat>:<id>' "
                        "such as 'ob:group:123456789:-1234567'."
                    ),
                },
                "command": {
                    "type": "string",
                    "description": (
                        "Single-line command exactly as the user would type it, e.g. "
                        "'gs体力' or '/rss list' (max 200 characters)."
                    ),
                },
            },
            "required": ["message_id", "command"],
            "additionalProperties": False,
        },
        "annotations": {
            "readOnlyHint": False,
            "destructiveHint": False,
            "idempotentHint": False,
            "openWorldHint": True,
        },
    },
]


def check_bearer(header: str | None, token: str) -> bool:
    """Constant-time check of an ``Authorization: Bearer <token>`` header."""
    if not token or not header:
        return False
    scheme, _, value = header.strip().partition(" ")
    if scheme.lower() != "bearer":
        return False
    return hmac.compare_digest(value.strip().encode("utf-8"), token.encode("utf-8"))


def _json(data: Any, status: int = 200, headers: dict | None = None) -> web.Response:
    return web.Response(
        text=json.dumps(data, ensure_ascii=False),
        status=status,
        content_type="application/json",
        headers=headers,
    )


def _rpc_result(msg_id: Any, result: Any) -> dict:
    return {"jsonrpc": "2.0", "id": msg_id, "result": result}


def _rpc_error(msg_id: Any, code: int, message: str) -> dict:
    return {"jsonrpc": "2.0", "id": msg_id, "error": {"code": code, "message": message}}


class BridgeServer:
    def __init__(
        self,
        service: BridgeService,
        *,
        host: str,
        port: int,
        token: str,
        logger: logging.Logger | None = None,
    ) -> None:
        if not token:
            raise ValueError("token must not be empty")
        self.service = service
        self.host = host
        self.port = port
        self._token = token
        self.log = logger or logging.getLogger("astrbot")
        self._runner: web.AppRunner | None = None
        self.bound_port: int | None = None

    # -- lifecycle ------------------------------------------------------------

    def build_app(self) -> web.Application:
        app = web.Application(client_max_size=MAX_BODY_BYTES, middlewares=[self._auth])
        app.router.add_get("/healthz", self._healthz)
        app.router.add_route("*", "/mcp", self._mcp)
        app.router.add_route("*", "/mcp/", self._mcp)
        app.router.add_get("/v1/commands", self._http_commands)
        app.router.add_post("/v1/run", self._http_run)
        return app

    async def start(self) -> None:
        runner = web.AppRunner(
            self.build_app(), access_log=None, handle_signals=False, shutdown_timeout=2.0
        )
        await runner.setup()
        site = web.TCPSite(runner, self.host, self.port)
        try:
            await site.start()
        except BaseException:
            await runner.cleanup()
            raise
        self._runner = runner
        addresses = runner.addresses
        self.bound_port = addresses[0][1] if addresses else self.port

    async def stop(self) -> None:
        runner, self._runner = self._runner, None
        if runner is not None:
            await runner.cleanup()

    # -- middleware -------------------------------------------------------------

    @web.middleware
    async def _auth(self, request: web.Request, handler):
        if request.path == "/healthz":
            return await handler(request)
        if request.headers.get("Origin"):
            # Browsers send Origin; agents do not. Blocks DNS-rebinding style access.
            return _json({"ok": False, "error": "forbidden", "message": "Origin not allowed"}, 403)
        if not check_bearer(request.headers.get("Authorization"), self._token):
            return _json(
                {
                    "ok": False,
                    "error": "unauthorized",
                    "message": "missing or invalid bearer token",
                },
                401,
                headers={"WWW-Authenticate": 'Bearer realm="astrbot-mcp-bridge"'},
            )
        return await handler(request)

    # -- plain HTTP -------------------------------------------------------------

    async def _healthz(self, request: web.Request) -> web.Response:
        return _json({"ok": True})

    async def _http_commands(self, request: web.Request) -> web.Response:
        commands = await self.service.list_commands()
        return _json({"ok": True, "commands": commands})

    async def _http_run(self, request: web.Request) -> web.Response:
        try:
            body = await request.json()
        except web.HTTPException:
            raise
        except Exception:
            return _json(BridgeError("invalid_argument", "请求体必须是 JSON").to_dict(), 400)
        if not isinstance(body, dict):
            return _json(BridgeError("invalid_argument", "请求体必须是 JSON 对象").to_dict(), 400)
        try:
            result = await self.service.run_command(body.get("message_id"), body.get("command"))
        except BridgeError as e:
            self._log_refusal(e, body.get("command"))
            return _json(e.to_dict(), e.http_status)
        except Exception:
            self.log.exception("[mcp_bridge] /v1/run failed")
            return _json(BridgeError("internal_error", "内部错误").to_dict(), 500)
        return _json(result)

    def _log_refusal(self, e: BridgeError, command: Any) -> None:
        shown = command[:80] if isinstance(command, str) else command
        self.log.info("[mcp_bridge] refused %r: %s (%s)", shown, e.code, e.message)

    # -- MCP --------------------------------------------------------------------

    async def _mcp(self, request: web.Request) -> web.Response:
        if request.method != "POST":
            return web.Response(status=405, headers={"Allow": "POST"})
        try:
            body = json.loads(await request.text())
        except web.HTTPException:
            raise
        except Exception:
            return _json(_rpc_error(None, -32700, "Parse error"), 400)

        if isinstance(body, list):
            if not body:
                return _json(_rpc_error(None, -32600, "Invalid Request"), 400)
            responses = [r for r in [await self._dispatch(m) for m in body] if r is not None]
            return _json(responses) if responses else web.Response(status=202)
        response = await self._dispatch(body)
        if response is None:
            return web.Response(status=202)
        return _json(response)

    async def _dispatch(self, msg: Any) -> dict | None:
        if not isinstance(msg, dict) or msg.get("jsonrpc") != "2.0":
            return _rpc_error(None, -32600, "Invalid Request")
        method = msg.get("method")
        if "id" not in msg:
            return None  # notification (e.g. notifications/initialized) or client response
        msg_id = msg["id"]
        if not isinstance(method, str):
            return None  # a JSON-RPC response sent to us; nothing to answer
        params = msg.get("params") or {}
        if not isinstance(params, dict):
            return _rpc_error(msg_id, -32602, "params must be an object")
        try:
            if method == "initialize":
                return _rpc_result(msg_id, self._initialize(params))
            if method == "ping":
                return _rpc_result(msg_id, {})
            if method == "tools/list":
                return _rpc_result(msg_id, {"tools": TOOLS})
            if method == "tools/call":
                return await self._tools_call(msg_id, params)
            return _rpc_error(msg_id, -32601, f"Method not found: {method}")
        except Exception:
            self.log.exception("[mcp_bridge] MCP %s failed", method)
            return _rpc_error(msg_id, -32603, "Internal error")

    def _initialize(self, params: dict) -> dict:
        requested = params.get("protocolVersion")
        version = requested if requested in SUPPORTED_PROTOCOL_VERSIONS else LATEST_PROTOCOL_VERSION
        return {
            "protocolVersion": version,
            "capabilities": {"tools": {"listChanged": False}},
            "serverInfo": {"name": SERVER_NAME, "version": __version__},
            "instructions": INSTRUCTIONS,
        }

    async def _tools_call(self, msg_id: Any, params: dict) -> dict:
        name = params.get("name")
        args = params.get("arguments") or {}
        if not isinstance(args, dict):
            return _rpc_error(msg_id, -32602, "arguments must be an object")
        if name == "list_commands":
            commands = await self.service.list_commands()
            return _rpc_result(
                msg_id,
                {
                    "content": [{"type": "text", "text": format_command_list(commands)}],
                    "structuredContent": {"commands": commands},
                    "isError": False,
                },
            )
        if name == "run_command":
            try:
                result = await self.service.run_command(args.get("message_id"), args.get("command"))
            except BridgeError as e:
                self._log_refusal(e, args.get("command"))
                return _rpc_result(
                    msg_id,
                    {
                        "content": [{"type": "text", "text": f"已拒绝（{e.code}）：{e.message}"}],
                        "structuredContent": e.to_dict(),
                        "isError": True,
                    },
                )
            return _rpc_result(
                msg_id,
                {
                    "content": [{"type": "text", "text": result["message"]}],
                    "structuredContent": result,
                    "isError": False,
                },
            )
        return _rpc_error(msg_id, -32602, f"Unknown tool: {name}")
