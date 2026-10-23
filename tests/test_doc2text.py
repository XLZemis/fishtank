import importlib.util
import os
import tempfile
import unittest
from pathlib import Path

DOC2TEXT = Path(os.environ.get("DOC2TEXT_PATH", "/tmp/fishtank-tests/doc2text.py"))


def load_doc2text():
    spec = importlib.util.spec_from_file_location("doc2text_under_test", DOC2TEXT)
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    return module


class TestDoc2Text(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        cls.module = load_doc2text()

    def test_text_and_csv(self):
        with tempfile.TemporaryDirectory() as tmp:
            path = Path(tmp) / "a.csv"
            path.write_text("姓名,能力\n示例角色,文档解析\n", encoding="utf-8")
            out = self.module.extract(str(path))
            self.assertIn("示例角色", out)
            self.assertIn("文档解析", out)

    def test_html(self):
        with tempfile.TemporaryDirectory() as tmp:
            path = Path(tmp) / "a.html"
            path.write_text("<html><body><h1>标题</h1><p>正文</p></body></html>", encoding="utf-8")
            out = self.module.extract(str(path))
            self.assertIn("标题", out)
            self.assertIn("正文", out)
