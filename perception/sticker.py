"""感知层 · 表情包（sticker.py）

她要能用两套表情包：
    ① **她自己的专属库**：`/data/persona/stickers`（414 张 示例角色，聊天尺寸）
       —— 由决策桥经 HTTP 提供（清单 GET /stickers/manifest、单张 GET /stickers/file?id=…）
    ② **公共生产库**：`/data/qqbot/stickers`（大量，带 usage_tags/attitudes/emotions/anti_tags）
       —— 直接读只读 SQLite（qqbot.sqlite3）与本机文件，零改动

"按需调用 + 审阅后发送" 的落地方式（不碰主 SPEAK 契约）：
    第一轮（她刚说完话，平台问）：
        「要不要顺手配一张表情包？要 → 只回 STICKER: <情绪或用途>；不要 → 只回 NO」
    第二轮（平台把候选 1..N 的**路径 + 描述**给她）：
        「看中哪张回 SEND: <编号>，都不要回 SKIP」——她用 read_image 亲眼看图（她有视觉）
    只有 SEND 命中的那一张会被发出去。主回复的 SPEAK/SILENCE 契约完全不受影响。

与决策桥的接口：
    /decide 支持 `parse: false` → 只回原文，不做 SPEAK 解析、不回滚会话
    （否则 "STICKER: 生气" 会被当成一条群消息发出去）
"""

from __future__ import annotations

import base64
import json
import random
import re
import sqlite3
import time
from dataclasses import dataclass
from pathlib import Path
from typing import Any

from nonebot import logger

from . import bridge, bus, sender

# 她的专属库（由桥提供）；公共库在 qqbot 容器里本来就是本机路径
OWN_MANIFEST_TTL = 300      # 清单 5 分钟缓存
PUBLIC_CACHE_TTL = 300      # 公共库索引 5 分钟缓存
MAX_QUERY_CHARS = 24        # 她给的"情绪/用途"最多这么长
PROD_DB = Path("/data/qqbot/db/qqbot.sqlite3")
PROD_STICKER_ROOT = Path("/data/qqbot/stickers")
ONTOLOGY = Path("/data/qqbot/config/sticker-tag-ontology.v1.json")
# 她审阅时用的路径前缀（人格容器里看到的路径）
OWN_VIEW_PREFIX = "/persona/stickers"
PROD_VIEW_PREFIX = "/data/prod-stickers"


@dataclass
class Candidate:
    """一个候选表情包"""

    source: str          # own / public
    key: str             # own: manifest id；public: sticker_id
    view_path: str       # 她 read_image 用的路径（人格容器内可见）
    caption: str
    tags: dict[str, list[str]]
    score: float

    def label(self) -> str:
        tag_bits = []
        for field in ("emotions", "usage_tags", "attitudes"):
            words = self.tags.get(field) or []
            if words:
                tag_bits.append("/".join(words[:3]))
        tag_text = "；".join(tag_bits) if tag_bits else "无标签"
        return f"{self.view_path} —— {self.caption[:60]}（{tag_text}）"


# ── 别名表（复用生产库的 ontology，让她用口语也能命中标签）────────────────────
_alias_cache: dict[str, str] | None = None


def _aliases() -> dict[str, str]:
    global _alias_cache
    if _alias_cache is not None:
        return _alias_cache
    mapping: dict[str, str] = {}
    try:
        data = json.loads(ONTOLOGY.read_text(encoding="utf-8"))
        for src, dst in (data.get("aliases") or {}).items():
            if isinstance(dst, str) and dst:
                mapping[str(src)] = dst
        # canonical 也自映射一次，方便统一查询
        for field, words in (data.get("canonical") or {}).items():
            for w in words or []:
                mapping.setdefault(str(w), str(w))
    except Exception as exc:  # noqa: BLE001 - 读不到别名不影响主流程
        logger.debug(f"[perception] ontology 读取失败（忽略别名）：{exc}")
    _alias_cache = mapping
    return mapping


def _normalize_query(query: str) -> str:
    q = re.sub(r"[\s，。！？、,.!?~～■「」（）()\[\\]【】]+", "", str(query or ""))
    q = q[:MAX_QUERY_CHARS]
    alias = _aliases().get(q)
    return alias or q


