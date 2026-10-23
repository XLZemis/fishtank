"""语音识别（ASR）——把群里的语音消息转成文字给她看

语音识别用于把群里的语音消息转成文字。

接口事实（2026-10-18 实测）：
    · MiMo 没有 OpenAI 风格的 /audio/transcriptions（404）
    · ASR 走 chat/completions，模型 `mimo-v2.5-asr`，消息必须是**只有音频**的 user 消息：
        {"role":"user","content":[{"type":"input_audio","input_audio":{"data":"<b64>","format":"wav"}}]}
      带多余 text 会 400（"ASR request requires a user message with input_audio content"）
    · 频控严格：短时间连发会 429，所以这里串行 + 退避重试一次

音频格式：QQ 语音是 amr，MiMo 只认 wav/mp3；宿主的 qqbot 容器里有 ffmpeg，本地转 16k 单声道 wav。
"""

from __future__ import annotations

import asyncio
import base64
import json
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
MIMO_MODEL = os.environ.get("FISHTANK_ASR_MODEL", os.environ.get("PERSONA_ASR_MODEL", "mimo-v2.5-asr"))
MAX_BYTES = 8 * 1024 * 1024  # 8MB 以上的语音不转写（够长的一句话约 100KB）
_lock = asyncio.Lock()
_last_call = 0.0


def _api_key() -> str:
    """从挂载文件读 MiMo key（不想把 key 塞进容器的环境变量）。"""
    path = SETTINGS.mimo_key_file
    try:
        return path.read_text(encoding="utf-8").strip()
    except OSError:
        return ""


def enabled() -> bool:
    return bool(_api_key())


def _to_wav(raw: bytes, ext: str) -> tuple[bytes, str]:
    """把 amr/mp3/silk 等转成 16kHz 单声道 wav；已经是 wav 就原样返回。"""
    if ext.lower() == "wav":
        return raw, "wav"
    with tempfile.TemporaryDirectory(prefix="asr-") as tmp:
        src = os.path.join(tmp, f"in.{ext or 'amr'}")
        dst = os.path.join(tmp, "out.wav")
        with open(src, "wb") as fh:
            fh.write(raw)
        try:
            subprocess.run(
                ["ffmpeg", "-y", "-loglevel", "error", "-i", src, "-ar", "16000", "-ac", "1", dst],
                check=True, timeout=30,
            )
        except Exception as exc:  # noqa: BLE001
            logger.warning(f"[perception] 音频转码失败（{ext}）：{exc}")
            return b"", ""
        with open(dst, "rb") as fh:
            return fh.read(), "wav"


async def _post_asr(audio_b64: str) -> dict[str, Any]:
    key = _api_key()
    if not key:
        return {"ok": False, "error": "no mimo key"}
    global _last_call
    # 频控：两次调用至少间隔 1.2 秒
    wait = 1.2 - (time.monotonic() - _last_call)
    if wait > 0:
        await asyncio.sleep(wait)
    payload = {
        "model": MIMO_MODEL,
        "messages": [
            {"role": "user", "content": [{"type": "input_audio", "input_audio": {"data": audio_b64, "format": "wav"}}]}
        ],
    }
    headers = {"authorization": f"Bearer {key}", "content-type": "application/json"}
    for attempt in (1, 2, 3):
        try:
            async with httpx.AsyncClient(timeout=60.0) as client:
                resp = await client.post(f"{MIMO_BASE}/chat/completions", json=payload, headers=headers)
            _last_call = time.monotonic()
            if resp.status_code == 429 and attempt < 3:
                await asyncio.sleep(4.0 * attempt)
                continue
            data = resp.json()
            if resp.status_code != 200:
                return {"ok": False, "error": f"http {resp.status_code}: {str(data)[:160]}"}
            text = ((data.get("choices") or [{}])[0].get("message") or {}).get("content") or ""
            return {"ok": True, "text": str(text).strip()}
        except Exception as exc:  # noqa: BLE001
            if attempt == 3:
                return {"ok": False, "error": f"{type(exc).__name__}: {exc}"}
            await asyncio.sleep(3.0 * attempt)
    return {"ok": False, "error": "unreachable"}


async def transcribe(url: str, ext: str = "amr") -> dict[str, Any]:
    """下载并转写一条语音。返回 {ok, text} 或 {ok:False, error}。"""
    if not url or not enabled():
        return {"ok": False, "error": "asr disabled or no url"}
    async with _lock:  # 串行，避免触发 429
        try:
            async with httpx.AsyncClient(timeout=40.0, follow_redirects=True) as client:
                resp = await client.get(url)
            if resp.status_code != 200 or not resp.content:
                return {"ok": False, "error": f"download {resp.status_code}"}
            raw = resp.content
            if len(raw) > MAX_BYTES:
                return {"ok": False, "error": "audio too large"}
            wav, fmt = await asyncio.to_thread(_to_wav, raw, ext)
            if not wav:
                return {"ok": False, "error": "convert failed"}
            return await _post_asr(base64.b64encode(wav).decode())
        except Exception as exc:  # noqa: BLE001
            logger.warning(f"[perception] 语音识别失败：{exc}")
            return {"ok": False, "error": f"{type(exc).__name__}: {exc}"}
