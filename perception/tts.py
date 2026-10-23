"""MiMo TTS（语音合成）——复用视频/ASR 的同一把 MiMo key。

接口事实（官方 2026-10-23 文档）：
    · 不是 /audio/speech，而是 /chat/completions；
    · 目标文本放 assistant，风格指令放 user；
    · 预置音色模型 mimo-v2.5-tts，audio={format:"wav", voice:"冰糖"}；
    · 返回 choices[0].message.audio.data（base64 wav）；
    · 另有 voicedesign / voiceclone，本工具先只开预置音色，避免误用样本/克隆。
    · 官方标注“限时免费”，后续可能计费，不把价格写死。

她可以调的：voice（预置音色）、style（自然语言风格）、tag（情绪/唱歌等音频标签）、text。
部署侧固定：model / format / key / timeout。
"""
from __future__ import annotations

import base64
import hashlib
import time
from typing import Any

import httpx
from nonebot import logger

from .settings import SETTINGS

VOICES = {
    "mimo_default": "MiMo 默认（中国集群为冰糖）",
    "冰糖": "中文女声",
    "茉莉": "中文女声",
    "苏打": "中文男声",
    "白桦": "中文男声",
    "Mia": "英文女声",
    "Chloe": "英文女声",
    "Milo": "英文男声",
    "Dean": "英文男声",
}
DEFAULT_VOICE = SETTINGS.tts_voice if SETTINGS.tts_voice in VOICES else "冰糖"
MAX_CHARS = max(40, min(int(SETTINGS.tts_max_chars), 2000))


def _key() -> str:
    try:
        return SETTINGS.mimo_key_file.read_text(encoding="utf-8").strip()
    except OSError:
        return ""


async def synthesize(
    text: str,
    *,
    voice: str = DEFAULT_VOICE,
    style: str = "",
    tag: str = "",
) -> dict[str, Any]:
    """调用 MiMo TTS，返回 {ok, data, bytes, voice, style, tag, usage}。

    失败返回 {ok: False, error}，绝不抛出给调用方。
    """
    body_text = str(text or "").strip()
    if not body_text:
        return {"ok": False, "error": "text 为空"}
    if len(body_text) > MAX_CHARS:
        return {"ok": False, "error": f"text 超过 {MAX_CHARS} 字"}
    key = _key()
    if not key:
        return {"ok": False, "error": "读不到 MiMo key（/data/qqbot/config/mimo.key）"}
    voice = str(voice or DEFAULT_VOICE)
    if voice not in VOICES:
        return {"ok": False, "error": f"不支持的预置音色：{voice}；可选：{', '.join(VOICES)}"}
    style = str(style or "").strip()[:600]
    tag = str(tag or "").strip()[:120]
    # 音频标签必须放 assistant 开头；用户没给就只用自然语言风格（user 消息）。
    assistant = f"{tag}{body_text}" if tag else body_text
    messages: list[dict[str, Any]] = []
    if style:
        messages.append({"role": "user", "content": style})
    messages.append({"role": "assistant", "content": assistant})
    payload = {
        "model": SETTINGS.tts_model or "mimo-v2.5-tts",
        "messages": messages,
        "audio": {"format": "wav", "voice": voice},
    }
    try:
        async with httpx.AsyncClient(timeout=90.0) as client:
            resp = await client.post(
                f"{SETTINGS.mimo_base}/chat/completions",
                json=payload,
                headers={"authorization": f"Bearer {key}", "content-type": "application/json"},
            )
        if resp.status_code != 200:
            return {"ok": False, "error": f"http {resp.status_code}: {resp.text[:200]}"}
        data = resp.json()
        msg = ((data.get("choices") or [{}])[0].get("message") or {})
        audio_b64 = str((msg.get("audio") or {}).get("data") or "")
        if not audio_b64:
            return {"ok": False, "error": "MiMo 返回里没有 audio.data"}
        raw = base64.b64decode(audio_b64)
        if not raw:
            return {"ok": False, "error": "MiMo 返回空音频"}
        if not raw.startswith(b"RIFF"):
            return {"ok": False, "error": "MiMo 返回的不是 wav（RIFF）"}
        return {
            "ok": True,
            "data": raw,
            "bytes": len(raw),
            "voice": voice,
            "style": style,
            "tag": tag,
            "usage": data.get("usage"),
        }
    except Exception as exc:  # noqa: BLE001
        logger.warning(f"[perception] TTS 合成失败：{exc}")
        return {"ok": False, "error": f"{type(exc).__name__}: {exc}"}


async def synthesize_to_exports(
    text: str,
    *,
    voice: str = DEFAULT_VOICE,
    style: str = "",
    tag: str = "",
) -> dict[str, Any]:
    """合成并写入 exports，返回 path/view_path；供路由发语音或返回路径。"""
    got = await synthesize(text, voice=voice, style=style, tag=tag)
    if not got.get("ok"):
        return got
    raw = got.pop("data")
    digest = hashlib.sha1(f"{time.time()}|{text}|{voice}".encode("utf-8", "ignore")).hexdigest()[:12]
    name = f"tts_{digest}.wav"
    out = SETTINGS.exports_dir / name
    out.parent.mkdir(parents=True, exist_ok=True)
    out.write_bytes(raw)
    try:
        out.chmod(0o644)
    except OSError:
        pass
    return {
        **got,
        "path": str(out),
        "view_path": f"{SETTINGS.persona_out_prefix}/{name}",
    }
