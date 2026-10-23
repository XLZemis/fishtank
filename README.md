# Fishtank Persona Runtime

> 一个「感知层 + 人格层」分离的 QQ 虚拟人格运行时骨架。
>
> Perception layer (NoneBot) 负责收消息、解析、触发与发送；persona layer 是一个
> DeepSeek Harness agent（DSH），负责记忆、判断、工具调用。两层通过 HTTP bridge 连接。

这是一个从真实部署中**脱敏提取**的工程骨架，不是开箱即用的商业产品。
QQ/NapCat、DSH、模型 API 都需要你自己配置；请先看「安全与合规」。

## 特性

- QQ 全类型消息解析：文本 / @ / 引用 / 图片 / 视频 / 语音 / 文件 / 卡片 / 合并转发
- 多层合并转发、引用回复、群名片与自定昵称两段显示
- 触发器：mention / poke / keyword / regex / sender / msgtype / interval / cron
- 人格工具：聊天检索、运行统计、戳一戳/点赞/头像、表情保存与视觉标注、文档解析
- 浏览器能力：QZone 空间、B 站解析/互动（需自行准备登录态）
- 媒体理解：ASR / 视觉理解三档（需配置 MiMo/兼容 API）
- 交互计划器：延迟 + 间隔 + 次数；发文件走共享目录
- 24 个单元测试 + 一键检查脚本

## 架构

```
QQ / NapCat (OneBot v11)
        │
        ▼
perception/ (NoneBot plugin)
  消息池 SQLite · 触发器 · 发送器 · 媒体下载
        │  HTTP /decide（Bearer token）
        ▼
bridge/server.mjs  ──  spawn  ──▶  DSH profile `qq-persona`
                                     ├─ system prompt / memory
                                     ├─ persona/plugins/* tools
                                     └─ 决策输出 SPEAK / SILENCE / actions
        │
        ▼
sender: 文本 / 图片 / 语音 / 文件 / 表情 / 引用
```

## 快速开始（概要）

1. **准备**
   - Python 3.12、Node.js 22
   - [NoneBot 2](https://nonebot.dev/) + `nonebot-adapter-onebot`
   - NapCat / OneBot v11 实现
   - DeepSeek Harness（DSH）及一个 `qq-persona` profile
   - 一个**专门用于测试**的 QQ 号；不要先用主号

2. **配置**
   ```bash
   cp .env.example .env
   # 填 BOT_QQ / PERSONA_OWNER_QQ / API keys / bridge token
   ```
   所有路径、端口和账号都应在部署侧按你的环境调整（代码里的 `/data/...`
   是参考路径）。

3. **部署感知层**
   - 把 `perception/` 放进 NoneBot 插件目录；
   - 把 `bridge/server.mjs` 放到 DSH 可访问的持久目录；
   - 安装 `persona/plugins/` 到 DSH profile 的插件目录，并按
     `deploy/cordis.patch.yml.example` 示例注册。

4. **运行**
   - 启动 NoneBot → bridge → DSH；
   - 用测试 QQ 发一条 @ 消息，确认 base flow；
   - 再逐步开启浏览器/媒体/互动工具。

更完整的工程说明见 [`docs/engineering.md`](docs/engineering.md)；配置变量见 [`docs/configuration.md`](docs/configuration.md)。

## 测试

```bash
python3 -m compileall -q perception persona/tools scripts
node --check bridge/server.mjs
for f in persona/plugins/*/lib/index.js; do node --check "$f"; done

pip install -r requirements-dev.txt
PERCEPTION_PARENT="$PWD" \
DOC2TEXT_PATH="$PWD/persona/tools/doc2text.py" \
python3 -m unittest discover -s tests -v
```

`scripts/test.sh` 额外支持在部署机的 `qqbot-nonebot` 容器里跑测试。

## 安全与合规（重要）

- 本项目面向**个人/自托管**场景，不是腾讯官方项目；QQ 个人号自动化可能违反平台条款并有封号风险，请使用测试号并自行评估。
- 不要把 bridge token、API key、QQ 密码、cookie、聊天数据库提交到 git。
- 人格 agent 可调用 bash/web/文件工具；公开部署时请做权限隔离、限流和提示注入防护。
- QZone/B 站等站点自动化请遵守其服务条款与 robots/频率限制；不要抓取或发布他人隐私数据。
- 仓库不包含任何真实聊天记录、头像、表情包素材或人格角色资产。

## License

MIT。见 [LICENSE](LICENSE)。
