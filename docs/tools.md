# 她的工具清单

> 这份文档同时是「给Persona看的说明书」——建议把「使用时机」一节原样塞进她的提示词。

---

## 1. 聊天与记忆

### `chat_search`
检索历史聊天记录（全文）。
- **什么时候用**：想确认"之前是不是聊过这个"、"某人上次提的那个东西叫什么"
- **参数**：`query`、`scope`（群/私聊，可选）

### `chat_detail`
拉取**详细格式**的聊天记录：`[QQ][昵称][群名][时间] 正文`。
- **什么时候用**：需要确认"这句话到底是谁说的、什么时候说的"（比如怀疑有人冒充主人）
- **参数**：`scope`（群名片段 / `group:<群号>` / `private:<QQ>` / `all`）、`limit`、`before_seq`（翻页）
- **例子**：`chat_detail(scope="示例群2", limit=20)`

### `nickname_list` / `nickname_set` / `nickname_remove`
给群友维护「自定昵称」，绑定 QQ、跨群生效。
- **什么时候用**：想给某个人起个你顺口的称呼；发现以前起的称呼不对了
- **注意**：**主人（QQ 1000000002）是黑名单**，改不了，他永远显示为「主人」
- **例子**：`nickname_set(qq="1000000009", name="示例昵称")`

---

## 2. 触发器（她自己的闹钟）

### `trigger_list` / `trigger_add` / `trigger_remove`
管理"什么时候该看一眼"的规则，8 种类型：`mention`（被@）、`poke`（被戳）、
`keyword`（关键词）、`regex`（正则）、`sender`（指定人）、`msgtype`（消息类型）、
`interval`（每隔多久）、`cron`（定时）。
- **什么时候用**：想给自己加个新闹钟 / 关掉某个太吵的
- **例子**：`trigger_add(type="keyword", keywords=["下雨"], cooldown=300)`

---

## 3. 看东西

### `media_look`
真正"看/听"一个图片、音频或视频。
- **什么时候用**：群里发了图/视频/语音，你好奇内容；或者别人说"你看这个"
- **参数**：
  - `media_id`（这批消息里 `#数字` 的媒体）或 `url`
  - `tier`：`asr`（只转写文字，便宜）/ `flash`（默认，性价比）/ `pro`（最细，贵）
  - `frames`（抽帧数）、`fps`（视频采样率）、`prompt`（你想关注什么）
- **例子**：`media_look(media_id=1024, prompt="这张表情包什么情绪")`

### `web_open`
用服务器上的真实浏览器打开网页并读正文（服务器在墙外，B站/YouTube 都能读）。
- **什么时候用**：群里有人发链接，你想看看是什么；查资料；确认页面内容
- **参数**：`url`、`wait_ms`
- **注意**：返回的是压缩后的正文；页面很重时可能只有主要文字。
  打开 B 站视频链接时会自动改走结构化摘要（标题/UP/简介/数据），不是整页推荐位。

### `bili_info`
解析一个 B 站资源，返回结构化摘要。支持的 `target`：
- **视频**：BV 号（`BV1FBnJ6rEFE`）、av 号、`bilibili.com/video/…` 链接 → 标题/UP/时长/播放/弹幕/评论/简介/分P
- **专栏**：`bilibili.com/read/cv…` 或 `read/mobile?id=…` → 标题/作者/发布时间/阅读/点赞/收藏/正文摘要
- **动态**：`t.bilibili.com/…` 或 `bilibili.com/opus/…` → 作者/时间/正文/赞·评·转发，转发内容会带“涉及：…”
- **个人主页**：`space.bilibili.com/<mid>` → 昵称/等级/签名/粉丝/关注/投稿视频数/专栏数/获赞
- **番剧/直播**：页面标题 + 简介兜底
- `b23.tv` 短链会先解析 302 再判型

- **什么时候用**：群里有人发 B 站卡片/链接、或有人说“看看这个视频/专栏/主页”；平台已自动解析标题，
  需要更多细节时再调
- **例子**：`bili_info(target="BV1FBnJ6rEFE")`、`bili_info(target="https://space.bilibili.com/2")`
- **数据来源**：视频优先 PC 代理（快），其余走服务器浏览器；登录态已就绪时走官方 API，否则页面兜底

