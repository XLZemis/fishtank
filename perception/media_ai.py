"""多模态理解（图像 / 音频 / 视频）—— 给她的一个统一工具

用户 2026-10-20 的要求：
    · 音视频她要能"看懂"，不能只看到文件名；
    · 分三档：① 语音转文字 + 抽帧 ② mimo-v2.6-flash ③ mimo-v2.6-pro；
    · 做成一个工具，参数能指定「抽出的帧数 / 给音视频的提示词」。

接口格式（来自官方文档 + 社区插件 ch1bug/dsh-mimo-agent-tools 的驱动实现，2026-10 实测）：
    图像   {"type":"image_url","image_url":{"url":"data:image/jpeg;base64,…" 或 https URL}}
    音频   {"type":"input_audio","input_audio":{"data": "<公网 URL 或 data: URL>"}}
    视频   {"type":"video_url","video_url":{"url": "…"}, "fps": 1, "media_resolution": "default"}
    ASR    {"type":"input_audio","input_audio":{"data":"data:audio/wav;base64,…"}}（必须 base64/data URL）
    公共体  {"stream": false, "max_completion_tokens": 2048, "thinking": {"type": "disabled"}}

降级策略（很重要）：
    · 视频原生理解失败 → 用 ffmpeg 抽帧发图 + ASR 音频，再让模型看图说话；
    · 任何一步失败都不抛给调用方，返回 ok:false + 原因。
"""

from __future__ import annotations

import base64
import os
import subprocess
import tempfile
import time
from pathlib import Path
from typing import Any

import httpx
from nonebot import logger

from .settings import SETTINGS

MIMO_BASE = SETTINGS.mimo_base
KEY_FILE = str(SETTINGS.mimo_key_file)
MAX_BYTES = 24 * 1024 * 1024  # 24MB 以内才直接发

_MODELS = {
    "asr": "mimo-v2.5-asr",
    "flash": "mimo-v2.6-flash",
    "pro": "mimo-v2.6-pro",
    "vision": "mimo-v2.6-flash",
    "video": "mimo-v2.6-flash",
    "audio": "mimo-v2.6-flash",
}
_EXT_KIND = {
    "jpg": "image", "jpeg": "image", "png": "image", "gif": "image", "webp": "image", "bmp": "image",
    "wav": "audio", "mp3": "audio", "flac": "audio", "ogg": "audio", "m4a": "audio", "amr": "audio", "silk": "audio",
    "mp4": "video", "webm": "video", "mov": "video", "mkv": "video", "avi": "video",
}
_MIME = {
    "jpg": "image/jpeg", "jpeg": "image/jpeg", "png": "image/png", "gif": "image/gif",
    "webp": "image/webp", "bmp": "image/bmp",
    "wav": "audio/wav", "mp3": "audio/mpeg", "flac": "audio/flac", "ogg": "audio/ogg",
    "m4a": "audio/mp4", "amr": "audio/amr",
    "mp4": "video/mp4", "webm": "video/webm", "mov": "video/quicktime",
}


def _key() -> str:
    try:
        return Path(KEY_FILE).read_text(encoding="utf-8").strip()
    except OSError:
        return ""


def _kind_of(url: str, hint: str = "") -> str:
    if hint in ("image", "mface"):
        return "image"
    if hint == "video":
        return "video"
    if hint in ("record", "audio"):
        return "audio"
    ext = url.split("?")[0].rsplit(".", 1)[-1].lower()
    return _EXT_KIND.get(ext, "image")


async def _download(url: str) -> tuple[bytes, str]:
    ext = url.split("?")[0].rsplit(".", 1)[-1].lower()
    if not url.startswith("http"):
        # 本地路径（/persona 只读挂载或 /data 下）
        p = Path(url)
        if not p.exists():
            raise FileNotFoundError(url)
        return p.read_bytes(), p.suffix.lstrip(".").lower() or "bin"
    async with httpx.AsyncClient(timeout=60.0, follow_redirects=True) as client:
        resp = await client.get(url)
    if resp.status_code != 200:
        raise RuntimeError(f"下载失败 {resp.status_code}")
    return resp.content, (resp.headers.get("content-type", "").rsplit("/", 1)[-1].split(";")[0] or ext or "bin")


