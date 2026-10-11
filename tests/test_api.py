"""Тести для FastAPI-ендпоінтів моніторингу та здоров'я (/health, /targets)."""
import tempfile
import unittest
from pathlib import Path

from starlette.testclient import TestClient

from src.api.server import app, set_app_state
from src.engine.tracker import TargetTracker
from src.learning.reference import (
    ActiveReferenceCache,
    ReferenceRepository,
    seed_from_dictionary,
)


class TestApiServer(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.repo = ReferenceRepository(Path(self.tmp.name) / "ref")
        v1 = self.repo.save_new(seed_from_dictionary())
        self.repo.promote(v1.version)
        self.cache = ActiveReferenceCache(self.repo)
        self.tracker = TargetTracker(self.cache)
        set_app_state(tracker=self.tracker, ref_cache=self.cache)
        self.client = TestClient(app)

    def tearDown(self):
        self.tmp.cleanup()

    def test_healthcheck_endpoint(self):
        resp = self.client.get("/health")
        self.assertEqual(resp.status_code, 200)
        data = resp.json()
        self.assertEqual(data["status"], "ok")
        self.assertEqual(data["reference_version"], 1)
        self.assertEqual(data["active_targets_count"], 0)
        self.assertIn("siren_alert_active", data)

    def test_targets_endpoint_empty(self):
        resp = self.client.get("/targets")
        self.assertEqual(resp.status_code, 200)
        self.assertEqual(resp.json(), [])

    def test_test_alert_endpoint(self):
        # 1. Without bot/subscribers configured
        resp = self.client.post("/test-alert")
        self.assertEqual(resp.status_code, 200)
        self.assertEqual(resp.json()["status"], "error")

        # 2. With mock subscribers and mock bot
        from unittest.mock import AsyncMock, MagicMock

        from src.telegram.bot import SubscriberRepository

        mock_bot = MagicMock()
        mock_bot.send_message = AsyncMock()
        mock_subscribers = MagicMock(spec=SubscriberRepository)
        mock_subscribers.count.return_value = 2
        mock_subscribers.get_all.return_value = [111, 222]

        set_app_state(
            tracker=self.tracker,
            ref_cache=self.cache,
            bot=mock_bot,
            subscribers=mock_subscribers,
        )

        resp = self.client.post("/test-alert?type=drone")
        self.assertEqual(resp.status_code, 200)
        data = resp.json()
        self.assertEqual(data["status"], "ok")
        self.assertEqual(data["alert_type"], "drone")
        self.assertEqual(data["subscribers_notified"], 2)
        self.assertEqual(mock_bot.send_message.call_count, 2)


if __name__ == "__main__":
    unittest.main()
