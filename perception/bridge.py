"""感知层 · 决策桥客户端（bridge.py）

部署文档 P4 原本写的是「bridge.py 用 subprocess 调 dsh」。实测在 NoneBot 容器里不可行：
    $ docker exec qqbot-nonebot sh -lc "which dsh node npm"
    → 只有 /usr/local/bin/python3；也没有 /var/run/docker.sock
所以 subprocess 那一层搬到了人格容器（dsh-persona 的 bridge/server.mjs），
本模块只做一件事：把决策请求 HTTP POST 过去，拿结构化结果回来。

协议见 /data/persona/bridge/server.mjs 顶部注释：
    POST /decide  {group_id, prompt, session_id, timeout_ms}
    → {ok, action: speak|silence, messages[], next_check, session_id, usage, elapsed_ms, ...}
"""

from __future__ import annotations

import os
from pathlib import Path
from typing import Any

import httpx
from nonebot import logger

from .settings import SETTINGS

# 容器网络内的服务名 / token 路径统一由 settings 管理（见 docs/configuration.md）
BRIDGE_URL = SETTINGS.bridge_url
TOKEN_FILE = SETTINGS.bridge_token_file


def _token() -> str:
    """每次调用现读：token 轮换后无需重启 NoneBot"""
    try:
        return TOKEN_FILE.read_text(encoding="utf-8").strip()
    except FileNotFoundError:
        logger.error(f"[perception] 找不到决策桥 token 文件：{TOKEN_FILE}")
        return ""


async def health() -> dict[str, Any]:
    """探活：给 /perception status 命令与启动自检用"""
    try:
        async with httpx.AsyncClient(timeout=5.0) as client:
            resp = await client.get(f"{BRIDGE_URL}/health")
            return resp.json()
    except Exception as exc:  # noqa: BLE001 - 探活失败就是失败，不需要细分
        return {"ok": False, "error": str(exc)}


async def fetch_triggers() -> dict[str, Any]:
    """取她的触发器配置（GET /triggers.yaml）。

    为什么走桥而不是直接挂文件：主机侧若用"原子写"（temp+rename）更新，
    bind mount 的单个文件会一直指向旧 inode；每次真实 HTTP 读则永远是最新内容。
    返回 {ok, text, sha, mtime} 或 {ok: False, error}。
    """
    token = _token()
    if not token:
        return {"ok": False, "error": "missing bridge token"}
    try:
        async with httpx.AsyncClient(timeout=8.0) as client:
            resp = await client.get(f"{BRIDGE_URL}/triggers.yaml", headers={"authorization": f"Bearer {token}"})
            return resp.json()
    except Exception as exc:  # noqa: BLE001 - 取配置失败由 triggers 层降级处理
        return {"ok": False, "error": f"{type(exc).__name__}: {exc}"}


async def decide(
    *,
    scope: str,
    prompt: str,
    session_id: str | None = None,
    timeout_ms: int = 45000,
    raw: bool = False,
) -> dict[str, Any]:
    """请求一次决策。

    返回统一结构（失败也返回 dict，绝不抛异常给调用方——感知层不能把生产链路带崩）：
        {ok, action, messages, next_check, session_id, usage, elapsed_ms, error}
    """
    token = _token()
    if not token:
        return {"ok": False, "action": "error", "error": "missing bridge token", "messages": []}

    payload = {
        "group_id": scope,
        "prompt": prompt,
        "session_id": session_id,
        # 桥侧默认 45s；这里多给 5s 让"桥内部超时"先生效，client 超时只做兜底
        "timeout_ms": timeout_ms,
        # raw=True → 桥只回原文（不做 SPEAK 解析、不回滚会话）；
        # 表情包那两轮（STICKER:/SEND:）用它，避免把控制行当成群消息发出去
        "parse": not raw,
    }
    headers = {"authorization": f"Bearer {token}"}
    try:
        # httpx 的 timeout 要留出桥内部 kill 进程的余量
        async with httpx.AsyncClient(timeout=(timeout_ms / 1000) + 15) as client:
            resp = await client.post(f"{BRIDGE_URL}/decide", json=payload, headers=headers)
            data = resp.json()
            if resp.status_code != 200:
                data.setdefault("ok", False)
                data.setdefault("action", "error")
                logger.warning(f"[perception] 决策桥返回 {resp.status_code}: {str(data)[:300]}")
            # 主回复里可能带附带动作（STICKER/ATTACH/LINK/FORWARD）：统一成 [action, arg] 列表
            data.setdefault("actions", [])
            return data
    except Exception as exc:  # noqa: BLE001
        logger.exception(f"[perception] 决策桥调用失败 scope={scope}")
        return {"ok": False, "action": "error", "error": str(exc), "messages": []}


async def fetch_sticker_manifest() -> dict[str, Any]:
    """取她专属表情包库的清单（GET /stickers/manifest）"""
    token = _token()
    if not token:
        return {"ok": False, "error": "missing bridge token"}
    try:
        async with httpx.AsyncClient(timeout=20.0) as client:
            resp = await client.get(f"{BRIDGE_URL}/stickers/manifest", headers={"authorization": f"Bearer {token}"})
            return resp.json()
    except Exception as exc:  # noqa: BLE001
        return {"ok": False, "error": f"{type(exc).__name__}: {exc}"}


