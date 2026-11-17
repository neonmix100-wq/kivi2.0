"""Запись фразы: pre-roll + речь -> data/last_utterance.wav (16000 Гц, моно, PCM16).

Рекордер получает блоки 48 кГц, копит их от speech_start до speech_end,
отрезает хвостовую тишину по времени speech_end и ресемплит в 16000 через soxr.
"""

from __future__ import annotations

from dataclasses import dataclass
from pathlib import Path

import numpy as np
import soundfile as sf
import soxr

from kivi.config import DATA_DIR, AudioConfig
from kivi.log import logger

__all__ = ["DEFAULT_UTTERANCE_PATH", "Recorder", "Utterance"]

DEFAULT_UTTERANCE_PATH = DATA_DIR / "last_utterance.wav"
MIN_UTTERANCE_MS = 250.0  # короче — это щелчок/кашель, не фраза
TAIL_PAD_MS = 250.0  # запас после конца речи, чтобы не срезать хвост слова


@dataclass(frozen=True)
class Utterance:
    """Записанная фраза."""

    path: Path
    start_t: float  # начало речи по часам VAD, с
    end_t: float  # конец речи (начало тишины), с
    speech_s: float  # длительность речи, с
    duration_s: float  # длительность файла (с pre-roll и хвостом), с
    samples: int  # сэмплов в файле (16 кГц)
    samplerate: int
    rms: float
    peak: float

    def __str__(self) -> str:
        return (
            f"{self.path.name}: речь {self.speech_s:.2f} c, файл {self.duration_s:.2f} c, "
            f"пик {self.peak:.2f}, RMS {self.rms:.3f}"
        )


class Recorder:
    """Копит аудио одной фразы и сохраняет её в WAV."""

    def __init__(
        self,
        audio: AudioConfig,
        output_path: Path | None = None,
        *,
        tail_pad_ms: float = TAIL_PAD_MS,
        min_utterance_ms: float = MIN_UTTERANCE_MS,
    ) -> None:
        self.input_samplerate = int(audio.capture_samplerate)
        self.output_samplerate = int(audio.target_samplerate)
        self.output_path = (
            Path(output_path) if output_path is not None else DEFAULT_UTTERANCE_PATH
        )
        self.tail_pad_ms = float(tail_pad_ms)
        self.min_utterance_ms = float(min_utterance_ms)

        self._chunks: list[np.ndarray] = []
        self._active = False
        self._start_t = 0.0
        self._end_t = 0.0
        self._buffer_start_t = 0.0

    @property
    def is_recording(self) -> bool:
        return self._active

    def start(self, start_t: float, pre_roll: np.ndarray) -> None:
        """Начать фразу.

        start_t — момент начала речи по часам VAD.
        pre_roll — готовый кусок из кольцевого буфера capture (int16, 48 кГц),
        который ЗАКАНЧИВАЕТСЯ в момент прихода события. Поэтому начало буфера
        считаем как start_t минус его длительность.
        """
        pre = np.asarray(pre_roll, dtype=np.int16).reshape(-1).copy()
        self._active = True
        self._chunks = [pre] if pre.size else []
        self._start_t = float(start_t)
        self._end_t = float(start_t)
        self._buffer_start_t = float(start_t) - pre.size / self.input_samplerate
        logger.debug(
            "Recorder: старт фразы t={:.2f} c, pre-roll {:.0f} мс",
            self._start_t,
            pre.size * 1000.0 / self.input_samplerate,
        )

    def push(self, block: np.ndarray) -> None:
        """Добавить очередной блок (int16, 48 кГц) к текущей фразе."""
        if not self._active:
            return
        data = np.asarray(block, dtype=np.int16).reshape(-1)
        if data.size:
            self._chunks.append(data.copy())

    def cancel(self) -> None:
        """Выбросить недописанную фразу (например, при остановке)."""
        if self._active:
            logger.debug("Recorder: недописанная фраза отброшена")
        self._active = False
        self._chunks = []

    def stop(self, end_t: float) -> Utterance | None:
        """Закрыть фразу, сохранить WAV. Возвращает Utterance или None, если фраза пустая."""
        if not self._active:
            return None
        self._active = False
        self._end_t = float(end_t)
        chunks, self._chunks = self._chunks, []
        if not chunks:
            logger.warning("Recorder: нет данных для сохранения (фраза пустая)")
            return None

        audio48 = chunks[0] if len(chunks) == 1 else np.concatenate(chunks)

        # Отрезаем хвостовую тишину: оставляем всё до end_t + небольшой запас.
        keep_until = (self._end_t + self.tail_pad_ms / 1000.0) - self._buffer_start_t
        keep = int(round(keep_until * self.input_samplerate))
        keep = max(0, min(keep, audio48.size))
        audio48 = audio48[:keep]

        speech_ms = (self._end_t - self._start_t) * 1000.0
        if audio48.size == 0 or speech_ms < self.min_utterance_ms:
            logger.warning(
                "Recorder: фраза слишком короткая ({:.0f} мс) — не сохраняю", speech_ms
            )
            return None

        samples = audio48.astype(np.float32) / 32768.0
        if self.input_samplerate != self.output_samplerate:
            # 48000 -> 16000, целочисленный коэффициент 3.
            samples = soxr.resample(samples, self.input_samplerate, self.output_samplerate)
        samples = np.clip(np.asarray(samples, dtype=np.float32), -1.0, 1.0)

        self.output_path.parent.mkdir(parents=True, exist_ok=True)
        sf.write(str(self.output_path), samples, self.output_samplerate, subtype="PCM_16")

        utterance = Utterance(
            path=self.output_path,
            start_t=self._start_t,
            end_t=self._end_t,
            speech_s=self._end_t - self._start_t,
            duration_s=samples.size / self.output_samplerate,
            samples=int(samples.size),
            samplerate=self.output_samplerate,
            rms=float(np.sqrt(np.mean(np.square(samples)))) if samples.size else 0.0,
            peak=float(np.max(np.abs(samples))) if samples.size else 0.0,
        )
        logger.info("Фраза сохранена: {}", utterance)
        return utterance
