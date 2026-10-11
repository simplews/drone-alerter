"""Версійовані довідники: пункти коридору, таблиця ETA, ключові слова.

Кожне оновлення створює новий незмінний файл ``vNNNN.json``. Рушій читає лише
активну версію (номер у файлі ``ACTIVE``). Тому будь-яку зміну можна
переглянути, порівняти з попередньою і відкотити.
"""
from __future__ import annotations

import copy
import json
import os
from dataclasses import asdict, dataclass, field
from datetime import UTC, datetime
from pathlib import Path

from .text import normalize

STATUS_ACTIVE = "active"
STATUS_PENDING = "pending"
STATUS_REJECTED = "rejected"
STATUS_ARCHIVED = "archived"

# Слова, які схожі на назви пунктів, але ними не є (області, сторони світу,
# службові слова). Використовується під час пошуку нових назв.
DEFAULT_STOPWORDS = [
    "україна", "увага", "оновлення", "upd", "захід", "схід", "північ", "південь",
    "повітряні", "сили", "реактивна", "реактивний", "реактивні", "герань", "шахед",
    "бпла", "іф", "франківськ", "івано-франківськ", "тернопіль", "хмельницький",
    "волинь", "буковина", "поділля",
]


@dataclass
class PointRef:
    id: str
    names: list[str]
    level: int
    # Відстань до опорної точки (аеродром). Лише для розрахунків на сервері,
    # користувачу не показується.
    distance_km: float | None = None
    eta_min: float | None = None
    eta_max: float | None = None
    # seed | manual | physics | minmax+margin[+prior] | empirical_q
    eta_method: str = "none"
    n_obs: int = 0
    # P(ціль іде на ІФ | ціль згадана в цьому пункті), згладжена оцінка.
    base_rate: float | None = None
    n_outcomes: int = 0


@dataclass
class ReferenceData:
    version: int = 0
    parent_version: int | None = None
    created_at: str = ""
    status: str = STATUS_PENDING
    points: dict[str, PointRef] = field(default_factory=dict)
    speed_kmh: list[float] = field(default_factory=lambda: [400.0, 480.0])
    # Шум часу публікації (хв). Враховує запізнення і першої згадки, і
    # повідомлення про прибуття.
    post_lag_min: list[float] = field(default_factory=lambda: [0.5, 1.5])

    target_keywords: list[str] = field(default_factory=list)
    cancel_keywords: list[str] = field(default_factory=list)
    stopwords: list[str] = field(default_factory=lambda: list(DEFAULT_STOPWORDS))
    changelog: list[str] = field(default_factory=list)
    metrics: dict = field(default_factory=dict)

    # --- серіалізація ---------------------------------------------------
    def to_dict(self) -> dict:
        data = asdict(self)
        data["points"] = {pid: asdict(p) for pid, p in self.points.items()}
        return data

    @classmethod
    def from_dict(cls, data: dict) -> ReferenceData:
        data = dict(data)
        points = {pid: PointRef(**p) for pid, p in data.pop("points", {}).items()}
        return cls(points=points, **data)

    # --- допоміжне ------------------------------------------------------
    def alias_index(self) -> dict[str, str]:
        """Нормалізована назва -> id пункту."""
        index: dict[str, str] = {}
        for pid, point in self.points.items():
            for name in point.names:
                index[normalize(name)] = pid
        return index

    def clone_as_candidate(self) -> ReferenceData:
        cand = copy.deepcopy(self)
        cand.parent_version = self.version
        cand.version = 0
        cand.status = STATUS_PENDING
        cand.created_at = ""
        cand.changelog = []
        cand.metrics = {}
        return cand


def _utcnow_iso() -> str:
    return datetime.now(UTC).replace(microsecond=0).isoformat()


def _atomic_write(path: Path, content: str) -> None:
    tmp = path.with_suffix(path.suffix + ".tmp")
    tmp.write_text(content, encoding="utf-8")
    os.replace(tmp, path)


