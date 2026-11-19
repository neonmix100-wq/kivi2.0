"""Детектор имени «Киви» в распознанном тексте.

Никаких моделей и зависимостей: нормализация + точные варианты имени
+ нечёткое совпадение по расстоянию Левенштейна.

Использование:
    from kivi.wake.detector import is_wake
    found, command = is_wake("Киви, включи музыку")   # (True, "включи музыку")
"""

from __future__ import annotations

import re
from dataclasses import dataclass

__all__ = ["FILLERS", "WAKE_WORDS", "WakeMatch", "detect", "is_wake", "levenshtein", "normalize"]

# Варианты имени: как слышит Whisper и как пишут латиницей.
WAKE_WORDS: frozenset[str] = frozenset(
    {"киви", "киве", "кеви", "кивy", "kiwi", "kivi"}
)
# Если после имени только эти слова — команды нет, просто позвали.
FILLERS: frozenset[str] = frozenset({"эй", "слушай", "привет"})
BASE_WORD = "киви"
FUZZY_LENGTH = 4  # нечёткое сравнение только для слов из 4 букв
FUZZY_DISTANCE = 1
SEARCH_TOKENS = 3  # имя ищем только в первых трёх токенах

_PUNCT_RE = re.compile(r"[^\w\s]", re.UNICODE)
# Только знаки ударения. Breve (U+0306) — это «й», его трогать нельзя.
_ACCENT_MARKS = {"\u0300", "\u0301", "\u0341"}  # grave, acute, double acute


def normalize(text: str) -> str:
    """Нижний регистр, без пунктуации и ударений, ё -> е. Латиницу не трогаем."""
    lowered = str(text).lower().replace("ё", "е")
    # Ударение в русском тексте — отдельный combining-символ после гласной.
    without_accents = "".join(ch for ch in lowered if ch not in _ACCENT_MARKS)
    cleaned = _PUNCT_RE.sub(" ", without_accents)
    return " ".join(cleaned.split())


def levenshtein(a: str, b: str) -> int:
    """Классическое расстояние редактирования (вставка/удаление/замена)."""
    if a == b:
        return 0
    if not a:
        return len(b)
    if not b:
        return len(a)
    if len(a) > len(b):
        a, b = b, a
    previous = list(range(len(a) + 1))
    for j, char_b in enumerate(b, start=1):
        current = [j]
        for i, char_a in enumerate(a, start=1):
            cost = 0 if char_a == char_b else 1
            current.append(
                min(
                    previous[i] + 1,       # удаление
                    current[i - 1] + 1,    # вставка
                    previous[i - 1] + cost,  # замена
                )
            )
        previous = current
    return previous[-1]


@dataclass(frozen=True)
class WakeMatch:
    """Результат поиска имени во фразе."""

    is_wake: bool
    command: str
    fuzzy: bool = False
    word: str = ""

    def __bool__(self) -> bool:
        return self.is_wake


def _match_token(token: str) -> tuple[bool, bool]:
    """(совпало ли, нечёткое ли совпадение)."""
    if token in WAKE_WORDS:
        return True, False
    if len(token) == FUZZY_LENGTH and levenshtein(token, BASE_WORD) <= FUZZY_DISTANCE:
        return True, True
    return False, False


def detect(text: str) -> WakeMatch:
    """Найти имя и вернуть остаток фразы как команду."""
    normalized = normalize(text)
    if not normalized:
        return WakeMatch(False, "")

    tokens = normalized.split()
    for index, token in enumerate(tokens[:SEARCH_TOKENS]):
        matched, fuzzy = _match_token(token)
        if not matched:
            continue
        rest = tokens[index + 1 :]
        # «Киви, эй, слушай» — это всё ещё просто зов, команды нет.
        while rest and rest[0] in FILLERS:
            rest = rest[1:]
        return WakeMatch(True, " ".join(rest), fuzzy, token)

    return WakeMatch(False, "")


def is_wake(text: str) -> tuple[bool, str]:
    """Совместимый интерфейс: (нашли ли имя, команда после имени)."""
    match = detect(text)
    return match.is_wake, match.command
