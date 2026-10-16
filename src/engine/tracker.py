"""Target lifecycle tracker, spatio-temporal deduplication and alert dispatch."""
from __future__ import annotations

import logging
import re
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

# Geographic stage classification of divert destinations (by distance from Ivano-Frankivsk)
_FAR_NORTH_DEST_RE = re.compile(
    r"(?i)\b(рівне\w*|рівненщин\w*|волин\w*|житомир\w*|дубн\w*|шепетівк\w*|"
    r"кременец\w*|шумськ\w*|ланівц\w*|вишнівец\w*|збараж\w*)\b"
)
_EAST_UPSTREAM_DEST_RE = re.compile(
    r"(?i)\b(шаргород\w*|віньківц\w*|вовковинц\w*|деражн\w*|стару\s+синяву|стара\s+синява)\b"
)
_KHMELNYTSKYI_DEST_RE = re.compile(
    r"(?i)\b(хмельницьк\w*|городок\w*|закупн\w*|смотрич\w*|чемерівц\w*|ярмолинц\w*|"
    r"красилів\w*|волочиськ\w*|сатанів\w*|старокостянтинів\w*|куч[уаі]\b)\b"
)
_TERNOPIL_LVIV_DEST_RE = re.compile(
    r"(?i)\b(гусятин\w*|чортків\w*|копичинц\w*|хоростків\w*|теребовл\w*|микулинц\w*|"
    r"козов\w*|тернопіль\w*|бережан\w*|підгайц\w*|монастириськ\w*|бучач\w*|"
    r"перемишлян\w*|поморян\w*|львів\w*|винник\w*)\b"
)
_LVIV_BORDER_DEST_RE = re.compile(
    r"(?i)\b(перемишлян\w*|поморян\w*|львів\w*|винник\w*|стрий\w*|жидaчів\w*|ходарів\w*|рогатин\w*)\b"
)


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

    @classmethod
    def _is_divert_compatible(
        cls,
        target_dist_km: float | None,
        target_level: AlertLevel,
        is_regional: bool,
        dest: str | None,
    ) -> bool:
        """Check if a divert destination is geographically compatible with the target's corridor position."""
        if target_dist_km is None:
            return True

        dest_norm = normalize(dest) if dest else ""

        # 1. Targets already inside Ivano-Frankivsk oblast (<= 65 km or Level 3A/3B)
        if target_dist_km <= 65.0 or target_level in (AlertLevel.LEVEL_3A, AlertLevel.LEVEL_3B):
            return bool(is_regional and dest_norm and _LVIV_BORDER_DEST_RE.search(dest_norm))

        # 2. Far-north destinations (Rivne, Volyn, Dubno, Shepetivka, Kremenets, Shumsk, Zhytomyr)
        # do not apply to southern-corridor targets without an explicit corridor point on the same line
        if dest_norm and _FAR_NORTH_DEST_RE.search(dest_norm):
            return False

        # 3. Targets in Southern Ternopilska oblast (65 < d < 120 km: Skala-Podilska, Borshchiv, Tovste)
        if target_dist_km < 120.0:
            return not bool(
                dest_norm and (_EAST_UPSTREAM_DEST_RE.search(dest_norm) or _KHMELNYTSKYI_DEST_RE.search(dest_norm))
            )

        # 4. Targets in Southern Khmelnytskyi (120 <= d <= 195 km: Kamianets-Podilskyi, Dunaivtsi, Stara/Nova Ushytsia)
        if target_dist_km <= 195.0:
            # Shargorod is upstream in Vinnytsia (~235 km) and cannot divert a target already at Kamianets/Dunaivtsi
            return not bool(dest_norm and re.search(r"(?i)\bшаргород\w*\b", dest_norm) and target_dist_km < 180.0)

        # 5. Far upstream targets in Vinnytsia (> 195 km: Pishchanka, Vendychany, Murovani Kyrylivtsi)
        return True

    def _find_matching_target(
        self,
        point_id: str,
        posted_at: datetime,
        drone_type: DroneType | None = None,
        is_new_target_marker: bool = False,
        exclude_target_ids: set[str] | None = None,
    ) -> Target | None:
        """Find the active target that the message can belong to."""
        if is_new_target_marker:
            return None

        ref = self._get_ref()
        new_point = ref.points.get(point_id)
        if not new_point:
            return None

        candidate: Target | None = None
        best_score = float("inf")

        for target in self._active_targets.values():
            if target.status != TargetStatus.ACTIVE:
                continue

            if exclude_target_ids and target.id in exclude_target_ids:
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
            # it cannot match distant upstream points (Level 1 > 60 km or distant Level 2).
            if target.level in (AlertLevel.LEVEL_3A, AlertLevel.LEVEL_3B):
                if new_point.level == 1 and (not new_point.distance_km or new_point.distance_km > 60.0):
                    continue
                if last_point.distance_km and new_point.distance_km and (new_point.distance_km - last_point.distance_km) > 40.0:
                    continue

            # Prevent cross-corridor merging between regional points and distant main corridor points
            if (
                getattr(last_point, "is_regional", False) != getattr(new_point, "is_regional", False)
                and last_point.distance_km
                and new_point.distance_km
                and abs(last_point.distance_km - new_point.distance_km) > 45.0
            ):
                continue

            time_diff_min = time_diff.total_seconds() / 60.0

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

            # 3. Target advancing forward towards the protected city (bounded by physically plausible jump distance)
            is_forward = False
            if last_point.distance_km and new_point.distance_km:
                forward_jump_km = last_point.distance_km - new_point.distance_km
                max_forward_km = 50.0 + time_diff_min * 10.0
                is_forward = (-10.0 <= forward_jump_km <= max_forward_km)
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

            if is_in_sequence or is_same_point or is_forward or is_lagged_upstream:
                dist_gap = (
                    abs(last_point.distance_km - new_point.distance_km)
                    if (last_point.distance_km and new_point.distance_km)
                    else 0.0
                )
                score = dist_gap + time_diff_min * 4.0
                if score < best_score:
                    best_score = score
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

        # 3. Process the points in the message, grouped by line so multi-drone digests stay separate
        # while multi-point single lines (e.g. "повз Муровані Курилівці в бік Дунаївців") advance a single target.
        groups = parsed.point_groups if parsed.point_groups else [[pid] for pid in parsed.points]
        targets_matched_in_post: set[str] = set()
        pending_alerts_by_target: dict[str, AlertEvent] = {}

        for group in groups:
            line_target: Target | None = None
            for point_id in group:
                point = ref.points.get(point_id)
                if not point:
                    continue

                if line_target is not None:
                    target = line_target
                else:
                    target = self._find_matching_target(
                        point_id,
                        post.posted_at_utc,
                        drone_type=parsed.drone_type,
                        is_new_target_marker=parsed.is_new_target_marker,
                        exclude_target_ids=targets_matched_in_post,
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
                        level=determine_level(point_id, point.level, is_regional=getattr(point, "is_regional", False)),
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

                    # Keep target.last_point_id monotonic (closest to protected city);
                    # do not overwrite a main-corridor Level 2/3 target with a regional point
                    curr_last_pt = ref.points.get(target.last_point_id)
                    is_regional_on_main_track = (
                        bool(getattr(point, "is_regional", False))
                        and target.level in (AlertLevel.LEVEL_2, AlertLevel.LEVEL_3A, AlertLevel.LEVEL_3B)
                    )
                    if not is_regional_on_main_track:
                        if curr_last_pt and curr_last_pt.distance_km and point.distance_km:
                            if point.distance_km < curr_last_pt.distance_km:
                                target.last_point_id = point_id
                                is_advance = True
                        else:
                            new_pt_level = determine_level(point_id, point.level, is_regional=getattr(point, "is_regional", False))
                            if self._level_rank(new_pt_level) > self._level_rank(target.level):
                                target.last_point_id = point_id
                                is_advance = True

                    target.last_seen_utc = max(target.last_seen_utc, post.posted_at_utc)

                    # Update the number of targets: if the new post states the number explicitly, use it as the most current
                    if parsed.explicit_count:
                        target.target_count = parsed.count

                    target.channels.add(post.channel)
                    if (post.channel, post.message_id) not in target.message_ids:
                        target.message_ids.append((post.channel, post.message_id))

                    # Escalate the level (never downgrade a LEVEL_2/3A/3B target to REGIONAL)
                    new_level = determine_level(point_id, point.level, is_regional=getattr(point, "is_regional", False))
                    if self._level_rank(new_level) > self._level_rank(target.level):
                        target.level = new_level

                line_target = target
                targets_matched_in_post.add(target.id)

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

                # If the post is too stale (e.g. from a Telethon backlog replay after reconnect/restart,
                # or an edit to an old message), do not broadcast a new alert to subscribers.
                post_age_min = max(0.0, (now - post.posted_at_utc).total_seconds() / 60.0)
                max_age_min = 7.0 if target.drone_type == DroneType.JET else 15.0
                is_stale_post = post_age_min > max_age_min or (post.is_edit and post_age_min > 5.0)

                if is_stale_post and should_alert:
                    logger.warning(
                        "Пропуск запізнілого сповіщення: пост %s/%s опубліковано %.1f хв тому (поріг %.1f хв)",
                        post.channel,
                        post.message_id,
                        post_age_min,
                        max_age_min,
                    )
                    should_alert = False
                    is_update = True

                if should_alert or is_update:
                    prev_alert = pending_alerts_by_target.get(target.id)
                    effective_is_update = is_update and (prev_alert is None or prev_alert.is_update)
                    alert = self._build_alert(target, calc_point, w_str, post=post, is_update=effective_is_update)
                    pending_alerts_by_target[target.id] = alert
                    if should_alert:
                        self._target_last_alerted_level[target.id] = target.level

        alerts.extend(pending_alerts_by_target.values())
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
        target: Target | None = None
        matched_dest: str | None = parsed.divert_destination
        ref = self._get_ref()

        # 1. If an explicit action_point_id was mentioned (e.g. "повз Дунаївці курсом на Городок", "біля Тлумача збито"),
        # the target MUST be near that point. Never fall back to a distant target if no target is near action_point_id.
        if parsed.action_point_id:
            p_act = ref.points.get(parsed.action_point_id)
            candidates = [t for t in self.get_active_targets() if t.drone_type == parsed.drone_type]
            if not candidates:
                candidates = self.get_active_targets()

            best_dist = float("inf")
            for cand in candidates:
                time_diff = abs(post.posted_at_utc - cand.last_seen_utc)
                if time_diff > self._max_gap:
                    continue
                if cand.last_point_id == parsed.action_point_id or parsed.action_point_id in cand.points_sequence:
                    dist = 0.0
                else:
                    p_cand = ref.points.get(cand.last_point_id)
                    if p_act and p_cand and p_act.distance_km and p_cand.distance_km:
                        dist = abs(p_act.distance_km - p_cand.distance_km)
                    else:
                        dist = 999.0
                if dist <= 50.0 and dist < best_dist:
                    best_dist = dist
                    target = cand

            if target is None:
                return alerts

        # 2. If no action_point_id was mentioned, match against active targets with stage/time validation
        elif self._active_targets:
            active_same_type = [t for t in self.get_active_targets() if t.drone_type == parsed.drone_type]
            active_all = active_same_type if active_same_type else self.get_active_targets()
            active_sorted = sorted(active_all, key=lambda t: t.last_seen_utc, reverse=True)

            if parsed.action == ActionType.DIVERTED:
                dest_candidates = parsed.divert_destinations if parsed.divert_destinations else (
                    [parsed.divert_destination] if parsed.divert_destination else [None]
                )
                for cand in active_sorted:
                    time_diff = abs(post.posted_at_utc - cand.last_seen_utc)
                    if time_diff > self._max_gap:
                        continue
                    p_cand = ref.points.get(cand.last_point_id)
                    cand_dist = p_cand.distance_km if p_cand else None
                    cand_reg = bool(getattr(p_cand, "is_regional", False)) if p_cand else False
                    for d in dest_candidates:
                        if self._is_divert_compatible(cand_dist, cand.level, cand_reg, d):
                            target = cand
                            matched_dest = d
                            break
                    if target is not None:
                        break
            else:
                # For generic DOWNED/LOST without a settlement name, only match a recently updated target (<= 15 min)
                for cand in active_sorted:
                    time_diff = abs(post.posted_at_utc - cand.last_seen_utc)
                    if time_diff <= timedelta(minutes=15):
                        target = cand
                        break

        if target is None:
            return alerts

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
                    f"⚪️ За повідомленнями каналів, одну з цілей імовірно збито або втрачено. "
                    f"Ще {target.target_count} ціль(і) може бути в повітрі. "
                    f"Залишайтеся в укриттях до офіційного відбою."
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
                    "⚪️ За повідомленнями каналів, ціль, яку ми відстежували, імовірно, уражена або втрачена. "
                    "Це не скасування тривоги: залишайтеся в укриттях до офіційного відбою ОВА/ПС."
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
            dest_str = f" на {matched_dest}" if matched_dest else ""
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

        if target.level == AlertLevel.REGIONAL or getattr(point, "is_regional", False):
            name_gen = getattr(point, "name_gen", "") or point_display
            dist = point.distance_km or 0.0
            title = f"ℹ️ {count_prefix}Рух у напрямку {name_gen} (~{dist:.0f} км від Франківська). Ймовірна тривога в області"
            body = (
                f"⚠️ Зафіксовано рух БпЛА{type_suffix} у районі / напрямку н.п. **{point_display}** "
                f"(~{dist:.0f} км від Івано-Франківська).\n\n"
                f"🚨 **Найімовірніше, в області буде оголошено тривогу.**\n"
                f"Прямого курсу на Івано-Франківськ наразі немає, але загроза для області реальна. "
                f"Слідкуйте за офіційними повідомленнями та будьте готові перейти в укриття."
            )
            is_silent = (dist > 30.0)

        elif target.level in (AlertLevel.LEVEL_1, AlertLevel.LEVEL_2, AlertLevel.LEVEL_3A):
            if target.eta_min_minutes is not None and target.eta_max_minutes is not None:
                eta_duration = f" (≈{target.eta_min_minutes:.0f}–{target.eta_max_minutes:.0f} хв)"
            else:
                eta_duration = ""
            body = f"⏱ Очікується біля міста: **{window_str}**{eta_duration}"

            if target.level == AlertLevel.LEVEL_1:
                title = f"🟡 {count_prefix}Потенційна загроза{type_suffix}: {point_display}"
                is_silent = True
            elif target.level == AlertLevel.LEVEL_2:
                title = f"🟠 {count_prefix}Наближення{type_suffix}: {point_display}"
                is_silent = False
            else:  # LEVEL_3A
                title = f"🔴 {count_prefix}Небезпека (підліт){type_suffix}: {point_display}"
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
            AlertLevel.REGIONAL: 1,
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

            # Case 0: Regional target whose ETA window expired without escalating
            if (
                target.level == AlertLevel.REGIONAL
                and target.eta_window_end_utc
                and now > (target.eta_window_end_utc + timedelta(minutes=10))
            ):
                target.status = TargetStatus.LOST
                if self._store:
                    try:
                        self._store.label_target(
                            target.id,
                            OUTCOME_UNKNOWN,
                            notes="Regional target expired without approaching IF",
                        )
                    except Exception as exc:  # noqa: BLE001
                        logger.warning("Не вдалося автоматично розмітити ціль %s: %s", target.id, exc)
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
                    "⚪️ Нових повідомлень про цю ціль немає: вона могла змінити курс, бути втраченою локаційно або знищеною. "
                    "Це не означає, що загрози немає — слідкуйте за офіційними повідомленнями."
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
                    "Імовірно, змінила курс або втрачена, але це не означає, що загрози немає. "
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
