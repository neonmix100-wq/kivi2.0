"""Silero VAD (onnxruntime, CPU). Торч не используется.

Модель silero_vad.onnx скачивается из snakers4/silero-vad в data/models/,
если её там нет. Вход — байты PCM int16 моно на частоте захвата (48000),
внутри ресемплинг в 16000 через soxr и окна по 512 сэмплов (32 мс).

Порог 0.5, min_speech 250 мс (защита от щелчков), min_silence из конфига (400 мс).
"""

from __future__ import annotations

from pathlib import Path
from typing import Literal, TypedDict

import numpy as np
import onnxruntime as ort
import requests
import soxr

from kivi.config import MODELS_DIR, AudioConfig
from kivi.log import logger

__all__ = ["MIN_SPEECH_MS", "WINDOW_SAMPLES", "VAD", "VADEvent", "ensure_model"]

MODEL_FILENAME = "silero_vad.onnx"
MIN_MODEL_BYTES = 500_000
MODEL_URLS: tuple[str, ...] = (
    "https://raw.githubusercontent.com/snakers4/silero-vad/master/src/silero_vad/data/silero_vad.onnx",
    "https://github.com/snakers4/silero-vad/raw/master/src/silero_vad/data/silero_vad.onnx",
)

WINDOW_SAMPLES = 512  # окно Silero v5 при 16 кГц = 32 мс
CONTEXT_SAMPLES = 64  # хвост предыдущего окна, который модель ждёт на входе
STATE_SHAPE = (2, 1, 128)  # состояние рекуррентной части модели
MIN_SPEECH_MS = 250.0
DEFAULT_THRESHOLD = 0.5


class VADEvent(TypedDict):
    """Событие VAD: t — время в секундах от начала потока."""

    type: Literal["speech_start", "speech_end"]
    t: float


def ensure_model(models_dir: Path | None = None) -> Path:
    """Путь к модели; при отсутствии скачивает её в data/models/."""
    directory = Path(models_dir) if models_dir is not None else MODELS_DIR
    path = directory / MODEL_FILENAME
    if path.exists() and path.stat().st_size >= MIN_MODEL_BYTES:
        return path

    directory.mkdir(parents=True, exist_ok=True)
    last_error: Exception | None = None
    for url in MODEL_URLS:
        part = path.with_name(MODEL_FILENAME + ".part")
        try:
            logger.info("Скачиваю модель Silero VAD: {}", url)
            written = 0
            with requests.get(url, stream=True, timeout=(10, 120)) as response:
                response.raise_for_status()
                with part.open("wb") as handle:
                    for chunk in response.iter_content(chunk_size=65536):
                        if chunk:
                            handle.write(chunk)
                            written += len(chunk)
            if written < MIN_MODEL_BYTES:
                raise RuntimeError(f"файл подозрительно маленький: {written} байт")
            part.replace(path)
            logger.info(
                "Модель Silero VAD сохранена: {} ({:.1f} МБ)", path, written / 2**20
            )
            return path
        except Exception as exc:  # noqa: BLE001 - пробуем следующий адрес
            last_error = exc
            logger.warning("Не удалось скачать {}: {}", url, exc)
    raise RuntimeError(
        "Не удалось скачать модель Silero VAD ни с одного адреса "
        f"(последняя ошибка: {last_error}). Проверь интернет и повтори запуск."
    )


# --- КЛАСС VAD ---


