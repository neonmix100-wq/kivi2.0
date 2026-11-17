"""Озвучка: TTS -> файл от провайдера -> ffprobe -> soxr resample -> наушники."""

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
import soundfile as sf
import soxr
from dotenv import load_dotenv

from kivi.config import DATA_DIR, get_settings
from kivi.log import logger

__all__ = ["DEFAULT_TIMEOUT", "SpeakError", "Speech", "probe_sample_rate", "speak"]

TMP_DIR = DATA_DIR / "tmp"
MP3_PATH = TMP_DIR / "tts_speech.mp3"
PCM_PATH = TMP_DIR / "tts_speech.pcm"
DEFAULT_TIMEOUT = 180.0
MIN_AUDIO_BYTES = 1000


class SpeakError(RuntimeError):
    """Озвучка не удалась."""


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
    load_dotenv()
    base = (os.getenv("LLM_BASE_URL") or "").strip()
    key = (os.getenv("LLM_API_KEY") or "").strip()
    if not base or not key:
        raise SpeakError("Не заданы LLM_BASE_URL и/или LLM_API_KEY в окружении.")
    return base.rstrip("/"), key


def _ffprobe() -> str:
    exe = shutil.which("ffprobe")
    if not exe:
        raise SpeakError("ffprobe не найден в PATH.")
    return exe


def probe_sample_rate(audio_path: Path) -> int:
    exe = _ffprobe()
    command = [
        exe, "-v", "error", "-select_streams", "a:0",
        "-show_entries", "stream=sample_rate", "-of", "csv=p=0", str(audio_path),
    ]
    try:
        completed = subprocess.run(command, capture_output=True, check=False)
    except OSError as exc:
        raise SpeakError(f"Не удалось запустить ffprobe: {exc}") from exc
    stdout = completed.stdout.decode("utf-8", errors="replace").strip()
    if completed.returncode != 0 or not stdout:
        stderr = completed.stderr.decode("utf-8", errors="replace").strip()
        raise SpeakError(f"ffprobe не смог определить частоту: {(stderr or stdout)[:300]}")
    first = stdout.splitlines()[0].strip().rstrip(",")
    if not first.isdigit():
        raise SpeakError(f"ffprobe вернул не число: {stdout[:100]!r}")
    rate = int(first)
    if rate <= 0:
        raise SpeakError(f"ffprobe вернул некорректную частоту: {rate}")
    logger.debug("ffprobe: {} -> {} Гц", audio_path.name, rate)
    return rate


def synthesize_mp3(text: str, *, model: str, voice: str | None, timeout: float = DEFAULT_TIMEOUT) -> Path:
    base, key = _credentials()
    url = f"{base}/audio/speech"
    payload: dict[str, str] = {"model": model, "input": text, "response_format": "mp3"}
    if voice:
        payload["voice"] = voice
    logger.debug("TTS: {} символов, model={}, voice={}", len(text), model, voice)
    try:
        response = requests.post(url, headers={"Authorization": f"Bearer {key}"}, json=payload, timeout=timeout)
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
            raise SpeakError(f"Не удалось декодировать base64: {exc}") from exc
        logger.debug("TTS: JSON->base64->mp3, {} байт", len(audio_bytes))
    else:
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
    audio, native_sr = sf.read(str(mp3_path))
    if audio.ndim > 1:
        audio = audio.mean(axis=1)
    logger.debug("TTS: mp3 прочитан: {:.2f} c, {} Гц", len(audio)/native_sr, native_sr)
    if native_sr != samplerate:
        audio = soxr.resample(audio, native_sr, samplerate, quality='VHQ')
        logger.debug("TTS: soxr resample: {} -> {} Гц", native_sr, samplerate)
    peak = np.max(np.abs(audio))
    if peak > 0.90:
        audio = audio * (0.90 / peak)
        logger.debug("TTS: headroom применён: peak {:.3f} -> 0.90", peak)
    audio_int16 = (audio * 32767).astype(np.int16)
    pcm_path.parent.mkdir(parents=True, exist_ok=True)
    pcm_path.write_bytes(audio_int16.tobytes())
    if not pcm_path.exists() or pcm_path.stat().st_size == 0:
        raise SpeakError(f"Не удалось создать pcm: {pcm_path}")
    logger.debug("TTS: pcm создан: {} байт, {} Гц", pcm_path.stat().st_size, samplerate)
    return pcm_path


