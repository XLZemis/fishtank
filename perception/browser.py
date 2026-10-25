"""浏览器自动化（CDP 连接独立 chromium 容器）

架构（2026-10-20 定稿）：
    qqbot-chromium 容器（独立、常驻、共享 qqbot 网络命名空间）
        └─ Chrome --headless --remote-debugging-port=9222
           profile 落在 /data/qqbot/chrome-profile（磁盘，重启不丢）
    感知层（bot 容器）每次动作 connect_over_cdp("http://<容器IP>:9222")
        —— Chrome 校验 Host 头（只接受 IP/localhost），所以这里先解析容器 IP。

好处：重启 bot 不影响浏览器；登录态在磁盘 profile；页面操作与业务进程解耦。

2026-10-20 晚（本次）改动：
    1. QZone 点赞/评论按真实 DOM 重写（排障脚本 scripts/qzone-inspect.py 实测）：
       · 点赞按钮是 `a.qz_like_btn_v3[data-islike]`；data-islike="1" 表示已赞，
         再点会“取消赞”，所以必须先判重；
       · 评论不是按 Enter 提交：点 `a[data-cmd=qz_reply]` 展开后，编辑器是
         `li.f-single` 内新出现的 `div[contenteditable=true].c_tx2`，
         提交按钮是同一条说说里的 `a.btn-post`（文字「发表」）；
       · 编辑器和「发表」在同一个 `li.f-single` 里，所以全部操作都限定在第一条说说内，
         避免误点到页面顶部“发说说”的编辑器。
    2. 新增 B 站视频页兜底取数（bili_info）：
       · 服务器在香港，api.bilibili.com 的 view 接口对本 IP 直接 -412，
         但 **视频页本身能打开**（SSR + window.__INITIAL_STATE__），
         所以从页面里抠标题/UP/简介/播放量/弹幕数，作为 PC 代理离线时的兜底。
"""

from __future__ import annotations

import asyncio
import datetime as _dt
import hashlib
import json
import os
import re
import subprocess
import tempfile
import time
from pathlib import Path
from typing import Any
from urllib.parse import urlsplit

import httpx

from nonebot import logger

from . import netguard
from .settings import SETTINGS


# Chrome 会校验 Host 头（只接受 IP/localhost），所以用容器 IP 而不是主机名。
# 2026-10-25：CDP 改为 socat 边车（9223）后，配置里若写的是主机名，也要解析成 IP 再连，
# 否则 Chrome 收到 Host: qqbot-chromium:9223 会直接返回 500。
def _cdp_url() -> str:
    import socket
    from urllib.parse import urlsplit, urlunsplit

    configured = str(SETTINGS.cdp_url or "").strip()
    if configured:
        try:
            parts = urlsplit(configured)
            host = parts.hostname or ""
            ip = socket.gethostbyname(host) if host else ""
            if ip:
                port = f":{parts.port}" if parts.port else ""
                return urlunsplit((parts.scheme or "http", ip + port, parts.path or "", "", ""))
        except Exception:  # noqa: BLE001
            pass
        return configured

    for host in ("qqbot-chromium", "127.0.0.1"):
        try:
            ip = socket.gethostbyname(host)
            return f"http://{ip}:9222"
        except Exception:  # noqa: BLE001
            continue
    return "http://127.0.0.1:9222"


# 仅用于日志/排障；真正连接时每次重新解析（容器重建后 IP 会变）
CDP_URL = _cdp_url()
QZONE = SETTINGS.qzone_base
UIN = SETTINGS.bot_qq

_QZONE_MARKERS = ("的QQ空间", "说点儿什么吧", "今日访客", "好友动态", "写说说")
_BILI_URL_RE = re.compile(
    r"https?://(?:b23\.tv|(?:www\.|m\.|t\.|space\.|live\.)?bilibili\.com)/[^\s\"'<>]+",
    re.I,
)
_BV_RE = re.compile(r"(BV[0-9A-Za-z]{10})")
_AV_RE = re.compile(r"\bav(\d+)\b", re.I)
_CV_RE = re.compile(r"/read/(?:mobile/)?cv(\d+)", re.I)
_CV_QUERY_RE = re.compile(r"/read/(?:mobile|article)[^#]*?[?&]id=(\d+)", re.I)
_OPUS_RE = re.compile(r"(?:bilibili\.com/opus/|t\.bilibili\.com/|bilibili\.com/dynamic/)(\d+)", re.I)
_MID_RE = re.compile(r"space\.bilibili\.com/(\d+)")
_BANGUMI_RE = re.compile(r"bilibili\.com/bangumi/(?:play|media)/(?:ep|ss)?(\d+)", re.I)
_LIVE_RE = re.compile(r"live\.bilibili\.com/(\d+)")

_pw = None


async def _connect():
    """连到常驻 Chromium，返回 (playwright, browser, context)。"""
    global _pw
    from playwright.async_api import async_playwright

    if _pw is None:
        _pw = await async_playwright().start()
    browser = await _pw.chromium.connect_over_cdp(_cdp_url())
    ctx = browser.contexts[0] if browser.contexts else await browser.new_context()
    return _pw, browser, ctx


# ── 出站守卫（2026-10-25 安全复核）────────────────────────────────────────────
# 通用网页抓取默认不携带登录态：隔离 context + 路由守卫，任何内网/回环/链路本地
# 地址（包括重定向后、子资源）一律 abort。只有白名单登录域才复用常驻 context。
_LOGIN_HOSTS = tuple(SETTINGS.browser_login_hosts) or ("user.qzone.qq.com", "qzone.qq.com", "bilibili.com", "b23.tv")


def _host_of(url: str) -> str:
    """取 URL 主机名（小写、去掉末尾点）；解析失败返回空串。"""
    try:
        return (urlsplit(str(url or "")).hostname or "").lower().rstrip(".")
    except ValueError:
        return ""


def _is_login_url(url: str) -> bool:
    """是否属于需要登录态的域（QZone/B 站）。"""
    host = _host_of(url)
    return bool(host) and any(host == h or host.endswith("." + h) for h in _LOGIN_HOSTS)


def _verify_url(url: str) -> netguard.Decision:
    """白名单登录域 + 公网 http(s)，端口只放 80/443。"""
    return netguard.check_url(url, allow_hosts=_LOGIN_HOSTS, ports=netguard.DEFAULT_PORTS)


async def _guard_route(route: Any) -> None:
    """路由守卫：页面导航/子资源/重定向的每一跳都过一遍地址策略。"""
    req_url = str(route.request.url or "")
    if req_url.startswith(("data:", "blob:", "about:")):
        return await route.continue_()
    decision = netguard.check_url(req_url, ports=None)
    if decision.ok:
        return await route.continue_()
    logger.warning(f"[perception] 浏览器拦截出站请求：{req_url[:120]}（{decision.reason}）")
    return await route.abort()


async def _open_page(url: str) -> tuple[Any, Any, bool]:
    """打开页面，返回 (context, page, 是否隔离 context)。

    登录域 → 常驻 context（有 QZone/B 站 cookie）；其余 → 新建无存储 context
    并挂路由守卫，避免任意网页碰到内网或浏览器里的登录态。
    """
    _pw, browser, persistent = await _connect()
    if _is_login_url(url):
        page = await persistent.new_page()
        # 登录域也挂逐跳守卫：域名本身可信，但页面里的重定向/子资源仍可能指向内网。
        await page.route("**/*", _guard_route)
        return persistent, page, False
    ctx = await browser.new_context()
    await ctx.route("**/*", _guard_route)
    return ctx, await ctx.new_page(), True


async def _close_page(page: Any, ctx: Any, isolated: bool) -> None:
    """关页面；隔离 context 顺手销毁（cookie/缓存不落盘）。"""
    if page is not None:
        try:
            await page.close()
        except Exception:  # noqa: BLE001
            pass
    if isolated and ctx is not None:
        try:
            await ctx.close()
        except Exception:  # noqa: BLE001
            pass


async def screenshot(
    url: str,
    *,
    full_page: bool = False,
    width: int = 1280,
    height: int = 900,
    wait_ms: int = 2500,
) -> dict[str, Any]:
    """用常驻 Chromium 打开网页并截图，写到 exports 供她发邮件/发文件。

    安全：先过 netguard（公网 http(s)、80/443）；通用页面走隔离 context；
    跳转后的最终 URL 再校验一次。返回 {ok, path, view_path, url, bytes, full_page}。
    """
    url = str(url or "").strip()
    decision = _verify_url(url)
    if not decision.ok:
        logger.warning(f"[perception] 截图拒绝：{url[:120]}（{decision.reason}）")
        return {"ok": False, "error": f"url 被拒绝：{decision.reason}"}
    width = max(320, min(int(width or 1280), 3840))
    height = max(240, min(int(height or 900), 2160))
    wait_ms = max(0, min(int(wait_ms or 2500), 15000))
    page = None
    ctx = None
    isolated = False
    try:
        ctx, page, isolated = await _open_page(url)
        try:
            await page.set_viewport_size({"width": width, "height": height})
            await page.bring_to_front()
        except Exception:  # noqa: BLE001
            pass
        await page.goto(url, timeout=45000, wait_until="domcontentloaded")
        final = _verify_url(page.url)
        if not final.ok:
            return {"ok": False, "error": f"跳转后的地址被拒绝：{final.reason}"}
        await page.wait_for_timeout(wait_ms)
        png = await page.screenshot(full_page=bool(full_page), timeout=30000)
        if not png:
            return {"ok": False, "error": "截图内容为空"}
        digest = hashlib.sha1(f"{url}|{full_page}|{time.time()}".encode("utf-8", "ignore")).hexdigest()[:12]
        name = f"shot_{digest}.png"
        out = SETTINGS.exports_dir / name
        out.parent.mkdir(parents=True, exist_ok=True)
        out.write_bytes(png)
        try:
            out.chmod(0o644)
        except OSError:
            pass
        return {
            "ok": True,
            "path": str(out),
            "view_path": f"{SETTINGS.persona_out_prefix}/{name}",
            "url": url,
            "bytes": len(png),
            "full_page": bool(full_page),
        }
    except Exception as exc:  # noqa: BLE001
        logger.warning(f"[perception] 网页截图失败 {url}: {exc}")
        return {"ok": False, "error": f"{type(exc).__name__}: {exc}"}
    finally:
        await _close_page(page, ctx, isolated)


async def _frames_text(page) -> str:
    out = []
    for frame in page.frames:
        try:
            out.append(await frame.inner_text("body"))
        except Exception:  # noqa: BLE001
            continue
    return "\n".join(out)


