"""Тести механізму навчання. Усі дані тут СИНТЕТИЧНІ (не реальні епізоди).

Запуск: python3 -m unittest discover -s tests -v
"""
import tempfile
import unittest
from datetime import UTC, datetime, timedelta
from pathlib import Path

from src.learning.aliases import discover_aliases
from src.learning.calibration import (
    CalibrationConfig,
    base_rates,
    calibrate_point,
    evaluate,
)
from src.learning.pipeline import build_candidate, diff, retrain
from src.learning.reference import (
    STATUS_ACTIVE,
    STATUS_ARCHIVED,
    STATUS_PENDING,
    STATUS_REJECTED,
    ActiveReferenceCache,
    PointRef,
    ReferenceRepository,
    seed_from_dictionary,
)
from src.learning.store import (
    OUTCOME_DIVERTED,
    OUTCOME_DOWNED,
    OUTCOME_REACHED,
    ArrivalObservation,
    LearningStore,
    OutcomeRow,
)
from src.learning.text import extract_place_candidates, normalize, phrases_match

T0 = datetime(2026, 10, 1, 18, 0, tzinfo=UTC)


class TextTests(unittest.TestCase):
    def test_case_forms_match(self):
        self.assertTrue(phrases_match("Нову Ушицю", "Нова Ушиця"))
        self.assertTrue(phrases_match("Новій Ушиці", "нова ушиця"))
        self.assertTrue(phrases_match("Дунаївцях", "Дунаївці"))
        self.assertTrue(phrases_match("Камʼянці-Подільському", "Кам'янець-Подільський"))
        self.assertFalse(phrases_match("Борщів", "Тлумач"))
        self.assertFalse(phrases_match("Нова Ушиця", "Ушиця"))  # різна кількість слів

    def test_normalize_apostrophes_and_abbrev(self):
        self.assertEqual(normalize("Кам'янець"), normalize("Камʼянець"))
        self.assertEqual(normalize("Н.Ушиця"), "н. ушиця")

    def test_extract_candidates(self):
        text = "Реактивна герань через Скалат курсом на Тернопільщину\n🛵 Борщів\nУвага всім!"
        found = extract_place_candidates(text)
        self.assertIn("Скалат", found)
        self.assertIn("Тернопільщину", found)
        self.assertIn("Борщів", found)
        self.assertNotIn("Увага всім", found)


