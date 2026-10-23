"""测试共用：加载 perception 子模块时绕开 __init__ 的 NoneBot 插件注册。"""
from __future__ import annotations

import importlib.util
import os
import sys
import types
from pathlib import Path

PERCEPTION_PARENT = Path(os.environ.get("PERCEPTION_PARENT", "/app/app/plugins"))
PERCEPTION_DIR = PERCEPTION_PARENT / "perception"


def load_perception(name: str):
    """按子模块加载，不执行 perception/__init__.py。"""
    pkg = sys.modules.get("perception")
    if pkg is None:
        pkg = types.ModuleType("perception")
        pkg.__path__ = [str(PERCEPTION_DIR)]
        sys.modules["perception"] = pkg
    full = f"perception.{name}"
    if full in sys.modules:
        return sys.modules[full]
    spec = importlib.util.spec_from_file_location(full, PERCEPTION_DIR / f"{name}.py")
    if spec is None or spec.loader is None:
        raise ImportError(f"找不到 {full}")
    module = importlib.util.module_from_spec(spec)
    sys.modules[full] = module
    spec.loader.exec_module(module)
    return module


class FakeSender:
    def __init__(self, nickname: str = "", card: str = ""):
        self.nickname = nickname
        self.card = card


class FakeMessageEvent:
    def __init__(self, *, user_id: int = 1000000002, group_id: int = 1000000003,
                 nickname: str = "", card: str = "", to_me: bool = False,
                 time: int = 1792540000, message=None):
        self.user_id = user_id
        self.group_id = group_id
        self.sender = FakeSender(nickname, card)
        self.to_me = to_me
        self.time = time
        self.message = message or []
        self.original_message = message or []

    def get_message(self):
        return self.message
