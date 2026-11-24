"""Мгновенные маршруты для тривиальных реплик: привет / спасибо / как дела.

Такие фразы не требуют планировщика и LLM: ответ берётся из готовой
библиотеки kivi.tts.replies (озвучка уже в кэше). Это убирает задержку
на «привет» и «спасибо» — самый частый разговорный вход.

Использование:
    from kivi.brain.fastpath import fast_route
    fast_route("привет")   # {"route": "chat", "fast": "greeting"}
    fast_route("какой курс биткоина")   # None -> обычный путь через planner
"""

from __future__ import annotations

import re

__all__ = ["FAST_KINDS", "RULES", "fast_route"]

# Фразы сравниваем целиком (match, не search): «привет, что по погоде»
# уходит в обычный путь, потому что там есть настоящая команда.
RULES: list[tuple[re.Pattern[str], str]] = [
    (
        re.compile(
            r"^(привет|здравствуй|здравствуйте|хай|приветик|ку|"
            r"добрый (день|вечер|утро))[!., ]*$"
        ),
        "greeting",
    ),
    (
        re.compile(r"^(спасибо|благодарю|пасиб|спс)[!., ]*$"),
        "thanks",
    ),
    (
        re.compile(r"^(как дела|как ты|как жизнь|что нового)[?!., ]*$"),
        "smalltalk",
    ),
]

FAST_KINDS: tuple[str, ...] = ("greeting", "thanks", "smalltalk")


def fast_route(command: str) -> dict[str, str] | None:
    """Вернуть {"route": "chat", "fast": kind} или None для обычного пути."""
    text = " ".join(str(command or "").split()).lower()
    if not text:
        return None
    for pattern, kind in RULES:
        if pattern.match(text):
            return {"route": "chat", "fast": kind}
    return None
