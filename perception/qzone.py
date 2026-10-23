"""QQ 空间（QZone）对接 —— 读说说、发说说、点赞、评论

用户 2026-10-20：这个号是单独注册的，凭据算不得风险；要从"只能读"升级到
"读 + 发 + 赞 + 评论"，并注册成一组工具让她主动使用。

凭据从哪来：
    机器人自己登录着 QQ（NapCat），OneBot 的 `get_cookies` 能拿到 qzone 域的 cookie
    （uin / skey / p_skey …）。**不落盘、不写日志**，只在进程内缓存 30 分钟。

接口（都是 QZone 老接口，社区里通行）：
    发说说  POST /proxy/domain/taotao.qzone.qq.com/cgi-bin/emotion_cgi_publish_v6
    说说列表 GET  /proxy/domain/taotao.qzone.qq.com/cgi-bin/emotion_cgi_msglist_v6
    点赞    POST /proxy/domain/w.qzone.qq.com/cgi-bin/likes/internal_dolike_app
    评论    POST /proxy/domain/taotao.qzone.qq.com/cgi-bin/emotion_cgi_re_feeds

风险与克制（写进工具描述）：
    · 点赞/评论/发说说都是**对外可见**的行为，风控比群消息狠；
    · 这里不自动重试、不批量、不带随机定时——每次调用都是她的明确决定。
"""

from __future__ import annotations

import json
import time
import urllib.parse
from typing import Any

import httpx
from nonebot import logger

QZONE = "https://user.qzone.qq.com"
_ck: dict[str, Any] = {"at": 0.0, "cookies": {}, "uin": ""}


def _gtk(skey: str) -> int:
    """QZone 的 g_tk：hash33(p_skey) —— 老算法，社区一致。"""
    h = 5381
    for ch in skey:
        h += (h << 5) + ord(ch)
    return h & 0x7FFFFFFF


async def _cookies(bot: Any) -> tuple[dict[str, str], str]:
    """从 OneBot 拿 qzone cookie（30 分钟缓存）。返回 (cookies, uin)。"""
    now = time.time()
    if now - float(_ck["at"]) < 1800 and _ck["cookies"]:
        return _ck["cookies"], _ck["uin"]
    # 优先用扫码登录铸出来的 cookie（比 NapCat get_cookies 的 p_skey 更“空间域”）
    try:
        with open("/data/qqbot/config/qzone-cookies.json", encoding="utf-8") as fh:
            saved = json.load(fh)
        uin = str(saved.get("uin") or saved.get("p_uin") or "").lstrip("o")
        if saved.get("p_skey") or saved.get("skey"):
            _ck.update({"at": now, "cookies": saved, "uin": uin})
            return saved, uin
    except (OSError, ValueError):
        pass
    if bot is None:
        return {}, ""
    domains = ("qzone.qq.com", "user.qzone.qq.com")
    cookies: dict[str, str] = {}
    for domain in domains:
        try:
            resp = await bot.call_api("get_cookies", domain=domain)
        except Exception as exc:  # noqa: BLE001
            logger.debug(f"[perception] get_cookies({domain}) 失败：{exc}")
            continue
        part = None
        if isinstance(resp, dict):
            part = resp.get("cookies")
        if isinstance(part, str):
            for item in part.split("; "):
                if "=" in item:
                    k, v = item.split("=", 1)
                    cookies[k.strip()] = v.strip()
        elif isinstance(part, dict):
            cookies.update({str(k): str(v) for k, v in part.items()})
        if cookies:
            break
    raw_uin = cookies.get("uin") or cookies.get("p_uin") or ""
    if raw_uin.startswith("o"):
        raw_uin = raw_uin[1:]
    _ck.update({"at": now, "cookies": cookies, "uin": raw_uin})
    return _ck["cookies"], str(_ck["uin"])


