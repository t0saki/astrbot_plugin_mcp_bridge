"""
AstrBot MCP Bridge — main entry point.

Starts a small HTTP server inside the AstrBot process that lets an external AI
agent run AstrBot plugin commands *as the chat user who asked*:

* MCP (Streamable HTTP, stateless, JSON responses) at ``/mcp`` with the tools
  ``list_commands`` and ``run_command``;
* the same two operations as plain JSON at ``GET /v1/commands`` and
  ``POST /v1/run``.

The caller only names a OneBot message id; sender, chat and group are read back
from OneBot ``get_msg`` over AstrBot's own aiocqhttp connection, and the command
is injected as a synthetic message through the aiocqhttp adapter, so AstrBot's
wake-prefix, permission and plugin dispatch behave exactly as for a real one.
"""

from __future__ import annotations

import ipaddress
from typing import Any

from astrbot.api import AstrBotConfig, logger
from astrbot.api.star import Context, Star

from .mcp_bridge.commands import unlisted_extras
from .mcp_bridge.config import BridgeConfig
from .mcp_bridge.errors import BridgeError
from .mcp_bridge.http_server import BridgeServer
from .mcp_bridge.service import BridgeService

ADAPTER_NAME = "aiocqhttp"


class AstrBotBackend:
    """Access to the live AstrBot instance used by :class:`BridgeService`."""

    def __init__(self, context: Context, platform_id: str) -> None:
        self.context = context
        self.platform_id = platform_id

    def _aiocqhttp_insts(self) -> list[Any]:
        insts = getattr(self.context.platform_manager, "platform_insts", []) or []
        return [p for p in insts if p.meta().name == ADAPTER_NAME]

    def get_adapter(self) -> Any:
        if self.platform_id:
            inst = self.context.get_platform_inst(self.platform_id)
            if inst is None or inst.meta().name != ADAPTER_NAME:
                known = ", ".join(str(p.meta().id) for p in self._aiocqhttp_insts()) or "无"
                raise BridgeError(
                    "platform_unavailable",
                    f"找不到 id 为 {self.platform_id!r} 的 aiocqhttp 平台（现有: {known}）",
                )
            return inst
        insts = self._aiocqhttp_insts()
        if not insts:
            raise BridgeError("platform_unavailable", "AstrBot 中没有已加载的 aiocqhttp 平台")
        return insts[0]

    async def registry(self) -> list[dict]:
        from astrbot.core.star.command_management import list_commands

        return await list_commands()

    def wake_prefixes(self) -> list[str]:
        try:
            prefixes = self.context.get_config().get("wake_prefix", ["/"])
        except Exception:
            return ["/"]
        if isinstance(prefixes, str):
            prefixes = [prefixes]
        return [str(p) for p in prefixes if isinstance(p, str)]


def _is_loopback(host: str) -> bool:
    if host == "localhost":
        return True
    try:
        return ipaddress.ip_address(host).is_loopback
    except ValueError:
        return False


class McpBridgePlugin(Star):
    def __init__(self, context: Context, config: AstrBotConfig) -> None:
        super().__init__(context)
        self.cfg = BridgeConfig.from_dict(dict(config) if config else {})
        self.server: BridgeServer | None = None

    async def initialize(self) -> None:
        cfg = self.cfg
        if not cfg.token:
            logger.error(
                "[mcp_bridge] token 未配置：MCP/HTTP 服务不会启动。"
                "请在插件配置中设置一个足够长的随机 token 后重载插件。"
            )
            return
        if len(cfg.token) < 16:
            logger.warning("[mcp_bridge] token 少于 16 个字符，建议使用更长的随机字符串。")
        if not cfg.allow_commands:
            logger.warning("[mcp_bridge] allow_commands 为空：run_command 会拒绝一切指令。")
        for command in unlisted_extras(cfg.allow_commands, cfg.extra_commands):
            logger.warning(
                "[mcp_bridge] extra_commands 中的 %r 不在 allow_commands 内，"
                "不会被列出也不能执行。",
                command,
            )
        if not _is_loopback(cfg.listen_host):
            logger.warning(
                "[mcp_bridge] 监听地址 %s 不是回环地址：token 将以明文 HTTP 传输，"
                "请只在可信网络中使用或在前面加 TLS 反向代理。",
                cfg.listen_host,
            )

        service = BridgeService(cfg, AstrBotBackend(self.context, cfg.platform_id), logger=logger)
        server = BridgeServer(
            service, host=cfg.listen_host, port=cfg.listen_port, token=cfg.token, logger=logger
        )
        try:
            await server.start()
        except OSError as e:
            logger.error(
                "[mcp_bridge] 无法监听 %s:%s：%s（端口被占用？）",
                cfg.listen_host,
                cfg.listen_port,
                e,
            )
            return
        self.server = server
        logger.info(
            "[mcp_bridge] 已启动：MCP http://%s:%s/mcp ，HTTP /v1/commands /v1/run ；"
            "放行 %d 条指令前缀",
            cfg.listen_host,
            server.bound_port,
            len(cfg.allow_commands),
        )

    async def terminate(self) -> None:
        server, self.server = self.server, None
        if server is not None:
            await server.stop()
            logger.info("[mcp_bridge] 服务已停止")
