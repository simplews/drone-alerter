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
_DOWNED_RE = re.compile(
    r"(?i)\b(збит[оаіи]|збили|уражен[оа]|мінус\s+ціль|знищен[оа]|"
    r"працювали\s+сили\s+оборони|відпрацювал[аи]\s+(?:ппо|сили))\b"
)
_EXPLOSION_RE = re.compile(r"(?i)\b(вибух|вибухи|гучно|чутно\s+вибух)\b")
_LOST_RE = re.compile(
    r"(?i)\b(локаційно\s+втрачен\w*|втрачено\s+з\s+радар\w*|втрата\s+фіксації|"
    r"втрачено\s+фіксацію|переста\w*\s+фіксуватис\w*|не\s+фіксуєтьс\w*|зник\w*\s+з\s+радар\w*)\b"
)
_DIVERTED_RE = re.compile(
    r"(?i)\b(на\s+рівне|на\s+рівненщину|курсом\s+на\s+рівненщин\w*|на\s+волинь|на\s+житомир|на\s+житомирщину|"
    r"на\s+північ|відвернув|відвернул[аои]|відвертає|змінил[аои]?\s+курс)\b"
)
_CLEAR_RE = re.compile(r"(?i)\b(відбій|чисто|дорозвідка)\b")
_PREVENTIVE_RE = re.compile(r"(?i)\b(превентив\w*)\b")
_SUMMARY_OR_NEWS_RE = re.compile(
    r"(?i)(?:збито\s*/\s*подавлено|протягом\s+(?:дня|ночі|доби)|усього\s+збито|"
    r"загальні\s+бойові\s+втрати|су-34|су-35|су-24|ка-52|мі-8|крилат\w+\s+ракет\w+\s+«?нептун)"
)
_ANOTHER_TARGET_RE = re.compile(
    r"(?i)\b(ще\s+один|ще\s+одна|ще\s+1|ще\s+дв[іа]|інша\s+ціль|інший\s+шахед|інша\s+герань|нова\s+ціль|новий\s+шахед|другий\s+реактивн|друга\s+ціль)\b"
)


# Distant regions that do not belong to the western approach corridor
_DISTANT_REGION_RE = re.compile(
    r"(?i)\b(брянськ|брянщин|чернігів|сумщин|сумськ|харків|полтав|дніпропетров|запоріж|"
    r"херсон|миколаїв|одещин|одеськ|кіровоград|черкащин|черкаськ|донецьк|луганськ|крим|"
    r"київ|києв|вишгород|бровар|дарниц|дврз|чорнобил|славутич|житомир|бердичів)\w*"
)

# Section headers for northern-track oblasts/cities where drones are already far north of the IF corridor
_NORTHERN_SECTION_HEADER_RE = re.compile(
    r"(?i)\b(рівненщин|рівне|волин|луцьк|ковел)\w*"
)

# Northern-track locations and phrases (Zhytomyr -> North Khmelnytskyi -> Rivne -> Volyn)
_NORTHERN_TRACK_RE = re.compile(
    r"(?i)(?:"
    r"(?<!курсом\s)(?<!напрямком\s)\b(?:на|у|в)\s+(?:заході\s+|півдні\s+|півночі\s+|сході\s+|межі\s+)?(?:рівненщині|рівненщини|волині)\b|"
    r"\b(?:з\s+)?півночі\s+хмельниччин\w*|"
    r"\bна\s+північ\w*\s+хмельниччин\w*|"
    r"\bмежі\s+хмельниччин\w*\s+(?:та|і|й)\s+рівненщин\w*|"
    r"\bшепетівськ\w*\s+(?:район\w*|р-н\w*|рн)\b|"
    r"\b(?:луцьк|ковел|сарн|березн|костопіл|степан|вараш|дубровиц|клеван|оржів|"
    r"острог|здолбунів|мізоч|смиг|млинів|радивилів|колк|ківерц|маневич|рожищ|"
    r"торчин|турійськ|голоб|ягодин|любомл|головне|головного|головному|нововолинськ|ратн|шацьк|"
    r"славут|білогір|ізяслав|теофіпол|полонн|гриців|баранівк|ємільчин|звягел|коростен|овруч)\w*"
    r")"
)

