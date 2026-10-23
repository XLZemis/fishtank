"""消息段解析器（segments.py）—— 把 OneBot 消息段解析成「文本 + 媒体清单」

用户要求：**图片、视频、链接、卡片、表情包、合并转发等递归消息**都要有解析，而不是只留占位符。

设计要点：
    1. 纯函数 + 一个异步扩展点（合并转发要调 get_forward_msg 拉嵌套节点）；
    2. 每个媒体段产出两样东西：
         · 文本里一个锚点 `{{M<idx>}}`（idx = 该消息里媒体列表的下标）
         · media 列表里一条结构化记录（kind/url/file/summary/title/size）
       锚点由 bus.record 替换成数据库里真实的 media id（`〈图片#12〉`），
       这样她在 prompt 里看到的编号 → 平台能反查到 URL、能下载给她看；
    3. 递归深度与节点数都有上限（防循环/防爆量）。
"""

from __future__ import annotations

import html
import json
import re
from dataclasses import dataclass, field
from typing import Any

from nonebot import logger

MAX_FORWARD_DEPTH = 4      # 合并转发最多展开四层（多层嵌套可能打不开）
MAX_FORWARD_NODES = 40     # 单条合并转发最多展开 40 个节点
MAX_FORWARD_CHARS = 160    # 每个转发节点正文最多保留 160 字
MAX_FORWARD_MEDIA = 60     # 一条合并转发最多把多少个内层媒体接进媒体池


@dataclass
class Media:
    """一条媒体/可回指结构（图片、视频、语音、文件、表情包、链接、卡片、合并转发）"""

    kind: str                      # image/video/record/file/mface/link/card/forward
    url: str = ""
    file: str = ""
    summary: str = ""
    title: str = ""
    size: int = 0
    forward_id: str = ""
    # 合并转发段里内嵌的完整节点（NapCat 对"转发里套转发"会把 content 直接带下来）；
    # 有它就不需要再调 get_forward_msg（嵌套 id 走 API 会报"找不到相关的聊天记录"）。
    forward_content: list[Any] = field(default_factory=list)
    depth: int = 0

    def anchor(self, idx: int) -> str:
        """文本里用的占位锚点（bus.record 会替换成真实 media id）"""
        return "{{M%d}}" % idx


@dataclass
class Parsed:
    text: str
    media: list[Media] = field(default_factory=list)


# ── 卡片/链接提取 ─────────────────────────────────────────────────────────────

_JSON_TITLE_KEYS = ("prompt", "title", "desc", "summary", "app")
_XML_TITLE_RE = re.compile(r"<title[^>]*>(.*?)</title>", re.S | re.I)
_XML_URL_RE = re.compile(r"url=\"(http[^\"]+)\"", re.I)
_JSON_URL_RE = re.compile(r'"(?:qqdocurl|jumpUrl|url)"\s*:\s*"(http[^"]+)"')
_HTTP_URL_RE = re.compile(r"https?://[^\s\"'<>]+", re.I)


def _deep_strings(node):
    """递归遍历卡片 JSON 的所有字符串值。

    为什么需要：QQ 卡片（尤其 B 站小程序）的字段位置并不固定 ——
    `qqdocurl` 在 `meta.detail_1` 里，有时候 `desc` 才是视频标题、`title` 只是「哔哩哔哩」；
    只按固定路径取会漏。这里统一深搜，拿到所有字符串再按语义挑。
    """
    if isinstance(node, str):
        yield node
    elif isinstance(node, dict):
        for value in node.values():
            yield from _deep_strings(value)
    elif isinstance(node, (list, tuple)):
        for value in node:
            yield from _deep_strings(value)


def _deep_first(data, keys: tuple[str, ...]) -> str:
    """深搜第一个命中的字符串字段（用于 title / desc 这类语义字段）。"""
    found = ""

    def walk(node) -> None:
        nonlocal found
        if found:
            return
        if isinstance(node, dict):
            for key, value in node.items():
                if key in keys and isinstance(value, str) and value.strip():
                    found = value.strip()
                    return
                walk(value)
        elif isinstance(node, (list, tuple)):
            for value in node:
                walk(value)

    walk(data)
    return found