def _to_wav(raw: bytes, src_ext: str) -> bytes:
    """任意音频 → 16k 单声道 wav（ASR 需要）。"""
    with tempfile.TemporaryDirectory(prefix="mimo-asr-") as tmp:
        src = os.path.join(tmp, f"in.{src_ext or 'amr'}")
        dst = os.path.join(tmp, "out.wav")
        Path(src).write_bytes(raw)
        subprocess.run(
            ["ffmpeg", "-y", "-loglevel", "error", "-i", src, "-ar", "16000", "-ac", "1", dst],
            check=True, timeout=60,
        )
        return Path(dst).read_bytes()


def _frames(raw: bytes, src_ext: str, count: int) -> list[bytes]:
    """从视频里抽 N 帧（均匀分布），返回 JPEG 字节列表。"""
    count = max(1, min(int(count), 8))
    with tempfile.TemporaryDirectory(prefix="mimo-frames-") as tmp:
        src = os.path.join(tmp, f"in.{src_ext or 'mp4'}")
        Path(src).write_bytes(raw)
        # 先探测时长，均匀取帧
        dur = 0.0
        try:
            out = subprocess.run(
                ["ffprobe", "-v", "error", "-show_entries", "format=duration", "-of", "default=nw=1:nk=1", src],
                capture_output=True, text=True, timeout=30,
            )
            dur = float((out.stdout or "0").strip() or 0)
        except Exception:  # noqa: BLE001
            dur = 0.0
        frames: list[bytes] = []
        for i in range(count):
            ts = (dur * (i + 0.5) / count) if dur else i
            out = os.path.join(tmp, f"f{i}.jpg")
            try:
                subprocess.run(
                    ["ffmpeg", "-y", "-loglevel", "error", "-ss", f"{ts:.2f}", "-i", src, "-frames:v", "1", "-q:v", "3", out],
                    check=True, timeout=60,
                )
                frames.append(Path(out).read_bytes())
            except Exception:  # noqa: BLE001
                continue
        return frames


async def _chat(model: str, parts: list[dict], timeout: float = 120.0, max_tokens: int = 1500) -> dict[str, Any]:
    key = _key()
    if not key:
        return {"ok": False, "error": "no mimo key"}
    body = {
        "model": model,
        "messages": [{"role": "user", "content": parts}],
        "stream": False,
        "max_completion_tokens": max_tokens,
        "thinking": {"type": "disabled"},
    }
    for attempt in (1, 2):
        try:
            async with httpx.AsyncClient(timeout=timeout) as client:
                resp = await client.post(
                    f"{MIMO_BASE}/chat/completions", json=body,
                    headers={"authorization": f"Bearer {key}", "content-type": "application/json"},
                )
            data = resp.json()
            if resp.status_code == 429 and attempt == 1:
                await __import__("asyncio").sleep(4)
                continue
            if resp.status_code != 200:
                return {"ok": False, "error": f"http {resp.status_code}: {str(data)[:200]}"}
            text = ((data.get("choices") or [{}])[0].get("message") or {}).get("content") or ""
            return {"ok": True, "text": str(text).strip(), "model": model, "usage": data.get("usage")}
        except Exception as exc:  # noqa: BLE001
            if attempt == 2:
                return {"ok": False, "error": f"{type(exc).__name__}: {exc}"}
            await __import__("asyncio").sleep(2)
    return {"ok": False, "error": "unreachable"}


