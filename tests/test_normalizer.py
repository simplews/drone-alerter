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

    def test_kamianets_podilskyi_raion_maps_to_nova_ushytsia(self):
        # A drone heading from Vinnytsia to Kamianets-Podilskyi district enters the corridor
        # at Nova Ushytsia (Level 1) rather than prematurely triggering the city (Level 2).
        post = RawPost(
            "monitorwarr",
            42625,
            "⚠️ 1 реактив на Вінниччині у бік Хмельниччини, Камʼянець-Подільський р-н.",
            T0,
        )
        parsed = self.normalizer.parse(post)
        self.assertTrue(parsed.is_jet)
        self.assertEqual(parsed.points, ["nova_ushytsia"])

    def test_nova_ushytsia_inflections(self):
        # "дрон ще в районі Н. Ушиці" must match nova_ushytsia
        post = RawPost("monitoring_zaxid", 28069, "дрон ще в районі Н. Ушиці", T0)
        parsed = self.normalizer.parse(post)
        self.assertEqual(parsed.points, ["nova_ushytsia"])

    def test_settlement_in_kamianets_district_disambiguation(self):
        # Specific settlement (Dunaivtsi) takes precedence over the raion qualifier
        post = RawPost("monitorwarr", 42640, "⚠️ 1 реактив: Дунаївці, Камʼянець-Подільський р-н", T0)
        parsed = self.normalizer.parse(post)
        self.assertEqual(parsed.points, ["dunaivtsi"])

    def test_kamianets_podilskyi_city_direct_mention(self):
        # Explicit mention of the city without district suffix triggers the city point
        post = RawPost("kudy_letyt", 42650, "Хмельниччина:\nРеактивний БпЛА курсом на Камʼянець-Подільський", T0)
        parsed = self.normalizer.parse(post)
        self.assertEqual(parsed.points, ["kamianets_podilskyi"])

    def test_markdown_bold_distant_section_header_ignored(self):
        # **Кіровоградщина:** with trailing ** must be recognized as a distant section header
        # so "БпЛА курсом на Камʼянець" (Novhorodka in Kirovohrad region) does NOT trigger Kamianets-Podilskyi
        text = (
            "**Київ:**\n"
            "Реактивний БпЛА над містом\n\n"
            "**Кіровоградщина:**\n"
            "БпЛА курсом на Камʼянець\n"
            "2х Реактивні БпЛА курсом на Павлиш"
        )
        post = RawPost("kudy_letyt", 123797, text, T0)
        parsed = self.normalizer.parse(post)
        self.assertEqual(parsed.points, [])
        self.assertEqual(parsed.action, ActionType.NONE)

    def test_locative_region_and_northern_track_not_parsed_as_corridor_divert(self):
        # "на Рівненщині" / "на Тернопільщині" (locative) and northern-track messages must not trigger DIVERTED
        for idx, msg in enumerate([
            "📡Дорозвідка на Рівненщині!",
            "Реактивний БпЛА повз Степань на Рівненщині",
            "🏍 Реактивний БпЛА на заході Рівненщини, курс на Волинь.",
            "🏍 Реактивний БпЛА з півночі Хмельниччини на Рівненщину.",
            "Пішла все-таки на Волинь, Колки/Ківерці",
            "**Рівненщина:**\nРеактивний БпЛА повз Городок залітає на Рівне",
        ]):
            post = RawPost("monitoring_zaxid", 28100 + idx, msg, T0)
            parsed = self.normalizer.parse(post)
            self.assertEqual(parsed.points, [], msg)
            self.assertNotEqual(parsed.action, ActionType.DIVERTED, msg)

    def test_preventive_and_section_header_reconnaissance_not_active_points(self):
        # "**Коломия:**\nДорозвідка по БпЛА" and "Калуський район ... превентивано" must not trigger active points
        p1 = RawPost("kudy_letyt", 123399, "**Коломия:**\nДорозвідка по БпЛА", T0)
        parsed1 = self.normalizer.parse(p1)
        self.assertEqual(parsed1.points, [])
        self.assertEqual(parsed1.action, ActionType.CLEAR)

        p2 = RawPost(
            "monitoring_zaxid",
            28141,
            "Калуський район Франківщини - дронова небезпека превентивано. В регіоні триває дорозвідка!",
            T0,
        )
        parsed2 = self.normalizer.parse(p2)
        self.assertEqual(parsed2.points, [])
        self.assertEqual(parsed2.action, ActionType.CLEAR)

    def test_lost_fixation_and_defense_forces_engagement_actions(self):
        # "Втрата фіксації цілі поблизу Коломиї" -> LOST at kolomyia
        p_lost = RawPost("monitoring_zaxid", 28025, "📡Втрата фіксації цілі поблизу Коломиї, деталі від офіційних осіб.", T0)
        parsed_lost = self.normalizer.parse(p_lost)
        self.assertEqual(parsed_lost.points, [])
        self.assertEqual(parsed_lost.action, ActionType.LOST)
        self.assertEqual(parsed_lost.action_point_id, "kolomyia")

        # "по тому, котрий летів на Нову Ушицю - працювали сили оборони" -> DOWNED at nova_ushytsia
        p_down = RawPost(
            "monitoring_zaxid",
            28071,
            "Рівненщина:\n1 реактивна герань на Дубно\n\nХмельниччина - дорозвідка, по тому, котрий летів на Нову Ушицю - працювали сили оборони",
            T0,
        )
        parsed_down = self.normalizer.parse(p_down)
        self.assertEqual(parsed_down.points, [])
        self.assertEqual(parsed_down.action, ActionType.DOWNED)
        self.assertEqual(parsed_down.action_point_id, "nova_ushytsia")

    def test_daily_summary_ignored(self):
        p_sum = RawPost("kpszsu", 87000, "⚡️ ЗБИТО/ПОДАВЛЕНО 536 ВОРОЖИХ БПЛА", T0)
        parsed = self.normalizer.parse(p_sum)
        self.assertEqual(parsed.points, [])
        self.assertEqual(parsed.action, ActionType.NONE)

    def test_pudlivtsi_corridor_point(self):
        post = RawPost("kudy_letyt", 124001, "Хмельниччина:\nРеактивний БпЛА курсом на Пудлівці", T0)
        parsed = self.normalizer.parse(post)
        self.assertTrue(parsed.is_jet)
        self.assertEqual(parsed.points, ["pudlivtsi"])
        self.assertEqual(parsed.action, ActionType.NONE)

    def test_cruise_missile_kh101_and_kalibr_parsing(self):
        # 1. Explicit Х-101 group
        p_kh101 = RawPost("kpszsu", 90001, "🚀 3х КР Х-101 через Нову Ушицю в напрямку Дунаївців", T0)
        parsed_kh101 = self.normalizer.parse(p_kh101)
        self.assertEqual(parsed_kh101.drone_type, DroneType.MISSILE)
        self.assertEqual(parsed_kh101.target_type, "КР Х-101")
        self.assertEqual(parsed_kh101.count, 3)
        self.assertTrue(parsed_kh101.explicit_count)
        self.assertEqual(parsed_kh101.points, ["nova_ushytsia", "dunaivtsi"])

        # 2. Explicit Kalibr
        p_kalibr = RawPost("monitoring_zaxid", 90002, "2 Калібри повз Камʼянець-Подільський на Городенку", T0)
        parsed_kalibr = self.normalizer.parse(p_kalibr)
        self.assertEqual(parsed_kalibr.drone_type, DroneType.MISSILE)
        self.assertEqual(parsed_kalibr.target_type, "КР Калібр")
        self.assertEqual(parsed_kalibr.count, 2)
        self.assertEqual(parsed_kalibr.points, ["kamianets_podilskyi", "horodenka"])

        # 3. Follow-up in same channel without keyword inherits MISSILE context
        p_follow = RawPost("monitoring_zaxid", 90003, "Курсом на Бурштин!", T0 + timedelta(minutes=2))
        parsed_follow = self.normalizer.parse(p_follow)
        self.assertEqual(parsed_follow.drone_type, DroneType.MISSILE)
        self.assertEqual(parsed_follow.target_type, "КР Калібр")
        self.assertEqual(parsed_follow.points, ["burshtyn"])

    def test_missed_if_course_and_corridor_examples(self):
        # 1. 28000 -> 28002 (Bukovyna: Kelmentsi -> IF raion drone danger on Bukovyna)
        p_28000 = RawPost("monitoring_zaxid", 28000, "Буковина:\n1 реактивна герань на Кельменці", T0)
        res_28000 = self.normalizer.parse(p_28000)
        self.assertTrue(res_28000.is_jet)
        self.assertEqual(res_28000.points, ["kelmentsi"])
        self.assertEqual(res_28000.action, ActionType.NONE)

        p_28002 = RawPost(
            "monitoring_zaxid",
            28002,
            "Івано-франківський район - дронова небезпека по реактиву на Буковині із західним курсом",
            T0 + timedelta(seconds=50),
        )
        res_28002 = self.normalizer.parse(p_28002)
        self.assertTrue(res_28002.is_jet)
        self.assertEqual(res_28002.points, ["bukovyna_if_course"])
        self.assertEqual(res_28002.action, ActionType.NONE)

        # 2. 28309 -> 28310 -> 28312 (Martynivka/Buchach -> IF raion danger on Ternopil -> Pidvertaie na Frankivsk)
        t_ep2 = T0 + timedelta(hours=1)
        p_28309 = RawPost(
            "monitoring_zaxid",
            28309,
            "Тернопільська область:\n1 реактивна герань поблизу Чорткова, курс на Мартинівку",
            t_ep2,
        )
        res_28309 = self.normalizer.parse(p_28309)
        self.assertTrue(res_28309.is_jet)
        self.assertEqual(res_28309.points, ["buchach"])
        self.assertEqual(res_28309.action, ActionType.NONE)

        p_28310 = RawPost(
            "monitoring_zaxid",
            28310,
            "Івано-Франківський район - дронова небезпека по реактиву на Тернопільщині",
            t_ep2 + timedelta(seconds=37),
        )
        res_28310 = self.normalizer.parse(p_28310)
        self.assertTrue(res_28310.is_jet)
        self.assertEqual(res_28310.points, ["ternopil_if_course"])
        self.assertEqual(res_28310.action, ActionType.NONE)

        p_28312 = RawPost(
            "monitoring_zaxid",
            28312,
            "Підвертає на Франківськ \n\n❗️",
            t_ep2 + timedelta(seconds=74),
        )
        res_28312 = self.normalizer.parse(p_28312)
        self.assertTrue(res_28312.is_jet)
        self.assertEqual(res_28312.points, ["if_direct_course"])
        self.assertEqual(res_28312.action, ActionType.NONE)

        # 3. 28346 -> 28348 (Drone danger in IF, nearest target towards Skala-Podilska)
        t_ep3 = T0 + timedelta(hours=2)
        p_28346 = RawPost(
            "monitoring_zaxid",
            28346,
            "Оголошено дронову небезпеку в Івано-франківську. Найближча ціль у бік Скали-Подільської, Тернопільщина",
            t_ep3,
        )
        res_28346 = self.normalizer.parse(p_28346)
        self.assertTrue(res_28346.is_jet)
        self.assertEqual(res_28346.points, ["skala_podilska"])
        self.assertEqual(res_28346.action, ActionType.NONE)

        p_28348 = RawPost(
            "monitoring_zaxid",
            28348,
            "1 реактивна герань на Скалу-П./Товсте",
            t_ep3 + timedelta(minutes=2),
        )
        res_28348 = self.normalizer.parse(p_28348)
        self.assertTrue(res_28348.is_jet)
        self.assertEqual(res_28348.points, ["skala_podilska", "tovste"])

        # 4. 28452 -> 28456 -> 124290 -> 28458 (Parenthetical preventive, south Ternopil towards IF, Tlumach)
        t_ep4 = T0 + timedelta(hours=3)
        p_28452 = RawPost(
            "monitoring_zaxid",
            28452,
            "Хмельниччина:\n1 реактивна герань на Камʼянець-Подільський (тривога в ІФ превентивна)",
            t_ep4,
        )
        res_28452 = self.normalizer.parse(p_28452)
        self.assertTrue(res_28452.is_jet)
        self.assertEqual(res_28452.points, ["kamianets_podilskyi"])
        self.assertEqual(res_28452.action, ActionType.NONE)

        p_28456 = RawPost(
            "monitoring_zaxid",
            28456,
            "1 реактивний на півдні Тернопільщини, рухається стандартним курсом у бік Франківська. Про наближення повідомлю",
            t_ep4 + timedelta(minutes=8),
        )
        res_28456 = self.normalizer.parse(p_28456)
        self.assertTrue(res_28456.is_jet)
        self.assertEqual(res_28456.points, ["ternopil_if_course"])
        self.assertEqual(res_28456.action, ActionType.NONE)

        p_124290 = RawPost(
            "kudy_letyt",
            124290,
            "Реактивний БпЛА курсом на Рівне \nРеактивний БпЛА курсом на Івано-Франківськ",
            t_ep4 + timedelta(minutes=9, seconds=30),
        )
        res_124290 = self.normalizer.parse(p_124290)
        self.assertTrue(res_124290.is_jet)
        self.assertEqual(res_124290.points, ["if_direct_course"])
        self.assertEqual(res_124290.action, ActionType.NONE)

        p_28458 = RawPost(
            "monitoring_zaxid",
            28458,
            "Франківськ - уважно, він уже біля Тлумача",
            t_ep4 + timedelta(minutes=13),
        )
        res_28458 = self.normalizer.parse(p_28458)
        self.assertTrue(res_28458.is_jet)
        self.assertEqual(res_28458.points, ["tlumach"])
        self.assertEqual(res_28458.action, ActionType.NONE)

    def test_missile_corridors_lviv_khmelnytskyi_ternopil(self):
        # For cruise missiles, directions to Khmelnytskyi, Ternopil, or Lviv are Level 1 corridors, NOT diversions
        t_m = T0 + timedelta(hours=5)
        p_khm = RawPost("kpszsu", 91001, "Група КР Х-101 курсом на Хмельницький", t_m)
        res_khm = self.normalizer.parse(p_khm)
        self.assertEqual(res_khm.drone_type, DroneType.MISSILE)
        self.assertEqual(res_khm.points, ["khmelnytskyi_missile"])
        self.assertEqual(res_khm.action, ActionType.NONE)

        p_ter = RawPost("monitoring_zaxid", 91002, "Калібри в напрямку Тернополя", t_m + timedelta(minutes=2))
        res_ter = self.normalizer.parse(p_ter)
        self.assertEqual(res_ter.drone_type, DroneType.MISSILE)
        self.assertEqual(res_ter.points, ["ternopil_missile"])
        self.assertEqual(res_ter.action, ActionType.NONE)

        p_lviv = RawPost("monitoring_zaxid", 91003, "Крилаті ракети змінили курс на Львів", t_m + timedelta(minutes=4))
        res_lviv = self.normalizer.parse(p_lviv)
        self.assertEqual(res_lviv.drone_type, DroneType.MISSILE)
        self.assertEqual(res_lviv.points, ["lviv_missile"])
        self.assertEqual(res_lviv.action, ActionType.NONE)

        p_if = RawPost("monitoring_zaxid", 91004, "Ракета повернула на Івано-Франківськ!", t_m + timedelta(minutes=6))
        res_if = self.normalizer.parse(p_if)
        self.assertEqual(res_if.drone_type, DroneType.MISSILE)
        self.assertEqual(res_if.points, ["if_direct_course"])
        self.assertEqual(res_if.action, ActionType.NONE)

    def test_raketa_dron_geran5_and_chornyi_ostriv_not_missile(self):
        """Regression test for 2026-10-18 false cruise missile alert:
        - 'ракета-дрон «Герань-5»' and 'Бандероль' must be classified as DroneType.JET, NOT DroneType.MISSILE.
        - Follow-up 'Хмельницький: Чорний Острів повз західні околиці ❗️' must be parsed as DIVERTED
          and must never synthesize 'khmelnytskyi_missile'.
        - 'підлітає до Деражні' must be parsed as ActionType.DIVERTED.
        """
        t0 = datetime(2026, 10, 18, 19, 26, 33, tzinfo=UTC)
        p_28673 = RawPost(
            "monitoring_zaxid",
            28673,
            "За уточненою інформацією та порівнянням ТТХ, вдалось визначити, що на Вінниччині в напрямку Козятина рухається не «Бандероль», а ракета-дрон «Герань-5»",
            t0,
        )
        res_28673 = self.normalizer.parse(p_28673)
        self.assertEqual(res_28673.drone_type, DroneType.JET)
        self.assertTrue(res_28673.is_jet)
        self.assertEqual(res_28673.target_type, "Герань-5")
        self.assertEqual(res_28673.points, [])

        p_28676 = RawPost(
            "monitoring_zaxid",
            28676,
            "Хмельниччина:\n1 реактивна герань поблизу Нової Ушиці, західний курс",
            t0 + timedelta(minutes=4),
        )
        res_28676 = self.normalizer.parse(p_28676)
        self.assertEqual(res_28676.drone_type, DroneType.JET)
        self.assertEqual(res_28676.points, ["nova_ushytsia"])

        p_28681 = RawPost(
            "monitoring_zaxid",
            28681,
            "Хмельницький: Чорний Острів повз західні околиці ❗️",
            t0 + timedelta(minutes=19, seconds=12),
        )
        res_28681 = self.normalizer.parse(p_28681)
        self.assertNotEqual(res_28681.drone_type, DroneType.MISSILE)
        self.assertEqual(res_28681.points, [])
        self.assertEqual(res_28681.action, ActionType.NONE)

        p_28682 = RawPost(
            "monitoring_zaxid",
            28682,
            "Йде на/повз Красилів з курсом на північ ❗️",
            t0 + timedelta(minutes=23, seconds=10),
        )
        res_28682 = self.normalizer.parse(p_28682)
        self.assertEqual(res_28682.action, ActionType.DIVERTED)

        p_der = RawPost("kudy_letyt", 124726, "Хмельниччина:\nБпЛА підлітає до Деражні", t0 + timedelta(minutes=25))
        res_der = self.normalizer.parse(p_der)
        self.assertEqual(res_der.action, ActionType.DIVERTED)
        self.assertEqual(res_der.divert_destination, "Деражні")

    def test_lisovi_hrynivtsi_and_cross_oblast_homonyms_do_not_trigger_tlumach(self):
        """Regression test for 2026-10-19 false Level 3A Tlumach alerts (kudy_letyt/125261 & 125305):
        - 'Лісові Гринівці' in Khmelnytskyi region must NEVER match 'tlumach' ('гринівці').
        - 'Озеряни' in Ternopil region ('на Скалу-Подільську/Озеряни') must match only 'skala_podilska', NOT 'tlumach'.
        - 'Гринівці' under '**Івано-Франківськ:**' DOES match 'tlumach'.
        - Cross-oblast suburb homonyms ('Микитинці' under 'Хмельниччина:', 'Вовчинці' under 'Вінниччина:') must NOT match IF suburbs.
        """
        t0 = datetime(2026, 10, 19, 13, 14, 51, tzinfo=UTC)

        # 1. Exact kudy_letyt/125261
        p_125261 = RawPost(
            "kudy_letyt",
            125261,
            "Хмельниччина:\nРеактивний БпЛА звернув на Лісові Гринівці",
            t0,
        )
        res_125261 = self.normalizer.parse(p_125261)
        self.assertEqual(res_125261.points, [])

        # 2. Exact kudy_letyt/125267
        p_125267 = RawPost(
            "kudy_letyt",
            125267,
            "Хмельниччина:\nРеактивний БпЛА повз Меджибіж тримає курс на Лісові Гринівці",
            t0 + timedelta(minutes=13),
        )
        res_125267 = self.normalizer.parse(p_125267)
        self.assertEqual(res_125267.points, [])

        # 3. Exact kudy_letyt/125305
        p_125305 = RawPost(
            "kudy_letyt",
            125305,
            "Хмельниччина:\nРеактивний БпЛА курсом на Лісові Гринівці",
            t0 + timedelta(minutes=95),
        )
        res_125305 = self.normalizer.parse(p_125305)
        self.assertEqual(res_125305.points, [])

        # 4. Ternopil Ozeriany must not jump to Tlumach
        p_ozer = RawPost(
            "monitoring_zaxid",
            28901,
            "Тернопільщина:\n1 реактивна герань на Скалу-Подільську/Озеряни",
            t0 + timedelta(minutes=100),
        )
        res_ozer = self.normalizer.parse(p_ozer)
        self.assertEqual(res_ozer.points, ["skala_podilska"])

        # 5. Real Hrynivtsi under Ivano-Frankivsk header DOES match tlumach
        p_if_hryn = RawPost(
            "kudy_letyt",
            125999,
            "**Івано-Франківськ:**\n2х Реактивні БпЛА курсом на Гринівці",
            t0 + timedelta(minutes=110),
        )
        res_if_hryn = self.normalizer.parse(p_if_hryn)
        self.assertEqual(res_if_hryn.points, ["tlumach"])
        self.assertEqual(res_if_hryn.count, 2)

        # 6. Cross-oblast suburb homonyms (Микитинці in Khmelnytskyi raion, Вовчинці in Vinnytsia oblast)
        p_myk = RawPost("kudy_letyt", 126001, "Хмельниччина:\nРеактивний БпЛА курсом на Микитинці", t0 + timedelta(minutes=120))
        self.assertEqual(self.normalizer.parse(p_myk).points, [])

        p_vov = RawPost("kudy_letyt", 126002, "Вінниччина:\nРеактивний БпЛА курсом на Вовчинці", t0 + timedelta(minutes=125))
        self.assertEqual(self.normalizer.parse(p_vov).points, [])

    def test_airport_in_other_cities_does_not_trigger_ivano_frankivsk(self):
        """Regression test for 2026-10-19/20 false Level 3B alerts caused by 'аеропорт' in other cities:
        - 'Львів: Реактивний БпЛА курсом на аеропорт' (kudy_letyt/125480) -> points=[]
        - 'Хмельницький: Реактивний БпЛА курсом на аеропорт' (kudy_letyt/125547, 125632) -> points=[]
        - '‼️Хмельницький - уважно, реактивна герань у бік аеропорту!' (monitoring_zaxid/29047) -> points=[]
        - 'Хмельницький: Реактивний БпЛА підлітає до аеропорту' (kudy_letyt/125598) -> points=[]
        - 'Вінниця: Реактивний БпЛА курсом на Немирівський повз аеропорт' (kudy_letyt/123195) -> points=[]
        - 'Рівне: Реактивний БпЛА курсом на аеропорт' (kudy_letyt/123379) -> points=[]
        - 'Івано-Франківськ: Реактивний БпЛА курсом на аеропорт' (kudy_letyt/123913) -> points=['ivano_frankivsk']
        """
        t0 = datetime(2026, 10, 19, 21, 13, 11, tzinfo=UTC)

        non_if_airport_posts = [
            (125480, "Львів:\nРеактивний БпЛА курсом на аеропорт"),
            (125547, "Хмельницький:\nРеактивний БпЛА курсом на аеропорт"),
            (29047, "‼️Хмельницький - уважно, реактивна герань у бік аеропорту!"),
            (125598, "Хмельницький:\nРеактивний БпЛА підлітає до аеропорту"),
            (123195, "Вінниця:\nРеактивний БпЛА курсом на Немирівський повз аеропорт"),
            (123379, "Рівне:\nРеактивний БпЛА курсом на аеропорт"),
            (28661, "Рівненщина:\n1 реактивна герань на аеропорт Рівне ‼️"),
        ]
        for idx, (mid, text) in enumerate(non_if_airport_posts):
            p = RawPost("kudy_letyt", mid, text, t0 + timedelta(minutes=idx * 5))
            res = self.normalizer.parse(p)
            self.assertEqual(res.points, [], f"Expected no IF points for {text!r}, got {res.points}")

        # Explicit Ivano-Frankivsk airport posts MUST still trigger 'ivano_frankivsk'
        if_airport_posts = [
            (123913, "Івано-Франківськ:\nРеактивний БпЛА курсом на аеропорт"),
            (123927, "Івано-Франківськ:\nРеактивний БпЛА курсом на аеропорт (1км)"),
            (124068, "Івано-Франківськ: реактивний БпЛА курсом на аеропорт"),
            (124099, "Реактивний БпЛА на Франківськ (аеропорт)"),
        ]
        for idx, (mid, text) in enumerate(if_airport_posts):
            p = RawPost("kudy_letyt", mid, text, t0 + timedelta(minutes=100 + idx * 5))
            res = self.normalizer.parse(p)
            self.assertEqual(res.points, ["ivano_frankivsk"], f"Expected ['ivano_frankivsk'] for {text!r}, got {res.points}")


if __name__ == "__main__":
    unittest.main()