async def _qzone_logged_in(page) -> bool:
    text = await _frames_text(page)
    return any(m in text for m in _QZONE_MARKERS)


# ── 通用小工具：可见性 / 点击 / 找第一条说说 ────────────────────────────────
async def _visible(el) -> bool:
    """Playwright 的 is_visible() 对 QZone 的 hidden 元素判定偶尔与 offsetWidth 不一致，
    这里直接用 DOM 的布局盒判断，和排障脚本保持同一口径。"""
    try:
        return bool(await el.evaluate("el => !!(el.offsetWidth || el.offsetHeight || el.getClientRects().length)"))
    except Exception:  # noqa: BLE001
        return False


async def _click(el) -> bool:
    """正常点击优先；元素被浮层挡住/被判定不可见时退回 JS click（QZone 常见）。"""
    try:
        if await _visible(el):
            await el.click(timeout=5000)
            return True
    except Exception:  # noqa: BLE001
        pass
    try:
        await el.evaluate("el => el.click()")
        return True
    except Exception:  # noqa: BLE001
        return False


async def _first_feed(page, timeout_s: int = 14):
    """等第一条说说出现，返回 (frame, li.f-single)；找不到返回 (None, None)。

    说说列表在页面主 frame 里（实测），但保留遍历所有 frame 的写法，
    防止 QZone 以后又换回 g_iframe 结构（2026-10-17 那次就是在 iframe 里）。
    """
    deadline = asyncio.get_event_loop().time() + timeout_s
    while asyncio.get_event_loop().time() < deadline:
        for frame in page.frames:
            try:
                item = await frame.query_selector("li.f-single")
            except Exception:  # noqa: BLE001
                item = None
            if item is not None:
                return frame, item
        await asyncio.sleep(2)
    return None, None


async def _submit_of(editor):
    """从编辑器往上找到它所在 `.qz-poster` 的提交按钮（a.btn-post）。

    QZone 页面上同时存在两个编辑器（顶部“说点儿什么吧”和评论框），
    它们各有一个「发表」按钮；必须按祖先关系找最近的那个，否则会提交错对象。
    """
    try:
        btn = await editor.evaluate_handle(
            """el => {
                let p = el;
                for (let i = 0; i < 10 && p; i++) {
                    p = p.parentElement;
                    if (!p) return null;
                    const b = p.querySelector('a.btn-post');
                    if (b) return b;
                }
                return null;
            }"""
        )
        el = btn.as_element() if btn else None
        if el is not None:
            return el
    except Exception:  # noqa: BLE001
        pass
    # 兜底：页面级可见的「发表」（只有编辑器展开时才存在）
    return None


async def open_text(url: str, wait_ms: int = 4000) -> dict[str, Any]:
    """打开网页 → 返回标题 + 正文（已尽量压缩，避免把整页 DOM 塞给模型）。

    B 站视频页特殊处理：页面正文里有一半是推荐位，直接给她看会淹没重点；
    改为走 bili_info 的结构化摘要（标题/UP/简介/数据）。取不到再退回整页正文。
    """
    url = str(url or "").strip()
    decision = _verify_url(url)
    if not decision.ok:
        logger.warning(f"[perception] 读网页拒绝：{url[:120]}（{decision.reason}）")
        return {"ok": False, "error": f"url 被拒绝：{decision.reason}"}
    if _is_bili_target(url):
        info = await bili_resolve(url)
        if info:
            text = str(info.get("text") or format_bili_item(info))
            return {
                "ok": True,
                "title": str(info.get("title") or ""),
                "url": str(info.get("url") or url),
                "text": text,
                "chars": len(text),
                "bili": info,
            }
    page = None
    ctx = None
    isolated = False
    try:
        ctx, page, isolated = await _open_page(url)
        try:
            await page.goto(url, timeout=45000, wait_until="domcontentloaded")
            final = _verify_url(page.url)
            if not final.ok:
                return {"ok": False, "error": f"跳转后的地址被拒绝：{final.reason}"}
            await page.wait_for_timeout(wait_ms)
            title = await page.title()
            text = await _frames_text(page)
            # 压缩：去掉大量空行，截断
            lines = [ln.strip() for ln in text.splitlines() if ln.strip()]
            body = "\n".join(lines)
            return {"ok": True, "title": title, "url": page.url, "text": body[:6000], "chars": len(body)}
        finally:
            await _close_page(page, ctx, isolated)
    except Exception as exc:  # noqa: BLE001
        logger.warning(f"[perception] 浏览器打开失败 {url}: {exc}")
        return {"ok": False, "error": f"{type(exc).__name__}: {exc}"}


# ── QZone ───────────────────────────────────────────────────────────────────
async def qzone(action: str, *, text: str = "", limit: int = 10) -> dict[str, Any]:
    """QZone 动作：read / post / like / comment。登录态在常驻浏览器 profile 里。"""
    try:
        _pw_, browser, ctx = await _connect()
        page = await ctx.new_page()
        try:
            await page.goto(f"{QZONE}/{UIN}", timeout=60000, wait_until="domcontentloaded")
            await page.wait_for_timeout(5000)
            frame, feed = await _first_feed(page)
            if not await _qzone_logged_in(page):
                return {"ok": False, "error": "浏览器里的空间未登录（profile 可能失效，需要重新扫码）"}

            if action == "read":
                return await _qzone_read(page, limit)

            if action == "post":
                return await _qzone_post(page)

            if feed is None:
                return {"ok": False, "error": "没有可操作的说说（列表可能在 iframe 外或尚未加载）"}

            if action == "like":
                return await _qzone_like(page, feed)
            if action == "comment":
                return await _qzone_comment(page, feed, text)
            return {"ok": False, "error": f"未知动作 {action}"}
        finally:
            try:
                await page.close()
            except Exception:  # noqa: BLE001
                pass
    except Exception as exc:  # noqa: BLE001
        logger.warning(f"[perception] QZone 动作失败 {action}: {exc}")
        return {"ok": False, "error": f"{type(exc).__name__}: {exc}"}


async def _qzone_read(page, limit: int) -> dict[str, Any]:
    """读说说列表：遍历所有 frame 找 li.f-single，过滤导航/占位文案。"""
    junk = ("说点儿什么吧", "全部动态", "本地", "相册", "没有更多动态", "版权所有")
    out: list[str] = []
    cap = max(1, min(int(limit or 10), 20))
    for frame in page.frames:
        try:
            items = await frame.query_selector_all("li.f-single")
        except Exception:  # noqa: BLE001
            continue
        for it in items:
            try:
                t = (await it.inner_text()).strip()
            except Exception:  # noqa: BLE001
                continue
            if t and not any(j in t for j in junk) and len(t) > 10:
                out.append(t[:400])
            if len(out) >= cap:
                break
        if len(out) >= cap:
            break
    if not out:
        return {"ok": True, "mode": "cdp", "count": 0, "items": [],
                "note": "空间里还没有说说（页面显示：没有更多动态）"}
    return {"ok": True, "mode": "cdp", "count": len(out), "items": out}


async def _qzone_post(page) -> dict[str, Any]:
    """发说说：顶部编辑器是「占位符 → 点开 → 真编辑器」两段式（2026-10-20 实测）。

    DOM 事实：
        #qz_poster_v4_editor_container_1
          ├─ div.c_tx3[substitutor]   ← 空状态时显示「说点儿什么吧」（可见）
          ├─ div.c_tx2[content]       ← 真编辑器（点占位符后才可见）
          └─ ...
        .qz-poster-ft .op a.btn-post  ← 「发表」在 footer，不在编辑器容器内
    所以顺序必须是：点占位符 → 等 c_tx2 可见 → 输入 → 从编辑器往上找 a.btn-post。
    旧实现直接拿第一个可见 contenteditable，会拿到占位符，输入不会进真编辑器。
    """
    holder = None
    for sel in ("#qz_poster_v4_editor_container_1", ".qz-poster-editor-cont"):
        try:
            h = await page.query_selector(sel)
        except Exception:  # noqa: BLE001
            h = None
        if h is not None and await _visible(h):
            holder = h
            break
    if holder is None:
        return {"ok": False, "error": "找不到发布编辑器（说点儿什么吧）"}

    async def real_editor():
        """容器里可见的 c_tx2（真编辑器）；没有就返回 None。"""
        try:
            els = await holder.query_selector_all("div.c_tx2")
        except Exception:  # noqa: BLE001
            return None
        for el in els:
            if await _visible(el):
                return el
        return None

    box = await real_editor()
    if box is None:
        # 空状态：点占位符激活编辑器
        try:
            ph = await holder.query_selector("div.c_tx3")
        except Exception:  # noqa: BLE001
            ph = None
        if ph is None or not await _click(ph):
            return {"ok": False, "error": "点不动「说点儿什么吧」占位符"}
        await page.wait_for_timeout(1500)
        box = await real_editor()
    if box is None:
        return {"ok": False, "error": "编辑器没有激活（c_tx2 仍不可见）"}

    try:
        await box.evaluate("el => { el.focus(); el.click(); }")
    except Exception:  # noqa: BLE001
        pass
    await page.keyboard.type(text, delay=25)
    await page.wait_for_timeout(900)

    btn = await _submit_of(box)
    if btn is None:
        return {"ok": False, "error": "找不到「发表」按钮"}
    if not await _click(btn):
        return {"ok": False, "error": "点不动「发表」按钮"}
    # 发表成功时编辑器会被清空；等最多 6 秒
    for _ in range(12):
        await asyncio.sleep(0.5)
        try:
            left = (await box.inner_text()).strip()
        except Exception:  # noqa: BLE001
            left = ""
        if not left:
            return {"ok": True, "mode": "cdp", "submitted": True}
    return {"ok": True, "mode": "cdp", "submitted": True, "note": "已点发表，编辑器未清空（可能仍在提交）"}


async def _qzone_like(page, feed) -> dict[str, Any]:
    """点赞：按 data-islike 判重，已赞不点（否则会取消赞）。"""
    b = await feed.query_selector("a.qz_like_btn_v3, a[data-islike]")
    if b is None:
        return {"ok": False, "error": "找不到点赞按钮（a.qz_like_btn_v3）"}
    before = (await b.get_attribute("data-islike")) or ""
    if before == "1":
        return {"ok": True, "mode": "cdp", "liked": True, "already": True}
    if not await _click(b):
        return {"ok": False, "error": "点赞按钮点不动（可能被浮层遮挡）"}
    await page.wait_for_timeout(1500)
    after = (await b.get_attribute("data-islike")) or ""
    cls = (await b.get_attribute("class")) or ""
    liked = after == "1" or "item-on" in cls
    return {"ok": True, "mode": "cdp", "liked": liked, "before": before, "after": after}


