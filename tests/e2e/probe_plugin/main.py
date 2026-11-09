"""Tiny AstrBot plugin used only by the end-to-end test (tests/e2e/run_e2e.py)."""

from astrbot.api.event import AstrMessageEvent, filter
from astrbot.api.star import Context, Star


def _who(event: AstrMessageEvent) -> str:
    return (
        f"user={event.get_sender_id()} group={event.get_group_id() or '-'} "
        f"admin={event.is_admin()} mid={event.message_obj.message_id}"
    )


class E2EProbe(Star):
    def __init__(self, context: Context, config=None) -> None:
        super().__init__(context)

    @filter.command_group("e2e")
    def e2e(self):
        """e2e probe commands"""

    @e2e.command("ping")
    async def ping(self, event: AstrMessageEvent):
        """回显发送者身份（e2e 探针）"""
        yield event.plain_result(f"pong {_who(event)}")

    @filter.permission_type(filter.PermissionType.ADMIN)
    @filter.command("e2eadmin")
    async def admin_only(self, event: AstrMessageEvent):
        """仅管理员可用（e2e 探针）"""
        yield event.plain_result(f"admin-ok {_who(event)}")

    @filter.command("e2esecret")
    async def secret(self, event: AstrMessageEvent):
        """不在白名单里的指令（e2e 探针）"""
        yield event.plain_result("SECRET-RAN")

    @filter.permission_type(filter.PermissionType.ADMIN)
    @filter.command("e2ereload")
    async def reload_bridge(self, event: AstrMessageEvent):
        """重载 mcp_bridge 插件（e2e 探针）"""
        ok, err = await self.context._star_manager.reload("astrbot_plugin_mcp_bridge")
        yield event.plain_result(f"reloaded ok={ok} err={err}")

    @filter.event_message_type(filter.EventMessageType.ALL)
    async def catch_all(self, event: AstrMessageEvent):
        """Like the GsCore adapter: sees every message, answers 'gs...' ones."""
        text = event.message_str.strip()
        if text.startswith("gs"):
            yield event.plain_result(f"catchall text={text} {_who(event)}")
