"""Normalization and parsing of the message text from the monitoring channels."""
from __future__ import annotations

import re
from datetime import datetime, timedelta

from src.engine.models import ActionType, DroneType, ParsedMessage, RawPost
from src.learning.reference import ActiveReferenceCache, ReferenceData
from src.learning.text import format_place_title, normalize

# Patterns for the number of targets
_COUNT_PATTERNS = [
    (re.compile(r"(?i)\b(\d+)\s*(?:х|x)\b"), lambda m: int(m.group(1))),
    (re.compile(r"(?i)\b(\d+)\s+(?:реактивн|бпла|шахед|геран)"), lambda m: int(m.group(1))),
    (re.compile(r"(?i)\bдві\s+реактивн"), lambda _: 2),
    (re.compile(r"(?i)\bпара\s+реактивн"), lambda _: 2),
    (re.compile(r"(?i)\bтри\s+реактивн"), lambda _: 3),
    (re.compile(r"(?i)\bодин\s+реактивн"), lambda _: 1),
    (re.compile(r"(?i)\bодна\s+реактивн"), lambda _: 1),
]

# Patterns for conventional / propeller drones
_CONVENTIONAL_KW_RE = re.compile(
    r"(?i)\b(шахед-136|shahed-136|геран-2|герань-2|мопед\w*|поршнев\w*|гвинтов\w*|звичайні?\s+шахед\w*|звичайні?\s+бпла)\b"
)
_GENERAL_DRONE_RE = re.compile(
    r"(?i)\b(шахед\w*|геран\w*|бпла)\b"
)

# Patterns for actions
_DOWNED_RE = re.compile(r"(?i)\b(збит[оаіи]|збили|уражен[оа]|мінус\s+ціль|знищен[оа])\b")
_EXPLOSION_RE = re.compile(r"(?i)\b(вибух|вибухи|гучно|чутно\s+вибух)\b")
_LOST_RE = re.compile(r"(?i)\b(локаційно\s+втрачен[оа]|втрачено\s+з\s+радарів)\b")
_DIVERTED_RE = re.compile(r"(?i)\b(на\s+рівне|на\s+рівненщину|на\s+волинь|на\s+житомир|на\s+житомирщину|на\s+північ|відвернув|відвернул[аои]|відвертає|змінил[аои]?\s+курс)\b")
_CLEAR_RE = re.compile(r"(?i)\b(відбій|чисто|дорозвідка)\b")
_ANOTHER_TARGET_RE = re.compile(
    r"(?i)\b(ще\s+один|ще\s+одна|ще\s+1|ще\s+дв[іа]|інша\s+ціль|інший\s+шахед|інша\s+герань|нова\s+ціль|новий\s+шахед|другий\s+реактивн|друга\s+ціль)\b"
)


# Distant regions that do not belong to the western approach corridor
_DISTANT_REGION_RE = re.compile(
    r"(?i)\b(брянськ|брянщин|чернігів|сумщин|сумськ|харків|полтав|дніпропетров|запоріж|"
    r"херсон|миколаїв|одещин|одеськ|кіровоград|черкащин|черкаськ|донецьк|луганськ|крим|"
    r"київщин|київськ|вишгород|бровар|дарниц|дврз|чорнобил|славутич|житомир|бердичів)\w*"
)

# Approach / western regions relevant to the corridor
_CORRIDOR_REGION_RE = re.compile(
    r"(?i)\b(вінниччин|вінницьк|хмельниччин|хмельницьк|тернопіль|франків|прикарпат|буковин|чернівецьк|львів|рівнен|волин)\w*"
)