# ── ① 她的专属库（走桥）─────────────────────────────────────────────────────
_own_cache: dict[str, Any] = {"at": 0.0, "items": []}


async def own_items(force: bool = False) -> list[dict[str, Any]]:
    """取她专属库的清单（5 分钟缓存；桥不可用时返回空表，不抛）"""
    now = time.time()
    if not force and _own_cache["items"] and now - _own_cache["at"] < OWN_MANIFEST_TTL:
        return _own_cache["items"]
    payload = await bridge.fetch_sticker_manifest()
    if not payload or not payload.get("ok"):
        logger.debug(f"[perception] 专属表情包清单取不到：{(payload or {}).get('error')}")
        return _own_cache["items"]
    items = (payload.get("manifest") or {}).get("items") or []
    _own_cache.update({"at": now, "items": items})
    logger.info(f"[perception] 专属表情包清单已加载：{len(items)} 张")
    return items


# ── ② 公共生产库（直读 SQLite）──────────────────────────────────────────────
_public_cache: dict[str, Any] = {"at": 0.0, "items": []}


def public_items(force: bool = False) -> list[dict[str, Any]]:
    """把公共库的资产 + 标签读成内存索引（5 分钟缓存，只读，不加锁碰生产写路径）"""
    now = time.time()
    if not force and _public_cache["items"] and now - _public_cache["at"] < PUBLIC_CACHE_TTL:
        return _public_cache["items"]
    if not PROD_DB.exists():
        return _public_cache["items"]
    try:
        conn = sqlite3.connect(f"file:{PROD_DB}?mode=ro", uri=True, timeout=3)
        conn.row_factory = sqlite3.Row
        try:
            assets = conn.execute(
                "SELECT sticker_id, asset_path, mime_type, model_caption, core_meaning, safety_risk "
                "FROM sticker_assets WHERE enabled=1 AND sendability>0 AND safety_risk<>'high'"
            ).fetchall()
            tags: dict[int, dict[str, list[str]]] = {}
            for row in conn.execute("SELECT sticker_id, field, tag FROM sticker_tags WHERE enabled=1"):
                tags.setdefault(int(row["sticker_id"]), {}).setdefault(str(row["field"]), []).append(str(row["tag"]))
        finally:
            conn.close()
        items = []
        for row in assets:
            sid = int(row["sticker_id"])
            items.append(
                {
                    "id": sid,
                    "file": str(row["asset_path"]),
                    "caption": str(row["model_caption"] or ""),
                    "meaning": str(row["core_meaning"] or ""),
                    "tags": tags.get(sid, {}),
                }
            )
        _public_cache.update({"at": now, "items": items})
        logger.info(f"[perception] 公共表情包索引已加载：{len(items)} 张")
        return items
    except Exception as exc:  # noqa: BLE001
        logger.warning(f"[perception] 公共表情包库读取失败：{exc}")
        return _public_cache["items"]


# ── 打分与解析 ──────────────────────────────────────────────────────────────


def _score(query: str, tags: dict[str, list[str]], caption: str, meaning: str = "") -> float:
    """把查询词与标签/描述做匹配打分（标签命中权重最高，anti_tags 命中扣分）"""
    if not query:
        return 0.0
    score = 0.0
    for field, words in tags.items():
        for word in words:
            word = str(word)
            if field == "anti_tags":
                if query in word:
                    score -= 1.5
                continue
            if word == query:
                score += 3.0
            elif query in word or word in query:
                score += 2.0
    if query in caption:
        score += 1.5
    if meaning and query in meaning:
        score += 1.0
    return score


