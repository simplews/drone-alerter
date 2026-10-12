"""Tests for the city profile loader and the seed of the reference."""
import copy
import json
import sqlite3
import tempfile
import unittest
from pathlib import Path

from src.learning.reference import seed_from_profile
from src.learning.store import LearningStore
from src.profile import load_profile, parse_profile

PROFILES_DIR = Path(__file__).resolve().parent.parent / "data" / "profiles"


def _valid() -> dict:
    return json.loads((PROFILES_DIR / "example-city.json").read_text(encoding="utf-8"))


class ProfileTests(unittest.TestCase):
    def test_all_shipped_profiles_are_valid(self):
        files = sorted(PROFILES_DIR.glob("*.json"))
        self.assertTrue(files)
        for path in files:
            profile = load_profile(path.stem, PROFILES_DIR)
            self.assertTrue(seed_from_profile(profile).points)

    def test_seed_uses_profile_data(self):
        profile = load_profile("example-city", PROFILES_DIR)
        ref = seed_from_profile(profile)
        self.assertEqual(set(ref.points), {"alpha", "bravo", "charlie"})
        self.assertIsNone(ref.points["alpha"].eta_min)
        self.assertEqual(ref.points["alpha"].distance_km, 120.0)
        self.assertEqual(ref.stopwords, profile.stopwords)

    def test_rejects_path_traversal_name(self):
        with self.assertRaises(ValueError):
            load_profile("../secrets", PROFILES_DIR)

    def test_rejects_duplicate_point_id(self):
        data = _valid()
        data["points"].append(copy.deepcopy(data["points"][0]))
        with self.assertRaises(ValueError):
            parse_profile(data)

    def test_rejects_bad_level_and_missing_text(self):
        data = _valid()
        data["points"][0]["level"] = 7
        with self.assertRaises(ValueError):
            parse_profile(data)
        data = _valid()
        del data["texts"]["city_acc"]
        with self.assertRaises(ValueError):
            parse_profile(data)

    def test_rejects_id_that_differs_from_file_name(self):
        with tempfile.TemporaryDirectory() as tmp:
            (Path(tmp) / "other.json").write_text(json.dumps(_valid()), encoding="utf-8")
            with self.assertRaises(ValueError):
                load_profile("other", tmp)


class LegacyOutcomeMigrationTests(unittest.TestCase):
    def test_old_labels_are_renamed(self):
        with tempfile.TemporaryDirectory() as tmp:
            db = Path(tmp) / "learning.db"
            LearningStore(db).close()
            conn = sqlite3.connect(db)
            conn.execute("INSERT INTO targets (id, first_seen_utc, outcome) VALUES ('a', 'x', 'reached_if')")
            conn.execute("INSERT INTO targets (id, first_seen_utc, outcome) VALUES ('b', 'x', 'downed_en_route_if')")
            conn.commit()
            conn.close()
            store = LearningStore(db)
            rows = dict(store.conn.execute("SELECT id, outcome FROM targets").fetchall())
            store.close()
        self.assertEqual(rows, {"a": "reached_target", "b": "downed_en_route"})
