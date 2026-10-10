"""Нормалізація та парсинг тексту повідомлень моніторингових каналів."""
from __future__ import annotations

import re
from datetime import datetime, timedelta

from src.engine.models import ActionType, ParsedMessage, RawPost
from src.learning.reference import ActiveReferenceCache, ReferenceData
from src.learning.text import normalize

# Патерни кількості цілей
_COUNT_PATTERNS = [
    (re.compile(r"(?i)\b(\d+)\s*(?:х|x)\b"), lambda m: int(m.group(1))),
    (re.compile(r"(?i)\b(\d+)\s+(?:реактивн|бпла|шахед|геран)"), lambda m: int(m.group(1))),
    (re.compile(r"(?i)\bдві\s+реактивн"), lambda _: 2),
    (re.compile(r"(?i)\bпара\s+реактивн"), lambda _: 2),
    (re.compile(r"(?i)\bтри\s+реактивн"), lambda _: 3),
    (re.compile(r"(?i)\bодин\s+реактивн"), lambda _: 1),
    (re.compile(r"(?i)\bодна\s+реактивн"), lambda _: 1),
]

# Патерни дій
_DOWNED_RE = re.compile(r"(?i)\b(збит[оаіи]|збили|уражен[оа]|мінус\s+ціль|знищен[оа])\b")
_EXPLOSION_RE = re.compile(r"(?i)\b(вибух|вибухи|гучно|чутно\s+вибух)\b")
_LOST_RE = re.compile(r"(?i)\b(локаційно\s+втрачен[оа]|втрачено\s+з\s+радарів)\b")
_DIVERTED_RE = re.compile(r"(?i)\b(на\s+рівне|на\s+рівненщину|на\s+волинь|на\s+житомир|на\s+житомирщину|на\s+північ|відвернув)\b")
_CLEAR_RE = re.compile(r"(?i)\b(відбій|чисто|дорозвідка)\b")


class MessageNormalizer:
    """Аналізатор повідомлень із підтримкою контекстного успадкування типу цілі."""

    def __init__(self, ref_cache: ActiveReferenceCache, context_window_minutes: float = 20.0):
        self._ref_cache = ref_cache
        self._context_window = timedelta(minutes=context_window_minutes)
        # Останній час згадки реактивної цілі для кожного каналу: channel -> datetime
        self._channel_jet_context: dict[str, datetime] = {}

    def _get_ref(self) -> ReferenceData:
        return self._ref_cache.get()

    def parse(self, post: RawPost) -> ParsedMessage:
        text = post.text
        norm_text = normalize(text)
        ref = self._get_ref()

        # 1. Перевірка явних ключових слів реактивного БпЛА
        explicit_jet = any(kw.lower() in norm_text for kw in ref.target_keywords)

        # 2. Перевірка контекстного успадкування
        now = post.posted_at_utc
        last_jet_time = self._channel_jet_context.get(post.channel)
        inherited_jet = False
        if last_jet_time and (now - last_jet_time) <= self._context_window:
            inherited_jet = True

        # 3. Виявлення населених пунктів коридору
        points_found: list[str] = []
        alias_index = ref.alias_index()

        # Сортуємо аліаси від довших до коротших, щоб "нова ушиця" матчилась перед "ушиця"
        sorted_aliases = sorted(alias_index.keys(), key=len, reverse=True)
        matched_spans: list[tuple[int, int]] = []

        for alias in sorted_aliases:
            # Шукаємо аліас із межами слів або на початку/кінці рядка
            pattern = re.compile(rf"(?i)(?:\b|^){re.escape(alias)}(?:\b|$)")
            for match in pattern.finditer(norm_text):
                start, end = match.span()
                # Перевіряємо, чи цей фрагмент уже не покритий довшим аліасом
                overlap = any(s <= start < e or s < end <= e for s, e in matched_spans)
                if not overlap:
                    matched_spans.append((start, end))
                    point_id = alias_index[alias]
                    if point_id not in points_found:
                        points_found.append(point_id)

        # 4. Виявлення дій
        action = ActionType.NONE
        if _DOWNED_RE.search(norm_text):
            action = ActionType.DOWNED
        elif _LOST_RE.search(norm_text):
            action = ActionType.LOST
        elif _EXPLOSION_RE.search(norm_text):
            action = ActionType.EXPLOSION
        elif _DIVERTED_RE.search(norm_text):
            action = ActionType.DIVERTED
        elif _CLEAR_RE.search(norm_text):
            action = ActionType.CLEAR

        # 5. Підрахунок кількості цілей
        count = 1
        for pattern, extractor in _COUNT_PATTERNS:
            m = pattern.search(text)
            if m:
                count = extractor(m)
                break

        # Якщо є підтвердження реактивної цілі — продовжуємо контекстне вікно каналу
        is_jet = explicit_jet or (inherited_jet and bool(points_found or action != ActionType.NONE))
        if is_jet:
            self._channel_jet_context[post.channel] = now

        # Прив'язка дії до першого знайденого пункту
        action_point = points_found[0] if points_found else None

        return ParsedMessage(
            is_jet=is_jet,
            points=points_found,
            count=count,
            action=action,
            action_point_id=action_point,
            raw_text=text,
        )
