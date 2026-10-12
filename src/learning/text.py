"""Text normalization, rough stemming of place names and search for name candidates."""
from __future__ import annotations

import re

_APOSTROPHES = re.compile(r"[’'`ʹ]")
_SPACES = re.compile(r"\s+")

# Case endings that occur most often in point names.
# Order: from longer to shorter.
_ENDINGS = sorted(
    [
        "ями", "ами", "ові", "еві", "єві", "ого", "ому", "ець", "ою", "ею", "ій", "ий",
        "ах", "ях", "ів", "їв", "ам", "ям", "ом", "ем", "ці", "цю", "ця",
        "а", "я", "у", "ю", "і", "е", "о", "и", "ь",
    ],
    key=len,
    reverse=True,
)
_MIN_STEM = 3
_MIN_PREFIX_MATCH = 4

_UPPER = "А-ЩЬЮЯҐЄІЇ"
_LOWER = "а-щьюяґєіїʼ'’"
_CAP_WORD = rf"[{_UPPER}][{_LOWER}]+"
_PLACE = rf"{_CAP_WORD}(?:(?:-|\s){_CAP_WORD})?"
_PREPOSITIONS = (
    r"(?i:курсом\s+на|у\s+бік|в\s+бік|в\s+районі|у\s+районі|в\s+напрямку|"
    r"на|через|біля|повз|над|до|між)"
)
_AFTER_PREP_RE = re.compile(rf"(?:^|[\s(,;:—–-]){_PREPOSITIONS}\s+({_PLACE})")
# A line that has only a name (typical format of monitoring channels):
# "🛵 Борщів", "Тлумач.", "— Скала-Подільська"
_LINE_ONLY_RE = re.compile(rf"^[^\w{_UPPER}]*({_PLACE})[\s.!,:;)]*$", re.MULTILINE)
_REGION_SUFFIXES = ("щина", "ччина", "щини", "ччини", "щину", "ччину", "щині", "ччині")


def normalize(text: str) -> str:
    text = _APOSTROPHES.sub("ʼ", text.lower())
    text = re.sub(r"\.(?=\S)", ". ", text)  # "н.ушиця" -> "н. ушиця"
    return _SPACES.sub(" ", text).strip()


def stem_word(word: str) -> str:
    for ending in _ENDINGS:
        if word.endswith(ending) and len(word) - len(ending) >= _MIN_STEM:
            return word[: -len(ending)]
    return word


def stem_phrase(phrase: str) -> list[str]:
    return [stem_word(w) for w in re.split(r"[\s\-]+", normalize(phrase)) if w]


def _stems_match(a: str, b: str) -> bool:
    if a == b:
        return True
    short, long_ = (a, b) if len(a) <= len(b) else (b, a)
    return len(short) >= _MIN_PREFIX_MATCH and long_.startswith(short)


def phrases_match(a: str, b: str) -> bool:
    """True if two phrases can be grammatical cases of one name ("Нову Ушицю" ~ "Нова Ушиця").

    This is a heuristic. It gives candidates to a person for review. It is not a final decision.
    """
    sa, sb = stem_phrase(a), stem_phrase(b)
    return len(sa) == len(sb) and all(_stems_match(x, y) for x, y in zip(sa, sb))


def is_region_name(phrase: str) -> bool:
    return normalize(phrase).endswith(_REGION_SUFFIXES)


def extract_place_candidates(text: str) -> list[str]:
    """Return the phrases from the original text that look like names of settlements."""
    found: list[str] = []
    for regex in (_AFTER_PREP_RE, _LINE_ONLY_RE):
        for match in regex.finditer(text):
            phrase = match.group(1).strip()
            if phrase not in found:
                found.append(phrase)
    return found
