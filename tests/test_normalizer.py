"""Tests for the message parser and the normalizer (MessageNormalizer)."""
import tempfile
import unittest
from datetime import UTC, datetime, timedelta
from pathlib import Path

from src.engine.models import ActionType, RawPost
from src.learning.reference import (
    ActiveReferenceCache,
    ReferenceRepository,
    seed_from_profile,
)
from src.nlp.normalizer import MessageNormalizer

T0 = datetime(2026, 10, 10, 21, 28, tzinfo=UTC)


class TestMessageNormalizer(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.repo = ReferenceRepository(Path(self.tmp.name) / "ref")
        v1 = self.repo.save_new(seed_from_profile())
        self.repo.promote(v1.version)
        self.cache = ActiveReferenceCache(self.repo)
        self.normalizer = MessageNormalizer(self.cache)

    def tearDown(self):
        self.tmp.cleanup()

    def test_explicit_jet_detection(self):
        post = RawPost("chan_a", 1, "Реактивна герань на Нову Ушицю", T0)
        parsed = self.normalizer.parse(post)
        self.assertTrue(parsed.is_jet)
        self.assertEqual(parsed.points, ["nova_ushytsia"])
        self.assertEqual(parsed.count, 1)

    def test_contextual_type_inheritance(self):
        # Post 1: the channel writes about a jet target
        p1 = RawPost("chan_a", 1, "Реактивна герань на Стару Ушицю", T0)
        parsed1 = self.normalizer.parse(p1)
        self.assertTrue(parsed1.is_jet)
        self.assertEqual(parsed1.points, ["stara_ushytsia"])

        # Post 2, 3 minutes later: the same channel writes only a city name
        p2 = RawPost("chan_a", 2, "Камʼянець-Подільський", T0 + timedelta(minutes=3))
        parsed2 = self.normalizer.parse(p2)
        # The type must be inherited from the context!
        self.assertTrue(parsed2.is_jet)
        self.assertEqual(parsed2.points, ["kamianets_podilskyi"])

        # Post 3, 15 minutes later: the next point
        p3 = RawPost("chan_a", 3, "Товсте", T0 + timedelta(minutes=15))
        parsed3 = self.normalizer.parse(p3)
        self.assertTrue(parsed3.is_jet)
        self.assertEqual(parsed3.points, ["tovste"])

    def test_multi_target_count(self):
        p1 = RawPost("chan_b", 10, "2х Реактивні БпЛА залітають на Івано-Франківськ через Тлумач", T0)
        parsed1 = self.normalizer.parse(p1)
        self.assertTrue(parsed1.is_jet)
        self.assertEqual(parsed1.count, 2)
        self.assertEqual(parsed1.points, ["tlumach"])

    def test_action_detection(self):
        p1 = RawPost("chan_a", 20, "Ціль біля Тлумача збито!", T0)
        parsed1 = self.normalizer.parse(p1)
        self.assertEqual(parsed1.action, ActionType.DOWNED)
        self.assertEqual(parsed1.action_point_id, "tlumach")

    def test_multi_region_digest_count_isolation(self):
        # Check the case of 12 October: the summary has 2 targets over Kirovohrad region, but only 1 for Nova Ushytsia
        text = (
            "Хмельниччина:\n"
            "Реактивний БпЛА курсом на Нову Ушицю\n\n"
            "Кіровоградщина:\n"
            "2х Реактивні БпЛА курсом на Новомиргород"
        )
        post = RawPost("kudy_letyt", 122404, text, T0)
        parsed = self.normalizer.parse(post)
        self.assertTrue(parsed.is_jet)
        self.assertEqual(parsed.points, ["nova_ushytsia"])
        # It must be 1, not 2!
        self.assertEqual(parsed.count, 1)

    def test_new_target_marker(self):
        post = RawPost("chan_a", 30, "Ще один реактивний БпЛА на Нову Ушицю", T0)
        parsed = self.normalizer.parse(post)
        self.assertTrue(parsed.is_jet)
        self.assertTrue(parsed.is_new_target_marker)

        post2 = RawPost("chan_a", 31, "Реактивний БпЛА на Нову Ушицю", T0)
        parsed2 = self.normalizer.parse(post2)
        self.assertFalse(parsed2.is_new_target_marker)

    def test_divert_destination_extraction(self):
        # 1. Destination from cancel keywords
        p1 = RawPost("chan_a", 40, "Реактивний БпЛА на Закупне", T0)
        parsed1 = self.normalizer.parse(p1)
        self.assertEqual(parsed1.action, ActionType.DIVERTED)
        self.assertEqual(parsed1.divert_destination, "Закупне")

        # 2. Destination from cardinal direction
        p2 = RawPost("chan_a", 41, "Ціль відвернула на північ", T0)
        parsed2 = self.normalizer.parse(p2)
        self.assertEqual(parsed2.action, ActionType.DIVERTED)
        self.assertEqual(parsed2.divert_destination, "Північ")

        # 3. Passing corridor point with destination
        p3 = RawPost("chan_a", 42, "Реактивний БпЛА курсом на Городок повз Дунаївці", T0)
        parsed3 = self.normalizer.parse(p3)
        self.assertEqual(parsed3.action, ActionType.DIVERTED)
        self.assertEqual(parsed3.action_point_id, "dunaivtsi")
        self.assertEqual(parsed3.divert_destination, "Городок")


if __name__ == "__main__":
    unittest.main()