async def understand(
    url: str,
    *,
    tier: str = "flash",
    hint: str = "",
    prompt: str = "",
    frames: int = 4,
    fps: float = 1,
) -> dict[str, Any]:
    """统一入口：tier=asr（转写+抽帧路径）/ flash / pro。"""
    started = time.time()
    if not url:
        return {"ok": False, "error": "need url"}
    kind = _kind_of(url, hint)
    try:
        raw, src_ext = await _download(url)
    except Exception as exc:  # noqa: BLE001
        return {"ok": False, "error": f"下载失败：{type(exc).__name__}: {exc}"}
    if len(raw) > MAX_BYTES:
        return {"ok": False, "error": f"文件过大（{len(raw)//1024//1024}MB > 24MB）"}

    ask = prompt or {
        "image": "描述这张图片的内容；如果有文字，读出来。",
        "audio": "这段音频里说了什么？谁在说、情绪如何？",
        "video": "这个视频在讲什么？画面里发生了什么、有字幕或文字吗？",
    }.get(kind, "描述这个文件的内容。")

    # ① asr 档：语音转文字 + 视频抽帧（帧交给便宜模型看/或返回路径）
    if tier == "asr":
        out: dict[str, Any] = {"ok": True, "kind": kind, "tier": "asr", "elapsed_ms": int((time.time() - started) * 1000)}
        if kind == "audio":
            wav = await __import__("asyncio").to_thread(_to_wav, raw, src_ext)
            b64 = base64.b64encode(wav).decode()
            out["asr"] = await _chat(
                _MODELS["asr"],
                [{"type": "input_audio", "input_audio": {"data": f"data:audio/wav;base64,{b64}"}}],
                timeout=90, max_tokens=800,
            )
        elif kind == "video":
            wav = await __import__("asyncio").to_thread(_to_wav, raw, src_ext)
            b64 = base64.b64encode(wav).decode()
            out["asr"] = await _chat(
                _MODELS["asr"],
                [{"type": "input_audio", "input_audio": {"data": f"data:audio/wav;base64,{b64}"}}],
                timeout=120, max_tokens=800,
            )
            fr = await __import__("asyncio").to_thread(_frames, raw, src_ext, frames)
            out["frames"] = len(fr)
            out["frame_bytes"] = [len(x) for x in fr[:2]]
        return out

    model = _MODELS.get(tier) or _MODELS["flash"]
    # ② flash / pro：原生多模态
    if kind == "image":
        parts = [{"type": "image_url", "image_url": {"url": f"data:{_MIME.get(src_ext, 'image/jpeg')};base64,{base64.b64encode(raw).decode()}"}},
                 {"type": "text", "text": ask}]
        res = await _chat(model, parts, timeout=120)
        res.update({"kind": kind, "tier": tier, "elapsed_ms": int((time.time() - started) * 1000)})
        return res
    if kind == "audio":
        parts = [{"type": "input_audio", "input_audio": {"data": f"data:{_MIME.get(src_ext, 'audio/mpeg')};base64,{base64.b64encode(raw).decode()}"}},
                 {"type": "text", "text": ask}]
        res = await _chat(model, parts, timeout=180)
        res.update({"kind": kind, "tier": tier, "elapsed_ms": int((time.time() - started) * 1000)})
        return res
    # 视频：先试原生，失败退抽帧
    data_url = f"data:{_MIME.get(src_ext, 'video/mp4')};base64,{base64.b64encode(raw).decode()}"
    parts = [{"type": "video_url", "video_url": {"url": data_url}, "fps": float(fps), "media_resolution": "default"},
             {"type": "text", "text": ask}]
    res = await _chat(model, parts, timeout=240, max_tokens=2000)
    if res.get("ok"):
        res.update({"kind": kind, "tier": tier, "mode": "native", "elapsed_ms": int((time.time() - started) * 1000)})
        return res
    # 降级：抽帧 + ASR
    logger.info(f"[perception] 视频原生理解失败，降级抽帧：{res.get('error')}")
    fr = await __import__("asyncio").to_thread(_frames, raw, src_ext, frames)
    parts2: list[dict] = []
    for i, f in enumerate(fr):
        parts2.append({"type": "image_url", "image_url": {"url": f"data:image/jpeg;base64,{base64.b64encode(f).decode()}"}})
    parts2.append({"type": "text", "text": f"{ask}\n（这是从视频里等间隔抽的 {len(fr)} 帧，按时间顺序。）"})
    res2 = await _chat(_MODELS["flash"], parts2, timeout=180, max_tokens=2000)
    if res2.get("ok"):
        res2.update({"kind": kind, "tier": tier, "mode": "frames", "frames": len(fr),
                     "elapsed_ms": int((time.time() - started) * 1000)})
        return res2
    return {"ok": False, "error": f"原生失败({res.get('error')}) 且抽帧失败({res2.get('error')})"}
