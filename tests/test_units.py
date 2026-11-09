"""Unit tests: id parsing, allowlist, config, resolver, rate limit, payload."""

from __future__ import annotations

import pytest

from mcp_bridge.allowlist import check_permitted, find_denied, normalize_command, parse_allowlist
from mcp_bridge.commands import (
    build_command_list,
    format_command_list,
    registry_denial,
    unlisted_extras,
)
from mcp_bridge.config import BridgeConfig, parse_extra_commands
from mcp_bridge.errors import BridgeError
from mcp_bridge.ids import MessageRef, parse_message_id
from mcp_bridge.injector import SYNTHETIC_MARKER, build_payload, cq_escape, make_event
from mcp_bridge.ratelimit import RateLimiter
from mcp_bridge.resolver import resolve_message

from .fakes import BOT, GROUP, NOW, USER, group_msg


def check_allowed(allow, command):
    return check_permitted(allow, [], command)


def code_of(fn, *a, **kw) -> str:
    with pytest.raises(BridgeError) as ei:
        fn(*a, **kw)
    return ei.value.code


# -- message ids --------------------------------------------------------------


@pytest.mark.parametrize(
    "raw,expected",
    [
        (12345, MessageRef(12345)),
        (-1234567, MessageRef(-1234567)),
        ("-1234567", MessageRef(-1234567)),
        ("  42 ", MessageRef(42)),
        (42.0, MessageRef(42)),
        ("ob:group:123456789:-1234567", MessageRef(-1234567, "group", "123456789")),
        ("ob:private:20001:99", MessageRef(99, "private", "20001")),
        ("ob:qq:whatever:7", MessageRef(7)),
    ],
)
def test_parse_message_id_ok(raw, expected):
    assert parse_message_id(raw) == expected


@pytest.mark.parametrize(
    "raw",
    [
        None,
        True,
        "",
        "abc",
        "1.5",
        1.5,
        "+5",
        "1 2",
        "0",
        0,
        "ob:",
        "ob:group:1:",
        "ob:5",
        "ob:group:1:x",
        "9" * 30,
        [1],
        {"id": 1},
    ],
)
def test_parse_message_id_rejects(raw):
    assert code_of(parse_message_id, raw) == "invalid_message_id"


# -- allowlist ------------------------------------------------------------------


def test_allowlist_exact_and_whitespace_args():
    allow = parse_allowlist(["gs体力", "/rss list"])
    assert check_allowed(allow, "gs体力").prefix == "gs体力"
    assert check_allowed(allow, "gs体力 123").prefix == "gs体力"
    assert check_allowed(allow, "gs体力　123").prefix == "gs体力"  # full-width space
    assert check_allowed(allow, "/rss list").prefix == "/rss list"
    assert code_of(check_allowed, allow, "gs体力xyz") == "not_allowed"
    assert code_of(check_allowed, allow, "/rss") == "not_allowed"
    assert code_of(check_allowed, allow, "/rss listall") == "not_allowed"
    assert code_of(check_allowed, allow, "GS体力") == "not_allowed"  # case-sensitive
    assert code_of(check_allowed, allow, "gs深渊") == "not_allowed"


def test_allowlist_glued_star():
    allow = parse_allowlist(["gs查询*"])
    assert check_allowed(allow, "gs查询").glued
    assert check_allowed(allow, "gs查询123456789").glued
    assert check_allowed(allow, "gs查询 123").glued
    assert code_of(check_allowed, allow, "gs查") == "not_allowed"


def test_allowlist_star_only_special_at_end_and_dedup():
    allow = parse_allowlist(["a*b", " gs体力 ", "gs体力", "", "   ", "*", 5, "x\ny"])
    assert [e.display for e in allow] == ["a*b", "gs体力"]
    assert code_of(check_allowed, allow, "axxb") == "not_allowed"
    assert check_allowed(allow, "a*b")


def test_empty_allowlist_permits_everything():
    assert check_permitted([], [], "gs体力") is None
    assert check_permitted([], [], "/reset") is None


