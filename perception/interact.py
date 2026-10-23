"""互动动作（戳一戳 / 点赞）的计划执行器。

设计（用户 2026-10-23）：
    · 一个工具同时支持 poke / like，并允许「延迟开始 + 每隔 N 秒重复 M 次」；
    · 立即动作同步返回结果，计划动作交给本模块的 asyncio 任务执行；
    · 每次执行都重新做 G1 身份断言（sender.pick_bot），不缓存旧 bot；
    · 不落库、不跨重启；任务量有硬上限，防止一次决策把主进程拖死。

NapCat 接口：
    send_poke  {user_id, group_id?, target_id?}   group_id 有值=群戳，否则私聊戳
    send_like  {user_id, times}
"""

from __future__ import annotations

import asyncio
import re
import time
from typing import Any

from nonebot import logger

from . import sender

MAX_TIMES = 20
MAX_SPAN_SECONDS = 3600
MAX_ACTIVE_JOBS = 20
MIN_INTERVAL_IF_REPEAT = 3

# job_id → 简要状态（只保留最近 100 条；不落库）
_JOBS: dict[str, dict[str, Any]] = {}
_TASKS: dict[str, asyncio.Task] = {}
# NapCat/QQ 对短时间连戳/连赞会静默丢弃；这里串行化并强制最小间隔。
_ACTION_LOCK = asyncio.Lock()
_MIN_ACTION_GAP_SECONDS = 3.0
_last_action_at = 0.0


def _target_ok(target: str) -> bool:
    return bool(re.fullmatch(r"[1-9]\d{4,11}", str(target or "")))


async def _execute_once(action: str, target: str, group_id: int | None) -> dict[str, Any]:
    """执行一次互动；每次重新取 bot，避免使用已断开的旧连接。"""
    # 必须按配置的 bot_qq 取 Bot；nonebot.get_bots() 里还有 Minecraft 等适配器，
    # 不传 bot_qq 可能拿到 Minecraft Bot，call_api 会报 ApiNotAvailable。
    from . import triggers

    bot = sender.pick_bot(getattr(triggers._config, "bot_qq", None) or None)
    if bot is None:
        raise RuntimeError("QQ bot 不在线（G1 拒绝发送）")
    global _last_action_at
    async with _ACTION_LOCK:
        gap = _MIN_ACTION_GAP_SECONDS - (time.monotonic() - _last_action_at)
        if gap > 0:
            await asyncio.sleep(gap)
        if action == "poke":
            params: dict[str, Any] = {"user_id": str(target)}
            if group_id:
                params["group_id"] = str(group_id)
                params["target_id"] = str(target)
            await bot.call_api("send_poke", **params)
        elif action == "like":
            # 一次只点 1 个；重复由计划器负责，避免一口气把次数点满
            await bot.call_api("send_like", user_id=str(target), times=1)
        else:
            raise ValueError(f"未知 action: {action}")
        _last_action_at = time.monotonic()
    return {"action": action, "target": str(target), "group_id": group_id, "at": int(time.time())}


async def _run_job(job_id: str, action: str, target: str, group_id: int | None,
                   times: int, interval: int, delay: int) -> None:
    job = _JOBS[job_id]
    job["status"] = "running"
    failures = 0
    try:
        if delay > 0:
            await asyncio.sleep(delay)
        for index in range(times):
            if _TASKS.get(job_id) is not asyncio.current_task():
                job["status"] = "cancelled"
                return
            try:
                result = await _execute_once(action, target, group_id)
                job["done"] += 1
                job["last"] = result
                logger.info(
                    f"[perception] 互动 {action} target={target} group={group_id} "
                    f"({index + 1}/{times}) 成功"
                )
            except Exception as exc:  # noqa: BLE001
                failures += 1
                job["failures"] = failures
                job["last_error"] = str(exc)[:200]
                logger.warning(
                    f"[perception] 互动 {action} target={target} 第 {index + 1} 次失败：{exc}"
                )
                if failures >= 2:
                    job["status"] = "failed"
                    return
            if index < times - 1 and interval > 0:
                await asyncio.sleep(interval)
        job["status"] = "done"
        job["finished_at"] = int(time.time())
    except asyncio.CancelledError:
        job["status"] = "cancelled"
        raise
    except Exception as exc:  # noqa: BLE001
        job["status"] = "failed"
        job["last_error"] = str(exc)[:200]
        logger.exception(f"[perception] 互动任务异常 job={job_id}: {exc}")
    finally:
        _TASKS.pop(job_id, None)


