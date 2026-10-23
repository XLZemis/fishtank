"""感知层 · 发送（sender.py）

部署文档 P4：「通过 NoneBot 现有 bot 实例发消息（复用现成通道，不新建连接）」。

实现说明：
    * NoneBot 2.5 的 onebot v11 Bot **没有** `send_group_msg` 属性方法（实测 hasattr=False），
      只有 `send(event, msg)` 与 `send_handler`；定时器驱动的发送没有 event，所以走
      `bot.call_api("send_group_msg", ...)`（等价 API 调用，底层同一条连接）。
    * 连发多条时按 `reply_delay_ms` 间隔发，模拟真人打字节奏；超过 3 条由上层限制。
"""

from __future__ import annotations

import asyncio
import base64
import hashlib
import re
import time
from typing import Any

import nonebot
from nonebot import logger
from nonebot.adapters.onebot.v11 import Bot

from . import bus
from .settings import SETTINGS

# ── 出站幂等（用户 2026-10-17 事故：同一条汇报被发了 3 次）───────────────────
# 同一个 scope + 同一份内容 10 分钟内只发一次；重复的直接跳过并返回 False。
# 进程内表即可：重启后重新计窗口，不影响正确性。
DEDUP_WINDOW_SECONDS = 600
_dedup_seen: dict[str, float] = {}


def _dedup_key(scope: str, kind: str, token: str) -> str:
    raw = f"{scope}|{kind}|{token}".encode("utf-8", "ignore")
    return hashlib.sha1(raw).hexdigest()


def _is_duplicate(scope: str, kind: str, token: str) -> bool:
    """True = 10 分钟内发过同样内容，应跳过。"""
    key = _dedup_key(scope, kind, token)
    now = time.monotonic()
    last = _dedup_seen.get(key)
    if last is not None and now - last < DEDUP_WINDOW_SECONDS:
        logger.warning(f"[perception] 出站去重命中（跳过重复发送）scope={scope} kind={kind}")
        return True
    _dedup_seen[key] = now
    if len(_dedup_seen) > 500:
        for k in [k for k, t in _dedup_seen.items() if now - t >= DEDUP_WINDOW_SECONDS]:
            _dedup_seen.pop(k, None)
    return False


# QQ 单条消息字数上限约 4500；这里按纯文本保守截断，避免超长被协议端拒发
MAX_CHARS_PER_MESSAGE = 900
# ── 最近发送记录 / 主动撤回 ───────────────────────────────────────────────────
# OneBot 的 send_* 会返回 message_id；保留每个 scope 最近发送的消息，撤回工具只允许
# 撤回这些“Persona自己刚发的”消息，避免误删别人的消息。
_SENT_MAX = 80
_SENT_CACHE: dict[str, list[dict[str, Any]]] = {}


def _remember_sent(scope: str, response: Any, kind: str, text: str = "") -> None:
    try:
        data = response if isinstance(response, dict) else {}
        mid = data.get("message_id") or data.get("messageId") or data.get("msg_id")
        if mid is None:
            return
        item = {
            "message_id": str(mid),
            "kind": str(kind),
            "text": str(text or "")[:120],
            "at": int(time.time()),
        }
        bucket = _SENT_CACHE.setdefault(str(scope), [])
        bucket.append(item)
        del bucket[:-_SENT_MAX]
    except Exception:  # noqa: BLE001
        logger.debug("[perception] 记录 message_id 失败（忽略）", exc_info=True)


def recent_sent(scope: str, limit: int = 10) -> list[dict[str, Any]]:
    """最近由Persona发出的消息（用于撤回/排障）。"""
    rows = list(_SENT_CACHE.get(str(scope), []))
    return rows[-max(1, min(int(limit), _SENT_MAX)):]


