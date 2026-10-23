r"""B 站链接解析（感知层）——把群里的 B 站分享变成她看得懂的信息

覆盖类型（2026-10-20 晚扩展）：视频（BV/av）、专栏（read/cv）、动态（opus/t.bilibili）、
个人主页（space/<mid>）、番剧、直播；b23.tv 短链先 302 再判型。类型识别与具体取数
在 browser.bili_resolve（登录后走官方 API，未登录走页面 SSR/正文兜底）。

数据来源有两条（2026-10-20 晚补充第二条，解决“PC 关机就解析不了”）：
    1. **PC 代理解析**（首选，字段最全、最快）
       服务器在香港，`api.bilibili.com` 直接 -412（风控）；国内 PC 上跑 `bili_proxy.py`
       （127.0.0.1:13120），服务器通过 SSH 反向隧道访问同一个地址。
    2. **服务器浏览器兜底**（PC 离线/隧道断了时）
       视频页本身能打开，`browser.bili_info` 从页面 `__INITIAL_STATE__` /
       og:meta / 正文里抠标题、UP、简介、播放量。慢（几秒）但不需要 PC 在线。

在 prompt 里的形态：
    【这批里的 B 站链接】
      · 「大Persona成精了：它按自己作息刷B站」— UP 花离渐至见条鱼 · 20:48 · 8.3 万播放 · 1077 评论
        简介：我给 DSH 养了一条会自己刷 B 站的鱼…

不阻塞主流程：两条路都取不到就整段不出现（卡片标题仍然会保留在消息文本里）。
"""

from __future__ import annotations

import asyncio
import os
import re
import time
from typing import Any

import httpx
from nonebot import logger

from .settings import SETTINGS

PROXY = SETTINGS.bili_proxy  # docker 网关 → 宿主 relay → SSH 隧道 → PC
CACHE_TTL = int(os.getenv("FISHTANK_BILI_CACHE_TTL_S", "1800"))
# 浏览器兜底很慢（打开视频页 ~10-20s）。决策流程在前面等这一段，必须设上限，
# 否则一批 4 个链接会把她的回复拖到 1 分钟后。见 brief() 的总预算：
#   · 单个链接兜底最多 20s（BROWSER_TIMEOUT）；
#   · brief 总预算 20s：视频走代理时 <1s/个；专栏/动态/主页走浏览器约 3-5s/个，
#     4 个链接通常 12-18s；某个慢请求最多 20s，后面的链接直接跳过。
BROWSER_TIMEOUT = float(os.getenv("FISHTANK_BILI_BROWSER_TIMEOUT_S", "20"))
BRIEF_BUDGET = float(os.getenv("FISHTANK_BILI_BRIEF_BUDGET_S", "20"))
_cache: dict[str, tuple[float, dict]] = {}
_health_at = 0.0
_health_ok = False

# B 站各类链接：短链 / 视频 / 番剧 / 专栏 / 动态(opus) / 个人主页 / 直播
_URL_RE = re.compile(
    r"https?://(?:"
    r"b23\.tv/[\w-]+"
    r"|(?:www\.|m\.)?bilibili\.com/(?:video|bangumi|read|opus|dynamic|list|space)/[^\s，。、）)]+"
    r"|t\.bilibili\.com/\d+"
    r"|space\.bilibili\.com/\d+"
    r"|live\.bilibili\.com/\d+"
    r")",
    re.I,
)


def urls_in(text: str) -> list[str]:
    r"""从一段文本里抠出 B 站链接（去重保序）。

    注意：QQ 卡片的 JSON 里斜杠是转义的（`https:\/\/b23.tv\/xxx`），
    不先还原就匹配不到卡片里的链接。
    """
    text = (text or "").replace("\\/", "/")
    out: list[str] = []
    for m in _URL_RE.finditer(text):
        u = m.group(0).rstrip(".,;:!?")
        if u not in out:
            out.append(u)
    return out


async def _healthy() -> bool:
    global _health_at, _health_ok
    if time.monotonic() - _health_at < 60:
        return _health_ok
    _health_at = time.monotonic()
    try:
        async with httpx.AsyncClient(timeout=6.0) as client:
            resp = await client.get(f"{PROXY}/health")
            _health_ok = resp.status_code == 200 and bool(resp.json().get("ok"))
    except Exception:  # noqa: BLE001
        _health_ok = False
    return _health_ok


def _fmt_duration(sec: Any) -> str:
    try:
        sec = int(sec)
    except (TypeError, ValueError):
        return ""
    return f"{sec // 60}:{sec % 60:02d}"


def _fmt_count(n: Any) -> str:
    try:
        n = int(n)
    except (TypeError, ValueError):
        return ""
    if n >= 10000:
        return f"{n / 10000:.1f} 万"
    return str(n)


def format_text(d: dict[str, Any]) -> str:
    """把 resolve() 的结果排成给人/给她看的多行摘要（工具返回用）。

    resolve() 已经在 item["text"] 里放好按类型排版的文本（视频/专栏/动态/主页），
    这里优先用它；没有 text 的（比如代理返回的旧结构）走兜底。
    """
    text = str(d.get("text") or "").strip()
    if text:
        return text
    title = str(d.get("title") or "").strip()
    parts = [f"《{title}》" if title else "（没有标题）"]
    meta: list[str] = []
    up = str(d.get("up") or "")
    if up:
        meta.append(f"UP {up}")
    dur = _fmt_duration(d.get("duration"))
    if dur:
        meta.append(dur)
    for key, label in (("view", "播放"), ("danmaku", "弹幕"), ("reply", "评论"),
                       ("like", "点赞"), ("coin", "投币"), ("favorite", "收藏")):
        count = _fmt_count(d.get(key))
        if count:
            meta.append(f"{label} {count}")
    if meta:
        parts.append("— " + " · ".join(meta))
    desc = str(d.get("desc") or "").strip()
    if desc:
        parts.append("简介：" + desc[:300].replace("\n", " "))
    if d.get("bvid"):
        parts.append("BV：" + str(d["bvid"]))
    if d.get("url"):
        parts.append("链接：" + str(d["url"]))
    return "\n".join(parts)


