"""感知层集中配置（P2 配置边界）。

原则：
    · 只读环境变量，不 import NoneBot，便于单测；
    · 已有 PERSONA_* / BOT_QQ 等旧变量继续兼容；
    · 敏感/身份值不提供“真实个人”默认值（owner_qq 可为空，由部署注入）；
    · 路径/端口等有部署参考默认，生产环境用 FISHTANK_* 覆盖。

第一批只集中路径/模型端点；业务触发器（群、关键词、间隔）仍留在 triggers.yaml。
"""
from __future__ import annotations

import os
from dataclasses import dataclass
from pathlib import Path
from typing import Mapping


def _first(env: Mapping[str, str], *names: str, default: str = "") -> str:
    for name in names:
        value = str(env.get(name) or "").strip()
        if value:
            return value
    return default


def _path(value: str) -> Path:
    return Path(os.path.expanduser(value)).expanduser()


@dataclass(frozen=True)
class Settings:
    data_root: Path
    persona_root: Path
    db_path: Path
    prod_db: Path
    prod_sticker_root: Path
    sticker_ontology: Path
    own_sticker_root: Path
    own_sticker_view_prefix: str
    prod_sticker_view_prefix: str
    exports_dir: Path
    persona_out_prefix: str
    armed_file: Path
    media_dir: Path
    bridge_url: str
    bridge_token_file: Path
    mimo_base: str
    mimo_key_file: Path
    cdp_url: str
    qzone_base: str
    bot_qq: str
    owner_qq: str
    debug_routes: bool
    bili_proxy: str
    mimo_model_asr: str
    mimo_model_flash: str
    mimo_model_pro: str
    interact_max_times: int
    interact_max_span_seconds: int
    interact_max_active_jobs: int
    interact_min_interval_seconds: int
    interact_min_gap_seconds: float
    trigger_refresh_seconds: int
    roster_interval_seconds: int
    roster_members_per_group: int
    media_batch_limit: int
    media_prefetch_limit: int
    media_prompt_limit: int


