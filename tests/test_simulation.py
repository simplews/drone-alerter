"""Тести для модуля симуляції тіньового режиму (ShadowModeSimulator)."""
import tempfile
import unittest
from datetime import UTC, datetime, timedelta
from pathlib import Path

from src.engine.models import RawPost
from src.engine.simulation import ShadowModeSimulator, SimulationEpisode
from src.learning.reference import (
    ActiveReferenceCache,
    ReferenceRepository,
    seed_from_dictionary,
)

T0 = datetime(2026, 10, 10, 21, 28, tzinfo=UTC)


class TestShadowModeSimulator(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.repo = ReferenceRepository(Path(self.tmp.name) / "ref")
        v1 = self.repo.save_new(seed_from_dictionary())
        self.repo.promote(v1.version)
        self.cache = ActiveReferenceCache(self.repo)
        self.simulator = ShadowModeSimulator(self.cache)

    def tearDown(self):
        self.tmp.cleanup()

    def test_run_simulation_with_lead_time_verification(self):
        # Маршрут 8:
        # 21:28 Стара Ушиця
        # 21:31 Камʼянець (Рівень 2)
        # Офіційна небезпека по району оголошена о 21:38 (випередження = 7 хв!)
        # Перша згадка Івано-Франківщини в ручних моніторингах о 21:38 (випередження = 7 хв!)
        # Розрахункове прибуття до міста: 21:50-22:00
        episodes = [
            SimulationEpisode(
                episode_id="route_8",
                description="Маршрут 8 (10 жовтня)",
                posts=[
                    RawPost("monitoring_zaxid", 1, "Реактивна герань повз Стару Ушицю", T0),
                    RawPost("monitoring_zaxid", 2, "Камʼянець-Подільський", T0 + timedelta(minutes=3)),
                    RawPost("monitoring_zaxid", 3, "Товсте", T0 + timedelta(minutes=15)),
                ],
                actual_arrival_utc=T0 + timedelta(minutes=26),  # 21:54 (всередині вікна 21:50-22:00)
                official_alert_utc=T0 + timedelta(minutes=10),   # 21:38
                manual_channel_mention_utc=T0 + timedelta(minutes=10), # 21:38
            )
        ]

        metrics, _logs = self.simulator.run_simulation(episodes)
        self.assertEqual(metrics.total_episodes, 1)
        self.assertEqual(metrics.level_2_alerts_count, 1)
        self.assertEqual(metrics.eta_coverage_percent, 100.0)
        self.assertEqual(metrics.lead_time_over_official_minutes, [7.0])
        self.assertEqual(metrics.lead_time_over_manual_minutes, [7.0])
        
        # Перевірка виконання критеріїв Go/No-Go
        self.assertTrue(metrics.go_no_go_checklist["ETA coverage >= 80%"])
        self.assertTrue(metrics.go_no_go_checklist["Lead time over official alert >= 7 min"])
        self.assertTrue(metrics.go_no_go_checklist["Lead time over manual channels >= 5 min"])


if __name__ == "__main__":
    unittest.main()
