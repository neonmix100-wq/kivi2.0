"""Готовые короткие реплики для fast-path: привет / спасибо / smalltalk.

Каждая фраза синтезируется один раз через kivi.tts.cache и дальше играется
с диска без обращения к сети. Индекс: data/tts_cache/replies/index.json.

Здесь только «социальные» фразы без фактов — кэшировать ответы с фактами
запрещено (они устаревают).

Использование:
    from kivi.tts import replies
    replies.ensure_replies()          # прогрев на старте (TTS один раз)
    replies.pick("greeting")          # ("Привет, Лёша! Чем займёмся?", "....mp3")
"""

from __future__ import annotations

import json
import random
import threading
from pathlib import Path

from kivi.config import DATA_DIR
from kivi.log import logger
from kivi.tts import cache

__all__ = ["DIR", "INDEX", "LIB", "ensure_replies", "pick", "reset_state"]

DIR = DATA_DIR / "tts_cache" / "replies"
INDEX = DIR / "index.json"

LIB: dict[str, tuple[str, ...]] = {
    "greeting": (
        "Привет, Лёша! Чем займёмся?",
        "Привет! Я на связи.",
        "Здравствуй! Что делаем?",
    ),
    "thanks": (
        "Пожалуйста!",
        "Всегда пожалуйста.",
        "Обращайся!",
    ),
    "smalltalk": (
        "Всё отлично, готова помогать. А у тебя как?",
        "В порядке! Чем займёмся?",
    ),
}

_last: dict[str, str] = {}
_lock = threading.Lock()


def ensure_replies() -> dict[str, list[dict[str, str]]]:
    """Синтезировать все реплики (из кэша — бесплатно) и обновить index.json."""
    DIR.mkdir(parents=True, exist_ok=True)
    index: dict[str, list[dict[str, str]]] = {}
    for kind, texts in LIB.items():
        items: list[dict[str, str]] = []
        for text in texts:
            mp3 = cache.synthesize_cached(text)
            items.append({"text": text, "mp3": str(Path(mp3))})
        index[kind] = items
    temp = INDEX.with_suffix(".json.tmp")
    temp.write_text(
        json.dumps(index, ensure_ascii=False, indent=2), encoding="utf-8"
    )
    temp.replace(INDEX)
    logger.debug("реплики: индекс обновлён ({})", INDEX)
    return index


def pick(kind: str) -> tuple[str, str] | None:
    """Случайная реплика вида (текст, путь к mp3); None — библиотеки нет."""
    try:
        payload = json.loads(INDEX.read_text(encoding="utf-8"))
    except (OSError, json.JSONDecodeError):
        return None
    rows = payload.get(str(kind)) if isinstance(payload, dict) else None
    items = [
        row
        for row in list(rows or [])
        if isinstance(row, dict)
        and str(row.get("text") or "").strip()
        and Path(str(row.get("mp3") or "")).is_file()
    ]
    if not items:
        return None
    with _lock:
        pool = [row for row in items if row["text"] != _last.get(kind)] or items
        chosen = random.choice(pool)
        _last[kind] = str(chosen["text"])
    return str(chosen["text"]), str(chosen["mp3"])


def reset_state() -> None:
    """Забыть последние фразы (используется смоуками)."""
    with _lock:
        _last.clear()
