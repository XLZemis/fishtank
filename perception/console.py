"""感知层 · 控制台（console.py）

背景（2026-10-25）：
    旧架构的 /ask、/ops、/schedule、/ping 等命令随旧 bot 平台一起封存。
    主人准备用**另一个 QQ 号**作为Persona的控制台：私聊她发命令，不进入人格对话。

设计：
    · 只处理私聊；群聊里谁发都不认。
    · 只认白名单 QQ（`FISHTANK_CONSOLE_QQS`，逗号分隔的号码）。
    · 只认以 `/` 开头的已知命令；其余内容原样放给人格（她正常聊天）。
    · `/perception` / `#感知` 保留给感知层原有命令，不在这里拦截。
    · 每条命令的回复都走 sender.send_notice，进现有审计链路。

命令：
    /help                  帮助
    /status                感知层 + 决策桥 + 武装状态
    /stats [小时]          消息/决策/发送统计（默认 24h）
    /pause <scope> [分钟]  暂停某个 scope（默认 30 分钟）
    /resume <scope>        恢复某个 scope
    /scopes                列出已知 scope
"""
from __future__ import annotations

import os
import re
import time
from dataclasses import dataclass

from nonebot import logger

from . import bridge, bus, triggers

# 已知命令（顺序用于 /help）
KNOWN = ("help", "status", "stats", "pause", "resume", "scopes")

# 这些命令属于感知层原有命令（owner-only），控制台不拦截
RESERVED = ("perception", "感知")

_SCOPE_RE = re.compile(r"^(group|private):\d+$")
_CONSOLE_ENV = "FISHTANK_CONSOLE_QQS"


def console_qqs(env: dict[str, str] | None = None) -> frozenset[str]:
    """读控制台白名单；空 = 控制台关闭（默认）。"""
    raw = (env if env is not None else os.environ).get(_CONSOLE_ENV, "")
    return frozenset(x.strip() for x in str(raw).split(",") if x.strip().isdigit())


def parse_command(text: str) -> tuple[str, list[str]]:
    """把 `/pause group:1 30` 解析成 ("pause", ["group:1", "30"])。"""
    body = str(text or "").strip()
    if not body.startswith("/"):
        return "", []
    parts = body[1:].split()
    if not parts:
        return "", []
    cmd = parts[0].lower()
    return cmd, parts[1:]


def should_handle(user_id: str | int, text: str, *, allowed: frozenset[str] | None = None) -> bool:
    """是否是本模块要处理的控制台命令（纯函数，便于单测）。"""
    allowed = console_qqs() if allowed is None else allowed
    if str(user_id) not in allowed:
        return False
    cmd, _ = parse_command(text)
    if not cmd or cmd in RESERVED:
        return False
    return cmd in KNOWN


def _fmt_ts(ts: int | float | None) -> str:
    if not ts:
        return "-"
    return time.strftime("%m-%d %H:%M", time.localtime(int(ts)))


async def _list_scopes() -> list[str]:
    """从数据库读已知 scope（按最近活跃排序）。"""
    rows = await bus.recent_scopes() if hasattr(bus, "recent_scopes") else []
    return [str(r.get("scope")) for r in rows if str(r.get("scope") or "")]


async def _status_text() -> str:
    cfg = triggers.config()
    health = await bridge.health()
    stats = await bus.stats()
    sends = await bus.send_stats()
    return "\n".join([
        f"感知层：{'开启' if cfg.enabled else '关闭'}｜来源 {cfg.source}｜私聊 {cfg.private}",
        f"触发器 {len(cfg.triggers)} 个｜武装 {'是' if triggers.is_armed() else '否（只记录）'}",
        f"决策桥 {'在线' if health.get('ok') else '离线'}｜运行 {health.get('running', '-')} 排队 {health.get('queued', '-')}",
        f"池 {stats['messages']} 条｜决策 {stats['decisions']} 次｜作用域 {stats['scopes']}",
        f"24h 出站 {sends['24h_ok']}/{sends['24h_sends']}",
    ])


async def _stats_text(hours: int = 24) -> str:
    hours = max(1, min(int(hours or 24), 24 * 14))
    stats = await bus.stats()
    sends = await bus.send_stats()
    return "\n".join([
        f"近 {hours}h：消息 {stats['messages']}｜决策 {stats['decisions']}｜作用域 {stats['scopes']}",
        f"发送：24h {sends['24h_ok']}/{sends['24h_sends']}｜累计 {sends['ok']}/{sends['total']}",
    ])


async def _pause(scope: str, minutes: int = 30) -> str:
    if not _SCOPE_RE.match(scope):
        return "scope 形状不对：应为 group:<群号> 或 private:<QQ>"
    minutes = max(1, min(int(minutes or 30), 24 * 60))
    until = bus.now_ts() + minutes * 60
    await bus.kv_set(f"pause_until:{scope}", str(until))
    logger.info(f"[perception] 控制台暂停 scope={scope} until={until}")
    return f"已暂停 {scope} 到 {_fmt_ts(until)}"


async def _resume(scope: str) -> str:
    if not _SCOPE_RE.match(scope):
        return "scope 形状不对：应为 group:<群号> 或 private:<QQ>"
    await bus.kv_set(f"pause_until:{scope}", "0")
    logger.info(f"[perception] 控制台恢复 scope={scope}")
    return f"已恢复 {scope}"


async def dispatch(text: str) -> str:
    """执行一条控制台命令，返回要发回去的文本。永不抛异常。"""
    cmd, args = parse_command(text)
    try:
        if cmd == "help" or not cmd:
            return "\n".join([
                "Persona控制台：",
                "/status — 感知层/桥/武装状态",
                "/stats [小时] — 消息与发送统计",
                "/pause <scope> [分钟] — 暂停一个群/私聊",
                "/resume <scope> — 恢复",
                "/scopes — 已知作用域",
            ])
        if cmd == "status":
            return await _status_text()
        if cmd == "stats":
            hours = int(args[0]) if args and args[0].isdigit() else 24
            return await _stats_text(hours)
        if cmd == "pause":
            if not args:
                return "用法：/pause group:<群号> [分钟]"
            minutes = int(args[1]) if len(args) > 1 and args[1].isdigit() else 30
            return await _pause(args[0], minutes)
        if cmd == "resume":
            if not args:
                return "用法：/resume group:<群号>"
            return await _resume(args[0])
        if cmd == "scopes":
            scopes = await _list_scopes()
            return "已知作用域：\n" + ("\n".join(scopes[:30]) if scopes else "（空）")
        return f"未知命令：{cmd or '(空)'}（/help 看用法）"
    except Exception as exc:  # noqa: BLE001 - 控制台不能把主流程带崩
        logger.exception(f"[perception] 控制台命令失败 text={text[:80]}")
        return f"命令执行失败：{type(exc).__name__}: {exc}"
