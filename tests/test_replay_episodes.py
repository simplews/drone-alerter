"""Historical replay tests based on 8 episodes from 8–10 October 2026."""
import tempfile
import unittest
from datetime import UTC, datetime
from pathlib import Path

from src.engine.models import AlertLevel, RawPost
from src.engine.tracker import TargetTracker
from src.learning.reference import (
    ActiveReferenceCache,
    ReferenceRepository,
    seed_from_dictionary,
)
from src.nlp.normalizer import MessageNormalizer


class TestHistoricalReplayEpisodes(unittest.TestCase):
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

    def test_replay_route_8_october_10(self):
        """Route 8 (10 October 2026, 21:28–21:51).

        21:28 Stara Ushytsia -> 21:31 Kamianets-Podilskyi ->
        21:43 Tovste -> 21:48 Chernelytsia -> 21:51 shot down.
        """
        # 1. 21:28 @monitoring_zaxid: Stara Ushytsia (level 1)
        t_2128 = datetime(2026, 10, 10, 21, 28, tzinfo=UTC)
        p1 = RawPost("monitoring_zaxid", 101, "Реактивна герань повз Стару Ушицю", t_2128)
        alerts1 = self.tracker.process_message(p1, self.normalizer.parse(p1), now=t_2128)
        self.assertEqual(len(alerts1), 1)
        self.assertEqual(alerts1[0].level, AlertLevel.LEVEL_1)
        self.assertTrue(alerts1[0].is_silent)

        # 2. 21:31 @monitoring_zaxid: Kamianets-Podilskyi (level 2, 7 minutes ahead of the 21:38 alert!)
        t_2131 = datetime(2026, 10, 10, 21, 31, tzinfo=UTC)
        p2 = RawPost("monitoring_zaxid", 102, "Камʼянець-Подільський", t_2131)
        alerts2 = self.tracker.process_message(p2, self.normalizer.parse(p2), now=t_2131)
        self.assertEqual(len(alerts2), 1)
        self.assertEqual(alerts2[0].level, AlertLevel.LEVEL_2)
        self.assertFalse(alerts2[0].is_silent)

        # 3. 21:35 @monitorwarr: confirmation of the route toward Ivano-Frankivsk region
        t_2135 = datetime(2026, 10, 10, 21, 35, tzinfo=UTC)
        p3 = RawPost("monitorwarr", 50, "Класичний (за останні дні) маршрут на Івано-Франківщину", t_2135)
        self.tracker.process_message(p3, self.normalizer.parse(p3), now=t_2135)

        # 4. 21:43 @monitoring_zaxid: Tovste
        t_2143 = datetime(2026, 10, 10, 21, 43, tzinfo=UTC)
        p4 = RawPost("monitoring_zaxid", 103, "Товсте", t_2143)
        self.tracker.process_message(p4, self.normalizer.parse(p4), now=t_2143)

        # 5. 21:48 @monitoring_zaxid: Chernelytsia (level 3a, close approach)
        t_2148 = datetime(2026, 10, 10, 21, 48, tzinfo=UTC)
        p5 = RawPost("monitoring_zaxid", 104, "Чернелиця, Коломийський район", t_2148)
        alerts5 = self.tracker.process_message(p5, self.normalizer.parse(p5), now=t_2148)
        self.assertEqual(len(alerts5), 1)
        self.assertEqual(alerts5[0].level, AlertLevel.LEVEL_3A)

        # 6. 21:51 @monitoring_zaxid: shot down after Chernelytsia
        t_2151 = datetime(2026, 10, 10, 21, 51, tzinfo=UTC)
        p6 = RawPost("monitoring_zaxid", 105, "Ціль біля Чернелиці збито!", t_2151)
        alerts6 = self.tracker.process_message(p6, self.normalizer.parse(p6), now=t_2151)
        self.assertEqual(len(alerts6), 1)
        self.assertEqual(alerts6[0].level, AlertLevel.INFO)
        self.assertIn("імовірно, уражена або втрачена", alerts6[0].body)
        self.assertEqual(self.tracker.get_active_targets(), [])

    def test_replay_route_4_multi_target_tlumach(self):
        """Route 4 (03:16–03:17): 2 jet targets through Tlumach."""
        t_0316 = datetime(2026, 10, 9, 3, 16, tzinfo=UTC)
        p1 = RawPost("kudy_letyt", 201, "2х Реактивні БпЛА залітають на Івано-Франківськ через Тлумач", t_0316)
        alerts1 = self.tracker.process_message(p1, self.normalizer.parse(p1), now=t_0316)
        self.assertEqual(len(alerts1), 1)
        self.assertEqual(alerts1[0].level, AlertLevel.LEVEL_3A)
        self.assertIn("2х", alerts1[0].title)


        # 03:17 @monitoring_zaxid: confirmation of Tlumach
        t_0317 = datetime(2026, 10, 9, 3, 17, tzinfo=UTC)
        p2 = RawPost("monitoring_zaxid", 202, "Тлумач", t_0317)
        self.tracker.process_message(p2, self.normalizer.parse(p2), now=t_0317)
        # Deduplicated into the same target, an update without spam
        targets = self.tracker.get_active_targets()
        self.assertEqual(len(targets), 1)
        self.assertEqual(targets[0].target_count, 2)


if __name__ == "__main__":
    unittest.main()