async def _qzone_comment(page, feed, text: str) -> dict[str, Any]:
    """评论：展开评论框 → 输入 → 点同一条说说里的「发表」→ 回读验证。"""
    # 1) 点评论图标（a[data-cmd=qz_reply]），展开编辑器
    rep = await feed.query_selector("a[data-cmd='qz_reply'], a.qz_btn_reply")
    if rep is None:
        return {"ok": False, "error": "找不到评论入口（a[data-cmd=qz_reply]）"}
    if not await _click(rep):
        return {"ok": False, "error": "评论入口点不动"}
    await page.wait_for_timeout(1500)

    # 2) 找展开后的可见编辑器（li.f-single 内新出现的 c_tx2）
    editor = None
    for sel in ("div[contenteditable='true'].c_tx2", "div.qz-inputer div[contenteditable='true']", "div[contenteditable='true'][alt='replybtn']"):
        try:
            els = await feed.query_selector_all(sel)
        except Exception:  # noqa: BLE001
            els = []
        for el in els:
            if await _visible(el):
                editor = el
                break
        if editor is not None:
            break
    if editor is None:
        return {"ok": False, "error": "评论框没展开（找不到可见的 contenteditable）"}

    # 3) 输入（QZone 的 contenteditable 需要先 focus，键盘事件才会进编辑器）
    try:
        await editor.evaluate("el => { el.focus(); el.click(); }")
    except Exception:  # noqa: BLE001
        pass
    await page.keyboard.type(text, delay=25)
    await page.wait_for_timeout(600)
    typed = ""
    try:
        typed = (await editor.inner_text()).strip()
    except Exception:  # noqa: BLE001
        pass
    if text and text not in typed:
        return {"ok": False, "error": f"文字没进评论框（编辑器内容：{typed[:40]!r}）"}

    # 4) 提交：同一个 li.f-single 里的「发表」
    btn = await _submit_of(editor)
    if btn is None or not await _click(btn):
        return {"ok": False, "error": "找不到/点不动「发表」按钮"}
    await page.wait_for_timeout(2000)

    # 5) 回读：评论出现 = 真成功；编辑器清空但没回读 = 大概率成功（列表可能折叠）
    snippet = ""
    for _ in range(10):
        await asyncio.sleep(0.6)
        try:
            snippet = (await feed.inner_text()).strip()
        except Exception:  # noqa: BLE001
            snippet = ""
        if text and text in snippet:
            return {"ok": True, "mode": "cdp", "commented": True, "verified": True,
                    "snippet": snippet[-200:]}
    cleared = False
    try:
        cleared = not (await editor.inner_text()).strip()
    except Exception:  # noqa: BLE001
        pass
    return {"ok": True, "mode": "cdp", "commented": bool(cleared or snippet),
            "verified": False, "snippet": snippet[-200:]}


# ── B 站视频页兜底取数 ───────────────────────────────────────────────────────
_BILI_STATE_JS = r"""
() => {
  // 优先 __INITIAL_STATE__（新版视频页是 SSR + 内联状态），再退回 og:meta / 正文
  const s = window.__INITIAL_STATE__ || {};
  const v = s.videoData || s.videoInfo || s.upData || {};
  const og = (p) => {
    const n = document.querySelector(`meta[property="${p}"], meta[name="${p}"]`);
    return n ? (n.content || '') : '';
  };
  const txt = document.body ? document.body.innerText : '';
  const num = (re) => { const m = txt.match(re); return m ? parseInt(m[1], 10) : null; };
  const stat = s.stat || v.stat || null;
  return {
    url: location.href,
    page_title: document.title,
    og_title: og('og:title'),
    og_desc: og('og:description'),
    bvid: s.bvid || v.bvid || '',
    aid: s.aid || v.aid || null,
    cid: s.cid || v.cid || null,
    title: s.title || v.title || og('og:title').replace(/_哔哩哔哩_bilibili$/, ''),
    desc: String(s.desc || v.desc || og('og:description') || ''),
    owner: (s.owner && s.owner.name) || (v.owner && v.owner.name) || (s.upData && s.upData.name) || '',
    duration: v.duration || s.duration || null,
    pubdate: v.pubdate || s.pubdate || null,
    stat: stat ? { view: stat.view, danmaku: stat.danmaku, reply: stat.reply,
                   like: stat.like, coin: stat.coin, favorite: stat.favorite } : null,
    body_view: num(/播放量\s*([\d.]+万?)/),
    body_dm: num(/弹幕量\s*([\d.]+万?)/),
    body_reply: num(/([\d.]+万?)\s*条评论/),
    body_like: num(/点赞数\s*([\d.]+万?)/),
    body_coin: num(/投硬币枚数\s*([\d.]+万?)/),
    body_fav: num(/收藏人数\s*([\d.]+万?)/),
  };
}
"""


def _is_bili_target(target: str) -> bool:
    target = (target or "").strip()
    return bool(_BILI_URL_RE.match(target) or _BV_RE.fullmatch(target))


def _parse_count(value: Any) -> int | None:
    """把 '8.3万' / '9309' 这类计数转成 int；解析不了返回 None。"""
    if value is None:
        return None
    if isinstance(value, (int, float)):
        return int(value)
    text = str(value).strip()
    m = re.fullmatch(r"([\d.]+)\s*万?", text)
    if not m:
        return None
    try:
        n = float(m.group(1))
    except ValueError:
        return None
    return int(n * 10000) if "万" in text else int(n)


def _fmt_ts(ts: Any) -> str:
    """时间戳 → `YYYY-MM-DD HH:MM`；解析不了返回空串。"""
    try:
        return _dt.datetime.fromtimestamp(int(ts)).strftime("%Y-%m-%d %H:%M")
    except (TypeError, ValueError, OSError):
        return ""


def _as_int(value: Any) -> int | None:
    try:
        return int(value)
    except (TypeError, ValueError):
        return None


def _html_to_text(raw: str) -> str:
    """专栏 content 可能是 HTML：去标签、还原实体、压缩空白。"""
    import html as _html

    text = re.sub(r"<br\s*/?>", "\n", raw or "", flags=re.I)
    text = re.sub(r"<[^>]+>", "", text)
    text = _html.unescape(text)
    text = re.sub(r"[ \t\r\f\v]+", " ", text)
    text = re.sub(r"\n{3,}", "\n\n", text)
    return text.strip()


def bili_kind_of(text: str) -> str:
    """按链接/短链判断 B 站资源类型：video/article/dynamic/space/bangumi/live/short。"""
    text = (text or "").strip()
    if not text:
        return ""
    if "b23.tv" in text.lower():
        return "short"
    if _MID_RE.search(text):
        return "space"
    if _LIVE_RE.search(text):
        return "live"
    if _BANGUMI_RE.search(text):
        return "bangumi"
    if _CV_RE.search(text) or _CV_QUERY_RE.search(text) or "/read/" in text.lower():
        return "article"
    if _OPUS_RE.search(text):
        return "dynamic"
    if _BV_RE.search(text) or _AV_RE.search(text) or "/video/" in text.lower():
        return "video"
    return ""


def _is_bili_target(target: str) -> bool:
    """是不是 B 站资源（链接 / BV / av）。"""
    target = (target or "").strip()
    return bool(_BV_RE.fullmatch(target) or re.fullmatch(r"av\d+", target, re.I) or bili_kind_of(target))


async def _goto(page, url: str, wait_ms: int = 2500) -> bool:
    """打开页面（domcontentloaded + 固定等待）；失败返回 False。"""
    try:
        await page.goto(url, timeout=45000, wait_until="domcontentloaded")
    except Exception as exc:  # noqa: BLE001
        logger.debug(f"[perception] B站页面打开失败 {url}: {exc}")
        return False
    await page.wait_for_timeout(wait_ms)
    return True


async def _bili_api(page, path: str) -> dict[str, Any] | None:
    """在 bilibili 页面上下文里 fetch api.bilibili.com（继承登录 cookie），返回 JSON。

    登录后 view/nav 接口不再 -412；未登录时调用方需要自己退回页面 SSR。
    """
    url = path if path.startswith("http") else "https://api.bilibili.com" + path
    js = """
    async (url) => {
      try {
        const r = await fetch(url, {credentials: 'include'});
        const t = await r.text();
        return {status: r.status, text: t};
      } catch (e) { return {error: String(e)}; }
    }
    """
    try:
        got = await page.evaluate(js, url)
    except Exception as exc:  # noqa: BLE001
        logger.debug(f"[perception] B站 API 调用异常 {path}: {exc}")
        return None
    if not got or got.get("status") != 200:
        return None
    try:
        data = json.loads(got["text"])
    except Exception:  # noqa: BLE001
        return None
    return data if isinstance(data, dict) else None


async def _bili_video_from_page(page, url: str) -> dict[str, Any] | None:
    """视频页 SSR 兜底：未登录 / API 失败时，从 __INITIAL_STATE__ / og / 正文取数。"""
    data = None
    for _ in range(4):
        try:
            data = await page.evaluate(_BILI_STATE_JS)
            if data and (data.get("bvid") or data.get("title")):
                break
        except Exception:  # noqa: BLE001 - 前端跳转时执行上下文会被销毁，重试即可
            data = None
        await page.wait_for_timeout(2500)
    if not data:
        return None
    bv = str(data.get("bvid") or "")
    if not bv:
        m = _BV_RE.search(str(data.get("url") or ""))
        bv = m.group(1) if m else ""
    final_url = str(data.get("url") or url)
    title = str(data.get("title") or data.get("og_title") or "").strip()
    if not bv and not final_url.startswith(("https://www.bilibili.com/video", "https://m.bilibili.com/video")):
        logger.info(f"[perception] B站页面兜底取数失败（被重定向到 {final_url[:80]}）")
        return None
    if not title:
        return None
    stat = data.get("stat") or {}
    return {
        "kind": "video",
        "bvid": bv,
        "aid": data.get("aid"),
        "cid": data.get("cid"),
        "title": title,
        "up": str(data.get("owner") or "").strip(),
        "duration": data.get("duration"),
        "pubdate": data.get("pubdate"),
        "desc": str(data.get("desc") or "").strip().replace("\n", " "),
        "view": _parse_count(stat.get("view") if stat else None) or _parse_count(data.get("body_view")),
        "danmaku": _parse_count(stat.get("danmaku") if stat else None) or _parse_count(data.get("body_dm")),
        "reply": _parse_count(stat.get("reply") if stat else None) or _parse_count(data.get("body_reply")),
        "like": _parse_count(stat.get("like") if stat else None) or _parse_count(data.get("body_like")),
        "coin": _parse_count(stat.get("coin") if stat else None) or _parse_count(data.get("body_coin")),
        "favorite": _parse_count(stat.get("favorite") if stat else None) or _parse_count(data.get("body_fav")),
        "url": final_url,
        "source": "browser-ssr",
    }