class RepositoryTests(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.repo = ReferenceRepository(Path(self.tmp.name) / "ref")

    def tearDown(self):
        self.tmp.cleanup()

    def test_seed_promote_rollback(self):
        v1 = self.repo.save_new(seed_from_dictionary())
        self.repo.promote(v1.version)
        self.assertEqual(self.repo.active().points["borshchiv"].eta_min, 12.0)

        cand = self.repo.active().clone_as_candidate()
        cand.points["borshchiv"].eta_min = 10.0
        v2 = self.repo.save_new(cand)
        self.assertEqual(self.repo.load(v2.version).status, STATUS_PENDING)

        cache = ActiveReferenceCache(self.repo)
        self.assertEqual(cache.get().version, 1)
        self.repo.promote(v2.version)
        self.assertEqual(cache.get().version, 2)  # підхоплено без перезапуску
        self.assertEqual(self.repo.load(1).status, STATUS_ARCHIVED)

        self.assertEqual(self.repo.rollback(), 1)
        self.assertEqual(self.repo.active().status, STATUS_ACTIVE)
        self.assertEqual(self.repo.load(2).status, STATUS_REJECTED)

    def test_cannot_reject_active(self):
        v1 = self.repo.save_new(seed_from_dictionary())
        self.repo.promote(v1.version)
        with self.assertRaises(ValueError):
            self.repo.reject(v1.version)


class CalibrationTests(unittest.TestCase):
    cfg = CalibrationConfig(min_obs_empirical=8, margin_min=2, max_narrowing_per_update_min=3)

    def test_small_n_only_widens(self):
        p = PointRef("x", ["x"], 2, eta_min=14, eta_max=21, eta_method="seed")
        lo, hi, _ = calibrate_point(p, [16, 17], [340, 450], [1, 3], self.cfg)
        self.assertEqual((lo, hi), (14, 21))  # нові дані всередині — не звужуємо
        lo, hi, _ = calibrate_point(p, [11, 25], [340, 450], [1, 3], self.cfg)
        self.assertEqual((lo, hi), (9, 27))  # нові дані ширші — розширюємо одразу

    def test_large_n_narrows_gradually(self):
        p = PointRef("x", ["x"], 2, eta_min=10, eta_max=30, eta_method="seed")
        durations = [17, 18, 18, 19, 19, 20, 20, 21, 21, 22]
        lo, hi, method = calibrate_point(p, durations, [340, 450], [1, 3], self.cfg)
        self.assertEqual(method, "empirical_q")
        self.assertEqual((lo, hi), (13, 27))  # не більше ніж на 3 хв за раз

    def test_physics_prior_for_new_point(self):
        p = PointRef("chernelytsia", ["чернелиця"], 3, distance_km=54)
        lo, hi, method = calibrate_point(p, [], [340, 450], [1, 3], self.cfg)
        self.assertEqual(method, "physics")
        self.assertEqual((lo, hi), (4, 13))  # 54/450*60-3=4.2 -> 4; 54/340*60+3=12.5 -> 13

    def test_base_rates(self):
        rows = [OutcomeRow("a", "kam", OUTCOME_REACHED), OutcomeRow("b", "kam", OUTCOME_DOWNED),
                OutcomeRow("c", "kam", OUTCOME_DIVERTED), OutcomeRow("d", "kam", OUTCOME_DIVERTED)]
        rate, n = base_rates(rows, CalibrationConfig())["kam"]
        self.assertEqual(n, 4)
        self.assertAlmostEqual(rate, 0.5)  # (2+1)/(4+2)

    def test_evaluate(self):
        points = {"x": PointRef("x", ["x"], 2, eta_min=10, eta_max=20)}
        obs = [ArrivalObservation("t1", "x", T0, 15), ArrivalObservation("t2", "x", T0, 25)]
        self.assertEqual(evaluate(points, obs)["coverage"], 0.5)


class PipelineTests(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        root = Path(self.tmp.name)
        self.repo = ReferenceRepository(root / "ref")
        self.store = LearningStore(root / "learning.db")
        v1 = self.repo.save_new(seed_from_dictionary())
        self.repo.promote(v1.version)
        self.now = T0 + timedelta(days=30)

    def tearDown(self):
        self.store.close()
        self.tmp.cleanup()

    def _add_reached(self, idx: int, borshchiv_min: float, tlumach_min: float):
        target = f"T{idx:02d}"
        start = T0 + timedelta(days=idx)
        arrival = start + timedelta(minutes=borshchiv_min)
        self.store.add_mention(target, "borshchiv", "chan_a", start, idx * 10)
        self.store.add_mention(target, "borshchiv", "chan_b", start + timedelta(minutes=2), idx * 10 + 1)
        self.store.add_mention(target, "tlumach", "chan_a", arrival - timedelta(minutes=tlumach_min), idx * 10 + 2)
        self.store.label_target(target, OUTCOME_REACHED, arrival)

    def test_freeze_during_active_target(self):
        self.store.add_mention("LIVE", "borshchiv", "chan_a", self.now - timedelta(minutes=10))
        report = retrain(self.store, self.repo, now=self.now)
        self.assertEqual(report.status, "skipped")
        self.assertIn("відкладено", report.reason)

    def test_retrain_creates_pending_and_uses_earliest_mention(self):
        for i in range(1, 6):
            self._add_reached(i, borshchiv_min=24 + i % 2, tlumach_min=5)
        obs = [o for o in self.store.arrival_observations() if o.point_id == "borshchiv"]
        self.assertEqual(len(obs), 5)  # по одному на ціль, найраніший канал
        report = retrain(self.store, self.repo, now=self.now)
        self.assertEqual(report.status, "pending")
        self.assertEqual(self.repo.active_version(), 1)  # без схвалення не активується
        new = self.repo.load(report.version)
        self.assertEqual(new.points["borshchiv"].eta_max, 27)  # 25 + запас 2, розширено
        self.assertTrue(any("borshchiv" in line for line in diff(self.repo.active(), new)))

    def test_second_retrain_without_new_data_is_skipped(self):
        for i in range(1, 11):  # 10 цілей -> 3 у перевірці
            self._add_reached(i, 24, 5)
        first = retrain(self.store, self.repo, auto_promote=True, now=self.now)
        self.assertEqual(first.status, "promoted")
        second = retrain(self.store, self.repo, auto_promote=True, now=self.now)
        self.assertEqual(second.status, "skipped")

    def test_auto_promote_blocked_with_small_holdout(self):
        for i in range(1, 3):
            self._add_reached(i, 24, 5)
        report = retrain(self.store, self.repo, auto_promote=True, now=self.now)
        self.assertEqual(report.status, "pending")
        self.assertFalse(report.gates["enough_holdout"])

    def test_alias_discovery_and_approval_flow(self):
        self.store.add_post("chan_a", 1, T0, "Реактивна герань через Скалат на захід", True)
        self.store.add_post("chan_a", 2, T0, "Реактивна герань на Борщеві", True)
        self.store.add_post("chan_a", 3, T0, "Через Скалат ще одна", False)  # не про ціль
        discover_aliases(self.store, self.repo.active())
        cands = {c.phrase_norm: c for c in self.store.alias_candidates()}
        self.assertIn("скалат", cands)
        self.assertEqual(cands["скалат"].count, 1)
        self.assertNotIn("борщеві", cands)  # вже є в словнику

        # Повторне сканування не дублює лічильник
        discover_aliases(self.store, self.repo.active())
        self.assertEqual(self.store.alias_candidates()[0].count, 1)

        self.store.decide_alias("скалат", "approved", "skalat", new_point_level=2)
        report = retrain(self.store, self.repo, now=self.now)
        self.assertEqual(report.status, "pending")
        new = self.repo.load(report.version)
        self.assertIn("скалат", new.points["skalat"].names)
        self.assertEqual(new.points["skalat"].level, 2)

    def test_build_candidate_does_not_mutate_active(self):
        active = self.repo.active()
        before = active.points["borshchiv"].eta_max
        build_candidate(active, [ArrivalObservation("t", "borshchiv", T0, 40)], [], [], CalibrationConfig())
        self.assertEqual(active.points["borshchiv"].eta_max, before)


if __name__ == "__main__":
    unittest.main()