async def fetch_sticker_file(sticker_id: str) -> bytes:
    """按 id 取单张表情包字节（GET /stickers/file?id=…）；失败返回空字节"""
    token = _token()
    if not token:
        return b""
    try:
        async with httpx.AsyncClient(timeout=20.0) as client:
            resp = await client.get(
                f"{BRIDGE_URL}/stickers/file",
                params={"id": sticker_id},
                headers={"authorization": f"Bearer {token}"},
            )
            if resp.status_code != 200:
                return b""
            return resp.content
    except Exception as exc:  # noqa: BLE001
        logger.warning(f"[perception] 取表情包文件失败 id={sticker_id}: {exc}")
        return b""


async def fetch_media(media_id: int | str, url: str, ext: str = "jpg") -> dict[str, Any]:
    """让桥把群消息里的图片/视频下载到人格容器的 /persona/media/<id>.<ext>。"""
    token = _token()
    if not token:
        return {"ok": False, "error": "missing bridge token"}
    try:
        async with httpx.AsyncClient(timeout=40.0) as client:
            resp = await client.post(
                f"{BRIDGE_URL}/media/fetch",
                json={"id": str(media_id), "url": url, "ext": ext},
                headers={"authorization": f"Bearer {token}"},
            )
            return resp.json()
    except Exception as exc:  # noqa: BLE001
        return {"ok": False, "error": f"{type(exc).__name__}: {exc}"}


async def stage_file(path: str, name: str = "") -> dict[str, Any]:
    """把 /persona/refs|stickers|media 下的文件复制到 /persona/out（发文件用）。

    为什么不在感知层直接读：感知层容器只挂了 exports/db/config 等目录，
    看不到 /persona/refs；桥跑在人格容器里，能安全地做白名单校验 + 复制。
    """
    token = _token()
    if not token:
        return {"ok": False, "error": "missing bridge token"}
    try:
        async with httpx.AsyncClient(timeout=30.0) as client:
            resp = await client.post(
                f"{BRIDGE_URL}/file/stage",
                json={"path": str(path or ""), "name": str(name or "")},
                headers={"authorization": f"Bearer {token}"},
            )
            return resp.json()
    except Exception as exc:  # noqa: BLE001
        return {"ok": False, "error": f"{type(exc).__name__}: {exc}"}


async def read_media_file(media_id: int | str) -> tuple[bytes, str]:
    """读回桥缓存的媒体字节（她要转手发出去时用），返回 (data, ext)"""
    token = _token()
    if not token:
        return b"", ""
    try:
        async with httpx.AsyncClient(timeout=30.0) as client:
            resp = await client.get(
                f"{BRIDGE_URL}/media/file",
                params={"id": str(media_id)},
                headers={"authorization": f"Bearer {token}"},
            )
            if resp.status_code != 200:
                return b"", ""
            return resp.content, resp.headers.get("x-ext", "jpg")
    except Exception as exc:  # noqa: BLE001
        logger.warning(f"[perception] 读缓存媒体失败 id={media_id}: {exc}")
        return b"", ""


async def save_roster(text: str) -> bool:
    """把"群友档案"推给决策桥，由桥写进 /persona/memory/群友档案.md。

    为什么由桥写：感知层容器看不到 /persona（那是人格容器的卷），桥在人格容器里、有写权限。
    """
    token = _token()
    if not token or not text.strip():
        return False
    try:
        async with httpx.AsyncClient(timeout=15.0) as client:
            resp = await client.post(
                f"{BRIDGE_URL}/memory/roster",
                json={"text": text},
                headers={"authorization": f"Bearer {token}"},
            )
            return bool(resp.json().get("ok"))
    except Exception as exc:  # noqa: BLE001
        logger.warning(f"[perception] 保存群友档案失败：{exc}")
        return False


_NICK_CACHE: dict[str, Any] = {"at": 0.0, "map": {}, "owner": ""}


async def fetch_nicknames(force: bool = False) -> dict[str, Any]:
    """从桥取「自定昵称」映射（qq → 昵称）与主人 QQ；60 秒缓存。

    主人 QQ 是防冒充锚点：她在 prompt 里永远显示为「主人」，与群名片无关。
    """
    import time as _time

    now = _time.monotonic()
    if not force and now - float(_NICK_CACHE.get("at") or 0) < 60:
        return _NICK_CACHE
    token = _token()
    if not token:
        return _NICK_CACHE
    try:
        async with httpx.AsyncClient(timeout=8.0) as client:
            resp = await client.get(f"{BRIDGE_URL}/nicknames", headers={"authorization": f"Bearer {token}"})
        data = resp.json()
        if data.get("ok"):
            _NICK_CACHE.update({"at": now, "map": data.get("map") or {}, "owner": str(data.get("owner") or "")})
    except Exception as exc:  # noqa: BLE001
        logger.debug(f"[perception] 取自定昵称失败：{exc}")
    return _NICK_CACHE
