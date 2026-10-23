"""附带轮（outbound.py）—— 她说完话后，决定要不要再发点「东西」

图片、视频、链接、合并转发这些内容也要能「发出去」。
这里用**一次额外的决策调用**实现，完全不碰主 SPEAK 契约（桥用 parse:false 只回原文）：

    平台：你刚才说了……这批里能直接发出去的有 #12 图片 / #14 视频 / #15 链接
          要附带就回一行：STICKER: 情绪 / ATTACH: 12,14 / LINK: url | 标题 / FORWARD: 起-止 / NO
    她：  任意组合（最多 3 个动作）
    平台：按动作执行（表情包还多一轮"审阅后发送"）

设计原则：
    · 默认**不设冷却**（用户口径：发几次由她定），只由 attach_enabled 总开关控制；
    · 任何一步失败只记日志，绝不影响已经发出去的主回复；
    · 每一发都进 G6 审计（kind=image/video/link/forward/sticker）。
"""

from __future__ import annotations

import re
from typing import Any

from nonebot import logger

from . import bridge, bus, segments, sender, sticker

MAX_ACTIONS = 5          # 一轮最多执行几个附带动作（语音/音乐/引用/@全体 也算）
MAX_ATTACH = 3           # ATTACH 一次最多几张
FORWARD_MAX_NODES = 10   # 合并转发最多打包几条


def _media_brief(rows: list[dict[str, Any]]) -> str:
    """把可发送的媒体列成清单（给她选）"""
    lines: list[str] = []
    for row in rows[:8]:
        kind = str(row.get("kind") or "")
        label = {"image": "图片", "mface": "动画表情", "video": "视频",
                 "link": "链接", "card": "卡片", "record": "语音"}.get(kind, kind)
        extra = str(row.get("summary") or row.get("title") or "").strip()
        lines.append(f"  #{row['id']} {label}{('（' + extra[:30] + '）') if extra else ''}")
    return "\n".join(lines)


async def _do_file(scope: str, arg: str, bot: Any) -> dict[str, Any]:
    """发文件：参数形如 `<url> | <显示名>`（显示名可省）"""
    parts = [x.strip() for x in arg.split("|", 1)]
    url = parts[0]
    name = parts[1] if len(parts) > 1 else ""
    ok = await sender.send_file(bot, scope, url, name)
    return {"kind": "file", "ok": ok, "detail": f"{name or url[:40]}"}


async def _do_voice(scope: str, arg: str, bot: Any) -> dict[str, Any]:
    """发语音：参数是 /persona/out/xxx 或 http(s) 链接"""
    ok = await sender.send_voice(bot, scope, arg.strip())
    return {"kind": "voice", "ok": ok, "detail": arg.strip()[:60]}


async def _do_music(scope: str, arg: str, bot: Any) -> dict[str, Any]:
    ok = await sender.send_music(bot, scope, arg)
    return {"kind": "music", "ok": ok, "detail": arg[:60]}


async def _do_atall(scope: str, arg: str, bot: Any) -> dict[str, Any]:
    ok = await sender.send_at_all(bot, scope, arg.strip())
    return {"kind": "atall", "ok": ok, "detail": arg[:40]}


async def _do_reply(scope: str, arg: str, bot: Any, batch_seqs: list[int]) -> dict[str, Any]:
    """引用回复：`REPLY: <n> | <正文>`，n=本批第几条（1 = 最新）。"""
    head, _, body = arg.partition("|")
    head = head.strip()
    body = body.strip() or "（引用）"
    try:
        n = int(head)
    except ValueError:
        n = 1
    if not batch_seqs:
        return {"kind": "quote", "ok": False, "error": "本批没有可引用的消息"}
    idx = max(0, min(len(batch_seqs) - 1, n - 1))
    seq = batch_seqs[-1 - idx] if idx < len(batch_seqs) else batch_seqs[-1]
    mid = await bus.message_id_of(seq)
    ok = await sender.send_quote_reply(bot, scope, mid, body)
    return {"kind": "quote", "ok": ok, "detail": f"#{seq} -> msg_id={mid}"}


def parse_actions(text: str) -> list[tuple[str, str]]:
    """把她的附带决定解析成 [(action, arg)]；返回空 = 什么都没选"""
    actions: list[tuple[str, str]] = []
    for raw_line in str(text or "").replace("\r", "").split("\n"):
        line = raw_line.strip().strip("`").strip()
        if not line:
            continue
        if re.match(r"^NO\b", line, re.I):
            continue
        hit = re.match(r"^(STICKER|ATTACH|LINK|FORWARD|FILE|VOICE|MUSIC|REPLY|ATALL)\s*[:：]\s*(.*)$", line, re.I)
        if hit:
            actions.append((hit.group(1).upper(), hit.group(2).strip()))
            if len(actions) >= MAX_ACTIONS:
                break
    return actions