def _urls_in_text(text: str) -> list[str]:
    """从任意文本抠 HTTP 链接（先还原 QQ 卡片 JSON 里的转义斜杠 `https:\\/\\/…`）。"""
    text = html.unescape(text or "").replace("\\/", "/")
    out: list[str] = []
    for match in _HTTP_URL_RE.finditer(text):
        url = match.group(0).rstrip(".,;:!?")
        if url not in out:
            out.append(url)
    return out


def _deep_urls(data) -> list[str]:
    """深搜卡片 JSON 里的所有链接（去重保序）。"""
    out: list[str] = []
    for text in _deep_strings(data):
        for url in _urls_in_text(text):
            if url not in out:
                out.append(url)
    return out



def _card_title(raw: str) -> str:
    """从 json/xml 卡片里抠出一个可读标题"""
    if not raw:
        return ""
    # JSON 卡片
    if raw.lstrip().startswith("{"):
        try:
            data = json.loads(raw)
        except Exception:  # noqa: BLE001
            data = None
        if isinstance(data, dict):
            meta = data.get("meta") or {}
            if isinstance(meta, dict):
                for value in meta.values():
                    if isinstance(value, dict):
                        for key in _JSON_TITLE_KEYS:
                            title = value.get(key)
                            if isinstance(title, str) and title.strip():
                                return title.strip()[:60]
            for key in _JSON_TITLE_KEYS:
                title = data.get(key)
                if isinstance(title, str) and title.strip():
                    return title.strip()[:60]
    # XML 卡片
    match = _XML_TITLE_RE.search(raw)
    if match:
        return html.unescape(re.sub(r"<[^>]+>", "", match.group(1))).strip()[:60]
    return ""


# ── 卡片分型（用户 2026-10-20：B站至少解析出 BV 号，其它卡片按内容分型）──────────
_CARD_KIND = {
    "com.tencent.miniapp_01": "小程序",
    "com.tencent.miniapp": "小程序",
    "com.tencent.structmsg": "分享",
    "com.tencent.music.lua": "音乐",
    "com.tencent.music": "音乐",
    "com.tencent.doc": "文档",
    "com.tencent.multimsg": "聊天记录",
    "com.tencent.qqsports.figure": "话题",
}
_BV_RE = re.compile(r"(BV[0-9A-Za-z]{10})")


def _card_meta(raw: str) -> dict[str, str]:
    """解析卡片 → {app, kind, title, desc, url, bvid}。

    B 站卡片（很常见，实测 2026-10-20）：
        app=com.tencent.miniapp_01，meta.detail_1.title=「哔哩哔哩」，
        meta.detail_1.desc=真正的视频标题，meta.detail_1.qqdocurl=https://b23.tv/xxxx。
    旧实现只认 title，于是锚点永远是「B站:哔哩哔哩」，也没有 BV/链接 —— 这就是
    「B 站解析器失败」的根因；现在改成深搜 URL/BV，并用 desc 当标题。
    """
    out = {"app": "", "kind": "卡片", "title": "", "desc": "", "url": "", "bvid": ""}
    if not raw:
        return out
    if raw.lstrip().startswith("{"):
        try:
            data = json.loads(raw)
        except Exception:  # noqa: BLE001
            data = None
        if isinstance(data, dict):
            app = str(data.get("app") or "")
            out["app"] = app
            out["kind"] = _CARD_KIND.get(app, "卡片")

            # 深搜：所有链接、BV 号、候选标题/描述
            urls = _deep_urls(data)
            all_text = " ".join(_deep_strings(data))
            bv_match = _BV_RE.search(all_text)
            if bv_match:
                out["bvid"] = bv_match.group(1)
            bili_url = next(
                (u for u in urls if "b23.tv" in u or "bilibili.com" in u.lower()),
                "",
            )
            title = _deep_first(data, ("title",))
            desc = _deep_first(data, ("desc", "description"))
            prompt = str(data.get("prompt") or "").replace("[QQ小程序]", "").replace("[分享]", "").strip()

            # 普通卡片的 url 字段经常没有 scheme（如 m.q.qq.com/a/s/...），补上 https
            doc_url = _deep_first(data, ("qqdocurl", "jumpUrl", "url"))
            if doc_url and not doc_url.lower().startswith("http"):
                doc_url = "https://" + doc_url.lstrip("/")
            is_bili = bool(bili_url or out["bvid"] or re.search(r"b23\.tv|bilibili\.com", all_text, re.I))
            out["url"] = bili_url or doc_url
            out["desc"] = desc[:120]
            if is_bili:
                out["kind"] = "B站"
                # B 站卡片的 title 字段固定是「哔哩哔哩」，真正的视频标题在 desc
                out["title"] = (desc or prompt or title or "B站视频")[:80]
                out["desc"] = desc[:120]
            else:
                out["title"] = (title or prompt or desc)[:80]
            return out
    # XML / 兜底：至少把标题、链接、BV 抠出来
    match = _XML_TITLE_RE.search(raw)
    if match:
        out["title"] = html.unescape(re.sub(r"<[^>]+>", "", match.group(1))).strip()[:80]
    match_url = _JSON_URL_RE.search(raw) or _XML_URL_RE.search(raw)
    if match_url:
        out["url"] = html.unescape(match_url.group(1)).replace("\\/", "/")
    for url in _urls_in_text(raw):
        if "b23.tv" in url or "bilibili.com" in url.lower():
            out["url"] = url
            break
    if "b23.tv" in out["url"] or "bilibili.com" in out["url"].lower():
        out["kind"] = "B站"
    match_bv = _BV_RE.search(raw)
    if match_bv:
        out["bvid"] = match_bv.group(1)
        out["kind"] = "B站"
    return out


