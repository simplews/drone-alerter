"""Tests for the target tracker (TargetTracker)."""
import tempfile
import unittest
from datetime import UTC, datetime, timedelta
from pathlib import Path

from src.engine.models import ActionType, AlertLevel, DroneType, RawPost, TargetStatus
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
        self.assertEqual(alerts1[0].title, "🟡 Потенційна загроза (реактивна герань): Нова Ушиця")

        # 2. Borshchiv (level 2, loud) - inherits target_type from earlier post
        t2 = T0 + timedelta(minutes=10)
        p2 = RawPost("chan_a", 2, "Борщів", t2)
        alerts2 = self.tracker.process_message(p2, self.normalizer.parse(p2), now=t2)
        self.assertEqual(len(alerts2), 1)
        self.assertEqual(alerts2[0].level, AlertLevel.LEVEL_2)
        self.assertFalse(alerts2[0].is_silent)
        self.assertEqual(alerts2[0].title, "🟠 Наближення (реактивна герань): Борщів")

        # 3. Tlumach (level 3a, close approach)
        t3 = T0 + timedelta(minutes=18)
        p3 = RawPost("chan_a", 3, "Тлумач", t3)
        alerts3 = self.tracker.process_message(p3, self.normalizer.parse(p3), now=t3)
        self.assertEqual(len(alerts3), 1)
        self.assertEqual(alerts3[0].level, AlertLevel.LEVEL_3A)
        self.assertFalse(alerts3[0].is_silent)
        self.assertEqual(alerts3[0].title, "🔴 Небезпека (підліт) (реактивна герань): Тлумач")

        # 4. Khryplyn (level 3b, over the city)
        t4 = T0 + timedelta(minutes=24)
        p4 = RawPost("chan_a", 4, "Хриплин", t4)
        alerts4 = self.tracker.process_message(p4, self.normalizer.parse(p4), now=t4)
        self.assertEqual(len(alerts4), 1)
        self.assertEqual(alerts4[0].level, AlertLevel.LEVEL_3B)
        self.assertFalse(alerts4[0].is_silent)
        self.assertEqual(alerts4[0].title, "🔴 Небезпека біля / над містом (реактивна герань): Хриплин")

    def test_direct_level_2_entry_without_level_1(self):
        # Requirement F5: the target comes from the south, passes Ushytsia and goes straight to Kamianets-Podilskyi
        p1 = RawPost("chan_a", 10, "Реактивна герань на Камʼянець-Подільський", T0)
        alerts = self.tracker.process_message(p1, self.normalizer.parse(p1), now=T0)
        self.assertEqual(len(alerts), 1)
        self.assertEqual(alerts[0].level, AlertLevel.LEVEL_2)
        self.assertFalse(alerts[0].is_silent)
        self.assertEqual(alerts[0].title, "🟠 Наближення (реактивна герань): Камʼянець-Подільський")


    def test_multi_target_decrement_on_downed(self):
        # 1. Without active siren (default in self.tracker)
        p1 = RawPost("chan_a", 20, "2х Реактивні БпЛА через Тлумач", T0)
        alerts1 = self.tracker.process_message(p1, self.normalizer.parse(p1), now=T0)
        self.assertEqual(len(alerts1), 1)
        targets = self.tracker.get_active_targets()
        self.assertEqual(len(targets), 1)
        self.assertEqual(targets[0].target_count, 2)

        # Message about a shoot-down of one target (no siren active)
        p2 = RawPost("chan_a", 21, "Біля Тлумача одну збито!", T0 + timedelta(minutes=2))
        alerts2 = self.tracker.process_message(p2, self.normalizer.parse(p2), now=T0 + timedelta(minutes=2))
        self.assertEqual(len(alerts2), 1)
        self.assertIn("Ще 1 ціль(і) може бути в повітрі", alerts2[0].body)
        self.assertIn("не ігноруйте сигнали повітряної тривоги, якщо її буде оголошено", alerts2[0].body)
        self.assertNotIn("до офіційного відбою", alerts2[0].body)
        # The target stays active!
        self.assertEqual(targets[0].status, TargetStatus.ACTIVE)
        self.assertEqual(targets[0].target_count, 1)

        # Shoot-down of the last target (no siren active)
        p3 = RawPost("chan_a", 22, "Другу також збили біля Тлумача", T0 + timedelta(minutes=4))
        alerts3 = self.tracker.process_message(p3, self.normalizer.parse(p3), now=T0 + timedelta(minutes=4))
        self.assertEqual(len(alerts3), 1)
        self.assertEqual(targets[0].status, TargetStatus.PROBABLY_DOWNED)
        self.assertIn("імовірно, уражена або втрачена", alerts3[0].body)
        self.assertIn("не ігноруйте сигнали повітряної тривоги, якщо її все ж буде оголошено", alerts3[0].body)
        self.assertNotIn("до офіційного відбою", alerts3[0].body)

        # 2. With active siren
        tracker_siren = TargetTracker(self.cache, is_siren_active_fn=lambda: True)
        p4 = RawPost("chan_a", 23, "2х Реактивні БпЛА через Тлумач", T0)
        tracker_siren.process_message(p4, self.normalizer.parse(p4), now=T0)

        p5 = RawPost("chan_a", 24, "Біля Тлумача одну збито!", T0 + timedelta(minutes=2))
        alerts5 = tracker_siren.process_message(p5, self.normalizer.parse(p5), now=T0 + timedelta(minutes=2))
        self.assertEqual(len(alerts5), 1)
        self.assertIn("Залишайтеся в укриттях до офіційного відбою", alerts5[0].body)

        p6 = RawPost("chan_a", 25, "Другу також збили біля Тлумача", T0 + timedelta(minutes=4))
        alerts6 = tracker_siren.process_message(p6, self.normalizer.parse(p6), now=T0 + timedelta(minutes=4))
        self.assertEqual(len(alerts6), 1)
        self.assertIn("Це не скасування тривоги: залишайтеся в укриттях до офіційного відбою ОВА/ПС.", alerts6[0].body)

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
        self.assertIn("Ціль у районі Нова Ушиця відвернула на Хмельницького", alerts1[0].body)
        self.assertIn("не ігноруйте сигнали повітряної тривоги, якщо її все ж буде оголошено", alerts1[0].body)

        # 2. Divert with active siren
        tracker_siren = TargetTracker(self.cache, is_siren_active_fn=lambda: True)
        p2 = RawPost("chan_a", 3, "Реактивна герань на Нову Ушицю", T0)
        tracker_siren.process_message(p2, self.normalizer.parse(p2), now=T0)

        p_div2 = RawPost("chan_a", 4, "Ціль відвернула на північ", T0 + timedelta(minutes=5))
        alerts2 = tracker_siren.process_message(p_div2, self.normalizer.parse(p_div2), now=T0 + timedelta(minutes=5))
        self.assertEqual(len(alerts2), 1)
        self.assertEqual(alerts2[0].title, "Ціль змінила курс")
        self.assertIn("Ціль у районі Нова Ушиця відвернула на Північ", alerts2[0].body)
        self.assertIn("Залишайтеся в укриттях до оголошення відбою", alerts2[0].body)

        # 3. Divert to Peremyshliany
        p3 = RawPost("chan_a", 5, "Реактивна герань на Нову Ушицю", T0 + timedelta(minutes=20))
        tracker_siren.process_message(p3, self.normalizer.parse(p3), now=T0 + timedelta(minutes=20))
        p_div3 = RawPost("chan_a", 6, "Ціль відвернула на Перемишляни", T0 + timedelta(minutes=25))
        alerts3 = tracker_siren.process_message(p_div3, self.normalizer.parse(p_div3), now=T0 + timedelta(minutes=25))
        self.assertEqual(len(alerts3), 1)
        self.assertIn("Ціль у районі Нова Ушиця відвернула на Перемишляни", alerts3[0].body)

    def test_vendychany_to_murovani_kyrylivtsi_advances_target_without_divert(self):
        # User scenario: Vendychany (225 km) -> Murovani Kyrylivtsi (205 km)
        # Target is moving along the corridor towards Ivano-Frankivsk and MUST NOT trigger divert!
        p1 = RawPost("kudy_letyt", 101, "Реактивна герань на Вендичани", T0)
        alerts1 = self.tracker.process_message(p1, self.normalizer.parse(p1), now=T0)
        self.assertEqual(len(alerts1), 1)
        self.assertEqual(alerts1[0].level, AlertLevel.LEVEL_1)
        self.assertIn("Вендичани", alerts1[0].title)

        # 5 minutes later: post says target is now at / heading to Murovani Kyrylivtsi
        p2 = RawPost("monitoring_zaxid", 102, "Ціль у районі н.п. Муровані Курилівці", T0 + timedelta(minutes=5))
        parsed2 = self.normalizer.parse(p2)
        self.assertEqual(parsed2.action, ActionType.NONE)
        self.assertEqual(parsed2.points, ["murovani_kyrylivtsi"])

        alerts2 = self.tracker.process_message(p2, parsed2, now=T0 + timedelta(minutes=5))
        targets = self.tracker.get_active_targets()
        self.assertEqual(len(targets), 1)
        self.assertEqual(targets[0].last_point_id, "murovani_kyrylivtsi")
        # Ensure no divert alert was generated
        for a in alerts2:
            self.assertNotEqual(a.title, "Ціль змінила курс")



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
        self.assertIn("бути втраченою локаційно або знищеною", timeout_alerts[0].body)
        self.assertIn("Це не означає, що загрози немає", timeout_alerts[0].body)
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

    def test_unrelated_distant_post_does_not_divert_active_target(self):
        # 1. Active target at Pishchanka (Level 1, south Vinnytsia corridor)
        p1 = RawPost("chan_a", 1, "Реактивний БпЛА курсом на Піщанку", T0)
        alerts1 = self.tracker.process_message(p1, self.normalizer.parse(p1), now=T0)
        self.assertEqual(len(alerts1), 1)
        self.assertEqual(len(self.tracker.get_active_targets()), 1)
        self.assertEqual(self.tracker.get_active_targets()[0].last_point_id, "pishchanka")

        # 2. Next post 1 minute later from kpszsu reports unrelated target over Zhytomyr region / Berdychiv
        p2 = RawPost("kpszsu", 86185, "🏍 Реактивний БпЛА на Житомирщині, курс Бердичів", T0 + timedelta(minutes=1))
        parsed2 = self.normalizer.parse(p2)
        alerts2 = self.tracker.process_message(p2, parsed2, now=T0 + timedelta(minutes=1))

        # Must NOT emit divert alert or divert the target at Pishchanka
        self.assertEqual(len(alerts2), 0)
        self.assertEqual(len(self.tracker.get_active_targets()), 1)
        self.assertEqual(self.tracker.get_active_targets()[0].status, TargetStatus.ACTIVE)
        self.assertEqual(self.tracker.get_active_targets()[0].last_point_id, "pishchanka")

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

    def test_overnight_episodes_divert_to_lviv_and_ignore_bryansk(self):
        """Replicate 13 Oct overnight episodes:
        1. Drone at Dunaivtsi must NOT be diverted by 'Брянська обл -> на північ Чернігівщини'.
        2. 'Реактивний БпЛА курсом на Закупне повз Дунаївці' must divert the Dunaivtsi target, NOT create a new one.
        3. Multi-region digest where Khmelnytskyi/Ternopil has 'на Смотрич', 'на Микулинці', 'на Козову', 'на Львів'
           must divert the target immediately.
        """
        t0 = datetime(2026, 10, 13, 3, 13, 25, tzinfo=UTC)
        p1 = RawPost("monitoring_zaxid", 301, "Хмельниччина:\n1 реактивна герань на Дунаївці", t0)
        alerts1 = self.tracker.process_message(p1, self.normalizer.parse(p1), now=t0)
        self.assertEqual(len(alerts1), 1)
        self.assertEqual(len(self.tracker.get_active_targets()), 1)

        # Distant region post with 'на північ' must NOT divert our target
        t_bryansk = datetime(2026, 10, 13, 3, 16, 5, tzinfo=UTC)
        p_bryansk = RawPost("kpszsu", 302, "🏍 Реактивний БпЛА з Брянської області ➡️ на північ Чернігівщини", t_bryansk)
        parsed_bryansk = self.normalizer.parse(p_bryansk)
        self.assertEqual(parsed_bryansk.action, ActionType.NONE)
        alerts_b = self.tracker.process_message(p_bryansk, parsed_bryansk, now=t_bryansk)
        self.assertEqual(len(alerts_b), 0)
        self.assertEqual(len(self.tracker.get_active_targets()), 1)

        # Now 'Реактивний БпЛА курсом на Закупне повз Дунаївці' inside a multi-region digest
        t_zakupne = datetime(2026, 10, 13, 3, 17, 37, tzinfo=UTC)
        digest_text = (
            "Чернігівщина:\n"
            "Реактивний БпЛА курсом на Сновськ\n\n"
            "Хмельниччина:\n"
            "Реактивний БпЛА курсом на Закупне повз Дунаївці"
        )
        p_zakupne = RawPost("kudy_letyt", 303, digest_text, t_zakupne)
        parsed_z = self.normalizer.parse(p_zakupne)
        self.assertEqual(parsed_z.points, [])
        self.assertEqual(parsed_z.action, ActionType.DIVERTED)
        self.assertEqual(parsed_z.action_point_id, "dunaivtsi")

        alerts_z = self.tracker.process_message(p_zakupne, parsed_z, now=t_zakupne)
        self.assertEqual(len(alerts_z), 1)
        self.assertEqual(alerts_z[0].title, "Ціль змінила курс")
        self.assertEqual(len(self.tracker.get_active_targets()), 0)

        # Also verify Smotrych / Mykulyntsi / Kozova / Lviv digest diverts an active target
        t_ep1 = datetime(2026, 10, 13, 1, 51, 0, tzinfo=UTC)
        p_ep1 = RawPost("kudy_letyt", 304, "Хмельниччина:\nРеактивний БпЛА курсом на Дунаївці", t_ep1)
        self.tracker.process_message(p_ep1, self.normalizer.parse(p_ep1), now=t_ep1)
        self.assertEqual(len(self.tracker.get_active_targets()), 1)

        t_smotrych = datetime(2026, 10, 13, 1, 55, 10, tzinfo=UTC)
        smotrych_digest = (
            "Львівщина:\n"
            "Реактивний БпЛА курсом на Поморяни\n\n"
            "Хмельниччина:\n"
            "Реактивний БпЛА курсом на Смотрич\n\n"
            "Тернопільщина:\n"
            "Реактивний БпЛА курсом на Микулинці\n\n"
            "Київщина:\n"
            "Реактивний БпЛА маневрує над Києвом"
        )
        p_smotrych = RawPost("kudy_letyt", 305, smotrych_digest, t_smotrych)
        parsed_sm = self.normalizer.parse(p_smotrych)
        self.assertEqual(parsed_sm.action, ActionType.DIVERTED)
        alerts_sm = self.tracker.process_message(p_smotrych, parsed_sm, now=t_smotrych)
        self.assertEqual(len(alerts_sm), 1)
        self.assertEqual(alerts_sm[0].title, "Ціль змінила курс")
        self.assertEqual(len(self.tracker.get_active_targets()), 0)

    def test_level_3b_target_isolation_and_divert_matching(self):
        """Case: Target 1 reaches Tysmenytsia (Level 3B).
        Target 2 moves Vendychany -> Nova Ushytsia -> Dunaivtsi.
        A divert post at Dunaivtsi must divert Target 2 (Dunaivtsi), NOT Target 1 (Tysmenytsia).
        """
        # Target 1: Nova Ushytsia -> Tysmenytsia
        t1_start = datetime(2026, 10, 14, 14, 40, 0, tzinfo=UTC)
        p1 = RawPost("kudy_letyt", 401, "Реактивний БпЛА на Нову Ушицю", t1_start)
        self.tracker.process_message(p1, self.normalizer.parse(p1), now=t1_start)

        t1_tys = datetime(2026, 10, 14, 15, 7, 22, tzinfo=UTC)
        p1_tys = RawPost("monitoring_zaxid", 402, "Уже проходить Тлумач, наближається ближче до Тисмениці", t1_tys)
        alerts_tys = self.tracker.process_message(p1_tys, self.normalizer.parse(p1_tys), now=t1_tys)
        self.assertTrue(any(a.level == AlertLevel.LEVEL_3B for a in alerts_tys))

        # Target 2: Vendychany at 15:08:32
        t2_ven = datetime(2026, 10, 14, 15, 8, 32, tzinfo=UTC)
        p2_ven = RawPost("kpszsu", 403, "Реактивний БпЛА повз Вендичани на Вінниччині ➡️ у напрямку Хмельниччини", t2_ven)
        self.tracker.process_message(p2_ven, self.normalizer.parse(p2_ven), now=t2_ven)

        # Target 2 moves to Nova Ushytsia & Dunaivtsi at 15:16:24
        t2_dun = datetime(2026, 10, 14, 15, 16, 24, tzinfo=UTC)
        p2_dun = RawPost("kudy_letyt", 404, "Хмельниччина:\nРеактивний БпЛА повз Нову Ушицю в бік Дунаївців", t2_dun)
        self.tracker.process_message(p2_dun, self.normalizer.parse(p2_dun), now=t2_dun)

        # Divert post at Dunaivtsi towards Horodok at 15:24:41
        t_div = datetime(2026, 10, 14, 15, 24, 41, tzinfo=UTC)
        p_div = RawPost("kudy_letyt", 405, "Хмельниччина:\nРеактивний БпЛА повз Дунаївці курсом на Городок", t_div)
        alerts_div = self.tracker.process_message(p_div, self.normalizer.parse(p_div), now=t_div)

        self.assertEqual(len(alerts_div), 1)
        self.assertEqual(alerts_div[0].title, "Ціль змінила курс")
        self.assertIn("Дунаївці", alerts_div[0].body)
        self.assertIn("Городок", alerts_div[0].body)
        self.assertNotIn("Тисмениця", alerts_div[0].body)

    def test_conventional_shahed_eta_and_title(self):
        # Conventional Shahed reported at Nova Ushytsia (187 km)
        p1 = RawPost("chan_a", 501, "Шахед на Нову Ушицю", T0)
        parsed1 = self.normalizer.parse(p1)
        self.assertEqual(parsed1.drone_type, DroneType.CONVENTIONAL)

        alerts = self.tracker.process_message(p1, parsed1, now=T0)
        self.assertEqual(len(alerts), 1)
        # Verify title formatted as (Шахед)
        self.assertEqual(alerts[0].title, "🟡 Потенційна загроза (Шахед): Нова Ушиця")
        # Verify ETA is around 59–76 min (NOT 26–33 min)
        target = self.tracker.get_active_targets()[0]
        self.assertEqual(target.drone_type, DroneType.CONVENTIONAL)
        self.assertGreaterEqual(target.eta_min_minutes, 55.0)
        self.assertLessEqual(target.eta_max_minutes, 80.0)

    def test_jet_and_conventional_concurrent_isolation(self):
        # 1. Jet drone at Nova Ushytsia
        p_jet = RawPost("chan_a", 601, "Реактивна герань на Нову Ушицю", T0)
        self.tracker.process_message(p_jet, self.normalizer.parse(p_jet), now=T0)

        # 2. Conventional Shahed also at Nova Ushytsia (different speed, different wave)
        p_conv = RawPost("chan_b", 602, "Шахед на Нову Ушицю", T0 + timedelta(minutes=1))
        self.tracker.process_message(p_conv, self.normalizer.parse(p_conv), now=T0 + timedelta(minutes=1))

        # Must track as TWO separate targets!
        active = self.tracker.get_active_targets()
        self.assertEqual(len(active), 2)
        types = {t.drone_type for t in active}
        self.assertEqual(types, {DroneType.JET, DroneType.CONVENTIONAL})

    def test_regional_alert_kolomyia_silent(self):
        # Kolomyia (55 km, >30 km -> silent push, informational alert)
        p = RawPost("chan_a", 701, "Реактивна герань на Коломию", T0)
        alerts = self.tracker.process_message(p, self.normalizer.parse(p), now=T0)
        self.assertEqual(len(alerts), 1)
        alert = alerts[0]
        self.assertEqual(alert.level, AlertLevel.REGIONAL)
        self.assertTrue(alert.is_silent)
        self.assertIn("Рух у напрямку Коломиї", alert.title)
        self.assertIn("~55 км від Франківська", alert.title)
        self.assertIn("Найімовірніше, в області буде оголошено тривогу.", alert.body)

    def test_regional_alert_bohorodchany_loud(self):
        # Bohorodchany (18 km, <=30 km -> loud push)
        p = RawPost("chan_a", 702, "Реактивний шахед у районі Богородчан", T0)
        alerts = self.tracker.process_message(p, self.normalizer.parse(p), now=T0)
        self.assertEqual(len(alerts), 1)
        alert = alerts[0]
        self.assertEqual(alert.level, AlertLevel.REGIONAL)
        self.assertFalse(alert.is_silent)
        self.assertIn("Рух у напрямку Богородчан", alert.title)
        self.assertIn("~18 км від Франківська", alert.title)
        self.assertIn("Найімовірніше, в області буде оголошено тривогу.", alert.body)

    def test_regional_alert_with_active_siren(self):
        tracker_siren = TargetTracker(self.cache, is_siren_active_fn=lambda: True)
        p = RawPost("kudy_letyt", 7021, "Реактивний БпЛА на Калуш", T0)
        alerts = tracker_siren.process_message(p, self.normalizer.parse(p), now=T0)
        self.assertEqual(len(alerts), 1)
        alert = alerts[0]
        self.assertEqual(alert.level, AlertLevel.REGIONAL)
        self.assertIn("Рух у напрямку Калуша", alert.title)
        self.assertIn("Загроза в області", alert.title)
        self.assertNotIn("Ймовірна тривога в області", alert.title)
        self.assertIn("В області триває повітряна тривога.", alert.body)
        self.assertIn("Залишайтеся в укриттях до офіційного відбою", alert.body)
        self.assertNotIn("буде оголошено тривогу", alert.body)

    def test_regional_escalation_to_level_3a(self):
        # 1. Target at Kolomyia (regional)
        p1 = RawPost("chan_a", 703, "БпЛА біля Коломиї", T0)
        alerts1 = self.tracker.process_message(p1, self.normalizer.parse(p1), now=T0)
        self.assertEqual(len(alerts1), 1)
        self.assertEqual(alerts1[0].level, AlertLevel.REGIONAL)

        # 2. Target turns towards Tlumach (Level 3A, approaching IF)
        t2 = T0 + timedelta(minutes=5)
        p2 = RawPost("chan_a", 704, "Ціль з Коломиї на Тлумач", t2)
        alerts2 = self.tracker.process_message(p2, self.normalizer.parse(p2), now=t2)
        real_alerts2 = [a for a in alerts2 if not a.is_update]
        self.assertEqual(len(real_alerts2), 1)
        self.assertEqual(real_alerts2[0].level, AlertLevel.LEVEL_3A)
        self.assertFalse(real_alerts2[0].is_silent)
        self.assertIn("Тлумач", real_alerts2[0].title)

    def test_regional_isolation_from_distant_corridor(self):
        # 1. Target at Vendychany (180 km)
        p1 = RawPost("chan_a", 705, "Реактивна герань повз Вендичани", T0)
        self.tracker.process_message(p1, self.normalizer.parse(p1), now=T0)

        # 2. Simultaneous report from Kolomyia (55 km, jump of 125 km)
        t2 = T0 + timedelta(minutes=2)
        p2 = RawPost("chan_b", 706, "Реактивний дрон на Коломию", t2)
        self.tracker.process_message(p2, self.normalizer.parse(p2), now=t2)

        # Must not merge: must be 2 distinct active targets
        active = self.tracker.get_active_targets()
        self.assertEqual(len(active), 2)

    def test_kamianets_podilskyi_raion_to_city_escalation(self):
        # 1. Post from monitorwarr: target moving from Vinnytsia towards Kamianets-Podilskyi raion
        # Must produce Level 1 (silent alert at Nova Ushytsia), NOT premature Level 2 Kamianets city
        p1 = RawPost(
            "monitorwarr",
            42625,
            "⚠️ 1 реактив на Вінниччині у бік Хмельниччини, Камʼянець-Подільський р-н.",
            T0,
        )
        alerts1 = self.tracker.process_message(p1, self.normalizer.parse(p1), now=T0)
        self.assertEqual(len(alerts1), 1)
        self.assertEqual(alerts1[0].level, AlertLevel.LEVEL_1)
        self.assertTrue(alerts1[0].is_silent)
        self.assertIn("Нова Ушиця", alerts1[0].title)

        # 2. Corroborating post from monitoring_zaxid: "дрон ще в районі Н. Ушиці"
        t2 = T0 + timedelta(minutes=2)
        p2 = RawPost("monitoring_zaxid", 28069, "дрон ще в районі Н. Ушиці", t2)
        self.tracker.process_message(p2, self.normalizer.parse(p2), now=t2)
        active = self.tracker.get_active_targets()
        self.assertEqual(len(active), 1)
        self.assertEqual(active[0].level, AlertLevel.LEVEL_1)

        # 3. Later target reaches Kamianets-Podilskyi city directly
        t3 = T0 + timedelta(minutes=8)
        p3 = RawPost("monitorwarr", 42635, "⚠️ 1 реактив на Камʼянець-Подільський.", t3)
        alerts3 = self.tracker.process_message(p3, self.normalizer.parse(p3), now=t3)
        real_alerts3 = [a for a in alerts3 if not a.is_update]
        self.assertEqual(len(real_alerts3), 1)
        self.assertEqual(real_alerts3[0].level, AlertLevel.LEVEL_2)
        self.assertFalse(real_alerts3[0].is_silent)
        self.assertIn("Камʼянець-Подільський", real_alerts3[0].title)

    def test_stale_delayed_post_does_not_trigger_broadcast(self):
        # A post published 16 minutes ago (e.g. from backlog catch-up upon reconnect or old edit)
        # must be processed for tracking/store, but NOT broadcast as a fresh alert (is_update=True)
        t_posted = T0
        t_received = T0 + timedelta(minutes=16)
        post = RawPost("monitoring_zaxid", 28069, "дрон ще в районі Н. Ушиці", t_posted)
        parsed = self.normalizer.parse(post)
        alerts = self.tracker.process_message(post, parsed, now=t_received)
        # Must be treated as update/deduped, not a fresh broadcast alert
        broadcast_alerts = [a for a in alerts if not a.is_update]
        self.assertEqual(len(broadcast_alerts), 0)

    def test_simultaneous_two_waves_and_otynia_no_downgrade(self):
        """Replicate tonight's 00:03-00:30 UTC two-wave attack:
        - Wave 1 advances Nova Ushytsia -> Borshchiv -> Chernelytsia (Level 3A).
        - In the same 00:03:59 digest, Wave 2 enters at Stara Ushytsia -> Kamianets-Podilskyi (Level 2).
        - At 00:05:16, Otyniia (regional, 28 km) is reported: must NOT downgrade Wave 1 to REGIONAL or emit a regional alert.
        - At 00:07:55, Tlumach (25 km) must match Wave 1 (Chernelytsia), NOT Wave 2 (Kamianets-Podilskyi).
        - Northern-track posts ("на заході Рівненщини, курс на Волинь", "з півночі Хмельниччини на Рівненщину")
          must NOT divert either wave.
        """
        t_w1_start = datetime(2026, 10, 15, 23, 55, 35, tzinfo=UTC)
        p_w1_bor = RawPost("kudy_letyt", 123902, "**Тернопільщина:**\nРеактивний БпЛА курсом на Борщів", t_w1_bor := t_w1_start)
        self.tracker.process_message(p_w1_bor, self.normalizer.parse(p_w1_bor), now=t_w1_bor)
        self.assertEqual(len(self.tracker.get_active_targets()), 1)

        # Digest at 00:03:59 has both Wave 2 (Stara Ushytsia -> Kamianets) and Wave 1 (Chernelytsia)
        t_digest = datetime(2026, 10, 16, 0, 3, 59, tzinfo=UTC)
        digest_text = (
            "**Тернопільщина:**\n"
            "Реактивний БпЛА курсом на Шумськ\n\n"
            "**Хмельниччина:**\n"
            "Реактивний БпЛА залітає в область повз Стару Ушицю в бік Камʼянець-Подільського\n\n"
            "**Франківщина:**\n"
            "Реактивний БпЛА курсом на Івано-Франківськ повз Чернелицю"
        )
        p_digest = RawPost("kudy_letyt", 123904, digest_text, t_digest)
        alerts_d = [a for a in self.tracker.process_message(p_digest, self.normalizer.parse(p_digest), now=t_digest) if not a.is_update]
        self.assertEqual(len(alerts_d), 2)
        self.assertEqual(len(self.tracker.get_active_targets()), 2)

        # At 00:05:16, Otyniia is reported -> matches Wave 1 (Level 3A) without downgrading to REGIONAL
        t_oty = datetime(2026, 10, 16, 0, 5, 16, tzinfo=UTC)
        p_oty = RawPost("kudy_letyt", 123905, "**Франківщина:**\nРеактивний БпЛА курсом на Отинію", t_oty)
        alerts_oty = [a for a in self.tracker.process_message(p_oty, self.normalizer.parse(p_oty), now=t_oty) if not a.is_update]
        self.assertEqual(len(alerts_oty), 0)
        self.assertEqual(len(self.tracker.get_active_targets()), 2)

        # At 00:07:55, Tlumach matches Wave 1 (not Wave 2 at Kamianets)
        t_tlu = datetime(2026, 10, 16, 0, 7, 55, tzinfo=UTC)
        p_tlu = RawPost("kudy_letyt", 123907, "Реактивний БпЛА повз Тлумач рухається в бік Івано-Франківська", t_tlu)
        alerts_tlu = [a for a in self.tracker.process_message(p_tlu, self.normalizer.parse(p_tlu), now=t_tlu) if not a.is_update]
        self.assertEqual(len(alerts_tlu), 1)
        self.assertEqual(alerts_tlu[0].level, AlertLevel.LEVEL_3A)
        self.assertIn("Тлумач", alerts_tlu[0].title)

        # Northern-track post at 00:12:54 must NOT divert either wave
        t_north = datetime(2026, 10, 16, 0, 12, 54, tzinfo=UTC)
        p_north = RawPost("kpszsu", 87166, "🏍 Реактивний БпЛА на заході Рівненщини, курс на Волинь.", t_north)
        alerts_n = self.tracker.process_message(p_north, self.normalizer.parse(p_north), now=t_north)
        self.assertEqual(len(alerts_n), 0)
        self.assertEqual(len(self.tracker.get_active_targets()), 2)

    def test_stage_aware_divert_selects_matching_oblast_destination(self):
        """When a target is already at Skala-Podilska (110 km, Ternopil oblast),
        a digest containing both 'на Вовковинці' (Khmelnytskyi) and 'на Копичинці' (Ternopil)
        must divert the target to Kopychyntsi, while an upstream Shargorod/Derazhnia post must NOT divert it.
        """
        t1 = datetime(2026, 10, 15, 20, 35, 32, tzinfo=UTC)
        p1 = RawPost("monitoring_zaxid", 28197, "Реактивний БпЛА рухається далі на Скалу-Подільську, Тернопільщина", t1)
        self.tracker.process_message(p1, self.normalizer.parse(p1), now=t1)
        self.assertEqual(len(self.tracker.get_active_targets()), 1)

        # Upstream Khmelnytskyi diversion to Derazhnia must NOT divert a target already in Ternopil oblast
        t_der = t1 + timedelta(minutes=1)
        p_der = RawPost("monitoring_zaxid", 28198, "Хмельниччина:\n1 реактивна герань на Деражню", t_der)
        alerts_der = self.tracker.process_message(p_der, self.normalizer.parse(p_der), now=t_der)
        self.assertEqual(len(alerts_der), 0)
        self.assertEqual(len(self.tracker.get_active_targets()), 1)

        # Multi-region digest with Vovkovyntsi + Kopychyntsi diverts Skala-Podilska to Kopychyntsi
        t_kop = t1 + timedelta(minutes=2)
        digest = (
            "**Хмельниччина:**\n"
            "Реактивний БпЛА курсом на Вовковинці\n\n"
            "**Тернопільщина:**\n"
            "Реактивний БпЛА курсом на Копичинці"
        )
        p_kop = RawPost("kudy_letyt", 123780, digest, t_kop)
        alerts_kop = self.tracker.process_message(p_kop, self.normalizer.parse(p_kop), now=t_kop)
        self.assertEqual(len(alerts_kop), 1)
        self.assertIn("Копичинці", alerts_kop[0].body)
        self.assertEqual(len(self.tracker.get_active_targets()), 0)

    def test_single_line_multi_point_coalesces_to_single_alert(self):
        # "1 реактивна герань поблизу Мурованих-Курилівців в бік Дунаївців" -> 1 coalesced alert at Dunaivtsi
        p = RawPost("monitoring_zaxid", 28144, "1 реактивна герань поблизу Мурованих-Курилівців в бік Дунаївців", T0)
        alerts = [a for a in self.tracker.process_message(p, self.normalizer.parse(p), now=T0) if not a.is_update]
        self.assertEqual(len(alerts), 1)
        self.assertIn("Дунаївці", alerts[0].title)
        active = self.tracker.get_active_targets()
        self.assertEqual(len(active), 1)
        self.assertEqual(active[0].points_sequence, ["murovani_kyrylivtsi", "dunaivtsi"])

    def test_reply_to_unrelated_post_and_unknown_divert_destination(self):
        """Replicate 16 Oct 16:53 incident:
        - Target active at Nova Ushytsia (kudy_letyt/124270)
        - kudy_letyt/124283 reports drone in Volyn (Kovel -> Liuboml -> Yahodyn)
        - kudy_letyt/124284 replies to 124283 with 'Відвернув на Головне'
        Must NOT divert the Nova Ushytsia target (both with and without reply_to_message_id).
        """
        t0 = datetime(2026, 10, 16, 13, 45, 0, tzinfo=UTC)
        p_target = RawPost("kudy_letyt", 124270, "Реактивний БпЛА на Нову Ушицю", t0)
        self.tracker.process_message(p_target, self.normalizer.parse(p_target), now=t0)
        self.assertEqual(len(self.tracker.get_active_targets()), 1)

        # Unrelated Volyn post
        t1 = t0 + timedelta(minutes=6)
        p_volyn = RawPost(
            "kudy_letyt",
            124283,
            'Реактивний БпЛА пролетів Ковель повз Любомль курсом на Пункт пропуску для автомобільного сполучення "Ягодин"',
            t1,
        )
        self.assertEqual(self.tracker.process_message(p_volyn, self.normalizer.parse(p_volyn), now=t1), [])

        # 1. Reply to 124283: "Відвернув на Головне"
        t2 = t0 + timedelta(minutes=8)
        p_reply = RawPost("kudy_letyt", 124284, "Відвернув на Головне", t2, reply_to_message_id=124283)
        alerts_reply = self.tracker.process_message(p_reply, self.normalizer.parse(p_reply), now=t2)
        self.assertEqual(alerts_reply, [])
        self.assertEqual(len(self.tracker.get_active_targets()), 1)

        # 2. Even without reply_to_message_id, an unknown destination outside corridor ("Відвернув на Олевськ")
        # must NOT divert the Nova Ushytsia target
        p_unknown = RawPost("kudy_letyt", 124285, "Відвернув на Олевськ", t2 + timedelta(seconds=30))
        alerts_unknown = self.tracker.process_message(
            p_unknown, self.normalizer.parse(p_unknown), now=t2 + timedelta(seconds=30)
        )
        self.assertEqual(alerts_unknown, [])
        self.assertEqual(len(self.tracker.get_active_targets()), 1)

        # 3. Generic reply "Відвернув на північ" to unrelated 124283 must NOT divert 124270
        p_generic_unrel = RawPost(
            "kudy_letyt", 124286, "Відвернув на північ", t2 + timedelta(minutes=1), reply_to_message_id=124283
        )
        alerts_gen_unrel = self.tracker.process_message(
            p_generic_unrel, self.normalizer.parse(p_generic_unrel), now=t2 + timedelta(minutes=1)
        )
        self.assertEqual(alerts_gen_unrel, [])
        self.assertEqual(len(self.tracker.get_active_targets()), 1)

        # 4. Reply to the actual target message (124270) with a valid corridor divert DOES divert it
        p_valid_reply = RawPost(
            "kudy_letyt", 124287, "Відвернув на північ", t2 + timedelta(minutes=2), reply_to_message_id=124270
        )
        alerts_valid = self.tracker.process_message(
            p_valid_reply, self.normalizer.parse(p_valid_reply), now=t2 + timedelta(minutes=2)
        )
        self.assertEqual(len(alerts_valid), 1)
        self.assertEqual(alerts_valid[0].title, "Ціль змінила курс")
        self.assertEqual(len(self.tracker.get_active_targets()), 0)

    def test_cruise_missile_tracking_eta_and_regional_alerts(self):
        """Cruise missiles (Х-101 / Калібр) are tracked separately from drones,
        use 750–880 km/h speed for ETA, and format regional alerts appropriately.
        """
        t0 = datetime(2026, 10, 16, 4, 30, 0, tzinfo=UTC)

        # 1. Active Shahed at Nova Ushytsia
        p_shahed = RawPost("kudy_letyt", 5001, "БпЛА курсом на Нову Ушицю", t0)
        self.tracker.process_message(p_shahed, self.normalizer.parse(p_shahed), now=t0)

        # 2. Cruise missile Х-101 enters at Nova Ushytsia (must NOT merge with Shahed)
        t1 = t0 + timedelta(minutes=1)
        p_kh101 = RawPost("kpszsu", 5002, "🚀 Група КР Х-101 через Нову Ушицю", t1)
        alerts_kh = self.tracker.process_message(p_kh101, self.normalizer.parse(p_kh101), now=t1)
        self.assertEqual(len(self.tracker.get_active_targets()), 2)
        self.assertEqual(len(alerts_kh), 1)
        self.assertIn("(КР Х-101)", alerts_kh[0].title)

        # Verify missile ETA from Nova Ushytsia (~190 km at 750-880 km/h -> ~14-21 min, much faster than jet/shahed)
        missile_targets = [t for t in self.tracker.get_active_targets() if t.drone_type == DroneType.MISSILE]
        self.assertEqual(len(missile_targets), 1)
        self.assertLessEqual(missile_targets[0].eta_min_minutes, 16.0)
        self.assertLessEqual(missile_targets[0].eta_max_minutes, 22.0)

        # 3. Fast jump (~95 km in 5 min) to Borshchiv -> Chernelytsia links to the same missile target
        t2 = t1 + timedelta(minutes=5)
        p_cher = RawPost("monitoring_zaxid", 5003, "КР Х-101 повз Борщів у напрямку Чернелиці", t2)
        alerts_cher = self.tracker.process_message(p_cher, self.normalizer.parse(p_cher), now=t2)
        self.assertEqual(len(self.tracker.get_active_targets()), 2)
        self.assertEqual(len(alerts_cher), 1)
        self.assertEqual(alerts_cher[0].level, AlertLevel.LEVEL_3A)
        self.assertIn("(КР Х-101)", alerts_cher[0].title)

        # 4. Regional cruise missile alert (e.g. Kalibr on Burshtyn) is non-silent and uses missile phrasing
        t3 = t2 + timedelta(minutes=1)
        p_kalibr = RawPost("monitoring_zaxid", 5004, "2х Калібри курсом на Бурштин!", t3)
        alerts_bur = self.tracker.process_message(p_kalibr, self.normalizer.parse(p_kalibr), now=t3)
        self.assertEqual(len(alerts_bur), 1)
        self.assertEqual(alerts_bur[0].level, AlertLevel.REGIONAL)
        self.assertFalse(alerts_bur[0].is_silent)
        self.assertIn("🚀 Зафіксовано рух крилатої ракети (КР Калібр)", alerts_bur[0].body)

    def test_missile_corridor_level1_to_if_escalation(self):
        """Cruise missiles heading toward Khmelnytskyi, Ternopil, or Lviv must create Level 1 targets
        and smoothly escalate when turning toward Ivano-Frankivsk or Burshtyn.
        """
        t0 = datetime(2026, 10, 16, 15, 0, 0, tzinfo=UTC)
        p1 = RawPost("kpszsu", 6001, "Група КР Х-101 курсом на Хмельницький", t0)
        a1 = [a for a in self.tracker.process_message(p1, self.normalizer.parse(p1), now=t0) if not a.is_update]
        self.assertEqual(len(a1), 1)
        self.assertEqual(a1[0].level, AlertLevel.LEVEL_1)
        self.assertIn("Хмельницький (ракетний коридор)", a1[0].title)

        t1 = t0 + timedelta(minutes=4)
        p2 = RawPost("monitoring_zaxid", 6002, "КР Х-101 продовжують рух на Тернопіль", t1)
        a2 = [a for a in self.tracker.process_message(p2, self.normalizer.parse(p2), now=t1) if not a.is_update]
        self.assertEqual(len(a2), 1)
        self.assertEqual(a2[0].level, AlertLevel.LEVEL_1)
        self.assertIn("Тернопіль (ракетний коридор)", a2[0].title)
        self.assertEqual(len(self.tracker.get_active_targets()), 1)

        t2 = t1 + timedelta(minutes=3)
        p3 = RawPost("monitoring_zaxid", 6003, "КР Х-101 змінили курс на Івано-Франківськ!", t2)
        a3 = [a for a in self.tracker.process_message(p3, self.normalizer.parse(p3), now=t2) if not a.is_update]
        self.assertEqual(len(a3), 1)
        self.assertEqual(a3[0].level, AlertLevel.LEVEL_3A)
        self.assertFalse(a3[0].is_silent)
        self.assertIn("Курс на Івано-Франківськ", a3[0].title)
        self.assertEqual(len(self.tracker.get_active_targets()), 1)

    def test_missed_if_sequences_end_to_end(self):
        """End-to-end verification of the missed sequences from 2026-10-16:
        - 28309 (Martynivka) -> 28310 (IF raion danger) -> 28312 (Підвертає на Франківськ) -> 124063 (Monastyryska) -> 124068 (IF airport)
        - 28452 (Kamianets with parenthetical preventive) -> 28456 (south Ternopil towards IF) -> 124290 (Rivne + IF) -> 28458 (Tlumach)
        """
        t0 = datetime(2026, 10, 16, 4, 33, 50, tzinfo=UTC)
        seq1 = [
            (t0, "monitoring_zaxid", 28309, "Тернопільська область:\n1 реактивна герань поблизу Чорткова, курс на Мартинівку"),
            (t0 + timedelta(seconds=37), "monitoring_zaxid", 28310, "Івано-Франківський район - дронова небезпека по реактиву на Тернопільщині"),
            (t0 + timedelta(seconds=74), "monitoring_zaxid", 28312, "Підвертає на Франківськ \n\n❗️"),
            (t0 + timedelta(seconds=144), "kudy_letyt", 124063, "Реактивний БпЛА курсом на Монастириська"),
            (t0 + timedelta(seconds=288), "kudy_letyt", 124064, "Реактивний БпЛА курсом на Єзупіль"),
            (t0 + timedelta(seconds=471), "kudy_letyt", 124068, "Івано-Франківськ: реактивний БпЛА курсом на аеропорт"),
        ]
        emitted1 = []
        for ts, ch, mid, txt in seq1:
            post = RawPost(ch, mid, txt, ts)
            evts = [a for a in self.tracker.process_message(post, self.normalizer.parse(post), now=ts) if not a.is_update]
            emitted1.extend(evts)

        self.assertEqual(len(self.tracker.get_active_targets()), 1)
        levels1 = [a.level for a in emitted1]
        self.assertIn(AlertLevel.LEVEL_2, levels1)
        self.assertIn(AlertLevel.LEVEL_3A, levels1)
        self.assertIn(AlertLevel.LEVEL_3B, levels1)

    def test_suburban_level_3b_settlements(self):
        """All 9 suburbs around Ivano-Frankivsk (Вовчинці, Підпечери, Клузів, Ямниця, Угринів,
        Микитинці, Драгомирчани, Крихівці, Черніїв) and their inflections must trigger LEVEL_3B.
        """
        cases = [
            ("Реактивний БпЛА курсом на Вовчинці", "vovchyntsi", "Вовчинці"),
            ("Реактивний БпЛА над Вовчинцем", "vovchyntsi", "Вовчинці"),
            ("Реактивний БпЛА в районі Підпечер", "pidpechery", "Підпечери"),
            ("Реактивний БпЛА повз Клузів", "kluziv", "Клузів"),
            ("Реактивний БпЛА курсом на Ямницю", "yamnytsia", "Ямниця"),
            ("Реактивний БпЛА біля Угринова", "uhryniv", "Угринів"),
            ("Реактивний БпЛА у напрямку Микитинців", "mykytyntsi", "Микитинці"),
            ("Реактивний БпЛА над Драгомирчанами", "drahomyrchany", "Драгомирчани"),
            ("Реактивний БпЛА в районі Крихівців", "krykhivtsi", "Крихівці"),
            ("Реактивний БпЛА курсом на Черніїв", "cherniiv", "Черніїв"),
        ]
        for idx, (text, expected_pid, expected_title_name) in enumerate(cases):
            with self.subTest(text=text):
                tracker = TargetTracker(self.cache)
                ts = datetime(2026, 10, 16, 12, 0, 0, tzinfo=UTC) + timedelta(minutes=idx * 5)
                post = RawPost("kudy_letyt", 95000 + idx, text, ts)
                parsed = self.normalizer.parse(post)
                self.assertEqual(parsed.points, [expected_pid])
                alerts = tracker.process_message(post, parsed, now=ts)
                self.assertEqual(len(alerts), 1)
                self.assertEqual(alerts[0].level, AlertLevel.LEVEL_3B)
                self.assertIn("Небезпека біля / над містом", alerts[0].title)
                self.assertIn(expected_title_name, alerts[0].title)

    def test_oct18_geran5_sequence_no_false_missile_alert(self):
        """End-to-end replay of the 2026-10-18 19:26-19:46 UTC episode:
        Ensure 'ракета-дрон «Герань-5»' and subsequent Khmelnytskyi/Chornyi Ostriv posts
        never create a DroneType.MISSILE target or emit a cruise missile alert.
        """
        t0 = datetime(2026, 10, 18, 19, 26, 20, tzinfo=UTC)
        posts = [
            (t0, "kudy_letyt", 124865, "Хмельниччина:\nРеактивний БпЛА курсом на Дунаївці"),
            (
                t0 + timedelta(seconds=13),
                "monitoring_zaxid",
                28673,
                "За уточненою інформацією та порівнянням ТТХ, вдалось визначити, що на Вінниччині в напрямку Козятина рухається не «Бандероль», а ракета-дрон «Герань-5»",
            ),
            (
                t0 + timedelta(minutes=4, seconds=6),
                "monitoring_zaxid",
                28676,
                "Хмельниччина:\n1 реактивна герань поблизу Нової Ушиці, західний курс",
            ),
            (
                t0 + timedelta(minutes=7, seconds=17),
                "monitoring_zaxid",
                28679,
                "Йде повз Дунаївці у бік Камʼянця-Подільського",
            ),
            (
                t0 + timedelta(minutes=9, seconds=4),
                "monitoring_zaxid",
                28680,
                "Наразі рухається на північ області в напрямку Ярмолинців/Хмельницького",
            ),
            (
                t0 + timedelta(minutes=19, seconds=25),
                "monitoring_zaxid",
                28681,
                "Хмельницький: Чорний Острів повз західні околиці ❗️",
            ),
        ]
        all_alerts = []
        for ts, ch, mid, txt in posts:
            post = RawPost(ch, mid, txt, ts)
            parsed = self.normalizer.parse(post)
            self.assertNotEqual(parsed.drone_type, DroneType.MISSILE, txt)
            all_alerts.extend(self.tracker.process_message(post, parsed, now=ts))

        for alert in all_alerts:
            self.assertNotIn("крилата ракета", alert.title)
            self.assertNotIn("ракетний коридор", alert.title)
        self.assertEqual(len(self.tracker.get_active_targets()), 0)

    def test_oct19_lviv_jet_course_to_if_diverts_back_to_lviv(self):
        """End-to-end replay of 2026-10-19 05:04-05:07 UTC episode:
        A jet drone in Lviv region briefly takes a course toward Ivano-Frankivsk oblast (if_direct_course, Level 3A)
        and then turns back to Lviv ('Реактивний БпЛА повертається на Львів').
        Must transition the active target to DIVERTED.
        """
        t0 = datetime(2026, 10, 19, 5, 4, 40, tzinfo=UTC)
        p1 = RawPost("monitoring_zaxid", 28766, "Львівщина:\n1 реактивна герань на Миколаїв/Новий Розділ віддаляється", t0)
        self.tracker.process_message(p1, self.normalizer.parse(p1), now=t0)

        t1 = t0 + timedelta(seconds=38)
        p2 = RawPost("monitoring_zaxid", 28767, "Взяв курс на Франківщину взагалі", t1)
        alerts2 = self.tracker.process_message(p2, self.normalizer.parse(p2), now=t1)
        self.assertEqual(len(alerts2), 1)
        self.assertEqual(alerts2[0].level, AlertLevel.LEVEL_3A)
        self.assertEqual(len(self.tracker.get_active_targets()), 1)

        t2 = t0 + timedelta(minutes=2, seconds=24)
        p3 = RawPost("kudy_letyt", 125084, "Львівщина:\nРеактивний БпЛА повертається на Львів", t2)
        parsed3 = self.normalizer.parse(p3)
        self.assertEqual(parsed3.action, ActionType.DIVERTED)
        alerts3 = self.tracker.process_message(p3, parsed3, now=t2)
        self.assertEqual(len(alerts3), 1)
        self.assertEqual(alerts3[0].title, "Ціль змінила курс")
        self.assertIn("Львів", alerts3[0].body)
        self.assertEqual(len(self.tracker.get_active_targets()), 0)

    def test_dunaivtsi_and_kelmentsi_do_not_cross_merge(self):
        """Ensure a drone at Dunaivtsi (central Khmelnytskyi) and a drone at Vendychany crossing into
        Novodnistrovsk/Kelmentsi (Bukovyna) stay on separate tracks.
        """
        t0 = datetime(2026, 10, 18, 4, 0, 52, tzinfo=UTC)
        digest_124719 = (
            "Вінниччина:\n"
            "БпЛА курсом на Вендичани\n\n"
            "Хмельниччина:\n"
            "БпЛА курсом на Дунаївці"
        )
        p_124719 = RawPost("kudy_letyt", 124719, digest_124719, t0)
        self.tracker.process_message(p_124719, self.normalizer.parse(p_124719), now=t0)
        self.assertEqual(len(self.tracker.get_active_targets()), 2)

        t2 = t0 + timedelta(minutes=15)
        p_buk = RawPost("kudy_letyt", 124726, "Буковина:\nБпЛА курсом на Новодністровськ", t2)
        self.tracker.process_message(p_buk, self.normalizer.parse(p_buk), now=t2)

        active = {t.last_point_id: t for t in self.tracker.get_active_targets()}
        self.assertIn("dunaivtsi", active)
        self.assertIn("kelmentsi", active)
        self.assertIn("vendychany", active["kelmentsi"].points_sequence)

    def test_khmelnytskyi_airport_does_not_hijack_buchach_target(self):
        """Regression test for 2026-10-20T00:20Z:
        A target tracked at Buchach (125541, 29041) must NOT be escalated to Level 3B Ivano-Frankivsk
        when Khmelnytskyi airport posts arrive (kudy_letyt/125547, monitoring_zaxid/29047).
        Also verifies Rohatyn -> Khodoriv/Lvivshchyna diversion (kudy_letyt/125607, kpszsu/88620).
        """
        t0 = datetime(2026, 10, 20, 0, 11, 25, tzinfo=UTC)
        p_buch = RawPost("kudy_letyt", 125541, "Тернопільщина:\nРеактивний БпЛА курсом на Бучач", t0)
        alerts_buch = self.tracker.process_message(p_buch, self.normalizer.parse(p_buch), now=t0)
        self.assertEqual(len(alerts_buch), 1)
        self.assertEqual(alerts_buch[0].level, AlertLevel.LEVEL_2)

        t1 = t0 + timedelta(minutes=8, seconds=42)
        p_khm1 = RawPost("kudy_letyt", 125547, "Хмельницький:\nРеактивний БпЛА курсом на аеропорт", t1)
        alerts_khm1 = self.tracker.process_message(p_khm1, self.normalizer.parse(p_khm1), now=t1)
        self.assertEqual(alerts_khm1, [])

        t2 = t1 + timedelta(seconds=31)
        p_khm2 = RawPost("monitoring_zaxid", 29047, "‼️Хмельницький - уважно, реактивна герань у бік аеропорту!", t2)
        alerts_khm2 = self.tracker.process_message(p_khm2, self.normalizer.parse(p_khm2), now=t2)
        self.assertEqual(alerts_khm2, [])

        active = self.tracker.get_active_targets()
        self.assertEqual(len(active), 1)
        self.assertEqual(active[0].last_point_id, "buchach")

        # Rohatyn -> Khodoriv / Lvivshchyna diversion sequence (T-202610200239-ca7b)
        t_roh = datetime(2026, 10, 20, 2, 43, 26, tzinfo=UTC)
        p_roh = RawPost("kudy_letyt", 125606, "Франківщина:\nРеактивний БпЛА курсом на Рогатин з Тернопіщини", t_roh)
        self.tracker.process_message(p_roh, self.normalizer.parse(p_roh), now=t_roh)
        roh_targets = [t for t in self.tracker.get_active_targets() if t.last_point_id == "rohatyn"]
        self.assertEqual(len(roh_targets), 1)

        t_khod = t_roh + timedelta(minutes=3, seconds=39)
        p_khod = RawPost("kudy_letyt", 125607, "Продовжує рух в бік Ходорів (Львівщина)", t_khod)
        parsed_khod = self.normalizer.parse(p_khod)
        self.assertEqual(parsed_khod.action, ActionType.DIVERTED)
        alerts_khod = self.tracker.process_message(p_khod, parsed_khod, now=t_khod)
        self.assertEqual(len(alerts_khod), 1)
        self.assertEqual(alerts_khod[0].title, "Ціль змінила курс")
        self.assertIn("Ходорів", alerts_khod[0].body)


if __name__ == "__main__":
    unittest.main()