def load_settings(env: Mapping[str, str] | None = None) -> Settings:
    e = dict(os.environ if env is None else env)
    data_root = _path(_first(e, "FISHTANK_DATA_ROOT", "PERSONA_DATA_ROOT", default="/data/qqbot"))
    persona_root = _path(_first(e, "FISHTANK_PERSONA_ROOT", default="/data/persona"))
    db_path = _path(_first(e, "FISHTANK_DB_PATH", default=str(data_root / "db" / "perception.sqlite3")))
    prod_db = _path(_first(e, "FISHTANK_PROD_DB", default=str(data_root / "db" / "qqbot.sqlite3")))
    prod_sticker_root = _path(_first(e, "FISHTANK_PROD_STICKER_ROOT", default=str(data_root / "stickers")))
    sticker_ontology = _path(
        _first(e, "FISHTANK_STICKER_ONTOLOGY", default=str(data_root / "config" / "sticker-tag-ontology.v1.json"))
    )
    own_sticker_root = _path(_first(e, "FISHTANK_OWN_STICKER_ROOT", default=str(persona_root / "stickers")))
    own_sticker_view = _first(e, "FISHTANK_OWN_STICKER_VIEW", default="/persona/stickers").rstrip("/")
    prod_sticker_view = _first(e, "FISHTANK_PROD_STICKER_VIEW", default="/data/prod-stickers").rstrip("/")
    exports_dir = _path(_first(e, "FISHTANK_EXPORTS_DIR", default=str(data_root / "exports")))
    persona_out_prefix = _first(e, "FISHTANK_PERSONA_OUT_PREFIX", default="/persona/out").rstrip("/")
    armed_file = _path(_first(e, "FISHTANK_ARMED_FILE", default=str(data_root / "config" / "perception-armed")))
    media_dir = _path(_first(e, "FISHTANK_MEDIA_DIR", default=str(persona_root / "media")))
    bridge_url = _first(e, "FISHTANK_BRIDGE_URL", "PERSONA_BRIDGE_URL", default="http://dsh-persona:13101").rstrip("/")
    bridge_token = _path(
        _first(e, "FISHTANK_BRIDGE_TOKEN_FILE", "PERSONA_BRIDGE_TOKEN_FILE",
               default=str(data_root / "config" / "persona-bridge.token"))
    )
    mimo_base = _first(e, "FISHTANK_MIMO_BASE", default="https://api.xiaomimimo.com/v1").rstrip("/")
    mimo_key = _path(_first(e, "FISHTANK_MIMO_KEY_FILE", default=str(data_root / "config" / "mimo.key")))
    cdp_url = _first(e, "FISHTANK_CDP_URL")
    qzone_base = _first(e, "FISHTANK_QZONE_BASE", default="https://user.qzone.qq.com").rstrip("/")
    bot_qq = _first(e, "FISHTANK_BOT_QQ", "BOT_QQ")
    owner_qq = _first(e, "FISHTANK_OWNER_QQ", "PERSONA_OWNER_QQ")
    debug_routes = _first(e, "FISHTANK_DEBUG_ROUTES", default="0").lower() in {"1", "true", "yes", "on"}
    bili_proxy = _first(e, "FISHTANK_BILI_PROXY", "PERSONA_BILI_PROXY", default="http://172.18.0.1:13120")
    mimo_model_asr = _first(e, "FISHTANK_MIMO_MODEL_ASR", default="mimo-v2.5-asr")
    mimo_model_flash = _first(e, "FISHTANK_MIMO_MODEL_FLASH", default="mimo-v2.6-flash")
    mimo_model_pro = _first(e, "FISHTANK_MIMO_MODEL_PRO", default="mimo-v2.6-pro")

    def _int(names: tuple[str, ...], default: int) -> int:
        raw = _first(e, *names, default=str(default))
        try:
            return int(raw)
        except ValueError:
            return default

    def _float(names: tuple[str, ...], default: float) -> float:
        raw = _first(e, *names, default=str(default))
        try:
            return float(raw)
        except ValueError:
            return default

    interact_max_times = _int(("FISHTANK_INTERACT_MAX_TIMES",), 20)
    interact_max_span = _int(("FISHTANK_INTERACT_MAX_SPAN_S",), 3600)
    interact_max_jobs = _int(("FISHTANK_INTERACT_MAX_JOBS",), 20)
    interact_min_interval = _int(("FISHTANK_INTERACT_MIN_INTERVAL_S",), 3)
    interact_min_gap = _float(("FISHTANK_INTERACT_MIN_GAP_S",), 3.0)
    trigger_refresh = _int(("FISHTANK_TRIGGER_REFRESH_S",), 60)
    roster_interval = _int(("FISHTANK_ROSTER_INTERVAL_S",), 6 * 3600)
    roster_members = _int(("FISHTANK_ROSTER_MEMBERS_PER_GROUP",), 12)
    media_batch = _int(("FISHTANK_MEDIA_BATCH_LIMIT",), 24)
    media_prefetch = _int(("FISHTANK_MEDIA_PREFETCH_LIMIT",), 12)
    media_prompt = _int(("FISHTANK_MEDIA_PROMPT_LIMIT",), 24)
    return Settings(
        data_root=data_root,
        persona_root=persona_root,
        db_path=db_path,
        prod_db=prod_db,
        prod_sticker_root=prod_sticker_root,
        sticker_ontology=sticker_ontology,
        own_sticker_root=own_sticker_root,
        own_sticker_view_prefix=own_sticker_view,
        prod_sticker_view_prefix=prod_sticker_view,
        exports_dir=exports_dir,
        persona_out_prefix=persona_out_prefix,
        armed_file=armed_file,
        media_dir=media_dir,
        bridge_url=bridge_url,
        bridge_token_file=bridge_token,
        mimo_base=mimo_base,
        mimo_key_file=mimo_key,
        cdp_url=cdp_url,
        qzone_base=qzone_base,
        bot_qq=bot_qq,
        owner_qq=owner_qq,
        debug_routes=debug_routes,
        bili_proxy=bili_proxy,
        mimo_model_asr=mimo_model_asr,
        mimo_model_flash=mimo_model_flash,
        mimo_model_pro=mimo_model_pro,
        interact_max_times=interact_max_times,
        interact_max_span_seconds=interact_max_span,
        interact_max_active_jobs=interact_max_jobs,
        interact_min_interval_seconds=interact_min_interval,
        interact_min_gap_seconds=interact_min_gap,
        trigger_refresh_seconds=trigger_refresh,
        roster_interval_seconds=roster_interval,
        roster_members_per_group=roster_members,
        media_batch_limit=media_batch,
        media_prefetch_limit=media_prefetch,
        media_prompt_limit=media_prompt,
    )


SETTINGS = load_settings()