def _card_url(raw: str) -> str:
    """从卡片原文抠链接（JSON 走深搜，XML 走正则）。"""
    if not raw:
        return ""
    if raw.lstrip().startswith("{"):
        try:
            data = json.loads(raw)
        except Exception:  # noqa: BLE001
            data = None
        if isinstance(data, dict):
            urls = _deep_urls(data)
            if urls:
                bili = next((u for u in urls if "b23.tv" in u or "bilibili.com" in u.lower()), "")
                return bili or urls[0]
    match = _JSON_URL_RE.search(raw) or _XML_URL_RE.search(raw)
    if not match:
        return ""
    # QQ 卡片 JSON 里的斜杠是转义的（https:\/\/…），落库前还原，省得下游每处都要处理
    return html.unescape(match.group(1)).replace("\\/", "/")


def _forward_resid(raw: str) -> str:
    """从 json 卡片里抠出合并转发的 resid（QQ 的"聊天记录"卡片没有 forward 段）。

    典型：{"app":"com.tencent.multimsg","meta":{"detail":{"resid":"7698…","uniseq":"…"}}}
    """
    if not raw or not raw.lstrip().startswith("{"):
        return ""
    try:
        data = json.loads(raw)
    except Exception:  # noqa: BLE001
        return ""
    if not isinstance(data, dict):
        return ""
    app = str(data.get("app") or "")
    meta = data.get("meta") or {}
    if isinstance(meta, dict):
        for value in meta.values():
            if not isinstance(value, dict):
                continue
            for key in ("resid", "m_resid", "forward_id"):
                val = value.get(key)
                if isinstance(val, str) and val.strip().isdigit():
                    return val.strip()
    if "multimsg" in app:
        for key in ("resid", "m_resid"):
            val = data.get(key)
            if isinstance(val, str) and val.strip():
                return val.strip()
    return ""


# ── 主解析 ───────────────────────────────────────────────────────────────────


def _data_of(seg: Any) -> dict[str, Any]:
    """取消息段的 data。

    注意：一条消息段既可能是 nonebot 的 MessageSegment 对象，也可能是
    **get_forward_msg 返回的原始 dict**（{"type": "text", "data": {...}}）。
    只支持前者的话，合并转发里每条都会变成 `〈unknown〉`——2026-10-19 的 bug 就出在这。
    """
    if isinstance(seg, dict):
        return dict(seg.get("data") or {})
    return dict(getattr(seg, "data", {}) or {})


def _type_of(seg: Any) -> str:
    if isinstance(seg, dict):
        return str(seg.get("type") or "unknown")
    return str(getattr(seg, "type", "unknown") or "unknown")