def test_deny_matching_is_broad_prefix():
    deny = parse_allowlist(["gs绑定", "/rss", "/reset*"])
    for cmd in [
        "gs绑定",
        "gs绑定uid123",
        "GS绑定 123",
        "gs 绑定",
        "ｇｓ绑定1",
        "/rss add x",
        "/rssadmin",
        "/reset",
        "/RESET now",
    ]:
        assert find_denied(deny, cmd) is not None, cmd
    for cmd in ["gs体力", "gs解绑", "/rs", "/new"]:
        assert find_denied(deny, cmd) is None, cmd


def test_deny_wins_over_allow_and_default_open():
    allow = parse_allowlist(["gs*"])
    deny = parse_allowlist(["gs绑定"])
    assert check_permitted(allow, deny, "gs体力").prefix == "gs"
    assert code_of(check_permitted, allow, deny, "gs绑定123") == "denied"
    assert code_of(check_permitted, [], deny, "GS绑定 1") == "denied"
    assert check_permitted([], deny, "/reset") is None
    assert code_of(check_permitted, parse_allowlist(["gs体力"]), deny, "/reset") == "not_allowed"


@pytest.mark.parametrize(
    "raw", ["", "   ", "gs体力\n/reset", "a\rb", "a b", "x" * 201, "a\x00b", "a\tb", 5, None]
)
def test_normalize_command_rejects(raw):
    assert code_of(normalize_command, raw) == "invalid_command"


def test_normalize_command_strips_and_keeps_limit():
    assert normalize_command("  gs体力  ") == "gs体力"
    assert normalize_command("x" * 200) == "x" * 200


# -- config -------------------------------------------------------------------------


def test_config_defaults_and_coercion():
    cfg = BridgeConfig.from_dict({})
    assert (cfg.listen_host, cfg.listen_port, cfg.token) == ("127.0.0.1", 6210, "")
    assert cfg.allow_commands == [] and cfg.max_message_age_seconds == 600
    assert cfg.rate_limit_per_minute == 20
    cfg = BridgeConfig.from_dict(
        {
            "listen_port": "99999",
            "max_message_age_seconds": "abc",
            "rate_limit_per_minute": -1,
            "token": "  secret  ",
            "allow_commands": "gs体力\n/rss list",
        }
    )
    assert cfg.listen_port == 6210 and cfg.max_message_age_seconds == 600
    assert cfg.rate_limit_per_minute == 20 and cfg.token == "secret"
    assert [e.prefix for e in cfg.allow_commands] == ["gs体力", "/rss list"]
    assert BridgeConfig.from_dict({"rate_limit_per_minute": 0}).rate_limit_per_minute == 0


def test_extra_commands_parsing():
    extras = parse_extra_commands(["gs体力 | 查询原神体力", "gs深渊", " | nothing", "a|b|c"])
    assert [(e.command, e.description) for e in extras] == [
        ("gs体力", "查询原神体力"),
        ("gs深渊", ""),
        ("a", "b|c"),
    ]


# -- command list ----------------------------------------------------------------------

REGISTRY = [
    {
        "effective_command": "rss",
        "is_group": True,
        "enabled": True,
        "plugin_activated": True,
        "description": "RSS 订阅",
        "parent_signature": "",
        "aliases": [],
        "sub_commands": [
            {
                "effective_command": "rss list",
                "parent_signature": "rss",
                "aliases": ["ls"],
                "description": "列出订阅\n第二行",
                "enabled": True,
                "plugin_activated": True,
                "plugin": "rss",
                "permission": "everyone",
                "sub_commands": [],
            },
            {
                "effective_command": "rss add",
                "parent_signature": "rss",
                "aliases": [],
                "description": "添加订阅",
                "enabled": True,
                "plugin_activated": True,
                "sub_commands": [],
            },
        ],
    },
    {
        "effective_command": "help",
        "description": "帮助",
        "enabled": True,
        "plugin_activated": True,
        "aliases": [],
        "sub_commands": [],
    },
    {
        "effective_command": "off",
        "description": "disabled",
        "enabled": False,
        "plugin_activated": True,
        "aliases": [],
        "sub_commands": [],
    },
]


