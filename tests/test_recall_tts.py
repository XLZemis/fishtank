import asyncio
import unittest

from helpers import load_perception

sender = load_perception("sender")
tts = load_perception("tts")


class FakeBot:
    def __init__(self):
        self.calls = []

    async def call_api(self, api, **kwargs):
        self.calls.append((api, kwargs))
        return {"message_id": 424242}


class TestRecall(unittest.TestCase):
    def setUp(self):
        sender._SENT_CACHE.clear()

    def test_recent_and_recall_last(self):
        sender._remember_sent("group:1000000003", {"message_id": 101}, "text", "hello")
        self.assertEqual("101", sender.recent_sent("group:1000000003")[0]["message_id"])
        bot = FakeBot()
        result = asyncio.run(sender.recall_last(bot, "group:1000000003", count=1))
        self.assertTrue(result["ok"])
        self.assertEqual("101", result["recalled"][0]["message_id"])
        self.assertEqual("delete_msg", bot.calls[0][0])

    def test_only_own_cached_ids(self):
        bot = FakeBot()
        result = asyncio.run(sender.recall_last(bot, "group:1000000003", message_id="999"))
        self.assertFalse(result["ok"])
        self.assertFalse(bot.calls)


class TestTTS(unittest.TestCase):
    def test_empty_and_invalid_voice_are_rejected_before_network(self):
        self.assertFalse(asyncio.run(tts.synthesize(""))["ok"])
        self.assertFalse(asyncio.run(tts.synthesize("hi", voice="不存在"))["ok"])

    def test_voice_list(self):
        self.assertIn("冰糖", tts.VOICES)
        self.assertIn("mimo_default", tts.VOICES)