async def resolve(query: str, limit: int = 3, sources: tuple[str, ...] = ("own", "public")) -> list[Candidate]:
    """按她给的"情绪/用途"检索候选（两套库一起排，去重后取前 N）"""
    q = _normalize_query(query)
    if not q:
        return []
    pool: list[Candidate] = []

    if "own" in sources:
        for item in await own_items():
            tags = item.get("tags") or {}
            caption = f"{item.get('caption') or ''} {item.get('keywords') or ''}".strip()
            s = _score(q, tags, caption)
            if s > 0:
                pool.append(
                    Candidate(
                        source="own",
                        key=str(item.get("id")),
                        view_path=f"{OWN_VIEW_PREFIX}/{item.get('file')}",
                        caption=item.get("caption") or item.get("name") or "",
                        tags=tags,
                        score=s,
                    )
                )

    if "public" in sources:
        for item in public_items():
            tags = item.get("tags") or {}
            s = _score(q, tags, item.get("caption") or "", item.get("meaning") or "")
            if s > 0:
                pool.append(
                    Candidate(
                        source="public",
                        key=str(item.get("id")),
                        view_path=f"{PROD_VIEW_PREFIX}/{item.get('file')}",
                        caption=item.get("caption") or "",
                        tags=tags,
                        score=s,
                    )
                )

    pool.sort(key=lambda c: c.score, reverse=True)

    # 来源多样性：高分几乎总被公共库（标签/描述更稠密）拿走，
    # 但她自己的库是主人专门给她的，必须保证「有货就进候选」——
    # 先各库取一张最好的，再按分数补满 limit。
    picked: list[Candidate] = []
    for src in sources:
        best = next((c for c in pool if c.source == src), None)
        if best is not None:
            picked.append(best)
    for cand in pool:
        if len(picked) >= limit:
            break
        if cand not in picked:
            picked.append(cand)

    # 截断时保留"每库至少一张"的保障，剩下按分数
    if len(picked) > limit:
        picked = picked[:limit]
    # 同分档随机一点，避免每次都是同一张
    if len(picked) == limit and len(pool) > limit:
        same = [c for c in pool if c not in picked and c.score >= picked[-1].score]
        if same:
            picked[-1] = random.choice(same)
    return picked[:limit]


async def fetch_bytes(cand: Candidate) -> tuple[bytes, str]:
    """取候选表情包的字节（返回 data + 扩展名）"""
    if cand.source == "own":
        item = next((i for i in await own_items() if str(i.get("id")) == cand.key), None)
        payload = await bridge.fetch_sticker_file(cand.key)
        if not payload:
            return b"", ""
        return payload, str((item or {}).get("ext") or "png")
    # 公共库：本机只读
    path = (PROD_STICKER_ROOT / cand.key if cand.key.startswith("assets/") else PROD_STICKER_ROOT / cand.key)
    if not path.exists():
        path = PROD_STICKER_ROOT / str(_public_path_by_id(cand.key))
    try:
        return path.read_bytes(), path.suffix.lstrip(".").lower()
    except Exception as exc:  # noqa: BLE001
        logger.warning(f"[perception] 读公共表情包失败 {path}: {exc}")
        return b"", ""


def _public_path_by_id(sid: str) -> str:
    for item in _public_cache["items"]:
        if str(item.get("id")) == str(sid):
            return str(item.get("file") or "")
    return ""


# ── 两轮协议解析 ────────────────────────────────────────────────────────────


def parse_request(text: str) -> str | None:
    """解析第一轮：STICKER: <情绪> → 返回情绪词；NO/空 → None"""
    for line in str(text or "").replace("\r", "").split("\n"):
        line = line.strip().strip("`").strip()
        if not line:
            continue
        hit = re.match(r"^STICKER\s*[:：]\s*(.+)$", line, re.I)
        if hit:
            value = hit.group(1).strip().strip("\"'“”「」")
            value = value[:MAX_QUERY_CHARS]
            return value or None
        if re.match(r"^NO\b", line, re.I):
            return None
    return None


def parse_review(text: str) -> list[int]:
    """解析第二轮：`SEND: 1` / `SEND: 1,3` → 编号列表；SKIP/其它 → 空列表。

    允许她一次选多张（用户口径：附带几张图由她自己定）。
    """
    picks: list[int] = []
    for line in str(text or "").replace("\r", "").split("\n"):
        line = line.strip().strip("`").strip()
        if not line:
            continue
        hit = re.match(r"^SEND\s*[:：]\s*([0-9,\s，、]+)", line, re.I)
        if hit:
            for piece in re.split(r"[,\s，、]+", hit.group(1)):
                if piece.isdigit():
                    n = int(piece)
                    if n not in picks:
                        picks.append(n)
            return picks
        if re.match(r"^SKIP\b", line, re.I):
            return []
    return []