async def _qzonetoken(cookies: dict[str, str], uin: str) -> str:
    """从 qzone 首页抠 g_qzonetoken（老接口做参数校验用）。失败给空串。"""
    try:
        async with httpx.AsyncClient(timeout=15.0, headers={"user-agent": "Mozilla/5.0"}) as client:
            resp = await client.get(f"{QZONE}/{uin}", cookies=cookies)
        import re as _re

        m = _re.search(r'window\.g_qzonetoken\s*=\s*"([^"]+)"', resp.text)
        return m.group(1) if m else ""
    except Exception:  # noqa: BLE001
        return ""


async def feed_list(bot: Any, num: int = 10) -> dict[str, Any]:
    """读自己的说说列表（只读，最安全的一个接口）。"""
    cookies, uin = await _cookies(bot)
    if not cookies or not uin:
        return {"ok": False, "error": "拿不到 qzone cookie（机器人未登录或接口不支持）"}
    skey = cookies.get("p_skey") or cookies.get("skey") or ""
    qztoken = await _qzonetoken(cookies, uin)
    params = {
        "uin": uin,
        "ftype": "0",
        "sort": "0",
        "pos": "0",
        "num": str(max(1, min(num, 30))),
        "replynum": "10",
        "g_tk": str(_gtk(skey)),
        "qzonetoken": qztoken,
        "callback": "_preloadCallback",
        "code": "1",
        "format": "json",
        "qzreferrer": f"https://user.qzone.qq.com/{uin}",
    }
    url = f"{QZONE}/proxy/domain/taotao.qzone.qq.com/cgi-bin/emotion_cgi_msglist_v6?{urllib.parse.urlencode(params)}"
    try:
        async with httpx.AsyncClient(timeout=20.0, headers={"user-agent": "Mozilla/5.0", "referer": f"{QZONE}/{uin}"}) as client:
            resp = await client.get(url, cookies=cookies)
        text = resp.text.strip()
        if text.startswith("_preloadCallback("):
            text = text[len("_preloadCallback("):text.rfind(")")]
        data = json.loads(text)
        msgs = data.get("msglist") or []
        out = []
        for m in msgs[:num]:
            out.append({
                "tid": m.get("tid"),
                "content": str(m.get("content") or "")[:300],
                "created": m.get("created_time"),
                "likenum": m.get("likenum"),
                "cmtnum": m.get("cmtnum"),
                "unikey": m.get("unikey"),
            })
        return {"ok": True, "uin": uin, "count": len(out), "feeds": out, "raw_code": data.get("code")}
    except Exception as exc:  # noqa: BLE001
        return {"ok": False, "error": f"读说说失败：{type(exc).__name__}: {exc}"}


async def post_feed(bot: Any, content: str) -> dict[str, Any]:
    """发一条说说（纯文本）。"""
    cookies, uin = await _cookies(bot)
    if not cookies or not uin:
        return {"ok": False, "error": "拿不到 qzone cookie"}
    skey = cookies.get("p_skey") or cookies.get("skey") or ""
    form = {
        "syn_tweet_verson": "1",
        "paramstr": "1",
        "who": "1",
        "conformat": "1",
        "richtype": "1",
        "subrichtype": "1",
        "content": content,
        "feedversion": "1",
        "ver": "1",
        "ugc_right": "1",
        "to_sign": "0",
        "hostuin": uin,
        "code": "1",
        "format": "json",
        "g_tk": str(_gtk(skey)),
    }
    url = f"{QZONE}/proxy/domain/taotao.qzone.qq.com/cgi-bin/emotion_cgi_publish_v6"
    try:
        async with httpx.AsyncClient(timeout=25.0, headers={"user-agent": "Mozilla/5.0", "referer": f"{QZONE}/{uin}"}) as client:
            resp = await client.post(url, data=form, cookies=cookies)
        text = resp.text.strip()
        code = None
        if text.startswith("_Callback("):
            text = text[len("_Callback("):text.rfind(")")]
            try:
                code = json.loads(text).get("code")
            except Exception:  # noqa: BLE001
                pass
        return {"ok": resp.status_code == 200 and (code in (None, 0)), "status": resp.status_code, "code": code, "text": text[:200]}
    except Exception as exc:  # noqa: BLE001
        return {"ok": False, "error": f"发说说失败：{type(exc).__name__}: {exc}"}


