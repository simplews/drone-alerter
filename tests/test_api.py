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


if __name__ == "__main__":
    unittest.main()
