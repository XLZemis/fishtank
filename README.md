# Fishtank（鱼缸）

> 一个 QQ 虚拟人格的运行时工程 —— 「Persona / Persona」住在这里。
>
> 名字来由：她是一条大Persona，这套系统就是她的鱼缸——感知层是水，人格是鱼，
> 浏览器/音视频是她的眼睛耳朵，触发器是她自己养的闹钟。

**项目代号**：`fishtank`
**人格名**：Persona（QQ 1000000001）
**服务器**：`your-server`（香港，Ubuntu 22.04）
**首次部署**：2026-10-17，架构重构 2026-10-20

---

## 1. 这套系统是什么

一个**人格与平台分离**的 QQ 机器人：

```
QQ 群/私聊
   │  （NapCat / OneBot v11）
   ▼
感知层 perception（NoneBot 插件）      ← 水：收消息、判触发器、记流水、发消息
   │  HTTP 决策桥（Bearer token）
   ▼
人格层 dsh-persona（DSH 0.2.1-rc.2）   ← 鱼：一个真实的 agent，有记忆、工具、判断力
   ├─ 提示词 + 记忆（她自己写的）
   ├─ 触发器表（她自己维护）
   └─ 工具：trigger_* / chat_search / chat_detail / perception_stats /
            nickname_* / media_look / doc_read / interact / sticker_save / browser_shot / napcat_query / mail_send / web_open / qzone_*
   │
   ▼
发送器 sender（合并消息、图片、语音、文件、@、引用回复、表情包）
```

**核心设计原则**

1. **人格不是脚本**。她是一个 agent，看到群消息后自己决定说不说、说什么。
2. **平台不替她判断**。感知层只做"什么值得叫醒她"，不做"她该说什么"。
3. **她的东西归她**。提示词、记忆、触发器表都是她可编辑的文件。
4. **失败要能退**。所有改动限定在独立目录，回滚 = 删目录。
5. **不给人看的统计不做**。只保留能让她自己变好的数据（token、缓存、耗时）。

---

## 1.5 工程检查（2026-10-23 起）

```bash
./scripts/test.sh
```

先跑测试再改代码；覆盖消息解析、昵称格式、互动计划、表情协议、文档解析。
配置变量见 `docs/configuration.md`。
测试在 `qqbot-nonebot` 容器里跑；本机没有 Docker 时会自动走 ssh。详见 `docs/engineering.md`。

## 2. 目录结构

```
fishtank/
├── README.md                  ← 你在这里
├── perception/                ← 感知层（NoneBot 插件，服务器 /data/qqbot/bot/app/plugins/perception）
│   ├── __init__.py            ← 入口 + HTTP 路由（qzone/browser/messages/stats/media）
│   ├── triggers.py            ← 8 种触发器 + 决策调用 + 发送编排
│   ├── bus.py                 ← 消息池/媒体/决策/发送 记录（SQLite）
│   ├── segments.py            ← 全类型消息解析（文本/@/图片/卡片/转发/…）
│   ├── sender.py              ← 发送器（合并、去重、@全体、引用回复）
│   ├── outbound.py            ← 内联动作（STICKER/ATTACH/VOICE/MUSIC/FILE…）
│   ├── bridge.py              ← 决策桥客户端
│   ├── asr.py                 ← MiMo 语音识别
│   ├── media_ai.py            ← 音视频三档理解（asr/flash/pro）
│   ├── browser.py             ← CDP 浏览器：读网页 / QZone 动作 / B 站视频页兜底取数
│   ├── login.py               ← 扫码登录（QZone/B站）
│   ├── qzone.py               ← QZone API 尝试版（已被浏览器方案取代，保留参考）
│   ├── bili.py                ← B 站解析：视频/专栏/动态/个人主页（代理 → 浏览器 API/页面）
│   └── sticker.py             ← 表情包检索
├── bridge/                    ← 决策桥（Node.js，:13101）
│   └── server.mjs
├── persona/                   ← 人格层
│   ├── prompts/persona-v2.0-draft.md   ← 她的提示词（她可以改）
│   ├── memory/persona-memory.md        ← 她的记忆锚点
│   ├── triggers.yaml                   ← 她的触发器表
│   └── plugins/                        ← 她运行时的 DSH 插件
│       ├── trigger-tools/              ← trigger_list/add/remove
│       ├── chat-search/                ← chat_search（历史检索）
│       ├── nickname-tools/             ← 给群友起名（主人黑名单）
│       └── perception-tools/           ← chat_detail / perception_stats / media_look /
│                                          web_open / bili_info / qzone_*
├── deploy/
│   ├── qqbot-compose.yaml     ← QQ 侧容器编排（napcat + qqbot + chromium + …）
│   ├── compose.yaml           ← 人格侧容器（dsh-persona）
│   ├── cordis.patch.yml       ← 她的 DSH 运行时配置
│   ├── entrypoint.sh
│   └── ops/                   ← systemd 单元 + 运维脚本
├── scripts/
│   ├── bili-relay.py          ← PC 端 B 站代理的服务器侧中转
│   ├── media-prune.py         ← 群图缓存滚动删除
│   ├── qzone-inspect.py       ← QZone DOM 排障（评论框/点赞按钮结构）
│   └── …
└── docs/
    ├── engineering.md
    ├── configuration.md
    └── tools.md
