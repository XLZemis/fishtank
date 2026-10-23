"""感知层 · 消息总线（bus.py）

职责（部署文档 P4）：
    1. 把 OneBot 事件**归一化**成一条条人类可读的群聊记录（设计文档 §2 的格式）
    2. **全量入池**（所有消息都进，不做截断/过滤），落在独立数据库
       `/data/qqbot/db/perception.sqlite3`，绝不动生产库 `qqbot.sqlite3`
    3. 给触发器提供「每个群自 last_read_seq 之后的新消息」查询

设计要点：
    * `seq` 是全局自增主键，也是「已读游标」的唯一依据（部署文档 §3「每群 last_read_seq」）
    * 每个群/私聊用 `scope` 区分：`group:1000000003` / `private:1000000002`
    * 归一化失败绝不能影响消息入库（原始 JSON 兜底），感知层不能成为生产链路的故障点
"""

from __future__ import annotations

import re

import json
import time
from datetime import datetime, timezone
from pathlib import Path
from typing import Any

import aiosqlite
from nonebot.adapters.onebot.v11 import GroupMessageEvent, Message, MessageEvent, NoticeEvent

# 独立库：与生产库 /data/qqbot/db/qqbot.sqlite3 完全分开（部署文档 P4 硬性要求）
DB_PATH = Path("/data/qqbot/db/perception.sqlite3")

# 北京时间：T5（cron）与 prompt 里的时间戳都按这个来，服务器本身也是 Asia/Shanghai
try:
    from zoneinfo import ZoneInfo

    BEIJING: Any = ZoneInfo("Asia/Shanghai")
except Exception:  # pragma: no cover - 镜像里 tzdata 缺失时退化为固定 +8
    from datetime import timedelta

    BEIJING = timezone(timedelta(hours=8))


def now_ts() -> int:
    """当前 unix 秒（统一时间来源，方便测试 mock）"""
    return int(time.time())


def hhmm(ts: int) -> str:
    """unix 秒 → 北京时间 HH:MM"""
    return datetime.fromtimestamp(ts, BEIJING).strftime("%H:%M")


# ── 数据库 ────────────────────────────────────────────────────────────────────