async def recall_last(
    bot: Bot,
    scope: str,
    *,
    count: int = 1,
    message_id: str | None = None,
) -> dict[str, Any]:
    """主动撤回Persona自己最近发送的消息。

    · 只认当前进程里记录过的 message_id，绝不按用户给的消息 id 删别人的消息；
    · QQ 非管理员通常只有 2 分钟撤回窗口，超时返回明确错误；
    · 成功后从缓存移除，避免重复撤回。
    """
    now = int(time.time())
    max_age = max(1, int(SETTINGS.recall_max_age_seconds))
    bucket = _SENT_CACHE.get(str(scope), [])
    if message_id:
        targets = [row for row in bucket if str(row.get("message_id")) == str(message_id)]
        if not targets:
            return {"ok": False, "error": "message_id 不在Persona最近发送记录里", "recalled": []}
        targets = targets[-1:]
    else:
        count = max(1, min(int(count or 1), 10))
        targets = bucket[-count:]
    if not targets:
        return {"ok": False, "error": "没有可撤回的最近消息", "recalled": []}

    recalled: list[dict[str, Any]] = []
    errors: list[str] = []
    for row in targets:
        mid = str(row.get("message_id"))
        age = now - int(row.get("at") or 0)
        if age > max_age:
            errors.append(f"#{mid} 超过 {max_age}s 撤回窗口（{age}s）")
            continue
        try:
            await bot.call_api("delete_msg", message_id=int(mid))
            recalled.append({"message_id": mid, "kind": row.get("kind"), "text": row.get("text")})
            try:
                bucket.remove(row)
            except ValueError:
                pass
        except Exception as exc:  # noqa: BLE001
            errors.append(f"#{mid} {type(exc).__name__}: {exc}"[:200])
    return {"ok": bool(recalled), "recalled": recalled, "errors": errors}


def pick_bot(bot_qq: str | None = None) -> Bot | None:
    """取发送用的 bot —— **身份断言，fail-closed**（G1）。

    规则：
        · 配置了 bot_qq → 只认 self_id == bot_qq；它不在线就返回 None，
          **绝不 fallback 到"随便一个在线 bot"**（上次 4 条消息漏进真群就是这条路）；
        · 没配 bot_qq（只在测试里出现）才允许取任意一个。
    调用方拿到 None 必须**放弃发送**并记一条失败，而不是换通道。
    """
    bots: dict[str, Bot] = nonebot.get_bots()  # type: ignore[assignment]
    if not bots:
        logger.error("[perception] G1: 没有任何在线 bot，拒绝发送")
        return None
    if bot_qq:
        for self_id, bot in bots.items():
            if str(self_id) == str(bot_qq):
                return bot
        logger.error(
            f"[perception] G1 身份断言失败：配置 bot_qq={bot_qq} 不在线（在线={list(bots)}），拒绝发送"
        )
        return None
    return next(iter(bots.values()))


def _clean(text: str) -> str:
    """清理：出站防泄漏 → 去 CQ 码残留 → 去首尾空白 → 超长截断"""
    text = sanitize_outbound(str(text or ""))
    text = text.replace("&#91;", "[").replace("&#93;", "]")
    text = text.replace("[CQ:", "［CQ:")
    return text.strip()[:MAX_CHARS_PER_MESSAGE]


async def send_group(bot: Bot, group_id: int | str, messages: list[str], delay_ms: int = 1500) -> int:
    """向群里发若干条消息，返回成功条数。单条失败不影响后续。"""
    if _is_duplicate(f"group:{group_id}", "text", "\n".join(messages)):
        return 0
    sent = 0
    for index, raw in enumerate(messages):
        text = _clean(raw)
        if not text:
            continue
        try:
            members = _member_map(f"group:{group_id}")
            payload = _build_segments(text, members)
            response = await bot.call_api(
                "send_group_msg",
                group_id=int(group_id),
                message=payload,
            )
            _remember_sent(f"group:{group_id}", response, "text", text)
            sent += 1
            logger.info(f"[perception] 已发言 group={group_id} ({index + 1}/{len(messages)}): {text[:60]}")
        except Exception as exc:  # noqa: BLE001
            logger.exception(f"[perception] 发送失败 group={group_id}: {exc}")
            break
        # 最后一条之后不用等
        if index < len(messages) - 1 and delay_ms > 0:
            await asyncio.sleep(delay_ms / 1000)
    return sent


async def send_private(bot: Bot, user_id: int | str, messages: list[str], delay_ms: int = 1500) -> int:
    """私聊发送（灰度第 ① 级用得上）"""
    if _is_duplicate(f"private:{user_id}", "text", "\n".join(messages)):
        return 0
    sent = 0
    for index, raw in enumerate(messages):
        text = _clean(raw)
        if not text:
            continue
        try:
            response = await bot.call_api("send_private_msg", user_id=int(user_id), message=text)
            _remember_sent(f"private:{user_id}", response, "text", text)
            sent += 1
        except Exception as exc:  # noqa: BLE001
            logger.exception(f"[perception] 私聊发送失败 user={user_id}: {exc}")
            break
        if index < len(messages) - 1 and delay_ms > 0:
            await asyncio.sleep(delay_ms / 1000)
    return sent