class VAD:
    """Потоковый детектор речи. Кормить блоками захвата, получать события."""

    def __init__(
        self,
        audio: AudioConfig,
        *,
        model_path: Path | None = None,
        threshold: float = DEFAULT_THRESHOLD,
        min_speech_ms: float = MIN_SPEECH_MS,
        min_silence_ms: float | None = None,
        num_threads: int = 1,
    ) -> None:
        self.input_samplerate = int(audio.capture_samplerate)
        self.samplerate = int(audio.target_samplerate)
        self.threshold = float(threshold)
        self.window_samples = WINDOW_SAMPLES
        self.min_speech_samples = int(self.samplerate * float(min_speech_ms) / 1000.0)
        silence_ms = float(
            audio.vad_end_silence_ms if min_silence_ms is None else min_silence_ms
        )
        self.min_silence_samples = int(self.samplerate * silence_ms / 1000.0)
        if self.input_samplerate % self.samplerate != 0:
            raise ValueError(
                f"{self.input_samplerate} Гц нельзя свести к {self.samplerate} Гц "
                "целым коэффициентом (см. audio.capture_samplerate / target_samplerate)"
            )

        model_file = Path(model_path) if model_path is not None else ensure_model()
        options = ort.SessionOptions()
        options.intra_op_num_threads = max(1, int(num_threads))
        options.inter_op_num_threads = 1
        options.log_severity_level = 3
        self._session = ort.InferenceSession(
            str(model_file), sess_options=options, providers=["CPUExecutionProvider"]
        )
        self._sr_tensor = np.array(self.samplerate, dtype=np.int64)

        # Потоковый ресемплер 48000 -> 16000 (коэффициент 3), состояние между блоками.
        self._resampler = soxr.ResampleStream(
            self.input_samplerate, self.samplerate, 1, dtype="float32", quality="HQ"
        )
        self._pending = np.zeros(0, dtype=np.float32)  # неполное окно 16 кГц
        self._context = np.zeros(CONTEXT_SAMPLES, dtype=np.float32)  # хвост прошлого окна
        self._state = np.zeros(STATE_SHAPE, dtype=np.float32)
        self._fed = 0  # сэмплов 16 кГц, поданных в модель (для отсчёта времени)
        self.clock_offset = 0.0  # сдвиг часов VAD к часам захвата, с
        self._in_speech = False
        self._speech_run = 0
        self._speech_start_t = 0.0
        self._silence_run = 0
        self._silence_start_t = 0.0
        self.last_prob = 0.0

        logger.info(
            "Silero VAD готов (CPU, потоков {}): {} Гц -> {} Гц, порог {}, "
            "min_speech {:.0f} мс, min_silence {:.0f} мс",
            options.intra_op_num_threads,
            self.input_samplerate,
            self.samplerate,
            self.threshold,
            min_speech_ms,
            silence_ms,
        )

    # --- публичный интерфейс ---

    @property
    def in_speech(self) -> bool:
        return self._in_speech

    @property
    def stream_seconds(self) -> float:
        """Сколько секунд аудио уже прошло через VAD (в часах захвата)."""
        return self._fed / self.samplerate + self.clock_offset

    def feed_block(
        self, pcm: bytes | bytearray | memoryview | np.ndarray
    ) -> list[VADEvent]:
        """Скормить блок PCM int16 моно (48 кГц). Вернуть события, если появились."""
        samples = self._as_float(pcm)
        if samples.size == 0:
            return []

        resampled = self._resampler.resample_chunk(samples, last=False)
        if resampled.size:
            if self._pending.size:
                self._pending = np.concatenate((self._pending, resampled))
            else:
                self._pending = resampled.astype(np.float32, copy=True)

        events: list[VADEvent] = []
        while self._pending.size >= WINDOW_SAMPLES:
            window = self._pending[:WINDOW_SAMPLES]
            self._pending = self._pending[WINDOW_SAMPLES:]
            events.extend(self._process_window(window))
        return events

    def reset(self) -> None:
        """Сбросить состояние модели и логику (например, при перезапуске потока)."""
        self._state = np.zeros(STATE_SHAPE, dtype=np.float32)
        self._context = np.zeros(CONTEXT_SAMPLES, dtype=np.float32)
        self._pending = np.zeros(0, dtype=np.float32)
        self._fed = 0
        self._in_speech = False
        self._speech_run = 0
        self._silence_run = 0
        self.last_prob = 0.0
        self._resampler.clear()

    # --- внутреннее ---

    @staticmethod
    def _as_float(pcm: bytes | bytearray | memoryview | np.ndarray) -> np.ndarray:
        if isinstance(pcm, np.ndarray):
            data = pcm.reshape(-1)
            if data.dtype == np.int16:
                return data.astype(np.float32) / 32768.0
            return data.astype(np.float32)
        raw = bytes(pcm)
        if len(raw) % 2:
            raw = raw[:-1]  # обрезаем неполный сэмпл
        return np.frombuffer(raw, dtype="<i2").astype(np.float32) / 32768.0

    def _process_window(self, window: np.ndarray) -> list[VADEvent]:
        window_t = self._fed / self.samplerate + self.clock_offset
        prob = self._infer(window)
        self._fed += self.window_samples
        self.last_prob = prob

        if prob >= self.threshold:
            if self._speech_run == 0:
                self._speech_start_t = window_t
            self._speech_run += self.window_samples
            self._silence_run = 0
            if not self._in_speech and self._speech_run >= self.min_speech_samples:
                self._in_speech = True
                logger.info(
                    "VAD: speech_start t={:.2f} c (prob={:.2f})",
                    self._speech_start_t,
                    prob,
                )
                return [{"type": "speech_start", "t": round(self._speech_start_t, 3)}]
            return []

        self._speech_run = 0
        if self._silence_run == 0:
            self._silence_start_t = window_t
        self._silence_run += self.window_samples
        if self._in_speech and self._silence_run >= self.min_silence_samples:
            self._in_speech = False
            self._silence_run = 0
            end_t = self._silence_start_t
            # Новая фраза начнётся с чистого состояния модели.
            self._state = np.zeros(STATE_SHAPE, dtype=np.float32)
            self._context = np.zeros(CONTEXT_SAMPLES, dtype=np.float32)
            logger.info("VAD: speech_end t={:.2f} c (prob={:.2f})", end_t, prob)
            return [{"type": "speech_end", "t": round(end_t, 3)}]
        return []

    def _infer(self, window: np.ndarray) -> float:
        # Модель Silero v5 ждёт 512 сэмплов + 64 сэмпла хвоста предыдущего окна.
        feed = np.concatenate((self._context, window)).reshape(1, -1)
        outputs = self._session.run(
            None,
            {
                "input": feed.astype(np.float32, copy=False),
                "state": self._state,
                "sr": self._sr_tensor,
            },
        )
        self._context = window[-CONTEXT_SAMPLES:].astype(np.float32, copy=True)
        self._state = np.asarray(outputs[1], dtype=np.float32).copy()
        return float(np.asarray(outputs[0]).reshape(-1)[0])