async def _bili_video_resolve(page, url: str) -> dict[str, Any] | None:
    """视频：优先 view API（登录后可用），失败退回页面 SSR。"""
    bv = (_BV_RE.search(url) or [None])
    bvid = bv.group(1) if hasattr(bv, "group") else ""
    if not bvid:
        m = _AV_RE.search(url)
        if m:
            got = await _bili_api(page, f"/x/web-interface/view?aid={m.group(1)}")
        else:
            got = None
    else:
        got = await _bili_api(page, f"/x/web-interface/view?bvid={bvid}")
    if got and got.get("code") == 0 and isinstance(got.get("data"), dict):
        x = got["data"]
        stat = x.get("stat") or {}
        pages = [
            {
                "page": p.get("page"),
                "part": p.get("part"),
                "duration": p.get("duration"),
                "cid": p.get("cid"),
            }
            for p in (x.get("pages") or [])
            if isinstance(p, dict)
        ]
        owner = x.get("owner") or {}
        return {
            "kind": "video",
            "bvid": x.get("bvid") or bvid,
            "aid": x.get("aid"),
            "cid": x.get("cid"),
            "title": str(x.get("title") or ""),
            "up": str(owner.get("name") or ""),
            "up_mid": owner.get("mid"),
            "duration": x.get("duration"),
            "pubdate": x.get("pubdate"),
            "desc": str(x.get("desc") or "").strip(),
            "tname": x.get("tname") or "",
            "view": _as_int(stat.get("view")),
            "danmaku": _as_int(stat.get("danmaku")),
            "reply": _as_int(stat.get("reply")),
            "like": _as_int(stat.get("like")),
            "coin": _as_int(stat.get("coin")),
            "favorite": _as_int(stat.get("favorite")),
            "share": _as_int(stat.get("share")),
            "pages": pages,
            "url": f"https://www.bilibili.com/video/{x.get('bvid') or bvid}",
            "source": "api",
        }
    # API 失败（未登录/风控）→ 打开视频页走 SSR 兜底
    target_url = f"https://www.bilibili.com/video/{bvid}" if bvid else url
    await _goto(page, target_url, 2500)
    return await _bili_video_from_page(page, target_url)


async def _bili_article_resolve(page, url: str) -> dict[str, Any] | None:
    """专栏（cv 号）：x/article/view 里有正文、摘要、作者、发布时间、统计。"""
    m = _CV_RE.search(url) or _CV_QUERY_RE.search(url)
    cvid = m.group(1) if m else ""
    if not cvid:
        return await _bili_generic_resolve(page, url, "article")
    got = await _bili_api(page, f"/x/article/view?id={cvid}")
    if not (got and got.get("code") == 0 and isinstance(got.get("data"), dict)):
        await _goto(page, f"https://www.bilibili.com/read/cv{cvid}", 2500)
        return await _bili_generic_resolve(page, url, "article")
    x = got["data"]
    stats = x.get("stats") or {}
    author = x.get("author") or {}
    content = _html_to_text(str(x.get("content") or ""))
    return {
        "kind": "article",
        "cvid": _as_int(cvid),
        "title": str(x.get("title") or ""),
        "up": str(author.get("name") or ""),
        "up_mid": author.get("mid"),
        "pubdate": x.get("publish_time"),
        "desc": content or str(x.get("summary") or ""),
        "summary": str(x.get("summary") or ""),
        "words": _as_int(x.get("words")),
        "view": _as_int(stats.get("view")),
        "like": _as_int(stats.get("like")),
        "favorite": _as_int(stats.get("favorite")),
        "coin": _as_int(stats.get("coin")),
        "reply": _as_int(stats.get("reply")),
        "share": _as_int(stats.get("share")),
        "url": f"https://www.bilibili.com/read/cv{cvid}",
        "source": "api",
    }


async def _bili_dynamic_resolve(page, url: str) -> dict[str, Any] | None:
    """动态（opus / t.bilibili）：x/polymer/web-dynamic/v1/detail 结构化取数。"""
    m = _OPUS_RE.search(url) or _OPUS_RE.search(page.url if page else "")
    did = m.group(1) if m else ""
    if not did:
        return await _bili_generic_resolve(page, url, "dynamic")
    got = await _bili_api(page, f"/x/polymer/web-dynamic/v1/detail?id={did}")
    if not (got and got.get("code") == 0 and isinstance(got.get("data"), dict)):
        await _goto(page, f"https://t.bilibili.com/{did}", 2500)
        return await _bili_generic_resolve(page, url, "dynamic")
    item = (got["data"].get("item") or {})
    modules = item.get("modules") or {}
    author = modules.get("module_author") or {}
    dyn = modules.get("module_dynamic") or {}
    desc = dyn.get("desc") or {}
    text = str(desc.get("text") or "")
    major = dyn.get("major") or {}
    major_type = str(major.get("type") or "")
    major_title = ""
    for key in ("archive", "opus", "article", "ugc_season", "live_rcmd"):
        obj = major.get(key)
        if isinstance(obj, dict):
            major_title = str(obj.get("title") or major_title)
            if not major_title and isinstance(obj.get("summary"), dict):
                major_title = str((obj.get("summary") or {}).get("text") or "")
    stat = modules.get("module_stat") or {}
    def _cnt(key: str):
        val = stat.get(key)
        return _as_int(val.get("count")) if isinstance(val, dict) else None

    return {
        "kind": "dynamic",
        "id": did,
        "title": (text[:60] or major_title or f"{author.get('name') or ''}的动态"),
        "up": str(author.get("name") or ""),
        "up_mid": author.get("mid"),
        "pubdate": author.get("pub_ts"),
        "desc": text,
        "major_type": major_type,
        "major_title": major_title,
        "like": _cnt("like"),
        "reply": _cnt("comment"),
        "forward": _cnt("forward"),
        "url": f"https://t.bilibili.com/{did}",
        "source": "api",
    }


async def _bili_space_resolve(page, url: str) -> dict[str, Any] | None:
    """个人主页：x/web-interface/card 给昵称/签名/等级/粉丝/投稿数（不需要 wbi）。"""
    m = _MID_RE.search(url) or _MID_RE.search(page.url if page else "")
    mid = m.group(1) if m else ""
    if not mid:
        return await _bili_generic_resolve(page, url, "space")
    got = await _bili_api(page, f"/x/web-interface/card?mid={mid}")
    if not (got and got.get("code") == 0 and isinstance(got.get("data"), dict)):
        await _goto(page, f"https://space.bilibili.com/{mid}", 2500)
        return await _bili_generic_resolve(page, url, "space")
    data = got["data"]
    card = data.get("card") or {}
    level = (card.get("level_info") or {}).get("current_level")
    following = data.get("following")
    if following is None:
        following = card.get("friend")
    return {
        "kind": "space",
        "mid": _as_int(mid),
        "title": str(card.get("name") or ""),
        "up": str(card.get("name") or ""),
        "sign": str(card.get("sign") or ""),
        "level": _as_int(level),
        "fans": _as_int(data.get("follower")),
        "following": _as_int(following),
        "videos": _as_int(data.get("archive_count")),
        "articles": _as_int(data.get("article_count")),
        "likes": _as_int(data.get("like_num")),
        "url": f"https://space.bilibili.com/{mid}",
        "source": "api",
    }


async def _bili_generic_resolve(page, url: str, kind: str = "page") -> dict[str, Any] | None:
    """番剧/直播/未知页面：og:meta + 页面正文兜底。"""
    try:
        info = await page.evaluate(
            """() => {
              const og = (p) => { const n = document.querySelector(`meta[property="${p}"], meta[name="${p}"]`); return n ? (n.content || '') : ''; };
              return {title: document.title, og_title: og('og:title'), og_desc: og('og:description'),
                      body: document.body ? document.body.innerText.slice(0, 1200) : ''};
            }"""
        )
    except Exception:  # noqa: BLE001
        return None
    title = str(info.get("og_title") or info.get("title") or "").strip()
    desc = str(info.get("og_desc") or info.get("body") or "").strip()
    if not title and not desc:
        return None
    # 失效链接会被 B 站渲染成「出错啦!」错误页（例如不存在的直播间/视频）：
    # 这种页面没有价值，宁可返回 None，让调用方当作“没解析到”。
    if re.search(r"出错啦|页面不存在|视频不存在|404", title):
        logger.info(f"[perception] B站页面是错误页，放弃：{title[:40]}")
        return None
    return {
        "kind": kind or "page",
        "title": title,
        "desc": desc[:600],
        "url": url,
        "source": "browser-page",
    }