def group_id_of(scope: str) -> int | None:
    """scope → 群号（group:1000000003 → 1000000003）；私聊返回 None"""
    if scope.startswith("group:"):
        try:
            return int(scope.split(":", 1)[1])
        except (IndexError, ValueError):
            return None
    return None


def user_id_of(scope: str) -> int | None:
    """scope → QQ 号（private:1000000002 → 1000000002）"""
    if scope.startswith("private:"):
        try:
            return int(scope.split(":", 1)[1])
        except (IndexError, ValueError):
            return None
    return None


async def send_to_scope(bot: Bot, scope: str, messages: list[str], delay_ms: int = 1500) -> int:
    """按 scope 自动路由群聊/私聊"""
    gid = group_id_of(scope)
    if gid is not None:
        return await send_group(bot, gid, messages, delay_ms)
    uid = user_id_of(scope)
    if uid is not None:
        return await send_private(bot, uid, messages, delay_ms)
    logger.warning(f"[perception] 未知 scope，无法发送：{scope}")
    return 0


def bot_info(bot: Any) -> tuple[str, str]:
    """取 self_id 与机器人昵称（归一化 @ 时用）"""
    self_id = str(getattr(bot, "self_id", ""))
    name = "Persona"
    try:
        config = nonebot.get_driver().config
        nicknames = getattr(config, "nickname", None) or []
        if nicknames:
            name = str(next(iter(nicknames)))
    except Exception:  # noqa: BLE001 - 昵称取不到就用默认值
        pass
    return self_id, name


# 单张表情包上限：QQ/NapCat 对 base64 图片有体积限制，超过就不发（宁可不发也别报错）
MAX_STICKER_BYTES = 3 * 1024 * 1024


async def send_sticker(bot: Bot, scope: str, data: bytes, ext: str, name: str = "sticker") -> bool:
    if data and _is_duplicate(scope, "sticker", hashlib.sha1(data).hexdigest()):
        return False
    """发一张表情包（图片消息段，base64 内联，无需文件挂载）。

    返回是否发送成功；失败只记日志，绝不抛给上层。
    """
    if not data:
        return False
    if len(data) > MAX_STICKER_BYTES:
        logger.warning(f"[perception] 表情包过大跳过 name={name} size={len(data)}")
        return False
    segment = {"type": "image", "data": {"file": "base64://" + base64.b64encode(data).decode("ascii")}}
    gid = group_id_of(scope)
    try:
        if gid is not None:
            response = await bot.call_api("send_group_msg", group_id=gid, message=[segment])
        else:
            uid = user_id_of(scope)
            if uid is None:
                return False
            response = await bot.call_api("send_private_msg", user_id=uid, message=[segment])
        _remember_sent(scope, response, "sticker", name)
        logger.info(f"[perception] 表情包已发出 scope={scope} name={name} {len(data)}B")
        return True
    except Exception:  # noqa: BLE001
        logger.exception(f"[perception] 表情包发送失败 scope={scope} name={name}")
        return False


# ── 通用媒体发送（用户要求：图片/视频/链接/转发也能发）──────────────────────
MAX_MEDIA_BYTES = 8 * 1024 * 1024  # base64 内联上限（再大就走 URL 让协议端自己拉）


async def _send_payload(bot: Bot, scope: str, segments: list[dict], kind: str = "media") -> bool:
    """把消息段数组发到 scope（群/私聊）。异常只记日志。"""
    gid = group_id_of(scope)
    try:
        if gid is not None:
            response = await bot.call_api("send_group_msg", group_id=gid, message=segments)
        else:
            uid = user_id_of(scope)
            if uid is None:
                return False
            response = await bot.call_api("send_private_msg", user_id=uid, message=segments)
        _remember_sent(scope, response, kind)
        return True
    except Exception:  # noqa: BLE001
        logger.exception(f"[perception] 媒体发送失败 scope={scope}")
        return False


