import dataclasses
import tempfile
import unittest
from pathlib import Path

from helpers import load_perception

sender = load_perception("sender")


class TestOutboundPaths(unittest.TestCase):
    """出站文件路径：目录边界 + refs/stickers 中转的分流。"""

    def setUp(self):
        self._tmp = tempfile.TemporaryDirectory()
        self.tmp = Path(self._tmp.name)
        self.exports = self.tmp / "exports"
        self.exports.mkdir()
        self._orig = sender.SETTINGS
        # SETTINGS 是 frozen dataclass，用 replace 生成测试配置并替换模块引用
        sender.SETTINGS = dataclasses.replace(
            self._orig,
            exports_dir=self.exports,
            persona_out_prefix="/persona/out",
        )

    def tearDown(self):
        sender.SETTINGS = self._orig
        self._tmp.cleanup()

    def test_valid_out_file(self):
        target = self.exports / "hello.txt"
        target.write_text("hi", encoding="utf-8")
        self.assertEqual(str(target.resolve()), sender._resolve_export_file("/persona/out/hello.txt"))
        self.assertEqual(str(target.resolve()), sender._resolve_export_file("out/hello.txt"))

    def test_traversal_is_rejected(self):
        for raw in (
            "/persona/out/../../etc/passwd",
            "/persona/out/../secret.txt",
            "out/../../etc/passwd",
        ):
            self.assertIsNone(sender._resolve_export_file(raw), raw)

    def test_directory_is_not_a_file(self):
        (self.exports / "sub").mkdir()
        self.assertIsNone(sender._resolve_export_file("/persona/out/sub"))

    def test_missing_file(self):
        self.assertIsNone(sender._resolve_export_file("/persona/out/nope.txt"))

    def test_unknown_prefix(self):
        self.assertIsNone(sender._resolve_export_file("/etc/passwd"))
        self.assertIsNone(sender._resolve_export_file("/persona/refs/头像.png"))

    def test_stageable_prefixes(self):
        for raw in (
            "/persona/refs/头像.png",
            "/persona/stickers/own/1.png",
            "/persona/media/12.jpg",
        ):
            self.assertTrue(raw.startswith(sender._STAGEABLE_PREFIXES), raw)
        self.assertFalse("/etc/passwd".startswith(sender._STAGEABLE_PREFIXES))

    def test_overflow_outside_exports_is_rejected_even_if_exists(self):
        outside = self.tmp / "outside.txt"
        outside.write_text("secret", encoding="utf-8")
        # 构造一个指向 exports 外面的符号链接，resolve 后必须被拒绝
        link = self.exports / "link.txt"
        try:
            link.symlink_to(outside)
        except OSError:
            self.skipTest("当前环境不支持符号链接")
        self.assertIsNone(sender._resolve_export_file("/persona/out/link.txt"))


if __name__ == "__main__":
    unittest.main()
