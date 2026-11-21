"""Библиотека естественных вводных фраз для голосового ассистента.

Фразы генерирует LLM (один вызов на контекст), синтез — обычный TTS
с пунктуацией. Файлы: data/tts_cache/fillers/<context>/<hash>.mp3,
рядом index.json с текстом, контекстом и датой создания.

Контексты: think (думаю), search (ищу), code (пишу код).
pick(context) возвращает случайную фразу, отличную от последней сыгранной;
если библиотека пуста и генерация не удалась — None (тишина).

Использование:
    from kivi.tts import fillers
    fillers.ensure_library("search")      # прогрев/дозаполнение
    phrase = fillers.pick("search")       # текст или None
    mp3 = fillers.audio_path("search", phrase)
"""

from __future__ import annotations

import hashlib
import json
import random
import shutil
import threading
import time
from pathlib import Path
from typing import Any

from loguru import logger

from kivi.config import DATA_DIR

__all__ = [
    "CONTEXTS",
    "DEFAULT_MIN_COUNT",
    "FILLERS_DIR",
    "MAX_PHRASES",
    "TONE_EXAMPLES",
    "audio_path",
    "ensure_library",
    "last_phrase",
    "mark_played",
    "normalize_context",
    "phrases",
    "pick",
    "reset_state",
]

CONTEXTS: tuple[str, ...] = ("think", "search", "code")
DEFAULT_MIN_COUNT = 4
MAX_PHRASES = 12
FILLERS_DIR = DATA_DIR / "tts_cache" / "fillers"

# Примеры тона (уходят в промпт, а не в озвучку).
TONE_EXAMPLES: dict[str, tuple[str, ...]] = {
    "think": ("Дай-ка подумать.", "Секундочку."),
    "search": ("Уже ищу информацию.", "Сейчас посмотрю."),
    "code": ("Сейчас напишу.", "Пара секунд."),
}

_LOCK = threading.RLock()
_LAST: dict[str, str | None] = {context: None for context in CONTEXTS}
_router = None


def normalize_context(context: str | None) -> str:
    """Неизвестный контекст считаем разговорным (think)."""
    value = str(context or "").strip().lower()
    return value if value in CONTEXTS else "think"


def _default_router():
    """Router проекта создаётся один раз и только при первом LLM-вызове."""
    global _router
    if _router is None:
        from kivi.brain.router import Router
        from kivi.config import get_settings

        _router = Router(get_settings())
    return _router


def _context_dir(context: str) -> Path:
    return FILLERS_DIR / context


def _index_path(context: str) -> Path:
    return _context_dir(context) / "index.json"


def _parse_json(raw: str) -> dict[str, Any] | None:
    """Достать JSON-объект из ответа LLM (снимаем ```-обёртку)."""
    text = str(raw or "").strip()
    if not text:
        return None
    if text.startswith("```"):
        text = "\n".join(
            line for line in text.splitlines() if not line.strip().startswith("```")
        ).strip()
    candidates = [text]
    if "{" in text and "}" in text:
        candidates.append(text[text.find("{") : text.rfind("}") + 1])
    for candidate in candidates:
        if not candidate:
            continue
        try:
            payload = json.loads(candidate)
        except json.JSONDecodeError:
            continue
        if isinstance(payload, dict):
            return payload
    return None


def _clean_phrase(raw: Any) -> str:
    """Фраза — законченное предложение с точкой/восклицательным знаком."""
    text = " ".join(str(raw or "").split()).strip("`\"'«» ")
    if not text:
        return ""
    words = text.split()
    if len(words) > 8:
        text = " ".join(words[:8])
    text = text.rstrip(" ,;:—-")
    if text.endswith("?"):
        text = text[:-1].rstrip() + "."
    elif not text.endswith((".", "!")):
        text += "."
    return text if len(text) >= 6 else ""


def _read_entries(context: str) -> list[dict[str, Any]]:
    """Записи индекса, у которых реально есть mp3 (битые пропускаем)."""
    path = _index_path(context)
    if not path.is_file():
        return []
    try:
        payload = json.loads(path.read_text(encoding="utf-8"))
    except (OSError, json.JSONDecodeError) as exc:
        logger.warning("вводные: битый index.json {}: {}", path, exc)
        return []
    rows = payload.get("phrases") if isinstance(payload, dict) else payload
    entries: list[dict[str, Any]] = []
    for row in list(rows or []):
        if not isinstance(row, dict):
            continue
        text = str(row.get("text") or "").strip()
        name = str(row.get("file") or "").strip()
        if not text or not name:
            continue
        if not (_context_dir(context) / name).is_file():
            continue
        entries.append(
            {
                "text": text,
                "file": name,
                "created": float(row.get("created") or 0.0),
            }
        )
    return entries


def _write_entries(context: str, entries: list[dict[str, Any]]) -> None:
    path = _index_path(context)
    path.parent.mkdir(parents=True, exist_ok=True)
    payload = {
        "context": context,
        "updated": time.time(),
        "phrases": entries,
    }
    temp = path.with_suffix(".json.tmp")
    temp.write_text(json.dumps(payload, ensure_ascii=False, indent=2), encoding="utf-8")
    temp.replace(path)


def _prompt(context: str) -> str:
    examples = " ".join(f"«{item}»" for item in TONE_EXAMPLES.get(context, ()))
    return (
        "Придумай 6 коротких естественных русских фраз-ожиданий для голосового "
        "ассистента (женский, дружелюбный, на ты). Каждая — ЗАКОНЧЕННОЕ "
        "предложение из 2-5 слов с точкой или восклицательным знаком в конце. "
        f"Контекст: {context}. Примеры тона: {examples} Без markdown. "
        'JSON: {"phrases": [...]}'
    )


