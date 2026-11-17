"""Первый полный цикл разговора голосом:
микрофон -> VAD -> STT -> LLM -> TTS -> наушники.

Использование:
    from kivi.conversation import Conversation
    Conversation().run_once()

Шаги 0.1-0.5 не дублируются: используются готовые AudioCapture, VAD, Recorder,
transcribe_wav и speak.
"""

from __future__ import annotations

import os
import time
from dataclasses import dataclass

import requests
from dotenv import load_dotenv

from kivi.audio.capture import AudioCapture
from kivi.audio.recorder import Recorder
from kivi.audio.vad import VAD
from kivi.config import Settings, get_settings
from kivi.log import logger
from kivi.stt.transcribe import transcribe_wav
from kivi.tts.speak import Speech, speak

__all__ = ["Conversation", "ConversationError", "Turn"]

SYSTEM_PROMPT = (
    "Ты — Киви, голосовой ассистент Алексея. "
    "Отвечай коротко и дружелюбно, на русском."
)
WARMUP_SECONDS = 0.25  # первые блоки после открытия WASAPI не отдаём в VAD
LISTEN_TIMEOUT = 60.0  # секунд тишины до выхода
LLM_TIMEOUT = 60.0
MAX_REPLY_TOKENS = 600  # reasoning-модели тратят часть лимита на размышления


class ConversationError(RuntimeError):
    """Разговор не сложился: нет ключей, LLM не ответил, пустой текст."""


@dataclass(frozen=True)
class Turn:
    """Один цикл разговора."""

    heard: str
    reply: str
    speech: Speech

    def __str__(self) -> str:
        return f"услышано {self.heard!r} -> ответ {self.reply!r}"


class Conversation:
    """Слушает фразу, распознаёт, спрашивает LLM и озвучивает ответ."""

    def __init__(self, settings: Settings | None = None) -> None:
        self.settings = settings if settings is not None else get_settings()
        audio = self.settings.audio
        devices = self.settings.devices
        self.capture = AudioCapture(audio, devices.input_index)
        self.vad = VAD(audio)
        self.recorder = Recorder(audio)
        self.pre_roll_ms = float(audio.pre_roll_ms)
        logger.info(
            "Conversation готов: вход [{}] {}, выход [{}] {}",
            devices.input_index,
            devices.input_name,
            devices.output_index,
            devices.output_name,
        )

    # --- LLM ---

    @staticmethod
    def _llm_credentials() -> tuple[str, str]:
        """Адрес API и ключ из окружения. Значения ключа не печатаем никогда."""
        load_dotenv()
        base = (os.getenv("LLM_BASE_URL") or "").strip()
        key = (os.getenv("LLM_API_KEY") or "").strip()
        if not base or not key:
            raise ConversationError(
                "Не заданы LLM_BASE_URL и/или LLM_API_KEY в окружении (.env). "
                "Проверь файл .env и запусти из активированного .venv."
            )
        return base.rstrip("/"), key

    def think(self, user_text: str) -> str:
        """Отправить текст в LLM и вернуть ответ."""
        text = str(user_text).strip()
        if not text:
            raise ConversationError("Нечего отправлять в LLM: пустой текст")

        load_dotenv()
        model = (os.getenv("LLM_MODEL_CHEAP") or "").strip()
        if not model:
            raise ConversationError(
                "LLM_MODEL_CHEAP не задан в .env — не знаю, какую модель звать"
            )

        base, key = self._llm_credentials()
        url = f"{base}/chat/completions"
        payload = {
            "model": model,
            "messages": [
                {"role": "system", "content": SYSTEM_PROMPT},
                {"role": "user", "content": text},
            ],
            "max_tokens": MAX_REPLY_TOKENS,
        }

        logger.info("LLM: спрашиваю {}: {!r}", model, text)
        try:
            response = requests.post(
                url,
                headers={"Authorization": f"Bearer {key}"},
                json=payload,
                timeout=LLM_TIMEOUT,
            )
        except requests.RequestException as exc:
            raise ConversationError(f"Не удалось связаться с {url}: {exc}") from exc

        if response.status_code != 200:
            raise ConversationError(
                f"HTTP {response.status_code} от {url}: {response.text[:300]}"
            )

        try:
            body = response.json()
        except ValueError as exc:
            raise ConversationError(
                f"LLM ответил не JSON: {response.text[:300]}"
            ) from exc

        try:
            choice = body["choices"][0]
            message = choice.get("message") or {}
            reply = message.get("content")
            finish_reason = choice.get("finish_reason")
        except (KeyError, IndexError, TypeError, AttributeError) as exc:
            raise ConversationError(f"Неожиданный ответ LLM: {str(body)[:300]}") from exc

        usage = body.get("usage") or {}
        if usage:
            logger.debug(
                "LLM: finish={}, tokens={}, cost_rub={}",
                finish_reason,
                usage.get("total_tokens"),
                usage.get("cost_rub"),
            )

        answer = str(reply).strip() if reply else ""
        if not answer:
            raise ConversationError(
                f"LLM вернул пустой ответ (finish_reason={finish_reason}). "
                "Обычно так бывает, когда вся квота max_tokens ушла на размышления "
                "reasoning-модели."
            )
        logger.info("LLM: {}", answer)
        return answer

    # --- TTS ---

    def respond(self, text: str) -> Speech:
        """Озвучить текст в наушники (блокирует до конца проигрывания)."""
        phrase = str(text).strip()
        if not phrase:
            raise ConversationError("Нечего озвучивать: пустой текст")
        return speak(phrase)

