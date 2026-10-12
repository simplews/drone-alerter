"""Доменні моделі даних для обробки повідомлень, стану цілей та сповіщень."""
from __future__ import annotations

from dataclasses import dataclass, field
from datetime import datetime
from enum import Enum


class ActionType(str, Enum):
    NONE = "none"
    DOWNED = "downed"          # збито, уражено, мінус
    EXPLOSION = "explosion"    # вибух, чутно вибухи
    LOST = "lost"              # локаційно втрачено
    DIVERTED = "diverted"      # відворот на північ/Рівне/Волинь
    CLEAR = "clear"            # відбій


class AlertLevel(str, Enum):
    LEVEL_1 = "level_1"        # Увага (Піщанка, Вендичани, Нова/Стара Ушиця, Дунаївці) - silent push
    LEVEL_2 = "level_2"        # Наближається (Камʼянець, Скала, Борщів, Товсте) - loud push
    LEVEL_3A = "level_3a"      # Підліт (Чернелиця, Тлумач) 4-8 хв - urgent loud push
    LEVEL_3B = "level_3b"      # Над містом/передмістям (Тисмениця, Хриплин) 0-2 хв - urgent loud push
    INFO = "info"              # Інформаційне оновлення (імовірно збита, відворот)


class TargetStatus(str, Enum):
    ACTIVE = "active"
    PROBABLY_DOWNED = "probably_downed"
    DIVERTED = "diverted"
    EXPIRED = "expired"


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
    points: list[str]          # id пунктів у порядку згадки
    count: int = 1             # кількість цілей (за замовчуванням 1)
    explicit_count: bool = False  # чи було в тексті явно вказано кількість (наприклад, 2х, 1 ціль)
    action: ActionType = ActionType.NONE
    action_point_id: str | None = None
    raw_text: str = ""



@dataclass
class Target:
    id: str
    points_sequence: list[str] = field(default_factory=list)  # послідовність пройдених пунктів
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
    is_update: bool = False     # чи це редагування попереднього повідомлення
    source_links: list[tuple[str, str]] = field(default_factory=list)  # [(channel, url), ...]

