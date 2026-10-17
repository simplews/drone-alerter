"""City profile: all data that is specific to the protected city and its approach corridor.

A profile is one JSON file in ``data/profiles/``. The ``CITY_PROFILE`` setting selects it.
See docs/ADAPTING.md for the file format.
"""
from __future__ import annotations

import json
import re
from dataclasses import dataclass, field
from functools import lru_cache
from pathlib import Path

from src.config import settings

_PROFILE_ID = re.compile(r"^[a-z0-9]+(-[a-z0-9]+)*$")
_REQUIRED_TEXTS = (
    "city_gen", "city_acc", "region_loc", "route_regions", "route_short", "divert_phrase",
)


@dataclass(frozen=True)
class CityProfile:
    id: str
    short_name: str
    siren_region: str
    speed_kmh: list[float]
    post_lag_min: list[float]
    texts: dict
    stopwords: list[str]
    target_keywords: list[str]
    cancel_keywords: list[str]
    points: list[dict]
    conventional_speed_kmh: list[float] = field(default_factory=lambda: [150.0, 185.0])
    missile_speed_kmh: list[float] = field(default_factory=lambda: [750.0, 880.0])


def parse_profile(data: dict) -> CityProfile:
    """Validate the raw profile data and build a CityProfile. Raise ValueError on a bad profile."""
    for key in ("id", "short_name", "siren_region", "texts", "points", "target_keywords"):
        if not data.get(key):
            raise ValueError(f"У профілі немає обовʼязкового поля {key!r}")
    texts = data["texts"]
    missing = [k for k in _REQUIRED_TEXTS if not texts.get(k)]
    if missing:
        raise ValueError(f"У texts немає полів: {missing}")

    ids = set()
    for point in data["points"]:
        pid = point.get("id")
        if not pid or not point.get("names"):
            raise ValueError(f"Пункт без id або names: {point!r}")
        if pid in ids:
            raise ValueError(f"Дубль id пункту: {pid}")
        ids.add(pid)
        if point.get("level") not in (1, 2, 3):
            raise ValueError(f"Пункт {pid}: level має бути 1, 2 або 3")
        lo, hi = point.get("eta_min"), point.get("eta_max")
        if (lo is None) != (hi is None) or (lo is not None and lo > hi):
            raise ValueError(f"Пункт {pid}: eta_min/eta_max задано невірно")

    return CityProfile(
        id=data["id"],
        short_name=data["short_name"],
        siren_region=data["siren_region"],
        speed_kmh=[float(x) for x in data.get("speed_kmh", [400.0, 480.0])],
        post_lag_min=[float(x) for x in data.get("post_lag_min", [0.5, 1.5])],
        texts=texts,
        stopwords=list(data.get("stopwords", [])),
        target_keywords=list(data["target_keywords"]),
        cancel_keywords=list(data.get("cancel_keywords", [])),
        points=list(data["points"]),
        conventional_speed_kmh=[float(x) for x in data.get("conventional_speed_kmh", [150.0, 185.0])],
        missile_speed_kmh=[float(x) for x in data.get("missile_speed_kmh", [750.0, 880.0])],
    )


def load_profile(name: str, profiles_dir: str | Path) -> CityProfile:
    if not _PROFILE_ID.match(name):
        raise ValueError(f"Некоректне імʼя профілю {name!r}: дозволено малі латинські літери, цифри й дефіс")
    path = Path(profiles_dir) / f"{name}.json"
    profile = parse_profile(json.loads(path.read_text(encoding="utf-8")))
    if profile.id != name:
        raise ValueError(f"Поле id ({profile.id!r}) не збігається з імʼям файлу ({name!r})")
    return profile


@lru_cache(maxsize=1)
def get_profile() -> CityProfile:
    """Return the profile that the settings select."""
    return load_profile(settings.city_profile, settings.profiles_dir)