class ReferenceRepository:
    """Сховище версій довідників у теці (за замовчуванням ``data/reference``)."""

    def __init__(self, root: str | os.PathLike):
        self.root = Path(root)
        self.root.mkdir(parents=True, exist_ok=True)
        self._active_file = self.root / "ACTIVE"

    def _path(self, version: int) -> Path:
        return self.root / f"v{version:04d}.json"

    def versions(self) -> list[int]:
        return sorted(int(p.stem[1:]) for p in self.root.glob("v[0-9][0-9][0-9][0-9].json"))

    def load(self, version: int) -> ReferenceData:
        return ReferenceData.from_dict(json.loads(self._path(version).read_text(encoding="utf-8")))

    def _write(self, ref: ReferenceData) -> None:
        _atomic_write(self._path(ref.version), json.dumps(ref.to_dict(), ensure_ascii=False, indent=2))

    def save_new(self, ref: ReferenceData) -> ReferenceData:
        existing = self.versions()
        ref.version = (existing[-1] + 1) if existing else 1
        ref.created_at = _utcnow_iso()
        self._write(ref)
        return ref

    def set_status(self, version: int, status: str) -> None:
        ref = self.load(version)
        ref.status = status
        self._write(ref)

    def active_version(self) -> int | None:
        if not self._active_file.exists():
            return None
        return int(self._active_file.read_text().strip())

    def active(self) -> ReferenceData:
        version = self.active_version()
        if version is None:
            raise LookupError("Немає активної версії довідників. Запустіть `seed`.")
        return self.load(version)

    def promote(self, version: int) -> None:
        if version not in self.versions():
            raise LookupError(f"Версії v{version} не існує")
        old = self.active_version()
        if old is not None and old != version:
            self.set_status(old, STATUS_ARCHIVED)
        self.set_status(version, STATUS_ACTIVE)
        _atomic_write(self._active_file, str(version))

    def reject(self, version: int) -> None:
        if version == self.active_version():
            raise ValueError("Не можна відхилити активну версію. Використайте rollback.")
        self.set_status(version, STATUS_REJECTED)

    def rollback(self) -> int:
        """Повертає попередню версію (parent активної). Повертає її номер."""
        current = self.active()
        if current.parent_version is None:
            raise LookupError("Немає попередньої версії для відкату")
        target = current.parent_version
        self.promote(target)
        self.set_status(current.version, STATUS_REJECTED)
        return target


class ActiveReferenceCache:
    """Для рушія: віддає активну версію і перечитує її, коли змінився ACTIVE.

    Так нова версія підхоплюється без перезапуску сервісу.
    """

    def __init__(self, repo: ReferenceRepository):
        self._repo = repo
        self._version: int | None = None
        self._ref: ReferenceData | None = None

    def get(self) -> ReferenceData:
        version = self._repo.active_version()
        if self._ref is None or version != self._version:
            self._ref = self._repo.active()
            self._version = version
        return self._ref


def seed_from_dictionary() -> ReferenceData:
    """Перша версія з src/nlp/dictionary.py (дані з PRD)."""
    from src.nlp import dictionary as d

    # Реальні відстані польоту до Івано-Франківська вздовж коридору
    known_distances = {
        "khryplyn": 7.0,
        "tysmenytsia": 12.0,
        "tlumach": 25.0,
        "chernelytsia": 54.0,
        "tovste": 75.0,
        "borshchiv": 95.0,
        "skala_podilska": 110.0,
        "kamianets_podilskyi": 135.0,
        "dunaivtsi": 160.0,
        "stara_ushytsia": 165.0,
        "nova_ushytsia": 190.0,
        "vendychany": 225.0,
        "tomashpil": 250.0,
        "pishchanka": 265.0,
    }


    points: dict[str, PointRef] = {}
    for item in d.CORRIDOR_POINTS:
        pid = item["id"]
        eta = d.ETA_TABLE.get(pid)
        points[pid] = PointRef(
            id=pid,
            names=list(item["names"]),
            level=item["level"],
            distance_km=known_distances.get(pid),
            eta_min=float(eta[0]) if eta else None,
            eta_max=float(eta[1]) if eta else None,
            eta_method="seed" if eta else "none",
        )
    return ReferenceData(
        points=points,
        target_keywords=list(d.TARGET_KEYWORDS),
        cancel_keywords=list(d.CANCEL_KEYWORDS),
        changelog=["Початкова версія з src/nlp/dictionary.py (дані PRD, 8 епізодів 8–10.10.2026)"],
    )
