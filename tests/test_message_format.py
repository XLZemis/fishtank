import unittest

from helpers import FakeMessageEvent, load_perception

bus = load_perception("bus")


class FakeGroupEvent(FakeMessageEvent):
    pass


class TestNickname(unittest.TestCase):
    def test_card_takes_priority(self):
        self.assertEqual("群名片", bus._nickname(FakeMessageEvent(nickname="QQ昵称", card="群名片")))

    def test_no_fallback_to_qq(self):
        self.assertEqual("", bus._nickname(FakeMessageEvent(nickname="", card="")))

    def test_global_nickname_and_card_are_separate(self):
        ev = FakeMessageEvent(nickname="QQ昵称", card="群名片")
        self.assertEqual("QQ昵称", bus._global_nickname(ev))
        self.assertEqual("群名片", bus._group_card(ev))


class TestNormalizeMessage(unittest.TestCase):
    def setUp(self):
        self._orig = bus.GroupMessageEvent
        bus.GroupMessageEvent = FakeGroupEvent

    def tearDown(self):
        bus.GroupMessageEvent = self._orig

    def test_two_part_format(self):
        ev = FakeGroupEvent(nickname="示例用户", card="示例名片")
        text = bus.normalize_message(ev, "1000000001", "Persona", body="你好")
        self.assertEqual("[示例用户][示例名片]: 你好", text)

    def test_no_card_keeps_only_nickname(self):
        ev = FakeGroupEvent(nickname="示例用户乙", card="")
        text = bus.normalize_message(ev, "1000000001", "Persona", body="你好")
        self.assertEqual("[示例用户乙]: 你好", text)

    def test_missing_name_uses_empty_bracket(self):
        ev = FakeGroupEvent(nickname="", card="")
        text = bus.normalize_message(ev, "1000000001", "Persona", body="你好")
        self.assertEqual("[]: 你好", text)

    def test_owner_display_name(self):
        ev = FakeGroupEvent(nickname="任何人", card="任何人")
        text = bus.normalize_message(ev, "1000000001", "Persona", body="你好", display_name="主人")
        self.assertEqual("[主人][任何人]: 你好", text)

    def test_private_uses_private_placeholder(self):
        ev = FakeMessageEvent(nickname="示例用户乙", card="", group_id=0)
        text = bus.normalize_message(ev, "1000000001", "Persona", body="你好")
        self.assertEqual("[示例用户乙][私聊]: 你好", text)
