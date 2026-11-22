"""Выбор устройства вывода и воспроизведение PCM через sounddevice.

Устройство выбирается по имени из config или системный выход Windows;
результат кэшируется не дольше 2 секунд. Любой реально отправленный звук
попадает в PlaybackRef для AEC.
"""

from __future__ import annotations

import threading
import time
from pathlib import Path
from typing import Any

import numpy as np
import sounddevice as sd
import soundfile as sf
import soxr
from loguru import logger

from kivi.audio.echo import get_playback_ref

__all__ = ["play_file", "resolve_output_device"]

OUTPUT_CACHE_S = 2.0
PLAY_CHUNK = 4096
_OUTPUT_LOCK = threading.Lock()
_OUTPUT_CACHE: tuple[float, tuple[int, str, int]] | None = None
_OUTPUT_LAST: tuple[int, str, int] | None = None
_PLAY_LOCK = threading.Lock()


def _norm(text: object) -> str:
    return " ".join(str(text or "").replace("ё", "е").lower().split())


def _config() -> tuple[str, str]:
    try:
        from kivi.config import get_settings

        audio = get_settings().audio
        return str(audio.output_device_name or "").strip(), str(audio.hostapi or "")
    except Exception as exc:  # noqa: BLE001 - конфиг может быть не готов
        logger.warning("аудио: имя выхода из конфига не прочитано: {}", type(exc).__name__)
        return "", ""


def _config_name() -> str:
    return _config()[0]


def _query(index: int) -> dict[str, Any] | None:
    try:
        return dict(sd.query_devices(int(index)))
    except Exception:  # noqa: BLE001 - индекс мог устареть
        return None


def _samplerate(device: dict[str, Any] | None, index: int | None = None) -> int:
    try:
        from kivi.config import get_settings

        preferred = int(get_settings().audio.playback_samplerate)
    except Exception:  # noqa: BLE001
        preferred = 0
    if preferred > 0 and index is not None:
        try:
            sd.check_output_settings(
                device=int(index), samplerate=preferred, channels=1, dtype="int16"
            )
            return preferred
        except Exception:  # noqa: BLE001 - устройство не приняло частоту
            pass
    try:
        value = int(round(float((device or {}).get("default_samplerate") or 0)))
    except (TypeError, ValueError):
        value = 0
    if value > 0:
        return value
    try:
        from kivi.config import get_settings

        return int(get_settings().audio.playback_samplerate)
    except Exception:  # noqa: BLE001
        return 48000


def _default_output() -> tuple[int, str] | None:
    try:
        pair = sd.default.device
        index = pair[1] if isinstance(pair, (tuple, list)) and len(pair) > 1 else None
    except Exception:  # noqa: BLE001
        index = None
    if index is not None:
        device = _query(int(index))
        if device is not None and int(device.get("max_output_channels") or 0) > 0:
            return int(index), str(device.get("name") or "")
    for idx, device in enumerate(sd.query_devices()):
        if int(device.get("max_output_channels") or 0) > 0:
            return idx, str(device.get("name") or "")
    return None


def _find_output_substring(name: str, hostapi: str = "") -> tuple[int, str] | None:
    wanted = _norm(name)
    if not wanted:
        return None
    wanted_api = _norm(hostapi)
    api_index: int | None = None
    if wanted_api:
        try:
            for idx, api in enumerate(sd.query_hostapis()):
                if _norm(api.get("name")) == wanted_api:
                    api_index = idx
                    break
        except Exception:  # noqa: BLE001
            api_index = None
    best: tuple[int, int, int, str] | None = None
    for idx, device in enumerate(sd.query_devices()):
        if int(device.get("max_output_channels") or 0) <= 0:
            continue
        candidate = _norm(device.get("name"))
        if not candidate:
            continue
        if candidate == wanted:
            rank = 0
        elif wanted in candidate or candidate in wanted:
            rank = 1
        else:
            continue
        api_rank = 0 if api_index is not None and device.get("hostapi") == api_index else 1
        key = (api_rank, rank, idx, str(device.get("name") or ""))
        if best is None or key < best:
            best = key
    return (best[2], best[3]) if best is not None else None