async def _do_sticker(
    scope: str, session_id: str | None, query: str, bot: Any, review_enabled: bool = True
) -> dict[str, Any]:
    """表情包：检索候选 →（可选）她审阅 → 发送。

    审阅是**可选项**：`sticker_review_enabled: false` 时直接发最高分那张，
    少一次模型调用；true 时保留"她亲眼看一眼再选"的流程。
    """
    candidates = await sticker.resolve(query, limit=3)
    if not candidates:
        return {"kind": "sticker", "ok": False, "error": f"no-candidate:{query}"}
    if review_enabled:
        listing = "\n".join(f"{i + 1}. {c.label()}" for i, c in enumerate(candidates))
        review_prompt = (
            f"【表情包审阅】按你说的「{query}」，候选 {len(candidates)} 张：\n{listing}\n"
            "用 read_image 看一眼再决定（也可以只看描述）。\n"
            "选定 → 只回一行：SEND: <编号>（要多张就 SEND: 1,3）；都不要 → SKIP"
        )
        review = await bridge.decide(scope=scope, prompt=review_prompt, session_id=session_id, raw=True)
        picks = sticker.parse_review(review.get("text") or "")
        picks = [n for n in picks if 1 <= n <= len(candidates)][:MAX_ATTACH]
        if not picks:
            return {"kind": "sticker", "ok": False, "error": "skipped"}
    else:
        picks = [1]  # 不审阅：直接发最高分那张
    sent = 0
    for pick in picks:
        cand = candidates[pick - 1]
        data, ext = await sticker.fetch_bytes(cand)
        if data and await sender.send_sticker(bot, scope, data, ext, name=f"{cand.source}-{cand.key}"):
            sent += 1
    return {"kind": "sticker", "ok": sent > 0, "count": sent, "text": f"{query} x{sent}"}


async def _do_attach(scope: str, arg: str, media_rows: list[dict[str, Any]], bot: Any) -> dict[str, Any]:
    """把本批里的图片/视频/链接转发出去（她挑编号）"""
    wanted = [int(x) for x in re.findall(r"\d{1,8}", arg)][:MAX_ATTACH]
    by_id = {int(r["id"]): r for r in media_rows}
    sent, errors = 0, []
    for mid in wanted:
        row = by_id.get(mid)
        if not row:
            errors.append(f"#{mid} 不在本批")
            continue
        kind = str(row.get("kind") or "")
        if kind in ("image", "mface"):
            data, ext = await bridge.read_media_file(mid)
            if not data:
                # 缓存没有：现取一次
                fetched = await bridge.fetch_media(mid, row.get("url") or "", "jpg")
                if fetched.get("ok"):
                    data, ext = await bridge.read_media_file(mid)
            if data and await sender.send_image_bytes(bot, scope, data, ext or "jpg"):
                sent += 1
            else:
                errors.append(f"#{mid} 图片发送失败")
        elif kind == "video":
            ok = await sender.send_media_url(bot, scope, "video", str(row.get("url") or ""))
            sent += 1 if ok else 0
            if not ok:
                errors.append(f"#{mid} 视频发送失败")
        elif kind in ("link", "card"):
            ok = await sender.send_link_card(bot, scope, str(row.get("url") or ""), str(row.get("title") or ""))
            sent += 1 if ok else 0
            if not ok:
                errors.append(f"#{mid} 链接发送失败")
        else:
            errors.append(f"#{mid} 类型 {kind} 暂不支持转发")
    return {"kind": "attach", "ok": sent > 0, "count": sent, "text": arg, "error": "; ".join(errors) or None}


async def _do_link(scope: str, arg: str, bot: Any) -> dict[str, Any]:
    """发一个链接卡片：LINK: url | 标题"""
    parts = [p.strip() for p in arg.split("|", 1)]
    url = parts[0]
    title = parts[1] if len(parts) > 1 else ""
    ok = await sender.send_link_card(bot, scope, url, title)
    return {"kind": "link", "ok": ok, "count": 1 if ok else 0, "text": url}


async def _do_forward(scope: str, arg: str, bot: Any) -> dict[str, Any]:
    """把池子里某几条打包成合并转发：FORWARD: 120-125 或 FORWARD: 120,123"""
    ids = [int(x) for x in re.findall(r"\d{1,8}", arg)]
    if len(ids) == 2 and "-" in arg:
        start, end = sorted(ids)
        rows = await bus.messages_between(scope, start, end, limit=FORWARD_MAX_NODES)
    else:
        rows = await bus.messages_by_seqs(scope, ids[:FORWARD_MAX_NODES])
    if not rows:
        return {"kind": "forward", "ok": False, "error": "no-message-in-range"}
    nodes = [
        {"name": str(r.get("nickname") or "某人"), "uin": str(r.get("user_id") or "0"),
         "content": str(r.get("text") or "")}
        for r in rows
    ]
    ok = await sender.send_forward(bot, scope, nodes)
    return {"kind": "forward", "ok": ok, "count": len(nodes), "text": arg}


