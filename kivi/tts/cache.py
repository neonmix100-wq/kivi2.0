"""Кэш фиксированных TTS-фраз: синтез один раз, дальше — мгновенное воспроизведение.

Ключ: sha256(model + voice + text). Файл: data/tts_cache/<hash>.mp3.
Если файл уже есть — API не вызывается, mp3 сразу играется через ffplay.
Экономит деньги и убирает задержку на служебных фразах («Слушаю», «Готово»).

Использование:
    from kivi.tts.cache import speak_cached
    speak_cached("Слушаю")
"""

from __future__ import annotations

import hashlib
import shutil
from pathlib import Path

from kivi.config import DATA_DIR
from kivi.log import logger
from kivi.tts.speak import (
    SpeakError,
    _tts_settings,
    play_file,
    speak,
    speech_slot,
    synthesize_mp3,
)

__all__ = [
    "CACHE_DIR",
    "SKILLS_CACHE_DIR",
    "cache_key",
    "cache_path",
    "play_mp3",
    "skill_cache_key",
    "skill_cache_path",
    "speak_cached",
    "speak_skill_cached",
    "synthesize_cached",
    "synthesize_skill_cached",
]

CACHE_DIR = DATA_DIR / "tts_cache"
SKILLS_CACHE_DIR = CACHE_DIR / "skills"  # детерминированные ответы скиллов (без лимита длины)
MAX_CACHE_CHARS = 80  # длинные уникальные ответы не кэшируем — они засоряют диск
SERVICE_PHRASES: tuple[str, ...] = ("Слушаю", "Готово", "Не расслышала, повтори")


def cache_key(model: str, voice: str | None, text: str) -> str:
    """sha256 от model + voice + text."""
    raw = f"{model}|{voice or ''}|{' '.join(str(text).split())}"
    return hashlib.sha256(raw.encode("utf-8")).hexdigest()


def cache_path(model: str, voice: str | None, text: str) -> Path:
    return CACHE_DIR / f"{cache_key(model, voice, text)}.mp3"


def play_mp3(path: Path) -> float:
    """Проиграть mp3 общим плеером модуля озвучки (единственный ffplay)."""
    return play_file(path)


def synthesize_cached(
    text: str, *, model: str | None = None, voice: str | None = None
) -> Path:
    """Синтезировать фразу и положить mp3 в кэш. Если уже есть — вернуть готовый файл.

    Длинные фразы (>= MAX_CACHE_CHARS) не кэшируются: они уникальны.
    """
    phrase = " ".join(str(text).split())
    if not phrase:
        raise SpeakError("Пустой текст для озвучки")
    model_id, voice_id = _tts_settings(model, voice)
    if len(phrase) >= MAX_CACHE_CHARS:
        logger.debug(
            "TTS cache: фраза {} символов — синтез без кэша", len(phrase)
        )
        return synthesize_mp3(phrase, model=model_id, voice=voice_id)

    target = cache_path(model_id, voice_id, phrase)
    if target.exists() and target.stat().st_size > 0:
        logger.debug("TTS cache: попадание {!r} -> {}", phrase, target.name)
        return target

    logger.info("TTS cache: синтезирую {!r} (один раз, дальше бесплатно)", phrase)
    mp3_path = synthesize_mp3(phrase, model=model_id, voice=voice_id)
    CACHE_DIR.mkdir(parents=True, exist_ok=True)
    shutil.copyfile(mp3_path, target)
    logger.info("TTS cache: сохранено {} ({:.1f} КБ)", target.name, target.stat().st_size / 1024)
    return target


def skill_cache_key(text: str) -> str:
    """sha256 только от текста: ответы скиллов детерминированы."""
    normalized = " ".join(str(text).split())
    return hashlib.sha256(normalized.encode("utf-8")).hexdigest()


def skill_cache_path(text: str) -> Path:
    return SKILLS_CACHE_DIR / f"{skill_cache_key(text)}.mp3"


def synthesize_skill_cached(
    text: str, *, model: str | None = None, voice: str | None = None
) -> Path:
    """Синтез ответа скилла с кэшем БЕЗ ограничения длины (data/tts_cache/skills)."""
    phrase = " ".join(str(text).split())
    if not phrase:
        raise SpeakError("Пустой текст для озвучки")
    model_id, voice_id = _tts_settings(model, voice)
    target = skill_cache_path(phrase)
    if target.exists() and target.stat().st_size > 0:
        logger.debug("TTS cache(skill): попадание {!r}", phrase[:60])
        return target
    logger.info(
        "TTS cache(skill): синтезирую {} симв. (один раз, дальше бесплатно)",
        len(phrase),
    )
    mp3_path = synthesize_mp3(phrase, model=model_id, voice=voice_id)
    SKILLS_CACHE_DIR.mkdir(parents=True, exist_ok=True)
    shutil.copyfile(mp3_path, target)
    logger.info(
        "TTS cache(skill): сохранено {} ({:.1f} КБ)",
        target.name,
        target.stat().st_size / 1024,
    )
    return target


def speak_skill_cached(
    text: str, *, model: str | None = None, voice: str | None = None
) -> Path:
    """Сказать ответ скилла: из кэша (sha256 текста) или синтез один раз."""
    phrase = " ".join(str(text).split())
    if not phrase:
        raise SpeakError("Пустой текст для озвучки")
    model_id, voice_id = _tts_settings(model, voice)
    target = skill_cache_path(phrase)
    with speech_slot(phrase) as (run_id, state):
        if target.exists() and target.stat().st_size > 0:
            logger.debug("TTS cache(skill): играю из кэша {!r}", phrase[:60])
        else:
            target = synthesize_skill_cached(phrase, model=model_id, voice=voice_id)
        state["duration"] = play_file(target, run_id=run_id)
    logger.info("TTS cache(skill): сказано {!r}", phrase[:60])
    return target


def speak_cached(
    text: str, *, model: str | None = None, voice: str | None = None
) -> Path:
    """Сказать фразу: короткую — из кэша, длинную — обычным speak().

    Проигрывание ровно одно: speak()/play_file уже играют сами.
    """
    phrase = " ".join(str(text).split())
    if not phrase:
        raise SpeakError("Пустой текст для озвучки")
    model_id, voice_id = _tts_settings(model, voice)

    if len(phrase) >= MAX_CACHE_CHARS:
        logger.debug(
            "TTS cache: длинная фраза ({} симв.) — отдаю в speak() без кэша",
            len(phrase),
        )
        result = speak(phrase, model=model_id, voice=voice_id)
        return result.mp3_path

    target = cache_path(model_id, voice_id, phrase)
    with speech_slot(phrase) as (run_id, state):
        if target.exists() and target.stat().st_size > 0:
            logger.debug("TTS cache: играю из кэша {!r}", phrase)
        else:
            target = synthesize_cached(phrase, model=model_id, voice=voice_id)
        state["duration"] = play_file(target, run_id=run_id)
    logger.info("TTS cache: сказано {!r}", phrase)
    return target
