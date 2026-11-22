"""Мини-AEC по референсу воспроизведения.

PlaybackRef хранит последние 3 секунды того, что реально отправлено в
устройство вывода. EchoSuppressor ищет задержку по нормированной корреляции,
вычитает референс и отдаёт очищенный кадр до VAD/STT.
"""

from __future__ import annotations

import threading
import time
from typing import Any

import numpy as np
import soxr
from loguru import logger

__all__ = [
    "EchoSuppressor",
    "PlaybackRef",
    "get_playback_ref",
    "reset_playback_ref",
]

REF_SECONDS = 3.0
DELAY_STEP_MS = 8
LOG_INTERVAL_S = 60.0


def _as_mono_float(pcm: Any) -> np.ndarray:
    data = np.asarray(pcm)
    if data.ndim > 1:
        data = data.mean(axis=1)
    data = data.reshape(-1)
    if data.dtype == np.int16:
        return data.astype(np.float32) / 32768.0
    return data.astype(np.float32, copy=False)


def _dtype_result(resid: np.ndarray, dtype: np.dtype) -> np.ndarray:
    if np.issubdtype(dtype, np.integer):
        return np.clip(resid * 32767.0, -32768, 32767).astype(dtype)
    return resid.astype(dtype, copy=False)


class PlaybackRef:
    """Кольцевой PCM-буфер последних REF_SECONDS секунд (mono, mic sr)."""

    def __init__(self, samplerate: int = 48000, seconds: float = REF_SECONDS) -> None:
        self.samplerate = int(max(1, samplerate))
        self.capacity = int(self.samplerate * max(0.1, float(seconds)))
        self._buffer = np.zeros(self.capacity, dtype=np.float32)
        self._write_pos = 0
        self._filled = 0
        self._end_time: float | None = None
        self._last_end: float | None = None
        self._lock = threading.RLock()

    def _append_locked(self, data: np.ndarray) -> None:
        count = int(data.size)
        if count <= 0:
            return
        if count >= self.capacity:
            data = data[-self.capacity :]
            count = self.capacity
        end = self._write_pos + count
        if end <= self.capacity:
            self._buffer[self._write_pos : end] = data
        else:
            first = self.capacity - self._write_pos
            self._buffer[self._write_pos :] = data[:first]
            self._buffer[: end - self.capacity] = data[first:]
        self._write_pos = end % self.capacity
        self._filled = min(self.capacity, self._filled + count)

    def _linear_locked(self) -> np.ndarray:
        if self._filled <= 0:
            return np.zeros(0, dtype=np.float32)
        if self._filled < self.capacity:
            return self._buffer[: self._filled].copy()
        return np.concatenate((self._buffer[self._write_pos :], self._buffer[: self._write_pos]))

    def write(
        self,
        pcm: Any,
        samplerate: int | None = None,
        *,
        at: float | None = None,
    ) -> None:
        """Записать реально отправленный в устройство PCM в референс."""
        data = _as_mono_float(pcm)
        source_sr = int(samplerate or self.samplerate)
        if source_sr != self.samplerate and data.size:
            data = soxr.resample(data, source_sr, self.samplerate, quality="HQ").astype(
                np.float32
            )
        if data.size == 0:
            return
        now = time.monotonic() if at is None else float(at)
        with self._lock:
            if self._end_time is None:
                self._end_time = now
            if now > self._end_time:
                gap = min(
                    int(round((now - self._end_time) * self.samplerate)),
                    self.capacity,
                )
                if gap > 0:
                    self._append_locked(np.zeros(gap, dtype=np.float32))
                    self._end_time += gap / self.samplerate
            self._append_locked(data)
            self._end_time += data.size / self.samplerate
            self._last_end = max(
                self._last_end or self._end_time,
                now + data.size / self.samplerate,
            )

    def snapshot(
        self,
        frames: int,
        delay_s: float = 0.0,
        now: float | None = None,
    ) -> np.ndarray:
        """Кусок референса длиной frames, заканчивающийся в now - delay_s."""
        count = max(0, int(frames))
        if count == 0:
            return np.zeros(0, dtype=np.float32)
        current = time.monotonic() if now is None else float(now)
        with self._lock:
            if self._end_time is None:
                return np.zeros(count, dtype=np.float32)
            if current > self._end_time:
                gap = min(
                    int(round((current - self._end_time) * self.samplerate)),
                    self.capacity,
                )
                if gap > 0:
                    self._append_locked(np.zeros(gap, dtype=np.float32))
                    self._end_time += gap / self.samplerate
                self._end_time = current
            data = self._linear_locked()
            if data.size == 0:
                return np.zeros(count, dtype=np.float32)
            skip = int(round(max(0.0, float(delay_s)) * self.samplerate))
            end = data.size - skip
            if end <= 0:
                return np.zeros(count, dtype=np.float32)
            start = max(0, end - count)
            piece = data[start:end]
            if piece.size < count:
                piece = np.pad(piece, (count - piece.size, 0))
            return piece.astype(np.float32, copy=False)

    def is_recent(self, now: float | None = None, window_s: float = 0.0) -> bool:
        current = time.monotonic() if now is None else float(now)
        with self._lock:
            return self._last_end is not None and current <= self._last_end + max(
                0.0, float(window_s)
            )

    @property
    def last_playback_end(self) -> float | None:
        with self._lock:
            return self._last_end

    def clear(self) -> None:
        with self._lock:
            self._buffer.fill(0.0)
            self._write_pos = 0
            self._filled = 0
            self._end_time = None
            self._last_end = None



