"""扫码登录（CDP 连接常驻 chromium，登录态自动落在容器磁盘 profile）

不再使用 storage_state 文件：profile 在 /data/qqbot/chrome-profile（宿主磁盘），
浏览器容器重启后登录态仍在；bot 容器重启完全不影响浏览器。
"""

from __future__ import annotations

import base64
from typing import Any

from nonebot import logger

# Chrome 会校验 Host 头（只接受 IP/localhost），所以用容器 IP 而不是主机名
def _cdp_url() -> str:
    import socket

    for host in ("qqbot-chromium", "127.0.0.1"):
        try:
            ip = socket.gethostbyname(host)
            return f"http://{ip}:9222"
        except Exception:  # noqa: BLE001
            continue
    return "http://127.0.0.1:9222"


CDP_URL = _cdp_url()

_SERVICES: dict[str, dict[str, Any]] = {
    "qzone": {
        "url": "https://user.qzone.qq.com/1000000001",
        "ok": ("的QQ空间", "说点儿什么吧", "今日访客", "好友动态", "写说说"),
    },
    "bilibili": {
        "url": "https://www.bilibili.com",
        "ok": ("消息", "动态", "收藏", "历史"),
    },
}
_SESSIONS: dict[str, dict[str, Any]] = {}

_pw = None


async def _connect():
    global _pw
    from playwright.async_api import async_playwright

    if _pw is None:
        _pw = await async_playwright().start()
    browser = await _pw.chromium.connect_over_cdp(CDP_URL)
    ctx = browser.contexts[0] if browser.contexts else await browser.new_context()
    return browser, ctx


async def _text_of(page) -> str:
    out = []
    for frame in page.frames:
        try:
            out.append(await frame.inner_text("body"))
        except Exception:  # noqa: BLE001
            continue
    return "\n".join(out)


async def _logged_in(service: str, page) -> bool:
    text = await _text_of(page)
    return any(m in text for m in _SERVICES[service]["ok"])


async def start(service: str) -> dict[str, Any]:
    cfg = _SERVICES.get(service)
    if not cfg:
        return {"ok": False, "error": f"未知服务 {service}"}
    try:
        browser, ctx = await _connect()
        page = await ctx.new_page()
        # CDP 连接的页面默认没有可见表面，截图会报 "Unable to capture screenshot"：
        # 先设视口 + 置前，再截图。
        try:
            await page.set_viewport_size({"width": 1280, "height": 900})
            await page.bring_to_front()
        except Exception:  # noqa: BLE001
            pass
        await page.goto(cfg["url"], timeout=60000, wait_until="domcontentloaded")
        await page.wait_for_timeout(5000)
        if service == "bilibili":
            for sel in ("text=登录", ".header-login-entry"):
                try:
                    el = await page.query_selector(sel)
                    if el:
                        await el.click()
                        await page.wait_for_timeout(2500)
                        break
                except Exception:  # noqa: BLE001
                    continue
        _SESSIONS[service] = {"page": page, "ctx": ctx}
        # 二维码一般就在页面里，整页截图足够扫；另外尽量单独截二维码元素
        png = b""
        for sel in ("#qlogin_img", "img[src*='ptqrshow']", "canvas", ".login-scan-box canvas", ".qrcode-img", "img[src*='qrcode']"):
            try:
                el = await page.query_selector(sel)
                if el:
                    shot = await el.screenshot()
                    if shot and len(shot) > 800:
                        png = shot
                        break
            except Exception:  # noqa: BLE001
                continue
        if not png:
            try:
                png = await page.screenshot(full_page=False, timeout=20000)
            except Exception as exc:  # noqa: BLE001
                logger.warning(f"[perception] {service} 整页截图失败：{exc}")
                png = b""
        logger.info(f"[perception] {service} 登录页已打开，截图 {len(png)} 字节")
        return {"ok": True, "service": service, "png_bytes": len(png), "png_b64": base64.b64encode(png).decode()}
    except Exception as exc:  # noqa: BLE001
        return {"ok": False, "error": f"{type(exc).__name__}: {exc}"}


async def check(service: str) -> dict[str, Any]:
    cfg = _SERVICES.get(service)
    sess = _SESSIONS.get(service)
    if not cfg or not sess:
        return {"ok": False, "error": "没有进行中的登录，请先 start"}
    page = sess["page"]
    try:
        if not await _logged_in(service, page):
            return {"ok": True, "status": "waiting"}
        _SESSIONS.pop(service, None)
        logger.info(f"[perception] {service} 登录成功（登录态已在 chromium profile 里）")
        return {"ok": True, "status": "success", "persistent": True}
    except Exception as exc:  # noqa: BLE001
        return {"ok": False, "error": f"{type(exc).__name__}: {exc}"}


def png_of(service: str) -> bytes:
    return b""


async def shutdown() -> None:
    """CDP 模式无需保存/关闭浏览器：登录态属于 chromium 容器。"""
    return None
