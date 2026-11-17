"""Распознавание речи через OpenAI-совместимый эндпоинт /audio/transcriptions.

Только requests: локальный faster-whisper и torch здесь не используются.
Ключи берутся из окружения (.env подгружается штатно, как в tests/check_api.py),
значения ключей никогда не логируются.

Использование:
    from kivi.stt.transcribe import transcribe_wav
    text = transcribe_wav("data/last_utterance.wav")
"""

from __future__ import annotations

import os
from pathlib import Path

import requests
from dotenv import load_dotenv

from kivi.log import logger

__all__ = ["DEFAULT_TIMEOUT", "STT_MODEL", "TranscribeError", "transcribe_wav"]

STT_MODEL = "openai/whisper-large-v3-turbo"
DEFAULT_TIMEOUT = 120.0
MIN_WAV_BYTES = 44  # заголовок WAV


class TranscribeError(RuntimeError):
    """Распознавание не удалось (нет ключей, сеть, HTTP-ошибка, пустой ответ)."""


def _credentials() -> tuple[str, str]:
    """Адрес API и ключ из окружения. Значения ключа не печатаем никогда."""
    load_dotenv()
    base = (os.getenv("LLM_BASE_URL") or "").strip()
    key = (os.getenv("LLM_API_KEY") or "").strip()
    if not base or not key:
        raise TranscribeError(
            "Не заданы LLM_BASE_URL и/или LLM_API_KEY в окружении (.env). "
            "Проверь файл .env и запусти из активированного .venv."
        )
    return base.rstrip("/"), key


def transcribe_wav(
    path: str | Path,
    *,
    model: str = STT_MODEL,
    language: str | None = None,
    timeout: float = DEFAULT_TIMEOUT,
) -> str:
    """Распознать wav (16000 Гц, моно) и вернуть текст."""
    wav_path = Path(path)
    if not wav_path.exists():
        raise TranscribeError(f"Нет файла для распознавания: {wav_path}")

    payload = wav_path.read_bytes()
    if len(payload) < MIN_WAV_BYTES:
        raise TranscribeError(
            f"Файл слишком мал для WAV ({len(payload)} байт): {wav_path}"
        )

    try:
        import soundfile as sf

        info = sf.info(str(wav_path))
        if info.samplerate != 16000 or info.channels != 1:
            logger.warning(
                "STT: ожидался wav 16000 Гц моно, а в {} — {} Гц, каналов {}",
                wav_path.name,
                info.samplerate,
                info.channels,
            )
    except Exception as exc:  # noqa: BLE001 - проверка необязательная
        logger.debug("STT: не удалось прочитать заголовок {}: {}", wav_path.name, exc)

    base, key = _credentials()
    url = f"{base}/audio/transcriptions"
    data = {"model": model}
    if language:
        data["language"] = language

    logger.debug("STT: отправляю {} ({:.1f} КБ) на {}", wav_path.name, len(payload) / 1024, url)
    try:
        response = requests.post(
            url,
            headers={"Authorization": f"Bearer {key}"},
            files={"file": (wav_path.name, payload, "audio/wav")},
            data=data,
            timeout=timeout,
        )
    except requests.RequestException as exc:
        raise TranscribeError(f"Не удалось связаться с {url}: {exc}") from exc

    if response.status_code != 200:
        raise TranscribeError(
            f"HTTP {response.status_code} от {url}: {response.text[:300]}"
        )

    try:
        body = response.json()
    except ValueError:
        # Некоторые провайдеры отвечают простым текстом.
        text = response.text.strip()
        if text:
            logger.info("STT: {}", text)
            return text
        raise TranscribeError("Ответ сервера пустой и не является JSON") from None

    if not isinstance(body, dict) or "text" not in body:
        raise TranscribeError(f"В ответе нет поля 'text': {str(body)[:300]}")

    text = str(body["text"]).strip()
    logger.info("STT: {} -> {!r}", wav_path.name, text)
    return text
