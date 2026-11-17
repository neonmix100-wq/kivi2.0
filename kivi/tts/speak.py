"""Озвучка: TTS -> mp3 -> ffmpeg -> s16le 48000 -> наушники через sounddevice.

Эндпоинт OpenAI-совместимый, POST {LLM_BASE_URL}/audio/speech. Ответ приходит
JSON-ом {"audio": "<base64>"} — base64 обязательно декодировать.
Ключи берутся из окружения (.env подгружается штатно, как в tests/check_api.py),
значения ключей никогда не логируются.

Использование:
    from kivi.tts.speak import speak
    speak("Привет, Лёша. Я Киви.")
"""

from __future__ import annotations

import base64
import binascii
import json
import os
import shutil
import subprocess
from dataclasses import dataclass
from pathlib import Path

import numpy as np
import requests
import sounddevice as sd
from dotenv import load_dotenv

from kivi.config import DATA_DIR, get_settings
from kivi.log import logger

__all__ = ["DEFAULT_TIMEOUT", "SpeakError", "Speech", "speak"]

TMP_DIR = DATA_DIR / "tmp"
MP3_PATH = TMP_DIR / "tts_speech.mp3"
PCM_PATH = TMP_DIR / "tts_speech.pcm"
DEFAULT_TIMEOUT = 180.0
MIN_AUDIO_BYTES = 1000


class SpeakError(RuntimeError):
    """Озвучка не удалась (нет ключей, сеть, HTTP-ошибка, ffmpeg, устройство вывода)."""


@dataclass(frozen=True)
class Speech:
    """Результат озвучки."""

    text: str
    mp3_path: Path
    pcm_path: Path
    samplerate: int
    duration_s: float
    peak: float

    def __str__(self) -> str:
        return (
            f"{self.duration_s:.2f} c, {self.samplerate} Гц, пик {self.peak:.2f} "
            f"({self.mp3_path.name})"
        )


def _credentials() -> tuple[str, str]:
    """Адрес API и ключ из окружения. Значения ключа не печатаем никогда."""
    load_dotenv()
    base = (os.getenv("LLM_BASE_URL") or "").strip()
    key = (os.getenv("LLM_API_KEY") or "").strip()
    if not base or not key:
        raise SpeakError(
            "Не заданы LLM_BASE_URL и/или LLM_API_KEY в окружении (.env). "
            "Проверь файл .env и запусти из активированного .venv."
        )
    return base.rstrip("/"), key


def _ffmpeg() -> str:
    exe = shutil.which("ffmpeg")
    if not exe:
        raise SpeakError(
            "ffmpeg не найден в PATH. Он нужен для перевода mp3 в s16le 48000 Гц."
        )
    return exe


def synthesize_mp3(
    text: str,
    *,
    model: str,
    voice: str | None,
    timeout: float = DEFAULT_TIMEOUT,
) -> Path:
    """Запросить TTS и сохранить mp3 во временный файл data/tmp/tts_speech.mp3."""
    base, key = _credentials()
    url = f"{base}/audio/speech"
    payload: dict[str, str] = {
        "model": model,
        "input": text,
        "response_format": "mp3",
    }
    if voice:
        payload["voice"] = voice

    logger.debug("TTS: {} символов, model={}, voice={}", len(text), model, voice)
    try:
        response = requests.post(
            url,
            headers={"Authorization": f"Bearer {key}"},
            json=payload,
            timeout=timeout,
        )
    except requests.RequestException as exc:
        raise SpeakError(f"Не удалось связаться с {url}: {exc}") from exc

    if response.status_code != 200:
        raise SpeakError(f"HTTP {response.status_code} от {url}: {response.text[:300]}")

    audio_bytes: bytes | None = None
    try:
        body = json.loads(response.content)
    except ValueError:
        body = None

    if isinstance(body, dict) and "audio" in body:
        try:
            audio_bytes = base64.b64decode(body["audio"])
        except (binascii.Error, TypeError) as exc:
            raise SpeakError(f"Не удалось декодировать base64 из ответа: {exc}") from exc
        logger.debug("TTS: JSON->base64->mp3, {} байт", len(audio_bytes))
    else:
        # Некоторые модели отвечают сырым аудио.
        audio_bytes = response.content
        logger.debug("TTS: сырой ответ, {} байт", len(audio_bytes))

    size = 0 if not audio_bytes else len(audio_bytes)
    if size < MIN_AUDIO_BYTES:
        raise SpeakError(f"Слишком короткий аудиоответ: {size} байт")

    TMP_DIR.mkdir(parents=True, exist_ok=True)
    MP3_PATH.write_bytes(audio_bytes)
    logger.info("TTS: mp3 готов ({:.1f} КБ): {}", size / 1024, MP3_PATH)
    return MP3_PATH


