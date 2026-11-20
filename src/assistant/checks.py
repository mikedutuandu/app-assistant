"""Fact and rule checks: pure functions, no I/O, no models."""

import re
from typing import Literal

from pydantic import BaseModel, Field
from ulid import ULID

from assistant.packs import WriteSpec


class Fact(BaseModel):
    id: str = Field(default_factory=lambda: str(ULID()))
    text: str = ""
    numbers: list[float] = []
    source: Literal["typed", "voice", "image", "pdf", "link"] = "typed"
    file_id: str | None = None


_UNITS = {
    "one": 1,
    "two": 2,
    "three": 3,
    "four": 4,
    "five": 5,
    "six": 6,
    "seven": 7,
    "eight": 8,
    "nine": 9,
}
_WORDS = {
    **{k: v for k, v in _UNITS.items() if k != "one"},  # a lone "one" is a pronoun, not a count
    "ten": 10,
    "eleven": 11,
    "twelve": 12,
    "thirteen": 13,
    "fourteen": 14,
    "fifteen": 15,
    "sixteen": 16,
    "seventeen": 17,
    "eighteen": 18,
    "nineteen": 19,
    "twenty": 20,
}
_TENS = {"thirty": 30, "forty": 40, "fifty": 50, "sixty": 60, "seventy": 70, "eighty": 80, "ninety": 90}
_MULT = {"hundred": 100, "thousand": 1000}

_TENS_RE = "|".join([*_TENS, "twenty"])
_UNITS_RE = "|".join(_UNITS)
_WORDS_RE = "|".join(sorted(_WORDS, key=len, reverse=True))
_ALL_TENS_RE = "|".join(_TENS)

_NUMBER_RE = re.compile(
    rf"""
    (?P<digits>(?<!\d)(?P<num>\d{{1,3}}(?:,\d{{3}})+(?:\.\d+)?|\d+(?:\.\d+)?)
        (?:(?P<suffix>[kKmM])\b|\s*(?P<mult>hundred|thousand)\b)?)
    |
    (?P<word>\b(?:(?:{_TENS_RE})(?:[-\s](?:{_UNITS_RE}))?|{_ALL_TENS_RE}|{_WORDS_RE})\b
        (?:\s+(?P<wmult>hundred|thousand)\b)?)
    """,
    re.IGNORECASE | re.VERBOSE,
)
_YEAR_SAFE_AFTER = re.compile(r"[%kKmMxX×hH]|\s+(?:hours?|hrs?)\b", re.IGNORECASE)


def _word_value(word: str) -> float:
    parts = re.split(r"[-\s]+", word.lower())
    return float(sum(_TENS.get(p) or _WORDS.get(p) or _UNITS.get(p) or 0 for p in parts))


def numbers_in(text: str) -> list[float]:
    """Every number a text states, normalised, in reading order."""
    found: list[float] = []
    for m in _NUMBER_RE.finditer(text):
        if m.group("digits") is not None:
            raw = m.group("num")
            value = float(raw.replace(",", ""))
            is_plain_int = raw.isdigit() and len(raw) == 4
            if (
                is_plain_int
                and 1900 <= value <= 2100
                and not m.group("suffix")
                and not m.group("mult")
                and not _YEAR_SAFE_AFTER.match(text, m.end())
            ):
                continue
            suffix = (m.group("suffix") or "").lower()
            if suffix == "k":
                value *= 1_000
            elif suffix == "m":
                value *= 1_000_000
            elif m.group("mult"):
                value *= _MULT[m.group("mult").lower()]
        else:
            word = m.group("word")
            mult = m.group("wmult")
            if mult:
                word = word[: m.start("wmult") - m.start("word")].strip()
            value = _word_value(word)
            if mult:
                value *= _MULT[mult.lower()]
        found.append(round(value, 6))
    return found


def _close(a: float, b: float) -> bool:
    if a == 0 and b == 0:
        return True
    return abs(a - b) <= 0.005 * max(abs(a), abs(b))


def unsupported_numbers(text: str, facts: list[Fact]) -> list[float]:
    """Numbers in the text that no fact states (within 0.5%), in reading order, once each."""
    known = [n for fact in facts for n in fact.numbers]
    missing: list[float] = []
    for n in numbers_in(text):
        if n not in missing and not any(_close(n, k) for k in known):
            missing.append(n)
    return missing


def _words(text: str) -> set[str]:
    return set(re.findall(r"[^\W\d_]{3,}", text.lower()))


def repeats(text: str, recent: list[str], threshold: float = 0.6) -> str | None:
    """The first recent text whose word overlap with this one reaches the threshold."""
    mine = _words(text)
    if not mine:
        return None
    for other in recent:
        theirs = _words(other)
        if not theirs:
            continue
        if len(mine & theirs) / len(mine | theirs) >= threshold:
            return other
    return None


_TLDS = (
    "com|org|net|io|ai|co|app|dev|me|xyz|ly|to|gg|sh|so|us|uk|info|biz|tv|vn|edu|gov|in|de|fr|es|it|nl|ca|au|"
    "fm|cc|ws|site|online|tech|page|blog|link|store|shop"
)
_END = r"[^\s.,;:!?)\"']"
_URL_RE = re.compile(
    rf"https?://{_END}(?:\S*{_END})?"
    rf"|\b(?:[A-Za-z0-9-]+\.)+(?:{_TLDS})\b(?![A-Za-z0-9-])(?:/(?:\S*{_END})?)?",
    re.IGNORECASE,
)
_URL_LENGTH = 23


def _x_length(text: str) -> int:
    return len(_URL_RE.sub("x" * _URL_LENGTH, text))


def rule_problems(text: str, spec: WriteSpec) -> list[str]:
    problems: list[str] = []
    if spec.max_chars is not None and _x_length(text) > spec.max_chars:
        problems.append("too_long")
    if spec.no_links and _URL_RE.search(text):
        problems.append("has_link")
    if spec.no_dashes and ("—" in text or "–" in text):
        problems.append("has_dash")
    return problems
