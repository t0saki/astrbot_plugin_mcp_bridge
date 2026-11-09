"""Command validation and allowlist matching.

An allowlist entry is a command prefix written exactly as a user types it,
including AstrBot's wake prefix (``/rss list``) or the GsCore prefix
(``gs体力``). Matching is literal and case-sensitive:

* ``gs体力``  allows ``gs体力`` and ``gs体力 <args>`` (entry followed by
  whitespace and anything), but NOT ``gs体力xyz``. This keeps ``/rss`` from
  silently allowing a different command ``/rssadmin``.
* ``gs查询*`` (trailing ``*``) additionally allows text glued directly onto the
  prefix: ``gs查询123456789``. Use it only for commands whose glued argument is
  harmless — ``gs*`` would allow *every* GsCore command, including admin ones.

``*`` is only special at the end of an entry; anywhere else it is literal.
"""

from __future__ import annotations

import unicodedata
from dataclasses import dataclass

from .errors import BridgeError

MAX_COMMAND_LENGTH = 200


@dataclass(frozen=True)
class AllowEntry:
    prefix: str
    glued: bool  # True when the entry ended with "*"

    @property
    def display(self) -> str:
        return self.prefix + ("*" if self.glued else "")

    def matches(self, command: str) -> bool:
        if not command.startswith(self.prefix):
            return False
        rest = command[len(self.prefix) :]
        if not rest:
            return True
        if self.glued:
            return True
        return rest[0].isspace()


def parse_allowlist(entries: list[str] | None) -> list[AllowEntry]:
    result: list[AllowEntry] = []
    seen: set[str] = set()
    for raw in entries or []:
        if not isinstance(raw, str):
            continue
        text = raw.strip()
        glued = text.endswith("*")
        if glued:
            text = text[:-1].rstrip()
        if not text or "\n" in text or "\r" in text:
            continue
        entry = AllowEntry(text, glued)
        if entry.display in seen:
            continue
        seen.add(entry.display)
        result.append(entry)
    return result


def normalize_command(command: object) -> str:
    """Validate shape (not permission) of a command; return the stripped text."""
    if not isinstance(command, str):
        raise BridgeError("invalid_command", "command 必须是字符串")
    text = command.strip()
    if not text:
        raise BridgeError("invalid_command", "command 为空")
    if "\n" in text or "\r" in text or " " in text or " " in text:
        raise BridgeError("invalid_command", "command 不能包含换行（只允许单行指令）")
    if len(text) > MAX_COMMAND_LENGTH:
        raise BridgeError(
            "invalid_command", f"command 过长（{len(text)} > {MAX_COMMAND_LENGTH} 字符）"
        )
    for ch in text:
        if unicodedata.category(ch) == "Cc":
            raise BridgeError("invalid_command", "command 含有控制字符")
    return text


def find_match(entries: list[AllowEntry], command: str) -> AllowEntry | None:
    for entry in entries:
        if entry.matches(command):
            return entry
    return None


def check_allowed(entries: list[AllowEntry], command: str) -> AllowEntry:
    entry = find_match(entries, command)
    if entry is None:
        if not entries:
            raise BridgeError(
                "not_allowed", "allow_commands 为空：管理员尚未放行任何指令，拒绝执行"
            )
        raise BridgeError(
            "not_allowed",
            f"指令 {command!r} 不在 allow_commands 白名单内；可用 list_commands 查看可执行的指令",
        )
    return entry