def mp3_to_pcm(mp3_path: Path, *, samplerate: int, pcm_path: Path = PCM_PATH) -> Path:
    """ffmpeg: mp3 -> сырой s16le, моно, заданная частота."""
    exe = _ffmpeg()
    pcm_path.parent.mkdir(parents=True, exist_ok=True)
    command = [
        exe,
        "-y",
        "-loglevel",
        "error",
        "-i",
        str(mp3_path),
        "-ar",
        str(int(samplerate)),
        "-ac",
        "1",
        "-f",
        "s16le",
        str(pcm_path),
    ]
    try:
        completed = subprocess.run(command, capture_output=True, check=False)
    except OSError as exc:
        raise SpeakError(f"Не удалось запустить ffmpeg: {exc}") from exc
    if completed.returncode != 0:
        message = completed.stderr.decode("utf-8", errors="replace").strip()
        raise SpeakError(
            f"ffmpeg завершился с кодом {completed.returncode}: {message[:300]}"
        )
    if not pcm_path.exists() or pcm_path.stat().st_size == 0:
        raise SpeakError(f"ffmpeg не создал pcm: {pcm_path}")
    return pcm_path


def play_pcm(
    pcm_path: Path, *, samplerate: int, device_index: int | None = None
) -> tuple[float, float]:
    """Прочитать s16le и проиграть на устройство вывода. Блокирует до конца."""
    raw = pcm_path.read_bytes()
    samples = np.frombuffer(raw, dtype="<i2")
    if samples.size == 0:
        raise SpeakError(f"Пустой pcm: {pcm_path}")
    duration = samples.size / samplerate
    peak = float(np.max(np.abs(samples))) / 32768.0
    logger.info(
        "TTS: играю {:.2f} c, пик {:.2f}, устройство [{}]",
        duration,
        peak,
        device_index if device_index is not None else "default",
    )
    try:
        with sd.OutputStream(
            samplerate=int(samplerate),
            channels=1,
            dtype="int16",
            device=device_index,
        ) as stream:
            stream.write(samples.reshape(-1, 1))
    except Exception as exc:  # noqa: BLE001 - PortAudioError и прочее
        raise SpeakError(f"Не удалось воспроизвести звук: {exc}") from exc
    return duration, peak


def _tts_settings(model: str | None, voice: str | None) -> tuple[str, str | None]:
    """model/voice: аргумент -> переменная окружения (.env) -> config.yaml."""
    load_dotenv()
    tts_cfg = get_settings().section("tts")
    model_id = (
        (model or "").strip()
        or (os.getenv("TTS_MODEL") or "").strip()
        or str(tts_cfg.get("model") or "").strip()
    )
    voice_id = (
        (voice or "").strip()
        or (os.getenv("TTS_VOICE") or "").strip()
        or str(tts_cfg.get("voice") or "").strip()
    )
    if not model_id:
        raise SpeakError(
            "Не задана модель TTS: TTS_MODEL в .env или tts.model в config.yaml"
        )
    return model_id, (voice_id or None)


def speak(
    text: str,
    *,
    voice: str | None = None,
    model: str | None = None,
    device_index: int | None = None,
    samplerate: int | None = None,
) -> Speech:
    """Озвучить текст и проиграть в наушники из config.yaml. Блокирует до конца."""
    phrase = " ".join(str(text).split())
    if not phrase:
        raise SpeakError("Пустой текст для озвучки")

    settings = get_settings()
    rate = int(samplerate or settings.audio.playback_samplerate)
    device = settings.devices.output_index if device_index is None else device_index
    model_id, voice_id = _tts_settings(model, voice)
    _ffmpeg()  # проверяем до платного запроса

    mp3_path = synthesize_mp3(phrase, model=model_id, voice=voice_id)
    pcm_path = mp3_to_pcm(mp3_path, samplerate=rate)
    duration, peak = play_pcm(pcm_path, samplerate=rate, device_index=device)

    result = Speech(
        text=phrase,
        mp3_path=mp3_path,
        pcm_path=pcm_path,
        samplerate=rate,
        duration_s=duration,
        peak=peak,
    )
    logger.info("TTS: проиграно — {}", result)
    return result