def format_bili_item(item: dict[str, Any]) -> str:
    """把统一解析结果排成她一眼能读的几行（视频/专栏/动态/主页/番剧…）。"""
    kind = str(item.get("kind") or "page")
    title = str(item.get("title") or "").strip()
    lines: list[str] = []

    if kind == "video":
        lines.append(f"《{title}》" if title else "（B站视频）")
        meta: list[str] = []
        if item.get("up"):
            meta.append(f"UP {item['up']}")
        dur = _as_int(item.get("duration")) or 0
        if dur:
            meta.append(f"{dur // 60}:{dur % 60:02d}")
        for key, label in (("view", "播放"), ("danmaku", "弹幕"), ("reply", "评论"),
                           ("like", "点赞"), ("coin", "投币"), ("favorite", "收藏")):
            if item.get(key):
                meta.append(f"{label} {item[key]}")
        if meta:
            lines.append("— " + " · ".join(meta))
        pages = item.get("pages") or []
        if len(pages) > 1:
            lines.append(f"共 {len(pages)} P：" + " / ".join(
                f"P{p.get('page')} {p.get('part') or ''}".strip() for p in pages[:5]
            ))
        desc = str(item.get("desc") or "").strip().replace("\n", " ")
        if desc:
            lines.append("简介：" + desc[:300])
        if item.get("bvid"):
            lines.append("BV：" + str(item["bvid"]))

    elif kind == "article":
        lines.append(f"《{title}》〔专栏〕" if title else "（B站专栏）")
        meta = []
        if item.get("up"):
            meta.append(f"作者 {item['up']}")
        when = _fmt_ts(item.get("pubdate"))
        if when:
            meta.append(when)
        for key, label in (("view", "阅读"), ("like", "点赞"), ("favorite", "收藏"),
                           ("coin", "投币"), ("reply", "评论")):
            if item.get(key):
                meta.append(f"{label} {item[key]}")
        if item.get("words"):
            meta.append(f"{item['words']} 字")
        if meta:
            lines.append("— " + " · ".join(meta))
        desc = str(item.get("desc") or "").strip()
        if desc:
            lines.append("正文：" + desc[:400].replace("\n", " "))

    elif kind == "dynamic":
        head = "〔动态〕"
        if item.get("up"):
            head += str(item["up"])
        when = _fmt_ts(item.get("pubdate"))
        if when:
            head += " · " + when
        lines.append(head)
        desc = str(item.get("desc") or "").strip()
        if desc:
            lines.append(desc[:400])
        major_title = str(item.get("major_title") or "").strip()
        if major_title and major_title not in desc:
            lines.append("涉及：" + major_title[:120])
        meta = []
        for key, label in (("like", "赞"), ("reply", "评论"), ("forward", "转发")):
            if item.get(key):
                meta.append(f"{label} {item[key]}")
        if meta:
            lines.append(" · ".join(meta))

    elif kind == "space":
        head = "〔UP主〕" + (str(item.get("up") or item.get("title") or "") or "（未知）")
        if item.get("level"):
            head += f"（Lv.{item['level']}）"
        lines.append(head)
        sign = str(item.get("sign") or "").strip()
        if sign:
            lines.append("签名：" + sign[:120])
        meta = []
        for key, label in (("fans", "粉丝"), ("following", "关注"), ("videos", "投稿视频"),
                           ("articles", "专栏"), ("likes", "获赞")):
            if item.get(key) is not None:
                meta.append(f"{label} {item[key]}")
        if meta:
            lines.append(" · ".join(meta))
        if item.get("mid"):
            lines.append(f"UID：{item['mid']}")

    else:
        lines.append(f"《{title}》" if title else "（B站页面）")
        desc = str(item.get("desc") or "").strip()
        if desc:
            lines.append(desc[:400].replace("\n", " "))

    if item.get("url"):
        lines.append("链接：" + str(item["url"]))
    return "\n".join(lines)


async def bili_resolve(target: str) -> dict[str, Any] | None:
    """统一解析 B 站资源：视频 / 专栏 / 动态 / 个人主页 / 番剧 / 直播。

    流程：先用页面 URL（或短链 302 后的最终 URL）判断类型，再在该页面上下文里
    调官方 API（登录后可用；未登录时 view 会 -412，自动退回页面 SSR）。
    """
    target = (target or "").strip()
    if not target:
        return None
    kind = bili_kind_of(target)
    if _BV_RE.fullmatch(target):
        url = f"https://www.bilibili.com/video/{target}"
    elif re.fullmatch(r"av\d+", target, re.I):
        url = f"https://www.bilibili.com/video/{target}"
    elif target.startswith("http"):
        url = target
    else:
        return None

    try:
        _pw_, browser, ctx = await _connect()
        page = await ctx.new_page()
        try:
            # 短链必须先打开目标页（302 决定类型）；其余类型先开首页拿 cookie（更快），
            # API 失败时各自的 resolve 再 goto 目标页走页面兜底。
            if kind in ("", "short"):
                await _goto(page, url, 3000)
                final = str(page.url or url)
                kind = bili_kind_of(final) or kind
            else:
                await _goto(page, "https://www.bilibili.com/", 1200)
                final = url
            if kind == "video":
                item = await _bili_video_resolve(page, final if "/video/" in final else url)
            elif kind == "article":
                # 专栏新版会 302 到 opus；cv 号要在原始/最终 URL 里找
                item = await _bili_article_resolve(page, url if _CV_RE.search(url) else final)
            elif kind == "dynamic":
                item = await _bili_dynamic_resolve(page, final)
            elif kind == "space":
                item = await _bili_space_resolve(page, final)
            else:
                await _goto(page, final, 2500)
                item = await _bili_generic_resolve(page, final, kind or "page")
            if item:
                item.setdefault("url", final)
                # 专栏被 302 到 opus 时，对外仍给原专栏链接
                if kind == "article" and _CV_RE.search(url):
                    item["url"] = re.sub(r"^http://", "https://", url.split("?")[0])
                item["text"] = format_bili_item(item)
            return item
        finally:
            try:
                await page.close()
            except Exception:  # noqa: BLE001
                pass
    except Exception as exc:  # noqa: BLE001
        logger.warning(f"[perception] B站解析失败 {target}: {exc}")
        return None


async def bili_info(target: str) -> dict[str, Any] | None:
    """兼容旧调用：等价于 bili_resolve（现在覆盖视频/专栏/动态/主页）。"""
    return await bili_resolve(target)


# ── B 站互动 / 动态发布 / 消息中心（2026-10-20 深夜）──────────────────────────
# 设计原则：
#   · 互动（点赞/收藏/评论）都带“回读校验”，不靠接口返回码瞎猜；
#   · 投币不可撤销 → 两段式：先返回硬币余额和计划枚数，确认后才真投；
#   · 动态发布走页面编辑器（create/dyn 需要 wbi/风控参数，页面点发布最稳），
#     发布成功后从 create/dyn 响应里拿 dyn_id；
#   · 删除动态用旧版 dynamic_svr 接口（新版 remove/dyn 已 404），仅测试清理用。

async def _bili_cookie(page, name: str) -> str:
    """读 bilibili.com 的 cookie（bili_jct 用于 csrf、DedeUserID 是 mid）。"""
    try:
        cookies = await page.context.cookies("https://www.bilibili.com")
    except Exception:  # noqa: BLE001
        return ""
    for cookie in cookies:
        if cookie.get("name") == name:
            return str(cookie.get("value") or "")
    return ""


async def _bili_post_form(page, url: str, form: dict[str, Any]) -> dict[str, Any] | None:
    """在页面上下文 POST 表单（继承 cookie），返回 JSON。"""
    body = "&".join(f"{k}={v}" for k, v in form.items())
    js = """
    async ({url, body}) => {
      try {
        const r = await fetch(url, {method: 'POST', credentials: 'include',
          headers: {'content-type': 'application/x-www-form-urlencoded'}, body});
        const t = await r.text();
        return {status: r.status, text: t};
      } catch (e) { return {error: String(e)}; }
    }
    """
    try:
        got = await page.evaluate(js, {"url": url, "body": body})
    except Exception as exc:  # noqa: BLE001
        logger.debug(f"[perception] B站 POST 异常 {url}: {exc}")
        return None
    if not got or got.get("status") != 200:
        return None
    try:
        data = json.loads(got["text"])
    except Exception:  # noqa: BLE001
        return None
    return data if isinstance(data, dict) else None


async def _bili_video_ids(page, target: str) -> dict[str, Any] | None:
    """把 BV/av/链接/短链统一解析成 {bvid, aid, title, cid}（给互动用）。"""
    target = (target or "").strip()
    if not target:
        return None
    m = _BV_RE.search(target)
    bvid = m.group(1) if m else ""
    aid = None
    av = _AV_RE.search(target)
    if not bvid and "b23.tv" in target.lower():
        # 短链先落地，拿最终 URL（这里不用 bili_resolve，避免递归开页）
        if not await _goto(page, target, 2500):
            return None
        m = _BV_RE.search(page.url or "")
        bvid = m.group(1) if m else ""
    if bvid:
        got = await _bili_api(page, f"/x/web-interface/view?bvid={bvid}")
        if got and got.get("code") == 0 and isinstance(got.get("data"), dict):
            x = got["data"]
            return {"bvid": x.get("bvid") or bvid, "aid": x.get("aid"), "cid": x.get("cid"),
                    "title": x.get("title") or "", "duration": x.get("duration")}
        # view 接口失败（个别视频地区/风控限制）→ 打开视频页从 SSR 状态拿 aid/cid
        if await _goto(page, f"https://www.bilibili.com/video/{bvid}", 3500):
            ssr = await _bili_video_from_page(page, f"https://www.bilibili.com/video/{bvid}")
            if ssr and ssr.get("cid"):
                return {"bvid": ssr.get("bvid") or bvid, "aid": ssr.get("aid"),
                        "cid": ssr.get("cid"), "title": ssr.get("title") or "",
                        "duration": ssr.get("duration")}
        return {"bvid": bvid, "aid": None, "cid": None, "title": ""}
    if av:
        got = await _bili_api(page, f"/x/web-interface/view?aid={av.group(1)}")
        if got and got.get("code") == 0 and isinstance(got.get("data"), dict):
            x = got["data"]
            return {"bvid": x.get("bvid") or "", "aid": x.get("aid"), "cid": x.get("cid"),
                    "title": x.get("title") or "", "duration": x.get("duration")}
    return None


async def _bili_relation(page, bvid: str) -> dict[str, Any]:
    """读视频与当前账号的关系：like / favorite / coin 等。"""
    got = await _bili_api(page, f"/x/web-interface/archive/relation?bvid={bvid}")
    data = (got or {}).get("data") if isinstance(got, dict) else None
    return data if isinstance(data, dict) else {}


async def bili_like(target: str, like: bool = True) -> dict[str, Any]:
    """点赞/取消赞。API 的取消赞会 -400，所以统一用视频页 `.video-like` 点击，
    每点一次都回读 relation，最多点三次（页面初始状态可能滞后，实测踩过）。"""
    try:
        _pw_, _browser, ctx = await _connect()
        page = await ctx.new_page()
        try:
            await _goto(page, "https://www.bilibili.com/", 1200)
            ids = await _bili_video_ids(page, target)
            if not ids or not ids.get("bvid"):
                return {"ok": False, "error": f"认不出视频：{target}"}
            bvid = str(ids["bvid"])
            rel = await _bili_relation(page, bvid)
            if bool(rel.get("like")) == bool(like):
                return {"ok": True, "liked": bool(like), "already": True, "bvid": bvid,
                        "title": ids.get("title") or ""}
            await _goto(page, f"https://www.bilibili.com/video/{bvid}", 3500)
            for _ in range(3):
                await page.evaluate("""() => { const n=document.querySelector('.video-like'); if (n) n.click(); }""")
                await page.wait_for_timeout(1600)
                rel = await _bili_relation(page, bvid)
                if bool(rel.get("like")) == bool(like):
                    logger.info(f"[perception] B站{'点赞' if like else '取消赞'}成功 {bvid}")
                    return {"ok": True, "liked": bool(like), "bvid": bvid, "title": ids.get("title") or ""}
            return {"ok": False, "liked": bool(rel.get("like")), "bvid": bvid,
                    "error": "点了三次仍未达到目标状态（页面结构可能变了）"}
        finally:
            try:
                await page.close()
            except Exception:  # noqa: BLE001
                pass
    except Exception as exc:  # noqa: BLE001
        logger.warning(f"[perception] B站点赞失败 {target}: {exc}")
        return {"ok": False, "error": f"{type(exc).__name__}: {exc}"}