def has_meaningful_content(message: Any) -> bool:
    """这条消息是否有实际内容（用于丢弃空消息/输入中事件）。

    - 纯空文本（或纯空白）→ False
    - 空列表 → False
    - at / reply / 图片 / 文件 / 卡片 / 转发等任何非文本段 → True
      （只 @ 不说话、只引用不说话都是真实动作，不能丢）
    """
    for seg in message or []:
        if _type_of(seg) == "text":
            if str(_data_of(seg).get("text", "")).strip():
                return True
        else:
            return True
    return False


def parse_segments(
    message: Any,
    *,
    self_id: str = "",
    bot_name: str = "Persona",
    at_names: dict[str, str] | None = None,
    suppress_reply: bool = False,
) -> Parsed:
    """把一条消息的消息段列表解析成 (文本, 媒体清单)。深度 0，不含合并转发展开。"""
    parts: list[str] = []
    media: list[Media] = []
    has_reply = False

    for seg in message:
        seg_type = _type_of(seg)
        data = _data_of(seg)

        if seg_type == "text":
            parts.append(str(data.get("text", "")))

        elif seg_type == "at":
            qq = str(data.get("qq", ""))
            if qq == str(self_id):
                parts.append(f"@{bot_name}")
            elif qq == "all":
                parts.append("@全体成员")
            else:
                name = (at_names or {}).get(qq) or data.get("name") or qq
                parts.append(f"@{name}")

        elif seg_type == "reply":
            has_reply = True

        elif seg_type == "image":
            idx = len(media)
            # sub_type 1 = 动画表情（NapCat 常见）
            is_emoji = str(data.get("sub_type", "0")) == "1" or "动画表情" in str(data.get("summary", ""))
            summary = str(data.get("summary") or ("动画表情" if is_emoji else ""))
            media.append(
                Media(
                    kind="mface" if is_emoji else "image",
                    url=str(data.get("url") or ""),
                    file=str(data.get("file") or ""),
                    summary=summary,
                    size=_int(data.get("file_size")),
                )
            )
            parts.append(("〈动画表情" if is_emoji else "〈图片") + media[idx].anchor(idx) + "〉")

        elif seg_type == "mface":
            idx = len(media)
            media.append(
                Media(
                    kind="mface",
                    url=str(data.get("url") or ""),
                    file=str(data.get("file") or ""),
                    summary=str(data.get("summary") or ""),
                    title=str(data.get("summary") or ""),
                )
            )
            name = str(data.get("summary") or "表情包")
            parts.append(f"〈表情包:{name}" + media[idx].anchor(idx) + "〉")

        elif seg_type == "face":
            raw = data.get("raw") or {}
            text = raw.get("faceText") if isinstance(raw, dict) else None
            parts.append(f"[表情{text}]" if text else "[表情]")

        elif seg_type == "record":
            idx = len(media)
            media.append(Media(kind="record", url=str(data.get("url") or ""), file=str(data.get("file") or "")))
            parts.append("〈语音" + media[idx].anchor(idx) + "〉")

        elif seg_type == "video":
            idx = len(media)
            media.append(
                Media(kind="video", url=str(data.get("url") or ""), file=str(data.get("file") or ""), size=_int(data.get("file_size")))
            )
            parts.append("〈视频" + media[idx].anchor(idx) + "〉")

        elif seg_type == "file":
            name = str(data.get("name") or data.get("file") or "文件")
            idx = len(media)
            media.append(
                Media(kind="file", url=str(data.get("url") or ""), file=str(data.get("file") or ""), title=name, size=_int(data.get("file_size")))
            )
            parts.append(f"〈文件:{name}" + media[idx].anchor(idx) + "〉")

        elif seg_type in ("json", "xml"):
            raw = str(data.get("data") or "")
            meta = _card_meta(raw)
            title = meta["title"] or _card_title(raw)
            url = meta["url"] or _card_url(raw)
            resid = _forward_resid(raw)          # 卡片形态的"聊天记录"（multimsg）
            idx = len(media)
            if resid:
                media.append(Media(kind="forward", forward_id=resid, title=title or "聊天记录"))
                parts.append(f"〈合并转发:{title or '聊天记录'}" + media[idx].anchor(idx) + "〉")
            else:
                summary = "｜".join(x for x in (meta["desc"], meta["app"]) if x)[:200]
                media.append(Media(kind="card", url=url, title=title, summary=summary))
                label = title or meta["kind"]
                extra = f" {meta['bvid']}" if meta["bvid"] else ""
                parts.append(f"〈{meta['kind']}:{label}{extra}" + media[idx].anchor(idx) + "〉")

        elif seg_type == "share":
            url = str(data.get("url") or "")
            title = str(data.get("title") or "")
            idx = len(media)
            media.append(Media(kind="link", url=url, title=title, summary=title))
            parts.append(f"〈链接:{title or url[:40]}" + media[idx].anchor(idx) + "〉")

        elif seg_type == "forward":
            fid = str(data.get("id") or "")
            content = data.get("content")
            if isinstance(content, str):
                try:
                    content = json.loads(content)
                except Exception:  # noqa: BLE001
                    content = None
            if not isinstance(content, list):
                content = []
            idx = len(media)
            media.append(Media(kind="forward", forward_id=fid, forward_content=content))
            parts.append("〈合并转发" + media[idx].anchor(idx) + "〉")

        elif seg_type == "location":
            parts.append(f"〈位置:{data.get('title') or data.get('content') or ''}〉")

        elif seg_type == "contact":
            parts.append(f"〈名片:{data.get('nickname') or data.get('id') or ''}〉")

        elif seg_type == "music":
            parts.append(f"〈音乐:{data.get('title') or ''}〉")

        elif seg_type == "dice":
            parts.append(f"〈骰子:{data.get('result') or '?'}点〉")
        elif seg_type == "rps":
            parts.append(f"〈猜拳:{data.get('result') or '?'}〉")
        elif seg_type == "shake":
            parts.append("〈窗口抖动〉")
        elif seg_type == "markdown":
            raw_md = str((data.get("content") or data.get("data") or "") if isinstance(data, dict) else "")
            parts.append(f"〈富文本:{raw_md[:60]}〉" if raw_md else "〈富文本〉")
        elif seg_type == "keyboard":
            parts.append("〈按钮消息〉")
        elif seg_type in ("redbag", "red_packet"):
            parts.append(f"〈红包:{data.get('title') or '恭喜发财'}〉")
        elif seg_type == "gift":
            parts.append(f"〈礼物:{data.get('name') or ''}〉")
        elif seg_type == "onlinefile":
            name = str(data.get("name") or data.get("file") or "在线文件")
            idx = len(media)
            media.append(Media(kind="file", url=str(data.get("url") or ""), file=str(data.get("file") or ""), title=name))
            parts.append(f"〈文件:{name}" + media[idx].anchor(idx) + "〉")

        else:
            parts.append(f"〈{seg_type}〉")

    body = "".join(parts).strip()
    if has_reply and not suppress_reply:
        body = f"↳[回复] {body}"
    if not body:
        body = "〈空消息〉"
    return Parsed(text=body, media=media)


