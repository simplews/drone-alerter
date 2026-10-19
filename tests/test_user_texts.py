"""Tests for user-facing texts, copywriting invariants, and markdown safety."""
from __future__ import annotations

import re
import tempfile
import unittest
from datetime import UTC, datetime, timedelta
from pathlib import Path
from unittest.mock import AsyncMock, MagicMock, patch

from aiogram.types import Chat, Message

from src.engine.models import AlertLevel, DroneType, RawPost, Target
from src.engine.tracker import TargetTracker
from src.learning.reference import (
    ActiveReferenceCache,
    ReferenceRepository,
    seed_from_profile,
)
from src.main import Application
from src.nlp.normalizer import MessageNormalizer
from src.siren.service import SirenState
from src.telegram.bot import SubscriberRepository, create_bot_dispatcher

T0 = datetime(2026, 10, 15, 12, 0, tzinfo=UTC)


def check_markdown_pairs(text: str) -> tuple[int, int]:
    """Return remainder modulo 2 for bold (**) and italic (_) markdown delimiters outside URLs and code."""
    # Remove inline markdown links [text](url) keeping inner text
    cleaned = re.sub(r"\[([^\]]*)\]\(([^)]*)\)", r"\1", text)
    # Remove inline code spans `code`
    cleaned = re.sub(r"`[^`]*`", "", cleaned)
    bold_count = cleaned.count("**")
    no_bold = cleaned.replace("**", "")
    italic_count = no_bold.count("_")
    return bold_count % 2, italic_count % 2


