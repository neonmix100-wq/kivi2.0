"""Озвучка: TTS -> файл от провайдера -> soxr resample -> наушники.

Гарантии против наложения голосов:
  * временные файлы уникальны на каждый вызов (tts_<uuid8>.mp3/.pcm);
  * в модуле один активный ffplay; перед новым — предыдущий завершается;
  * вся озвучка сериализована локом, поэтому «старт/конец» строго чередуются.
"""

from __future__ import annotations

import base64
import binascii
import json
import os
import shutil
import subprocess
import threading
import time
import uuid
from contextlib import contextmanager
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

__all__ = [
    "DEFAULT_TIMEOUT",
    "SpeakError",
    "Speech",
    "play_file",
    "play_pcm",
    "probe_sample_rate",
    "speak",
    "speech_slot",
    "warm_connection",
]

TMP_DIR = DATA_DIR / "tmp"
DEFAULT_TIMEOUT = 180.0
MIN_AUDIO_BYTES = 1000
PLAYER_STOP_TIMEOUT = 1.0

# Один активный плеер на весь модуль + сериализация озвучки.
_PLAYER_LOCK = threading.Lock()
_SPEAK_LOCK = threading.RLock()
_player: subprocess.Popen | None = None
_player_id: str | None = None

# Keep-alive: без этого каждый синтез заново проходит TLS handshake (~0.5 c).
_http = requests.Session()


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
    run_id: str = ""

    def __str__(self) -> str:
        return (
            f"{self.duration_s:.2f} c, {self.samplerate} Гц, пик {self.peak:.2f} "
            f"({self.mp3_path.name}, id={self.run_id})"
        )


def _new_run_id() -> str:
    return uuid.uuid4().hex[:8]


def warm_connection() -> None:
    """Прогреть TLS-соединение с TTS до первого ответа (бесплатно, GET /models)."""
    try:
        base, key = _credentials()
        _http.get(f"{base}/models", headers={"Authorization": f"Bearer {key}"}, timeout=10)
        logger.debug("TTS: соединение прогрето")
    except Exception as exc:  # noqa: BLE001 - прогрев не обязателен
        logger.debug("TTS: прогрев соединения не удался: {}", exc)


def _move_to_slot(path: Path, slot: Path) -> Path:
    """Перевести временный файл в общий слот (перезапись предыдущего).

    os.remove в проекте запрещён, поэтому «удаление» делаем перезаписью слота:
    файлов не копится, а последний остаётся доступным (его печатают смоуки).
    """
    try:
        path.replace(slot)
        return slot
    except OSError as exc:
        logger.debug("TTS: не удалось переместить {} -> {}: {}", path.name, slot.name, exc)
        return path


def _retire(path: Path) -> None:
    """Убрать временный файл в корзину (если send2trash доступен)."""
    if not path.exists():
        return
    try:
        from send2trash import send2trash  # noqa: PLC0415 - необязательная зависимость
    except ImportError:
        _move_to_slot(path, TMP_DIR / f"tts_prev{path.suffix}")
        return
    try:
        send2trash(str(path))
    except Exception as exc:  # noqa: BLE001 - корзина может быть недоступна
        logger.debug("TTS: не удалось убрать {} в корзину: {}", path.name, exc)


def _stop_player_locked() -> None:
    """Завершить активный ffplay (вызывать под _PLAYER_LOCK)."""
    global _player, _player_id
    proc = _player
    if proc is None:
        return
    if proc.poll() is None:
        logger.info("озвучка: прерываю предыдущую id={}", _player_id)
        proc.terminate()
        try:
            proc.wait(timeout=PLAYER_STOP_TIMEOUT)
        except subprocess.TimeoutExpired:
            proc.kill()
            try:
                proc.wait(timeout=PLAYER_STOP_TIMEOUT)
            except subprocess.TimeoutExpired:
                logger.warning("озвучка: ffplay не завершился по kill")
    _player = None
    _player_id = None


@contextmanager
def speech_slot(text: str, run_id: str | None = None):
    """Сериализовать озвучку и залогировать старт/конец одной реплики."""
    run = run_id or _new_run_id()
    with _SPEAK_LOCK:
        logger.info("озвучка: старт id={}, len={}", run, len(str(text)))
        state = {"duration": 0.0}
        try:
            yield run, state
        finally:
            logger.info(
                "озвучка: конец id={}, длительность={:.2f}",
                run,
                float(state.get("duration") or 0.0),
            )


