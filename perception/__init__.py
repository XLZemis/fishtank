"""感知层插件入口（NoneBot 侧）· perception

落点：`/data/qqbot/bot/app/plugins/perception/`（新增目录，不改现有插件）
宿主：qqbot-nonebot 容器（NoneBot 2.5 + onebot v11 适配器）

接线原则（部署文档 §1「不改 NoneBot 现有功能」）：
    * 记录器 priority=40 / block=False：**所有**群消息入池，但绝不拦截现有逻辑
    * @ 接管  priority=45 / block=True：只在「有启用 mention 触发器的群」生效，
      用来压住 chat.py 原有的 mention_chat（priority=99）。没有 mention 触发器的群行为完全不变。
    * 通知    priority=40 / block=False：戳一戳等入池 + T2 触发
    * 定时器（interval/cron/她的 NEXT_CHECK）由 triggers.tick_loop 驱动（不走 apscheduler，
      避免与现有插件抢调度器）；触发器配置由她维护在 /data/persona/triggers.yaml，60s 热加载。
"""

from __future__ import annotations

import os
from pathlib import Path

from nonebot import on_command, on_message, on_notice, require
from nonebot.adapters.onebot.v11 import Bot, GroupMessageEvent, MessageEvent, NoticeEvent, PrivateMessageEvent

try:  # FastAPI 的 Request 必须能在模块全局解析，否则会被当成查询参数
    from fastapi import Request
except Exception:  # noqa: BLE001
    Request = None  # type: ignore[assignment]
from nonebot import logger
from nonebot.params import CommandArg
from nonebot.rule import Rule

from . import bridge, bus, sender, sticker, triggers

require("nonebot_plugin_apscheduler")  # 仅用于声明依赖关系（本插件不用它的调度）

# /debug/onebot 只放查询类 API（排障用；写操作一律不进白名单）
_DEBUG_APIS = frozenset({
    "get_msg",
    "get_forward_msg",
    "get_group_info",
    "get_group_list",
    "get_group_member_info",
    "get_stranger_info",
    "get_friend_list",
    "get_login_info",
    "get_status",
    "get_version_info",
})


def _bili_action_text(action: str, result: dict) -> str:
    """把 B 站互动结果排成一句给她看的话（投币确认包含余额与枚数）。"""
    if not result.get("ok"):
        return "操作失败：" + str(result.get("error") or result.get("message") or "未知错误")
    title = str(result.get("title") or "")
    name = f"《{title}》" if title else ""
    if action in ("like", "unlike"):
        if result.get("already"):
            return ("本来就是已赞" if action == "like" else "本来就没赞") + name
        return ("点赞成功" if action == "like" else "已取消赞") + name
    if action in ("favorite", "unfavorite"):
        if action == "favorite":
            return f"已收藏{name}到「{result.get('folder') or '默认收藏夹'}」"
        return f"已取消收藏{name}"
    if action == "coin":
        if result.get("need_confirm"):
            return (f"投币前确认：当前硬币余额 {result.get('balance')}，"
                    f"准备给{name or '这个视频'}投 {result.get('coins')} 枚。"
                    "确认的话再调用一次并传 confirm=true。")
        if result.get("ok"):
            return (f"投币成功：给{name or '这个视频'}投了 {result.get('coins')} 枚，"
                    f"余额 {result.get('balance_before')} → {result.get('balance_after')}")
        return f"投币失败：{result.get('message') or result.get('error')}"
    if action == "comment":
        return f"评论发出去了（rpid={result.get('rpid')}）：{result.get('text')}"
    if action == "post_dynamic":
        return "动态发出去了：" + str(result.get("url") or "")
    if action == "delete_dynamic":
        return "动态已删除"
    if action in ("danmaku", "comments", "messages"):
        return str(result.get("text") or "（没有内容）")
    if action == "watch":
        text = str(result.get("text") or ((result.get("asr") or {}).get("text")) or "").strip()
        head = f"《{result.get('title') or '视频'}》{result.get('tier') or ''}档"
        if result.get("watch_seconds"):
            head += f"（取前 {result['watch_seconds']}s）"
        return (head + "：\n" + text) if text else (head + "：没理解出内容")
    return str(result)

# ── 记录器：所有消息入池（不拦截）────────────────────────────────────────────
_recorder = on_message(priority=40, block=False)


@_recorder.handle()
async def _handle_record(bot: Bot, event: MessageEvent) -> None:
    """把消息喂给感知层；内部已 try/except，不会影响其它 matcher"""
    await triggers.ingest_message(event, bot)


# ── 通知：戳一戳 / 表情回应（不拦截）────────────────────────────────────────
_notice = on_notice(priority=40, block=False)


@_notice.handle()
async def _handle_notice(bot: Bot, event: NoticeEvent) -> None:
    await triggers.ingest_notice(event, bot)


# ── T1 接管：群 @（只在名单里的群拦截旧 @ 回复）───────────────────────────────


