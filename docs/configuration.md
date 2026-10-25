# 配置边界（P2）

感知层统一从 `perception/settings.py` 读环境变量。命名以 `FISHTANK_*` 为准，旧变量继续兼容。
代码不解析 `.env`：由 compose/systemd 注入。

## 读取顺序

1. `FISHTANK_<NAME>`
2. 旧别名（`PERSONA_*`、`BOT_QQ`、`NAPCAT_ACCOUNT` 等）
3. 由 `FISHTANK_DATA_ROOT` / `FISHTANK_PERSONA_ROOT` 派生
4. 部署参考默认（路径类）
5. 身份/敏感项无默认，缺失时相关功能报错或跳过（fail-closed）

## 第一批（已迁移）

| 变量 | 默认 / 说明 |
|---|---|
| `FISHTANK_DATA_ROOT` | `/data/qqbot`，派生 db/stickers/config |
| `FISHTANK_PERSONA_ROOT` | `/data/persona` |
| `FISHTANK_DB_PATH` | `<data>/db/perception.sqlite3` |
| `FISHTANK_PROD_DB` | `<data>/db/qqbot.sqlite3` |
| `FISHTANK_PROD_STICKER_ROOT` | `<data>/stickers` |
| `FISHTANK_STICKER_ONTOLOGY` | `<data>/config/sticker-tag-ontology.v1.json` |
| `FISHTANK_OWN_STICKER_ROOT` | `<persona>/stickers` |
| `FISHTANK_OWN_STICKER_VIEW` | `/persona/stickers`（她看到的路径） |
| `FISHTANK_PROD_STICKER_VIEW` | `/data/prod-stickers` |
| `FISHTANK_EXPORTS_DIR` | `<data>/exports` |
| `FISHTANK_MEDIA_DIR` | `<persona>/media` |
| `FISHTANK_BRIDGE_URL` | `http://dsh-persona:13101`（旧 `PERSONA_BRIDGE_URL`） |
| `FISHTANK_BRIDGE_TOKEN_FILE` | `<data>/config/persona-bridge.token`（旧名兼容） |
| `FISHTANK_MIMO_BASE` | `https://api.xiaomimimo.com/v1` |
| `FISHTANK_MIMO_KEY_FILE` | `<data>/config/mimo.key` |
| `FISHTANK_CDP_URL` | 空 = 自动探测 `qqbot-chromium:9222` |
| `FISHTANK_QZONE_BASE` | `https://user.qzone.qq.com` |
| `FISHTANK_BOT_QQ` | 无默认；读不到时 QZone UIN/登录 URL 不完整 |
| `FISHTANK_OWNER_QQ` | 无默认；读不到时二维码不私聊发送（不会崩） |
| `FISHTANK_ASR_MODEL` | `mimo-v2.5-asr`（旧 `PERSONA_ASR_MODEL`） |
| `FISHTANK_ARMED_FILE` | `<data>/config/perception-armed`；`FISHTANK_PERCEPTION_ARMED=1` 也可武装（兼容 `PERCEPTION_ARMED`） |
| `FISHTANK_DEBUG_ROUTES` | `0`；仅本地排障时置 1（`/debug/trigger`、`/debug/sendfile`） |

> `PERCEPTION_ARMED`、触发器表、群/关键词/cron/间隔仍留在 `triggers.yaml`，不迁 env。

## P2b 可调参数（可选，不设走默认）

| 变量 | 默认 |
|---|---|
| `FISHTANK_BRIDGE_PORT` / `BIND` | `13101` / `0.0.0.0` |
| `FISHTANK_BRIDGE_CONCURRENCY` | `2` |
| `FISHTANK_BRIDGE_TIMEOUT_MS` | `45000` |
| `FISHTANK_MAX_MESSAGES` / `MAX_MESSAGE_CHARS` | `20` / `200` |
| `FISHTANK_BRIDGE_MAX_BODY_BYTES` / `MEDIA_MAX_BYTES` | `262144` / `30MB` |
| `FISHTANK_MEDIA_BATCH_LIMIT` / `PREFETCH_LIMIT` / `PROMPT_LIMIT` | `24` / `12` / `24`（多图消息不再按 8 张截断） |
| `FISHTANK_BILI_PROXY` | `http://172.18.0.1:13120`（旧 `PERSONA_BILI_PROXY`） |
| `FISHTANK_BILI_CACHE_TTL_S` / `BROWSER_TIMEOUT_S` / `BRIEF_BUDGET_S` | `1800` / `20` / `20` |
| `FISHTANK_INTERACT_*` | 20 次 / 3600s / 20 job / 3s / 3s |
| `FISHTANK_TRIGGER_REFRESH_S` / `ROSTER_INTERVAL_S` / `ROSTER_MEMBERS_PER_GROUP` | `60` / `21600` / `12` |
| `FISHTANK_MIMO_MODEL_ASR/FLASH/PRO` | `mimo-v2.5-asr` / `mimo-v2.6-flash` / `mimo-v2.6-pro` |