def _int(value: Any) -> int:
    try:
        return int(value)
    except (TypeError, ValueError):
        return 0


# ── 合并转发：递归展开（需要 bot 调 get_forward_msg）──────────────────────────


def _forward_inner_text(parsed: Parsed, collected: list[Media] | None) -> str:
    """把转发节点里的局部锚点 {{Mk}} 换成全局媒体下标；collected=None 时只保留类型文字。

    注意：转发内部的媒体以前被直接删掉（只剩〈图片〉），导致她看不到图。
    现在把它们追加到 collected（由调用方交给 bus.record 入库），锚点改写成
    全局下标，bus.record 落库后就会变成真实的 `#media_id`。
    """
    if collected is None:
        return re.sub(r"\{\{M\d+\}\}", "", parsed.text)
    mapping: dict[int, str] = {}
    for idx, item in enumerate(parsed.media):
        if item.kind == "forward":
            # 嵌套转发由递归展开，它自己的锚点不要指向媒体池
            mapping[idx] = ""
            continue
        if len(collected) >= MAX_FORWARD_MEDIA:
            mapping[idx] = ""
            continue
        global_idx = len(collected)
        collected.append(item)
        mapping[idx] = "{{M%d}}" % global_idx
    return re.sub(r"\{\{M(\d+)\}\}", lambda m: mapping.get(int(m.group(1)), ""), parsed.text)