class MessageNormalizer:
    """Message analyzer that can inherit the target type from the context."""

    def __init__(self, ref_cache: ActiveReferenceCache, context_window_minutes: float = 20.0):
        self._ref_cache = ref_cache
        self._context_window = timedelta(minutes=context_window_minutes)
        # Last time of a jet target mention for each channel: channel -> datetime
        self._channel_jet_context: dict[str, datetime] = {}
        self._last_any_jet_time: datetime | None = None

    def _get_ref(self) -> ReferenceData:
        return self._ref_cache.get()

    @classmethod
    def _normalize_divert_dest(cls, dest: str) -> str:
        """Ensure destination is capitalized and inflected into accusative case if needed."""
        dest = dest.strip()
        if not dest:
            return ""
        # Handle locative region endings (-щині, -ччині -> -щину, -ччину)
        if re.search(r"(?i)ччині$", dest):
            dest = re.sub(r"(?i)ччині$", "ччину", dest)
        elif re.search(r"(?i)щині$", dest):
            dest = re.sub(r"(?i)щині$", "щину", dest)
        return format_place_title(dest)

    @classmethod
    def _extract_divert_destination(cls, line_norm: str, cancel_keywords: list[str]) -> str | None:
        """Extract destination or cardinal direction the target diverted to."""
        # 1. Check known cancel/divert keywords from profile that start with "на " or "повз "
        # Sort by length descending to match more specific phrases first (e.g. "на стару синяву" before "на синяву")
        kw_candidates = [
            kw.strip() for kw in cancel_keywords
            if kw.lower().startswith(("на ", "повз "))
        ]
        kw_candidates.sort(key=len, reverse=True)
        for kw in kw_candidates:
            if re.search(rf"(?i)\b{re.escape(kw)}\b", line_norm):
                # Clean prefix "на " or "повз "
                dest = re.sub(r"^(?:на|повз)\s+", "", kw, flags=re.IGNORECASE).strip()
                if dest:
                    return cls._normalize_divert_dest(dest)

        # 2. Check cardinal directions and general targets from regex
        cardinal_match = re.search(
            r"(?i)\b(?:курсом\s+на|напрямком\s+на|у\s+напрямку|в\s+напрямку|на|в\s+бік|у\s+бік)\s+"
            r"(північ\w*|південь\w*|схід\w*|захід\w*|рівненщин\w*|рівне\w*|волин\w*|житомир\w*|хмельницьк\w*|тернопіль\w*|львів\w*)\b",
            line_norm,
        )
        if cardinal_match:
            return cls._normalize_divert_dest(cardinal_match.group(1))

        # 3. Check general pattern "відвернув(-ла/-ли) на <слово>" або "курсом на <слово>"
        general_match = re.search(
            r"(?i)\b(?:відверну\w*|змінил\w*\s+курс|курсом)\s+(?:на|в\s+бік|у\s+бік)\s+([а-яґєії'-]{3,})\b",
            line_norm,
        )
        if general_match:
            dest = general_match.group(1).strip()
            # Ignore stop/generic words
            if dest.lower() not in ("нас", "наш", "напрямок", "інший", "іншу", "іншому"):
                return cls._normalize_divert_dest(dest)

        return None

    @staticmethod
    def _extract_target_type(text: str) -> str | None:
        """Extract explicit target type name if mentioned in the message text."""
        norm = normalize(text)
        # Check patterns from specific to general
        if re.search(r"(?i)\b(шахед-238|shahed-238)\b", norm):
            return "Шахед-238"
        if re.search(r"(?i)\b(геран-3|герань-3)\b", norm):
            return "Герань-3"
        if re.search(r"(?i)\b(реактивн\w*\s+бпла)\b", norm):
            return "реактивний БпЛА"
        if re.search(r"(?i)\b(реактивн\w*\s+шахед\w*)\b", norm):
            return "реактивний шахед"
        if re.search(r"(?i)\b(реактивн\w*\s+геран\w*)\b", norm):
            return "реактивна герань"
        if re.search(r"(?i)\b(джет\w*|jet)\b", norm):
            return "джет"
        if re.search(r"(?i)\b(реактивн\w*)\b", norm):
            return "реактивний"
        if re.search(r"(?i)\b(шахед-136|shahed-136)\b", norm):
            return "Шахед-136"
        if re.search(r"(?i)\b(геран-2|герань-2)\b", norm):
            return "Герань-2"
        if re.search(r"(?i)\b(мопед\w*)\b", norm):
            return "мопед"
        if re.search(r"(?i)\b(бпла|бпЛА)\b", norm):
            return "БпЛА"
        if re.search(r"(?i)\b(шахед\w*)\b", norm):
            return "шахед"
        if re.search(r"(?i)\b(геран\w*)\b", norm):
            return "герань"
        return None

    @classmethod
    def _has_divert_phrase(cls, line_norm: str, cancel_keywords: list[str]) -> bool:
        if _DIVERTED_RE.search(line_norm):
            return True
        return any(
            kw.lower() in line_norm
            for kw in cancel_keywords
            if kw.lower().startswith(("на ", "повз ")) or "відверн" in kw.lower() or "курс" in kw.lower()
        )

    def parse(self, post: RawPost) -> ParsedMessage:
        text = post.text
        norm_text = normalize(text)
        ref = self._get_ref()

        # 1. Check for explicit jet drone keywords
        explicit_jet = any(kw.lower() in norm_text for kw in ref.target_keywords)
        explicit_conventional = bool(_CONVENTIONAL_KW_RE.search(norm_text))

        # 2. Check for context inheritance
        now = post.posted_at_utc
        last_jet_time = self._channel_jet_context.get(post.channel)
        inherited_jet = bool(
            (last_jet_time and (now - last_jet_time) <= self._context_window)
            or (self._last_any_jet_time and (now - self._last_any_jet_time) <= self._context_window)
        )

        # 3. Line-aware analysis for multi-region digests and actions
        alias_index = ref.alias_index()
        sorted_aliases = sorted(alias_index.keys(), key=len, reverse=True)
        alias_patterns = [
            (alias, re.compile(rf"(?i)(?:\b|^){re.escape(alias)}(?:\b|$)"))
            for alias in sorted_aliases
        ]

        points_found: list[str] = []
        divert_detected = False
        divert_point_id: str | None = None
        divert_destination: str | None = None
        downed_detected = False
        lost_detected = False
        explosion_detected = False
        clear_detected = False
        relevant_lines: list[str] = []

        # Track whether the current paragraph block belongs to a distant region header (e.g. "Чернігівщина:")
        in_distant_section = False

        for raw_line in text.splitlines():
            line_strip = raw_line.strip()
            if not line_strip:
                in_distant_section = False
                continue

            line_norm = normalize(line_strip)

            # Check if this line is a section header or mentions a region
            has_distant = bool(_DISTANT_REGION_RE.search(line_norm))
            has_corridor_reg = bool(_CORRIDOR_REGION_RE.search(line_norm))

            if line_strip.endswith(":") and has_distant and not has_corridor_reg:
                in_distant_section = True
                continue
            if line_strip.endswith(":") and has_corridor_reg:
                in_distant_section = False

            # If the current line mentions a distant region (e.g. "в Подільському районі Одещини", "на Одещині", "Чернігівщина:"),
            # ignore it completely. Even if it says "на Вінниччину (Піщанка)", the drone has not entered the corridor yet.
            if in_distant_section or has_distant:
                continue

            # Find corridor points on this specific line
            raw_line_matches: list[tuple[str, str]] = []  # (point_id, matched_alias)
            line_spans: list[tuple[int, int]] = []
            for alias, pat in alias_patterns:
                for m in pat.finditer(line_norm):
                    s, e = m.span()
                    if not any(es <= s < ee or es < e <= ee for es, ee in line_spans):
                        line_spans.append((s, e))
                        pid = alias_index[alias]
                        raw_line_matches.append((pid, alias))

            # If a specific settlement is present on this line, generic raion qualifiers
            # (e.g. "Чернелиця, Коломийський район") are administrative clarifications and should
            # not trigger the raion center as a second point.
            has_specific_settlement = any("район" not in alias for _, alias in raw_line_matches)
            line_points: list[str] = []
            for pid, alias in raw_line_matches:
                if has_specific_settlement and "район" in alias:
                    continue
                if pid not in line_points:
                    line_points.append(pid)

            # If line mentions distant areas without corridor points,
            # it should not trigger actions (divert/downed/lost) for corridor targets
            is_corridor_line = bool(line_points or has_corridor_reg)

            line_has_divert = self._has_divert_phrase(line_norm, ref.cancel_keywords)

            if line_points and line_has_divert:
                # Example: "Реактивний БпЛА курсом на Закупне повз Дунаївці"
                # The drone is passing/leaving the corridor point towards a diversion destination!
                divert_detected = True
                if divert_point_id is None:
                    divert_point_id = line_points[0]
                if divert_destination is None:
                    divert_destination = self._extract_divert_destination(line_norm, ref.cancel_keywords)
            elif line_points:
                for pid in line_points:
                    if pid not in points_found:
                        points_found.append(pid)
                relevant_lines.append(line_strip)
            elif line_has_divert and (is_corridor_line or not has_distant):
                divert_detected = True
                if divert_destination is None:
                    divert_destination = self._extract_divert_destination(line_norm, ref.cancel_keywords)

            if _DOWNED_RE.search(line_norm) and (is_corridor_line or not has_distant):
                downed_detected = True
            elif _LOST_RE.search(line_norm) and (is_corridor_line or not has_distant):
                lost_detected = True
            elif _EXPLOSION_RE.search(line_norm) and (is_corridor_line or not has_distant):
                explosion_detected = True
            elif _CLEAR_RE.search(line_norm) and (is_corridor_line or not has_distant):
                clear_detected = True

        # 4. Determine overall action
        # Note: if an active corridor point is still reported on another line (e.g. a second drone at Dunaivtsi),
        # keep points_found so the active drone continues to be tracked; only set DIVERTED when no active corridor
        # points remain or when the action is DOWNED/LOST.
        action = ActionType.NONE
        if downed_detected:
            action = ActionType.DOWNED
        elif lost_detected:
            action = ActionType.LOST
        elif explosion_detected:
            action = ActionType.EXPLOSION
        elif divert_detected and not points_found:
            action = ActionType.DIVERTED
        elif clear_detected and not points_found:
            action = ActionType.CLEAR

        # 5. Count the targets locally in relevant corridor lines
        count = 1
        explicit_count = False
        search_target_text = "\n".join(relevant_lines) if relevant_lines else text
        for pattern, extractor in _COUNT_PATTERNS:
            m = pattern.search(search_target_text)
            if m:
                count = extractor(m)
                explicit_count = True
                break

        # Determine drone type and context inheritance
        has_general_drone = bool(_GENERAL_DRONE_RE.search(norm_text))
        if explicit_jet:
            drone_type = DroneType.JET
            is_jet = True
            self._channel_jet_context[post.channel] = now
            self._last_any_jet_time = now
        elif explicit_conventional:
            drone_type = DroneType.CONVENTIONAL
            is_jet = False
        elif inherited_jet and not has_general_drone and bool(points_found or action != ActionType.NONE):
            # Short follow-up along the jet track (e.g. "Дунаївці — курс на захід", "Хриплин")
            drone_type = DroneType.JET
            is_jet = True
            self._channel_jet_context[post.channel] = now
            self._last_any_jet_time = now
        else:
            drone_type = DroneType.CONVENTIONAL
            is_jet = False

        # Bind the action to the first found point or diverted point
        action_point = points_found[0] if points_found else divert_point_id
        is_new_target_marker = bool(_ANOTHER_TARGET_RE.search(norm_text))
        target_type = self._extract_target_type(search_target_text)

        return ParsedMessage(
            is_jet=is_jet,
            points=points_found,
            count=count,
            explicit_count=explicit_count,
            is_new_target_marker=is_new_target_marker,
            action=action,
            action_point_id=action_point,
            divert_destination=divert_destination,
            drone_type=drone_type,
            target_type=target_type,
            raw_text=text,
        )