async def bili_favorite(target: str, add: bool = True) -> dict[str, Any]:
    """收藏/取消收藏到默认收藏夹（API 可逆，实测 add/del 都 code=0 且 relation 跟着变）。"""
    try:
        _pw_, _browser, ctx = await _connect()
        page = await ctx.new_page()
        try:
            await _goto(page, "https://www.bilibili.com/", 1200)
            ids = await _bili_video_ids(page, target)
            if not ids or not ids.get("aid"):
                return {"ok": False, "error": f"认不出视频：{target}"}
            jct = await _bili_cookie(page, "bili_jct")
            mid = await _bili_cookie(page, "DedeUserID")
            folders = await _bili_api(page, f"/x/v3/fav/folder/created/list-all?up_mid={mid}")
            folder_list = ((folders or {}).get("data") or {}).get("list") or []
            folder = folder_list[0] if folder_list else None
            if not folder:
                return {"ok": False, "error": "没有可用的收藏夹"}
            fid = folder.get("id")
            form = {
                "rid": ids["aid"],
                "type": 2,
                "add_media_ids": fid if add else "",
                "del_media_ids": "" if add else fid,
                "csrf": jct,
            }
            got = await _bili_post_form(page, "https://api.bilibili.com/x/v3/fav/resource/deal", form)
            if not got or got.get("code") != 0:
                return {"ok": False, "error": (got or {}).get("message") or "收藏接口失败",
                        "code": (got or {}).get("code")}
            await page.wait_for_timeout(800)
            rel = await _bili_relation(page, str(ids["bvid"]))
            return {"ok": True, "favorited": bool(rel.get("favorite")), "bvid": ids.get("bvid"),
                    "title": ids.get("title") or "", "folder": folder.get("title"), "folder_id": fid}
        finally:
            try:
                await page.close()
            except Exception:  # noqa: BLE001
                pass
    except Exception as exc:  # noqa: BLE001
        logger.warning(f"[perception] B站收藏失败 {target}: {exc}")
        return {"ok": False, "error": f"{type(exc).__name__}: {exc}"}


async def bili_coin(target: str, count: int = 1, confirm: bool = False) -> dict[str, Any]:
    """投币（不可撤销）。两段式：
    第一次调用（confirm=False）只返回当前硬币余额和计划枚数，让她确认；
    她带 confirm=true 再调一次才真正投币。
    """
    try:
        count = max(1, min(int(count or 1), 2))  # B 站单个视频最多 2 枚
    except (TypeError, ValueError):
        count = 1
    try:
        _pw_, _browser, ctx = await _connect()
        page = await ctx.new_page()
        try:
            await _goto(page, "https://www.bilibili.com/", 1200)
            nav = await _bili_api(page, "/x/web-interface/nav")
            account = (nav or {}).get("data") or {}
            balance = account.get("money")
            if not account.get("isLogin"):
                return {"ok": False, "error": "B 站未登录"}
            ids = await _bili_video_ids(page, target)
            if not ids or not ids.get("bvid"):
                return {"ok": False, "error": f"认不出视频：{target}"}
            if not confirm:
                return {
                    "ok": True,
                    "need_confirm": True,
                    "balance": balance,
                    "coins": count,
                    "bvid": ids.get("bvid"),
                    "title": ids.get("title") or "",
                    "note": f"当前硬币余额 {balance}；准备投 {count} 枚。确认请再调用一次并传 confirm=true。",
                }
            if isinstance(balance, int) and balance < count:
                return {"ok": False, "error": f"硬币不足：余额 {balance}，需要 {count}"}
            jct = await _bili_cookie(page, "bili_jct")
            form = {"bvid": ids["bvid"], "multiply": count, "select_like": 0, "csrf": jct}
            got = await _bili_post_form(page, "https://api.bilibili.com/x/web-interface/coin/add", form)
            code = (got or {}).get("code")
            nav2 = await _bili_api(page, "/x/web-interface/nav")
            balance_after = ((nav2 or {}).get("data") or {}).get("money")
            ok = code == 0
            logger.info(f"[perception] B站投币 {ids.get('bvid')} x{count} -> code={code} balance={balance_after}")
            return {"ok": ok, "code": code, "message": (got or {}).get("message"),
                    "coins": count, "bvid": ids.get("bvid"), "title": ids.get("title") or "",
                    "balance_before": balance, "balance_after": balance_after}
        finally:
            try:
                await page.close()
            except Exception:  # noqa: BLE001
                pass
    except Exception as exc:  # noqa: BLE001
        logger.warning(f"[perception] B站投币失败 {target}: {exc}")
        return {"ok": False, "error": f"{type(exc).__name__}: {exc}"}


async def bili_comment(target: str, text: str) -> dict[str, Any]:
    """给视频发评论（API，实测可发可删）。返回 rpid 方便后续核对/清理。"""
    text = (text or "").strip()
    if not text:
        return {"ok": False, "error": "评论内容不能为空"}
    try:
        _pw_, _browser, ctx = await _connect()
        page = await ctx.new_page()
        try:
            await _goto(page, "https://www.bilibili.com/", 1200)
            ids = await _bili_video_ids(page, target)
            if not ids or not ids.get("aid"):
                return {"ok": False, "error": f"认不出视频：{target}"}
            jct = await _bili_cookie(page, "bili_jct")
            got = await _bili_post_form(page, "https://api.bilibili.com/x/v2/reply/add", {
                "oid": ids["aid"], "type": 1, "message": text, "csrf": jct,
            })
            code = (got or {}).get("code")
            rpid = ((got or {}).get("data") or {}).get("rpid")
            if code != 0:
                return {"ok": False, "code": code, "error": (got or {}).get("message") or "评论失败"}
            logger.info(f"[perception] B站评论成功 {ids.get('bvid')} rpid={rpid}")
            return {"ok": True, "rpid": rpid, "text": text, "bvid": ids.get("bvid"),
                    "title": ids.get("title") or ""}
        finally:
            try:
                await page.close()
            except Exception:  # noqa: BLE001
                pass
    except Exception as exc:  # noqa: BLE001
        logger.warning(f"[perception] B站评论失败 {target}: {exc}")
        return {"ok": False, "error": f"{type(exc).__name__}: {exc}"}


async def bili_post_dynamic(text: str) -> dict[str, Any]:
    """发一条 B 站动态（纯文本）。

    为什么走页面而不是 API：create/dyn 需要 wbi 签名 + 一堆风控参数（b_wet、
    device-req、dm_img_*…），手写极易失效；页面点「发布」最稳。
    实测要点：
      · 编辑器是 `.bili-rich-textarea__inner`，必须先 Ctrl+A/Delete 清掉自动恢复的草稿；
      · 用 keyboard.insert_text（不逐字敲）避免 React 重渲染把文字插重复；
      · 点 `.bili-dyn-publishing__action.launcher` 后有时会弹「确认并发送」；
      · 以 create/dyn 的响应为准，code=0 时拿 dyn_id。
    """
    text = (text or "").strip()
    if not text:
        return {"ok": False, "error": "动态内容不能为空"}
    if len(text) > 2000:
        return {"ok": False, "error": "动态太长（>2000 字），先精简"}
    try:
        _pw_, _browser, ctx = await _connect()
        page = await ctx.new_page()
        try:
            if not await _goto(page, "https://t.bilibili.com/", 5000):
                return {"ok": False, "error": "打不开 B 站动态页"}
            editor = await page.query_selector(".bili-rich-textarea__inner")
            if editor is None:
                return {"ok": False, "error": "找不到动态编辑器"}
            await editor.click()
            await page.keyboard.press("Control+A")
            await page.keyboard.press("Delete")
            await page.wait_for_timeout(500)
            await page.keyboard.insert_text(text)
            await page.wait_for_timeout(800)
            typed = ((await editor.inner_text()) or "").replace("\u200b", "").strip()
            if text not in typed:
                return {"ok": False, "error": f"文字没进编辑器（当前：{typed[:40]!r}）"}
            btn = await page.query_selector(".bili-dyn-publishing__action.launcher")
            if btn is None:
                return {"ok": False, "error": "找不到「发布」按钮"}

            async def _publish() -> dict[str, Any] | None:
                async with page.expect_response(
                    lambda r: "create/dyn" in r.url, timeout=25000
                ) as resp_info:
                    try:
                        await btn.click(timeout=5000)
                    except Exception:  # noqa: BLE001
                        await btn.evaluate("el => el.click()")
                    await page.wait_for_timeout(1200)
                    # 部分账号/场景会弹二次确认
                    conf = await page.query_selector("text=确认并发送")
                    if conf is not None:
                        try:
                            await conf.click(timeout=5000)
                        except Exception:  # noqa: BLE001
                            await conf.evaluate("el => el.click()")
                resp = await resp_info.value
                try:
                    return json.loads(await resp.text())
                except Exception:  # noqa: BLE001
                    return None

            try:
                result = await _publish()
            except Exception as exc:  # noqa: BLE001 - 没等到 create/dyn
                toast = ""
                try:
                    toast = (await page.evaluate("() => document.body.innerText"))[-200:]
                except Exception:  # noqa: BLE001
                    pass
                return {"ok": False, "error": f"没等到发布结果：{type(exc).__name__}: {exc}",
                        "page_tail": toast}
            if not result or result.get("code") != 0:
                return {"ok": False, "error": (result or {}).get("message") or "发布失败",
                        "code": (result or {}).get("code")}
            data = result.get("data") or {}
            did = str(data.get("dyn_id_str") or data.get("dyn_id") or "")
            logger.info(f"[perception] B站动态发布成功 dyn_id={did}")
            return {"ok": True, "dynamic_id": did, "text": text,
                    "url": f"https://t.bilibili.com/{did}" if did else ""}
        finally:
            try:
                await page.close()
            except Exception:  # noqa: BLE001
                pass
    except Exception as exc:  # noqa: BLE001
        logger.warning(f"[perception] B站动态发布失败：{exc}")
        return {"ok": False, "error": f"{type(exc).__name__}: {exc}"}