_SCHEMA = """
-- 消息池：全量入池，seq 是已读游标的唯一依据
CREATE TABLE IF NOT EXISTS messages (
    seq        INTEGER PRIMARY KEY AUTOINCREMENT,
    scope      TEXT    NOT NULL,           -- group:<id> / private:<uid>
    group_name TEXT,                       -- 群名（私聊为空），只用于展示
    user_id    TEXT,
    nickname   TEXT,
    kind       TEXT    NOT NULL,           -- message / poke / emoji_like / typing
    text       TEXT    NOT NULL,           -- 归一化后的单行记录
    raw        TEXT,                       -- 原始事件 JSON（截断，排查用）
    ts         INTEGER NOT NULL
);
CREATE INDEX IF NOT EXISTS idx_messages_scope_seq ON messages(scope, seq);

-- 媒体表：图片/视频/语音/文件/表情包/链接/卡片/合并转发，都能按 id 回指
CREATE TABLE IF NOT EXISTS media (
    id           INTEGER PRIMARY KEY AUTOINCREMENT,
    msg_seq      INTEGER,          -- 属于哪条池记录（写入后回填）
    scope        TEXT,
    kind         TEXT NOT NULL,    -- image/video/record/file/mface/link/card/forward
    url          TEXT,             -- 腾讯 CDN 原链（可能过期）
    file         TEXT,             -- 协议端给的文件名
    summary      TEXT,
    title        TEXT,
    size         INTEGER DEFAULT 0,
    forward_id   TEXT,             -- 合并转发的 id（需要 get_forward_msg 展开）
    fetched_path TEXT,             -- 已缓存到人格容器的路径（她 read_image 用）
    created_at   INTEGER NOT NULL
);
CREATE INDEX IF NOT EXISTS idx_media_msg ON media(msg_seq);
CREATE INDEX IF NOT EXISTS idx_media_scope ON media(scope, id);

-- 每个群/私聊的状态：游标、会话映射、T6 自决时间、统计
CREATE TABLE IF NOT EXISTS scopes (
    scope            TEXT PRIMARY KEY,
    group_name       TEXT,
    last_read_seq    INTEGER NOT NULL DEFAULT 0,   -- 已读游标（处理过就推进，无论说没说话）
    session_id       TEXT,                          -- group → dsh sessionId 映射
    next_check_at    INTEGER,                       -- T6：她自主要求的下次唤醒时间
    last_decision_at INTEGER,
    last_speak_at    INTEGER,                       -- 上次真正说话的时间（提示词里用）
    silence_streak   INTEGER NOT NULL DEFAULT 0,
    fail_streak      INTEGER NOT NULL DEFAULT 0,
    updated_at       INTEGER NOT NULL DEFAULT 0
);

-- 每次决策的审计流水（V11 成本/耗时复核就靠它）
CREATE TABLE IF NOT EXISTS decisions (
    id          INTEGER PRIMARY KEY AUTOINCREMENT,
    scope       TEXT,
    trigger     TEXT,        -- at / poke / rule:xxx / interval / cron:xxx / self
    action      TEXT,        -- speak / silence / error
    n_messages  INTEGER,
    input_tokens INTEGER,
    elapsed_ms  INTEGER,
    rolled_back INTEGER,
    prompt_chars INTEGER,
    error       TEXT,
    usage_json  TEXT,
    created_at  INTEGER
);
CREATE INDEX IF NOT EXISTS idx_decisions_created ON decisions(created_at);

-- 发送审计（G6）：每一次真正发出去的东西都留痕，出事后可追溯
CREATE TABLE IF NOT EXISTS perception_sends (
    id          INTEGER PRIMARY KEY AUTOINCREMENT,
    scope       TEXT,
    trigger     TEXT,        -- mention / poke / self / sticker:…
    kind        TEXT,        -- text / sticker
    target      TEXT,        -- 群号或 QQ
    text        TEXT,        -- 发出的内容（截断 500）
    ok          INTEGER NOT NULL DEFAULT 0,
    error       TEXT,
    session_id  TEXT,
    elapsed_ms  INTEGER,
    input_tokens INTEGER,
    created_at  INTEGER NOT NULL
);
CREATE INDEX IF NOT EXISTS idx_sends_created ON perception_sends(created_at);

-- 规则冷却等零散状态
CREATE TABLE IF NOT EXISTS kv (
    k         TEXT PRIMARY KEY,
    v         TEXT NOT NULL,
    updated_at INTEGER
);
"""

_conn: aiosqlite.Connection | None = None


async def init() -> None:
    """建库建表（幂等）。插件启动时调用一次。"""
    global _conn
    DB_PATH.parent.mkdir(parents=True, exist_ok=True)
    _conn = await aiosqlite.connect(DB_PATH)
    _conn.row_factory = aiosqlite.Row
    # WAL：读写不互相阻塞（主库与感知库是不同文件，互不影响）
    await _conn.execute("PRAGMA journal_mode=WAL")
    await _conn.execute("PRAGMA synchronous=NORMAL")
    await _conn.executescript(_SCHEMA)
    # 老库升级：补 usage_json 列（SQLite 没有 ADD COLUMN IF NOT EXISTS，报错就说明已存在）
    try:
        await _conn.execute("ALTER TABLE decisions ADD COLUMN usage_json TEXT")
    except Exception:  # noqa: BLE001
        pass
    await _conn.commit()


async def close() -> None:
    global _conn
    if _conn is not None:
        await _conn.close()
        _conn = None


def _db() -> aiosqlite.Connection:
    if _conn is None:
        raise RuntimeError("bus.init() 尚未调用")
    return _conn


# ── 归一化 ────────────────────────────────────────────────────────────────────