def resolve_output_device(*, force: bool = False) -> tuple[int, str, int]:
    """(index, name, samplerate) выхода; системный default — если имя пустое."""
    global _OUTPUT_CACHE, _OUTPUT_LAST
    now = time.monotonic()
    with _OUTPUT_LOCK:
        if not force and _OUTPUT_CACHE is not None and now - _OUTPUT_CACHE[0] < OUTPUT_CACHE_S:
            return _OUTPUT_CACHE[1]

        configured, configured_api = _config()
        found = _find_output_substring(configured, configured_api) if configured else None
        if configured and found is None:
            logger.warning(
                "аудио: выход {!r} не найден — использую системный default", configured
            )
        if found is None:
            found = _default_output()
        if found is None:
            raise RuntimeError("аудио: не найдено ни одного устройства вывода")
        index, name = found
        device = _query(index)
        result = (int(index), str(name), _samplerate(device, index))

        if _OUTPUT_LAST is not None and (
            _OUTPUT_LAST[0] != result[0] or _OUTPUT_LAST[1] != result[1]
        ):
            logger.info("аудио: выход сменился на [{}] {}", result[0], result[1])
        _OUTPUT_LAST = result
        _OUTPUT_CACHE = (now, result)
        logger.debug(
            "аудио: выход [{}] {}, {} Гц (config={!r})",
            result[0],
            result[1],
            result[2],
            configured,
        )
        return result


def _read_pcm(path: Path, samplerate: int | None, target_sr: int) -> np.ndarray:
    if samplerate is not None:
        raw = path.read_bytes()
        data = np.frombuffer(raw, dtype="<i2").astype(np.float32) / 32768.0
        source_sr = int(samplerate)
    else:
        try:
            data, source_sr = sf.read(str(path), dtype="float32", always_2d=True)
        except Exception as exc:  # noqa: BLE001 - mp3/wav не прочитался
            raise RuntimeError(f"аудио: не удалось прочитать {path.name}: {exc}") from exc
        data = np.asarray(data, dtype=np.float32)
        if data.ndim > 1:
            data = data.mean(axis=1)
        else:
            data = data.reshape(-1)
        source_sr = int(source_sr)
    if source_sr != target_sr and data.size:
        data = soxr.resample(data, source_sr, target_sr, quality="HQ").astype(np.float32)
    return data.reshape(-1)


def _channels(device_index: int, samplerate: int) -> int:
    device = _query(device_index) or {}
    max_channels = int(device.get("max_output_channels") or 1)
    for channels in (2, 1):
        if channels > max_channels:
            continue
        try:
            sd.check_output_settings(
                device=device_index, samplerate=samplerate, channels=channels, dtype="int16"
            )
            return channels
        except Exception:  # noqa: BLE001 - пробуем моно
            continue
    return 1


def _mic_samplerate() -> int:
    try:
        from kivi.config import get_settings

        return int(get_settings().audio.capture_samplerate)
    except Exception:  # noqa: BLE001
        return 48000


def play_file(
    path: Path,
    *,
    samplerate: int | None = None,
    device_index: int | None = None,
    run_id: str | None = None,
    volume: float = 1.0,
) -> float:
    """Проиграть файл на выбранном устройстве; писать PCM в PlaybackRef."""
    if not path.exists() or path.stat().st_size == 0:
        raise RuntimeError(f"аудио: пустой файл {path}")
    index, name, device_sr = resolve_output_device()
    if device_index is not None:
        index = int(device_index)
        name = str((_query(index) or {}).get("name") or name)
    data = _read_pcm(path, samplerate, device_sr)
    if volume != 1.0:
        data = data * float(volume)
    data = np.clip(data, -1.0, 1.0)
    pcm = (data * 32767.0).astype(np.int16)
    channels = _channels(index, device_sr)
    ref = get_playback_ref(_mic_samplerate())

    with _PLAY_LOCK:
        stream = sd.OutputStream(
            samplerate=device_sr,
            channels=channels,
            dtype="int16",
            device=index,
        )
        started = time.perf_counter()
        try:
            stream.start()
            for start in range(0, pcm.size, PLAY_CHUNK):
                block = pcm[start : start + PLAY_CHUNK]
                if not block.size:
                    continue
                ref.write(block, samplerate=device_sr, at=time.monotonic())
                frames = block.reshape(-1, 1)
                if channels > 1:
                    frames = np.repeat(frames, channels, axis=1)
                stream.write(frames)
        finally:
            try:
                stream.stop()
                stream.close()
            except Exception:  # noqa: BLE001 - поток мог уже закрыться
                pass
        elapsed = time.perf_counter() - started
    logger.debug(
        "аудио: отыграно {:.2f} c на [{}] {} (id={}, {} кан.)",
        elapsed,
        index,
        name,
        run_id or "-",
        channels,
    )
    return elapsed