async def run_round(
    scope: str,
    session_id: str | None,
    last_reply: list[str],
    media_rows: list[dict[str, Any]],
    bot: Any,
    review_enabled: bool = True,
) -> dict[str, Any]:
    """附带轮主流程：问她要不要再发点东西 → 按她的选择执行。永不抛异常。"""
    info: dict[str, Any] = {"actions": [], "sent": 0}
    try:
        if bot is None:
            return {**info, "error": "no-bot"}
        said = " / ".join(last_reply)[:300]
        brief = _media_brief([r for r in media_rows if r.get("kind") in ("image", "mface", "video", "link", "card")])
        ask = (
            f"【附带请求】你刚才在群里说了：{said}\n"
            + (f"这批消息里可转手发出去的有：\n{brief}\n" if brief else "")
            + "要不要再附带点什么？想要就回对应的一行（最多 3 条，不要就只回 NO）：\n"
            "  发表情包        → STICKER: <情绪或用途，2~6 字>\n"
            "  转发上面的图片/视频 → ATTACH: <编号,逗号分隔>\n"
            "  发一个链接卡片      → LINK: <网址> | <标题>\n"
            "  把池子里几条打包转发 → FORWARD: <起>-<止>（池子编号，可问平台）\n"
            "  都不发            → NO"
        )
        first = await bridge.decide(scope=scope, prompt=ask, session_id=session_id, raw=True)
        if not first.get("ok"):
            return {**info, "error": f"ask-failed:{first.get('error')}"}
        actions = parse_actions(first.get("text") or "")
        if not actions:
            return info

        for action, arg in actions:
            try:
                if action == "STICKER":
                    result = await _do_sticker(scope, session_id, arg, bot, review_enabled=review_enabled)
                elif action == "ATTACH":
                    result = await _do_attach(scope, arg, media_rows, bot)
                elif action == "LINK":
                    result = await _do_link(scope, arg, bot)
                elif action == "FORWARD":
                    result = await _do_forward(scope, arg, bot)
                elif action == "FILE":
                    result = await _do_file(scope, arg, bot)
                else:
                    continue
            except Exception as exc:  # noqa: BLE001 - 单个动作失败不影响其它
                logger.exception(f"[perception] 附带动作 {action} 失败")
                result = {"kind": action.lower(), "ok": False, "error": str(exc)}
            info["actions"].append(result)
            if result.get("ok"):
                info["sent"] += 1
            await bus.log_send(
                scope,
                trigger=f"attach:{action.lower()}",
                kind=result.get("kind") or action.lower(),
                target=scope,
                text=str(result.get("text") or "")[:300],
                ok=bool(result.get("ok")),
                error=result.get("error"),
                session_id=session_id,
            )
        if info["sent"]:
            logger.info(f"[perception] 附带轮完成 scope={scope} 动作={[(a.get('kind'), a.get('ok')) for a in info['actions']]}")
        return info
    except Exception:  # noqa: BLE001
        logger.exception("[perception] 附带轮异常")
        return {**info, "error": "exception"}


async def apply_actions(
    scope: str,
    session_id: str | None,
    actions: list[list[str]] | list[tuple[str, str]],
    media_rows: list[dict[str, Any]],
    bot: Any,
    review_enabled: bool = True,
    batch_seqs: list[int] | None = None,
) -> list[dict[str, Any]]:
    """执行主回复里带的动作（省掉"附带轮"那次额外模型调用）。

    actions 来自桥的解析结果：[[action, arg], ...]；最多 3 个，逐个执行、失败不影响后续。
    """
    results: list[dict[str, Any]] = []
    for item in list(actions)[:MAX_ACTIONS]:
        try:
            action, arg = (item[0], item[1]) if isinstance(item, (list, tuple)) else ("", "")
        except Exception:  # noqa: BLE001
            continue
        action = str(action).upper()
        try:
            if action == "STICKER":
                res = await _do_sticker(scope, session_id, str(arg), bot, review_enabled=review_enabled)
            elif action == "ATTACH":
                res = await _do_attach(scope, str(arg), media_rows, bot)
            elif action == "LINK":
                res = await _do_link(scope, str(arg), bot)
            elif action == "FORWARD":
                res = await _do_forward(scope, str(arg), bot)
            elif action == "FILE":
                res = await _do_file(scope, str(arg), bot)
            elif action == "VOICE":
                res = await _do_voice(scope, str(arg), bot)
            elif action == "MUSIC":
                res = await _do_music(scope, str(arg), bot)
            elif action == "REPLY":
                res = await _do_reply(scope, str(arg), bot, batch_seqs or [])
            elif action == "ATALL":
                res = await _do_atall(scope, str(arg), bot)
            else:
                continue
        except Exception as exc:  # noqa: BLE001
            logger.exception(f"[perception] 附带动作 {action} 执行失败：{exc}")
            res = {"kind": action.lower(), "ok": False, "error": str(exc)}
        results.append({"action": action, **res})
    return results