async def bili_delete_dynamic(dynamic_id: str) -> dict[str, Any]:
    """删除自己发的动态（旧版 dynamic_svr 接口；新版 remove/dyn 已 404）。
    只用于测试清理/误发兜底，不注册成她的常规工具。"""
    dynamic_id = (dynamic_id or "").strip()
    if not dynamic_id:
        return {"ok": False, "error": "缺 dynamic_id"}
    try:
        _pw_, _browser, ctx = await _connect()
        page = await ctx.new_page()
        try:
            await _goto(page, "https://t.bilibili.com/", 3000)
            jct = await _bili_cookie(page, "bili_jct")
            got = await _bili_post_form(
                page,
                "https://api.vc.bilibili.com/dynamic_svr/v1/dynamic_svr/rm_dynamic",
                {"dynamic_id": dynamic_id, "csrf": jct},
            )
            ok = bool(got and got.get("code") == 0)
            logger.info(f"[perception] B站删除动态 {dynamic_id} -> ok={ok}")
            return {"ok": ok, "code": (got or {}).get("code"),
                    "message": (got or {}).get("message")}
        finally:
            try:
                await page.close()
            except Exception:  # noqa: BLE001
                pass
    except Exception as exc:  # noqa: BLE001
        return {"ok": False, "error": f"{type(exc).__name__}: {exc}"}


_MSGFEED_PATHS = {
    "reply": "/x/msgfeed/reply?platform=web&build=0&mobi_app=web",
    "at": "/x/msgfeed/at?platform=web&build=0&mobi_app=web",
    "like": "/x/msgfeed/like?platform=web&build=0&mobi_app=web",
    "system": "/x/msgfeed/system?platform=web&build=0&mobi_app=web",
}


def _fmt_message_item(item: dict[str, Any]) -> str:
    """把一条消息中心条目排成一行（带可跳转链接）。字段不固定，尽量宽容。"""
    user = item.get("user") or {}
    name = str(user.get("nickname") or user.get("nick") or user.get("uname") or "某人")
    detail = item.get("item") or {}
    text = str(
        detail.get("source_content")
        or detail.get("title")
        or detail.get("content")
        or item.get("title")
        or item.get("content")
        or ""
    ).strip().replace("\n", " ")
    if len(text) > 80:
        text = text[:80] + "…"
    ts = item.get("reply_time") or item.get("at_time") or item.get("like_time") or item.get("timestamp")
    when = _fmt_ts(ts)
    uri = str(detail.get("uri") or item.get("uri") or "").strip()
    if not uri:
        did = str(detail.get("id") or item.get("id") or "").strip()
        # 动态 id 是 19 位数字；评论/视频类通知一般会直接给 uri
        if did.isdigit() and len(did) >= 15:
            uri = f"https://t.bilibili.com/{did}"
    parts = [f"{name}" + (f" · {when}" if when else "")]
    if text:
        parts.append(text)
    if uri:
        parts.append(f"[打开]({uri})")
    return " · ".join(parts)


async def bili_messages(kind: str = "reply", limit: int = 10) -> dict[str, Any]:
    """B 站消息中心：回复我的 / @我 / 收到的赞 / 系统通知。

    「回复与@」条目会带来源内容与跳转链接；没有新消息时返回空列表。
    """
    kind = (kind or "reply").strip().lower()
    if kind not in _MSGFEED_PATHS:
        return {"ok": False, "error": f"未知消息类型 {kind}（reply/at/like/system）"}
    limit = max(1, min(int(limit or 10), 30))
    try:
        _pw_, _browser, ctx = await _connect()
        page = await ctx.new_page()
        try:
            await _goto(page, "https://message.bilibili.com/", 3000)
            got = await _bili_api(page, _MSGFEED_PATHS[kind])
            if not got or got.get("code") != 0:
                return {"ok": False, "error": (got or {}).get("message") or "消息接口失败",
                        "code": (got or {}).get("code")}
            data = got.get("data") or {}
            if kind == "like":
                total = data.get("total")
                latest = data.get("latest") or []
                if isinstance(latest, dict):  # 有的返回单条对象而不是数组
                    latest = [latest]
                lines = [f"收到的赞：共 {total} 个" + (f"，最近 {len(latest)} 条：" if latest else "")]
                for item in latest[:limit]:
                    lines.append("  · " + _fmt_message_item(item))
                return {"ok": True, "kind": kind, "count": len(latest), "total": total,
                        "text": "\n".join(lines)}
            items = data.get("items") or []
            if isinstance(items, dict):
                items = [items]
            lines = []
            for item in items[:limit]:
                lines.append("  · " + _fmt_message_item(item))
            if not lines:
                lines = [f"（{kind}：暂时没有新消息）"]
            return {"ok": True, "kind": kind, "count": len(items),
                    "text": f"B站消息（{kind}，{len(items)} 条）：\n" + "\n".join(lines)}
        finally:
            try:
                await page.close()
            except Exception:  # noqa: BLE001
                pass
    except Exception as exc:  # noqa: BLE001
        logger.warning(f"[perception] B站消息读取失败：{exc}")
        return {"ok": False, "error": f"{type(exc).__name__}: {exc}"}


# ── B 站弹幕 / 评论读取（2026-10-20 深夜）──────────────────────────────────
# 弹幕：x/v1/dm/list.so?oid=<cid> 返回 XML（登录/未登录都能取，需 cid）；
# 评论：x/v2/reply?type=1&oid=<aid>&pn=&ps=&sort=2（按热度），实测可用。

async def _bili_get_text(page, url: str) -> str:
    """页面上下文 GET 纯文本（弹幕 XML 不是 JSON，不能复用 _bili_api）。"""
    js = """
    async (url) => {
      try {
        const r = await fetch(url, {credentials: 'include'});
        return {status: r.status, text: await r.text()};
      } catch (e) { return {error: String(e)}; }
    }
    """
    try:
        got = await page.evaluate(js, url)
    except Exception as exc:  # noqa: BLE001
        logger.debug(f"[perception] B站文本接口异常 {url}: {exc}")
        return ""
    if not got or got.get("status") != 200:
        return ""
    return str(got.get("text") or "")


def _fmt_dm_time(sec: float) -> str:
    try:
        total = int(float(sec))
    except (TypeError, ValueError):
        return "0:00"
    return f"{total // 60}:{total % 60:02d}"


async def bili_danmaku(target: str, limit: int = 100) -> dict[str, Any]:
    """读视频弹幕（XML）：按出现时间排序，默认取前 100 条。"""
    limit = max(1, min(int(limit or 100), 500))
    try:
        _pw_, _browser, ctx = await _connect()
        page = await ctx.new_page()
        try:
            await _goto(page, "https://www.bilibili.com/", 1200)
            ids = await _bili_video_ids(page, target)
            if not ids or not ids.get("cid"):
                return {"ok": False, "error": f"拿不到视频 cid：{target}"}
            xml = await _bili_get_text(page, f"https://api.bilibili.com/x/v1/dm/list.so?oid={ids['cid']}")
            if not xml:
                return {"ok": False, "error": "弹幕接口没返回内容（可能未开放/风控）"}
            import html as _html

            items: list[dict[str, Any]] = []
            for m in re.finditer(r'<d p="([^"]*)">(.*?)</d>', xml, re.S):
                params = str(m.group(1) or "").split(",")
                try:
                    ts = float(params[0]) if params else 0.0
                except (TypeError, ValueError):
                    ts = 0.0
                text = _html.unescape(re.sub(r"<[^>]+>", "", m.group(2) or "")).strip()
                if text:
                    items.append({"time": ts, "text": text})
            items.sort(key=lambda x: x["time"])
            total = len(items)
            picked = items[:limit]
            lines = [f"《{ids.get('title') or ''}》弹幕：接口返回 {total} 条，取前 {len(picked)} 条（按时间）"]
            for it in picked:
                lines.append(f"{_fmt_dm_time(it['time'])} {it['text']}")
            if not picked:
                lines.append("（这个视频没有弹幕）")
            return {"ok": True, "bvid": ids.get("bvid"), "cid": ids.get("cid"),
                    "title": ids.get("title") or "", "total": total,
                    "count": len(picked), "items": picked, "text": "\n".join(lines)}
        finally:
            try:
                await page.close()
            except Exception:  # noqa: BLE001
                pass
    except Exception as exc:  # noqa: BLE001
        logger.warning(f"[perception] B站弹幕读取失败 {target}: {exc}")
        return {"ok": False, "error": f"{type(exc).__name__}: {exc}"}


async def bili_comments(target: str, limit: int = 20) -> dict[str, Any]:
    """读视频评论（默认按热度），支持翻页到 limit 条（每页 20）。"""
    limit = max(1, min(int(limit or 20), 100))
    try:
        _pw_, _browser, ctx = await _connect()
        page = await ctx.new_page()
        try:
            await _goto(page, "https://www.bilibili.com/", 1200)
            ids = await _bili_video_ids(page, target)
            if not ids or not ids.get("aid"):
                return {"ok": False, "error": f"拿不到视频 aid：{target}"}
            items: list[dict[str, Any]] = []
            total = None
            pn = 1
            while len(items) < limit and pn <= 5:
                ps = min(limit - len(items), 20)
                got = await _bili_api(
                    page,
                    f"/x/v2/reply?type=1&oid={ids['aid']}&pn={pn}&ps={ps}&sort=2",
                )
                if not got or got.get("code") != 0:
                    if pn == 1:
                        return {"ok": False,
                                "error": (got or {}).get("message") or "评论接口失败",
                                "code": (got or {}).get("code")}
                    break
                data = got.get("data") or {}
                total = ((data.get("page") or {}).get("count")) or total
                replies = data.get("replies") or []
                if not replies:
                    break
                for r in replies:
                    member = r.get("member") or {}
                    content = (r.get("content") or {}).get("message") or ""
                    sub = []
                    for s in (r.get("replies") or [])[:2]:
                        sm = s.get("member") or {}
                        smsg = (s.get("content") or {}).get("message") or ""
                        if smsg:
                            sub.append(f"{sm.get('uname') or '?'}：{smsg[:60]}")
                    items.append({
                        "rpid": r.get("rpid"),
                        "user": member.get("uname") or "",
                        "uid": member.get("mid"),
                        "text": content,
                        "like": r.get("like") or 0,
                        "ctime": r.get("ctime"),
                        "sub_count": r.get("rcount") or len(r.get("replies") or []),
                        "sub": sub,
                    })
                    if len(items) >= limit:
                        break
                pn += 1
            lines = [f"《{ids.get('title') or ''}》评论：共 {total if total is not None else '?'} 条，取 {len(items)} 条（按热度）"]
            for idx, it in enumerate(items, 1):
                when = _fmt_ts(it.get("ctime"))
                lines.append(f"{idx}. {it.get('user') or '匿名'}"
                             + (f" · {it['like']} 赞" if it.get("like") else "")
                             + (f" · {when}" if when else "")
                             + f"：{str(it.get('text') or '').replace(chr(10), ' ')[:200]}")
                if it.get("sub"):
                    for s in it["sub"]:
                        lines.append(f"   └ {s}")
            if not items:
                lines.append("（没有评论）")
            return {"ok": True, "bvid": ids.get("bvid"), "aid": ids.get("aid"),
                    "title": ids.get("title") or "", "total": total,
                    "count": len(items), "items": items, "text": "\n".join(lines)}
        finally:
            try:
                await page.close()
            except Exception:  # noqa: BLE001
                pass
    except Exception as exc:  # noqa: BLE001
        logger.warning(f"[perception] B站评论读取失败 {target}: {exc}")
        return {"ok": False, "error": f"{type(exc).__name__}: {exc}"}