_ref_lock = threading.Lock()
_ref: PlaybackRef | None = None


def get_playback_ref(samplerate: int = 48000) -> PlaybackRef:
    """Единый референс воспроизведения для output и capture."""
    global _ref
    with _ref_lock:
        if _ref is None:
            _ref = PlaybackRef(samplerate=samplerate)
        return _ref


def reset_playback_ref() -> None:
    """Очистить общий референс (выход/тесты)."""
    global _ref
    with _ref_lock:
        _ref = None


class EchoSuppressor:
    """Мини-AEC: поиск задержки по корреляции + вычитание референса."""

    def __init__(
        self,
        *,
        ref: PlaybackRef | None = None,
        samplerate: int = 48000,
        enabled: bool = True,
        corr_threshold: float = 0.6,
        tail_ms: int = 600,
        max_delay_ms: int = 400,
    ) -> None:
        self.samplerate = int(max(1, samplerate))
        self.ref = ref if ref is not None else get_playback_ref(self.samplerate)
        self.enabled = bool(enabled)
        self.corr_threshold = float(corr_threshold)
        self.tail_s = max(0.0, float(tail_ms) / 1000.0)
        self.max_delay_s = max(0.0, float(max_delay_ms) / 1000.0)
        self.echo_dropped = 0
        self.echo_cleaned = 0
        self._last_log = time.monotonic()

    @classmethod
    def from_config(cls, config: Any, samplerate: int) -> "EchoSuppressor":
        return cls(
            samplerate=samplerate,
            enabled=bool(getattr(config, "enabled", True)),
            corr_threshold=float(getattr(config, "corr_threshold", 0.6)),
            tail_ms=int(getattr(config, "tail_ms", 600)),
            max_delay_ms=int(getattr(config, "max_delay_ms", 400)),
        )

    def reset_counters(self) -> None:
        self.echo_dropped = 0
        self.echo_cleaned = 0

    def _log_if_needed(self, now: float) -> None:
        if not (self.echo_dropped or self.echo_cleaned):
            return
        if now - self._last_log < LOG_INTERVAL_S:
            return
        self._last_log = now
        logger.info(
            "AEC: echo_dropped={}, echo_cleaned={}",
            self.echo_dropped,
            self.echo_cleaned,
        )

    def process(self, frame: Any, now: float | None = None) -> np.ndarray:
        """Очистить один кадр микрофона; вернуть массив той же длины/типа."""
        source = np.asarray(frame)
        if not self.enabled or source.size == 0:
            return source
        current = time.monotonic() if now is None else float(now)
        window_s = self.tail_s + self.max_delay_s + source.size / self.samplerate + 0.05
        if not self.ref.is_recent(current, window_s):
            return source

        mic = _as_mono_float(source)
        mic_energy = float(np.dot(mic, mic))
        if mic_energy <= 1e-12:
            return source

        step = max(1, int(self.samplerate * DELAY_STEP_MS / 1000))
        max_delay = int(self.max_delay_s * self.samplerate)
        best_corr = -1.0
        best_ref: np.ndarray | None = None
        for delay in range(0, max_delay + 1, step):
            reference = self.ref.snapshot(
                mic.size,
                delay_s=delay / self.samplerate,
                now=current,
            )
            ref_energy = float(np.dot(reference, reference))
            if ref_energy <= 1e-12:
                continue
            corr = float(np.dot(mic, reference) / np.sqrt(mic_energy * ref_energy))
            if corr > best_corr:
                best_corr = corr
                best_ref = reference

        if best_ref is None or best_corr <= self.corr_threshold:
            return source

        ref_energy = float(np.dot(best_ref, best_ref))
        if ref_energy <= 1e-12:
            return source
        gain = float(np.dot(mic, best_ref) / ref_energy)
        resid = mic - gain * best_ref
        resid_energy = float(np.dot(resid, resid))
        if resid_energy < 0.25 * mic_energy:
            self.echo_dropped += 1
            result = np.zeros(source.size, dtype=source.dtype)
        else:
            self.echo_cleaned += 1
            result = _dtype_result(resid, source.dtype)
        self._log_if_needed(current)
        return result

