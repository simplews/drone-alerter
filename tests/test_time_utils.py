"""Тести для підтримки київського часового поясу (Europe/Kyiv) та переходу на літній/зимовий час."""
import unittest
from datetime import UTC, datetime

from src.engine.eta import calculate_eta
from src.learning.reference import PointRef, ReferenceData
from src.utils.time import KYIV_TZ, format_kyiv_time, now_kyiv, to_kyiv_time


class TestKyivTimezone(unittest.TestCase):
    def test_summer_daylight_saving_utc_plus_3(self):
        # 10 липня (літній час, EEST = UTC+3)
        summer_utc = datetime(2026, 7, 10, 21, 30, tzinfo=UTC)
        kyiv_time = to_kyiv_time(summer_utc)
        self.assertEqual(kyiv_time.tzinfo, KYIV_TZ)
        self.assertEqual(kyiv_time.hour, 0)
        self.assertEqual(kyiv_time.day, 11)
        self.assertEqual(format_kyiv_time(summer_utc), "00:30")

    def test_winter_standard_time_utc_plus_2(self):
        # 10 січня (зимовий час, EET = UTC+2)
        winter_utc = datetime(2026, 1, 10, 21, 30, tzinfo=UTC)
        kyiv_time = to_kyiv_time(winter_utc)
        self.assertEqual(kyiv_time.tzinfo, KYIV_TZ)
        self.assertEqual(kyiv_time.hour, 23)
        self.assertEqual(kyiv_time.day, 10)
        self.assertEqual(format_kyiv_time(winter_utc), "23:30")

    def test_naive_datetime_handled_safely(self):
        naive = datetime(2026, 7, 10, 14, 0)
        kyiv = to_kyiv_time(naive)
        self.assertEqual(kyiv.hour, 17)

    def test_now_kyiv_returns_valid_timezone(self):
        now = now_kyiv()
        self.assertEqual(now.tzinfo, KYIV_TZ)

    def test_eta_window_formats_in_kyiv_time(self):
        # Пост о 21:00 UTC (влітку це 00:00 за Києвом наступного дня)
        post_utc = datetime(2026, 7, 10, 21, 0, tzinfo=UTC)
        ref = ReferenceData(version=1, status="active", speed_kmh=[400, 480], post_lag_min=[0.5, 1.5])
        point = PointRef(id="kam", names=["камʼянець"], level=2, eta_min=20.0, eta_max=25.0)

        _, _, _, _, w_str = calculate_eta(
            point=point,
            ref=ref,
            post_time_utc=post_utc,
            current_time_utc=post_utc,
        )

        # 21:00 + 20 хв = 21:20 UTC -> за Києвом має бути 00:20!
        # 21:00 + 25 хв = 21:25 UTC -> за Києвом має бути 00:25!
        self.assertIn("00:20–00:25", w_str)
        self.assertNotIn("21:20", w_str)


if __name__ == "__main__":
    unittest.main()
