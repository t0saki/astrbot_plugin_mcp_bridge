"""Typed view of the plugin configuration (``_conf_schema.json``)."""

from __future__ import annotations

from dataclasses import dataclass, field
from typing import Any

from .allowlist import AllowEntry, parse_allowlist

DEFAULT_PORT = 6210
DEFAULT_MAX_AGE = 600
DEFAULT_RATE = 20


@dataclass(frozen=True)
class ExtraCommand:
    command: str
    description: str


def _int(raw: dict, key: str, default: int, minimum: int | None = None) -> int:
    value = raw.get(key, default)
    try:
        value = int(value)
    except (TypeError, ValueError):
        return default
    if minimum is not None and value < minimum:
        return default
    return value


def _str(raw: dict, key: str, default: str = "") -> str:
    value = raw.get(key, default)
    if value is None:
        return default
    return str(value).strip()


def _str_list(raw: dict, key: str) -> list[str]:
    value = raw.get(key) or []
    if isinstance(value, str):
        value = value.splitlines()
    if not isinstance(value, list):
        return []
    return [str(v) for v in value if isinstance(v, (str, int)) and str(v).strip()]


def parse_extra_commands(entries: list[str]) -> list[ExtraCommand]:
    """``"gs体力 | 查询原神体力"`` -> ExtraCommand("gs体力", "查询原神体力")."""
    result: list[ExtraCommand] = []
    for raw in entries:
        command, _, desc = raw.partition("|")
        command = command.strip()
        if not command or "\n" in command:
            continue
        result.append(ExtraCommand(command, " ".join(desc.split())))
    return result


@dataclass(frozen=True)
class BridgeConfig:
    listen_host: str = "127.0.0.1"
    listen_port: int = DEFAULT_PORT
    token: str = ""
    platform_id: str = ""
    self_id: str = ""
    allow_commands: list[AllowEntry] = field(default_factory=list)
    extra_commands: list[ExtraCommand] = field(default_factory=list)
    max_message_age_seconds: int = DEFAULT_MAX_AGE
    rate_limit_per_minute: int = DEFAULT_RATE

    @classmethod
    def from_dict(cls, raw: dict[str, Any] | None) -> BridgeConfig:
        raw = dict(raw or {})
        port = _int(raw, "listen_port", DEFAULT_PORT, minimum=1)
        if port > 65535:
            port = DEFAULT_PORT
        return cls(
            listen_host=_str(raw, "listen_host", "127.0.0.1") or "127.0.0.1",
            listen_port=port,
            token=_str(raw, "token"),
            platform_id=_str(raw, "platform_id"),
            self_id=_str(raw, "self_id"),
            allow_commands=parse_allowlist(_str_list(raw, "allow_commands")),
            extra_commands=parse_extra_commands(_str_list(raw, "extra_commands")),
            max_message_age_seconds=_int(raw, "max_message_age_seconds", DEFAULT_MAX_AGE, 1),
            rate_limit_per_minute=_int(raw, "rate_limit_per_minute", DEFAULT_RATE, 0),
        )
