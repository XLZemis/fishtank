import tempfile
import unittest
from pathlib import Path

from helpers import load_perception

settings_mod = load_perception("settings")


class TestSettings(unittest.TestCase):
    def test_defaults_are_legacy_paths(self):
        s = settings_mod.load_settings({})
        self.assertEqual(Path("/data/qqbot/db/perception.sqlite3"), s.db_path)
        self.assertEqual(Path("/data/qqbot/db/qqbot.sqlite3"), s.prod_db)
        self.assertEqual(Path("/data/qqbot/stickers"), s.prod_sticker_root)
        self.assertEqual(Path("/data/persona/stickers"), s.own_sticker_root)
        self.assertEqual("", s.owner_qq)
        self.assertEqual("http://dsh-persona:13101", s.bridge_url)
        self.assertEqual(24, s.media_batch_limit)
        self.assertEqual(12, s.media_prefetch_limit)
        self.assertEqual(24, s.media_prompt_limit)
        self.assertEqual("mimo-v2.5-tts", s.tts_model)
        self.assertEqual("冰糖", s.tts_voice)
        self.assertEqual(120, s.recall_max_age_seconds)
        # 2026-10-25 安全复核：高危 debug 开关必须默认关闭
        self.assertFalse(s.debug_routes)
        self.assertFalse(s.debug_sendfile)
        self.assertIn("qzone.qq.com", s.browser_login_hosts)
        self.assertIn("bilibili.com", s.browser_login_hosts)

    def test_overrides_and_derived_paths(self):
        with tempfile.TemporaryDirectory() as tmp:
            s = settings_mod.load_settings({
                "FISHTANK_DATA_ROOT": tmp,
                "FISHTANK_PERSONA_ROOT": tmp + "/persona",
                "FISHTANK_OWNER_QQ": "12345",
                "BOT_QQ": "67890",
                "PERSONA_BRIDGE_URL": "http://bridge:19191/",
            })
            self.assertEqual(Path(tmp) / "db" / "perception.sqlite3", s.db_path)
            self.assertEqual(Path(tmp) / "persona" / "stickers", s.own_sticker_root)
            self.assertEqual("http://bridge:19191", s.bridge_url)
            self.assertEqual("12345", s.owner_qq)
            self.assertEqual("67890", s.bot_qq)

    def test_explicit_file_overrides_root(self):
        s = settings_mod.load_settings({
            "FISHTANK_DATA_ROOT": "/srv/qqbot",
            "FISHTANK_DB_PATH": "/var/lib/fishtank/messages.sqlite3",
            "FISHTANK_PROD_DB": "/var/lib/fishtank/prod.sqlite3",
        })
        self.assertEqual(Path("/var/lib/fishtank/messages.sqlite3"), s.db_path)
        self.assertEqual(Path("/var/lib/fishtank/prod.sqlite3"), s.prod_db)