async def like_feed(bot: Any, unikey: str, like: bool = True) -> dict[str, Any]:
    """给一条说说点赞/取消赞（unikey 从 feed_list 里取）。"""
    cookies, uin = await _cookies(bot)
    if not cookies or not uin:
        return {"ok": False, "error": "拿不到 qzone cookie"}
    skey = cookies.get("p_skey") or cookies.get("skey") or ""
    form = {
        "opuin": uin,
        "unikey": unikey,
        "curkey": unikey,
        "appid": "311",
        "from": "1",
        "typeid": "0",
        "abstime": str(int(time.time())),
        "fid": "",
        "active": "0",
        "fupdate": "1",
        "opr_type": "2001" if like else "2002",
        "g_tk": str(_gtk(skey)),
        "code": "1",
        "format": "json",
        "qzreferrer": f"{QZONE}/{uin}",
    }
    url = f"{QZONE}/proxy/domain/w.qzone.qq.com/cgi-bin/likes/internal_dolike_app"
    try:
        async with httpx.AsyncClient(timeout=20.0, headers={"user-agent": "Mozilla/5.0", "referer": f"{QZONE}/{uin}"}) as client:
            resp = await client.post(url, data=form, cookies=cookies)
        text = resp.text.strip()
        ok = '"code":0' in text.replace(" ", "") or resp.status_code == 200
        return {"ok": ok, "status": resp.status_code, "text": text[:200]}
    except Exception as exc:  # noqa: BLE001
        return {"ok": False, "error": f"点赞失败：{type(exc).__name__}: {exc}"}


async def comment_feed(bot: Any, tid: str, content: str, uin_ref: str = "") -> dict[str, Any]:
    """评论一条说说。"""
    cookies, uin = await _cookies(bot)
    if not cookies or not uin:
        return {"ok": False, "error": "拿不到 qzone cookie"}
    skey = cookies.get("p_skey") or cookies.get("skey") or ""
    form = {
        "topicId": f"{uin_ref or uin}_{tid}",
        "uin": uin,
        "hostUin": uin_ref or uin,
        "feedsType": "0",
        "inCharset": "utf-8",
        "outCharset": "utf-8",
        "plat": "qzone",
        "source": "ic",
        "content": content,
        "code": "1",
        "format": "json",
        "g_tk": str(_gtk(skey)),
    }
    url = f"{QZONE}/proxy/domain/taotao.qzone.qq.com/cgi-bin/emotion_cgi_re_feeds"
    try:
        async with httpx.AsyncClient(timeout=20.0, headers={"user-agent": "Mozilla/5.0", "referer": f"{QZONE}/{uin}"}) as client:
            resp = await client.post(url, data=form, cookies=cookies)
        text = resp.text.strip()
        code = None
        if text.startswith("_Callback("):
            try:
                code = json.loads(text[len("_Callback("):text.rfind(")")]).get("code")
            except Exception:  # noqa: BLE001
                pass
        return {"ok": resp.status_code == 200 and (code in (None, 0)), "status": resp.status_code, "code": code, "text": text[:200]}
    except Exception as exc:  # noqa: BLE001
        return {"ok": False, "error": f"评论失败：{type(exc).__name__}: {exc}"}


# ── 扫码登录（用户 2026-10-20：用手机扫一次，铸出空间 session）──────────────────
_QR: dict[str, Any] = {"qrsig": "", "ptqrtoken": "", "started_at": 0.0, "png": b"", "status": "idle"}


def _hash33(text: str) -> int:
    h = 0
    for ch in text:
        h += (h << 5) + ord(ch)
    return h & 0x7FFFFFFF


