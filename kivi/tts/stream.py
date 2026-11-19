"""Озвучка ответа по мере генерации: один непрерывный поток, без щелчков.

Отличия от покадрового ffplay:
  * один sounddevice.OutputStream на весь ответ (плеер не перезапускаем);
  * чанки ~2-3 предложения (200-300 символов), чтобы реже сбрасывать просодию;
  * между чанками кроссфейд 30 мс — нет щелчка и рваного стыка;
  * если первый чанк не готов за 1.5 с — один раз звучит короткая вводная
    из tts_cache («Секунду»/«Мгновение»/«Так, думаю») тем же потоком;
    фраза запоминается как «сыгранная» и не повторяется в следующий раз,
    а если вариантов меньше двух — вводная не играется вовсе.

Использование:
    speaker = SentenceSpeaker(started_at=time.perf_counter())
    for chunk in router.complete_stream(messages):
        speaker.feed(chunk)
    total = speaker.finish()
"""

from __future__ import annotations

import queue
import random
import re
import threading
import time
import uuid
from collections.abc import Callable
from dataclasses import dataclass

import numpy as np
import sounddevice as sd

from kivi.brain.persona import to_speech
from kivi.config import get_settings
from kivi.log import logger
from kivi.tts.cache import synthesize_cached
from kivi.tts.speak import TMP_DIR, _move_to_slot, _tts_settings, mp3_to_pcm, synthesize_mp3

__all__ = [
    "ACK_DELAY_S",
    "ACK_PHRASES",
    "FIRST_CHUNK_MAX",
    "FIRST_CHUNK_MIN",
    "MAX_CHUNK_CHARS",
    "MIN_SENTENCE_CHARS",
    "OVERLAP_MS",
    "TARGET_CHUNK_CHARS",
    "SentenceSpeaker",
    "last_ack_phrase",
    "pick_ack_phrase",
]

MIN_SENTENCE_CHARS = 20
FIRST_CHUNK_MIN = 120  # первый чанк: копим хотя бы столько символов
FIRST_CHUNK_MAX = 200  # ...и не больше — чтобы раньше прозвучал первый звук
TARGET_CHUNK_CHARS = 250  # 2-3 предложения в чанке
MAX_CHUNK_CHARS = 320
OVERLAP_MS = 30  # кроссфейд на стыке чанков
ACK_DELAY_S = 1.5
ACK_PHRASES: tuple[str, ...] = ("Секунду", "Мгновение", "Так, думаю")

_SENTENCE_END = re.compile(r"[.!?…]+[\"'»)]?(?=\s)|\n+")
_COMMA = ",;:"
# Последняя РЕАЛЬНО СЫГРАННАЯ вводная: обновляется в плеере, а не при выборе,
# иначе фраза «сгорает» беззвучно и следующий выбор может повторить слышимую.
_last_ack: str | None = None


@dataclass
class _AudioChunk:
    index: int
    pcm: np.ndarray
    synth_s: float
    is_ack: bool = False
    text: str | None = None  # текст вводной (для лога и памяти о повторе)
    done: threading.Event | None = None  # ждём фактического проигрывания


def pick_ack_phrase() -> str | None:
    """Выбрать вводную, отличную от предыдущей сыгранной.

    None означает «молчим»: если вариантов меньше двух, повторять одну и ту же
    фразу бессмысленно — лучше не играть ничего.
    """
    if len(ACK_PHRASES) < 2:
        logger.debug("озвучка: вводных меньше двух — играю молча")
        return None
    options = [phrase for phrase in ACK_PHRASES if phrase != _last_ack]
    if not options:
        logger.debug("озвучка: все вводные совпали с прошлой — играю молча")
        return None
    return random.choice(options)


def _mark_ack_played(phrase: str) -> None:
    """Запомнить вводную, которая действительно прозвучала."""
    global _last_ack
    _last_ack = phrase


def last_ack_phrase() -> str | None:
    """Какая вводная звучала последней (для логов и тестов)."""
    return _last_ack