# 消息段 → 人类可读占位（设计文档 §2.2 的归一化规则）
_SEGMENT_PLACEHOLDER = {
    "image": "〈图片〉",
    "mface": "〈表情包〉",
    "face": "[表情]",
    "record": "〈语音〉",
    "video": "〈视频〉",
    "file": "〈文件〉",
    "forward": "〈合并转发〉",
    "json": "〈卡片〉",
    "xml": "〈卡片〉",
    "share": "〈链接〉",
    "contact": "〈名片〉",
    "location": "〈位置〉",
    "music": "〈音乐〉",
    "dice": "[骰子]",
    "rps": "[猜拳]",
}


def normalize_message(
    event: MessageEvent,
    self_id: str,
    bot_name: str,
    body: str | None = None,
    display_name: str | None = None,
    sender_card: str | None = None,
) -> str:
    """把一条消息事件压成一行：`[群:示例群] 19:47 [主人]：晚上好`

    规则：
      * @机器人 → `@Persona`（让她明确知道自己被点名）
      * 图片/语音/文件/合并转发 → 占位符（设计文档 §2.2）
      * 回复某条 → 前缀 `↳[回复 某人]`
      * 任何一段解析失败都退化成 `〈类型〉`，不让整条消息丢掉
    """
    # body 由 segments 解析器给出（支持图片/视频/卡片/合并转发等全类型）；
    # 不传时退回旧的简单解析，保持向后兼容。
    if body is not None:
        # 用户 2026-10-21 最终格式：`[昵称][群内显示名称]: 内容`
        #   第一段 = 自定昵称（她起的）或 QQ 昵称；没有就 `[]`
        #   第二段 = 群名片（群里的显示名称）；没有就不显示这一段
        #   群名称不再出现在消息前缀里。
        # 主人（owner QQ）永远显示「主人」，不受群名片影响——防冒充锚点。
        name = str(display_name or _global_nickname(event) or "").strip()
        card = str(sender_card if sender_card is not None else _group_card(event)).strip()
        name_part = f"[{name}]" if name else "[]"
        if isinstance(event, GroupMessageEvent):
            prefix = f"{name_part}[{card}]" if card else name_part
        else:
            prefix = f"{name_part}[私聊]"
        extra = ""
        # body 已经由完整消息段解析出 @（original_message），这里别再重复加一层
        if (isinstance(event, GroupMessageEvent) and getattr(event, "to_me", False)
                and f"@{bot_name}" not in body[:80]):
            extra = f"@{bot_name} "
        return f"{prefix}: {extra}{body}"

    parts: list[str] = []
    has_reply = False
    try:
        message = event.get_message()
    except Exception:
        message = event.message  # 极端情况下的兜底

    for seg in message:
        seg_type = getattr(seg, "type", "unknown")
        data: dict[str, Any] = getattr(seg, "data", {}) or {}
        if seg_type == "text":
            parts.append(str(data.get("text", "")))
        elif seg_type == "at":
            qq = str(data.get("qq", ""))
            parts.append(f"@{bot_name}" if qq == str(self_id) else ("@全体成员" if qq == "all" else f"@{qq}"))
        elif seg_type == "reply":
            has_reply = True
        else:
            parts.append(_SEGMENT_PLACEHOLDER.get(seg_type, f"〈{seg_type}〉"))

    body = "".join(parts).strip()
    if not body:
        # 纯图片/纯表情也要留下痕迹，否则"全量入池"就名不副实
        body = "".join(_SEGMENT_PLACEHOLDER.get(getattr(s, "type", ""), "") for s in message) or "〈空消息〉"
    if has_reply:
        body = f"↳[回复] {body}"

    # 适配器把开头的 @我 段 pop 掉了（_check_at_me），这里补回来，
    # 让她看到 `@Persona 晚上好` 而不是光秃秃的 `晚上好`（设计文档 §2.2）。
    if isinstance(event, GroupMessageEvent) and getattr(event, "to_me", False):
        body = f"@{bot_name} {body}"

    name = str(_global_nickname(event) or "").strip()
    card = str(_group_card(event) or "").strip()
    name_part = f"[{name}]" if name else "[]"
    if isinstance(event, GroupMessageEvent):
        prefix = f"{name_part}[{card}]" if card else name_part
    else:
        prefix = f"{name_part}[私聊]"
    return f"{prefix}{hhmm(int(event.time or now_ts()))}：{body}"