def _video_item_from_proxy(d: dict[str, Any]) -> dict[str, Any]:
    """PC 代理 /info 的返回 → 与浏览器解析一致的统一结构。"""
    bvid = str(d.get("bvid") or "")
    return {
        "kind": "video",
        "bvid": bvid,
        "aid": d.get("aid"),
        "cid": d.get("cid"),
        "title": d.get("title"),
        "up": d.get("up"),
        "up_mid": d.get("up_mid"),
        "duration": d.get("duration"),
        "pubdate": d.get("pubdate"),
        "desc": d.get("desc"),
        "tname": d.get("tname"),
        "view": d.get("view"),
        "danmaku": d.get("danmaku"),
        "reply": d.get("reply"),
        "like": d.get("like"),
        "coin": d.get("coin"),
        "favorite": d.get("favorite"),
        "url": f"https://www.bilibili.com/video/{bvid}" if bvid else "",
        "source": "proxy",
    }


async def _via_proxy(target: str) -> dict[str, Any] | None:
    """走 PC 代理取数（首选）。代理不可用/返回失败时返回 None，交给浏览器兜底。"""
    if not await _healthy():
        return None
    try:
        param = f"bv={target}" if re.fullmatch(r"BV[0-9A-Za-z]{10}", target) else f"url={target}"
        async with httpx.AsyncClient(timeout=20.0) as client:
            resp = await client.get(f"{PROXY}/info?{param}")
        data = resp.json()
        if not data.get("ok"):
            logger.debug(f"[perception] B站代理取数失败 {target}: {data.get('error')}")
            return None
        payload = data.get("data") or {}
        if not payload.get("bvid") and not payload.get("title"):
            return None
        return payload
    except Exception as exc:  # noqa: BLE001
        logger.debug(f"[perception] B站代理取数异常 {target}: {exc}")
        return None


async def _via_browser(target: str) -> dict[str, Any] | None:
    """走常驻 Chromium 解析（视频/专栏/动态/个人主页都覆盖），带硬超时。"""
    try:
        from . import browser  # 延迟导入：bili 也被独立脚本/自测用到，避免无谓依赖 playwright

        return await asyncio.wait_for(browser.bili_resolve(target), timeout=BROWSER_TIMEOUT)
    except asyncio.TimeoutError:
        logger.warning(f"[perception] B站浏览器解析超时（>{BROWSER_TIMEOUT:.0f}s）：{target}")
        return None
    except Exception as exc:  # noqa: BLE001
        logger.debug(f"[perception] B站浏览器解析异常 {target}: {exc}")
        return None


async def resolve(target: str) -> dict[str, Any] | None:
    """解析一个 B 站链接/BV/av/主页/专栏/动态，带 30 分钟缓存。

    视频优先走 PC 代理（快），其余类型/代理失败走服务器浏览器；
    浏览器侧登录后走官方 API，未登录自动退回页面 SSR。
    """
    target = (target or "").strip()
    if not target:
        return None
    hit = _cache.get(target)
    if hit and time.time() - hit[0] < CACHE_TTL:
        return hit[1]
    item: dict[str, Any] | None = None
    try:
        from . import browser

        kind = browser.bili_kind_of(target)
    except Exception:  # noqa: BLE001 - 没装 playwright 等环境时仍允许代理路径
        kind = "video" if re.search(r"BV[0-9A-Za-z]{10}|/video/", target) else ""
    if kind == "video":
        payload = await _via_proxy(target)
        if payload:
            item = _video_item_from_proxy(payload)
    if item is None:
        item = await _via_browser(target)
    if item:
        if not item.get("text"):
            try:
                from . import browser

                item["text"] = browser.format_bili_item(item)
            except Exception:  # noqa: BLE001
                item["text"] = format_text(item)
        _cache[target] = (time.time(), item)
    return item


async def info(target: str) -> dict[str, Any] | None:
    """兼容旧名：等价 resolve()。"""
    return await resolve(target)


async def brief(targets: list[str]) -> str:
    """把若干链接渲染成给 prompt 用的一段文本；一个都取不到就返回空串。

    时间预算：PC 代理在线时视频 <1s；其余类型走浏览器（单次最多 BROWSER_TIMEOUT）。
    每次进入下一个链接前检查总预算 BRIEF_BUDGET —— 超了就只渲染已经拿到的部分，
    不能让她的回复无限拖后（视频有代理时很快；纯浏览器解析最坏 ≈ 20s/个）。
    """
    lines: list[str] = []
    seen: set[str] = set()
    started = time.monotonic()
    for target in targets[:4]:
        if time.monotonic() - started > BRIEF_BUDGET:
            logger.info(f"[perception] B站解析预算用尽（{BRIEF_BUDGET:.0f}s），剩余链接跳过")
            break
        d = await resolve(target)
        if not d:
            continue
        key = str(d.get("bvid") or d.get("cvid") or d.get("id") or d.get("mid") or target)
        if key in seen:
            continue
        seen.add(key)
        text = str(d.get("text") or "").strip()
        if not text:
            continue
        lines.append("  · " + text.replace("\n", "\n    "))
    if not lines:
        return ""
    return "【这批里的 B 站链接（平台已解析，不用再猜标题）】\n" + "\n".join(lines)
