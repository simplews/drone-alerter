"""Tests for the message parser and the normalizer (MessageNormalizer)."""
import tempfile
import unittest
from datetime import UTC, datetime, timedelta
from pathlib import Path

from src.engine.models import ActionType, DroneType, RawPost
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

        # 4. Grammatical case normalization for regions (locative -щині/-ччині -> accusative -щину/-ччину)
        p4 = RawPost("chan_a", 43, "Реактивний БпЛА курсом на Рівненщині", T0)
        parsed4 = self.normalizer.parse(p4)
        self.assertEqual(parsed4.divert_destination, "Рівненщину")

        # 5. Compound place name divert destination
        p5 = RawPost("chan_a", 44, "Реактивний БпЛА на Перемишляни", T0)
        parsed5 = self.normalizer.parse(p5)
        self.assertEqual(parsed5.action, ActionType.DIVERTED)
        self.assertEqual(parsed5.divert_destination, "Перемишляни")

    def test_murovani_kyrylivtsi_is_corridor_point_not_divert(self):
        # Murovani Kyrylivtsi is in the corridor between Vendychany and Nova Ushytsia.
        # It must NOT trigger ActionType.DIVERTED.
        p = RawPost("chan_a", 50, "Реактивний БпЛА на Муровані-Курилівці", T0)
        parsed = self.normalizer.parse(p)
        self.assertEqual(parsed.action, ActionType.NONE)
        self.assertEqual(parsed.points, ["murovani_kyrylivtsi"])



    def test_distant_region_does_not_trigger_divert_action(self):
        # A post describing drone heading in Zhytomyr region / Berdychiv without corridor points
        # must NOT trigger ActionType.DIVERTED for western corridor
        p = RawPost("kpszsu", 86185, "🏍 Реактивний БпЛА на Житомирщині, курс Бердичів", T0)
        parsed = self.normalizer.parse(p)
        self.assertEqual(parsed.action, ActionType.NONE)
        self.assertEqual(parsed.points, [])

    def test_distant_section_place_name_not_matched_as_corridor_point(self):
        # Digest with Kamianets under Kirovohrad region header
        text = (
            "Житомирщина:\n"
            "Реактивний БпЛА курсом на Чоповичі\n\n"
            "Кіровоградщина:\n"
            "Реактивний БпЛА курсом на Камʼянець\n"
            "Реактивний БпЛА курсом на Голованівськ"
        )
        p = RawPost("kudy_letyt", 122901, text, T0)
        parsed = self.normalizer.parse(p)
        self.assertEqual(parsed.points, [])
        self.assertEqual(parsed.action, ActionType.NONE)
    def test_distant_region_approaching_corridor_is_ignored(self):
        # Post 86371: drone still in Podilsk district of Odesa region heading towards Vinnytsia/Pishchanka
        # Must be ignored until drone actually enters Vinnytsia region / Pishchanka
        text = "🏍 Реактивний БпЛА в Подільському районі Одещини ➡️ на Вінниччину (Піщанка)"
        p = RawPost("kpszsu", 86371, text, T0)
        parsed = self.normalizer.parse(p)
        self.assertEqual(parsed.points, [])
        self.assertEqual(parsed.action, ActionType.NONE)

    def test_conventional_vs_jet_drone_classification(self):
        # 1. Conventional Shahed mention
        p_conv = RawPost("chan_a", 1, "Шахед на Нову Ушицю", T0)
        parsed_conv = self.normalizer.parse(p_conv)
        self.assertEqual(parsed_conv.drone_type, DroneType.CONVENTIONAL)
        self.assertFalse(parsed_conv.is_jet)
        self.assertEqual(parsed_conv.points, ["nova_ushytsia"])

        # 2. Moped mention (conventional)
        p_moped = RawPost("chan_a", 2, "2х мопеди на Дунаївці", T0)
        parsed_moped = self.normalizer.parse(p_moped)
        self.assertEqual(parsed_moped.drone_type, DroneType.CONVENTIONAL)
        self.assertFalse(parsed_moped.is_jet)
        self.assertEqual(parsed_moped.count, 2)
        self.assertEqual(parsed_moped.points, ["dunaivtsi"])

        # 3. Explicit Jet mention
        p_jet = RawPost("chan_a", 3, "Реактивний шахед на Камʼянець-Подільський", T0)
        parsed_jet = self.normalizer.parse(p_jet)
        self.assertEqual(parsed_jet.drone_type, DroneType.JET)
        self.assertTrue(parsed_jet.is_jet)
        self.assertEqual(parsed_jet.points, ["kamianets_podilskyi"])

    def test_context_inheritance_respects_drone_type(self):
        # Post 1: Jet drone on Nova Ushytsia
        p1 = RawPost("chan_a", 10, "Реактивний БпЛА на Нову Ушицю", T0)
        parsed1 = self.normalizer.parse(p1)
        self.assertEqual(parsed1.drone_type, DroneType.JET)

        # Post 2: 3 minutes later, follow-up without general drone keyword inherits jet
        p2 = RawPost("chan_a", 11, "Дунаївці — курс на захід", T0 + timedelta(minutes=3))
        parsed2 = self.normalizer.parse(p2)
        self.assertEqual(parsed2.drone_type, DroneType.JET)
        self.assertTrue(parsed2.is_jet)

        # Post 3: 5 minutes later, explicit "Шахед" keyword does NOT inherit jet!
        p3 = RawPost("chan_a", 12, "Шахед через Камʼянець-Подільський", T0 + timedelta(minutes=5))
        parsed3 = self.normalizer.parse(p3)
        self.assertEqual(parsed3.drone_type, DroneType.CONVENTIONAL)
        self.assertFalse(parsed3.is_jet)

    def test_raion_disambiguation_with_specific_settlement(self):
        # When a post mentions a specific settlement and its district on the same line,
        # the district center should be suppressed in favor of the specific settlement.
        p1 = RawPost("chan_a", 20, "Чернелиця, Коломийський район", T0)
        parsed1 = self.normalizer.parse(p1)
        self.assertEqual(parsed1.points, ["chernelytsia"])

    def test_standalone_raion_detection(self):
        # When only the district is mentioned, the regional center point is identified.
        p1 = RawPost("chan_a", 21, "Шахед у Коломийському районі", T0)
        parsed1 = self.normalizer.parse(p1)
        self.assertEqual(parsed1.points, ["kolomyia"])


if __name__ == "__main__":
    unittest.main()