MAX_QUOTE_MEDIA = 12  # 被引用消息里最多接多少个媒体进池


def quote_preview(message: Any, collected: list[Media], *, bot_name: str = "Persona",
                  max_chars: int = 240, at_names: dict[str, str] | None = None) -> str:
    """把"被回复的那条消息"渲染成一段预览，并把里面的媒体接进全局媒体池。

    预览会保留所有段类型（图片/视频/语音/文件/卡片/表情/合并转发…）；
    媒体锚点改写成 collected 里的全局下标，bus.record 落库后会变成 #media_id。
    合并转发只留标记，不在这里递归展开（避免一条回复把整棵转发树拖进来）。
    """
    parsed = parse_segments(message, bot_name=bot_name, at_names=at_names, suppress_reply=True)
    mapping: dict[int, str] = {}
    added = 0  # 本条引用消息最多新增 MAX_QUOTE_MEDIA 个媒体
    for idx, item in enumerate(parsed.media):
        if item.kind == "forward" or added >= MAX_QUOTE_MEDIA:
            mapping[idx] = ""
            continue
        global_idx = len(collected)
        collected.append(item)
        mapping[idx] = "{{M%d}}" % global_idx
        added += 1
    text = re.sub(r"\{\{M(\d+)\}\}", lambda m: mapping.get(int(m.group(1)), ""), parsed.text)
    text = " ".join(text.split())
    return text[:max_chars]


async def expand_forward(bot: Any, media: Media, *, depth: int = 0,
                         collected: list[Media] | None = None) -> str:
    """把一条合并转发（forward）展开成可读文本；失败返回空串（不影响主流程）。

    递归：转发里还套转发 → 再展开一层（最多 MAX_FORWARD_DEPTH 层）。
    collected：可选的媒体收集器；转发内部的图片/视频/文件会被追加进去，
    文本里的锚点改写成它们在 collected 里的全局下标，调用方随后交给 bus.record 入库。
    """
    if media.kind != "forward" or depth > MAX_FORWARD_DEPTH:
        return ""
    # 优先用消息段里内嵌的 content（多层转发的正确来源）；没有才回退 get_forward_msg。
    nodes = list(getattr(media, "forward_content", None) or [])
    if not nodes:
        if not media.forward_id:
            return "（转发内容取不到）"
        try:
            payload = await bot.call_api("get_forward_msg", id=media.forward_id)
        except Exception as exc:  # noqa: BLE001
            logger.debug(f"[perception] 合并转发展开失败 id={media.forward_id}: {exc}")
            return "（转发内容取不到）"
        nodes = (payload or {}).get("messages") or (payload or {}).get("message") or []
        if isinstance(nodes, dict):
            nodes = [nodes]
    lines: list[str] = []
    for node in list(nodes)[:MAX_FORWARD_NODES]:
        sender = (node.get("sender") or {}) if isinstance(node, dict) else {}
        name = sender.get("card") or sender.get("nickname") or "某人"
        content = node.get("message") or node.get("content") or ""
        parsed = parse_segments(content if isinstance(content, list) else [], bot_name="Persona")
        inner_text = _forward_inner_text(parsed, collected)[:MAX_FORWARD_CHARS]
        pad = "  " * (depth + 1)
        lines.append(f"{pad}↳[{name}]：{inner_text}")
        # 嵌套转发：先输出本条，再把里面套的转发展开在后面（缩进更深），层级一眼能看出来
        for inner in parsed.media:
            if inner.kind == "forward":
                more = await expand_forward(bot, inner, depth=depth + 1, collected=collected)
                if more:
                    lines.append(more)
    if len(nodes) > MAX_FORWARD_NODES:
        lines.append(f"  ↳…（还有 {len(nodes) - MAX_FORWARD_NODES} 条未展开）")
    return "\n".join(lines)