async def send_image_bytes(bot: Bot, scope: str, data: bytes, ext: str = "jpg") -> bool:
    """发一张图（base64 内联）。超过上限就不发，避免把协议端打爆。"""
    if data and _is_duplicate(scope, "image", hashlib.sha1(data).hexdigest()):
        return False
    if not data or len(data) > MAX_MEDIA_BYTES:
        logger.warning(f"[perception] 图片过大或为空，跳过 {len(data)}B")
        return False
    b64 = base64.b64encode(data).decode("ascii")
    return await _send_payload(bot, scope, [{"type": "image", "data": {"file": "base64://" + b64}}], kind="image")


async def send_media_url(bot: Bot, scope: str, kind: str, url: str) -> bool:
    """按 URL 发图片/视频（让协议端自己拉，省带宽）"""
    if not url or _is_duplicate(scope, kind, url):
        return False
    seg_type = "video" if kind == "video" else "image"
    return await _send_payload(bot, scope, [{"type": seg_type, "data": {"file": url}}], kind=kind)


async def send_link_card(bot: Bot, scope: str, url: str, title: str = "", desc: str = "") -> bool:
    """发一张链接卡片（share 段）"""
    if not url or _is_duplicate(scope, "link", f"{url}|{title}"):
        return False
    return await _send_payload(
        bot, scope, [{"type": "share", "data": {"url": url, "title": title or url, "content": desc or ""}}],
        kind="link",
    )


async def send_forward(bot: Bot, scope: str, nodes: list[dict]) -> bool:
    """发合并转发（nodes: [{name, uin, content}]）"""
    if not nodes or _is_duplicate(scope, "forward", "\n".join(str(n.get("content")) for n in nodes)):
        return False
    gid = group_id_of(scope)
    payload_nodes = [
        {"type": "node", "data": {"name": str(n.get("name") or "某人"), "uin": str(n.get("uin") or "0"),
                                  "content": str(n.get("content") or "")}}
        for n in nodes[:20]
    ]
    try:
        if gid is not None:
            response = await bot.call_api("send_group_forward_msg", group_id=gid, messages=payload_nodes)
        else:
            uid = user_id_of(scope)
            if uid is None:
                return False
            response = await bot.call_api("send_private_forward_msg", user_id=uid, messages=payload_nodes)
        _remember_sent(scope, response, "forward")
        return True
    except Exception:  # noqa: BLE001
        logger.exception(f"[perception] 合并转发发送失败 scope={scope}")
        return False


async def send_notice(bot: Bot, scope: str, text: str) -> int:
    """发一条系统/故障提示（统一出口：走 send_to_scope，因此有幂等与审计）"""
    if not text:
        return 0
    return await send_to_scope(bot, scope, [text], 0)


# ── 出站防泄漏（注入防护第 4 层，用户 2026-10-18）───────────────────────────
# 她的话发出去之前过一遍：把"内部路径 / 密钥 / 提示词原文碎片"抹掉。
# 为什么需要：一旦被注入成功，模型可能把 /persona/... 路径、桥 token、系统提示词原文吐到群里。
# 原则：宁可少说一句，不泄露一行内部信息；命中就替换成 `[已隐去]`，并记一条 warning 便于排查。
_LEAK_PATTERNS = (
    (re.compile(r"/persona/[\w./-]*"), "[路径已隐去]"),
    (re.compile(r"/data/[\w./-]*"), "[路径已隐去]"),
    (re.compile(r"\bsk-[A-Za-z0-9]{10,}\b"), "[密钥已隐去]"),
    (re.compile(r"(?i)bearer\s+[A-Za-z0-9._-]{10,}"), "[令牌已隐去]"),
    (re.compile(r"SESSION_ID|PERSONA_BRIDGE|ONEBOT_ACCESS_TOKEN"), "[标识已隐去]"),
)


def sanitize_outbound(text: str) -> str:
    """发送前的最后一道闸：抹掉内部路径/密钥/环境变量名。"""
    if not text:
        return text
    out = text
    hit = False
    for pat, repl in _LEAK_PATTERNS:
        new = pat.sub(repl, out)
        if new != out:
            hit = True
            out = new
    if hit:
        logger.warning("[perception] 出站内容含内部信息，已隐去（注入防护第 4 层）")
    return out


