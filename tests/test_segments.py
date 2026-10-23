import unittest

from helpers import load_perception

segments = load_perception("segments")


class TestMeaningfulContent(unittest.TestCase):
    def test_empty_text_is_not_meaningful(self):
        self.assertFalse(segments.has_meaningful_content([{"type": "text", "data": {"text": ""}}]))
        self.assertFalse(segments.has_meaningful_content([{"type": "text", "data": {"text": "   "}}]))
        self.assertFalse(segments.has_meaningful_content([]))

    def test_non_text_segments_are_meaningful(self):
        self.assertTrue(segments.has_meaningful_content([{"type": "at", "data": {"qq": "1"}}]))
        self.assertTrue(segments.has_meaningful_content([{"type": "reply", "data": {"id": "1"}}]))
        self.assertTrue(segments.has_meaningful_content([{"type": "image", "data": {"file": "x"}}]))
        self.assertTrue(segments.has_meaningful_content([{"type": "text", "data": {"text": "hi"}}]))


class TestParseSegments(unittest.TestCase):
    def test_text_and_at_name(self):
        parsed = segments.parse_segments(
            [
                {"type": "text", "data": {"text": "你好 "}},
                {"type": "at", "data": {"qq": "1000000009"}},
            ],
            self_id="1000000001",
            bot_name="Persona",
            at_names={"1000000009": "群友A"},
        )
        self.assertIn("你好", parsed.text)
        self.assertIn("@群友A", parsed.text)

    def test_reply_suppressed_when_asked(self):
        msg = [
            {"type": "reply", "data": {"id": "123"}},
            {"type": "text", "data": {"text": "正文"}},
        ]
        with_marker = segments.parse_segments(msg)
        self.assertIn("↳[回复]", with_marker.text)
        without = segments.parse_segments(msg, suppress_reply=True)
        self.assertNotIn("↳[回复]", without.text)
        self.assertIn("正文", without.text)

    def test_image_becomes_media_with_anchor(self):
        parsed = segments.parse_segments([{"type": "image", "data": {"url": "https://example.com/a.jpg"}}])
        self.assertEqual(1, len(parsed.media))
        self.assertEqual("image", parsed.media[0].kind)

    def test_multiple_images_are_all_kept(self):
        message = [
            {"type": "image", "data": {"url": f"https://example.com/{i}.jpg", "file": f"{i}.jpg"}}
            for i in range(10)
        ]
        parsed = segments.parse_segments(message)
        self.assertEqual(10, len(parsed.media))
        self.assertIn("{{M0}}", parsed.text)
        self.assertIn("{{M9}}", parsed.text)