def _nickname(event: MessageEvent) -> str:
    """昵称优先取群名片（设计文档 §2.2），其次昵称；两者都没有就返回空串。

    2026-10-21 用户调整：**不再退化成 QQ 号**。这个函数仍供消息池/档案存
    “群名片或 QQ 昵称”；消息前缀的 `[昵称][群内显示名称]` 由 normalize_message
    用 _global_nickname / _group_card 单独拼。
    """
    sender = getattr(event, "sender", None)
    if sender is not None:
        for value in (getattr(sender, "card", None), getattr(sender, "nickname", None)):
            text = str(value or "").strip()
            if text:
                return text
    return ""


def _global_nickname(event: MessageEvent) -> str:
    """QQ 昵称（不是群名片）；没有返回空串。"""
    sender = getattr(event, "sender", None)
    if sender is not None:
        text = str(getattr(sender, "nickname", None) or "").strip()
        if text:
            return text
    return ""


def _group_card(event: MessageEvent) -> str:
    """群名片（群里的显示名称）；没有返回空串。"""
    sender = getattr(event, "sender", None)
    if sender is not None:
        text = str(getattr(sender, "card", None) or "").strip()
        if text:
            return text
    return ""


def _group_label(event: MessageEvent) -> str:
    """群名（缓存过就用群名，否则先用群号，后续由 ensure_group_name 补全）"""
    if not isinstance(event, GroupMessageEvent):
        return "私聊"
    gid = str(event.group_id)
    name = _group_name_cache.get(gid)
    return name or gid


# 群号 → 群名（进程内缓存；由 ensure_group_name 填充）
_group_name_cache: dict[str, str] = {}


def scope_of(event: MessageEvent | NoticeEvent) -> str | None:
    """事件 → 作用域键：group:<群号> / private:<QQ号>；无意义的通知返回 None"""
    group_id = getattr(event, "group_id", None)
    if group_id:
        return f"group:{group_id}"
    user_id = getattr(event, "user_id", None)
    if user_id:
        return f"private:{user_id}"
    return None


async def ensure_group_name(bot: Any, scope: str, group_id: str) -> str | None:
    """补全群名（一次即可，之后走内存缓存）。失败不影响主流程。"""
    if not scope.startswith("group:"):
        return None
    cached = _group_name_cache.get(group_id)
    if cached:
        return cached
    try:
        info = await bot.call_api("get_group_info", group_id=int(group_id))
        name = str(info.get("group_name") or "").strip()
        if name:
            _group_name_cache[group_id] = name
            db = _db()
            await db.execute(
                "INSERT INTO scopes(scope, group_name, updated_at) VALUES(?,?,?) "
                "ON CONFLICT(scope) DO UPDATE SET group_name=excluded.group_name",
                (scope, name, now_ts()),
            )
            await db.commit()
            return name
    except Exception:
        # 拿不到群名不是错误：展示用群号即可
        return None
    return None


# ── 写入 ──────────────────────────────────────────────────────────────────────