def _ask_llm(context: str, router: Any | None) -> list[str]:
    """Один вызов LLM на контекст. Возвращает очищенные фразы (может быть [])."""
    try:
        active = router if router is not None else _default_router()
        result = active.complete(
            [
                {"role": "system", "content": _prompt(context)},
                {"role": "user", "content": context},
            ],
            task_type="chat",
        )
    except Exception as exc:  # noqa: BLE001 - без вводных можно жить
        logger.warning("вводные: LLM недоступна ({}): {}: {}", context, type(exc).__name__, exc)
        return []

    payload = _parse_json(str((result or {}).get("text") or ""))
    raw = payload.get("phrases") if payload is not None else None
    if not isinstance(raw, list):
        logger.warning("вводные: LLM вернула не JSON-список фраз ({})", context)
        return []
    seen: set[str] = set()
    phrases_out: list[str] = []
    for item in raw:
        phrase = _clean_phrase(item)
        if phrase and phrase not in seen:
            seen.add(phrase)
            phrases_out.append(phrase)
    return phrases_out


def _default_synth(text: str) -> Path:
    """Синтез обычным TTS (без проигрывания), пунктуация сохраняется."""
    from kivi.tts.speak import _tts_settings, synthesize_mp3

    model_id, voice_id = _tts_settings(None, None)
    return synthesize_mp3(text, model=model_id, voice=voice_id)


def _store_phrase(
    context: str,
    text: str,
    synth: Any | None,
) -> Path | None:
    """Синтезировать фразу и положить mp3 в кэш вводных (без повторов)."""
    target_dir = _context_dir(context)
    target_dir.mkdir(parents=True, exist_ok=True)
    name = hashlib.sha1(text.encode("utf-8")).hexdigest()[:16] + ".mp3"
    target = target_dir / name
    if target.exists() and target.stat().st_size > 0:
        return target
    try:
        maker = synth if synth is not None else _default_synth
        mp3 = Path(maker(text))
    except Exception as exc:  # noqa: BLE001 - одна фраза не критична
        logger.warning("вводные: синтез «{}» не удался: {}: {}", text, type(exc).__name__, exc)
        return None
    if not mp3.is_file() or mp3.stat().st_size == 0:
        logger.warning("вводные: синтез «{}» вернул пустой файл: {}", text, mp3)
        return None
    if mp3.resolve() != target.resolve():
        shutil.copyfile(mp3, target)
    return target


def _entries(context: str) -> list[dict[str, Any]]:
    ctx = normalize_context(context)
    with _LOCK:
        return _read_entries(ctx)


def phrases(context: str) -> list[str]:
    """Тексты библиотеки без генерации (для отчётов и тестов)."""
    return [entry["text"] for entry in _entries(context)]


def audio_path(context: str, text: str) -> Path | None:
    """mp3 для конкретной фразы контекста (None — если её нет в библиотеке)."""
    ctx = normalize_context(context)
    for entry in _entries(ctx):
        if entry["text"] == text:
            path = _context_dir(ctx) / entry["file"]
            return path if path.is_file() else None
    return None


def ensure_library(
    context: str,
    min_count: int = DEFAULT_MIN_COUNT,
    *,
    router: Any | None = None,
    synth: Any | None = None,
) -> list[str]:
    """Дозаполнить библиотеку контекста до min_count фраз (кэп MAX_PHRASES).

    Один вызов LLM и синтез только для новых фраз. Ошибки не бросаем:
    возвращаем то, что уже есть.
    """
    ctx = normalize_context(context)
    required = max(1, int(min_count))
    with _LOCK:
        entries = _read_entries(ctx)
        if len(entries) >= required:
            return [entry["text"] for entry in entries]

        added = 0
        for phrase in _ask_llm(ctx, router):
            if len(entries) >= MAX_PHRASES:
                break
            if any(entry["text"] == phrase for entry in entries):
                continue
            path = _store_phrase(ctx, phrase, synth)
            if path is None:
                continue
            entries.append({"text": phrase, "file": path.name, "created": time.time()})
            added += 1

        if added:
            _write_entries(ctx, entries)
        texts = [entry["text"] for entry in entries]
        logger.info(
            "вводные: {} — {} фраз (добавлено {})", ctx, len(texts), added
        )
        return texts


def pick(context: str) -> str | None:
    """Случайная фраза контекста, отличная от последней сыгранной."""
    ctx = normalize_context(context)
    texts = ensure_library(ctx, min_count=1)
    if not texts:
        return None
    last = _LAST.get(ctx)
    options = [text for text in texts if text != last]
    if not options:
        logger.debug("вводные: {} — нечего выбрать кроме прошлой фразы", ctx)
        return None
    return random.choice(options)


def mark_played(context: str, text: str | None) -> None:
    """Запомнить фразу, которая действительно прозвучала."""
    if not text:
        return
    ctx = normalize_context(context)
    _LAST[ctx] = str(text)


def last_phrase(context: str = "think") -> str | None:
    """Последняя сыгранная фраза контекста (для логов и тестов)."""
    return _LAST.get(normalize_context(context))


def reset_state(context: str | None = None) -> None:
    """Сбросить память о последней фразе (для тестов и перезапуска)."""
    if context is None:
        for key in _LAST:
            _LAST[key] = None
        return
    _LAST[normalize_context(context)] = None