## 安全约束

- QR 图接口（`/qzone/login/qr.png`、`/browser/login/qr.png`）token 缺失时 **401**，不再 fail-open。
- owner/bot 不得写成真实个人默认值；部署侧必须显式注入。
- `debug_routes` 默认关闭；打开时仍要求 Bearer token。
- 后续 P2b：bridge 绑定地址/超时/体积上限、CDP host/port、剩余 limits。

## 兼容 / 清理

- `perception/rules.toml` 已删除（配置早已迁到 `triggers.yaml`）。
- `.env.example` 只放占位符，不放真实 QQ。
- `tests/` 使用合成身份，不回落真实值。

## 人格侧邮件（mail_send）

`mail_send` 在 persona 容器里调用 `/persona/tools/mail_send.py`，只读这些环境变量（只注入 persona，不需要进感知层）：

| 变量 | 说明 |
|---|---|
| `FISHTANK_SMTP_HOST` | SMTP 服务器（必填） |
| `FISHTANK_SMTP_PORT` | 端口，默认 465 |
| `FISHTANK_SMTP_SECURITY` | `ssl` / `starttls` / `plain`；默认按端口（465→ssl，其余→starttls） |
| `FISHTANK_SMTP_USER` | 登录用户 |
| `FISHTANK_SMTP_PASSWORD` / `FISHTANK_SMTP_PASSWORD_FILE` | 授权码/密码；文件更安全（生产用文件） |
| `FISHTANK_SMTP_FROM` | 发件地址，默认同 USER |
| `FISHTANK_MAIL_TO` | 默认收件人（`to` 省略时使用；可逗号分隔） |
| `FISHTANK_SMTP_TIMEOUT` | 超时秒数，默认 45 |

示例（QQ 邮箱/企业邮箱一般用 `ssl` + 465 + 授权码）：

```bash
# /data/persona/.env（600），然后重启 persona
FISHTANK_SMTP_HOST=smtp.qq.com
FISHTANK_SMTP_PORT=465
FISHTANK_SMTP_SECURITY=ssl
FISHTANK_SMTP_USER=xxx@qq.com
FISHTANK_SMTP_PASSWORD=<授权码>
FISHTANK_SMTP_FROM=xxx@qq.com
```

## TTS / 撤回

| 变量 | 默认 |
|---|---|
| `FISHTANK_TTS_MODEL` | `mimo-v2.5-tts`（预置音色；MiMo 限时免费） |
| `FISHTANK_TTS_VOICE` | `冰糖`（可选 冰糖/茉莉/苏打/白桦/Mia/Chloe/Milo/Dean/mimo_default） |
| `FISHTANK_TTS_STYLE` | 温柔、轻快、有点俏皮的女仆语气。 |
| `FISHTANK_TTS_MAX_CHARS` | `500` |
| `FISHTANK_RECALL_MAX_AGE_S` | `120`（QQ 普通成员撤回窗口） |

TTS key 复用 `/data/qqbot/config/mimo.key`；不额外配置。

## 出站安全 / 浏览器（2026-10-25）

| 变量 | 默认 | 说明 |
|---|---|---|
| `FISHTANK_DEBUG_ROUTES` | `0` | 只读 debug 路由总开关；生产为 1，且只接受回环/私网来源 + token |
| `FISHTANK_DEBUG_SENDFILE` | `0` | `/debug/sendfile` 高危开关（绕过人格决策直接发文件），默认关闭 |
| `FISHTANK_BROWSER_LOGIN_HOSTS` | `user.qzone.qq.com,qzone.qq.com,bilibili.com,b23.tv` | 允许复用浏览器登录态的域；其余网页走隔离 context（无 cookie） |
| `FISHTANK_MEDIA_MAX_BYTES` | `31457280`（30MB） | 桥 `/media/fetch` 流式上限，超限即中断 |
| `FISHTANK_STAGE_MAX_BYTES` | `25165824`（24MB） | 桥 `/file/stage` 单文件上限（refs/stickers/media → out） |

## 控制台（2026-10-25）

| 变量 | 默认 | 说明 |
|---|---|---|
| `FISHTANK_CONSOLE_QQS` | 空（关闭） | 允许私聊发控制台命令的 QQ（逗号分隔）。只认 `/` 开头的已知命令；其它私聊照常交给人格 |

命令：`/help` `/status` `/stats [小时]` `/pause <scope> [分钟]` `/resume <scope>` `/scopes`。
scope 形状：`group:<群号>` 或 `private:<QQ>`。