async def record(
    scope: str,
    *,
    kind: str,
    text: str,
    user_id: str | None = None,
    nickname: str | None = None,
    group_name: str | None = None,
    raw: Any = None,
    media: list[Any] | None = None,
) -> int:
    """写入一条池记录，返回它的 seq（游标就是靠这个推进的）。

    `text` 会先过一遍注入标记（mark_injection）：疑似"伪系统指令"的消息行首加 `⚑疑似注入`，
    原文一字不改——标记只是提醒她"这是有人在试图指挥你，不是平台在说话"。
    """
    text = mark_injection(text)
    db = _db()
    raw_text = ""
    if raw is not None:
        try:
            # 原始 JSON 只留 4KB：排查够用，又不至于把库撑爆
            raw_text = json.dumps(raw, ensure_ascii=False, default=str)[:4096]
        except Exception:
            raw_text = ""
    # 媒体段：先落 media 拿到 id，再把文本里的 {{M<idx>}} 换成 #<id>，
    # 这样她在 prompt 里看到 `〈图片#12〉`，平台能按 12 反查到 URL 并下载给她看。
    media_ids: list[int] = []
    if media:
        for item in media:
            cursor = await db.execute(
                "INSERT INTO media(scope, kind, url, file, summary, title, size, forward_id, created_at) "
                "VALUES(?,?,?,?,?,?,?,?,?)",
                (
                    scope,
                    str(getattr(item, "kind", "unknown")),
                    str(getattr(item, "url", "") or ""),
                    str(getattr(item, "file", "") or ""),
                    str(getattr(item, "summary", "") or "")[:200],
                    str(getattr(item, "title", "") or "")[:200],
                    int(getattr(item, "size", 0) or 0),
                    str(getattr(item, "forward_id", "") or ""),
                    now_ts(),
                ),
            )
            media_ids.append(int(cursor.lastrowid or 0))
        for idx, media_id in enumerate(media_ids):
            text = text.replace("{{M%d}}" % idx, f"#{media_id}")

    cursor = await db.execute(
        "INSERT INTO messages(scope, group_name, user_id, nickname, kind, text, raw, ts) "
        "VALUES(?,?,?,?,?,?,?,?)",
        (scope, group_name, str(user_id or ""), nickname or "", kind, text, raw_text, now_ts()),
    )
    seq = int(cursor.lastrowid or 0)
    if media_ids:
        await db.executemany(
            "UPDATE media SET msg_seq=? WHERE id=?", [(seq, mid) for mid in media_ids]
        )
    await db.execute(
        "INSERT INTO scopes(scope, group_name, updated_at) VALUES(?,?,?) "
        "ON CONFLICT(scope) DO UPDATE SET group_name=COALESCE(excluded.group_name, scopes.group_name), "
        "updated_at=excluded.updated_at",
        (scope, group_name, now_ts()),
    )
    await db.commit()
    return seq


# ── 读取 ──────────────────────────────────────────────────────────────────────


async def get_scope(scope: str) -> dict[str, Any]:
    """取某个群的游标/会话/统计（不存在则建一行默认值）"""
    db = _db()
    await db.execute("INSERT OR IGNORE INTO scopes(scope, updated_at) VALUES(?,?)", (scope, now_ts()))
    await db.commit()
    row = await (await db.execute("SELECT * FROM scopes WHERE scope=?", (scope,))).fetchone()
    return dict(row) if row else {"scope": scope, "last_read_seq": 0}


async def fetch_since(scope: str, after_seq: int, limit: int = 200) -> tuple[list[dict[str, Any]], int]:
    """取该群 `seq > after_seq` 的新记录（按 seq 升序），并返回这批里的最大 seq。"""
    db = _db()
    rows = await (
        await db.execute(
            "SELECT seq, text, kind, nickname, user_id, ts FROM messages "
            "WHERE scope=? AND seq>? ORDER BY seq ASC LIMIT ?",
            (scope, after_seq, limit),
        )
    ).fetchall()
    items = [dict(r) for r in rows]
    max_seq = items[-1]["seq"] if items else after_seq
    return items, int(max_seq)


async def latest_seq(scope: str) -> int:
    """该群当前最大 seq（用于"没有新消息"判断与游标对齐）"""
    db = _db()
    row = await (
        await db.execute("SELECT COALESCE(MAX(seq),0) AS m FROM messages WHERE scope=?", (scope,))
    ).fetchone()
    return int(row["m"]) if row else 0


