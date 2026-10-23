import asyncio
import unittest
from types import SimpleNamespace

from helpers import load_perception

interact = load_perception("interact")
triggers = load_perception("triggers")


class FakeBot:
    def __init__(self):
        self.calls = []

    async def call_api(self, api, **params):
        self.calls.append((api, params))
        return {}


class TestInteractValidation(unittest.TestCase):
    def test_target_rules(self):
        self.assertTrue(interact._target_ok("1000000002"))
        self.assertFalse(interact._target_ok("123"))
        self.assertFalse(interact._target_ok("abc"))

    def test_rejects_bad_action(self):
        result = asyncio.run(interact.start("hug", "1000000002"))
        self.assertFalse(result["ok"])

    def test_rejects_too_long_span(self):
        result = asyncio.run(interact.start("poke", "1000000002", times=20, interval_seconds=3600))
        self.assertFalse(result["ok"])


class TestInteractExecution(unittest.IsolatedAsyncioTestCase):
    def setUp(self):
        self.bot = FakeBot()
        self.orig_pick = interact.sender.pick_bot
        self.orig_config = getattr(triggers, "_config", None)
        interact.sender.pick_bot = lambda _qq=None: self.bot
        triggers._config = SimpleNamespace(bot_qq="1000000001")

    def tearDown(self):
        interact.sender.pick_bot = self.orig_pick
        triggers._config = self.orig_config

    async def test_immediate_poke(self):
        result = await interact.start("poke", "1000000002", group_id=1000000003, times=1)
        self.assertTrue(result["ok"])
        self.assertTrue(result["executed"])
        self.assertEqual("send_poke", self.bot.calls[0][0])
        self.assertEqual("1000000003", self.bot.calls[0][1]["group_id"])
        self.assertEqual("1000000002", self.bot.calls[0][1]["target_id"])

    async def test_immediate_like(self):
        result = await interact.start("like", "1000000002", times=1)
        self.assertTrue(result["ok"])
        self.assertEqual("send_like", self.bot.calls[0][0])
        self.assertEqual(1, self.bot.calls[0][1]["times"])

    async def test_scheduled_job_runs(self):
        result = await interact.start("poke", "1000000002", times=2, interval_seconds=1)
        self.assertTrue(result["ok"])
        self.assertFalse(result["executed"])
        job_id = result["job"]["job_id"]
        for _ in range(30):
            status = interact.status(job_id)
            if status["job"]["status"] in ("done", "failed"):
                break
            await asyncio.sleep(0.1)
        status = interact.status(job_id)
        self.assertEqual("done", status["job"]["status"])
        self.assertEqual(2, status["job"]["done"])