# --- СЛУШАЕМ ---

    def listen_once(self, timeout: float = LISTEN_TIMEOUT) -> str:
        """Слушать до конца одной фразы и вернуть распознанный текст.

        Пустая строка — если за timeout речь так и не началась/не закончилась.
        """
        samplerate = self.capture.samplerate
        warmup_samples = int(WARMUP_SECONDS * samplerate)
        pre_roll_ms = self.pre_roll_ms

        self.vad.reset()
        self.recorder.cancel()
        self.capture.start()
        logger.info("Слушаю фразу (таймаут {:.0f} c)...", timeout)
        started_at = time.monotonic()
        samples_read = 0
        clock_set = False
        utterance = None

        try:
            while True:
                if time.monotonic() - started_at > timeout:
                    logger.warning("Тишина дольше {:.0f} c — прекращаю слушать", timeout)
                    break

                block = self.capture.read_block(timeout=0.5)
                if block is None:
                    if not self.capture.is_running:
                        break
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
                started_here = False
                finished = False
                for event in self.vad.feed_block(block):
                    if event["type"] == "speech_start":
                        # Pre-roll из кольцевого буфера: речь не обрезается спереди.
                        pre_ms = (block_end_t - event["t"]) * 1000.0 + pre_roll_ms
                        self.recorder.start(event["t"], self.capture.snapshot(pre_ms))
                        logger.info("Фраза началась на {:.2f} c", event["t"])
                        started_here = True
                    else:
                        utterance = self.recorder.stop(event["t"])
                        if utterance is not None:
                            logger.info("Фраза закончилась: {}", utterance)
                            finished = True
                        else:
                            logger.info("Фраза слишком короткая — слушаю дальше")

                # Текущий блок уже попал в pre-roll — второй раз не добавляем.
                if self.recorder.is_recording and not started_here:
                    self.recorder.push(block)

                if finished:
                    break
        finally:
            self.capture.stop()
            if self.recorder.is_recording:
                self.recorder.cancel()

        if utterance is None:
            return ""

        logger.info("STT: распознаю {}", utterance.path.name)
        return transcribe_wav(utterance.path)

    # --- ЦИКЛ ---

    def run_once(self, timeout: float = LISTEN_TIMEOUT) -> Turn | None:
        """listen_once() -> think() -> respond(). None — если была тишина."""
        heard = self.listen_once(timeout=timeout)
        if not heard.strip():
            logger.warning("Не расслышал ничего — цикл не начался")
            return None

        reply = self.think(heard)
        speech = self.respond(reply)
        turn = Turn(heard=heard, reply=reply, speech=speech)
        logger.info("Цикл завершён: {}", turn)
        return turn