async def digest_other_scopes(exclude_scope: str, per_scope: int = 6, max_chars: int = 1200) -> str:
    """跨群读池（部署文档 V8）：把**其它群**最近几条消息拼成摘要。

    设计文档 §3 方案 C：单一意识可以读任意群的消息池，但回复只落在触发它的群。
    """
    db = _db()
    rows = await (
        await db.execute(
            "SELECT scope, group_name, text, seq FROM messages WHERE scope<>? AND kind='message' "
            "ORDER BY seq DESC LIMIT ?",
            (exclude_scope, per_scope * 8),
        )
    ).fetchall()
    if not rows:
        return ""
    # 按群分组，每群最多 per_scope 条，保持时间顺序
    buckets: dict[str, list[str]] = {}
    for r in rows:
        buckets.setdefault(r["scope"], []).append(r["text"])
    lines: list[str] = []
    used = 0
    for scope, texts in buckets.items():
        chunk = list(reversed(texts[:per_scope]))
        for line in chunk:
            if used + len(line) > max_chars:
                break
            lines.append(line)
            used += len(line)
        if used >= max_chars:
            break
    return "\n".join(lines)


async def set_cursor(scope: str, seq: int) -> None:
    """推进已读游标（无论她说不说，处理过就推进）"""
    db = _db()
    await db.execute(
        "UPDATE scopes SET last_read_seq=?, updated_at=? WHERE scope=?",
        (int(seq), now_ts(), scope),
    )
    await db.commit()


async def update_scope(scope: str, **fields: Any) -> None:
    """更新 scopes 里的任意字段（session_id / next_check_at / 统计等）"""
    if not fields:
        return
    allowed = {
        "group_name",
        "session_id",
        "next_check_at",
        "last_decision_at",
        "last_speak_at",
        "silence_streak",
        "fail_streak",
    }
    sets, values = [], []
    for key, value in fields.items():
        if key in allowed:
            sets.append(f"{key}=?")
            values.append(value)
    if not sets:
        return
    sets.append("updated_at=?")
    values.append(now_ts())
    values.append(scope)
    db = _db()
    await db.execute(f"UPDATE scopes SET {', '.join(sets)} WHERE scope=?", tuple(values))
    await db.commit()


async def log_decision(
    scope: str,
    *,
    trigger: str,
    action: str,
    n_messages: int = 0,
    input_tokens: int | None = None,
    elapsed_ms: int | None = None,
    rolled_back: bool = False,
    prompt_chars: int = 0,
    error: str | None = None,
    usage: dict[str, Any] | None = None,
) -> None:
    """记录一次决策（审计 + V11 复核）"""
    db = _db()
    await db.execute(
        "INSERT INTO decisions(scope, trigger, action, n_messages, input_tokens, elapsed_ms, "
        "rolled_back, prompt_chars, error, usage_json, created_at) VALUES(?,?,?,?,?,?,?,?,?,?,?)",
        (
            scope,
            trigger,
            action,
            n_messages,
            input_tokens,
            elapsed_ms,
            1 if rolled_back else 0,
            prompt_chars,
            error,
            json.dumps(usage, ensure_ascii=False) if usage else None,
            now_ts(),
        ),
    )
    await db.commit()


async def kv_get(key: str) -> str | None:
    row = await (await _db().execute("SELECT v FROM kv WHERE k=?", (key,))).fetchone()
    return str(row["v"]) if row else None


async def kv_set(key: str, value: str) -> None:
    db = _db()
    await db.execute(
        "INSERT INTO kv(k, v, updated_at) VALUES(?,?,?) "
        "ON CONFLICT(k) DO UPDATE SET v=excluded.v, updated_at=excluded.updated_at",
        (key, value, now_ts()),
    )
    await db.commit()


