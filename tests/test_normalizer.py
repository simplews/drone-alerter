"""Тести для парсера повідомлень та нормалізатора (MessageNormalizer)."""
import tempfile
import unittest
from datetime import datetime, timedelta, timezone
from pathlib import Path

from src.engine.models import ActionType, RawPost
from src.learning.reference import ActiveReferenceCache, ReferenceRepository, seed_from_dictionary
from src.nlp.normalizer import MessageNormalizer

T0 = datetime(2026, 10, 10, 21, 28, tzinfo=timezone.utc)


class TestMessageNormalizer(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.repo = ReferenceRepository(Path(self.tmp.name) / "ref")
        v1 = self.repo.save_new(seed_from_dictionary())
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
        # Пост 1: канал пише про реактивну ціль
        p1 = RawPost("chan_a", 1, "Реактивна герань на Стару Ушицю", T0)
        parsed1 = self.normalizer.parse(p1)
        self.assertTrue(parsed1.is_jet)
        self.assertEqual(parsed1.points, ["stara_ushytsia"])

        # Пост 2 через 3 хв: той самий канал пише просто назву міста
        p2 = RawPost("chan_a", 2, "Камʼянець-Подільський", T0 + timedelta(minutes=3))
        parsed2 = self.normalizer.parse(p2)
        # Тип має успадкуватись з контексту!
        self.assertTrue(parsed2.is_jet)
        self.assertEqual(parsed2.points, ["kamianets_podilskyi"])

        # Пост 3 через 15 хв: наступний пункт
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

        p2 = RawPost("chan_a", 21, "Відвернув на Рівне", T0)
        parsed2 = self.normalizer.parse(p2)
        self.assertEqual(parsed2.action, ActionType.DIVERTED)


if __name__ == "__main__":
    unittest.main()
