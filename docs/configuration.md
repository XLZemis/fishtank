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

## 安全约束

- QR 图接口（`/qzone/login/qr.png`、`/browser/login/qr.png`）token 缺失时 **401**，不再 fail-open。
- owner/bot 不得写成真实个人默认值；部署侧必须显式注入。
- `debug_routes` 默认关闭；打开时仍要求 Bearer token。
- 后续 P2b：bridge 绑定地址/超时/体积上限、CDP host/port、剩余 limits。

## 兼容 / 清理

- `perception/rules.toml` 已删除（配置早已迁到 `triggers.yaml`）。
- `.env.example` 只放占位符，不放真实 QQ。
- `tests/` 使用合成身份，不回落真实值。