# ── 真 @（用户 2026-10-18）：她说"@某某"要变成 QQ 真正的@蓝字，而不是纯文本 ────────
# 做法：从消息池里取该群"昵称 → QQ"的映射，把文本里的 `@昵称` 拆成 at 段 + text 段发送。
# 匹配规则：优先最长昵称（避免"小猫"命中"小猫咪"的前缀）；只对已知群成员生效，未知名字保持纯文本。
_AT_RE = re.compile(r"@([^\s@:：,，。！!？?]{1,24})")


def _member_map(scope: str) -> dict[str, str]:
    """scope → {昵称: QQ}。读池子（同步 sqlite3，数据量小，几十毫秒）。"""
    import sqlite3

    path = getattr(bus, "DB_PATH", None)
    if not path:
        return {}
    try:
        conn = sqlite3.connect(f"file:{path}?mode=ro", uri=True, timeout=3)
        try:
            rows = conn.execute(
                "SELECT nickname, user_id FROM messages WHERE scope=? AND nickname<>'' AND user_id<>'' "
                "GROUP BY user_id ORDER BY MAX(seq) DESC LIMIT 200",
                (scope,),
            ).fetchall()
        finally:
            conn.close()
    except Exception:  # noqa: BLE001
        return {}
    out: dict[str, str] = {}
    for nick, uid in rows:
        nick = str(nick or "").strip()
        if nick and nick not in out:
            out[nick] = str(uid)
    return out


def _build_segments(text: str, members: dict[str, str]) -> list[dict[str, Any]]:
    """把 `@昵称` 转成 at 段；其余保持文本。返回 onebot v11 消息段数组。"""
    if not text or "@" not in text or not members:
        return [{"type": "text", "data": {"text": text}}]

    # 昵称按长度倒序，长名字优先匹配
    names = sorted(members.keys(), key=len, reverse=True)
    segments: list[dict[str, Any]] = []
    pos = 0
    while pos < len(text):
        at = text.find("@", pos)
        if at < 0:
            segments.append({"type": "text", "data": {"text": text[pos:]}})
            break
        if at > pos:
            segments.append({"type": "text", "data": {"text": text[pos:at]}})
        rest = text[at + 1 :]
        hit = next((n for n in names if rest.startswith(n)), None)
        if hit:
            segments.append({"type": "at", "data": {"qq": members[hit]}})
            # @ 之后补一个空格，但要避免和原文里已有的空格重复
            tail = rest[len(hit):]
            if not tail.startswith((" ", "\u3000")):
                segments.append({"type": "text", "data": {"text": " "}})
            pos = at + 1 + len(hit)
        else:
            segments.append({"type": "text", "data": {"text": "@"}})
            pos = at + 1
    return [seg for seg in segments if not (seg["type"] == "text" and not seg["data"]["text"])]


async def send_file(bot: Bot, scope: str, url: str, name: str = "") -> bool:
    """发一个文件（OneBot upload_group_file / upload_private_file）。

    限制：NapCat 只能拿**URL**（远端的它自己下载）或它自己主机上的路径。
    本地文件走 `/persona/out/` → 宿主 `/data/qqbot/exports`（NapCat 容器同路径只读挂载）。
    注意：NapCat 进程以 uid 1000(napcat) 运行，`/data/qqbot/exports` 必须让 uid 1000
    可进入/可读（部署时 `chgrp 1000 && chmod 2750`，文件 644）；否则 NapCat 的
    `fs.existsSync()` 会返回 false，报 `识别URL失败, uri= /data/qqbot/exports/...`。
    """
    # 本地文件：她写到 /persona/out/ 的文件，NapCat 能从 settings.exports_dir 读到
    prefix = SETTINGS.persona_out_prefix + "/"
    local = ""
    if url.startswith(prefix):
        local = str(SETTINGS.exports_dir / url[len(prefix):])
        url = local
    elif url.startswith("out/"):
        local = str(SETTINGS.exports_dir / url[len("out/"):])
        url = local
    if not url or not (re.match(r"^https?://", url) or local):
        logger.warning(f"[perception] send_file 只支持 http(s) URL 或 /persona/out/ 下的文件：{url[:60]}")
        return False
    if _is_duplicate(scope, "file", url):
        return False
    filename = name or url.rsplit("/", 1)[-1][:60] or "file"
    group_id = group_id_of(scope)
    user_id = user_id_of(scope)
    try:
        if group_id is not None:
            response = await bot.call_api(
                "upload_group_file",
                group_id=int(group_id),
                file=url,
                name=filename,
            )
        elif user_id is not None:
            response = await bot.call_api(
                "upload_private_file",
                user_id=int(user_id),
                file=url,
                name=filename,
            )
        else:
            return False
        _remember_sent(scope, response, "file", filename)
        logger.info(f"[perception] 已发文件 scope={scope} name={filename}")
        return True
    except Exception as exc:  # noqa: BLE001
        hint = ""
        if "识别URL失败" in str(exc) or "1200" in str(exc):
            hint = "（检查 /data/qqbot/exports 对 uid 1000 是否可读：chgrp 1000 && chmod 2750，文件 644）"
        logger.exception(f"[perception] 发文件失败 scope={scope}: {exc}{hint}")
        return False