async def start(action: str, target: str, *, group_id: int | None = None,
                times: int = 1, interval_seconds: int = 0,
                start_delay_seconds: int = 0) -> dict[str, Any]:
    """启动一个互动计划。立即动作会等执行完再返回；计划动作返回 job_id。"""
    action = str(action or "").strip().lower()
    target = str(target or "").strip()
    if action not in {"poke", "like"}:
        return {"ok": False, "error": "action 只能是 poke 或 like"}
    if not _target_ok(target):
        return {"ok": False, "error": "target 必须是 5–12 位 QQ 号"}
    times = max(1, min(int(times or 1), MAX_TIMES))
    interval_seconds = max(0, min(int(interval_seconds or 0), MAX_SPAN_SECONDS))
    start_delay_seconds = max(0, min(int(start_delay_seconds or 0), MAX_SPAN_SECONDS))
    warning = ""
    if times > 1 and interval_seconds < MIN_INTERVAL_IF_REPEAT:
        interval_seconds = MIN_INTERVAL_IF_REPEAT
        warning = "重复动作的 interval 小于 3 秒，已按 3 秒执行"
    span = start_delay_seconds + max(0, times - 1) * interval_seconds
    if span > MAX_SPAN_SECONDS:
        return {"ok": False, "error": f"计划跨度过长（{span}s，上限 {MAX_SPAN_SECONDS}s）"}
    if len(_TASKS) >= MAX_ACTIVE_JOBS:
        return {"ok": False, "error": f"同时进行的互动计划太多（上限 {MAX_ACTIVE_JOBS}）"}

    # 单次立即动作：直接执行，给调用方明确结果
    if times == 1 and interval_seconds == 0 and start_delay_seconds == 0:
        try:
            result = await _execute_once(action, target, group_id)
            return {"ok": True, "executed": True, "result": result, "warning": warning}
        except Exception as exc:  # noqa: BLE001
            logger.warning(f"[perception] 立即互动失败 {action} target={target}: {exc}")
            return {"ok": False, "error": str(exc)[:200]}

    job_id = f"{action}-{target}-{int(time.time())}-{len(_JOBS) % 1000}"
    now = int(time.time())
    job = {
        "job_id": job_id,
        "action": action,
        "target": target,
        "group_id": group_id,
        "times": times,
        "done": 0,
        "failures": 0,
        "interval_seconds": interval_seconds,
        "start_delay_seconds": start_delay_seconds,
        "first_at": now + start_delay_seconds,
        "last_at": now + span,
        "status": "scheduled",
        "created_at": now,
    }
    _JOBS[job_id] = job
    if len(_JOBS) > 100:
        for key in list(_JOBS)[: max(0, len(_JOBS) - 100)]:
            _JOBS.pop(key, None)
    task = asyncio.create_task(_run_job(
        job_id, action, target, group_id, times, interval_seconds, start_delay_seconds
    ))
    _TASKS[job_id] = task
    task.add_done_callback(lambda _t: _TASKS.pop(job_id, None))
    logger.info(
        f"[perception] 互动计划 {job_id}: {action} target={target} group={group_id} "
        f"times={times} interval={interval_seconds}s delay={start_delay_seconds}s"
    )
    return {"ok": True, "executed": False, "job": job, "warning": warning}


def status(job_id: str | None = None) -> dict[str, Any]:
    if job_id:
        job = _JOBS.get(str(job_id))
        if not job:
            return {"ok": False, "error": "job 不存在（可能已过期或重启）"}
        return {"ok": True, "job": job}
    return {"ok": True, "jobs": list(_JOBS.values()), "active": len(_TASKS)}