async def _at_me_rule(event: MessageEvent, bot: Bot) -> bool:
    """规则：群消息 + 该群允许人格接管 @ + 消息里 @ 了机器人自己"""
    if not isinstance(event, GroupMessageEvent):
        return False
    scope = bus.scope_of(event)
    if not scope or not triggers.at_takeover(scope):
        return False
    # 注意：适配器已经把开头的 @我 段从 event.message 里摘掉了（_check_at_me），
    # 所以只能看 event.is_tome()，扫消息段会永远 False → 挡不住 chat.py 的旧 @ 回复。
    try:
        return bool(event.is_tome())
    except Exception:  # noqa: BLE001
        return False


_at_takeover = on_message(priority=45, block=True, rule=Rule(_at_me_rule))


@_at_takeover.handle()
async def _handle_at(bot: Bot, event: MessageEvent) -> None:
    """记录与触发已在 _recorder 里做过；这里只负责"到此为止"，
    阻止 chat.py 的 mention_chat（priority=99）再回一遍（双回复风险）。"""
    logger.debug("[perception] @ 接管生效，已阻止旧 @ 回复")


# ── 私聊接管（谨慎阶段；默认关闭）────────────────────────────────────────


async def _private_rule(event: MessageEvent) -> bool:
    """私聊接管：只有 triggers.yaml 里 private: true 时才生效（谨慎阶段）"""
    if not isinstance(event, PrivateMessageEvent):
        return False
    scope = bus.scope_of(event)
    # 空消息（疑似输入中）不接管、不叫醒
    return bool(scope) and triggers.private_handled(scope) and triggers.message_has_content(event)


_private_takeover = on_message(priority=45, block=True, rule=Rule(_private_rule))


@_private_takeover.handle()
async def _handle_private(bot: Bot, event: MessageEvent) -> None:
    """私聊接管：**每条私聊都叫醒她**（用户 2026-10-18：私聊功能上线）。

    群里有 @/戳/关键词等触发器；私聊没有 @ 的概念，所以这里直接把每条私聊当成一次叫醒。
    她仍然可以 SILENCE（不想回的就不回）。
    """
    scope = bus.scope_of(event)
    if not scope:
        return
    logger.debug(f"[perception] 私聊接管生效 scope={scope}")
    await triggers.request(scope, "private", "有人私聊你，单独跟你说话")


# ── 运维状态命令 ─────────────────────────────────────────────────────────────
_status = on_command("perception", priority=5, block=True, aliases={"感知"})


@_status.handle()
async def _handle_status(bot: Bot, event: MessageEvent, arg=CommandArg()) -> None:
    """`/perception` 或 `#感知`：看感知层是否健康（仅超级管理员）"""
    from app.services import permissions  # 延迟导入：避免插件目录脱离 app 包时导入失败

    role = await permissions.get_role(str(event.user_id))
    if role != "super_admin":
        return
    cfg = triggers.config()
    health = await bridge.health()
    stats = await bus.stats()
    labels = "、".join(t.label() for t in cfg.triggers) or "无"
    sends = await bus.send_stats()
    lines = [
        f"感知层：{'开启' if cfg.enabled else '关闭'}｜来源 {cfg.source}｜私聊 {cfg.private}",
        f"触发器（{len(cfg.triggers)}）：{labels}",
        f"决策桥 {'在线' if health.get('ok') else '离线'}｜运行 {health.get('running', '-')} 排队 {health.get('queued', '-')}",
        f"池 {stats['messages']} 条｜决策 {stats['decisions']} 次｜作用域 {stats['scopes']}",
        f"默认间隔 {cfg.interval_default_seconds}s｜debounce {cfg.debounce_seconds}s",
        f"武装 {'是' if triggers.is_armed() else '否（只记录）'}｜熔断 "
        f"{'中' if triggers._breaker_tripped() else '无'}｜24h 发送 {sends['24h_ok']}/{sends['24h_sends']}",
        f"表情包 {'开' if cfg.sticker_enabled else '关'}｜专属 {sticker.stats()['own']} 张 / "
        f"公共 {sticker.stats()['public']} 张｜冷却 {cfg.sticker_cooldown_seconds}s",
    ]
    if cfg.warning:
        lines.append(f"⚠️ TRIGGER_WARN: {cfg.warning}")
    await _status.finish("\n".join(lines))


# ── 生命周期 ─────────────────────────────────────────────────────────────────
async def _on_startup() -> None:
    await triggers.start()
    # QZone HTTP 路由（她的 qzone_* 工具用）：FastAPI 就绪后再注册
    try:
        _register_qzone_routes()
    except Exception:  # noqa: BLE001
        logger.exception("[perception] 注册 QZone 路由失败（不影响主流程）")


async def _on_shutdown() -> None:
    # 先存浏览器登录态（否则重启就丢 QZone 会话），再停触发器
    try:
        from . import login as _login

        await _login.shutdown()
    except Exception:  # noqa: BLE001
        logger.exception("[perception] 保存浏览器登录态失败")
    await triggers.stop()


