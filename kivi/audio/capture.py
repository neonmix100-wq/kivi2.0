"""Захват микрофона (WASAPI) + кольцевой буфер на 1 секунду.

Поток открывается на НАТИВНОЙ частоте 48000 Гц (WASAPI shared не даст 16000),
моно, int16, блок 32 мс. Callback PortAudio ничего не ждёт: если буфер занят
читателем, блок просто теряется (лучше потерять 32 мс, чем сорвать аудио-поток).
"""

from __future__ import annotations

import threading
import time

import numpy as np
import sounddevice as sd

from kivi.config import AudioConfig
from kivi.log import logger

__all__ = ["AudioCapture", "CaptureError"]


class CaptureError(RuntimeError):
    """Не удалось открыть поток захвата."""


class AudioCapture:
    """Поток WASAPI -> кольцевой буфер (1 с) -> блоки для VAD и pre-roll для рекордера."""

    def __init__(
        self, audio: AudioConfig, device_index: int, *, ring_seconds: float = 1.0
    ) -> None:
        self.samplerate = int(audio.capture_samplerate)
        self.channels = int(audio.channels)
        self.block_frames = int(audio.block_frames)
        self.device_index = int(device_index)
        self.ring_seconds = float(ring_seconds)
        self.input_gain = float(audio.input_gain)

        self._capacity = int(self.samplerate * self.ring_seconds)  # 48000 сэмплов = 1 с
        self._buffer = np.zeros(self._capacity, dtype=np.int16)
        self._write_pos = 0
        self._read_pos = 0
        self._filled = 0

        self._lock = threading.Lock()
        self._data_ready = threading.Condition(self._lock)
        self._stream: sd.InputStream | None = None
        self._running = threading.Event()

        self._read_total = 0  # сколько сэмплов уже выдано читателю
        self._written_total = 0  # сколько сэмплов записано в кольцо (для истории)
        self._dropped = 0  # блоки, потерянные из-за занятой блокировки
        self._overflows = 0  # сэмплы, затрёртые поверх непрочитанных
        self._xruns = 0  # статусы PortAudio (переполнения драйвера)
        self.last_status: str = ""

    # --- callback PortAudio (аудио-поток) ---

    def _callback(self, indata, frames: int, time_info, status) -> None:  # noqa: ANN001
        if status:
            self._xruns += 1
            self.last_status = str(status)
        if not self._lock.acquire(blocking=False):
            # Читатель держит буфер — не блокируем аудио-поток, теряем блок.
            self._dropped += 1
            return
        try:
            mono = indata[:, 0] if getattr(indata, "ndim", 1) > 1 else indata
            self._write_locked(np.asarray(mono, dtype=np.int16))
            self._data_ready.notify_all()
        finally:
            self._lock.release()

    # --- кольцевой буфер (внутреннее, под блокировкой) ---

    def _write_locked(self, block: np.ndarray) -> None:
        count = int(block.size)
        if count <= 0:
            return
        capacity = self._capacity
        if count >= capacity:
            block = block[-capacity:]
            count = capacity
        end = self._write_pos + count
        if end <= capacity:
            self._buffer[self._write_pos : end] = block
        else:
            first = capacity - self._write_pos
            self._buffer[self._write_pos :] = block[:first]
            self._buffer[: end - capacity] = block[first:]
        self._write_pos = end % capacity
        self._filled += count
        self._written_total += count
        if self._filled > capacity:
            # Читатель не успевает: сдвигаем курсор чтения, старые сэмплы теряются.
            lost = self._filled - capacity
            self._overflows += lost
            self._read_pos = (self._read_pos + lost) % capacity
            self._filled = capacity

    def _read_locked(self, count: int) -> np.ndarray:
        capacity = self._capacity
        end = self._read_pos + count
        if end <= capacity:
            block = self._buffer[self._read_pos : end].copy()
        else:
            first = capacity - self._read_pos
            block = np.concatenate(
                (self._buffer[self._read_pos :], self._buffer[: count - first])
            )
        self._read_pos = end % capacity
        self._filled -= count
        self._read_total += count
        return block

    def _apply_gain(self, block: np.ndarray) -> np.ndarray:
        """Входной гейн: int16 -> float, усиление, мягкий лимитер -> int16.

        Применяется к каждому выданному блоку и к pre-roll, поэтому VAD и Recorder
        получают одинаково усиленный сигнал.
        """
        if self.input_gain == 1.0:
            return block
        a = block.astype(np.float32) / 32768.0
        a = a * self.input_gain
        a = np.clip(a, -0.99, 0.99)  # мягкий лимитер, без переполнения int16
        return np.round(a * 32768.0).astype(np.int16)

    # --- публичный интерфейс ---

    def start(self) -> None:
        """Открыть поток WASAPI и начать захват."""
        if self._stream is not None:
            return
        with self._lock:
            self._buffer.fill(0)
            self._write_pos = self._read_pos = self._filled = 0
            self._read_total = 0
            self._written_total = 0
        try:
            stream = sd.InputStream(
                samplerate=self.samplerate,
                blocksize=self.block_frames,
                device=self.device_index,
                channels=self.channels,
                dtype="int16",
                callback=self._callback,
            )
            stream.start()
        except Exception as exc:  # noqa: BLE001 - PortAudioError и всё остальное
            raise CaptureError(
                f"Не удалось открыть микрофон (device {self.device_index}) "
                f"на {self.samplerate} Гц: {exc}"
            ) from exc
        self._stream = stream
        self._running.set()
        logger.info("input_gain = {}", self.input_gain)
        logger.debug(
            "Захват запущен: device={} {} Гц, блок {} сэмплов ({} мс)",
            self.device_index,
            self.samplerate,
            self.block_frames,
            int(round(self.block_frames * 1000 / self.samplerate)),
        )

    def stop(self) -> None:
        """Остановить и закрыть поток."""
        stream, self._stream = self._stream, None
        self._running.clear()
        with self._data_ready:
            self._data_ready.notify_all()
        if stream is not None:
            try:
                stream.stop()
            except Exception:  # noqa: BLE001 - поток мог уже закрыться
                pass
            try:
                stream.close()
            except Exception:  # noqa: BLE001
                pass
        # Отпускаем numpy-буферы, чтобы не тянуть память при выходе процесса.
        import gc

        gc.collect()
        logger.debug(
            "Захват остановлен: xruns={}, потеряно_блоков={}, переполнений={}",
            self._xruns,
            self._dropped,
            self._overflows,
        )

    def read_block(self, timeout: float | None = 1.0) -> np.ndarray | None:
        """Вернуть очередной блок (int16, моно, 1536 сэмплов).

        None — если за timeout сэмплов не набралось или поток уже остановлен.
        """
        frames = self.block_frames
        deadline = None if timeout is None else time.monotonic() + timeout
        with self._data_ready:
            while self._filled < frames:
                if not self._running.is_set():
                    return None
                if deadline is None:
                    self._data_ready.wait(0.5)
                    continue
                remaining = deadline - time.monotonic()
                if remaining <= 0:
                    return None
                self._data_ready.wait(remaining)
            return self._apply_gain(self._read_locked(frames))

    def snapshot(self, milliseconds: float) -> np.ndarray:
        """Последние `milliseconds` мс из кольцевого буфера. Данные не потребляет.

        История отсчитывается от курсора ЗАПИСИ: читатель к этому моменту мог
        уже забрать блоки, но в кольце они остаются.
        """
        with self._lock:
            count = int(self.samplerate * max(0.0, float(milliseconds)) / 1000.0)
            count = min(count, self._written_total, self._capacity)
            if count <= 0:
                return np.zeros(0, dtype=np.int16)
            start = (self._write_pos - count) % self._capacity
            end = start + count
            if end <= self._capacity:
                block = self._buffer[start:end].copy()
            else:
                first = self._capacity - start
                block = np.concatenate(
                    (self._buffer[start:], self._buffer[: count - first])
                )
            return self._apply_gain(block)

    @property
    def is_running(self) -> bool:
        return self._running.is_set()

    @property
    def read_seconds(self) -> float:
        """Сколько секунд аудио уже выдано читателю."""
        with self._lock:
            return self._read_total / self.samplerate

    @property
    def stats(self) -> dict[str, int]:
        return {
            "xruns": self._xruns,
            "dropped_blocks": self._dropped,
            "overflow_samples": self._overflows,
        }

    def __enter__(self) -> "AudioCapture":
        self.start()
        return self

    def __exit__(self, *exc_info: object) -> None:
        self.stop()
