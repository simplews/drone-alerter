"""Tests for the target tracker (TargetTracker)."""
import tempfile
import unittest
from datetime import UTC, datetime, timedelta
from pathlib import Path

from src.engine.models import ActionType, AlertLevel, RawPost, TargetStatus
from src.engine.tracker import TargetTracker
from src.learning.reference import (
    ActiveReferenceCache,
    ReferenceRepository,
    seed_from_profile,
)
from src.learning.store import OUTCOME_UNKNOWN, LearningStore
from src.nlp.normalizer import MessageNormalizer

T0 = datetime(2026, 10, 10, 21, 28, tzinfo=UTC)


class TestTargetTracker(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.repo = ReferenceRepository(Path(self.tmp.name) / "ref")
        v1 = self.repo.save_new(seed_from_profile())
        self.repo.promote(v1.version)
        self.cache = ActiveReferenceCache(self.repo)
        self.normalizer = MessageNormalizer(self.cache)
        self.tracker = TargetTracker(self.cache)

    def tearDown(self):
        self.tmp.cleanup()

    def test_escalation_flow_levels_1_2_3a_3b(self):
        # 1. Nova Ushytsia (level 1, silent)
        p1 = RawPost("chan_a", 1, "Реактивна герань на Нову Ушицю", T0)
        alerts1 = self.tracker.process_message(p1, self.normalizer.parse(p1), now=T0)
        self.assertEqual(len(alerts1), 1)
        self.assertEqual(alerts1[0].level, AlertLevel.LEVEL_1)
        self.assertTrue(alerts1[0].is_silent)

        # 2. Borshchiv (level 2, loud)
        t2 = T0 + timedelta(minutes=10)
        p2 = RawPost("chan_a", 2, "Борщів", t2)
        alerts2 = self.tracker.process_message(p2, self.normalizer.parse(p2), now=t2)
        self.assertEqual(len(alerts2), 1)
        self.assertEqual(alerts2[0].level, AlertLevel.LEVEL_2)
        self.assertFalse(alerts2[0].is_silent)

        # 3. Tlumach (level 3a, close approach)
        t3 = T0 + timedelta(minutes=18)
        p3 = RawPost("chan_a", 3, "Тлумач", t3)
        alerts3 = self.tracker.process_message(p3, self.normalizer.parse(p3), now=t3)
        self.assertEqual(len(alerts3), 1)
        self.assertEqual(alerts3[0].level, AlertLevel.LEVEL_3A)
        self.assertFalse(alerts3[0].is_silent)

        # 4. Khryplyn (level 3b, over the city)
        t4 = T0 + timedelta(minutes=24)
        p4 = RawPost("chan_a", 4, "Хриплин", t4)
        alerts4 = self.tracker.process_message(p4, self.normalizer.parse(p4), now=t4)
        self.assertEqual(len(alerts4), 1)
        self.assertEqual(alerts4[0].level, AlertLevel.LEVEL_3B)
        self.assertFalse(alerts4[0].is_silent)

    def test_direct_level_2_entry_without_level_1(self):
        # Requirement F5: the target comes from the south, passes Ushytsia and goes straight to Kamianets-Podilskyi
        p1 = RawPost("chan_a", 10, "Реактивна герань на Камʼянець-Подільський", T0)
        alerts = self.tracker.process_message(p1, self.normalizer.parse(p1), now=T0)
        self.assertEqual(len(alerts), 1)
        self.assertEqual(alerts[0].level, AlertLevel.LEVEL_2)
        self.assertFalse(alerts[0].is_silent)

    def test_multi_target_decrement_on_downed(self):
        # 2 targets to Tlumach
        p1 = RawPost("chan_a", 20, "2х Реактивні БпЛА через Тлумач", T0)
        alerts1 = self.tracker.process_message(p1, self.normalizer.parse(p1), now=T0)
        self.assertEqual(len(alerts1), 1)
        targets = self.tracker.get_active_targets()
        self.assertEqual(len(targets), 1)
        self.assertEqual(targets[0].target_count, 2)

        # Message about a shoot-down of one target
        p2 = RawPost("chan_a", 21, "Біля Тлумача одну збито!", T0 + timedelta(minutes=2))
        alerts2 = self.tracker.process_message(p2, self.normalizer.parse(p2), now=T0 + timedelta(minutes=2))
        self.assertEqual(len(alerts2), 1)
        self.assertIn("ще 1 ціль(і) у повітрі", alerts2[0].body)
        # The target stays active!
        self.assertEqual(targets[0].status, TargetStatus.ACTIVE)
        self.assertEqual(targets[0].target_count, 1)

        # Shoot-down of the last target
        p3 = RawPost("chan_a", 22, "Другу також збили біля Тлумача", T0 + timedelta(minutes=4))
        alerts3 = self.tracker.process_message(p3, self.normalizer.parse(p3), now=T0 + timedelta(minutes=4))
        self.assertEqual(len(alerts3), 1)
        self.assertEqual(targets[0].status, TargetStatus.PROBABLY_DOWNED)
        self.assertIn("імовірно, уражена або втрачена", alerts3[0].body)
        self.assertIn("до офіційного відбою", alerts3[0].body)

    def test_cross_channel_deduplication(self):
        # Channel A: Borshchiv at 21:30
        p1 = RawPost("chan_a", 1, "Реактивна герань на Борщів", T0)
        alerts1 = self.tracker.process_message(p1, self.normalizer.parse(p1), now=T0)
        self.assertEqual(len(alerts1), 1)

        # Channel B: Borshchiv at 21:31 (deduplicated into the same target)
        p2 = RawPost("chan_b", 5, "Борщів - реактивний шахед курсом на Франківщину", T0 + timedelta(minutes=1))
        alerts2 = self.tracker.process_message(p2, self.normalizer.parse(p2), now=T0 + timedelta(minutes=1))
        # It must not create a new target!
        self.assertEqual(len(self.tracker.get_active_targets()), 1)
        # Anti-spam: the code does not send a new loud alert (is_update = True)
        self.assertTrue(alerts2[0].is_update)

    def test_divert_message_with_and_without_siren(self):
        # 1. Divert without siren
        tracker_no_siren = TargetTracker(self.cache, is_siren_active_fn=lambda: False)
        p1 = RawPost("chan_a", 1, "Реактивна герань на Нову Ушицю", T0)
        tracker_no_siren.process_message(p1, self.normalizer.parse(p1), now=T0)

        p_div1 = RawPost("chan_a", 2, "Ціль відвернула в бік Хмельницького", T0 + timedelta(minutes=5))
        alerts1 = tracker_no_siren.process_message(p_div1, self.normalizer.parse(p_div1), now=T0 + timedelta(minutes=5))
        self.assertEqual(len(alerts1), 1)
        self.assertEqual(alerts1[0].title, "Ціль змінила курс")
        self.assertIn("Ціль змінила курс у напрямку іншої області", alerts1[0].body)
        self.assertIn("Слідкуйте за офіційними повідомленнями", alerts1[0].body)

        # 2. Divert with active siren
        tracker_siren = TargetTracker(self.cache, is_siren_active_fn=lambda: True)
        p2 = RawPost("chan_a", 3, "Реактивна герань на Нову Ушицю", T0)
        tracker_siren.process_message(p2, self.normalizer.parse(p2), now=T0)

        p_div2 = RawPost("chan_a", 4, "Ціль відвернула на північ", T0 + timedelta(minutes=5))
        alerts2 = tracker_siren.process_message(p_div2, self.normalizer.parse(p_div2), now=T0 + timedelta(minutes=5))
        self.assertEqual(len(alerts2), 1)
        self.assertEqual(alerts2[0].title, "Ціль змінила курс")
        self.assertIn("змінила курс і прямує в іншому напрямку", alerts2[0].body)
        self.assertIn("Залишайтеся в укриттях до оголошення відбою", alerts2[0].body)

    def test_level_1_target_eta_timeout_marked_lost(self):
        store = LearningStore(":memory:")
        tracker = TargetTracker(self.cache, store=store)

        p1 = RawPost("chan_a", 1, "Реактивна герань на Нову Ушицю", T0)
        tracker.process_message(p1, self.normalizer.parse(p1), now=T0)
        targets = tracker.get_active_targets()
        self.assertEqual(len(targets), 1)
        target = targets[0]
        self.assertEqual(target.level, AlertLevel.LEVEL_1)
        self.assertIsNotNone(target.eta_window_end_utc)

        # Before ETA expiration: check_timeouts returns no alerts, target is ACTIVE
        before_end = target.eta_window_end_utc - timedelta(minutes=1)
        self.assertEqual(len(tracker.check_timeouts(now=before_end)), 0)
        self.assertEqual(target.status, TargetStatus.ACTIVE)

        # After ETA expiration: target transitions to LOST, alert is returned
        after_end = target.eta_window_end_utc + timedelta(seconds=10)
        timeout_alerts = tracker.check_timeouts(now=after_end)
        self.assertEqual(len(timeout_alerts), 1)
        self.assertEqual(timeout_alerts[0].title, "Ціль локаційно втрачена")
        self.assertIn("Можливо локаційно втрачена", timeout_alerts[0].body)
        self.assertIn("Слідкуйте за подальшими повідомленнями", timeout_alerts[0].body)
        self.assertEqual(target.status, TargetStatus.LOST)
        self.assertEqual(len(tracker.get_active_targets()), 0)

        # Check that it was labeled OUTCOME_UNKNOWN in LearningStore
        row = store.conn.execute("SELECT outcome FROM targets WHERE id=?", (target.id,)).fetchone()
        self.assertIsNotNone(row)
        self.assertEqual(row["outcome"], OUTCOME_UNKNOWN)
        store.close()

    def test_level_2_target_eta_timeout_marked_lost(self):
        store = LearningStore(":memory:")
        tracker = TargetTracker(self.cache, store=store)

        # Level 2 point (Borshchiv)
        p1 = RawPost("chan_a", 1, "Реактивна герань на Борщів", T0)
        tracker.process_message(p1, self.normalizer.parse(p1), now=T0)
        targets = tracker.get_active_targets()
        self.assertEqual(len(targets), 1)
        target = targets[0]
        self.assertEqual(target.level, AlertLevel.LEVEL_2)
        self.assertIsNotNone(target.eta_window_end_utc)

        # After ETA expiration + 4 minutes buffer: Level 2 target becomes LOST
        after_end = target.eta_window_end_utc + timedelta(minutes=5)
        timeout_alerts = tracker.check_timeouts(now=after_end)
        self.assertEqual(len(timeout_alerts), 1)
        self.assertEqual(target.status, TargetStatus.LOST)
        self.assertIn("Ціль змінила курс / втрачена", timeout_alerts[0].title)
        self.assertIn("час очікування минув", timeout_alerts[0].body)
        self.assertEqual(len(tracker.get_active_targets()), 0)

        # Labeled OUTCOME_UNKNOWN in learning store
        row = store.conn.execute("SELECT outcome FROM targets WHERE id=?", (target.id,)).fetchone()
        self.assertIsNotNone(row)
        self.assertEqual(row["outcome"], OUTCOME_UNKNOWN)
        store.close()

    def test_divert_keywords_from_profile(self):
        # 1. Target at Dunaivtsi
        p1 = RawPost("chan_a", 1, "Реактивний БпЛА на Дунаївці", T0)
        self.tracker.process_message(p1, self.normalizer.parse(p1), now=T0)
        self.assertEqual(len(self.tracker.get_active_targets()), 1)

        # 2. Channel reports drone heading to Gorodok / Ternopil
        p_div = RawPost("chan_a", 2, "Реактивний БпЛА на Городок у північно-західному напрямку", T0 + timedelta(minutes=4))
        parsed_div = self.normalizer.parse(p_div)
        self.assertEqual(parsed_div.action, ActionType.DIVERTED)

        alerts = self.tracker.process_message(p_div, parsed_div, now=T0 + timedelta(minutes=4))
        self.assertEqual(len(alerts), 1)
        self.assertEqual(alerts[0].title, "Ціль змінила курс")
        self.assertEqual(len(self.tracker.get_active_targets()), 0)

    def test_multi_channel_lag_deduplication(self):
        """Replicate the real attack episode:
        - 20:19: kudy_letyt -> Nova Ushytsia
        - 20:20: monitoring_zaxid -> Dunaivtsi
        - 20:22: kpszsu -> Nova Ushytsia (lagged report from slower official channel)
        - 20:24: kudy_letyt -> Dunaivtsi (repeat / update)
        All 4 posts describe a SINGLE drone track. Only 2 alerts must be emitted (Nova Ushytsia, then Dunaivtsi).
        """
        t1 = datetime(2026, 10, 12, 17, 19, 3, tzinfo=UTC)
        p1 = RawPost("kudy_letyt", 101, "Реактивна герань на Нову Ушицю", t1)
        alerts1 = self.tracker.process_message(p1, self.normalizer.parse(p1), now=t1)
        self.assertEqual(len(alerts1), 1)
        self.assertFalse(alerts1[0].is_update)
        self.assertEqual(len(self.tracker.get_active_targets()), 1)

        t2 = datetime(2026, 10, 12, 17, 20, 16, tzinfo=UTC)
        p2 = RawPost("monitoring_zaxid", 102, "Дунаївці - реактивний шахед курсом на Франківщину", t2)
        alerts2 = self.tracker.process_message(p2, self.normalizer.parse(p2), now=t2)
        self.assertEqual(len(alerts2), 1)
        self.assertFalse(alerts2[0].is_update)
        self.assertEqual(len(self.tracker.get_active_targets()), 1)
        self.assertEqual(self.tracker.get_active_targets()[0].last_point_id, "dunaivtsi")

        # 3. Lagged post from kpszsu reporting Nova Ushytsia (upstream of Dunaivtsi)
        t3 = datetime(2026, 10, 12, 17, 22, 34, tzinfo=UTC)
        p3 = RawPost("kpszsu", 103, "БпЛА реактивного типу на Нову Ушицю курсом на захід", t3)
        alerts3 = self.tracker.process_message(p3, self.normalizer.parse(p3), now=t3)
        self.assertEqual(len(self.tracker.get_active_targets()), 1)
        # Target position must remain at dunaivtsi (closest to city)
        self.assertEqual(self.tracker.get_active_targets()[0].last_point_id, "dunaivtsi")
        # Alert must be flagged as update (not broadcast)
        self.assertEqual(len(alerts3), 1)
        self.assertTrue(alerts3[0].is_update)

        # 4. Update from kudy_letyt reporting Dunaivtsi
        t4 = datetime(2026, 10, 12, 17, 24, 8, tzinfo=UTC)
        p4 = RawPost("kudy_letyt", 104, "Реактивний БпЛА курсом на Дунаївці", t4)
        alerts4 = self.tracker.process_message(p4, self.normalizer.parse(p4), now=t4)
        self.assertEqual(len(self.tracker.get_active_targets()), 1)
        self.assertEqual(self.tracker.get_active_targets()[0].last_point_id, "dunaivtsi")
        self.assertEqual(len(alerts4), 1)
        self.assertTrue(alerts4[0].is_update)

    def test_explicit_new_target_marker_creates_separate_target(self):
        """When text explicitly says 'ще один', spawn a new target even for the same point."""
        t1 = datetime(2026, 10, 12, 17, 19, 0, tzinfo=UTC)
        p1 = RawPost("kudy_letyt", 201, "Реактивний БпЛА на Нову Ушицю", t1)
        self.tracker.process_message(p1, self.normalizer.parse(p1), now=t1)
        self.assertEqual(len(self.tracker.get_active_targets()), 1)

        t2 = t1 + timedelta(minutes=2)
        p2 = RawPost("monitoring_zaxid", 202, "Ще один реактивний БпЛА на Нову Ушицю", t2)
        self.tracker.process_message(p2, self.normalizer.parse(p2), now=t2)
        self.assertEqual(len(self.tracker.get_active_targets()), 2)


if __name__ == "__main__":
    unittest.main()
