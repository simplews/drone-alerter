"""Пошук нових назв пунктів і нових форм відомих назв у постах про цілі.

Нічого не додається до словника автоматично: знахідки потрапляють у чергу
``alias_candidates``, а людина схвалює або відхиляє їх (CLI `aliases`,
`approve-alias`, `reject-alias`). Причина — омоніми: евристика легко сплутає
«Скала» і «Скалат» або два різні села «Товсте».
"""
from __future__ import annotations

from dataclasses import dataclass

from .reference import ReferenceData
from .store import LearningStore
from .text import extract_place_candidates, is_region_name, normalize, phrases_match


@dataclass
class DiscoveryReport:
    posts_scanned: int = 0
    candidates_seen: int = 0


def suggest_point(phrase: str, ref: ReferenceData) -> str | None:
    """Id відомого пункту, відмінком якого може бути ця фраза."""
    for pid, point in ref.points.items():
        if any(phrases_match(phrase, name) for name in point.names):
            return pid
    return None


def discover_aliases(store: LearningStore, ref: ReferenceData) -> DiscoveryReport:
    known = ref.alias_index()
    stop = {normalize(s) for s in ref.stopwords}
    report = DiscoveryReport()
    for post in store.unscanned_target_posts():
        report.posts_scanned += 1
        for phrase in extract_place_candidates(post["text"]):
            norm = normalize(phrase)
            if norm in known or norm in stop or is_region_name(phrase):
                continue
            store.upsert_alias_candidate(
                phrase_norm=norm,
                surface=phrase,
                seen_at=post["posted_at_utc"],
                suggested_point_id=suggest_point(phrase, ref),
                example=f"[{post['channel']}] {post['text']}",
            )
            report.candidates_seen += 1
        store.mark_scanned(post["channel"], post["message_id"])
    return report
