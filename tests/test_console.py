import unittest

from helpers import load_perception

console = load_perception("console")


class TestConsoleAuth(unittest.TestCase):
    def test_whitelist_parsing(self):
        qqs = console.console_qqs({"FISHTANK_CONSOLE_QQS": "10001, 10002,abc,  "})
        self.assertEqual(frozenset({"10001", "10002"}), qqs)

    def test_disabled_by_default(self):
        self.assertEqual(frozenset(), console.console_qqs({}))

    def test_only_whitelisted_and_private_slash_commands(self):
        allowed = frozenset({"10001"})
        self.assertTrue(console.should_handle("10001", "/status", allowed=allowed))
        self.assertTrue(console.should_handle("10001", "/stats 48", allowed=allowed))
        self.assertFalse(console.should_handle("10002", "/status", allowed=allowed))
        self.assertFalse(console.should_handle("10001", "status", allowed=allowed))
        self.assertFalse(console.should_handle("10001", "你好呀", allowed=allowed))

    def test_reserved_and_unknown_commands_fall_through(self):
        allowed = frozenset({"10001"})
        self.assertFalse(console.should_handle("10001", "/perception", allowed=allowed))
        self.assertFalse(console.should_handle("10001", "/感知", allowed=allowed))
        self.assertFalse(console.should_handle("10001", "/deploy", allowed=allowed))

    def test_parse_command(self):
        self.assertEqual(("pause", ["group:1", "30"]), console.parse_command("/pause group:1 30"))
        self.assertEqual(("help", []), console.parse_command("/help"))
        self.assertEqual(("", []), console.parse_command("hello"))
        self.assertEqual(("", []), console.parse_command("/"))


class TestConsoleDispatch(unittest.IsolatedAsyncioTestCase):
    async def test_help(self):
        text = await console.dispatch("/help")
        self.assertIn("/status", text)
        self.assertIn("/pause", text)

    async def test_invalid_scope_rejected_without_db(self):
        text = await console.dispatch("/pause not-a-scope")
        self.assertIn("scope 形状不对", text)
        text = await console.dispatch("/resume 123")
        self.assertIn("scope 形状不对", text)

    async def test_unknown_command(self):
        text = await console.dispatch("/nope")
        self.assertIn("未知命令", text)


if __name__ == "__main__":
    unittest.main()
