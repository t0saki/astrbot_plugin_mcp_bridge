# astrbot_plugin_mcp_bridge

让外部 AI agent 通过 **MCP**（或普通 HTTP）以**提问者本人的身份**触发 [AstrBot](https://github.com/AstrBotDevs/AstrBot) 的插件指令。插件的回复照常直接发到聊天里。

典型场景：同一个 QQ 号经 NapCat（OneBot v11）同时接了 AstrBot（只当插件宿主：GsCore 适配器、rss……，自身 LLM 关闭）和一个独立的聊天 agent。用户自己敲 `gs体力` 时插件会回答；但有人用自然语言说「帮我看看体力」时，agent 以前没办法替他触发插件。装上本插件后，agent 调用 `run_command(message_id, "gs体力")`，AstrBot 就像那个人亲自发了 `gs体力` 一样处理，插件把结果发回群里。

## 工作原理

1. agent 调用 `run_command`，只提供两样东西：**提问者最近一条消息的 OneBot message_id** 和**指令文本**。
2. 插件检查指令格式和白名单（`allow_commands`），不通过直接拒绝，不会碰 OneBot。
3. 插件通过 AstrBot 自己的 aiocqhttp 连接调用 OneBot `get_msg`，从返回结果里取**发送者、会话类型、群号**——身份从不采信调用方。
4. 拒绝以下情况：消息查不到；消息是机器人自己发的；消息早于 `max_message_age_seconds`；`ob:group:<群号>:<id>` 形式的 id 与实际所在会话不符。
5. 构造一条合成的 OneBot v11 消息事件（同 `self_id` / `message_type` / `sub_type` / `group_id` / `user_id` / `sender`，内容为一段文本 = 指令，`time` = 当前时间，沿用原消息的 `message_id`），按真实消息进入 AstrBot 的同一条路径投递：`adapter.convert_message(event)` → `adapter.handle_msg(abm)`。
6. 于是唤醒前缀、群白名单、管理员/成员权限（由真实发送者的 QQ 号与 `admins_id` 决定）、指令过滤器、以及「监听所有消息」的插件（如 GsCore 适配器把每条消息转发给 GsCore）都与真实消息完全一致。
7. 工具立即返回「已交给 AstrBot」，**不等待、也不返回插件输出**——结果会出现在聊天里（agent 本来就能看到）。

## 安全模型（务必读）

- **身份只来自 `get_msg`**。调用方无法指定「以谁的身份」，只能指向一条真实存在、足够新的、非机器人发出的消息。
- **白名单默认为空 = 什么都不能执行**。`allow_commands` 只应放行**任何群成员都可以安全触发**的只读查询类指令。
- **切勿放行破坏性或管理类指令**（重置、删除、绑定/解绑、`/reset`、`/plugin`、GsCore 的管理指令……）。原因：任何群成员都可以用话术诱导 agent 拿**最近发过言的另一个人**（包括管理员）的 message_id 来调用本工具；若那人是 AstrBot 管理员，指令就会以管理员权限执行。白名单是唯一可靠的边界。
- 需要 `Authorization: Bearer <token>`，常量时间比较，失败返回 401；token 不会写入日志。`token` 为空时服务**不会启动**（日志会说明原因）。
- 默认只监听 `127.0.0.1`。改成其他地址时 token 会以明文 HTTP 传输，请只在可信网络使用或在前面加 TLS 反向代理。
- 带 `Origin` 头的请求（即浏览器发起的）一律 403，防止 DNS rebinding 类攻击。
- `run_command` 有全局限流（默认每分钟 20 次，所有尝试都计数，包括被拒绝的）。
- 指令必须是单行、非空、≤200 字符、不含控制字符。

## 安装

1. 把本目录放到 AstrBot 的 `data/plugins/astrbot_plugin_mcp_bridge/`（或在 WebUI 里从仓库安装）。
2. 在 WebUI 填写插件配置，至少设置 `token` 和 `allow_commands`。
3. 重载插件。日志出现 `[mcp_bridge] 已启动：MCP http://127.0.0.1:6210/mcp ...` 即成功。

前置条件：AstrBot ≥ 4（已在 4.29.4 上验证）、aiocqhttp（OneBot v11）平台、OneBot 实现支持 `get_msg`（NapCat 支持）。除 AstrBot 自带依赖（aiohttp、aiocqhttp）外不需要额外依赖。

## 配置

| 字段 | 默认值 | 说明 |
|------|--------|------|
| `token` | （空） | **必填**。客户端需携带的 Bearer token，建议 32 位以上随机串（如 `openssl rand -hex 32`） |
| `listen_host` | `127.0.0.1` | 监听地址 |
| `listen_port` | `6210` | 端口。MCP 地址为 `http://<host>:<port>/mcp` |
| `allow_commands` | `[]` | 允许执行的指令前缀，规则见下 |
| `extra_commands` | `[]` | AstrBot 指令表之外的指令说明，格式 `指令 \| 说明`，如 `gs体力 \| 查询原神体力` |
| `max_message_age_seconds` | `600` | 引用的消息超过多少秒即拒绝 |
| `rate_limit_per_minute` | `20` | `run_command` 每分钟上限（0 = 不限） |
| `platform_id` | （空） | 使用哪个 aiocqhttp 平台实例；留空 = 第一个 aiocqhttp 平台 |
| `self_id` | （空） | 机器人 QQ 号；仅当同一个 aiocqhttp 平台连了多个账号时需要 |

### `allow_commands` 匹配规则

条目按**用户实际输入的样子**书写，包括 AstrBot 的唤醒前缀（`/rss list`）或 GsCore 前缀（`gs体力`）。匹配是字面、区分大小写的：

| 条目 | 放行 | 不放行 |
|------|------|--------|
| `gs体力` | `gs体力`、`gs体力 123`（后接空白再跟参数） | `gs体力xyz`、`GS体力` |
| `/rss list` | `/rss list`、`/rss list 2` | `/rss`、`/rss listall`、`/rss add …` |
| `gs查询*` | `gs查询`、`gs查询123456789`、`gs查询 1` | `gs查` |

- 不带 `*` 时，条目后面只能是「结束」或「空白 + 参数」。这样 `/rss` 不会顺带放行另一个指令 `/rssadmin`。
- 结尾加 `*` 表示允许参数**紧贴**在前缀后面（GsCore 常见写法）。只对紧贴参数无害的指令使用：`gs*` 会放行**所有** GsCore 指令，包括管理类。`*` 只在结尾有特殊含义。
- `extra_commands` 只是说明文字，**仍需**被 `allow_commands` 放行才会被列出和执行（不匹配的会在启动时打印警告）。

## MCP 工具

- `list_commands()`：可执行的指令及一句话说明。来源：AstrBot 已注册指令（按唤醒前缀渲染，如 `/rss list`）∩ 白名单，加上 `extra_commands` ∩ 白名单，再加上未被以上覆盖的白名单条目。
- `run_command(message_id, command)`：以 `message_id` 那条消息的作者身份、在那条消息所在的会话里执行 `command`。
  - `message_id`：整数（数字或字符串，可为负，如 `-1234567`），或 `ob:<chat>:<整数>`，如 `ob:group:123456789:-1234567`、`ob:private:10001:42`。只把末尾整数传给 `get_msg`；若 `<chat>` 是 `group:<群号>` / `private:<QQ>`，还会与实际会话核对，不符即拒绝。
  - 成功：`isError=false`，文本说明已交给 AstrBot；`structuredContent` 含 `as_user`、`chat`。
  - 拒绝：`isError=true`，`structuredContent.error` 为错误码：`invalid_message_id` / `invalid_command` / `not_allowed` / `message_not_found` / `self_message` / `stale_message` / `chat_mismatch` / `unsupported_message` / `rate_limited` / `platform_unavailable` / `internal_error`。

message_id 从哪来：agent 与 AstrBot 连的是**同一个 NapCat**，agent 收到的消息事件里的 `message_id` 就是 AstrBot 侧 `get_msg` 能查到的那个。应当用**提出请求的那个人**的那条消息。

## 注册到 MCP 客户端

传输方式为 Streamable HTTP（无状态、JSON 响应），地址 `http://127.0.0.1:6210/mcp`。

**agy**（flag 必须写在名字前面）：

```bash
agy mcp add --header "Authorization: Bearer <token>" astrbot http://127.0.0.1:6210/mcp
```

**通用 JSON 配置**（Claude Code / Cursor / AstrBot 等支持 streamable HTTP 的客户端）：

```json
{
  "mcpServers": {
    "astrbot": {
      "type": "http",
      "url": "http://127.0.0.1:6210/mcp",
      "headers": { "Authorization": "Bearer <token>" }
    }
  }
}
```

## 普通 HTTP 接口

不支持 MCP 的客户端可以直接调用：

```bash
TOKEN=<token>

# 列出可执行指令
curl -s http://127.0.0.1:6210/v1/commands -H "Authorization: Bearer $TOKEN"

# 以某条消息作者的身份执行指令
curl -s -X POST http://127.0.0.1:6210/v1/run \
  -H "Authorization: Bearer $TOKEN" -H 'Content-Type: application/json' \
  -d '{"message_id": "ob:group:123456789:-1234567", "command": "gs体力"}'
# 成功 200: {"ok": true, "status": "submitted", "as_user": {...}, "chat": "group:123456789", ...}
# 拒绝 4xx: {"ok": false, "error": "stale_message", "message": "..."}

# 健康检查（无需 token）
curl -s http://127.0.0.1:6210/healthz
```

HTTP 状态码：`400` 参数错误、`401` token 错误、`403` 白名单/自身消息/过期/会话不符、`404` 消息不存在、`413` 请求体过大、`422` 不支持的消息、`429` 限流、`503` OneBot 未连接或超时。

## 已知副作用与限制

- **合成消息会再次经过所有「监听所有消息」的插件**。例如记忆类插件会把它当成那个用户发的一条消息记录下来；GsCore 也会收到一条来自该用户的 `gs体力`。合成事件的原始 OneBot 数据带有 `astrbot_mcp_bridge` 字段（`event.message_obj.raw_message.get("astrbot_mcp_bridge")`），其他插件可据此识别或跳过。
- 合成事件沿用原消息的 `message_id`（AstrBot 不按 message_id 去重）。因此开启「回复时引用」时，插件回复会引用用户那条原始消息；同一 message_id 可以被多次使用（在有效期和限流内）。
- 工具不返回插件输出；指令不存在、无权限或插件不回复时，聊天里可能什么都没有。
- 仅支持 aiocqhttp（OneBot v11）平台。
- 同一 aiocqhttp 平台连了多个 OneBot 账号时需配置 `self_id`，否则 `get_msg` 无法确定走哪条连接。

## 实现说明

服务端基于 AstrBot 自带的 aiohttp，自行实现了 MCP Streamable HTTP 的一个最小子集（`POST /mcp`：`initialize`、`notifications/initialized`、`ping`、`tools/list`、`tools/call`，只回 JSON；不发 session id，`GET`/`DELETE /mcp` 返回 405）。没有使用 `mcp` SDK 的 FastMCP 服务端：它依赖 uvicorn + starlette lifespan，在 AstrBot 已有的事件循环里启停（插件重载时释放端口、不抢占 AstrBot 的信号处理）不如 aiohttp `AppRunner` 干净，而且 FastMCP 的 streamable HTTP 参数与默认安全策略在 mcp 1.8 → 1.x 之间多次变化。协议兼容性由测试中官方 `mcp` SDK 客户端的往返测试保证。

## 测试

```bash
# 单元测试 + HTTP 层 + mcp SDK 往返（需要 aiohttp、aiocqhttp、mcp、pytest、pytest-asyncio，AstrBot 环境里都有）
python -m pytest -q

# 端到端：启动真实 AstrBot（全新数据目录、无 LLM、无 WebUI、屏蔽外网），
# 用假的 OneBot 反向 WS 客户端 + mcp SDK 客户端驱动
env -i PATH=<venv>/bin:/usr/bin:/bin HOME=<work>/home \
  <venv>/bin/python tests/e2e/run_e2e.py --astrbot-src <AstrBot 源码目录> --work <工作目录>
```

