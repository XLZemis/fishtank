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
    )


SETTINGS = load_settings()
