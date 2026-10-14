"""Target lifecycle tracker, spatio-temporal deduplication and alert dispatch."""
from __future__ import annotations

import logging
import uuid
from collections.abc import Callable
from datetime import UTC, datetime, timedelta

from src.engine.eta import calculate_eta, determine_level
from src.engine.models import (
    ActionType,
    AlertEvent,
    AlertLevel,
    DroneType,
    ParsedMessage,
    RawPost,
    Target,
    TargetStatus,
)
from src.learning.reference import ActiveReferenceCache, ReferenceData
from src.learning.store import OUTCOME_UNKNOWN, LearningStore
from src.learning.text import format_place_title, normalize

logger = logging.getLogger(__name__)


class TargetTracker:
    """Manage the active target hypotheses, the deduplication between channels and the creation of alerts."""

    def __init__(
        self,
        ref_cache: ActiveReferenceCache,
        store: LearningStore | None = None,
        dedup_window_minutes: float = 6.0,
        max_corridor_gap_minutes: float = 35.0,
        is_siren_active_fn: Callable[[], bool] | None = None,
    ):
        self._ref_cache = ref_cache
        self._store = store
        self._dedup_window = timedelta(minutes=dedup_window_minutes)
        self._max_gap = timedelta(minutes=max_corridor_gap_minutes)
        self._is_siren_active_fn = is_siren_active_fn or (lambda: False)
        self._active_targets: dict[str, Target] = {}
        # Saved earlier alerts that prevent spam: target_id -> last_level
        self._target_last_alerted_level: dict[str, AlertLevel] = {}
        # Points that already caused an alert: target_id -> set(point_ids)
        self._target_alerted_points: dict[str, set[str]] = {}


    def get_active_targets(self) -> list[Target]:
        return [t for t in self._active_targets.values() if t.status == TargetStatus.ACTIVE]

    def _get_ref(self) -> ReferenceData:
        return self._ref_cache.get()

    def _find_matching_target(
        self,
        point_id: str,
        posted_at: datetime,
        drone_type: DroneType | None = None,
        is_new_target_marker: bool = False,
    ) -> Target | None:
        """Find the active target that the message can belong to."""
        if is_new_target_marker:
            return None

        ref = self._get_ref()
        new_point = ref.points.get(point_id)
        if not new_point:
            return None

        candidate: Target | None = None
        min_time_diff = timedelta.max

        for target in self._active_targets.values():
            if target.status != TargetStatus.ACTIVE:
                continue

            # Strict isolation by drone speed category / type
            if drone_type is not None and target.drone_type != drone_type:
                continue

            time_diff = abs(posted_at - target.last_seen_utc)
            if time_diff > self._max_gap:
                continue

            last_point = ref.points.get(target.last_point_id)
            if not last_point:
                continue

            # If target has already reached Level 3 (immediate approach or over city),
            # it cannot match distant upstream points (Level 1 or distant Level 2).
            if target.level in (AlertLevel.LEVEL_3A, AlertLevel.LEVEL_3B):
                if new_point.level == 1:
                    continue
                if last_point.distance_km and new_point.distance_km and (new_point.distance_km - last_point.distance_km) > 40.0:
                    continue

            # Check if this point matches the target track:
            # 1. Point already visited in this target's sequence (delayed report from another channel)
            dist_back = (new_point.distance_km - last_point.distance_km) if (last_point.distance_km and new_point.distance_km) else 0.0
            is_in_sequence = (
                point_id in target.points_sequence
                and dist_back <= 40.0
                and time_diff <= timedelta(minutes=8)
            )

            # 2. Same point as the current last_point_id inside dedup window
            is_same_point = (point_id == target.last_point_id) and (time_diff <= self._dedup_window)

            # 3. Target advancing forward towards the protected city
            is_forward = False
            if last_point.distance_km and new_point.distance_km:
                is_forward = (new_point.distance_km <= last_point.distance_km + 10.0)
            else:
                is_forward = (new_point.level >= last_point.level)

            # 4. Upstream point reported with latency from a slower channel
            # (e.g. kpszsu reporting an upstream point a few minutes after monitoring reported downstream)
            is_lagged_upstream = False
            if last_point.distance_km and new_point.distance_km:
                dist_diff = new_point.distance_km - last_point.distance_km
                if 0.0 < dist_diff <= 70.0 and time_diff <= timedelta(minutes=8):
                    is_lagged_upstream = True
            elif new_point.level == last_point.level and time_diff <= timedelta(minutes=10):
                is_lagged_upstream = True

            if (is_in_sequence or is_same_point or is_forward or is_lagged_upstream) and time_diff < min_time_diff:
                min_time_diff = time_diff
                candidate = target

        return candidate

    def process_message(
        self,
        post: RawPost,
        parsed: ParsedMessage,
        now: datetime | None = None,
    ) -> list[AlertEvent]:
        """Process a parsed post, update the target state and create alerts."""
        now = now or datetime.now(UTC)
        ref = self._get_ref()
        alerts: list[AlertEvent] = []

        # 1. Write to LearningStore for analytics and retraining
        if self._store and (parsed.is_jet or parsed.points):
            self._store.add_post(
                channel=post.channel,
                message_id=post.message_id,
                posted_at=post.posted_at_utc,
                text=post.text,
                is_target_related=True,
            )

        # 2. Process actions (shot down / lost / turned away)
        if parsed.action in (ActionType.DOWNED, ActionType.LOST, ActionType.DIVERTED):
            action_alerts = self._handle_action(post, parsed, now)
            alerts.extend(action_alerts)
            alerts.extend(self.check_timeouts(now))
            return alerts

        # If the post has no point mentions, stop here
        if not parsed.points:
            alerts.extend(self.check_timeouts(now))
            return alerts

        # 3. Process the points in the message
        for point_id in parsed.points:
            point = ref.points.get(point_id)
            if not point:
                continue

            # Find a matching existing target or create a new one
            target = self._find_matching_target(
                point_id,
                post.posted_at_utc,
                drone_type=parsed.drone_type,
                is_new_target_marker=parsed.is_new_target_marker,
            )
            is_new_target = False
            is_advance = False

            if target is None:
                # Create a new target
                is_new_target = True
                is_advance = True
                target_id = f"T-{post.posted_at_utc.strftime('%Y%m%d%H%M')}-{uuid.uuid4().hex[:4]}"
                target = Target(
                    id=target_id,
                    points_sequence=[point_id],
                    last_point_id=point_id,
                    drone_type=parsed.drone_type,
                    target_type=parsed.target_type,
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
                # Update the existing target
                if point_id not in target.points_sequence:
                    target.points_sequence.append(point_id)

                if parsed.target_type:
                    target.target_type = parsed.target_type

                # Keep target.last_point_id monotonic (closest to protected city)
                curr_last_pt = ref.points.get(target.last_point_id)
                if curr_last_pt and curr_last_pt.distance_km and point.distance_km:
                    if point.distance_km < curr_last_pt.distance_km:
                        target.last_point_id = point_id
                        is_advance = True
                else:
                    new_pt_level = determine_level(point_id, point.level)
                    if self._level_rank(new_pt_level) > self._level_rank(target.level):
                        target.last_point_id = point_id
                        is_advance = True

                target.last_seen_utc = max(target.last_seen_utc, post.posted_at_utc)

                # Update the number of targets: if the new post states the number explicitly, use it as the most current
                if parsed.explicit_count:
                    target.target_count = parsed.count

                target.channels.add(post.channel)
                target.message_ids.append((post.channel, post.message_id))

                # Escalate the level (the level can only go up)
                new_level = determine_level(point_id, point.level)
                if self._level_rank(new_level) > self._level_rank(target.level):
                    target.level = new_level

            # Record the mention in the learning store
            if self._store:
                self._store.add_mention(
                    target_id=target.id,
                    point_id=point_id,
                    channel=post.channel,
                    posted_at=post.posted_at_utc,
                    message_id=post.message_id,
                )

            # Calculate the ETA using the furthest forward point on the track
            calc_point = ref.points.get(target.last_point_id, point)
            rem_min, rem_max, w_start, w_end, w_str = calculate_eta(
                point=calc_point,
                ref=ref,
                post_time_utc=post.posted_at_utc,
                current_time_utc=now,
                drone_type=target.drone_type,
            )
            target.eta_min_minutes = rem_min
            target.eta_max_minutes = rem_max
            target.eta_window_start_utc = w_start
            target.eta_window_end_utc = w_end

            # Confidence
            if len(target.channels) >= 2 or "на іф" in normalize(post.text) or "франківськ" in normalize(post.text):
                target.confidence = "high"
            elif len(target.points_sequence) >= 2:
                target.confidence = "medium"
            else:
                target.confidence = "low"

            # 4. Build the alert
            # Only trigger a new alert if the target advanced to a new point, escalated level, or is brand new.
            # Lagged/repeated upstream posts are treated as non-alert updates (is_update=True).
            alerted_points = self._target_alerted_points.setdefault(target.id, set())
            is_new_point_for_target = point_id not in alerted_points
            last_alerted = self._target_last_alerted_level.get(target.id)
            should_alert = False
            is_update = False

            level_escalated = (last_alerted is not None and self._level_rank(target.level) > self._level_rank(last_alerted))
            point_advanced = (is_advance and is_new_point_for_target)

            if is_new_target or last_alerted is None or level_escalated or point_advanced:
                should_alert = True
                alerted_points.add(point_id)
            else:
                is_update = True

            if should_alert or is_update:
                alert = self._build_alert(target, calc_point, w_str, post=post, is_update=is_update)
                alerts.append(alert)
                if should_alert:
                    self._target_last_alerted_level[target.id] = target.level



        alerts.extend(self.check_timeouts(now))
        return alerts

    def _handle_action(
        self,
        post: RawPost,
        parsed: ParsedMessage,
        now: datetime,
    ) -> list[AlertEvent]:
        """Safe asymmetric handling of shoot-down and turn-away (rule F7)."""
        alerts: list[AlertEvent] = []
        target = None
        ref = self._get_ref()

        # Find the target by point, or the nearest active target
        if parsed.action_point_id:
            target = self._find_matching_target(
                parsed.action_point_id,
                post.posted_at_utc,
                drone_type=parsed.drone_type,
            )
            if target is None:
                target = self._find_matching_target(parsed.action_point_id, post.posted_at_utc)

        if target is None and self._active_targets:
            # Take the most recent active target of the same drone type, fallback to any active
            active = [t for t in self.get_active_targets() if t.drone_type == parsed.drone_type]
            if not active:
                active = self.get_active_targets()
            if active:
                candidate = max(active, key=lambda t: t.last_seen_utc)
                # If this is a DIVERTED action:
                if parsed.action == ActionType.DIVERTED:
                    # If an action_point_id was explicitly stated, candidate must be near that point
                    if parsed.action_point_id and candidate.last_point_id:
                        p_act = ref.points.get(parsed.action_point_id)
                        p_cand = ref.points.get(candidate.last_point_id)
                        if p_act and p_cand and p_act.distance_km and p_cand.distance_km and abs(p_act.distance_km - p_cand.distance_km) > 50.0:
                            return alerts
                    time_diff = abs(post.posted_at_utc - candidate.last_seen_utc)
                    # Gap between the last seen point and divert mention must be within plausible track window
                    if time_diff <= self._max_gap:
                        target = candidate
                else:
                    target = candidate

        if target is None:
            return alerts

        ref = self._get_ref()
        point_name = format_place_title(target.last_point_id)
        if target.last_point_id in ref.points and ref.points[target.last_point_id].names:
            point_name = format_place_title(ref.points[target.last_point_id].names[0])


        source_links: list[tuple[str, str]] = []
        if post and post.channel and post.message_id:
            source_links.append((post.channel, f"https://t.me/{post.channel}/{post.message_id}"))

        if parsed.action in (ActionType.DOWNED, ActionType.LOST):
            if target.target_count > 1:
                # Decrease the target counter in the group
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
                    source_links=source_links,
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
                    source_links=source_links,
                ))

        elif parsed.action == ActionType.DIVERTED:
            target.status = TargetStatus.DIVERTED
            dest_str = f" на {parsed.divert_destination}" if parsed.divert_destination else ""
            if self._is_siren_active_fn():
                body = (
                    f"↗️ Ціль у районі {point_name} відвернула{dest_str}. "
                    f"Залишайтеся в укриттях до оголошення відбою."
                )
            else:
                body = (
                    f"↗️ Ціль у районі {point_name} відвернула{dest_str}. "
                    "Слідкуйте за офіційними повідомленнями і не ігноруйте сигнали "
                    "повітряної тривоги, якщо її все ж буде оголошено."
                )
            alerts.append(AlertEvent(
                target_id=target.id,
                level=AlertLevel.INFO,
                title="Ціль змінила курс",
                body=body,
                is_silent=True,
                source_links=source_links,
            ))

        return alerts

    def _build_alert(
        self,
        target: Target,
        point,
        window_str: str,
        post: RawPost | None = None,
        is_update: bool = False,
    ) -> AlertEvent:
        point_display = format_place_title(point.names[0]) if point.names else format_place_title(target.last_point_id)
        count_prefix = f"{target.target_count}х " if target.target_count > 1 else ""

        # Collect the links to the original sources
        source_links: list[tuple[str, str]] = []
        if post and post.channel and post.message_id:
            source_links.append((post.channel, f"https://t.me/{post.channel}/{post.message_id}"))
        elif target.message_ids:
            for ch, mid in target.message_ids[-2:]:
                source_links.append((ch, f"https://t.me/{ch}/{mid}"))

        if target.drone_type == DroneType.CONVENTIONAL:
            type_suffix = " (Шахед)"
        elif target.target_type:
            type_suffix = f" ({target.target_type})"
        else:
            type_suffix = " (реактивний)"

        if target.level == AlertLevel.LEVEL_1:
            title = f"🟡 {count_prefix}Потенційна загроза{type_suffix}: {point_display}"
            body = f"⏱ Очікується біля міста: **{window_str}**"
            is_silent = True

        elif target.level == AlertLevel.LEVEL_2:
            title = f"🟠 {count_prefix}Наближення{type_suffix}: {point_display}"
            body = f"⏱ Очікується біля міста: **{window_str}**"
            is_silent = False

        elif target.level == AlertLevel.LEVEL_3A:
            title = f"🔴 {count_prefix}Небезпека (підліт){type_suffix}: {point_display}"
            body = f"⏱ Очікується біля міста: **{window_str}**"
            is_silent = False

        else:  # LEVEL_3B
            title = f"🔴 {count_prefix}Небезпека біля / над містом{type_suffix}: {point_display}"
            body = "❗️ Ціль безпосередньо біля або над містом зараз! Перебувайте в безпечних місцях."
            is_silent = False

        return AlertEvent(
            target_id=target.id,
            level=target.level,
            title=title,
            body=body,
            is_silent=is_silent,
            eta_window_str=window_str,
            is_update=is_update,
            source_links=source_links,
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

    def check_timeouts(self, now: datetime | None = None) -> list[AlertEvent]:
        """Check for expired targets and transition them to lost or expired state."""
        now = now or datetime.now(UTC)
        alerts: list[AlertEvent] = []

        for target in list(self._active_targets.values()):
            if target.status != TargetStatus.ACTIVE:
                continue

            # Case 1: Level 1 target whose ETA window expired without reaching Level 2
            if (
                target.level == AlertLevel.LEVEL_1
                and target.eta_window_end_utc
                and now > target.eta_window_end_utc
            ):
                target.status = TargetStatus.LOST
                if self._store:
                    try:
                        self._store.label_target(
                            target.id,
                            OUTCOME_UNKNOWN,
                            notes="ETA expired at Level 1 without reaching Level 2",
                        )
                    except Exception as exc:  # noqa: BLE001
                        logger.warning("Не вдалося автоматично розмітити ціль %s: %s", target.id, exc)

                source_links: list[tuple[str, str]] = []
                if target.message_ids:
                    for ch, mid in target.message_ids[-2:]:
                        source_links.append((ch, f"https://t.me/{ch}/{mid}"))

                body = (
                    "⚪️ Нових повідомлень про рух цілі немає. "
                    "Можливо локаційно втрачена або змінила курс. "
                    "Слідкуйте за подальшими повідомленнями."
                )
                alerts.append(AlertEvent(
                    target_id=target.id,
                    level=AlertLevel.INFO,
                    title="Ціль локаційно втрачена",
                    body=body,
                    is_silent=True,
                    source_links=source_links,
                ))
                continue

            # Case 2: Level 2 target whose ETA window expired without reaching Level 3
            if (
                target.level == AlertLevel.LEVEL_2
                and target.eta_window_end_utc
                and now > (target.eta_window_end_utc + timedelta(minutes=4))
            ):
                target.status = TargetStatus.LOST
                if self._store:
                    try:
                        self._store.label_target(
                            target.id,
                            OUTCOME_UNKNOWN,
                            notes="ETA expired at Level 2 without reaching Level 3",
                        )
                    except Exception as exc:  # noqa: BLE001
                        logger.warning("Не вдалося автоматично розмітити ціль %s: %s", target.id, exc)

                source_links: list[tuple[str, str]] = []
                if target.message_ids:
                    for ch, mid in target.message_ids[-2:]:
                        source_links.append((ch, f"https://t.me/{ch}/{mid}"))

                body = (
                    "⚪️ Нових повідомлень про рух цілі в напрямку міста немає (час очікування минув). "
                    "Імовірно, змінила курс або втрачена. "
                    "Слідкуйте за офіційними повідомленнями."
                )
                alerts.append(AlertEvent(
                    target_id=target.id,
                    level=AlertLevel.INFO,
                    title="Ціль змінила курс / втрачена",
                    body=body,
                    is_silent=True,
                    source_links=source_links,
                ))
                continue

            # Case 3: Level 3A target whose ETA window expired without reaching Level 3B
            if (
                target.level == AlertLevel.LEVEL_3A
                and target.eta_window_end_utc
                and now > (target.eta_window_end_utc + timedelta(minutes=5))
            ):
                target.status = TargetStatus.LOST
                if self._store:
                    try:
                        self._store.label_target(
                            target.id,
                            OUTCOME_UNKNOWN,
                            notes="ETA expired at Level 3A without reaching Level 3B",
                        )
                    except Exception as exc:  # noqa: BLE001
                        logger.warning("Не вдалося автоматично розмітити ціль %s: %s", target.id, exc)
                continue

            # Case 4: Level 3B target whose ETA window expired (drone is directly at or over the city).
            # 4 minutes after ETA window end, the drone has already reached or been engaged.
            if (
                target.level == AlertLevel.LEVEL_3B
                and target.eta_window_end_utc
                and now > (target.eta_window_end_utc + timedelta(minutes=4))
            ):
                target.status = TargetStatus.EXPIRED
                continue

            # Case 5: General cleanup for targets older than 45 minutes
            if (now - target.last_seen_utc) > timedelta(minutes=45):
                target.status = TargetStatus.EXPIRED

        return alerts
