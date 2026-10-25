import unittest

from helpers import load_perception

sender = load_perception("sender")


class FakeBot:
    """只记录 call_api 的假 bot；fail_on_json=True 模拟 NapCat 拒绝卡片。"""

    def __init__(self, fail_on_json: bool = False):
        self.calls = []
        self.fail_on_json = fail_on_json

    async def call_api(self, api, **kwargs):
        self.calls.append((api, kwargs))
        if self.fail_on_json and "json" in str(kwargs):
            raise RuntimeError("未知的消息类型：json")
        return {"message_id": 1000 + len(self.calls)}


class TestLinkCard(unittest.IsolatedAsyncioTestCase):
    def test_builds_structmsg_card(self):
        raw = sender._link_card_json("https://example.com/a", "示例标题", "说明")
        self.assertIn("com.tencent.structmsg", raw)
        self.assertIn("https://example.com/a", raw)
        self.assertIn("示例标题", raw)

    async def test_json_segment_sent_first(self):
        bot = FakeBot()
        ok = await sender.send_link_card(bot, "group:1000000003", "https://example.com/a", "示例标题")
        self.assertTrue(ok)
        self.assertEqual(1, len(bot.calls))
        api, kwargs = bot.calls[0]
        self.assertEqual("send_group_msg", api)
        message = kwargs["message"]
        self.assertEqual("json", message[0]["type"])
        self.assertIn("example.com", message[0]["data"]["data"])

    async def test_falls_back_to_text_when_card_fails(self):
        bot = FakeBot(fail_on_json=True)
        ok = await sender.send_link_card(bot, "group:1000000004", "https://example.com/b", "兜底标题")
        self.assertTrue(ok)
        # 第一次 json 失败，第二次纯文本成功
        self.assertEqual(2, len(bot.calls))
        message = bot.calls[1][1]["message"]
        self.assertEqual("text", message[0]["type"])
        self.assertIn("https://example.com/b", message[0]["data"]["text"])
        self.assertIn("兜底标题", message[0]["data"]["text"])

    async def test_empty_url_is_rejected(self):
        bot = FakeBot()
        self.assertFalse(await sender.send_link_card(bot, "group:1000000005", "", "x"))
        self.assertEqual([], bot.calls)


if __name__ == "__main__":
    unittest.main()