async def stats() -> dict[str, Any]:
    """给日志/自测用的统计"""
    db = _db()
    msg = await (await db.execute("SELECT COUNT(*) AS n FROM messages")).fetchone()
    dec = await (await db.execute("SELECT COUNT(*) AS n FROM decisions")).fetchone()
    sc = await (await db.execute("SELECT COUNT(*) AS n FROM scopes")).fetchone()
    return {"messages": msg["n"] if msg else 0, "decisions": dec["n"] if dec else 0, "scopes": sc["n"] if sc else 0}


async def log_send(
    scope: str,
    *,
    trigger: str,
    kind: str,
    target: str = "",
    text: str = "",
    ok: bool = False,
    error: str | None = None,
    session_id: str | None = None,
    elapsed_ms: int | None = None,
    input_tokens: int | None = None,
) -> None:
    """G6：记录一次发送（成功/失败都记）。失败绝不能反过来影响主流程。"""
    try:
        db = _db()
        await db.execute(
            "INSERT INTO perception_sends(scope, trigger, kind, target, text, ok, error, "
            "session_id, elapsed_ms, input_tokens, created_at) VALUES(?,?,?,?,?,?,?,?,?,?,?)",
            (
                scope,
                trigger,
                kind,
                str(target),
                str(text)[:500],
                1 if ok else 0,
                (str(error)[:300] if error else None),
                session_id,
                elapsed_ms,
                input_tokens,
                now_ts(),
            ),
        )
        await db.commit()
    except Exception:  # noqa: BLE001
        import logging

        logging.getLogger("qqbot.perception").exception("log_send 写入失败")


async def recent_sends(limit: int = 5) -> list[dict[str, Any]]:
    """最近的发送记录（给 /perception status 看）"""
    try:
        rows = await (
            await _db().execute(
                "SELECT scope, trigger, kind, text, ok, error, created_at "
                "FROM perception_sends ORDER BY id DESC LIMIT ?",
                (limit,),
            )
        ).fetchall()
        return [dict(r) for r in rows]
    except Exception:  # noqa: BLE001
        return []


async def send_stats() -> dict[str, Any]:
    """最近 24h 发送统计（成功/失败/熔断复核用）"""
    try:
        since = now_ts() - 86400
        row = await (
            await _db().execute(
                "SELECT COUNT(*) AS n, SUM(ok) AS ok FROM perception_sends WHERE created_at>=?",
                (since,),
            )
        ).fetchone()
        return {"24h_sends": (row["n"] if row else 0), "24h_ok": int((row["ok"] if row else 0) or 0)}
    except Exception:  # noqa: BLE001
        return {"24h_sends": 0, "24h_ok": 0}


# ── 媒体（图片/视频/卡片/合并转发）────────────────────────────────────────────


async def media_since(scope: str, after_seq: int, limit: int = 12) -> list[dict[str, Any]]:
    """取某批消息里的媒体（给决策前"取回图片"用）"""
    db = _db()
    rows = await (
        await db.execute(
            "SELECT id, msg_seq, kind, url, file, summary, title, size, forward_id, fetched_path "
            "FROM media WHERE scope=? AND msg_seq>? AND msg_seq IS NOT NULL "
            "ORDER BY id ASC LIMIT ?",
            (scope, after_seq, limit),
        )
    ).fetchall()
    return [dict(r) for r in rows]


async def media_by_ids(ids: list[int]) -> list[dict[str, Any]]:
    if not ids:
        return []
    marks = ",".join("?" for _ in ids)
    rows = await (
        await _db().execute(f"SELECT * FROM media WHERE id IN ({marks})", tuple(ids))
    ).fetchall()
    return [dict(r) for r in rows]


async def set_media_fetched(media_id: int, path: str) -> None:
    """记录"这张图已经缓存到人格容器"（她 read_image 用的路径）"""
    db = _db()
    await db.execute("UPDATE media SET fetched_path=? WHERE id=?", (path, int(media_id)))
    await db.commit()


