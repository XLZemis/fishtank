import unittest

from helpers import load_perception

sticker = load_perception("sticker")


class TestStickerProtocol(unittest.TestCase):
    def test_parse_request(self):
        self.assertEqual("看戏", sticker.parse_request("STICKER: 看戏"))
        self.assertIsNone(sticker.parse_request("NO"))
        self.assertIsNone(sticker.parse_request("随便说点"))

    def test_parse_review(self):
        self.assertEqual([1], sticker.parse_review("SEND: 1"))
        self.assertEqual([1, 3], sticker.parse_review("SEND: 1,3"))
        self.assertEqual([], sticker.parse_review("SKIP"))

    def test_score_prefers_tag(self):
        tags = {"usage_tags": ["看戏"], "anti_tags": ["正式场合"]}
        self.assertGreater(sticker._score("看戏", tags, ""), 0)
        self.assertLess(sticker._score("正式场合", tags, ""), 0)
