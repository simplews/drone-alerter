"""Normalization and parsing of the message text from the monitoring channels."""
from __future__ import annotations

import re
from datetime import datetime, timedelta

from src.engine.models import ActionType, ParsedMessage, RawPost
from src.learning.reference import ActiveReferenceCache, ReferenceData
from src.learning.text import normalize

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

# Patterns for actions
_DOWNED_RE = re.compile(r"(?i)\b(збит[оаіи]|збили|уражен[оа]|мінус\s+ціль|знищен[оа])\b")
_EXPLOSION_RE = re.compile(r"(?i)\b(вибух|вибухи|гучно|чутно\s+вибух)\b")
_LOST_RE = re.compile(r"(?i)\b(локаційно\s+втрачен[оа]|втрачено\s+з\s+радарів)\b")
_DIVERTED_RE = re.compile(r"(?i)\b(на\s+рівне|на\s+рівненщину|на\s+волинь|на\s+житомир|на\s+житомирщину|на\s+північ|відвернув|відвернул[аои]|відвертає|змінил[аои]?\s+курс)\b")
_CLEAR_RE = re.compile(r"(?i)\b(відбій|чисто|дорозвідка)\b")


class MessageNormalizer:
    """Message analyzer that can inherit the target type from the context."""

    def __init__(self, ref_cache: ActiveReferenceCache, context_window_minutes: float = 20.0):
        self._ref_cache = ref_cache
        self._context_window = timedelta(minutes=context_window_minutes)
        # Last time of a jet target mention for each channel: channel -> datetime
        self._channel_jet_context: dict[str, datetime] = {}

    def _get_ref(self) -> ReferenceData:
        return self._ref_cache.get()

    def parse(self, post: RawPost) -> ParsedMessage:
        text = post.text
        norm_text = normalize(text)
        ref = self._get_ref()

        # 1. Check for explicit jet drone keywords
        explicit_jet = any(kw.lower() in norm_text for kw in ref.target_keywords)

        # 2. Check for context inheritance
        now = post.posted_at_utc
        last_jet_time = self._channel_jet_context.get(post.channel)
        inherited_jet = False
        if last_jet_time and (now - last_jet_time) <= self._context_window:
            inherited_jet = True

        # 3. Detect settlements in the corridor
        points_found: list[str] = []
        alias_index = ref.alias_index()

        # Sort the aliases from longer to shorter, so "нова ушиця" matches before "ушиця"
        sorted_aliases = sorted(alias_index.keys(), key=len, reverse=True)
        matched_spans: list[tuple[int, int]] = []

        for alias in sorted_aliases:
            # Look for the alias with word boundaries, or at the start/end of the line
            pattern = re.compile(rf"(?i)(?:\b|^){re.escape(alias)}(?:\b|$)")
            for match in pattern.finditer(norm_text):
                start, end = match.span()
                # Check that a longer alias does not already cover this fragment
                overlap = any(s <= start < e or s < end <= e for s, e in matched_spans)
                if not overlap:
                    matched_spans.append((start, end))
                    point_id = alias_index[alias]
                    if point_id not in points_found:
                        points_found.append(point_id)

        # 4. Detect actions
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

        # 5. Count the targets (locally for the found points, to avoid false matches from summary messages)
        count = 1
        explicit_count = False
        if points_found and matched_spans:

            # Find the local context around the found points
            # Find the paragraph or line boundaries around the found points
            first_span_start = min(s for s, e in matched_spans)
            last_span_end = max(e for s, e in matched_spans)
            
            # Look for the paragraph boundary (\n\n or \n)
            prev_nl = text.rfind("\n\n", 0, first_span_start)
            if prev_nl == -1:
                # Try a single line break if this is a list of regions
                prev_nl = text.rfind("\n", 0, first_span_start)
            start_ctx = 0 if prev_nl == -1 else prev_nl + 1

            next_nl = text.find("\n\n", last_span_end)
            if next_nl == -1:
                next_nl = text.find("\n", last_span_end)
            end_ctx = len(text) if next_nl == -1 else next_nl

            local_text = text[start_ctx:end_ctx]
            for pattern, extractor in _COUNT_PATTERNS:
                m = pattern.search(local_text)
                if m:
                    count = extractor(m)
                    explicit_count = True
                    break
        else:
            for pattern, extractor in _COUNT_PATTERNS:
                m = pattern.search(text)
                if m:
                    count = extractor(m)
                    explicit_count = True
                    break

        # If a jet target is confirmed, extend the context window of the channel
        is_jet = explicit_jet or (inherited_jet and bool(points_found or action != ActionType.NONE))
        if is_jet:
            self._channel_jet_context[post.channel] = now

        # Bind the action to the first found point
        action_point = points_found[0] if points_found else None

        return ParsedMessage(
            is_jet=is_jet,
            points=points_found,
            count=count,
            explicit_count=explicit_count,
            action=action,
            action_point_id=action_point,
            raw_text=text,
        )