def _register_lifecycle() -> None:
    """注册启动/停止钩子。用 driver 钩子而不是 run_postprocessor，保证定时器随 bot 起来。"""
    import nonebot

    driver = nonebot.get_driver()
    driver.on_startup(_on_startup)
    driver.on_shutdown(_on_shutdown)


try:
    _register_lifecycle()
except Exception:  # noqa: BLE001 - 极端情况下（未 init）不阻断插件导入
    logger.exception("[perception] 注册生命周期钩子失败")


# ── QZone HTTP 接口（她的工具通过桥/直连调用；token 保护）───────────────────────
# 路由挂在 NoneBot 的 FastAPI app 上（8080），只有内网可达。
def _register_qzone_routes() -> None:
    from nonebot import get_driver

    app = get_driver().server_app
    if app is None:
        logger.warning("[perception] 没有 FastAPI app，跳过 qzone 路由")
        return

    def _ok(req: Request) -> bool:
        import pathlib as _p

        try:
            token = _p.Path("/data/qqbot/config/persona-bridge.token").read_text(encoding="utf-8").strip()
        except OSError:
            return False
        auth = req.headers.get("authorization") or ""
        return auth == f"Bearer {token}"

    @app.get("/qzone/feed")
    async def qzone_feed(request: Request, num: int = 10):
        from fastapi.responses import JSONResponse

        from . import qzone

        if not _ok(request):
            return JSONResponse({"ok": False, "error": "unauthorized"}, status_code=401)
        return JSONResponse(await qzone.feed_list(sender.pick_bot(triggers.config().bot_qq), num=num))

    @app.post("/qzone/post")
    async def qzone_post(request: Request):
        from fastapi.responses import JSONResponse

        from . import qzone

        if not _ok(request):
            return JSONResponse({"ok": False, "error": "unauthorized"}, status_code=401)
        body = await request.json()
        content = str(body.get("content") or "").strip()
        if not content:
            return JSONResponse({"ok": False, "error": "content 不能为空"}, status_code=400)
        return JSONResponse(await qzone.post_feed(sender.pick_bot(triggers.config().bot_qq), content))

    @app.post("/qzone/like")
    async def qzone_like(request: Request):
        from fastapi.responses import JSONResponse

        from . import qzone

        if not _ok(request):
            return JSONResponse({"ok": False, "error": "unauthorized"}, status_code=401)
        body = await request.json()
        return JSONResponse(
            await qzone.like_feed(
                sender.pick_bot(triggers.config().bot_qq),
                str(body.get("unikey") or ""),
                bool(body.get("like", True)),
            )
        )

    @app.post("/qzone/comment")
    async def qzone_comment(request: Request):
        from fastapi.responses import JSONResponse

        from . import qzone

        if not _ok(request):
            return JSONResponse({"ok": False, "error": "unauthorized"}, status_code=401)
        body = await request.json()
        return JSONResponse(
            await qzone.comment_feed(
                sender.pick_bot(triggers.config().bot_qq),
                str(body.get("tid") or ""),
                str(body.get("content") or ""),
                str(body.get("uin_ref") or ""),
            )
        )

    @app.get("/qzone/debug")
    async def qzone_debug(request: Request):
        """临时排障：看看 NapCat 到底支持哪些 cookie 相关 API。"""
        from fastapi.responses import JSONResponse

        if not _ok(request):
            return JSONResponse({"ok": False, "error": "unauthorized"}, status_code=401)
        import nonebot as _nb

        bots = {}
        try:
            bots = {str(k): type(v).__name__ for k, v in _nb.get_bots().items()}
        except Exception as exc:  # noqa: BLE001
            bots = {"err": str(exc)}
        bot = sender.pick_bot(triggers.config().bot_qq)
        out = {
            "bot": str(getattr(bot, "self_id", None)),
            "bots": bots,
            "cfg_bot_qq": str(triggers.config().bot_qq),
            "sender_bot_info": str(sender.bot_info(bots and next(iter(_nb.get_bots().values())) or None)),
        }
        if bot is None:
            return JSONResponse(out)
        for api, kwargs in (
            ("get_cookies", {"domain": "qzone.qq.com"}),
            ("get_cookies", {}),
            ("get_cookies", {"domain": "user.qzone.qq.com"}),
            ("get_credentials", {"domain": "qzone.qq.com"}),
        ):
            try:
                out[f"{api}{kwargs}"] = await bot.call_api(api, **kwargs)
            except Exception as exc:  # noqa: BLE001
                out[f"{api}{kwargs}"] = f"ERR {type(exc).__name__}: {exc}"
        return JSONResponse(out)


    @app.post("/qzone/login/start")
    async def qzone_login_start(request: Request):
        """生成登录二维码，并私聊发给主人（她/主人用手机 QQ 扫）。"""
        from fastapi.responses import JSONResponse

        from . import qzone

        if not _ok(request):
            return JSONResponse({"ok": False, "error": "unauthorized"}, status_code=401)
        got = await qzone.qr_start()
        if not got.get("ok"):
            return JSONResponse(got)
        png = qzone.qr_png()
        sent = False
        try:
            bot = sender.pick_bot(triggers.config().bot_qq)
            if bot is not None and png:
                import base64 as _b64

                await bot.call_api(
                    "send_private_msg",
                    user_id=int(os.environ.get("PERSONA_OWNER_QQ", "1000000002")),
                    message=[{"type": "image", "data": {"file": "base64://" + _b64.b64encode(png).decode()}}],
                )
                sent = True
        except Exception as exc:  # noqa: BLE001
            logger.warning(f"[perception] 二维码私聊发送失败：{exc}")
        return JSONResponse({**got, "sent_to_owner": sent})

    @app.post("/browser/login/start")
    async def browser_login_start(request: Request):
        """发起扫码登录：截图二维码 + 私聊发给主人。"""
        from fastapi.responses import JSONResponse

        from . import login as login_mod

        if not _ok(request):
            return JSONResponse({"ok": False, "error": "unauthorized"}, status_code=401)
        body = await request.json()
        service = str(body.get("service") or "qzone")
        got = await login_mod.start(service)
        if not got.get("ok"):
            return JSONResponse(got)
        png_b64 = got.pop("png_b64", "")
        sent = False
        try:
            bot = sender.pick_bot(triggers.config().bot_qq)
            if bot is not None and png_b64:
                await bot.call_api(
                    "send_private_msg",
                    user_id=int(os.environ.get("PERSONA_OWNER_QQ", "1000000002")),
                    message=[{"type": "image", "data": {"file": "base64://" + png_b64}}],
                )
                sent = True
        except Exception as exc:  # noqa: BLE001
            logger.warning(f"[perception] 登录二维码发送失败：{exc}")
        return JSONResponse({**got, "sent_to_owner": sent})

    @app.get("/browser/login/check")
    async def browser_login_check(request: Request, service: str = "qzone"):
        from fastapi.responses import JSONResponse

        from . import login as login_mod

        if not _ok(request):
            return JSONResponse({"ok": False, "error": "unauthorized"}, status_code=401)
        return JSONResponse(await login_mod.check(service))

    @app.get("/browser/login/qr.png")
    async def browser_login_qr(request: Request, token: str = "", service: str = "qzone"):
        from fastapi.responses import JSONResponse, Response

        from . import login as login_mod

        try:
            expect = __import__("pathlib").Path("/data/qqbot/config/persona-bridge.token").read_text(encoding="utf-8").strip()
        except OSError:
            expect = ""
        if token != expect:
            return Response(status_code=401)
        sess = login_mod._SESSIONS.get(service)
        if not sess:
            return Response(status_code=404)
        try:
            from . import login as _l

            await _l._connect()  # 确保 page 仍有效
            png = await sess["page"].screenshot(full_page=False)
        except Exception as exc:  # noqa: BLE001
            return JSONResponse({"ok": False, "error": str(exc)}, status_code=500)
        return Response(content=png, media_type="image/png")

    @app.post("/browser/open")
    async def browser_open(request: Request):
        """通用"打开网页读正文"（服务器上的 headless Chromium）。"""
        from fastapi.responses import JSONResponse

        from . import browser

        if not _ok(request):
            return JSONResponse({"ok": False, "error": "unauthorized"}, status_code=401)
        body = await request.json()
        url = str(body.get("url") or "")
        if not url.startswith("http"):
            return JSONResponse({"ok": False, "error": "need http(s) url"}, status_code=400)
        return JSONResponse(await browser.open_text(url, int(body.get("wait_ms") or 4000)))

    @app.post("/browser/qzone")
    async def browser_qzone(request: Request):
        """QZone 浏览器动作：read / post / like / comment。"""
        from fastapi.responses import JSONResponse

        from . import browser

        if not _ok(request):
            return JSONResponse({"ok": False, "error": "unauthorized"}, status_code=401)
        body = await request.json()
        return JSONResponse(await browser.qzone(
            str(body.get("action") or "read"),
            text=str(body.get("text") or ""),
            limit=int(body.get("limit") or 10),
        ))

    @app.post("/bili/info")
    async def bili_info_route(request: Request):
        """B 站视频信息（她的 bili_info 工具用）：PC 代理优先 → 服务器浏览器兜底。"""
        from fastapi.responses import JSONResponse

        from . import bili

        if not _ok(request):
            return JSONResponse({"ok": False, "error": "unauthorized"}, status_code=401)
        body = await request.json()
        target = str(body.get("target") or body.get("bvid") or body.get("url") or "").strip()
        if not target:
            return JSONResponse({"ok": False, "error": "需要 target（BV 号或 B 站链接）"}, status_code=400)
        data = await bili.info(target)
        if not data:
            return JSONResponse(
                {"ok": False, "error": "解析失败：PC 代理离线，服务器浏览器也没取到（链接可能失效）"},
                status_code=502,
            )
        return JSONResponse({"ok": True, "data": data, "text": bili.format_text(data)})

    @app.post("/bili/action")
    async def bili_action(request: Request):
        """B 站互动/动态/消息统一入口（她的 bili 工具用）。

        动作：like / unlike / favorite / unfavorite / coin / comment /
              post_dynamic / delete_dynamic / messages
        投币是两段式：confirm=false 只返回余额与计划枚数，confirm=true 才真投。
        """
        from fastapi.responses import JSONResponse

        from . import browser

        if not _ok(request):
            return JSONResponse({"ok": False, "error": "unauthorized"}, status_code=401)
        body = await request.json()
        action = str(body.get("action") or "").strip().lower()
        target = str(body.get("target") or "").strip()
        text = str(body.get("text") or "")
        try:
            count = int(body.get("count") or 1)
        except (TypeError, ValueError):
            count = 1
        confirm = bool(body.get("confirm"))
        messages_type = str(body.get("messages_type") or "reply")
        try:
            limit = int(body.get("limit") or 10)
        except (TypeError, ValueError):
            limit = 10
        try:
            if action == "like":
                result = await browser.bili_like(target, True)
            elif action == "unlike":
                result = await browser.bili_like(target, False)
            elif action == "favorite":
                result = await browser.bili_favorite(target, True)
            elif action == "unfavorite":
                result = await browser.bili_favorite(target, False)
            elif action == "coin":
                result = await browser.bili_coin(target, count, confirm)
            elif action == "comment":
                result = await browser.bili_comment(target, text)
            elif action == "post_dynamic":
                result = await browser.bili_post_dynamic(text)
            elif action == "delete_dynamic":
                result = await browser.bili_delete_dynamic(str(body.get("dynamic_id") or target))
            elif action == "watch":
                result = await browser.bili_watch(
                    target,
                    tier=str(body.get("tier") or "flash"),
                    frames=int(body.get("frames") or 4),
                    fps=float(body.get("fps") or 1.0),
                    prompt=str(body.get("prompt") or ""),
                    max_seconds=int(body.get("max_seconds") or 180),
                )
            elif action == "danmaku":
                result = await browser.bili_danmaku(target, limit)
            elif action == "comments":
                result = await browser.bili_comments(target, limit)
            elif action == "messages":
                result = await browser.bili_messages(messages_type, limit)
            else:
                return JSONResponse({"ok": False, "error": f"未知动作 {action}"}, status_code=400)
        except Exception as exc:  # noqa: BLE001
            logger.exception("[perception] B站动作异常")
            return JSONResponse({"ok": False, "error": f"{type(exc).__name__}: {exc}"}, status_code=500)
        return JSONResponse({"ok": bool(result.get("ok")), "action": action,
                             "data": result, "text": _bili_action_text(action, result)})

    @app.post("/debug/onebot")
    async def debug_onebot(request: Request):
        """只读排障：替运维/开发调 OneBot v11 的查询类 API（get_msg / get_forward_msg …）。

        为什么需要它：QQ 卡片里的结构化信息（B站小程序的 qqdocurl、BV 号等）会在
        入池时被截断（bus.record 只存 4KB raw），排障时得回 NapCat 取原始消息。
        只允许查询类 API，绝不暴露发消息/踢人这类写操作。
        """
        from fastapi.responses import JSONResponse

        if not _ok(request):
            return JSONResponse({"ok": False, "error": "unauthorized"}, status_code=401)
        body = await request.json()
        api = str(body.get("api") or "")
        if api not in _DEBUG_APIS:
            return JSONResponse({"ok": False, "error": f"api 不在白名单：{api}"}, status_code=400)
        params = body.get("params") or {}
        if not isinstance(params, dict):
            return JSONResponse({"ok": False, "error": "params 必须是对象"}, status_code=400)
        bot = sender.pick_bot(triggers.config().bot_qq)
        if bot is None:
            return JSONResponse({"ok": False, "error": "没有在线 bot"}, status_code=503)
        try:
            data = await bot.call_api(api, **params)
            return JSONResponse({"ok": True, "api": api, "data": data})
        except Exception as exc:  # noqa: BLE001
            return JSONResponse({"ok": False, "error": f"{type(exc).__name__}: {exc}"})

    @app.post("/media/understand")
    async def media_understand(request: Request):
        """统一多模态理解：图像 / 音频 / 视频。tier=asr|flash|pro，frames/fps/prompt 可调。"""
        from fastapi.responses import JSONResponse

        from . import media_ai

        if not _ok(request):
            return JSONResponse({"ok": False, "error": "unauthorized"}, status_code=401)
        body = await request.json()
        url = str(body.get("url") or "")
        media_id = body.get("media_id")
        hint = str(body.get("kind") or "")
        if not url and media_id:
            row = await (await bus._db().execute(
                "SELECT url, kind, fetched_path FROM media WHERE id=?", (int(media_id),)
            )).fetchone()
            if not row:
                return JSONResponse({"ok": False, "error": f"media #{media_id} 不存在"}, status_code=404)
            url = str(row["fetched_path"] or row["url"] or "")
            hint = hint or str(row["kind"] or "")
        if not url:
            return JSONResponse({"ok": False, "error": "need url or media_id"}, status_code=400)
        got = await media_ai.understand(
            url,
            tier=str(body.get("tier") or "flash"),
            hint=hint,
            prompt=str(body.get("prompt") or ""),
            frames=int(body.get("frames") or 4),
            fps=float(body.get("fps") or 1),
        )
        return JSONResponse(got)

    @app.post("/stickers/tag")
    async def sticker_tag(request: Request):
        """给一张表情图片做视觉标注（她的 sticker_save 工具用；token 保护）。

        输入：{image_b64, mime?, tier?}；输出：{ok, tags:{caption,emotions,usage_tags,
        attitudes,anti_tags,keywords,safety_risk}}。模型只出 JSON，服务端再按 ontology 清洗。
        """
        import base64 as _b64
        import json as _json
        import os as _os
        import re as _re
        import tempfile as _tempfile

        from fastapi.responses import JSONResponse

        from . import media_ai

        if not _ok(request):
            return JSONResponse({"ok": False, "error": "unauthorized"}, status_code=401)
        try:
            body = await request.json()
        except Exception:  # noqa: BLE001
            return JSONResponse({"ok": False, "error": "invalid json"}, status_code=400)
        raw_b64 = str(body.get("image_b64") or "")
        mime = str(body.get("mime") or "image/jpeg").lower()
        tier = str(body.get("tier") or "flash")
        if not raw_b64:
            return JSONResponse({"ok": False, "error": "need image_b64"}, status_code=400)
        if len(raw_b64) > 16 * 1024 * 1024:
            return JSONResponse({"ok": False, "error": "图片太大（base64 超过 16MB）"}, status_code=413)
        if not mime.startswith("image/"):
            return JSONResponse({"ok": False, "error": "只支持图片"}, status_code=400)
        try:
            data = _b64.b64decode(raw_b64, validate=True)
        except Exception:  # noqa: BLE001
            return JSONResponse({"ok": False, "error": "bad base64"}, status_code=400)
        if len(data) > 12 * 1024 * 1024:
            return JSONResponse({"ok": False, "error": "图片太大（>12MB）"}, status_code=413)
        suffix = {"image/jpeg": ".jpg", "image/png": ".png", "image/gif": ".gif", "image/webp": ".webp"}.get(mime, ".jpg")
        # 读 ontology（bot 容器有 /data/qqbot/config）
        allowed: dict[str, list[str]] = {}
        try:
            onto = _json.loads(Path("/data/qqbot/config/sticker-tag-ontology.v1.json").read_text(encoding="utf-8"))
            for field, words in (onto.get("canonical") or {}).items():
                allowed[str(field)] = [str(x) for x in (words or [])]
        except Exception as exc:  # noqa: BLE001
            logger.warning(f"[perception] sticker ontology 读取失败：{exc}")
        prompt = "\n".join([
            "你是聊天表情包标注器。看这张图，只返回一个 JSON 对象（不要 markdown、不要解释）：",
            '{"caption":"20字以内画面描述","emotions":["..."],"usage_tags":["..."],'
            '"attitudes":["..."],"anti_tags":["..."],"keywords":["..."],'
            '"safety_risk":"low|medium|high"}',
            "规则：emotions/usage_tags/attitudes/anti_tags 只能从给定词表里选，每类最多 3 个，"
            "没把握就留空；caption 要写画面本身，不要写“表情包”三个字；anti_tags 写不适合的场景。",
            f"emotions 词表：{'、'.join(allowed.get('emotions') or [])}",
            f"usage_tags 词表：{'、'.join(allowed.get('usage_tags') or [])}",
            f"attitudes 词表：{'·'.join(allowed.get('attitudes') or [])}",
            "safety_risk：色情/血腥/极端政治/隐私泄露=high；擦边/轻微攻击=medium；正常=low。",
        ])
        fd, path = _tempfile.mkstemp(prefix="sticker-tag-", suffix=suffix)
        try:
            with _os.fdopen(fd, "wb") as fh:
                fh.write(data)
            result = await media_ai.understand(path, tier=tier, hint="image", prompt=prompt)
        finally:
            try:
                _os.unlink(path)
            except OSError:
                pass
        if not result.get("ok"):
            return JSONResponse({"ok": False, "error": str(result.get("error") or "vision failed")}, status_code=502)
        text = str(result.get("text") or "")
        match = _re.search(r"\{.*\}", text, _re.S)
        try:
            obj = _json.loads(match.group(0) if match else text)
        except Exception:  # noqa: BLE001
            obj = {}
        def clean_list(field: str, maxn: int = 3) -> list[str]:
            vals = obj.get(field) or []
            if isinstance(vals, str):
                vals = _re.split(r"[,，、;；\s]+", vals)
            out: list[str] = []
            allowed_set = set(allowed.get(field) or [])
            for v in vals if isinstance(vals, list) else []:
                v = str(v).strip()
                if v and (not allowed_set or v in allowed_set) and v not in out:
                    out.append(v)
                if len(out) >= maxn:
                    break
            return out
        caption = str(obj.get("caption") or "").strip()[:80]
        risk = str(obj.get("safety_risk") or "low").lower()
        if risk not in {"low", "medium", "high"}:
            risk = "low"
        tags = {
            "caption": caption,
            "emotions": clean_list("emotions"),
            "usage_tags": clean_list("usage_tags"),
            "attitudes": clean_list("attitudes"),
            "anti_tags": clean_list("anti_tags"),
            "keywords": (obj.get("keywords") if isinstance(obj.get("keywords"), list) else [])[:6],
            "safety_risk": risk,
        }
        return JSONResponse({"ok": True, "tags": tags, "model": result.get("model"), "raw_text": text[:800]})

    @app.get("/messages")
    async def perception_messages(request: Request, scope: str = "", limit: int = 20, before_seq: int = 0):
        """详细消息拉取（她的 chat_detail 工具用）：带 QQ / 昵称 / 群名 / 时间 / 正文。"""
        from fastapi.responses import JSONResponse

        if not _ok(request):
            return JSONResponse({"ok": False, "error": "unauthorized"}, status_code=401)
        limit = max(1, min(int(limit), 50))
        db = bus._db()
        sql = "SELECT seq, scope, user_id, COALESCE(nickname,'') AS nickname, COALESCE(group_name,'') AS group_name, text, ts FROM messages"
        args: list = []
        conds = []
        if scope and scope != "all":
            if scope.startswith(("group:", "private:")):
                conds.append("scope = ?")
                args.append(scope)
            else:
                # 允许按群名/昵称模糊查（她不知道群号）
                conds.append("(scope LIKE ? OR COALESCE(group_name,'') LIKE ?)")
                args.extend([f"%{scope}%", f"%{scope}%"])
        if before_seq:
            conds.append("seq < ?")
            args.append(int(before_seq))
        if conds:
            sql += " WHERE " + " AND ".join(conds)
        sql += " ORDER BY seq DESC LIMIT ?"
        args.append(limit)
        rows = await (await db.execute(sql, tuple(args))).fetchall()
        out = []
        for r in rows:
            text = str(r["text"] or "")
            body = text.split("]: ", 1)[1] if "]: " in text else text
            out.append({
                "seq": r["seq"],
                "scope": r["scope"],
                "qq": r["user_id"],
                "nickname": r["nickname"],
                "group": r["group_name"],
                "ts": r["ts"],
                "time": __import__("datetime").datetime.fromtimestamp(r["ts"]).strftime("%m-%d %H:%M"),
                "text": body,
            })
        return JSONResponse({"ok": True, "count": len(out), "messages": out})

    @app.get("/stats")
    async def perception_stats(request: Request, hours: int = 24):
        """运行数据（她/主人的 /ops 式统计）：决策数、token、缓存命中、出站数。"""
        from fastapi.responses import JSONResponse

        if not _ok(request):
            return JSONResponse({"ok": False, "error": "unauthorized"}, status_code=401)
        import datetime as _dt
        import json as _json

        since = int((_dt.datetime.now() - _dt.timedelta(hours=max(1, min(hours, 720)))).timestamp())
        db = bus._db()
        rows = await (await db.execute(
            "SELECT trigger, action, input_tokens, elapsed_ms, usage_json FROM decisions WHERE created_at >= ?",
            (since,),
        )).fetchall()
        total_in = sum(int(r["input_tokens"] or 0) for r in rows)
        cache_read = 0
        cache_write = 0
        out_tokens = 0
        for r in rows:
            try:
                u = _json.loads(r["usage_json"] or "{}")
            except Exception:  # noqa: BLE001
                continue
            cache_read += int(u.get("cacheReadTokens") or 0)
            cache_write += int(u.get("cacheWriteTokens") or 0)
            out_tokens += int(u.get("outputTokens") or 0)
        sends = await (await db.execute(
            "SELECT COUNT(*) AS n, SUM(ok) AS ok FROM perception_sends WHERE created_at >= ?", (since,)
        )).fetchone()
        speak = sum(1 for r in rows if r["action"] == "speak")
        return JSONResponse({
            "ok": True,
            "hours": hours,
            "decisions": len(rows),
            "speak": speak,
            "silence": len(rows) - speak,
            "input_tokens": total_in,
            "output_tokens": out_tokens,
            "cache_read_tokens": cache_read,
            "cache_write_tokens": cache_write,
            "cache_hit_rate": round(cache_read / (input_tokens_total := (total_in + cache_read)) * 100, 1) if (total_in + cache_read) else 0,
            "avg_elapsed_ms": round(sum(int(r["elapsed_ms"] or 0) for r in rows) / len(rows)) if rows else 0,
            "sends": int(sends["n"] or 0),
            "sends_ok": int(sends["ok"] or 0),
        })

    @app.post("/debug/trigger")
    async def debug_trigger(request: Request):
        """手动叫醒一次（仅本地运维测试用，token 保护）：让她执行一次决策。"""
        from fastapi.responses import JSONResponse

        if not _ok(request):
            return JSONResponse({"ok": False, "error": "unauthorized"}, status_code=401)
        try:
            body = await request.json()
        except Exception:  # noqa: BLE001
            body = {}
        scope = str(body.get("scope") or "").strip()
        reason = str(body.get("reason") or "手动测试叫醒").strip()
        if not scope.startswith(("group:", "private:")):
            return JSONResponse({"ok": False, "error": "scope 必须是 group:... 或 private:..."}, status_code=400)
        await triggers.request(scope, "private" if scope.startswith("private:") else "mention", reason)
        return JSONResponse({"ok": True, "scope": scope, "reason": reason, "queued": True})

    @app.post("/debug/sendfile")
    async def debug_sendfile(request: Request):
        """直连发送一个文件（仅本地运维测试用，token 保护）：绕过决策验证 sender/NapCat。"""
        from fastapi.responses import JSONResponse

        if not _ok(request):
            return JSONResponse({"ok": False, "error": "unauthorized"}, status_code=401)
        try:
            body = await request.json()
        except Exception:  # noqa: BLE001
            body = {}
        scope = str(body.get("scope") or "").strip()
        path = str(body.get("path") or "").strip()
        name = str(body.get("name") or "").strip()
        if not scope.startswith(("group:", "private:")) or not path:
            return JSONResponse({"ok": False, "error": "need scope + path"}, status_code=400)
        bot = sender.pick_bot()
        if bot is None:
            return JSONResponse({"ok": False, "error": "没有在线 bot"}, status_code=503)
        ok = await sender.send_file(bot, scope, path, name)
        return JSONResponse({"ok": bool(ok), "scope": scope, "path": path, "name": name})

    @app.post("/interact")
    async def interaction(request: Request):
        """戳一戳/点赞计划（她的 interact 工具用；token 保护）。"""
        from fastapi.responses import JSONResponse

        from . import interact as interact_mod

        if not _ok(request):
            return JSONResponse({"ok": False, "error": "unauthorized"}, status_code=401)
        try:
            body = await request.json()
        except Exception:  # noqa: BLE001
            body = {}
        scope = str(body.get("scope") or "").strip()
        group_id = sender.group_id_of(scope) if scope.startswith("group:") else None
        if group_id is None and body.get("group_id"):
            try:
                group_id = int(body.get("group_id"))
            except (TypeError, ValueError):
                group_id = None
        result = await interact_mod.start(
            str(body.get("action") or ""),
            str(body.get("target") or ""),
            group_id=group_id,
            times=int(body.get("times") or 1),
            interval_seconds=int(body.get("interval_seconds") or 0),
            start_delay_seconds=int(body.get("start_delay_seconds") or 0),
        )
        return JSONResponse(result)

    @app.get("/interact/status")
    async def interaction_status(request: Request, job_id: str = ""):
        """查询互动计划状态（立即动作不会创建 job）。"""
        from fastapi.responses import JSONResponse

        from . import interact as interact_mod

        if not _ok(request):
            return JSONResponse({"ok": False, "error": "unauthorized"}, status_code=401)
        return JSONResponse(interact_mod.status(job_id or None))

    @app.get("/qzone/login/qr.png")
    async def qzone_login_qr(request: Request, token: str = ""):
        """把当前二维码原图吐出来（方便在没有 QQ 私聊通道时直接看）。"""
        from fastapi.responses import Response

        from . import qzone

        try:
            expect = __import__("pathlib").Path("/data/qqbot/config/persona-bridge.token").read_text(encoding="utf-8").strip()
        except OSError:
            expect = ""
        if token != expect:
            return Response(status_code=401)
        png = qzone.qr_png()
        if not png:
            return Response(status_code=404)
        return Response(content=png, media_type="image/png")

    @app.get("/qzone/login/poll")
    async def qzone_login_poll(request: Request):
        from fastapi.responses import JSONResponse

        from . import qzone

        if not _ok(request):
            return JSONResponse({"ok": False, "error": "unauthorized"}, status_code=401)
        return JSONResponse(await qzone.qr_poll())
    logger.info("[perception] QZone 路由已注册：/qzone/feed /qzone/post /qzone/like /qzone/comment")