### `bili`（互动 / 动态 / 消息）
登录态已就绪，她可以主动做这些（都是公开行为，谨慎）：
- `action=like` / `unlike`：给视频点赞/取消赞。点赞会回读 `relation` 校验，重复调用安全。
- `action=favorite` / `unfavorite`：收藏/取消收藏到默认收藏夹（可逆）。
- `action=coin`：**投币不可撤销，两段式**——
  1. 先只传 `target` 和 `count`（1 或 2），不传 `confirm`：返回**当前硬币余额**与计划枚数；
  2. 她确认后带 `confirm=true` 再调一次，才真正投。
- `action=comment`：给视频发评论（`target` + `text`，公开可见）。
- `action=post_dynamic`：发一条 B 站动态（`text`，纯文本，公开可见）。
- `action=watch`：**真正看/听视频**，复用 `media_ai` 三档：
  - `tier=asr`：只转写音频（最便宜，适合讲解/语音视频）；
  - `tier=flash`：原生视频理解（画面+声音，性价比档，默认推荐）；
  - `tier=pro`：原生视频理解（最细、最贵）。
  长视频截断到 `max_seconds`（默认 180，上限 300），自动压到 24MB 内；`prompt` 可指定关注点。
- `action=danmaku`：读弹幕（按时间排序，`limit` 默认 100，上限 500）。
- `action=comments`：读评论（按热度，带点赞数、时间、楼中楼；`limit` 默认 20，上限 100）。
- `action=messages`：看消息中心（`messages_type=reply` 回复我的 / `at` @我 / `like` 收到的赞 / `system` 系统通知），
  条目带来源内容与**可点击跳转链接**。

### `bili_info`（只读解析）
（见上，视频/专栏/动态/主页/番剧/直播）

## 3.5 文档解析（`doc_read`）

读本地文档并转成纯文本，适合群文件、资料、表格、幻灯片：

```
doc_read: { path: "/persona/out/xxx.pdf", max_chars: 12000 }
```

- 支持 PDF / DOCX / DOC / XLSX / XLSM / PPTX / ODT / ODS / ODP / HTML / CSV / TSV / TXT / MD / JSON / XML / RTF；
- 默认最多 12000 字符，上限 50000，超出截断；
- PDF 扫描件只取文本层，不做 OCR（可用容器里的 `tesseract` 手动 OCR）；
- 图片请用 `read_image`，不要用 `doc_read`；
- 容器重建后如果工具丢了，在宿主机仓库根目录跑 `./scripts/persona-tools-install.sh` 重装。

## 3.6 互动（`interact`）

戳一戳 / 点赞 / 读取头像：

```
interact: { action: "poke", target: "1000000002" }
interact: { action: "poke", target: "1000000002", times: 5, interval_seconds: 10, start_delay_seconds: 30 }
interact: { action: "like", target: "1000000002", times: 3, interval_seconds: 60 }
interact: { action: "avatar", target: "1000000002" }
```

- `poke` 在群聊里会带当前 scope 变成群戳，私聊则是私聊戳；
- `times` 1–20，`interval_seconds`/`start_delay_seconds` 0–3600；计划总跨度最多 1 小时；
- 重复动作最小间隔 3 秒（连戳会被 QQ 静默丢弃）；同时最多 20 个计划；计划任务在 bot 进程里执行，**bot 重启会丢**；
- `avatar` 会把头像下载到 `/persona/out/avatar_<QQ>.jpg`，返回路径可直接 `read_image`；
- target 也可以写“主人”（映射 1000000002）。

## 3.7 表情保存（`sticker_save`）

把图存进她自己的专属表情库，并自动视觉标注：

```
sticker_save: { source: "https://.../x.png" }
sticker_save: { source: "12345" }                 ← 这批消息里的 media_id
sticker_save: { source: "/persona/out/x.jpg", name: "小狗", tags: "看戏,吐槽" }
```

- 自动产出 caption / emotions / usage_tags / attitudes / anti_tags / safety_risk；
- 只写入 `/persona/stickers/own-uploads/` 并更新 `/persona/stickers/manifest.json`，
  **不碰公共生产库**；
- 同 sha256 不重复存；safety_risk=high 拒绝；检索缓存约 5 分钟刷新。

