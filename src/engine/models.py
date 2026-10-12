"""Domain data models for message processing, target state and alerts."""
from __future__ import annotations

from dataclasses import dataclass, field
from datetime import datetime
from enum import Enum


class ActionType(str, Enum):
    NONE = "none"
    DOWNED = "downed"          # shot down, hit, minus
    EXPLOSION = "explosion"    # explosion, explosions heard
    LOST = "lost"              # radar contact lost
    DIVERTED = "diverted"      # turn north / Rivne / Volyn
    CLEAR = "clear"            # all clear


class AlertLevel(str, Enum):
    LEVEL_1 = "level_1"        # Attention (Pishchanka, Vendychany, Nova/Stara Ushytsia, Dunaivtsi) - silent push
    LEVEL_2 = "level_2"        # Approaching (Kamianets-Podilskyi, Skala, Borshchiv, Tovste) - loud push
    LEVEL_3A = "level_3a"      # Close approach (Chernelytsia, Tlumach) 4-8 min - urgent loud push
    LEVEL_3B = "level_3b"      # Over the city or suburbs (Tysmenytsia, Khryplyn) 0-2 min - urgent loud push
    INFO = "info"              # Information update (probably shot down, turn away)


class TargetStatus(str, Enum):
    ACTIVE = "active"
    PROBABLY_DOWNED = "probably_downed"
    DIVERTED = "diverted"
    EXPIRED = "expired"
    LOST = "lost"


@dataclass
class RawPost:
    channel: str
    message_id: int
    text: str
    posted_at_utc: datetime
    is_edit: bool = False


@dataclass
class ParsedMessage:
    is_jet: bool
    points: list[str]          # IDs of points in the order of mention
    count: int = 1             # number of targets (default 1)
    explicit_count: bool = False  # True if the text states the number explicitly (for example 2х, 1 ціль)
    action: ActionType = ActionType.NONE
    action_point_id: str | None = None
    raw_text: str = ""



@dataclass
class Target:
    id: str
    points_sequence: list[str] = field(default_factory=list)  # sequence of points that the target passed
    last_point_id: str = ""
    target_count: int = 1
    status: TargetStatus = TargetStatus.ACTIVE
    first_seen_utc: datetime = field(default_factory=datetime.utcnow)
    last_seen_utc: datetime = field(default_factory=datetime.utcnow)
    level: AlertLevel = AlertLevel.LEVEL_1
    eta_min_minutes: float | None = None
    eta_max_minutes: float | None = None
    eta_window_start_utc: datetime | None = None
    eta_window_end_utc: datetime | None = None
    confidence: str = "low"     # low, medium, high
    channels: set[str] = field(default_factory=set)
    message_ids: list[tuple[str, int]] = field(default_factory=list)


@dataclass
class AlertEvent:
    target_id: str
    level: AlertLevel
    title: str
    body: str
    is_silent: bool
    eta_window_str: str = ""
    is_update: bool = False     # True if this is an edit of an earlier message
    source_links: list[tuple[str, str]] = field(default_factory=list)  # [(channel, url), ...]