# ── B 站视频「真正看内容」：复用 media_ai 三档理解（asr / flash / pro）──────
# 流程：playurl 取最低码率音/视频流 → 下载 → ffmpeg 截断/压低到 24MB 内 →
#       media_ai.understand(...)。asr 只送音频；flash/pro 送带音轨的 mp4。
# 注意：B站 DASH 的音视频是分开的（.m4s），必须 ffmpeg 合流后 media_ai 才能原生看。
_MEDIA_UA = ("Mozilla/5.0 (X11; Linux x86_64) AppleWebKit/537.36 "
             "(KHTML, like Gecko) Chrome/124.0 Safari/537.36")
_MAX_STREAM_BYTES = 90 * 1024 * 1024  # 单条流下载上限（90MB），再大就放弃
_MEDIA_MAX_BYTES = 24 * 1024 * 1024   # media_ai 的直接发送上限


async def _bili_download_stream(url: str, dest: Path, *, referer: str, cookies: dict[str, str]) -> int:
    """带 Referer/UA/Cookie 下载 B站 CDN 流；超过上限抛错。"""
    headers = {"referer": referer, "user-agent": _MEDIA_UA}
    total = 0
    async with httpx.AsyncClient(timeout=180.0, follow_redirects=True) as client:
        async with client.stream("GET", url, headers=headers, cookies=cookies) as resp:
            resp.raise_for_status()
            with open(dest, "wb") as fh:
                async for chunk in resp.aiter_bytes(64 * 1024):
                    total += len(chunk)
                    if total > _MAX_STREAM_BYTES:
                        raise RuntimeError(f"流太大（>{_MAX_STREAM_BYTES // 1024 // 1024}MB）")
                    fh.write(chunk)
    return total


def _bili_stream_urls(stream: dict[str, Any]) -> list[str]:
    """一条流的主地址 + backupUrl 列表（CDN 偶发 567 时换镜像重试）。"""
    urls = [str(stream.get("baseUrl") or "")]
    for u in (stream.get("backupUrl") or []):
        if isinstance(u, str) and u:
            urls.append(u)
    return [u for u in urls if u]


async def _bili_download_first(urls: list[str], dest: Path, *, referer: str,
                               cookies: dict[str, str]) -> int:
    """按顺序尝试多个 CDN 地址，成功即返回；全失败抛最后一个错误。"""
    last: Exception | None = None
    for url in urls[:5]:
        try:
            return await _bili_download_stream(url, dest, referer=referer, cookies=cookies)
        except Exception as exc:  # noqa: BLE001
            last = exc
            logger.warning(f"[perception] B站流下载失败，换下一个镜像：{type(exc).__name__}")
            await asyncio.sleep(0.6)
    raise last or RuntimeError("没有可用的流地址")


def _ffmpeg(args: list[str]) -> None:
    """跑 ffmpeg/ffprobe（同步，调用方用 asyncio.to_thread 包）。"""
    proc = subprocess.run(args, capture_output=True, text=True, timeout=300)
    if proc.returncode != 0:
        raise RuntimeError((proc.stderr or proc.stdout or "ffmpeg failed")[:400])


def _bili_mux_for_media(video: Path, audio: Path | None, out: Path, max_seconds: int,
                        height: int = 480, crf: int = 28) -> None:
    """把 DASH 音视频合成一个 ≤24MB 的 mp4：截断 + 缩放 + 重编码。"""
    args = ["ffmpeg", "-y", "-loglevel", "error", "-i", str(video)]
    if audio is not None:
        args += ["-i", str(audio)]
    args += ["-t", str(max_seconds), "-map", "0:v:0"]
    if audio is not None:
        args += ["-map", "1:a:0"]
    args += ["-vf", f"scale=-2:{height}", "-c:v", "libx264", "-preset", "veryfast",
             "-crf", str(crf), "-c:a", "aac", "-b:a", "64k", "-movflags", "+faststart", str(out)]
    _ffmpeg(args)


def _bili_clip_audio(audio: Path, out: Path, max_seconds: int) -> None:
    """ASR 只送音频：截断到 max_seconds，转 m4a（media_ai 会再转 wav）。"""
    _ffmpeg(["ffmpeg", "-y", "-loglevel", "error", "-i", str(audio),
             "-t", str(max_seconds), "-c:a", "aac", "-b:a", "64k", str(out)])


async def bili_watch(target: str, *, tier: str = "flash", frames: int = 4, fps: float = 1.0,
                     prompt: str = "", max_seconds: int = 180) -> dict[str, Any]:
    """真正“看/听”一个 B 站视频：复用 media_ai 的 asr / flash / pro 三档。

    · asr   ：只送音频做转写（最便宜；适合有讲解/语音的视频）
    · flash ：原生视频理解（画面+声音，性价比档）
    · pro   ：原生视频理解（最细，最贵）
    长视频会截断到 max_seconds（默认 180s），再压到 24MB 内。
    """
    tier = tier if tier in ("asr", "flash", "pro") else "flash"
    try:
        max_seconds = max(15, min(int(max_seconds or 180), 300))
    except (TypeError, ValueError):
        max_seconds = 180
    try:
        frames = max(1, min(int(frames or 4), 8))
    except (TypeError, ValueError):
        frames = 4
    try:
        from . import media_ai  # 延迟导入：只有真正看视频时才需要
    except Exception as exc:  # noqa: BLE001
        return {"ok": False, "error": f"media_ai 不可用：{exc}"}

    try:
        _pw_, _browser, ctx = await _connect()
        page = await ctx.new_page()
        tmpdir = Path(tempfile.mkdtemp(prefix="bili-watch-"))
        try:
            await _goto(page, "https://www.bilibili.com/", 1200)
            ids = await _bili_video_ids(page, target)
            if not ids or not ids.get("bvid") or not ids.get("cid"):
                return {"ok": False, "error": f"拿不到视频信息：{target}"}
            bvid, cid = str(ids["bvid"]), str(ids["cid"])
            play = await _bili_api(
                page,
                f"/x/player/playurl?bvid={bvid}&cid={cid}&qn=16&fnval=16&fourk=0",
            )
            data = (play or {}).get("data") or {}
            dash = data.get("dash") or {}
            videos = [v for v in (dash.get("video") or []) if isinstance(v, dict) and v.get("baseUrl")]
            audios = [a for a in (dash.get("audio") or []) if isinstance(a, dict) and a.get("baseUrl")]
            if not videos:
                return {"ok": False, "error": "playurl 没给视频流（可能需要大会员/地区限制）"}
            # 选最低码率（体积最小），音频同理
            video = min(videos, key=lambda v: int(v.get("bandwidth") or 10**12))
            audio = min(audios, key=lambda a: int(a.get("bandwidth") or 10**12)) if audios else None
            # 同一码率可能有多个镜像（backupUrl），再加上几档更高码率做兜底
            video_urls = _bili_stream_urls(video)
            for extra in sorted(videos, key=lambda v: int(v.get("bandwidth") or 10**12))[1:4]:
                video_urls.extend(_bili_stream_urls(extra))
            audio_urls = _bili_stream_urls(audio) if audio else []
            cookies = {c["name"]: c["value"] for c in await ctx.cookies("https://www.bilibili.com")}
            referer = f"https://www.bilibili.com/video/{bvid}"
            duration = ids.get("duration") or 0
            clip_s = min(max_seconds, int(duration) or max_seconds)
            if tier == "asr":
                # 只转写音频：视频流根本不用下（长视频能省几十 MB 带宽）
                if audio is None:
                    return {"ok": False, "error": "没有音轨，asr 档用不了"}
                apath = tmpdir / "audio.m4a"
                await _bili_download_first(audio_urls, apath, referer=referer, cookies=cookies)
                out = tmpdir / "audio-clip.m4a"
                await asyncio.to_thread(_bili_clip_audio, apath, out, clip_s)
                file_for_ai, hint = out, "audio"
            else:
                vpath = tmpdir / "video.mp4"
                apath = tmpdir / "audio.m4a" if audio else None
                await _bili_download_first(video_urls, vpath, referer=referer, cookies=cookies)
                if audio is not None and apath is not None:
                    await _bili_download_first(audio_urls, apath, referer=referer, cookies=cookies)
                out = tmpdir / "watch.mp4"
                await asyncio.to_thread(_bili_mux_for_media, vpath, apath, out, clip_s)
                # 超过 24MB 就降档重来（360p / 90s / 60s）
                attempts = [(360, 32, min(clip_s, 120)), (360, 34, min(clip_s, 60))]
                for height, crf, secs in attempts:
                    if out.stat().st_size <= _MEDIA_MAX_BYTES:
                        break
                    await asyncio.to_thread(_bili_mux_for_media, vpath, apath, out, secs, height, crf)
                if out.stat().st_size > _MEDIA_MAX_BYTES:
                    return {"ok": False, "error": "压缩后仍超过 24MB，先把视频截短再试"}
                file_for_ai, hint = out, "video"

            result = await media_ai.understand(
                str(file_for_ai),
                tier=tier,
                hint=hint,
                prompt=prompt,
                frames=frames,
                fps=float(fps or 1.0),
            )
            if isinstance(result, dict):
                result.setdefault("bvid", bvid)
                result.setdefault("title", ids.get("title") or "")
                result["watch_seconds"] = clip_s
                result["file_bytes"] = file_for_ai.stat().st_size
            logger.info(f"[perception] B站视频理解完成 {bvid} tier={tier} "
                        f"bytes={file_for_ai.stat().st_size}")
            return result or {"ok": False, "error": "media_ai 没返回结果"}
        finally:
            try:
                await page.close()
            except Exception:  # noqa: BLE001
                pass
            try:
                import shutil

                shutil.rmtree(tmpdir, ignore_errors=True)
            except Exception:  # noqa: BLE001
                pass
    except Exception as exc:  # noqa: BLE001
        logger.warning(f"[perception] B站视频理解失败 {target}: {exc}")
        return {"ok": False, "error": f"{type(exc).__name__}: {exc}"}