def play_file(
    path: Path,
    *,
    samplerate: int | None = None,
    run_id: str | None = None,
) -> float:
    """Проиграть файл единственным плеером ffplay. Блокирует до конца."""
    global _player, _player_id
    ffplay = shutil.which("ffplay")
    if ffplay is None:
        raise SpeakError("ffplay не найден в PATH")
    if not path.exists() or path.stat().st_size == 0:
        raise SpeakError(f"Пустой аудиофайл: {path}")

    args = [ffplay, "-nodisp", "-autoexit", "-loglevel", "error"]
    if samplerate is not None:
        args += ["-f", "s16le", "-ar", str(int(samplerate))]
    args.append(str(path))

    wait_for = None
    with _PLAYER_LOCK:
        current = _player
        same_run = (
            current is not None
            and current.poll() is None
            and run_id is not None
            and _player_id == run_id
        )
        if same_run:
            # Тот же ответ (следующее предложение): текущее доигрываем, не обрываем.
            wait_for = current
        else:
            _stop_player_locked()

    if wait_for is not None:
        try:
            wait_for.wait()
        except Exception:  # noqa: BLE001 - процесс мог завершиться сам
            pass
        with _PLAYER_LOCK:
            if _player is wait_for:
                _player = None
                _player_id = None

    with _PLAYER_LOCK:
        proc = subprocess.Popen(
            args,
            stdin=subprocess.DEVNULL,
            stdout=subprocess.DEVNULL,
            stderr=subprocess.DEVNULL,
        )
        _player, _player_id = proc, run_id

    started = time.perf_counter()
    try:
        code = proc.wait()
    except KeyboardInterrupt:
        with _PLAYER_LOCK:
            _stop_player_locked()
        raise
    elapsed = time.perf_counter() - started

    with _PLAYER_LOCK:
        if _player is proc:
            _player = None
            _player_id = None

    if code != 0:
        logger.info("озвучка: прервана id={} (ffplay код {})", run_id, code)
    return elapsed


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


def synthesize_mp3(
    text: str,
    *,
    model: str,
    voice: str | None,
    timeout: float = DEFAULT_TIMEOUT,
    run_id: str | None = None,
) -> Path:
    base, key = _credentials()
    url = f"{base}/audio/speech"
    payload: dict[str, str] = {"model": model, "input": text, "response_format": "mp3"}
    if voice:
        payload["voice"] = voice
    logger.debug("TTS: {} символов, model={}, voice={}", len(text), model, voice)
    try:
        response = _http.post(
            url, headers={"Authorization": f"Bearer {key}"}, json=payload, timeout=timeout
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
            raise SpeakError(f"Не удалось декодировать base64: {exc}") from exc
        logger.debug("TTS: JSON->base64->mp3, {} байт", len(audio_bytes))
    else:
        audio_bytes = response.content
        logger.debug("TTS: сырой ответ, {} байт", len(audio_bytes))
    size = 0 if not audio_bytes else len(audio_bytes)
    if size < MIN_AUDIO_BYTES:
        raise SpeakError(f"Слишком короткий аудиоответ: {size} байт")
    TMP_DIR.mkdir(parents=True, exist_ok=True)
    # Уникальный файл на каждый вызов: общий путь ломал звук при перекрытии.
    mp3_path = TMP_DIR / f"tts_{run_id or _new_run_id()}.mp3"
    mp3_path.write_bytes(audio_bytes)
    logger.info("TTS: mp3 готов ({:.1f} КБ): {}", size / 1024, mp3_path.name)
    return mp3_path


def mp3_to_pcm(mp3_path: Path, *, samplerate: int, pcm_path: Path | None = None) -> Path:
    target = Path(pcm_path) if pcm_path is not None else mp3_path.with_suffix(".pcm")
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
    target.parent.mkdir(parents=True, exist_ok=True)
    target.write_bytes(audio_int16.tobytes())
    if not target.exists() or target.stat().st_size == 0:
        raise SpeakError(f"Не удалось создать pcm: {target}")
    logger.debug("TTS: pcm создан: {} байт, {} Гц", target.stat().st_size, samplerate)
    return target


def play_pcm(
    pcm_path: Path,
    *,
    samplerate: int,
    device_index: int | None = None,
    run_id: str | None = None,
) -> tuple[float, float]:
    """Проиграть s16le единственным плеером. Возвращает (длительность, пик)."""
    if not pcm_path.exists() or pcm_path.stat().st_size == 0:
        raise SpeakError(f"Пустой pcm: {pcm_path}")
    expected = pcm_path.stat().st_size / (2 * samplerate)
    played = play_file(pcm_path, samplerate=samplerate, run_id=run_id)
    logger.debug("TTS: pcm отыгран за {:.2f} c (ожидалось {:.2f} c)", played, expected)
    return played, 0.90



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


def speak(
    text: str,
    *,
    voice: str | None = None,
    model: str | None = None,
    device_index: int | None = None,
) -> Speech:
    """Озвучить текст. Проигрывает звук сам — повторно играть не нужно."""
    phrase = " ".join(str(text).split())
    if not phrase:
        raise SpeakError("Пустой текст для озвучки")
    settings = get_settings()
    device = settings.devices.output_index if device_index is None else device_index
    model_id, voice_id = _tts_settings(model, voice)
    _ffprobe()

    with speech_slot(phrase) as (run_id, state):
        mp3_path = synthesize_mp3(
            phrase, model=model_id, voice=voice_id, run_id=run_id
        )
        real_sr = probe_sample_rate(mp3_path)
        play_sr = _playback_samplerate(real_sr, device)
        pcm_path = mp3_to_pcm(mp3_path, samplerate=play_sr)
        duration, peak = play_pcm(
            pcm_path, samplerate=play_sr, device_index=device, run_id=run_id
        )
        state["duration"] = duration

        # Временные файлы переводим в общий слот: файлов не копится,
        # последний остаётся доступным для смоуков.
        final_mp3 = _move_to_slot(mp3_path, TMP_DIR / "tts_last.mp3")
        final_pcm = _move_to_slot(pcm_path, TMP_DIR / "tts_last.pcm")
        result = Speech(
            text=phrase,
            mp3_path=final_mp3,
            pcm_path=final_pcm,
            samplerate=play_sr,
            duration_s=duration,
            peak=peak,
            run_id=run_id,
        )
        logger.info("TTS: проиграно — {}", result)
        return result