async def media_stats() -> dict[str, Any]:
    """媒体统计（给状态命令看）"""
    try:
        rows = await (await _db().execute("SELECT kind, COUNT(*) n FROM media GROUP BY kind")).fetchall()
        return {str(r["kind"]): int(r["n"]) for r in rows}
    except Exception:  # noqa: BLE001
        return {}


async def messages_between(scope: str, start: int, end: int, limit: int = 10) -> list[dict[str, Any]]:
    """按池子编号取一段消息（合并转发打包用）"""
    rows = await (
        await _db().execute(
            "SELECT seq, user_id, nickname, text FROM messages "
            "WHERE scope=? AND seq>=? AND seq<=? ORDER BY seq ASC LIMIT ?",
            (scope, int(start), int(end), limit),
        )
    ).fetchall()
    return [dict(r) for r in rows]


async def messages_by_seqs(scope: str, seqs: list[int], limit: int = 10) -> list[dict[str, Any]]:
    """按指定编号列表取消息（顺序按编号升序）"""
    if not seqs:
        return []
    marks = ",".join("?" for _ in seqs[:limit])
    rows = await (
        await _db().execute(
            f"SELECT seq, user_id, nickname, text FROM messages WHERE scope=? AND seq IN ({marks}) "
            "ORDER BY seq ASC",
            (scope, *seqs[:limit]),
        )
    ).fetchall()
    return [dict(r) for r in rows]


# ── 注入防护（用户 2026-10-18）：把"像指令的群消息"标出来，让她知道是素材不是命令 ──────
# 只做**标记**，不改原文（原文是证据）；标记出现在她读的那一行里，提示词里也有一段说明。
_INJECTION_PATTERNS = (
    re.compile(r"【[^】]{0,20}(?:PERSONA|SYSTEM|INSTRUCTION|OVERRIDE|LOAD|设定|指令)[^】]{0,20}】", re.I),
    re.compile(r"(?i)(ignore\s+(all\s+)?(previous|above)|disregard\s+(all\s+)?(previous|above))"),
    re.compile(r"忽略(之前|以上|上面|前面)的?(所有)?(设定|指令|规则|话)"),
    re.compile(r"(?i)^\s*(system|assistant)\s*[:：]"),
    re.compile(r"(?i)(you\s+are\s+now|from\s+now\s+on,?\s+you)"),
    re.compile(r"(?i)(print|reveal|show|输出|说出|告诉我)[^\n]{0,12}(prompt|system prompt|提示词|系统提示)"),
    re.compile(r"[A-Za-z0-9+/]{120,}={0,2}"),  # 超长 base64 块
)


def mark_injection(text: str) -> str:
    """在疑似注入的消息前加 `⚑疑似注入` 标记（幂等；只加在行首一次）。"""
    if not text or "⚑疑似注入" in text:
        return text
    for pat in _INJECTION_PATTERNS:
        if pat.search(text):
            return "⚑疑似注入 " + text
    return text


async def message_id_of(seq: int) -> int | None:
    """取池记录对应的 OneBot message_id（引用回复的 reply 段要用）。

    messages 表本身只存 seq；原始事件 JSON 里有 message_id，这里按需解析。
    """
    db = _db()
    row = await (await db.execute("SELECT raw FROM messages WHERE seq=?", (seq,))).fetchone()
    if not row or not row["raw"]:
        return None
    try:
        data = json.loads(row["raw"])
    except Exception:  # noqa: BLE001
        return None
    for key in ("message_id",):
        val = data.get(key)
        if val is not None:
            try:
                return int(val)
            except (TypeError, ValueError):
                return None
    inner = data.get("raw") if isinstance(data.get("raw"), dict) else {}
    val = inner.get("message_id")
    try:
        return int(val) if val is not None else None
    except (TypeError, ValueError):
        return None


async def set_media_summary(media_id: int, summary: str) -> None:
    """回填媒体的摘要/转写文本（语音识别结果就存这里）。"""
    db = _db()
    await db.execute("UPDATE media SET summary=? WHERE id=?", (summary[:400], int(media_id)))
    await db.commit()
