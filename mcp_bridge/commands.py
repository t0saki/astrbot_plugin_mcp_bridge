"""Build the ``list_commands`` result.

Three sources, all filtered by the allowlist so that everything listed can
actually be run:

1. AstrBot's command registry (``astrbot.core.star.command_management
   .list_commands()``), rendered as the user would type them: wake prefix +
   effective command (``/rss list``);
2. ``extra_commands`` from the config, for commands outside AstrBot's registry
   (GsCore's ``gs体力`` etc.);
3. any allowlist entry not covered by 1 or 2, so the agent still sees it.
"""

from __future__ import annotations

from collections.abc import Iterable
from typing import Any

from .allowlist import AllowEntry, find_match
from .config import ExtraCommand

MAX_DESCRIPTION = 120


def _one_line(text: Any) -> str:
    if not text:
        return ""
    first = str(text).strip().splitlines()[0] if str(text).strip() else ""
    first = " ".join(first.split())
    if len(first) > MAX_DESCRIPTION:
        first = first[: MAX_DESCRIPTION - 1] + "…"
    return first


def _flatten(entries: Iterable[dict]) -> Iterable[dict]:
    for entry in entries:
        if not isinstance(entry, dict):
            continue
        yield entry
        subs = entry.get("sub_commands") or []
        if subs:
            yield from _flatten(subs)


def _forms(entry: dict, wake_prefixes: list[str]) -> list[str]:
    effective = (entry.get("effective_command") or "").strip()
    if not effective:
        return []
    parent = (entry.get("parent_signature") or "").strip()
    names = [effective]
    for alias in entry.get("aliases") or []:
        alias = str(alias).strip()
        if alias:
            names.append(f"{parent} {alias}" if parent else alias)
    prefixes = wake_prefixes or [""]
    return [p + n for n in names for p in prefixes]


def registry_commands(
    entries: Iterable[dict], wake_prefixes: list[str], allow: list[AllowEntry]
) -> list[dict]:
    result = []
    for entry in _flatten(entries):
        if entry.get("is_group"):
            continue  # a group by itself only prints help; its sub-commands are listed
        if entry.get("enabled") is False or entry.get("plugin_activated") is False:
            continue
        for form in _forms(entry, wake_prefixes):
            if find_match(allow, form):
                result.append(
                    {
                        "command": form,
                        "description": _one_line(entry.get("description")),
                        "source": "astrbot",
                        "plugin": entry.get("plugin_display_name") or entry.get("plugin") or "",
                        "permission": entry.get("permission") or "everyone",
                    }
                )
                break
    return result


def build_command_list(
    registry: Iterable[dict],
    wake_prefixes: list[str],
    allow: list[AllowEntry],
    extras: list[ExtraCommand],
) -> list[dict]:
    if not allow:
        return []
    listed = registry_commands(registry, wake_prefixes, allow)
    seen = {c["command"] for c in listed}
    for extra in extras:
        if extra.command in seen or not find_match(allow, extra.command):
            continue
        seen.add(extra.command)
        listed.append(
            {"command": extra.command, "description": extra.description, "source": "extra"}
        )
    covered = {c["command"] for c in listed}
    for entry in allow:
        if entry.prefix in covered:
            continue
        description = "（可在后面直接接参数）" if entry.glued else ""
        listed.append({"command": entry.display, "description": description, "source": "allowlist"})
    return listed


def unlisted_extras(allow: list[AllowEntry], extras: list[ExtraCommand]) -> list[str]:
    """extra_commands that the allowlist does not permit (for a startup warning)."""
    return [e.command for e in extras if not find_match(allow, e.command)]


def format_command_list(commands: list[dict]) -> str:
    if not commands:
        return "当前没有可执行的指令（管理员未在 allow_commands 中放行任何指令）。"
    lines = ["可通过 run_command 执行的指令（按用户输入的原样书写）："]
    for c in commands:
        desc = f" — {c['description']}" if c.get("description") else ""
        lines.append(f"- {c['command']}{desc}")
    return "\n".join(lines)
