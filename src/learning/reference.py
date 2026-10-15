"""Versioned references: corridor points, ETA table, keywords.

The first version comes from the city profile (``data/profiles``).

Each update creates a new immutable file ``vNNNN.json``. The engine reads only the
active version (the number in the file ``ACTIVE``). So you can review
any change, compare it with the previous version and roll it back.
"""
from __future__ import annotations

import copy
import json
import os
from dataclasses import asdict, dataclass, field
from datetime import UTC, datetime
from pathlib import Path

from src.profile import CityProfile, get_profile

from .text import normalize

STATUS_ACTIVE = "active"
STATUS_PENDING = "pending"
STATUS_REJECTED = "rejected"
STATUS_ARCHIVED = "archived"

@dataclass
class PointRef:
    id: str
    names: list[str]
    level: int
    # Distance to the reference point (airfield). Only the server uses it for
    # calculations. The user does not see it.
    distance_km: float | None = None
    eta_min: float | None = None
    eta_max: float | None = None
    # seed | manual | physics | minmax+margin[+prior] | empirical_q
    eta_method: str = "none"
    n_obs: int = 0
    # P(target goes to the city | target mentioned at this point), smoothed estimate.
    base_rate: float | None = None
    n_outcomes: int = 0
    is_regional: bool = False
    name_gen: str = ""


@dataclass
class ReferenceData:
    version: int = 0
    parent_version: int | None = None
    created_at: str = ""
    status: str = STATUS_PENDING
    points: dict[str, PointRef] = field(default_factory=dict)
    speed_kmh: list[float] = field(default_factory=lambda: [400.0, 480.0])
    conventional_speed_kmh: list[float] = field(default_factory=lambda: [150.0, 185.0])
    # Publication time noise (minutes). It covers the delay of the first mention and
    # of the arrival message.
    post_lag_min: list[float] = field(default_factory=lambda: [0.5, 1.5])

    target_keywords: list[str] = field(default_factory=list)
    cancel_keywords: list[str] = field(default_factory=list)
    stopwords: list[str] = field(default_factory=list)
    changelog: list[str] = field(default_factory=list)
    metrics: dict = field(default_factory=dict)

    # --- serialization ---------------------------------------------
    def to_dict(self) -> dict:
        data = asdict(self)
        data["points"] = {pid: asdict(p) for pid, p in self.points.items()}
        return data

    @classmethod
    def from_dict(cls, data: dict) -> ReferenceData:
        data = dict(data)
        points = {pid: PointRef(**p) for pid, p in data.pop("points", {}).items()}
        return cls(points=points, **data)

    # --- helpers ------------------------------------------------------
    def alias_index(self) -> dict[str, str]:
        """Normalized name -> point ID."""
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
    """Storage of reference versions in a directory (default ``data/reference``)."""

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
        """Return the previous version (the parent of the active one). Return its number."""
        current = self.active()
        if current.parent_version is None:
            raise LookupError("Немає попередньої версії для відкату")
        target = current.parent_version
        self.promote(target)
        self.set_status(current.version, STATUS_REJECTED)
        return target


class ActiveReferenceCache:
    """For the engine: return the active version and reload it when ACTIVE changes.

    So the service picks up a new version without a restart.
    """

    def __init__(self, repo: ReferenceRepository):
        self._repo = repo
        self._version: int | None = None
        self._ref: ReferenceData | None = None

    def get(self) -> ReferenceData:
        version = self._repo.active_version()
        if self._ref is None or version != self._version:
            ref = self._repo.active()
            try:
                profile = get_profile()
                if hasattr(profile, "conventional_speed_kmh") and profile.conventional_speed_kmh:
                    ref.conventional_speed_kmh = list(profile.conventional_speed_kmh)
                for kw in profile.target_keywords:
                    if kw not in ref.target_keywords:
                        ref.target_keywords.append(kw)
                if profile.cancel_keywords:
                    ref.cancel_keywords = list(profile.cancel_keywords)
                for sw in profile.stopwords:
                    if sw not in ref.stopwords:
                        ref.stopwords.append(sw)
                for item in profile.points:
                    pid = item["id"]
                    if pid not in ref.points:
                        eta_min, eta_max = item.get("eta_min"), item.get("eta_max")
                        has_eta = eta_min is not None
                        ref.points[pid] = PointRef(
                            id=pid,
                            names=list(item["names"]),
                            level=item["level"],
                            distance_km=item.get("distance_km"),
                            eta_min=float(eta_min) if has_eta else None,
                            eta_max=float(eta_max) if has_eta else None,
                            eta_method="profile_seed" if has_eta else "none",
                            is_regional=bool(item.get("is_regional", False)),
                            name_gen=str(item.get("name_gen", "")),
                        )
                    else:
                        if "is_regional" in item:
                            ref.points[pid].is_regional = bool(item["is_regional"])
                        if "name_gen" in item:
                            ref.points[pid].name_gen = str(item["name_gen"])
                        for name in item.get("names", []):
                            if name not in ref.points[pid].names:
                                ref.points[pid].names.append(name)
            except Exception:  # noqa: BLE001, S110
                pass
            self._ref = ref
            self._version = version
        return self._ref


def seed_from_profile(profile: CityProfile | None = None) -> ReferenceData:
    """First version from the city profile (default: the profile that the settings select)."""
    profile = profile or get_profile()
    points: dict[str, PointRef] = {}
    for item in profile.points:
        eta_min, eta_max = item.get("eta_min"), item.get("eta_max")
        has_eta = eta_min is not None
        points[item["id"]] = PointRef(
            id=item["id"],
            names=list(item["names"]),
            level=item["level"],
            distance_km=item.get("distance_km"),
            eta_min=float(eta_min) if has_eta else None,
            eta_max=float(eta_max) if has_eta else None,
            eta_method="seed" if has_eta else "none",
            is_regional=bool(item.get("is_regional", False)),
            name_gen=str(item.get("name_gen", "")),
        )
    return ReferenceData(
        points=points,
        speed_kmh=list(profile.speed_kmh),
        conventional_speed_kmh=list(getattr(profile, "conventional_speed_kmh", [150.0, 185.0])),
        post_lag_min=list(profile.post_lag_min),
        target_keywords=list(profile.target_keywords),
        cancel_keywords=list(profile.cancel_keywords),
        stopwords=list(profile.stopwords),
        changelog=[f"Початкова версія з профілю {profile.id}"],
    )
