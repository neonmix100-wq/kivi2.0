"""Голосовое ядро: захват -> VAD -> запись фразы.

Поток читает блоки из кольцевого буфера AudioCapture, отдаёт их в VAD,
события VAD управляют Recorder. run() блокирующий.
"""

from __future__ import annotations

import threading
import time
from collections.abc import Callable

from kivi.audio.capture import AudioCapture
from kivi.audio.recorder import Recorder, Utterance
from kivi.audio.vad import VAD, VADEvent
from kivi.config import Settings, get_settings
from kivi.log import logger

__all__ = ["EventCallback", "UtteranceCallback", "VoicePipeline"]

EventCallback = Callable[[VADEvent], None]
UtteranceCallback = Callable[[Utterance], None]

WARMUP_SECONDS = 0.25  # первые 250 мс после открытия потока в VAD не отдаём


class VoicePipeline:
    """Связка capture + VAD + recorder одним потоком-обработчиком."""

    def __init__(
        self,
        settings: Settings | None = None,
        *,
        on_event: EventCallback | None = None,
        on_utterance: UtteranceCallback | None = None,
    ) -> None:
        self.settings = settings if settings is not None else get_settings()
        audio = self.settings.audio
        devices = self.settings.devices

        self.pre_roll_ms = float(audio.pre_roll_ms)
        self.capture = AudioCapture(audio, devices.input_index)
        self.vad = VAD(audio)
        self.recorder = Recorder(audio)

        self.on_event = on_event
        self.on_utterance = on_utterance
        self.utterances: list[Utterance] = []

        self._stop = threading.Event()
        self._thread: threading.Thread | None = None
        self._error: BaseException | None = None

    # --- управление ---

    def start(self) -> None:
        """Открыть микрофон и запустить обработчик."""
        if self._thread is not None and self._thread.is_alive():
            return
        self._stop.clear()
        self._error = None
        self.vad.reset()
        self.capture.start()
        self._thread = threading.Thread(target=self._worker, name="kivi-voice", daemon=True)
        self._thread.start()
        logger.info(
            "Слушаю микрофон [{idx}] {name}. Ctrl+C — стоп.",
            idx=self.settings.devices.input_index,
            name=self.settings.devices.input_name,
        )

    def stop(self) -> None:
        """Остановить обработчик и микрофон."""
        self._stop.set()
        self.capture.stop()
        thread, self._thread = self._thread, None
        if thread is not None and thread.is_alive():
            thread.join(timeout=2.0)
        self.recorder.cancel()
        if self._error is not None:
            error, self._error = self._error, None
            raise RuntimeError(f"Голосовое ядро остановлено из-за ошибки: {error}") from error

    def run(
        self,
        max_seconds: float | None = None,
        *,
        on_event: EventCallback | None = None,
        on_utterance: UtteranceCallback | None = None,
    ) -> list[Utterance]:
        """Блокирующий прогон. max_seconds=None — до Ctrl+C. Возвращает записанные фразы."""
        if on_event is not None:
            self.on_event = on_event
        if on_utterance is not None:
            self.on_utterance = on_utterance

        self.start()
        started = time.monotonic()
        try:
            while not self._stop.is_set():
                self._stop.wait(0.1)
                if max_seconds is not None and time.monotonic() - started >= max_seconds:
                    break
        except KeyboardInterrupt:
            logger.info("Остановка по Ctrl+C")
        finally:
            self.stop()
        return self.utterances

    # --- обработка ---

    def _worker(self) -> None:
        samplerate = self.capture.samplerate
        warmup_samples = int(WARMUP_SECONDS * samplerate)
        samples_read = 0
        clock_set = False
        try:
            while not self._stop.is_set():
                block = self.capture.read_block(timeout=0.5)
                if block is None:
                    if not self.capture.is_running:
                        break  # микрофон закрыт — выходим
                    continue
                samples_read += block.size
                if samples_read <= warmup_samples:
                    # Первые блоки после открытия потока — возможен щелчок драйвера.
                    continue
                if not clock_set:
                    # VAD начинает свой отсчёт с этого блока — выравниваем часы.
                    self.vad.clock_offset = (samples_read - block.size) / samplerate
                    clock_set = True
                block_end_t = samples_read / samplerate
                events = self.vad.feed_block(block)

                started_here = False
                for event in events:
                    if event["type"] == "speech_start":
                        # Кольцевой буфер отдаёт последние N мс. К началу речи
                        # добавляем задержку VAD (min_speech), поэтому pre-roll
                        # получается больше номинального — первое слово не режется.
                        pre_ms = (block_end_t - event["t"]) * 1000.0 + self.pre_roll_ms
                        self.recorder.start(event["t"], self.capture.snapshot(pre_ms))
                        started_here = True
                    elif event["type"] == "speech_end":
                        utterance = self.recorder.stop(event["t"])
                        if utterance is not None:
                            self.utterances.append(utterance)
                            if self.on_utterance is not None:
                                self.on_utterance(utterance)
                    if self.on_event is not None:
                        self.on_event(event)

                # Текущий блок уже попал в pre-roll — второй раз не добавляем.
                if self.recorder.is_recording and not started_here:
                    self.recorder.push(block)
        except Exception as exc:  # noqa: BLE001 - пробрасываем в run()/stop()
            self._error = exc
            logger.exception("Голосовое ядро упало: {}", exc)
            self._stop.set()

    # --- диагностика ---

    @property
    def stats(self) -> dict[str, int]:
        return self.capture.stats

    def __enter__(self) -> "VoicePipeline":
        self.start()
        return self

    def __exit__(self, *exc_info: object) -> None:
        self.stop()