def play_pcm(pcm_path: Path, *, samplerate: int, device_index: int | None = None) -> tuple[float, float]:
    """Проиграть s16le через ffplay (эталонный аудио-движок)."""
    if not pcm_path.exists() or pcm_path.stat().st_size == 0:
        raise SpeakError(f"Пустой pcm: {pcm_path}")
    
    duration = pcm_path.stat().st_size / (2 * samplerate)  # 2 байта на сэмпл int16
    peak = 0.90  # headroom уже применён в mp3_to_pcm
    
    logger.info("TTS: проигрываю через ffplay, {} Гц, {:.2f} c", samplerate, duration)
    
    command = [
        "ffplay",
        "-f", "s16le",
        "-ar", str(samplerate),
        "-nodisp",
        "-autoexit",
        str(pcm_path),
    ]
    
    try:
        result = subprocess.run(
            command,
            stdout=subprocess.DEVNULL,
            stderr=subprocess.DEVNULL,
            timeout=duration + 5.0,
            check=True,
        )
    except subprocess.TimeoutExpired:
        raise SpeakError(f"ffplay завис на {pcm_path}")
    except subprocess.CalledProcessError as exc:
        raise SpeakError(f"ffplay завершился с ошибкой: {exc}")
    except FileNotFoundError:
        raise SpeakError("ffplay не найден в PATH. Установите ffmpeg.")
    
    logger.info("TTS: проиграно через ffplay — {:.2f} c", duration)
    return duration, peak


def _playback_samplerate(real_sr: int, device_index: int | None) -> int:
    try:
        sd.check_output_settings(device=device_index, samplerate=int(real_sr), channels=1, dtype="int16")
        return int(real_sr)
    except Exception:
        pass
    fallback = int(get_settings().audio.playback_samplerate)
    if fallback == real_sr:
        return int(real_sr)
    logger.warning("TTS: файл {} Гц, но устройство [{}] принимает только {} Гц — ресемплирую", real_sr, device_index, fallback)
    return fallback


def _tts_settings(model: str | None, voice: str | None) -> tuple[str, str | None]:
    load_dotenv()
    tts_cfg = get_settings().section("tts")
    model_id = ((model or "").strip() or (os.getenv("TTS_MODEL") or "").strip() or str(tts_cfg.get("model") or "").strip())
    voice_id = ((voice or "").strip() or (os.getenv("TTS_VOICE") or "").strip() or str(tts_cfg.get("voice") or "").strip())
    if not model_id:
        raise SpeakError("Не задана модель TTS: TTS_MODEL в .env или tts.model в config.yaml")
    return model_id, (voice_id or None)


def speak(text: str, *, voice: str | None = None, model: str | None = None, device_index: int | None = None) -> Speech:
    phrase = " ".join(str(text).split())
    if not phrase:
        raise SpeakError("Пустой текст для озвучки")
    settings = get_settings()
    device = settings.devices.output_index if device_index is None else device_index
    model_id, voice_id = _tts_settings(model, voice)
    _ffprobe()
    mp3_path = synthesize_mp3(phrase, model=model_id, voice=voice_id)
    real_sr = probe_sample_rate(mp3_path)
    play_sr = _playback_samplerate(real_sr, device)
    pcm_path = mp3_to_pcm(mp3_path, samplerate=play_sr)
    duration, peak = play_pcm(pcm_path, samplerate=play_sr, device_index=device)
    result = Speech(text=phrase, mp3_path=mp3_path, pcm_path=pcm_path, samplerate=play_sr, duration_s=duration, peak=peak)
    logger.info("TTS: проиграно — {}", result)
    return result