class TestUserTextsInvariants(unittest.IsolatedAsyncioTestCase):
    async def asyncSetUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.repo = ReferenceRepository(Path(self.tmp.name) / "ref")
        v1 = self.repo.save_new(seed_from_profile())
        self.repo.promote(v1.version)
        self.cache = ActiveReferenceCache(self.repo)
        self.normalizer = MessageNormalizer(self.cache)
        self.tracker = TargetTracker(self.cache)

        self.sub_repo = MagicMock(spec=SubscriberRepository)
        self.siren_service = MagicMock()
        self.siren_service.get_current_state.return_value = None
        self.feedback_repo = MagicMock()
        self.feedback_repo.last_ticket_time.return_value = None

        self.dp = create_bot_dispatcher(
            self.sub_repo,
            tracker=self.tracker,
            siren_service=self.siren_service,
            feedback_repo=self.feedback_repo,
        )

    async def asyncTearDown(self):
        self.tmp.cleanup()

    def _get_handler(self, name: str):
        for h in self.dp.message.handlers:
            if h.callback.__name__ == name:
                return h.callback
        raise ValueError(f"Handler {name} not found")

    async def test_siren_end_has_no_all_clear_words(self):
        """Verify siren end notification never claims danger has passed or area is safe."""
        app = Application()
        app.bot = MagicMock()
        state_end = SirenState(region="Івано-Франківська область", is_active=False)

        with patch("src.main.broadcast_siren_event", new_callable=AsyncMock) as mock_broadcast:
            await app.on_siren_end(state_end)
            mock_broadcast.assert_called_once()
            call_args = mock_broadcast.call_args[0]
            body = call_args[3].lower()

            forbidden_words = ["минула", "безпечно", "безпечн"]
            for word in forbidden_words:
                self.assertNotIn(word, body)

            self.assertIn("за офіційними даними", body)
            self.assertIn("відбій", body)
            self.assertIn("залишайтеся уважними", body)

    def test_downed_alert_does_not_contain_point_name(self):
        """Verify shoot-down and target loss alerts do not publish the point name and respect siren state."""
        cases = [
            ("Тлумач", "2х Реактивні БпЛА через Тлумач", "Біля Тлумача одну збито!"),
            ("Борщів", "2х Реактивні БпЛА на Борщів", "В районі Борщева одну ціль збито!"),
            ("Нова Ушиця", "2х Реактивні БпЛА повз Нову Ушицю", "Біля Нової Ушиці одну збито!"),
        ]

        for pt_name, initial_text, downed_text in cases:
            for siren_active in (False, True):
                with self.subTest(point=pt_name, siren_active=siren_active):
                    tracker = TargetTracker(self.cache, is_siren_active_fn=lambda s=siren_active: s)
                    p1 = RawPost("chan_a", 10, initial_text, T0)
                    alerts1 = tracker.process_message(p1, self.normalizer.parse(p1), now=T0)
                    self.assertEqual(len(alerts1), 1)

                    # Partial shoot-down
                    p2 = RawPost("chan_a", 11, downed_text, T0 + timedelta(minutes=2))
                    alerts2 = tracker.process_message(p2, self.normalizer.parse(p2), now=T0 + timedelta(minutes=2))
                    self.assertEqual(len(alerts2), 1)
                    self.assertNotIn(pt_name, alerts2[0].body)
                    if siren_active:
                        self.assertIn("до офіційного відбою", alerts2[0].body)
                    else:
                        self.assertNotIn("до офіційного відбою", alerts2[0].body)
                        self.assertIn("не ігноруйте сигнали повітряної тривоги", alerts2[0].body)

                    # Final shoot-down
                    p3 = RawPost("chan_a", 12, "Другу також знищено", T0 + timedelta(minutes=4))
                    alerts3 = tracker.process_message(p3, self.normalizer.parse(p3), now=T0 + timedelta(minutes=4))
                    self.assertEqual(len(alerts3), 1)
                    self.assertNotIn(pt_name, alerts3[0].body)
                    if siren_active:
                        self.assertIn("до офіційного відбою", alerts3[0].body)
                    else:
                        self.assertNotIn("до офіційного відбою", alerts3[0].body)
                        self.assertIn("не ігноруйте сигнали повітряної тривоги", alerts3[0].body)

    def test_lost_timeout_alert_mentions_threat_remains(self):
        """Verify timeout alerts explicitly state that lack of updates does not mean danger is gone."""
        # Level 1 target timeout
        tracker1 = TargetTracker(self.cache)
        p1 = RawPost("chan_a", 1, "Реактивна герань на Нову Ушицю", T0)
        tracker1.process_message(p1, self.normalizer.parse(p1), now=T0)
        target1 = tracker1.get_active_targets()[0]
        timeout_time1 = target1.eta_window_end_utc + timedelta(seconds=10)
        alerts1 = tracker1.check_timeouts(now=timeout_time1)
        self.assertEqual(len(alerts1), 1)
        self.assertIn("не означає, що загрози немає", alerts1[0].body)

        # Level 2 target timeout
        tracker2 = TargetTracker(self.cache)
        p2 = RawPost("chan_a", 2, "Реактивна герань на Борщів", T0)
        tracker2.process_message(p2, self.normalizer.parse(p2), now=T0)
        target2 = tracker2.get_active_targets()[0]
        timeout_time2 = target2.eta_window_end_utc + timedelta(minutes=5)
        alerts2 = tracker2.check_timeouts(now=timeout_time2)
        self.assertEqual(len(alerts2), 1)
        self.assertIn("однак загроза може повторитися", alerts2[0].body)

    def test_regional_alert_does_not_claim_100_percent(self):
        """Verify regional alert does not contain false certainty '100%'."""
        p = RawPost("chan_a", 1, "БпЛА біля Коломиї", T0)
        alerts = self.tracker.process_message(p, self.normalizer.parse(p), now=T0)
        self.assertEqual(len(alerts), 1)
        alert = alerts[0]
        self.assertEqual(alert.level, AlertLevel.REGIONAL)
        self.assertNotIn("100%", alert.body)
        self.assertNotIn("100%", alert.title)
        self.assertIn("Найімовірніше, в області буде оголошено тривогу", alert.body)
        self.assertIn("Франківськ", alert.body)

    def test_eta_body_no_duplicate_minutes_and_level_phrasing(self):
        """Verify ETA body does not repeat duration in parentheses and reflects uncertainty on distant levels."""
        # Level 1 (distant)
        t1 = TargetTracker(self.cache)
        p1 = RawPost("chan_a", 1, "Шахед на Нову Ушицю", T0)
        a1 = t1.process_message(p1, self.normalizer.parse(p1), now=T0)[0]
        self.assertIn("У разі руху в напрямку міста — орієнтовно:", a1.body)
        self.assertNotIn("Очікується біля міста", a1.body)
        self.assertNotIn("(≈", a1.body)
        self.assertEqual(a1.body.count("хв)"), 1)

        # Level 2 (approach)
        t2 = TargetTracker(self.cache)
        p2 = RawPost("chan_a", 2, "Реактивна герань на Борщів", T0)
        a2 = t2.process_message(p2, self.normalizer.parse(p2), now=T0)[0]
        self.assertIn("Орієнтовний час підльоту (якщо курс збережеться):", a2.body)
        self.assertNotIn("(≈", a2.body)
        self.assertEqual(a2.body.count("хв)"), 1)

        # Level 3A (close approach)
        t3 = TargetTracker(self.cache)
        p3 = RawPost("chan_a", 3, "Реактивна герань на Тлумач", T0)
        a3 = t3.process_message(p3, self.normalizer.parse(p3), now=T0)[0]
        self.assertIn("Орієнтовний час підльоту до міста:", a3.body)
        self.assertNotIn("(≈", a3.body)
        self.assertEqual(a3.body.count("хв)"), 1)

    async def test_cmd_start_mentions_general_rule_and_shahed(self):
        """Verify /start text includes key safety disclaimers, Shahed reference, /stop and no 'акаунт'."""
        handler = self._get_handler("cmd_start")
        msg = MagicMock(spec=Message, text="/start", chat=MagicMock(spec=Chat, id=100))
        msg.answer = AsyncMock()

        await handler(msg)
        msg.answer.assert_called_once()
        text = msg.answer.call_args[0][0]

        self.assertIn("Головне правило", text)
        self.assertIn("Шахед", text)
        self.assertIn("/stop", text)
        self.assertNotIn("акаунт", text.lower())

    async def test_cmd_status_no_internal_level_codes(self):
        """Verify /status text does not leak internal technical codes 3A or 3B."""
        # 1. With active Level 3A target
        t3a = Target(
            id="t1",
            points_sequence=["tlumach"],
            last_point_id="tlumach",
            first_seen_utc=T0,
            last_seen_utc=T0,
            eta_min_minutes=5.0,
            eta_max_minutes=8.0,
            eta_window_start_utc=T0 + timedelta(minutes=5),
            eta_window_end_utc=T0 + timedelta(minutes=8),
            level=AlertLevel.LEVEL_3A,
            drone_type=DroneType.JET,
        )
        self.tracker.get_active_targets = MagicMock(return_value=[t3a])

        handler = self._get_handler("cmd_status")
        msg = MagicMock(spec=Message, text="/status", chat=MagicMock(spec=Chat, id=100))
        msg.answer = AsyncMock()

        await handler(msg)
        text3a = msg.answer.call_args[0][0]
        self.assertNotIn("3A", text3a)
        self.assertNotIn("3B", text3a)
        self.assertIn("Небезпека (підліт)", text3a)

        # 2. With active Level 3B target
        t3b = Target(
            id="t2",
            points_sequence=["tysmenytsia"],
            last_point_id="tysmenytsia",
            first_seen_utc=T0,
            last_seen_utc=T0,
            eta_min_minutes=1.0,
            eta_max_minutes=3.0,
            eta_window_start_utc=T0 + timedelta(minutes=1),
            eta_window_end_utc=T0 + timedelta(minutes=3),
            level=AlertLevel.LEVEL_3B,
            drone_type=DroneType.JET,
        )
        self.tracker.get_active_targets = MagicMock(return_value=[t3b])
        msg.answer.reset_mock()

        await handler(msg)
        text3b = msg.answer.call_args[0][0]
        self.assertNotIn("3A", text3b)
        self.assertNotIn("3B", text3b)
        self.assertIn("Небезпека біля / над містом", text3b)

    async def test_markdown_formatting_valid(self):
        """Verify all static bot texts have strictly paired markdown ** and _ delimiters."""
        async def fetch_text(handler_name: str, cmd_str: str) -> str:
            msg = MagicMock(spec=Message, text=cmd_str, chat=MagicMock(spec=Chat, id=100))
            msg.answer = AsyncMock()
            h = self._get_handler(handler_name)
            try:
                await h(msg)
            except TypeError:
                state = MagicMock()
                state.set_state = AsyncMock()
                await h(msg, state)
            return msg.answer.call_args[0][0]

        commands_to_check = {
            "/start": ("cmd_start", "/start"),
            "/eta": ("cmd_eta", "/eta"),
            "/disclaimer": ("cmd_disclaimer", "/disclaimer"),
            "/howitworks": ("cmd_howitworks", "/howitworks"),
            "/test": ("cmd_test", "/test"),
            "/stop": ("cmd_stop", "/stop"),
            "/feedback": ("cmd_feedback", "/feedback"),
        }

        for cmd, (h_name, c_str) in commands_to_check.items():
            with self.subTest(command=cmd):
                raw_text = await fetch_text(h_name, c_str)
                bold_rem, italic_rem = check_markdown_pairs(raw_text)
                self.assertEqual(bold_rem, 0, f"Unmatched ** in {cmd} text")
                self.assertEqual(italic_rem, 0, f"Unmatched _ in {cmd} text")