# Approach / western regions relevant to the corridor
_CORRIDOR_REGION_RE = re.compile(
    r"(?i)\b(вінниччин|вінницьк|хмельниччин|хмельницьк|тернопіль|франків|прикарпат|буковин|чернівецьк|львів)\w*"
)


class MessageNormalizer:
    """Message analyzer that can inherit the target type from the context."""

    def __init__(self, ref_cache: ActiveReferenceCache, context_window_minutes: float = 20.0):
        self._ref_cache = ref_cache
        self._context_window = timedelta(minutes=context_window_minutes)
        # Last time of a jet target mention for each channel: channel -> datetime
        self._channel_jet_context: dict[str, datetime] = {}
        self._last_any_jet_time: datetime | None = None
        # Posts that had no corridor points (e.g. distant/northern regions) so replies to them are ignored
        self._non_corridor_posts: set[tuple[str, int]] = set()

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

        # 2a. Explicit directional prefix (allows any grammatical inflection, e.g. "курсом на Рівненщині")
        dir_prefix_match = re.search(
            r"(?i)\b(?:курсом\s+на|напрямком\s+на|у\s+напрямку|в\s+напрямку|в\s+бік|у\s+бік)\s+"
            r"(північ\w*|південь\w*|схід\w*|захід\w*|рівненщин\w*|рівне\w*|волин\w*|житомир\w*|хмельницьк\w*|тернопіль\w*|львів\w*)\b",
            line_norm,
        )
        if dir_prefix_match:
            return cls._normalize_divert_dest(dir_prefix_match.group(1))

        # 2b. Bare "на <direction>" — require accusative direction forms so locative ("на Рівненщині", "на Тернопільщині") is not treated as a direction
        bare_na_match = re.search(
            r"(?i)\bна\s+"
            r"(північ|південь|схід|захід|рівненщину|рівне|волинь|житомир|житомирщину|хмельницький|тернопіль|львів|львівщину)\b",
            line_norm,
        )
        if bare_na_match:
            return cls._normalize_divert_dest(bare_na_match.group(1))

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
            bool(re.search(rf"(?i)\b{re.escape(kw.lower())}\b", line_norm))
            for kw in cancel_keywords
            if kw.lower().startswith(("на ", "повз ")) or "відверн" in kw.lower() or "курс" in kw.lower()
        )

    @staticmethod
    def _is_section_header(line_strip: str) -> bool:
        """Return True if the line is a region/city header ending with ':' (allowing markdown bold/italic)."""
        cleaned = re.sub(r"[\*_~`!\s]+$", "", line_strip)
        return cleaned.endswith(":")

    def parse(self, post: RawPost) -> ParsedMessage:
        text = post.text
        norm_text = normalize(text)
        ref = self._get_ref()

        # 0. Ignore statistical summaries and non-UAV news reports
        if _SUMMARY_OR_NEWS_RE.search(norm_text):
            return ParsedMessage(
                is_jet=False,
                points=[],
                action=ActionType.NONE,
                raw_text=text,
            )

        # 1. Check for context inheritance
        now = post.posted_at_utc
        last_jet_time = self._channel_jet_context.get(post.channel)
        inherited_jet = bool(
            (last_jet_time and (now - last_jet_time) <= self._context_window)
            or (self._last_any_jet_time and (now - self._last_any_jet_time) <= self._context_window)
        )

        # 2. Line-aware analysis for multi-region digests and actions
        alias_index = ref.alias_index()
        sorted_aliases = sorted(alias_index.keys(), key=len, reverse=True)
        alias_patterns = [
            (alias, re.compile(rf"(?i)(?:\b|^){re.escape(alias)}(?:\b|$)"))
            for alias in sorted_aliases
        ]

        points_found: list[str] = []
        point_groups: list[list[str]] = []
        divert_detected = False
        divert_point_id: str | None = None
        divert_destination: str | None = None
        divert_destinations: list[str] = []
        downed_detected = False
        downed_point_id: str | None = None
        lost_detected = False
        lost_point_id: str | None = None
        explosion_detected = False
        clear_detected = False
        relevant_lines: list[str] = []
        corridor_action_lines: list[str] = []

        # Track whether the current paragraph block belongs to a distant or northern region header (e.g. "**Кіровоградщина:**")
        in_distant_section = False

        for raw_line in text.splitlines():
            line_strip = raw_line.strip()
            if not line_strip:
                in_distant_section = False
                continue

            line_norm = normalize(line_strip)
            is_header = self._is_section_header(line_strip)

            # Check if this line is a section header or mentions a region
            has_distant = bool(_DISTANT_REGION_RE.search(line_norm))
            has_northern_header = bool(_NORTHERN_SECTION_HEADER_RE.search(line_norm))
            has_corridor_reg = bool(_CORRIDOR_REGION_RE.search(line_norm))

            if is_header:
                if (has_distant or has_northern_header) and not has_corridor_reg:
                    in_distant_section = True
                elif has_corridor_reg:
                    in_distant_section = False
                # Section headers only set region context and never represent active target sightings
                continue

            # If the current line is inside a distant/northern section or mentions a distant region, ignore it
            if in_distant_section or has_distant:
                continue

            # Find corridor points on this specific line
            raw_line_matches: list[tuple[str, str]] = []  # (point_id, matched_alias)
            line_spans: list[tuple[int, int]] = []
            for alias, pat in alias_patterns:
                for m in pat.finditer(line_norm):
                    s, e = m.span()
                    if any(es <= s < ee or es < e <= ee for es, ee in line_spans):
                        continue
                    # A settlement alias without district suffix should not match if immediately
                    # followed by a district qualifier (e.g. 'Камʼянець-Подільський р-н' should not
                    # match the city 'Камʼянець-Подільський').
                    is_raion_alias = bool(re.search(r"(?i)\b(?:район\w*|р-н\w*|рн)\b", alias))
                    if not is_raion_alias and re.match(r"^\s*(?:район\w*|р-н\w*|рн\b)", line_norm[e:]):
                        continue

                    line_spans.append((s, e))
                    pid = alias_index[alias]
                    raw_line_matches.append((pid, alias))

            # If a specific settlement is present on this line, generic raion qualifiers
            # (e.g. "Чернелиця, Коломийський район", "Дунаївці, Камʼянець-Подільський р-н")
            # are administrative clarifications and should not trigger the raion center as a second point.
            has_specific_settlement = any(
                not re.search(r"(?i)\b(?:район\w*|р-н\w*|рн)\b", alias)
                for _, alias in raw_line_matches
            )
            line_points: list[str] = []
            for pid, alias in raw_line_matches:
                is_raion_alias = bool(re.search(r"(?i)\b(?:район\w*|р-н\w*|рн)\b", alias))
                if has_specific_settlement and is_raion_alias:
                    continue
                if pid not in line_points:
                    line_points.append(pid)

            # Sort multiple points on the same line from upstream to downstream (descending distance_km)
            if len(line_points) > 1:
                line_points.sort(
                    key=lambda pid: (ref.points[pid].distance_km or 0.0) if pid in ref.points else 0.0,
                    reverse=True,
                )

            # Ignore northern-track lines (Rivne/Volyn/Shepetivka/Bilohiria/etc.) that have no southern corridor points
            has_northern_track = bool(_NORTHERN_TRACK_RE.search(line_norm))
            if has_northern_track and not line_points:
                continue

            line_is_clear = bool(_CLEAR_RE.search(line_norm))
            line_is_preventive = bool(_PREVENTIVE_RE.search(line_norm))
            line_is_downed = bool(_DOWNED_RE.search(line_norm))
            line_is_lost = bool(_LOST_RE.search(line_norm))
            line_is_explosion = bool(_EXPLOSION_RE.search(line_norm))

            # If a line is a downed/lost report, record the action and its point rather than an active movement sighting
            if line_is_downed:
                downed_detected = True
                if line_points and downed_point_id is None:
                    downed_point_id = line_points[0]
                corridor_action_lines.append(line_strip)
                continue

            if line_is_lost:
                lost_detected = True
                if line_points and lost_point_id is None:
                    lost_point_id = line_points[0]
                corridor_action_lines.append(line_strip)
                continue

            # If a line is a reconnaissance ("дорозвідка") or preventive warning ("превентивно"),
            # do not treat mentioned districts/cities as active flying targets
            if line_is_clear or line_is_preventive:
                clear_detected = True
                continue

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
                dest = self._extract_divert_destination(line_norm, ref.cancel_keywords)
                if dest:
                    if divert_destination is None:
                        divert_destination = dest
                    if dest not in divert_destinations:
                        divert_destinations.append(dest)
                corridor_action_lines.append(line_strip)
            elif line_points:
                for pid in line_points:
                    if pid not in points_found:
                        points_found.append(pid)
                point_groups.append(line_points)
                relevant_lines.append(line_strip)
            elif line_has_divert and (is_corridor_line or not has_distant):
                divert_detected = True
                dest = self._extract_divert_destination(line_norm, ref.cancel_keywords)
                if dest:
                    if divert_destination is None:
                        divert_destination = dest
                    if dest not in divert_destinations:
                        divert_destinations.append(dest)
                corridor_action_lines.append(line_strip)

            if line_is_explosion and (is_corridor_line or not has_distant):
                explosion_detected = True
                corridor_action_lines.append(line_strip)

        # 3. Determine overall action
        # If an active corridor point is still reported on another line, keep tracking the active drone;
        # only set DIVERTED/DOWNED/LOST when no active movement points remain (or when the post is purely an action).
        has_any_corridor_point = bool(
            points_found or downed_point_id or lost_point_id or divert_point_id
        )
        is_reply_to_non_corridor = bool(
            post.reply_to_message_id is not None
            and (post.channel, post.reply_to_message_id) in self._non_corridor_posts
        )

        action = ActionType.NONE
        if not is_reply_to_non_corridor:
            if downed_detected and not points_found:
                action = ActionType.DOWNED
            elif lost_detected and not points_found:
                action = ActionType.LOST
            elif explosion_detected and not points_found:
                action = ActionType.EXPLOSION
            elif divert_detected and not points_found:
                action = ActionType.DIVERTED
            elif clear_detected and not points_found:
                action = ActionType.CLEAR

        if not has_any_corridor_point and (action == ActionType.NONE or is_reply_to_non_corridor):
            self._non_corridor_posts.add((post.channel, post.message_id))

        # 4. Count the targets locally in relevant corridor lines
        count = 1
        explicit_count = False
        if relevant_lines:
            search_target_text = "\n".join(relevant_lines)
        elif corridor_action_lines:
            search_target_text = "\n".join(corridor_action_lines)
        else:
            search_target_text = text

        for pattern, extractor in _COUNT_PATTERNS:
            m = pattern.search(search_target_text)
            if m:
                count = extractor(m)
                explicit_count = True
                break

        # 5. Determine drone type and context inheritance on corridor-relevant text
        norm_target_text = normalize(search_target_text)
        explicit_jet = any(kw.lower() in norm_target_text for kw in ref.target_keywords)
        explicit_conventional = bool(_CONVENTIONAL_KW_RE.search(norm_target_text))
        has_general_drone = bool(_GENERAL_DRONE_RE.search(norm_target_text))
        has_corridor_activity = bool(points_found or action != ActionType.NONE)

        if explicit_jet:
            drone_type = DroneType.JET
            is_jet = True
            if has_corridor_activity:
                self._channel_jet_context[post.channel] = now
                self._last_any_jet_time = now
        elif explicit_conventional:
            drone_type = DroneType.CONVENTIONAL
            is_jet = False
        elif inherited_jet and not has_general_drone and has_corridor_activity:
            # Short follow-up along the jet track (e.g. "Дунаївці — курс на захід", "Хриплин")
            drone_type = DroneType.JET
            is_jet = True
            self._channel_jet_context[post.channel] = now
            self._last_any_jet_time = now
        else:
            drone_type = DroneType.CONVENTIONAL
            is_jet = False

        # Bind the action to the specific action point or first found point
        if action == ActionType.DOWNED and downed_point_id:
            action_point = downed_point_id
        elif action == ActionType.LOST and lost_point_id:
            action_point = lost_point_id
        elif points_found:
            action_point = points_found[0]
        else:
            action_point = divert_point_id

        is_new_target_marker = bool(_ANOTHER_TARGET_RE.search(norm_target_text))
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
            divert_destinations=divert_destinations,
            point_groups=point_groups,
            drone_type=drone_type,
            target_type=target_type,
            raw_text=text,
        )