# ── 一轮完整表情包流程 ─────────────────────────────────────────────────────


async def run_round(scope: str, session_id: str | None, last_reply: list[str], context: str,
                    bot: Any = None) -> dict[str, Any]:
    """她刚说完话之后调用：问要不要配图 → 检索 → 让她审阅 → 发她选的那张。

    返回 {used: bool, reason: str, ...}，永远不抛异常。
    """
    info: dict[str, Any] = {"used": False, "reason": "", "candidates": 0}
    try:
        if not last_reply:
            info["reason"] = "no-reply"
            return info

        said = " / ".join(last_reply)[:300]
        ask_prompt = (
            f"【配图请求】你刚才在群里说了：{said}\n"
            f"{context}\n"
            "要不要顺手配一张表情包？\n"
            "要 → 只回一行：STICKER: <情绪或用途，2~6 个字，比如 生气/委屈/卖萌/看戏/求投喂>\n"
            "不要 → 只回：NO"
        )
        first = await bridge.decide(scope=scope, prompt=ask_prompt, session_id=session_id, raw=True)
        if not first.get("ok"):
            info["reason"] = f"ask-failed:{first.get('error')}"
            return info
        query = parse_request(first.get("text") or "")
        if not query:
            info["reason"] = "declined"
            return info
        info["query"] = query

        candidates = await resolve(query, limit=3)
        info["candidates"] = len(candidates)
        if not candidates:
            info["reason"] = f"no-candidate:{query}"
            return info

        listing = "\n".join(f"{i + 1}. {c.label()}" for i, c in enumerate(candidates))
        review_prompt = (
            f"【表情包审阅】按你说的「{query}」，候选 {len(candidates)} 张：\n{listing}\n"
            "你可以用 read_image 打开看一眼（也可以只看描述）。\n"
            "选定一张 → 只回一行：SEND: <编号>\n"
            "都不满意 → 只回：SKIP"
        )
        second = await bridge.decide(scope=scope, prompt=review_prompt, session_id=session_id, raw=True)
        if not second.get("ok"):
            info["reason"] = f"review-failed:{second.get('error')}"
            return info
        picks = [n for n in parse_review(second.get("text") or "") if 1 <= n <= len(candidates)]
        if not picks:
            info["reason"] = "skipped"
            return info
        # G1：发送方必须由调用方给出（已按配置账号断言）；这里**不做任何回落**
        if bot is None:
            info["reason"] = "no-bot"
            return info

        sent_paths: list[str] = []
        for pick in picks:
            chosen = candidates[pick - 1]
            data, ext = await fetch_bytes(chosen)
            if not data:
                continue
            if await sender.send_sticker(
                bot, scope, data, ext, name=f"{chosen.source}-{chosen.key}"
            ):
                sent_paths.append(chosen.view_path)
        info.update(
            {
                "used": bool(sent_paths),
                "reason": "sent" if sent_paths else "send-failed",
                "source": ",".join(sorted({candidates[p - 1].source for p in picks})),
                "key": ",".join(str(candidates[p - 1].key) for p in picks),
                "path": " ".join(sent_paths),
                "bytes": len(sent_paths),
            }
        )
        logger.info(
            f"[perception] 表情包已发 {len(sent_paths)} 张 scope={scope} 查询={query} 路径={sent_paths}"
        )
        return info
    except Exception:  # noqa: BLE001 - 表情包失败绝不能影响主回复
        logger.exception("[perception] 表情包流程异常")
        info["reason"] = "exception"
        return info


def stats() -> dict[str, Any]:
    """给 /perception status 用的统计"""
    return {
        "own": len(_own_cache["items"]),
        "public": len(_public_cache["items"]),
        "own_at": int(_own_cache["at"]),
        "public_at": int(_public_cache["at"]),
    }