async def qr_start() -> dict[str, Any]:
    """取一张 QQ 登录二维码（appid=549000912 空间），存到模块状态里。"""
    import random

    url = (
        "https://ssl.ptlogin2.qq.com/ptqrshow?appid=549000912&e=2&l=M&s=3&d=72&v=4"
        f"&t={random.random()}&daid=5&pt_3rd_aid=0"
    )
    try:
        async with httpx.AsyncClient(timeout=20.0, headers={"user-agent": "Mozilla/5.0"}) as client:
            resp = await client.get(url)
        qrsig = ""
        for raw in resp.headers.get_list("set-cookie"):
            if raw.startswith("qrsig="):
                qrsig = raw.split(";")[0].split("=", 1)[1]
        _QR.update({
            "qrsig": qrsig,
            "ptqrtoken": str(_hash33(qrsig)),
            "started_at": time.time(),
            "png": resp.content,
            "status": "waiting",
        })
        return {"ok": True, "qrsig_len": len(qrsig), "png_bytes": len(resp.content), "ptqrtoken": _QR["ptqrtoken"]}
    except Exception as exc:  # noqa: BLE001
        return {"ok": False, "error": f"{type(exc).__name__}: {exc}"}


async def qr_poll() -> dict[str, Any]:
    """轮询扫码状态；成功后把 cookie 存起来（qzone 用）。"""
    if not _QR.get("qrsig"):
        return {"ok": False, "error": "还没开始扫码"}
    import random

    ts = int(time.time() * 1000)
    url = (
        "https://ssl.ptlogin2.qq.com/ptqrlogin?u1=https%3A%2F%2Fqzone.qq.com%2F"
        f"&ptqrtoken={_QR['ptqrtoken']}&ptredirect=0&h=1&t=1&g=1&from_ui=1&ptlang=2052"
        f"&action=0-0-{ts}&js_ver=10233&js_type=1&login_sig=&pt_uistyle=40&aid=549000912&daid=5&"
    )
    try:
        async with httpx.AsyncClient(timeout=20.0, headers={"user-agent": "Mozilla/5.0", "referer": "https://qzone.qq.com/"}) as client:
            resp = await client.get(url, cookies={"qrsig": _QR["qrsig"]})
        text = resp.text
        import re as _re

        m = _re.search(r"ptuiCB\('(\d+)','(\d+)','([^']*)','\d+','([^']*)'", text)
        code = m.group(1) if m else "?"
        msg = m.group(4) if m else text[:80]
        if code == "0" and m and m.group(3):
            redirect = m.group(3)
            async with httpx.AsyncClient(timeout=20.0, headers={"user-agent": "Mozilla/5.0"}, follow_redirects=True) as client:
                await client.get(redirect)
                jar = dict(resp.cookies) | dict(client.cookies)
            # 再从 qzone 首页补一次 p_skey
            _QR["status"] = "success"
            _QR["cookies"] = {k: v for k, v in jar.items() if v}
            path = None
            try:
                import json as _json

                path = "/data/qqbot/config/qzone-cookies.json"
                with open(path, "w", encoding="utf-8") as fh:
                    _json.dump(_QR["cookies"], fh)
            except Exception as exc:  # noqa: BLE001
                logger.warning(f"[perception] 保存 qzone cookie 失败：{exc}")
            _ck.update({"at": 0.0, "cookies": _QR["cookies"], "uin": str(_QR["cookies"].get("uin", "")).lstrip("o")})
            return {"ok": True, "code": code, "msg": msg, "saved": path, "has_p_skey": "p_skey" in _QR["cookies"]}
        _QR["status"] = {"66": "waiting-scan", "67": "scanned-confirm", "65": "expired"}.get(code, "unknown")
        return {"ok": True, "code": code, "msg": msg, "status": _QR["status"]}
    except Exception as exc:  # noqa: BLE001
        return {"ok": False, "error": f"{type(exc).__name__}: {exc}"}


def qr_png() -> bytes:
    return _QR.get("png") or b""