def test_build_command_list_explicit_allowlist():
    allow = parse_allowlist(["/rss ls", "/off", "gs体力", "gs查询*", "sr体力"])
    extras = parse_extra_commands(["gs体力 | 查询原神体力", "gs删除 | 不应出现"])
    listed = build_command_list(REGISTRY, ["/"], allow, extras)
    by_cmd = {c["command"]: c for c in listed}
    assert by_cmd["/rss ls"]["source"] == "astrbot"
    assert by_cmd["/rss ls"]["description"] == "列出订阅"
    # disabled in AstrBot: not listed from the registry, only as a bare allowlist entry
    assert by_cmd["/off"] == {"command": "/off", "description": "", "source": "allowlist"}
    assert "/rss add" not in by_cmd and "/help" not in by_cmd
    assert by_cmd["gs体力"] == {
        "command": "gs体力",
        "description": "查询原神体力",
        "source": "extra",
    }
    assert "gs删除" not in by_cmd
    assert by_cmd["gs查询*"]["source"] == "allowlist"
    assert by_cmd["sr体力"]["description"] == ""
    assert unlisted_extras(allow, extras) == ["gs删除"]
    assert "- gs体力 — 查询原神体力" in format_command_list(listed)


def test_build_command_list_explicit_allowlist_minus_denied():
    allow = parse_allowlist(["/rss list", "/help", "gs体力", "gs绑定"])
    deny = parse_allowlist(["/rss ls", "gs绑定"])  # alias spelling denies the whole command
    extras = parse_extra_commands(["gs体力 | 体力", "gs绑定 | 绑定"])
    listed = build_command_list(REGISTRY, ["/"], allow, extras, deny)
    assert [c["command"] for c in listed] == ["/help", "gs体力"]


def test_build_command_list_default_open():
    extras = parse_extra_commands(["gs体力 | 查询原神体力", "gs绑定 | 绑定 uid"])
    listed = build_command_list(REGISTRY, ["/", "!"], [], extras)
    assert [(c["command"], c["source"]) for c in listed] == [
        ("/rss list", "astrbot"),
        ("/rss add", "astrbot"),
        ("/help", "astrbot"),
        ("gs体力", "extra"),
        ("gs绑定", "extra"),
    ]  # groups and disabled commands are skipped; no "allowlist" entries
    assert unlisted_extras([], extras) == []
    deny = parse_allowlist(["!rss add", "GS绑定"])
    listed = build_command_list(REGISTRY, ["/", "!"], [], extras, deny)
    assert [c["command"] for c in listed] == ["/rss list", "/help", "gs体力"]
    assert "没有可列出的指令" in format_command_list([])


def test_registry_denial_expands_aliases_prefixes_and_bare_names():
    deny = parse_allowlist(["/rss list"])
    assert registry_denial(REGISTRY, ["/", "!"], deny, "/rss ls") == "/rss ls"
    assert registry_denial(REGISTRY, ["/", "!"], deny, "!rss list 2") == "!rss list"
    assert registry_denial(REGISTRY, ["/", "!"], deny, "rss ls") == "rss ls"  # private chat
    assert registry_denial(REGISTRY, ["/", "!"], deny, "/rss add") is None
    assert registry_denial(REGISTRY, ["/"], deny, "/rss lsx") is None
    assert registry_denial(REGISTRY, ["/"], [], "/rss ls") is None


# -- resolver ---------------------------------------------------------------------------


def resolve(data, ref=MessageRef(-1234567), **kw):
    kw.setdefault("now", NOW)
    kw.setdefault("max_age_seconds", 600)
    return resolve_message(data, ref, **kw)


def test_resolve_group_message():
    r = resolve(group_msg())
    assert (r.self_id, r.user_id, r.message_type, r.group_id) == (
        str(BOT),
        str(USER),
        "group",
        str(GROUP),
    )
    assert r.sub_type == "normal" and r.sender_name == "明明" and r.group_name == "测试群"
    assert r.sender["user_id"] == USER and r.sender["role"] == "member"
    assert r.chat_label == f"group:{GROUP}"