## 3.8 网页截图（`browser_shot`）

```
browser_shot: { url: "https://example.com", full_page: false, wait_ms: 2500 }
```

- 用常驻 Chromium（CDP）真实渲染并截图，保存到 `/persona/out/shot_*.png`；
- 返回路径可直接 `read_image`，也可作为 `mail_send` 附件；
- 可能带登录态/隐私内容，外发前先确认。

## 3.9 NapCat 只读查询（`napcat_query`）

```
napcat_query: { api: "get_group_member_info", params: { group_id: "...", user_id: "..." } }
```

- 只读白名单：`get_msg` / `get_forward_msg` / group/friend/member info / status 等；
- 不暴露发消息、踢人、改群等写操作；互动请用 `interact`。

### `tts_speak`（MiMo 语音）
把一句话合成语音并**直接发到当前 scope**（没有 send 开关）。
```
tts_speak: { text: "主人，晚上好呀", voice: "冰糖", style: "温柔、轻快", tag: "(开心)" }
```
- `voice`：冰糖/茉莉/苏打/白桦/Mia/Chloe/Milo/Dean/mimo_default；
- `style`：自然语言控制语速/情绪/方言/角色；
- `tag`：音频标签（开心/唱歌等），会拼在正文开头；

### `message_recall`（撤回）
```
message_recall: { count: 1 }        # 撤回最近 1 条自己发的消息
message_recall: { message_id: "..." }
```
只撤回感知层记录过的、Persona自己最近发出的消息；QQ 普通成员通常 2 分钟内有效。

## 3.10 发邮件（`mail_send`）

```
mail_send: {
  to: "you@example.com",
  subject: "网页截图",
  body: "见附件",
  attachments: ["/persona/out/shot_xxx.png"]
}
```

- `to` 可省略，使用部署侧 `FISHTANK_MAIL_TO` 默认收件人；
- 附件只允许 `/persona` 下文件；
- 需要部署侧配置 `FISHTANK_SMTP_HOST/PORT/USER/PASSWORD/FROM`，见 `docs/configuration.md`。

## 4. QQ 空间（`qzone_*`）

| 工具 | 作用 | 什么时候用 |
|---|---|---|
| `qzone_read` | 读自己的说说 | 想知道自己空间里有什么 |
| `qzone_post` | 发一条说说 | 想分享点什么（**对外可见，想清楚再发**） |
| `qzone_like` | 给最新一条点赞 | 自己发了说说之后顺手；或看到喜欢的（已赞过会自动跳过，不会取消赞） |
| `qzone_comment` | 给最新一条评论 | 想补一句（发表后平台会回读确认） |

- **参数**：`qzone_post` / `qzone_comment` 需要 `text`
- **注意**：
  - 说说发出去是**公开的**，群里的人也可能看到
  - 点赞/评论目前只针对"最新一条说说"
  - 登录态在服务器的常驻浏览器里，正常不用管

---

## 5. 运行数据

### `perception_stats`
看最近 N 小时的运行数据：决策次数、说话/沉默比、输入输出 token、缓存命中、平均耗时、出站数。
- **什么时候用**：想知道自己花了多少、缓存有没有生效
- **参数**：`hours`（默认 24）
- **例子**：`perception_stats(hours=6)`

---

## 6. 建议写进提示词的"使用时机"段

```
【你有哪些工具】
· media_look —— 有人发图/视频/语音，想知道内容时用；看不懂的图先看再说话
· bili_info —— 有人发 B 站视频/专栏/动态/主页，想知道内容时用
· bili —— 想给B站视频点赞/收藏/评论、投币（先确认枚数）、发动态、看视频内容（watch）、读弹幕/评论、看回复和@消息时用
· web_open —— 有人发链接、你想查东西时用；B站/YouTube 都能打开
· chat_detail —— 怀疑有人冒充主人、或想知道某句话是谁什么时候说的
· chat_search —— 想找以前聊过的事
· nickname_set/list —— 想给群友起称呼（主人改不了）
· qzone_read/post/like/comment —— 你的 QQ 空间
· trigger_add/list/remove —— 给自己加/关闹钟
· perception_stats —— 看自己的运行数据
别为了用工具而用工具；不需要的时候别调。
```
