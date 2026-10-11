"""Трекер життєвого циклу цілей, просторово-часова дедуплікація та диспетчеризація сповіщень."""
from __future__ import annotations

import logging
import uuid
from datetime import UTC, datetime, timedelta

from src.engine.eta import calculate_eta, determine_level
from src.engine.models import (
    ActionType,
    AlertEvent,
    AlertLevel,
    ParsedMessage,
    RawPost,
    Target,
    TargetStatus,
)
from src.learning.reference import ActiveReferenceCache, ReferenceData
from src.learning.store import LearningStore
from src.learning.text import normalize

logger = logging.getLogger(__name__)


class TargetTracker:
    """Управляє активними гіпотезами цілей, дедуплікацією між каналами та формуванням сповіщень."""

    def __init__(
        self,
        ref_cache: ActiveReferenceCache,
        store: LearningStore | None = None,
        dedup_window_minutes: float = 6.0,
        max_corridor_gap_minutes: float = 35.0,
    ):
        self._ref_cache = ref_cache
        self._store = store
        self._dedup_window = timedelta(minutes=dedup_window_minutes)
        self._max_gap = timedelta(minutes=max_corridor_gap_minutes)
        self._active_targets: dict[str, Target] = {}
        # Збережені попередні сповіщення для запобігання спаму: target_id -> last_level
        self._target_last_alerted_level: dict[str, AlertLevel] = {}
        # Зафіксовані пункти, за якими вже виходило сповіщення: target_id -> set(point_ids)
        self._target_alerted_points: dict[str, set[str]] = {}


    def get_active_targets(self) -> list[Target]:
        return [t for t in self._active_targets.values() if t.status == TargetStatus.ACTIVE]

    def _get_ref(self) -> ReferenceData:
        return self._ref_cache.get()

    def _find_matching_target(self, point_id: str, posted_at: datetime) -> Target | None:
        """Знаходить активну ціль, якій може належати повідомлення."""
        ref = self._get_ref()
        new_point = ref.points.get(point_id)
        if not new_point:
            return None

        candidate: Target | None = None
        min_time_diff = timedelta.max

        for target in self._active_targets.values():
            if target.status != TargetStatus.ACTIVE:
                continue

            time_diff = abs(posted_at - target.last_seen_utc)
            if time_diff > self._max_gap:
                continue

            last_point = ref.points.get(target.last_point_id)
            if not last_point:
                continue

            # Дозволено:
            # 1. Той самий пункт із іншого каналу протягом dedup_window
            # 2. Новий пункт, що ближче до міста (рівень >= попередньому, або менша відстань)
            is_same_point = (point_id == target.last_point_id) and (time_diff <= self._dedup_window)
            is_forward = False

            if not is_same_point:
                if last_point.distance_km and new_point.distance_km:
                    is_forward = (new_point.distance_km <= last_point.distance_km + 10.0)
                else:
                    is_forward = (new_point.level >= last_point.level)

            if (is_same_point or is_forward) and time_diff < min_time_diff:
                min_time_diff = time_diff
                candidate = target

        return candidate

    def process_message(
        self,
        post: RawPost,
        parsed: ParsedMessage,
        now: datetime | None = None,
    ) -> list[AlertEvent]:
        """Обробляє розпарсений пост, оновлює стан цілей і генерує сповіщення."""
        now = now or datetime.now(UTC)
        ref = self._get_ref()
        alerts: list[AlertEvent] = []

        # 1. Запис у LearningStore для аналітики та перенавчання
        if self._store and (parsed.is_jet or parsed.points):
            self._store.add_post(
                channel=post.channel,
                message_id=post.message_id,
                posted_at=post.posted_at_utc,
                text=post.text,
                is_target_related=True,
            )

        # 2. Обробка дій (збито / втрачено / відворот)
        if parsed.action in (ActionType.DOWNED, ActionType.LOST, ActionType.DIVERTED):
            action_alerts = self._handle_action(post, parsed, now)
            alerts.extend(action_alerts)
            self._cleanup_expired(now)
            return alerts

        # Якщо в пості немає згадок пунктів — завершуємо
        if not parsed.points:
            self._cleanup_expired(now)
            return alerts

        # 3. Обробка пунктів у повідомленні
        for point_id in parsed.points:
            point = ref.points.get(point_id)
            if not point:
                continue

            # Шукаємо відповідну наявну ціль або створюємо нову
            target = self._find_matching_target(point_id, post.posted_at_utc)
            is_new_target = False

            if target is None:
                # Створюємо нову ціль
                is_new_target = True
                target_id = f"T-{post.posted_at_utc.strftime('%Y%m%d%H%M')}-{uuid.uuid4().hex[:4]}"
                target = Target(
                    id=target_id,
                    points_sequence=[point_id],
                    last_point_id=point_id,
                    target_count=parsed.count,
                    status=TargetStatus.ACTIVE,
                    first_seen_utc=post.posted_at_utc,
                    last_seen_utc=post.posted_at_utc,
                    level=determine_level(point_id, point.level),
                    channels={post.channel},
                    message_ids=[(post.channel, post.message_id)],
                )
                self._active_targets[target_id] = target
            else:
                # Оновлюємо наявну ціль
                if point_id not in target.points_sequence:
                    target.points_sequence.append(point_id)
                target.last_point_id = point_id
                target.last_seen_utc = post.posted_at_utc
                target.target_count = max(target.target_count, parsed.count)
                target.channels.add(post.channel)
                target.message_ids.append((post.channel, post.message_id))

                # Ескалація рівня (рівень може тільки підвищуватись)
                new_level = determine_level(point_id, point.level)
                if self._level_rank(new_level) > self._level_rank(target.level):
                    target.level = new_level

            # Фіксація згадки в сховищі навчання
            if self._store:
                self._store.add_mention(
                    target_id=target.id,
                    point_id=point_id,
                    channel=post.channel,
                    posted_at=post.posted_at_utc,
                    message_id=post.message_id,
                )

            # Розрахунок ETA
            rem_min, rem_max, w_start, w_end, w_str = calculate_eta(
                point=point,
                ref=ref,
                post_time_utc=post.posted_at_utc,
                current_time_utc=now,
            )
            target.eta_min_minutes = rem_min
            target.eta_max_minutes = rem_max
            target.eta_window_start_utc = w_start
            target.eta_window_end_utc = w_end

            # Впевненість
            if len(target.channels) >= 2 or "на іф" in normalize(post.text) or "франківськ" in normalize(post.text):
                target.confidence = "high"
            elif len(target.points_sequence) >= 2:
                target.confidence = "medium"
            else:
                target.confidence = "low"

            # 4. Формування сповіщення: перша ж поява в пункті одразу дає алерт
            alerted_points = self._target_alerted_points.setdefault(target.id, set())
            is_new_point_for_target = point_id not in alerted_points

            last_alerted = self._target_last_alerted_level.get(target.id)
            should_alert = False
            is_update = False

            if is_new_target or last_alerted is None or is_new_point_for_target or self._level_rank(target.level) > self._level_rank(last_alerted):
                should_alert = True
                alerted_points.add(point_id)
            elif last_alerted == target.level:
                is_update = True

            if should_alert or is_update:
                alert = self._build_alert(target, point, w_str, is_update=is_update)
                alerts.append(alert)
                if should_alert:
                    self._target_last_alerted_level[target.id] = target.level


        self._cleanup_expired(now)
        return alerts

    def _handle_action(
        self,
        post: RawPost,
        parsed: ParsedMessage,
        now: datetime,
    ) -> list[AlertEvent]:
        """Безпечна асиметрична обробка збиття/відвороту (правило F7)."""
        alerts: list[AlertEvent] = []
        target = None

        # Шукаємо ціль за пунктом або найближчу активну
        if parsed.action_point_id:
            target = self._find_matching_target(parsed.action_point_id, post.posted_at_utc)

        if target is None and self._active_targets:
            # Беремо найсвіжішу активну ціль
            active = self.get_active_targets()
            if active:
                target = max(active, key=lambda t: t.last_seen_utc)

        if target is None:
            return alerts

        ref = self._get_ref()
        point_name = target.last_point_id
        if target.last_point_id in ref.points and ref.points[target.last_point_id].names:
            point_name = ref.points[target.last_point_id].names[0].capitalize()

        if parsed.action in (ActionType.DOWNED, ActionType.LOST):
            if target.target_count > 1:
                # Зменшуємо лічильник цілей у групі
                target.target_count -= 1
                body = (
                    f"⚪️ За повідомленнями каналів, одну ціль у районі {point_name}, "
                    f"імовірно, збито/уражено. Увага: ще {target.target_count} ціль(і) у повітрі! "
                    f"Залишайтеся в укриттях."
                )
                alerts.append(AlertEvent(
                    target_id=target.id,
                    level=AlertLevel.INFO,
                    title="Оновлення: часткове ураження цілі",
                    body=body,
                    is_silent=False,
                ))
            else:
                target.status = TargetStatus.PROBABLY_DOWNED
                body = (
                    f"⚪️ За повідомленнями каналів, ціль у районі {point_name}, "
                    f"імовірно, уражена або втрачена. "
                    f"Залишайтеся в укриттях до офіційного відбою ОВА/ПС."
                )
                alerts.append(AlertEvent(
                    target_id=target.id,
                    level=AlertLevel.INFO,
                    title="Ціль імовірно збита / втрачена",
                    body=body,
                    is_silent=True,
                ))

        elif parsed.action == ActionType.DIVERTED:
            target.status = TargetStatus.DIVERTED
            body = (
                f"↗️ Ціль у районі {point_name} змінила курс і відвернула з коридору на Франківськ. "
                f"Слідкуйте за офіційними повідомленнями."
            )
            alerts.append(AlertEvent(
                target_id=target.id,
                level=AlertLevel.INFO,
                title="Ціль змінила курс",
                body=body,
                is_silent=True,
            ))

        return alerts

    def _build_alert(
        self,
        target: Target,
        point,
        window_str: str,
        is_update: bool = False,
    ) -> AlertEvent:
        point_display = point.names[0].capitalize() if point.names else target.last_point_id
        count_prefix = f"[{target.target_count}х] " if target.target_count > 1 else ""

        disclaimer = (
            "\n\n⚠️ *Час підльоту є виключно довідковим розрахунком і може змінюватися "
            "через маневри або зміну курсу. Не орієнтуйтеся на нього як на істину — переходьте в безпечні місця!*"
        )

        if target.level == AlertLevel.LEVEL_1:
            title = f"🟡 {count_prefix}Ціль на коридорі ({point_display})"
            body = (
                f"Ціль зафіксовано на підходах до коридору ({point_display}). "
                f"Можливий рух у бік Франківщини.\n"
                f"Розрахункове вікно біля міста: {window_str}."
                f"{disclaimer}"
            )
            is_silent = True

        elif target.level == AlertLevel.LEVEL_2:
            title = f"🟠 {count_prefix}Ціль наближається ({point_display})"
            body = (
                f"Ціль прямує в бік Франківщини ({point_display})!\n"
                f"Очікується біля міста: {window_str}.\n"
                f"Підготуйтеся перейти в безпечне місце."
                f"{disclaimer}"
            )
            is_silent = False

        elif target.level == AlertLevel.LEVEL_3A:
            title = f"🔴 {count_prefix}УВАГА! Підліт до міста ({point_display})"
            body = (
                f"Ціль підлітає до Івано-Франківська через {point_display}!\n"
                f"Час до міста: {window_str}.\n"
                f"Негайно перебувайте в укриттях!"
                f"{disclaimer}"
            )
            is_silent = False

        else:  # LEVEL_3B
            title = f"🔴 {count_prefix}ЦІЛЬ БІЛЯ / НАД МІСТОМ ({point_display})"
            body = (
                f"Ціль безпосередньо біля або над містом зараз ({point_display})!\n"
                f"Негайно відійдіть від вікон та перебувайте в безпечних місцях!"
                f"{disclaimer}"
            )
            is_silent = False


        return AlertEvent(
            target_id=target.id,
            level=target.level,
            title=title,
            body=body,
            is_silent=is_silent,
            eta_window_str=window_str,
            is_update=is_update,
        )

    @staticmethod
    def _level_rank(level: AlertLevel) -> int:
        ranks = {
            AlertLevel.LEVEL_1: 1,
            AlertLevel.LEVEL_2: 2,
            AlertLevel.LEVEL_3A: 3,
            AlertLevel.LEVEL_3B: 4,
            AlertLevel.INFO: 0,
        }
        return ranks.get(level, 0)

    def _cleanup_expired(self, now: datetime) -> None:
        """Переводить у застарілі цілі, за якими немає оновлень понад 45 хв."""
        for target in list(self._active_targets.values()):
            if target.status == TargetStatus.ACTIVE and (now - target.last_seen_utc) > timedelta(minutes=45):
                target.status = TargetStatus.EXPIRED