def test_resolve_private_message():
    data = group_msg(message_type="private", sub_type="friend")
    data.pop("group_id")
    r = resolve(data, MessageRef(-1234567, "private", str(USER)))
    assert r.message_type == "private" and r.group_id is None
    assert r.chat_label == f"private:{USER}"


@pytest.mark.parametrize(
    "data,code",
    [
        (None, "message_not_found"),
        ({}, "message_not_found"),
        (group_msg(user_id=BOT, sender={"user_id": BOT}), "self_message"),
        (group_msg(post_type="message_sent"), "self_message"),
        (group_msg(message_sent_type="self"), "self_message"),
        (group_msg(time=int(NOW) - 601), "stale_message"),
        (group_msg(time=int(NOW * 1000)), "unsupported_message"),  # ms timestamp
        (group_msg(time=None), "unsupported_message"),
        (group_msg(message_type="guild"), "unsupported_message"),
        (group_msg(group_id=None), "unsupported_message"),
        (group_msg(user_id=0, sender={}), "unsupported_message"),
    ],
)
def test_resolve_refusals(data, code):
    assert code_of(resolve, data) == code


def test_resolve_age_boundary_and_custom_age():
    assert resolve(group_msg(time=int(NOW) - 600))
    assert code_of(resolve, group_msg(time=int(NOW) - 61), max_age_seconds=60) == "stale_message"


def test_resolve_self_id_sources():
    data = group_msg()
    data.pop("self_id")
    assert code_of(resolve, data) == "platform_unavailable"
    assert resolve(data, bot_self_id=str(BOT)).self_id == str(BOT)
    data_self = group_msg()
    data_self.pop("self_id")
    data_self["user_id"] = BOT
    assert code_of(resolve, data_self, bot_self_id=str(BOT)) == "self_message"
    assert code_of(resolve, group_msg(), bot_self_id="999") == "platform_unavailable"


def test_resolve_chat_mismatch():
    assert code_of(resolve, group_msg(), MessageRef(-1234567, "group", "1")) == "chat_mismatch"
    assert code_of(resolve, group_msg(), MessageRef(-1234567, "private", str(USER))) == (
        "chat_mismatch"
    )
    assert resolve(group_msg(), MessageRef(-1234567, "group", str(GROUP)))


# -- rate limit -------------------------------------------------------------------------


def test_rate_limiter_sliding_window():
    t = [0.0]
    rl = RateLimiter(3, 60, clock=lambda: t[0])
    assert [rl.try_acquire() for _ in range(4)] == [True, True, True, False]
    assert rl.retry_after() == pytest.approx(60)
    t[0] = 59.9
    assert not rl.try_acquire()
    t[0] = 60.0
    assert rl.try_acquire()
    assert RateLimiter(0).try_acquire()


# -- synthetic event ------------------------------------------------------------------------


def test_build_payload_group():
    r = resolve(group_msg())
    p = build_payload(r, "gs体力 [x]", now=NOW + 5)
    assert p["post_type"] == "message" and p["message_type"] == "group"
    assert p["sub_type"] == "normal" and p["message_id"] == -1234567
    assert (p["self_id"], p["user_id"], p["group_id"]) == (BOT, USER, GROUP)
    assert p["sender"]["user_id"] == USER and p["sender"]["card"] == "明明"
    assert p["message"] == [{"type": "text", "data": {"text": "gs体力 [x]"}}]
    assert p["raw_message"] == "gs体力 &#91;x&#93;"
    assert p["time"] == int(NOW + 5) and p["group_name"] == "测试群"
    assert p[SYNTHETIC_MARKER] == {"source_message_id": -1234567}
    event = make_event(p)
    assert event.detail_type == "group" and event.user_id == USER


def test_build_payload_private_has_no_group():
    data = group_msg(message_type="private", sub_type="friend")
    data.pop("group_id")
    p = build_payload(resolve(data), "gs体力")
    assert "group_id" not in p and p["message_type"] == "private"


def test_cq_escape():
    assert cq_escape("a&[b],") == "a&amp;&#91;b&#93;,"