class SentenceSpeaker:
    """Синтез чанками и одно непрерывное воспроизведение на весь ответ."""

    def __init__(
        self,
        *,
        model: str | None = None,
        voice: str | None = None,
        started_at: float | None = None,
        transform: Callable[[str], str] | None = None,
        ack_delay_s: float = ACK_DELAY_S,
    ) -> None:
        self.run_id = uuid.uuid4().hex[:8]
        self.model_id, self.voice_id = _tts_settings(model, voice)
        self.transform = transform or to_speech
        self.started_at = started_at if started_at is not None else time.perf_counter()
        self.ack_delay_s = float(ack_delay_s)

        settings = get_settings()
        self._device_index = settings.devices.output_index
        self._output_sr, self._out_channels = self._pick_output_format()
        self._overlap = max(1, int(self._output_sr * OVERLAP_MS / 1000))

        self._buffer = ""
        self._first = True  # первый чанк режем короче: быстрее первый звук
        self._lock = threading.Lock()
        self._text_q: queue.Queue[str | None] = queue.Queue()
        self._audio_q: queue.Queue[_AudioChunk | None] = queue.Queue()

        self._stream: sd.OutputStream | None = None
        self._tail = np.zeros(0, dtype=np.int16)
        self._samples_written = 0
        self._chunk_count = 0

        self._stopping = threading.Event()
        self._first_ready = threading.Event()
        self._first_sound_at: float | None = None
        self._ack_played = False

        logger.info("озвучка: старт id={}, len=stream", self.run_id)
        self._synth_thread = threading.Thread(
            target=self._synth_loop, name="kivi-tts-synth", daemon=True
        )
        self._play_thread = threading.Thread(
            target=self._play_loop, name="kivi-tts-play", daemon=True
        )
        self._synth_thread.start()
        self._play_thread.start()

        self._ack_timer = threading.Timer(self.ack_delay_s, self._maybe_ack)
        self._ack_timer.daemon = True
        self._ack_timer.start()

    # --- устройство ---

    def _pick_output_format(self) -> tuple[int, int]:
        """Частота и число каналов, которые реально примет устройство вывода."""
        preferred = int(get_settings().audio.playback_samplerate)
        try:
            max_channels = int(sd.query_devices(self._device_index)["max_output_channels"])
        except Exception:  # noqa: BLE001
            max_channels = 1
        channels = 2 if max_channels >= 2 else 1
        try:
            sd.check_output_settings(
                device=self._device_index, samplerate=preferred, channels=channels, dtype="int16"
            )
            return preferred, channels
        except Exception:  # noqa: BLE001 - устройство не приняло формат
            try:
                default_sr = int(float(sd.query_devices(self._device_index)["default_samplerate"]))
            except Exception:  # noqa: BLE001
                default_sr = 48000
            logger.warning(
                "озвучка: устройство [{}] не приняло {} Гц/{} кан. — играю на {} Гц/{} кан.",
                self._device_index,
                preferred,
                channels,
                default_sr,
                channels,
            )
            return default_sr, channels

    # --- вход ---

    def feed(self, chunk: str) -> None:
        """Добавить кусок текста; готовые чанки уходят в синтез."""
        if not chunk or self._stopping.is_set():
            return
        with self._lock:
            self._buffer += str(chunk)
            while True:
                piece = self._extract_chunk()
                if piece is None:
                    break
                self._text_q.put(piece)

    def _extract_chunk(self) -> str | None:
        """Выделить готовый чанк: 2-3 предложения (200-320 символов).

        Первый чанк короче (~120-200 символов): он определяет, как быстро
        зазвучит ответ. Остальные — крупнее, чтобы реже сбрасывать просодию.
        """
        text = self._buffer
        limit = FIRST_CHUNK_MIN if self._first else TARGET_CHUNK_CHARS
        if len(text) < limit:
            return None

        window = text[: FIRST_CHUNK_MAX if self._first else MAX_CHUNK_CHARS]
        bounds = [match.end() for match in _SENTENCE_END.finditer(window)]
        if self._first:
            cut = bounds[-1] if bounds and bounds[-1] >= limit else self._soft_cut(window)
            self._first = False
        else:
            long_enough = [end for end in bounds if end >= TARGET_CHUNK_CHARS]
            if long_enough:
                cut = long_enough[0]
            elif bounds:
                cut = bounds[-1]
            else:
                cut = self._soft_cut(window)
        part = text[:cut].strip()
        self._buffer = text[cut:]
        return part or None

    @staticmethod
    def _soft_cut(window: str) -> int:
        """Резать внутри слишком длинного предложения: запятая, потом пробел."""
        cut = max(window.rfind(ch) for ch in _COMMA)
        if cut < MIN_SENTENCE_CHARS:
            cut = window.rfind(" ")
        if cut < MIN_SENTENCE_CHARS:
            cut = len(window) - 1
        return cut + 1

    # --- фоновые потоки ---

    def _synth_loop(self) -> None:
        index = 0
        try:
            while True:
                piece = self._text_q.get()
                if piece is None:
                    return
                if self._stopping.is_set():
                    continue
                prepared = " ".join(self.transform(piece).split())
                if not prepared:
                    logger.debug("озвучка: чанк пустой после чистки — пропуск")
                    continue
                index += 1
                started = time.perf_counter()
                try:
                    mp3 = synthesize_mp3(
                        prepared,
                        model=self.model_id,
                        voice=self.voice_id,
                        run_id=f"{self.run_id}{index:02d}",
                    )
                    pcm_path = mp3_to_pcm(
                        mp3,
                        samplerate=self._output_sr,
                        pcm_path=TMP_DIR / f"tts_{self.run_id}{index:02d}.pcm",
                    )
                    pcm = np.frombuffer(pcm_path.read_bytes(), dtype=np.int16).copy()
                    _move_to_slot(pcm_path, TMP_DIR / "tts_last.pcm")
                    if mp3.parent == TMP_DIR:  # файл из кэша трогать нельзя
                        _move_to_slot(mp3, TMP_DIR / "tts_last.mp3")
                except Exception as exc:  # noqa: BLE001 - один чанк не должен рвать ответ
                    logger.error("озвучка: чанк {} не синтезирован: {}", index, exc)
                    continue
                synth_s = time.perf_counter() - started
                self._first_ready.set()
                logger.debug(
                    "озвучка: чанк {} ({:.0f} симв.) синтезирован за {:.2f} с",
                    index,
                    len(prepared),
                    synth_s,
                )
                self._audio_q.put(_AudioChunk(index=index, pcm=pcm, synth_s=synth_s))
        finally:
            # Сторож: плеер не должен ждать вечно, если синтез упал.
            self._audio_q.put(None)

    def _ack_pcm(self, text: str) -> np.ndarray | None:
        """PCM вводной из tts_cache (синтез — только если фразы ещё нет в кэше)."""
        try:
            mp3 = synthesize_cached(text, model=self.model_id, voice=self.voice_id)
            pcm_path = mp3_to_pcm(
                mp3,
                samplerate=self._output_sr,
                pcm_path=TMP_DIR / f"tts_{self.run_id}00.pcm",
            )
            pcm = np.frombuffer(pcm_path.read_bytes(), dtype=np.int16).copy()
            _move_to_slot(pcm_path, TMP_DIR / "tts_last.pcm")
            return pcm
        except Exception as exc:  # noqa: BLE001 - без вводной можно жить
            logger.warning("озвучка: вводную «{}» подготовить не удалось: {}", text, exc)
            return None

    def _maybe_ack(self) -> None:
        """Вводная фраза, если первый чанк не успел за ack_delay_s."""
        if self._stopping.is_set() or self._first_ready.is_set():
            return
        text = pick_ack_phrase()
        if text is None:
            return
        logger.debug("озвучка: выбрана вводная «{}» (порог {:.1f} с)", text, self.ack_delay_s)
        pcm = self._ack_pcm(text)
        if pcm is None:
            return
        if self._first_ready.is_set() or self._stopping.is_set():
            return  # пока готовили — ответ уже пошёл
        self._ack_played = True
        self._audio_q.put(_AudioChunk(index=0, pcm=pcm, synth_s=0.0, is_ack=True, text=text))

    def play_ack(self, phrase: str | None = None) -> str | None:
        """Проиграть одну вводную и вернуть её текст (None — если решили молчать).

        Нужна для ручных сценариев и смоука: играет тем же непрерывным потоком,
        а «последней сыгранной» фраза становится только после воспроизведения.
        """
        if not self._play_thread.is_alive():
            logger.warning("озвучка: плеер уже закрыт — вводную не играю")
            return None
        text = phrase if phrase is not None else pick_ack_phrase()
        if text is None:
            return None
        pcm = self._ack_pcm(text)
        if pcm is None:
            return None
        done = threading.Event()
        self._audio_q.put(
            _AudioChunk(index=0, pcm=pcm, synth_s=0.0, is_ack=True, text=text, done=done)
        )
        timeout = 10.0 + pcm.size / float(self._output_sr)
        if not done.wait(timeout=timeout):
            logger.warning("озвучка: вводная «{}» не проигралась за {:.0f} с", text, timeout)
            return None
        return text

    # --- воспроизведение ---

    def _ensure_stream(self) -> None:
        if self._stream is not None:
            return
        self._stream = sd.OutputStream(
            samplerate=self._output_sr,
            channels=self._out_channels,
            dtype="int16",
            device=self._device_index,
        )
        self._stream.start()
        logger.debug(
            "озвучка: OutputStream открыт ({} Гц, {} кан., кроссфейд {} мс)",
            self._output_sr,
            self._out_channels,
            OVERLAP_MS,
        )

    def _frames(self, mono: np.ndarray) -> np.ndarray:
        """Моно-сэмплы в формат устройства (при стерео дублируем канал)."""
        if self._out_channels == 1:
            return mono.reshape(-1, 1)
        return np.repeat(mono.reshape(-1, 1), self._out_channels, axis=1)

    def _write_pcm(self, chunk: np.ndarray) -> None:
        """Записать чанк с кроссфейдом на стыке (без щелчков).

        Последние OVERLAP_MS предыдущего чанка придерживаем и подмешиваем
        к первым OVERLAP_MS нового: амплитуда меняется плавно, щелчка нет.
        """
        if chunk.size == 0 or self._stream is None:
            return
        overlap = self._overlap

        if self._tail.size == 0:
            # начало ответа: хвост придержим до прихода следующего чанка
            if chunk.size <= overlap:
                self._tail = chunk.copy()
                return
            self._stream.write(self._frames(chunk[:-overlap]))
            self._samples_written += chunk.size - overlap
            self._tail = chunk[-overlap:].copy()
            return

        tail = self._tail
        if tail.size < overlap:  # добить слева, если предыдущий чанк был коротким
            tail = np.pad(tail, (overlap - tail.size, 0))

        head = chunk[:overlap]
        if head.size < overlap:
            head = np.pad(head, (0, overlap - head.size))
        fade_in = np.linspace(0.0, 1.0, overlap, dtype=np.float32)
        mixed = tail.astype(np.float32) * (1.0 - fade_in) + head.astype(np.float32) * fade_in
        self._stream.write(self._frames(np.clip(mixed, -32768, 32767).astype(np.int16)))
        self._samples_written += overlap

        rest = chunk[overlap:]
        if rest.size > overlap:
            self._stream.write(self._frames(rest[:-overlap]))
            self._samples_written += rest.size - overlap
            self._tail = rest[-overlap:].copy()
        elif rest.size > 0:
            self._stream.write(self._frames(rest))
            self._samples_written += rest.size
            self._tail = np.zeros(0, dtype=np.int16)
        else:
            self._tail = np.zeros(0, dtype=np.int16)

    def _play_loop(self) -> None:
        try:
            while True:
                item = self._audio_q.get()
                if item is None:
                    return
                try:
                    if self._stopping.is_set():
                        continue
                    if self._first_sound_at is None:
                        self._first_sound_at = time.perf_counter()
                        logger.info(
                            "озвучка: первый звук через {:.2f} с (id={})",
                            self._first_sound_at - self.started_at,
                            self.run_id,
                        )
                    at = time.perf_counter() - self.started_at
                    self._ensure_stream()
                    self._write_pcm(item.pcm)
                    if item.is_ack:
                        # «Сыграно» фиксируем здесь: только теперь фраза реально звучала.
                        if item.text:
                            _mark_ack_played(item.text)
                        logger.info(
                            "озвучка: вводная «{}» сыграна (id={}, t=+{:.2f} с, {:.2f} с)",
                            item.text,
                            self.run_id,
                            at,
                            item.pcm.size / self._output_sr,
                        )
                    else:
                        self._chunk_count = max(self._chunk_count, item.index)
                        logger.info(
                            "озвучка: чанк {}: синтез {:.2f} с, речь {:.2f} с (t=+{:.2f} с)",
                            item.index,
                            item.synth_s,
                            item.pcm.size / self._output_sr,
                            at,
                        )
                finally:
                    if item.done is not None:
                        item.done.set()
        finally:
            if self._stream is not None:
                try:
                    if self._tail.size:
                        self._stream.write(self._frames(self._tail))
                        self._samples_written += self._tail.size
                        self._tail = np.zeros(0, np.int16)
                except Exception:  # noqa: BLE001
                    pass
                try:
                    self._stream.stop()
                    self._stream.close()
                except Exception:  # noqa: BLE001
                    pass
                self._stream = None

    # --- завершение ---

    def finish(self) -> float:
        """Доиграть очередь и вернуть суммарную длительность речи (секунды)."""
        with self._lock:
            rest = self._buffer.strip()
            self._buffer = ""
            if rest:
                self._text_q.put(rest)
        if not self._first_ready.is_set():
            # Первого звука ещё нет: даём вводной шанс прозвучать до ответа.
            self._ack_timer.join()
        self._text_q.put(None)
        self._play_thread.join()
        self._synth_thread.join(timeout=2.0)
        total = self.played_seconds
        logger.info(
            "озвучка: конец id={}, длительность={:.2f} (чанков: {})",
            self.run_id,
            total,
            self._chunk_count,
        )
        return total

    def stop(self) -> None:
        """Очистить очередь и прервать воспроизведение (заготовка barge-in)."""
        self._stopping.set()
        try:
            self._ack_timer.cancel()
        except Exception:  # noqa: BLE001
            pass
        while True:
            try:
                self._text_q.get_nowait()
            except queue.Empty:
                break
        while True:
            try:
                item = self._audio_q.get_nowait()
            except queue.Empty:
                break
            if item is not None and item.done is not None:
                item.done.set()  # иначе play_ack() будет ждать впустую
        self._text_q.put(None)
        self._play_thread.join(timeout=2.0)
        self._synth_thread.join(timeout=2.0)
        logger.info("озвучка: остановлено вручную (id={})", self.run_id)

    @property
    def sentences(self) -> int:
        return self._chunk_count

    @property
    def played_seconds(self) -> float:
        """Сколько секунд звука уже ушло в устройство."""
        return self._samples_written / float(self._output_sr)
