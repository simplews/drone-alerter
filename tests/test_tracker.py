"""Тести для трекера цілей (TargetTracker)."""
import tempfile
import unittest
from datetime import datetime, timedelta, timezone
from pathlib import Path

from src.engine.models import AlertLevel, RawPost, TargetStatus
from src.engine.tracker import TargetTracker
from src.learning.reference import ActiveReferenceCache, ReferenceRepository, seed_from_dictionary
from src.nlp.normalizer import MessageNormalizer

T0 = datetime(2026, 10, 10, 21, 28, tzinfo=timezone.utc)


class TestTargetTracker(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.repo = ReferenceRepository(Path(self.tmp.name) / "ref")
        v1 = self.repo.save_new(seed_from_dictionary())
        self.repo.promote(v1.version)
        self.cache = ActiveReferenceCache(self.repo)
        self.normalizer = MessageNormalizer(self.cache)
        self.tracker = TargetTracker(self.cache)

    def tearDown(self):
        self.tmp.cleanup()

    def test_escalation_flow_levels_1_2_3a_3b(self):
        # 1. Нова Ушиця (Рівень 1 - silent)
        p1 = RawPost("chan_a", 1, "Реактивна герань на Нову Ушицю", T0)
        alerts1 = self.tracker.process_message(p1, self.normalizer.parse(p1), now=T0)
        self.assertEqual(len(alerts1), 1)
        self.assertEqual(alerts1[0].level, AlertLevel.LEVEL_1)
        self.assertTrue(alerts1[0].is_silent)

        # 2. Борщів (Рівень 2 - loud)
        t2 = T0 + timedelta(minutes=10)
        p2 = RawPost("chan_a", 2, "Борщів", t2)
        alerts2 = self.tracker.process_message(p2, self.normalizer.parse(p2), now=t2)
        self.assertEqual(len(alerts2), 1)
        self.assertEqual(alerts2[0].level, AlertLevel.LEVEL_2)
        self.assertFalse(alerts2[0].is_silent)

        # 3. Тлумач (Рівень 3а - підліт)
        t3 = T0 + timedelta(minutes=18)
        p3 = RawPost("chan_a", 3, "Тлумач", t3)
        alerts3 = self.tracker.process_message(p3, self.normalizer.parse(p3), now=t3)
        self.assertEqual(len(alerts3), 1)
        self.assertEqual(alerts3[0].level, AlertLevel.LEVEL_3A)
        self.assertFalse(alerts3[0].is_silent)

        # 4. Хриплин (Рівень 3б - над містом)
        t4 = T0 + timedelta(minutes=24)
        p4 = RawPost("chan_a", 4, "Хриплин", t4)
        alerts4 = self.tracker.process_message(p4, self.normalizer.parse(p4), now=t4)
        self.assertEqual(len(alerts4), 1)
        self.assertEqual(alerts4[0].level, AlertLevel.LEVEL_3B)
        self.assertFalse(alerts4[0].is_silent)

    def test_direct_level_2_entry_without_level_1(self):
        # Вимога F5: ціль заходить з півдня повз Ушицю відразу через Камʼянець
        p1 = RawPost("chan_a", 10, "Реактивна герань на Камʼянець-Подільський", T0)
        alerts = self.tracker.process_message(p1, self.normalizer.parse(p1), now=T0)
        self.assertEqual(len(alerts), 1)
        self.assertEqual(alerts[0].level, AlertLevel.LEVEL_2)
        self.assertFalse(alerts[0].is_silent)

    def test_multi_target_decrement_on_downed(self):
        # 2 цілі на Тлумач
        p1 = RawPost("chan_a", 20, "2х Реактивні БпЛА через Тлумач", T0)
        alerts1 = self.tracker.process_message(p1, self.normalizer.parse(p1), now=T0)
        self.assertEqual(len(alerts1), 1)
        targets = self.tracker.get_active_targets()
        self.assertEqual(len(targets), 1)
        self.assertEqual(targets[0].target_count, 2)

        # Повідомлення про збиття однієї
        p2 = RawPost("chan_a", 21, "Біля Тлумача одну збито!", T0 + timedelta(minutes=2))
        alerts2 = self.tracker.process_message(p2, self.normalizer.parse(p2), now=T0 + timedelta(minutes=2))
        self.assertEqual(len(alerts2), 1)
        self.assertIn("ще 1 ціль(і) у повітрі", alerts2[0].body)
        # Ціль залишається активною!
        self.assertEqual(targets[0].status, TargetStatus.ACTIVE)
        self.assertEqual(targets[0].target_count, 1)

        # Збиття останньої цілі
        p3 = RawPost("chan_a", 22, "Другу також збили біля Тлумача", T0 + timedelta(minutes=4))
        alerts3 = self.tracker.process_message(p3, self.normalizer.parse(p3), now=T0 + timedelta(minutes=4))
        self.assertEqual(len(alerts3), 1)
        self.assertEqual(targets[0].status, TargetStatus.PROBABLY_DOWNED)
        self.assertIn("імовірно, уражена або втрачена", alerts3[0].body)
        self.assertIn("до офіційного відбою", alerts3[0].body)

    def test_cross_channel_deduplication(self):
        # Канал А: Борщів о 21:30
        p1 = RawPost("chan_a", 1, "Реактивна герань на Борщів", T0)
        alerts1 = self.tracker.process_message(p1, self.normalizer.parse(p1), now=T0)
        self.assertEqual(len(alerts1), 1)

        # Канал Б: Борщів о 21:31 (дедуплікація в ту саму ціль)
        p2 = RawPost("chan_b", 5, "Борщів - реактивний шахед курсом на Франківщину", T0 + timedelta(minutes=1))
        alerts2 = self.tracker.process_message(p2, self.normalizer.parse(p2), now=T0 + timedelta(minutes=1))
        # Не повинно створити нову ціль!
        self.assertEqual(len(self.tracker.get_active_targets()), 1)
        # Антиспам: нове гучне сповіщення не надсилається (is_update = True)
        self.assertTrue(alerts2[0].is_update)


if __name__ == "__main__":
    unittest.main()