async def send_segments(bot: Bot, scope: str, segments: list[dict], kind: str = "segment") -> bool:
    """发任意消息段（语音/音乐/引用/@全体 都用它）。带幂等。"""
    if not segments:
        return False
    token = repr(segments)[:400]
    if _is_duplicate(scope, kind, token):
        return False
    ok = await _send_payload(bot, scope, segments, kind=kind)
    if ok:
        logger.info(f"[perception] 已发 {kind} scope={scope} 段数={len(segments)}")
    return ok


def _local_or_url(path: str) -> str:
    """把她看到的 /persona/out/xxx 映射成 NapCat 能读的 exports 路径。"""
    prefix = SETTINGS.persona_out_prefix + "/"
    if path.startswith(prefix):
        return str(SETTINGS.exports_dir / path[len(prefix):])
    if path.startswith("out/"):
        return str(SETTINGS.exports_dir / path[len("out/"):])
    return path


async def send_voice(bot: Bot, scope: str, path: str) -> bool:
    """发语音（record 段）。文件先放 /persona/out/，或给 http(s) URL。"""
    target = _local_or_url(path.strip())
    if not target:
        return False
    return await send_segments(bot, scope, [{"type": "record", "data": {"file": target}}], kind="voice")


async def send_music(bot: Bot, scope: str, arg: str) -> bool:
    """发音乐卡片。

    形如：
        163 1234567            → 网易云
        qq 1234567             → QQ 音乐
        custom <url>|<标题>|<歌手> → 自定义（需要可公网访问的音频）
    """
    parts = [x.strip() for x in arg.replace("｜", "|").split("|")]
    head = parts[0].split()
    platform = (head[0] if head else "").lower()
    ident = head[1] if len(head) > 1 else ""
    if platform in ("163", "netease"):
        data = {"type": "163", "id": ident}
    elif platform in ("qq", "qqmusic"):
        data = {"type": "qq", "id": ident}
    elif platform in ("custom", "自定义") and len(parts) >= 2:
        data = {
            "type": "custom",
            "url": parts[1],
            "audio": parts[1],
            "title": parts[2] if len(parts) > 2 else "分享一首歌",
            "singer": parts[3] if len(parts) > 3 else "",
            "image": "",
        }
    else:
        return False
    return await send_segments(bot, scope, [{"type": "music", "data": data}], kind="music")


async def send_quote_reply(bot: Bot, scope: str, message_id: int | None, text: str) -> bool:
    """引用回复：reply 段 + 文本。message_id 取不到就退化成普通文本。"""
    clean = _clean(text)
    if message_id is None:
        return await send_to_scope(bot, scope, [clean], 0) > 0
    if _is_duplicate(scope, "quote", f"{message_id}:{clean}"):
        return False
    return await _send_payload(
        bot,
        scope,
        [
            {"type": "reply", "data": {"id": str(message_id)}},
            {"type": "text", "data": {"text": " " + clean}},
        ],
        kind="quote",
    )


async def send_at_all(bot: Bot, scope: str, text: str = "") -> bool:
    """群发 @全体成员（私聊无效）。"""
    if group_id_of(scope) is None:
        return False
    segments = [{"type": "at", "data": {"qq": "all"}}]
    if text:
        segments.append({"type": "text", "data": {"text": " " + _clean(text)}})
    return await send_segments(bot, scope, segments, kind="atall")
