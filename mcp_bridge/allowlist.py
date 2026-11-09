"""Command validation, allowlist and denylist matching.

Entries are command prefixes written exactly as a user types them, including
AstrBot's wake prefix (``/rss list``) or the GsCore prefix (``gs体力``).

``allow_commands`` (empty = every command may run; otherwise only matches):
matching is literal and case-sensitive.

* ``gs体力``  allows ``gs体力`` and ``gs体力 <args>`` (entry followed by
  whitespace and anything), but NOT ``gs体力xyz``. This keeps ``/rss`` from
  silently allowing a different command ``/rssadmin``.
* ``gs查询*`` (trailing ``*``) additionally allows text glued directly onto the
  prefix: ``gs查询123456789``. Use it only for commands whose glued argument is
  harmless — ``gs*`` would allow *every* GsCore command, including admin ones.

``*`` is only special at the end of an entry; anywhere else it is literal.

``deny_commands`` is checked first and wins over the allowlist. It is matched
more broadly on purpose (it can only refuse more, never less, than the
allowlist rule would): a command is denied when, after NFKC normalisation,
case folding and removing all whitespace, it *starts with* the equally
normalised entry. So ``gs绑定`` also denies ``GS绑定uid123`` and ``gs 绑定 1``,
and ``/rss`` denies the whole ``/rss ...`` group. A trailing ``*`` is accepted
and ignored.
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


def _fold(text: str) -> str:
    return "".join(unicodedata.normalize("NFKC", text).casefold().split())


def find_denied(entries: list[AllowEntry], command: str) -> AllowEntry | None:
    folded = _fold(command)
    for entry in entries:
        prefix = _fold(entry.prefix)
        if prefix and folded.startswith(prefix):
            return entry
    return None


def check_permitted(
    allow: list[AllowEntry], deny: list[AllowEntry], command: str
) -> AllowEntry | None:
    """Raise unless ``command`` passes deny_commands and allow_commands.

    Returns the matching allow entry, or None when the allowlist is empty
    (default-open mode).
    """
    denied = find_denied(deny, command)
    if denied is not None:
        raise BridgeError(
            "denied", f"指令 {command!r} 命中 deny_commands 中的 {denied.display!r}，拒绝执行"
        )
    if not allow:
        return None
    entry = find_match(allow, command)
    if entry is None:
        raise BridgeError(
            "not_allowed",
            f"指令 {command!r} 不在 allow_commands 白名单内；可用 list_commands 查看可执行的指令",
        )
    return entry
