"""Build the ``list_commands`` result and expand denials through the registry.

Sources, each minus everything ``deny_commands`` refuses:

1. AstrBot's command registry (``astrbot.core.star.command_management
   .list_commands()``), rendered as the user would type them: wake prefix +
   effective command (``/rss list``). With an empty allowlist every enabled
   command is listed; otherwise only those the allowlist permits;
2. ``extra_commands`` from the config, for commands outside AstrBot's registry
   (GsCore's ``gs体力`` etc.), filtered the same way;
3. with a non-empty allowlist, any allowlist entry not covered by 1 or 2.

A registered command counts as denied when *any* of its spellings (every wake
prefix, no prefix, every alias) matches ``deny_commands``, so denying
``/reset`` also refuses its aliases and other wake prefixes. A deny entry that
also matches a wake prefix on its own (``core`` with wake prefix ``core``, or
``/``) says nothing about which command is meant, so it does not count for that
prefix's spellings; it is still applied literally to the typed text.
"""

from __future__ import annotations

from collections.abc import Iterable
from typing import Any

from .allowlist import AllowEntry, find_denied, find_match
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


def _names(entry: dict) -> list[str]:
    effective = (entry.get("effective_command") or "").strip()
    if not effective:
        return []
    parent = (entry.get("parent_signature") or "").strip()
    names = [effective]
    for alias in entry.get("aliases") or []:
        alias = str(alias).strip()
        if alias:
            names.append(f"{parent} {alias}" if parent else alias)
    return names


def _forms(entry: dict, wake_prefixes: list[str]) -> list[str]:
    prefixes = wake_prefixes or [""]
    return [p + n for n in _names(entry) for p in prefixes]


def _all_spellings(entry: dict, wake_prefixes: list[str]) -> list[str]:
    # private chats do not need a wake prefix, so the bare name counts too
    return _forms(entry, wake_prefixes) + _names(entry)


def _prefixed_spellings(entry: dict, wake_prefixes: list[str]) -> list[tuple[str, str]]:
    """``(wake prefix, spelling)`` for every spelling; ``""`` for the bare names."""
    names = _names(entry)
    return [(p, p + n) for n in names for p in wake_prefixes] + [("", n) for n in names]


def _identifies(deny: AllowEntry, wake_prefix: str, spelling: str) -> bool:
    # "core" matches "corehelp" only because "core" is a wake prefix: not this command
    return bool(find_denied([deny], spelling)) and not find_denied([deny], wake_prefix)


def wake_prefix_collisions(
    deny: list[AllowEntry], wake_prefixes: list[str]
) -> list[tuple[str, str]]:
    """``(deny entry, wake prefix)`` pairs where the entry is, or is a leading part
    of, the wake prefix and is therefore applied literally only (startup warning)."""
    return [(d.display, p) for d in deny for p in wake_prefixes if find_denied([d], p)]


def allow_wake_prefix_entries(
    allow: list[AllowEntry], wake_prefixes: list[str]
) -> list[tuple[str, str]]:
    """``(allow entry, wake prefix)`` pairs where the entry permits every registered
    command through that prefix: ``core`` / ``core*`` (AstrBot strips the
    whitespace in ``core reset``), or ``co*`` (startup warning)."""
    return [
        (a.display, p)
        for a in allow
        for p in wake_prefixes
        if p and (a.prefix == p or (a.glued and p.startswith(a.prefix)))
    ]


def _runnable(entry: dict) -> bool:
    if entry.get("is_group"):
        return False  # a group by itself only prints help; its sub-commands are listed
    return entry.get("enabled") is not False and entry.get("plugin_activated") is not False


def _entry_denied(entry: dict, wake_prefixes: list[str], deny: list[AllowEntry]) -> bool:
    return any(
        _identifies(d, p, s) for p, s in _prefixed_spellings(entry, wake_prefixes) for d in deny
    )


