"""Распознавание речи: локальный faster-whisper с откатом на облако Polza.

Движки выбираются в config.yaml:
    stt:
      primary: local
      fallback: polza

Использование:
    from kivi.stt import transcribe
    text = transcribe("data/last_utterance.wav")
"""

from __future__ import annotations

from pathlib import Path

from kivi.config import get_settings
from kivi.log import logger
from kivi.stt import local as local_engine
from kivi.stt.transcribe import transcribe_wav as transcribe_cloud

__all__ = [
    "STTError",
    "transcribe",
    "transcribe_cloud",
    "transcribe_local",
]


class STTError(RuntimeError):
    """Ни один движок распознавания не справился."""


def transcribe_local(path: str | Path, *, language: str | None = None) -> str:
    """Локальный faster-whisper (CUDA, при недоступности — CPU)."""
    return local_engine.transcribe_wav(path, language=language)


def transcribe(
    path: str | Path,
    *,
    engine: str | None = None,
    language: str | None = None,
) -> str:
    """Распознать файл движком из конфига, с автоматическим откатом."""
    settings = get_settings()
    stt_cfg = settings.section("stt")
    primary = str(engine or stt_cfg.get("primary") or "local").strip().lower()
    fallback = str(stt_cfg.get("fallback") or "polza").strip().lower()
    lang = language or str(stt_cfg.get("language") or "ru")
    wav_path = Path(path)

    engines = {
        "local": lambda: transcribe_local(wav_path, language=lang),
        "polza": lambda: transcribe_cloud(wav_path, language=lang),
    }

    if primary not in engines:
        raise STTError(f"Неизвестный основной движок STT: {primary!r}")
    if fallback not in engines or fallback == primary:
        fallback = "polza" if primary == "local" else "local"

    logger.info("STT: движок {}, откат {}", primary, fallback)
    try:
        return engines[primary]()
    except Exception as exc:  # noqa: BLE001 - откатываемся на резервный движок
        logger.warning(
            "STT: движок {} не справился ({}: {}) — переключаюсь на {}",
            primary,
            type(exc).__name__,
            exc,
            fallback,
        )
        try:
            text = engines[fallback]()
        except Exception as exc2:  # noqa: BLE001
            raise STTError(
                f"Ни один движок STT не справился: {primary} -> {exc}; "
                f"{fallback} -> {exc2}"
            ) from exc2
        logger.info("STT: распознано резервным движком {}", fallback)
        return text