def _denied_spellings(
    entries: Iterable[dict], wake_prefixes: list[str], deny: list[AllowEntry]
) -> list[AllowEntry]:
    """Every spelling of every denied registered command, as deny entries."""
    if not deny:
        return []
    return [
        AllowEntry(spelling, False)
        for entry in _flatten(entries)
        if _entry_denied(entry, wake_prefixes, deny)
        for spelling in _all_spellings(entry, wake_prefixes)
    ]


def registry_denial(
    entries: Iterable[dict], wake_prefixes: list[str], deny: list[AllowEntry], command: str
) -> str | None:
    """If ``command`` invokes a registered command that is denied under any of
    its spellings, return the spelling that was invoked.

    Uses the same folding as the literal deny check, so ``core reset`` and
    ``GS  RESET`` (AstrBot strips the whitespace after a wake prefix) are caught
    as ``corereset`` / ``gsreset``."""
    hit = find_denied(_denied_spellings(entries, wake_prefixes, deny), command)
    return hit.prefix if hit else None


def registry_commands(
    entries: Iterable[dict],
    wake_prefixes: list[str],
    allow: list[AllowEntry],
    deny: list[AllowEntry] | None = None,
) -> list[dict]:
    deny = deny or []
    entries = list(entries)
    # never list a spelling that run_command refuses (deny "/" vs "/help", or a
    # spelling that folds onto a denied command's spelling)
    refused = deny + _denied_spellings(entries, wake_prefixes, deny)
    result = []
    for entry in _flatten(entries):
        if not _runnable(entry) or _entry_denied(entry, wake_prefixes, deny):
            continue
        chosen = next(
            (
                f
                for f in _forms(entry, wake_prefixes)
                if not find_denied(refused, f) and (not allow or find_match(allow, f))
            ),
            None,
        )
        if chosen is None:
            continue
        result.append(
            {
                "command": chosen,
                "description": _one_line(entry.get("description")),
                "source": "astrbot",
                "plugin": entry.get("plugin_display_name") or entry.get("plugin") or "",
                "permission": entry.get("permission") or "everyone",
            }
        )
    return result


def build_command_list(
    registry: Iterable[dict],
    wake_prefixes: list[str],
    allow: list[AllowEntry],
    extras: list[ExtraCommand],
    deny: list[AllowEntry] | None = None,
) -> list[dict]:
    deny = deny or []
    registry = list(registry)
    listed = registry_commands(registry, wake_prefixes, allow, deny)
    seen = {c["command"] for c in listed}
    for extra in extras:
        if extra.command in seen or find_denied(deny, extra.command):
            continue
        if registry_denial(registry, wake_prefixes, deny, extra.command):
            continue
        if allow and not find_match(allow, extra.command):
            continue
        seen.add(extra.command)
        listed.append(
            {"command": extra.command, "description": extra.description, "source": "extra"}
        )
    for entry in allow:
        if entry.prefix in seen or find_denied(deny, entry.prefix):
            continue
        if registry_denial(registry, wake_prefixes, deny, entry.prefix):
            continue
        description = "（可在后面直接接参数）" if entry.glued else ""
        listed.append({"command": entry.display, "description": description, "source": "allowlist"})
    return listed


def unlisted_extras(allow: list[AllowEntry], extras: list[ExtraCommand]) -> list[str]:
    """extra_commands that a non-empty allowlist does not permit (startup warning)."""
    if not allow:
        return []
    return [e.command for e in extras if not find_match(allow, e.command)]


def format_command_list(commands: list[dict]) -> str:
    if not commands:
        return "当前没有可列出的指令（可能都被 deny_commands 排除，或 AstrBot 未注册任何指令）。"
    lines = ["可通过 run_command 执行的指令（按用户输入的原样书写）："]
    for c in commands:
        desc = f" — {c['description']}" if c.get("description") else ""
        lines.append(f"- {c['command']}{desc}")
    return "\n".join(lines)
