"""Постоянная прослушка и отзыв на имя «Киви».

Конечный автомат:
    IDLE -> (VAD speech_end) -> локальный Whisper -> is_wake?
      имя + команда   -> THINKING (one-shot)
      только имя      -> сказать «Слушаю», открыть окно диалога
      нет имени       -> в окне диалога: команда без имени; иначе IDLE молча
    THINKING -> planner.plan(command)   # LLM-планировщик + difflib-фолбэк
      route=skill       -> loader.invoke -> SPEAKING (Router не зовём)
      route=search      -> search_web + summarize -> SPEAKING (одна реплика)
      route=action_gap  -> пробел: GapCloser или короткий честный ответ
      route=chat        -> Router.complete_stream + SentenceSpeaker
    После любого ответа: окно диалога открыто ещё silence_s секунд тишины —
    продолжение можно говорить без имени. Фразы «конец диалога/отбой» закрывают
    окно, а тишина дольше silence_s закрывает его автоматически.
    Ровно один ответ на команду; второй ответ через chat запрещён.

Никаких облачных вызовов для проверки имени: распознаёт локальный faster-whisper.
Каждое распознанное высказывание пишется в data/logs/wake.log.
"""

from __future__ import annotations

import threading
import time
from collections.abc import Callable
from dataclasses import dataclass
from typing import TYPE_CHECKING, Any, Literal

from loguru import logger

from kivi.audio.capture import AudioCapture
from kivi.audio.recorder import Recorder, Utterance
from kivi.audio.vad import VAD
from kivi.brain.persona import PERSONA_PROMPT, to_speech
from kivi.brain.session import Session
from kivi.config import LOG_DIR_PATH as LOG_DIR
from kivi.config import Settings, get_settings
from kivi.stt.local import transcribe_wav
from kivi.tts.stream import SentenceSpeaker
from kivi.wake.detector import detect

if TYPE_CHECKING:
    from kivi.skills.gaps import GapLog
    from kivi.skills.planner import Planner
    from kivi.skills.registry import SkillManifest, SkillRegistry

__all__ = [
    "COOLDOWN_S",
    "DIALOG_SILENCE_S",
    "DIALOG_WINDOW_S",
    "END_PHRASES",
    "FOLLOWUP_WINDOW_S",
    "GREETING_PHRASES",
    "MAX_UTTERANCE_S",
    "MIN_UTTERANCE_S",
    "WAKE_LOG",
    "WakeLoop",
    "WakeResult",
]

WAKE_LOG = LOG_DIR / "wake.log"
WARMUP_SECONDS = 0.25
MIN_UTTERANCE_S = 0.4
MAX_UTTERANCE_S = 15.0
COOLDOWN_S = 1.5
DIALOG_SILENCE_S = 8.0
DIALOG_WINDOW_S = DIALOG_SILENCE_S  # устаревшее имя, оставлено для совместимости
FOLLOWUP_WINDOW_S = DIALOG_SILENCE_S  # устаревшее имя
ACK_PHRASE = "Слушаю"
END_PHRASES = ("конец диалога", "заверши диалог", "отбой")
GREETING_PHRASES = (
    "привет",
    "здравствуй",
    "хай",
    "приветик",
    "добрый день",
    "добрый вечер",
)

Action = Literal["idle", "listen", "think", "followup", "cooldown"]
State = Literal["IDLE", "THINKING", "SPEAKING"]


@dataclass(frozen=True)
class WakeResult:
    """Одно распознанное высказывание и решение по нему."""

    heard: str
    is_wake: bool
    command: str
    action: Action
    fuzzy: bool = False
    duration_s: float = 0.0

    def __str__(self) -> str:
        mark = "WAKE" if self.is_wake else "----"
        fuzzy = " (нечёткое)" if self.fuzzy else ""
        return (
            f"[{mark}]{fuzzy} {self.heard!r} | команда: {self.command!r} | "
            f"решение: {self.action}"
        )


_wake_sink_ready = False


def _ensure_wake_log() -> None:
    """Отдельный файл data/logs/wake.log для срабатываний."""
    global _wake_sink_ready
    if _wake_sink_ready:
        return
    LOG_DIR.mkdir(parents=True, exist_ok=True)
    logger.add(
        str(WAKE_LOG),
        level="DEBUG",
        filter=lambda record: bool(record["extra"].get("wake")),
        rotation="10 MB",
        retention=5,
        encoding="utf-8",
        enqueue=True,
        format="{time:YYYY-MM-DD HH:mm:ss.SSS} | {level: <7} | {message}",
    )
    _wake_sink_ready = True


wake_log = logger.bind(wake=True)


def _normalize_phrase(text: str) -> str:
    """Нижний регистр, ё->е, пунктуация -> пробелы (как в registry)."""
    value = str(text or "").replace("ё", "е").replace("Ё", "Е").lower()
    cleaned = "".join(ch if ch.isalnum() or ch.isspace() else " " for ch in value)
    return " ".join(cleaned.split())


def _find_phrase(
    text: str,
    phrases: tuple[str, ...] | list[str],
    *,
    prefix: bool = False,
) -> str | None:
    """Вернуть первую найденную фразу из списка (или None)."""
    words = _normalize_phrase(text).split()
    if not words:
        return None
    joined = " ".join(words)
    for phrase in phrases:
        needle = _normalize_phrase(phrase)
        if not needle:
            continue
        if " " in needle:
            if f" {needle} " in f" {joined} ":
                return str(phrase)
        elif needle in words or (prefix and any(word.startswith(needle) for word in words)):
            return str(phrase)
    return None


def _contains_phrase(
    text: str,
    phrases: tuple[str, ...] | list[str],
    *,
    prefix: bool = False,
) -> bool:
    """Есть ли фраза из списка в тексте (целыми словами; prefix — для приветствий)."""
    return _find_phrase(text, phrases, prefix=prefix) is not None


def _default_speak(text: str) -> None:
    """Озвучка через кэш (служебные фразы — бесплатно после первого раза)."""
    from kivi.tts.cache import speak_cached

    speak_cached(text)


class WakeLoop:
    """Слушает микрофон, ловит имя «Киви» и обрабатывает команды."""

    def __init__(
        self,
        settings: Settings | None = None,
        *,
        router=None,
        capture: AudioCapture | None = None,
        vad: VAD | None = None,
        recorder: Recorder | None = None,
        speak_fn: Callable[[str], object] | None = None,
        think_fn: Callable[[str], str] | None = None,
        on_result: Callable[[WakeResult], None] | None = None,
        session: Session | None = None,
        silence_s: float | None = None,
        cooldown_s: float = COOLDOWN_S,
    ) -> None:
        _ensure_wake_log()

        self.settings = settings if settings is not None else get_settings()
        audio = self.settings.audio
        devices = self.settings.devices
        self.capture = capture or AudioCapture(audio, devices.input_index)
        self.vad = vad or VAD(audio)
        self.recorder = recorder or Recorder(audio)
        self.router = router
        self.speak_fn = speak_fn or _default_speak
        self.think_fn = think_fn
        self.on_result = on_result
        self.session = session
        self.cooldown_s = float(cooldown_s)

        # Окно диалога = тишина пользователя после ответа Киви.
        conversation = self.settings.section("conversation")
        self.conversation_enabled = bool(conversation.get("enabled", True))
        configured_silence = conversation.get("silence_s")
        chosen_silence = silence_s if silence_s is not None else configured_silence
        try:
            self.silence_s = max(
                1.0, float(chosen_silence if chosen_silence else DIALOG_SILENCE_S)
            )
        except (TypeError, ValueError):
            self.silence_s = DIALOG_SILENCE_S
        self.end_phrases = tuple(conversation.get("end_phrases") or END_PHRASES)
        self.greeting_phrases = tuple(
            conversation.get("greeting_phrases") or GREETING_PHRASES
        )

        self.pre_roll_ms = float(audio.pre_roll_ms)
        self._state: State = "IDLE"
        self.api: Any | None = None  # WS-сервер (kivi.api.server.ApiServer)
        self.window_until = 0.0
        self.last_wake_ts = 0.0
        self._speaking = False  # True пока Киви озвучивает ответ (защита от самослушания)
        self._stop = threading.Event()
        self._speaker: SentenceSpeaker | None = None  # активная озвучка ответа

        # Долгая память: блок в промпт + рефлексия по расписанию.
        memory_cfg = self.settings.section("memory")
        self.memory_enabled = bool(memory_cfg.get("enabled", True))
        self._memory_lock = threading.Lock()
        self._turns_since_reflect = 0
        self._last_activity_ts = time.time()
        self._idle_threshold_s = float(memory_cfg.get("reflect_on_idle_min") or 10) * 60.0
        self.reflector = None
        self._reflect_thread: threading.Thread | None = None
        if self.memory_enabled:
            try:
                from kivi.memory.reflect import Reflector

                self.reflector = Reflector(router)
            except Exception as exc:  # noqa: BLE001 - память не должна блокировать запуск
                logger.warning("Память: рефлексия недоступна: {}", exc)

        # Умения: реестр + планировщик (LLM-first, difflib как фолбэк).
        # Битые манифесты просто пропускаются, и команда уходит в Router как раньше.
        self.skills: SkillRegistry | None = None
        self.planner: Planner | None = None
        self.gaps: GapLog | None = None
        self.gap_config = self.settings.section("gap_closer")
        self.search_config = self.settings.section("search")
        try:
            from kivi.skills.gaps import get_gap_log
            from kivi.skills.planner import Planner as _Planner
            from kivi.skills.registry import SkillRegistry as _SkillRegistry

            registry = _SkillRegistry.from_config(self.settings.section("skills"))
            registry.scan()
            self.skills = registry
            self.gaps = get_gap_log()
            # Тот же router: в dry-run смоуке планировщик ничего не потратит.
            self.planner = _Planner(registry, router=router, gaps=self.gaps)
        except Exception as exc:  # noqa: BLE001 - скиллы не должны блокировать запуск
            logger.warning("Скиллы: недоступны: {}: {}", type(exc).__name__, exc)

        logger.info(
            "WakeLoop готов: вход [{}] {}, окно {:.0f} c, cooldown {:.1f} c, память: {}",
            devices.input_index,
            devices.input_name,
            self.silence_s,
            self.cooldown_s,
            "вкл" if self.memory_enabled else "выкл",
        )

    # --- публичный интерфейс ---

    def stop(self) -> None:
        self._stop.set()
        speaker = self._speaker
        if speaker is not None:
            try:
                speaker.stop()  # непрерывный поток надо закрыть, иначе он держит устройство
            except Exception as exc:  # noqa: BLE001 - остановка не должна падать
                wake_log.debug("не удалось прервать озвучку: {}", exc)
        self.capture.stop()
        try:
            self.vad.release()  # освобождаем nanobind-объект soxr
        except Exception as exc:  # noqa: BLE001 - не должно мешать остановке
            wake_log.debug("не удалось освободить soxr-ресемплер: {}", exc)
        # Рефлексию при выходе ждёт main (wait_reflection) ДО остановки потоков.

    @property
    def state(self) -> State:
        """Текущее состояние; смена шлёт status в WS-API (если подключён)."""
        return self._state

    @state.setter
    def state(self, value: State) -> None:
        changed = getattr(self, "_state", None) != value
        self._state = value
        if changed:
            self._emit_status()

    def status_snapshot(self) -> dict[str, object]:
        """Состояние для WS-событий status."""
        budget = 0.0
        try:
            if self.router is not None and hasattr(self.router, "budget"):
                budget = float(self.router.budget.month_spent_rub())
        except Exception:  # noqa: BLE001 - статус не должен падать
            budget = 0.0
        try:
            from kivi.prefs import get_tts_volume

            volume = float(get_tts_volume())
        except Exception:  # noqa: BLE001
            volume = 0.8
        return {
            "state": self._state,
            "budget_used": budget,
            "volume": volume,
            "window_open": bool(
                self.window_until and time.monotonic() <= self.window_until
            ),
        }

    def _emit_status(self) -> None:
        api = getattr(self, "api", None)
        if api is None:
            return
        try:
            api.broadcast_status(**self.status_snapshot())
        except Exception as exc:  # noqa: BLE001 - API не должен ронять цикл
            wake_log.debug("api: status не отправлен: {}", exc)

    def run(
        self,
        *,
        stop_event: threading.Event | None = None,
        max_seconds: float | None = None,
    ) -> None:
        """Блокирующий цикл прослушки."""
        samplerate = self.capture.samplerate
        warmup_samples = int(WARMUP_SECONDS * samplerate)
        samples_read = 0
        clock_set = False
        started_at = time.monotonic()

        self.capture.start()
        self.vad.reset()
        wake_log.info(
            "прослушка начата ({} Гц, блок {} сэмплов)",
            samplerate,
            self.capture.block_frames,
        )

        try:
            while not self._stop.is_set():
                if stop_event is not None and stop_event.is_set():
                    break
                if max_seconds is not None and time.monotonic() - started_at >= max_seconds:
                    break

                block = self.capture.read_block(timeout=0.5)
                if block is None:
                    if not self.capture.is_running:
                        break
                    continue

                samples_read += block.size
                if samples_read <= warmup_samples:
                    # Первые блоки после открытия WASAPI — возможен щелчок драйвера.
                    continue
                if not clock_set:
                    self.vad.clock_offset = (samples_read - block.size) / samplerate
                    clock_set = True

                block_end_t = samples_read / samplerate
                started_here = False
                finished = None
                for event in self.vad.feed_block(block):
                    if event["type"] == "speech_start":
                        pre_ms = (block_end_t - event["t"]) * 1000.0 + self.pre_roll_ms
                        self.recorder.start(event["t"], self.capture.snapshot(pre_ms))
                        started_here = True
                    else:
                        finished = self.recorder.stop(event["t"])

                if self.recorder.is_recording and not started_here:
                    self.recorder.push(block)

                if finished is not None:
                    self._handle_utterance(finished)

                self._expire_window()

                # Простой: если давно молчим и есть необработанные реплики — рефлексия.
                idle_seconds = time.time() - self._last_activity_ts
                if (
                    self._turns_since_reflect
                    and idle_seconds >= self._idle_threshold_s
                ):
                    self._maybe_reflect(idle_seconds=idle_seconds)
        except KeyboardInterrupt:
            wake_log.info("остановка по Ctrl+C")
        finally:
            self.capture.stop()
            if self.recorder.is_recording:
                self.recorder.cancel()
            wake_log.info("прослушка остановлена")

    # --- обработка ---

    def _expire_window(self) -> None:
        if self.window_until and time.monotonic() > self.window_until:
            self.window_until = 0.0
            wake_log.info("диалог: окно закрыто по тишине")
            self._emit_status()

    def _open_window(self) -> None:
        """Открыть окно диалога: тишина пользователя после ответа Киви."""
        if not self.conversation_enabled:
            return
        self.window_until = time.monotonic() + self.silence_s
        wake_log.info("диалог: окно открыто ({:.0f}с тишины)", self.silence_s)
        self._emit_status()

    def _close_window(self, reason: str = "") -> None:
        if not self.window_until:
            return
        self.window_until = 0.0
        wake_log.info("диалог: окно закрыто{}", f" {reason}" if reason else "")
        self._emit_status()

    def _is_greeting(self, text: str) -> bool:
        return _contains_phrase(text, self.greeting_phrases, prefix=True)

    def _is_end_phrase(self, text: str) -> bool:
        return _contains_phrase(text, self.end_phrases)

    def _drain(self, max_blocks: int = 32) -> None:
        """Выбросить накопленное аудио, чтобы не разбирать старый звук."""
        for _ in range(max_blocks):
            if self.capture.read_block(timeout=0.01) is None:
                return

    def _handle_utterance(self, utterance: Utterance) -> None:
        # Защита от самослушания: пока Киви говорит, её же фразы не обрабатываем.
        if self._speaking:
            wake_log.info("пропуск: говорю сама")
            return

        duration = float(utterance.speech_s)
        if duration < MIN_UTTERANCE_S or duration > MAX_UTTERANCE_S:
            wake_log.info(
                "пропуск: длительность {:.2f} c вне [{:.1f}, {:.1f}]",
                duration,
                MIN_UTTERANCE_S,
                MAX_UTTERANCE_S,
            )
            return

        try:
            heard = transcribe_wav(utterance.path).strip()
        except Exception as exc:  # noqa: BLE001 - локальный Whisper может упасть
            wake_log.error("Whisper упал: {}: {}", type(exc).__name__, exc)
            return
        if not heard:
            # Фильтры энергии и галлюцинаций живут в STT (MIN_RMS/is_hallucination)
            # и применяются к каждой фразе — в окне диалога тоже.
            wake_log.info("пропуск: пустое распознавание ({:.2f} c)", duration)
            return

        match = detect(heard)
        now = time.monotonic()
        in_window = (
            self.conversation_enabled
            and bool(self.window_until)
            and now <= self.window_until
        )
        wake_log.info("диалог: режим {}", "диалог" if in_window else "ожидание имени")

        if match.is_wake:
            if now - self.last_wake_ts < self.cooldown_s:
                result = WakeResult(
                    heard, True, match.command, "cooldown", match.fuzzy, duration
                )
                wake_log.info("{}", result)
                self._emit(result)
                return
            self.last_wake_ts = now
            command = match.command
            action: Action = "think" if command else "listen"
            if not command and self._is_greeting(heard):
                # «Киви, привет»: детектор считает приветствие филлером —
                # всё равно отвечаем естественно через chat.
                command = _find_phrase(heard, self.greeting_phrases, prefix=True) or "привет"
                action = "think"
        elif in_window:
            # Окно открыто — фраза без имени считается продолжением диалога.
            action = "followup"
            command = heard
        else:
            action = "idle"
            command = ""

        # Фразы завершения: закрыть окно молча, без ответа.
        if action in ("think", "followup") and self._is_end_phrase(command):
            self._close_window("по фразе")
            result = WakeResult(heard, match.is_wake, command, "idle", match.fuzzy, duration)
            wake_log.info("{}", result)
            self._emit(result)
            return

        result = WakeResult(heard, match.is_wake, command, action, match.fuzzy, duration)
        wake_log.info("{}", result)
        self._emit(result)

        if action == "listen":
            self._say(ACK_PHRASE)
            self._open_window()
            self._drain()
            self.vad.reset()
        elif action in ("think", "followup"):
            # Старое окно больше не действует: новое откроется после ответа.
            self.window_until = 0.0
            if self._is_greeting(command):
                wake_log.info("диалог: приветствие -> chat")
                self._answer_chat(
                    command,
                    {"route": "chat", "source": "greeting"},
                    time.perf_counter(),
                )
            else:
                self._think_and_speak(command)

    def _emit(self, result: WakeResult) -> None:
        if self.on_result is not None:
            try:
                self.on_result(result)
            except Exception as exc:  # noqa: BLE001 - колбэк не должен ломать цикл
                wake_log.error("колбэк on_result упал: {}", exc)

    def _think_and_speak(self, command: str) -> None:
        """Ровно один ответ на команду: маршрут берём из planner.plan()."""
        started = time.perf_counter()
        session = self._get_session()

        plan = self._plan_skill(command, session.history())
        route = str((plan or {}).get("route") or "chat")

        if plan is not None and plan.get("skill") is not None:
            self._run_skill(command, plan["skill"], plan.get("args") or {})
            return
        if route == "search":
            self._answer_search(command, plan, started)
            return
        if route == "action_gap":
            self._answer_action_gap(command, plan, started)
            return
        self._answer_chat(command, plan, started)

    def _answer_chat(self, command: str, plan: dict | None, started: float) -> None:
        """Разговорный путь: один стрим Router -> SentenceSpeaker."""
        session = self._get_session()
        memory = self._memory_block(command)
        system_text = PERSONA_PROMPT + (f"\n\n{memory}" if memory else "")
        messages = (
            [{"role": "system", "content": system_text}]
            + session.history()
            + [{"role": "user", "content": command}]
        )
        wake_log.info(
            "THINKING -> chat: {!r} (в контексте {} реплик, память {} символов)",
            command,
            len(messages) - 2,
            len(memory),
        )
        self.state = "THINKING"

        speaker = SentenceSpeaker(started_at=started, context="think")
        self._speaker = speaker
        self._speaking = True
        parts: list[str] = []
        try:
            if self.think_fn is not None:
                text = str(self.think_fn(command) or "")
                if text:
                    parts.append(text)
                    speaker.feed(text)
            elif self.router is None:
                wake_log.warning("роутер не задан — команду не обрабатываю: {!r}", command)
            elif hasattr(self.router, "complete_stream"):
                for chunk in self.router.complete_stream(messages, task_type="chat"):
                    parts.append(chunk)
                    speaker.feed(chunk)
            else:
                # Заглушки (например, DryRouter в смоуке) умеют только complete.
                result = self.router.complete(messages, task_type="chat")
                text = str((result or {}).get("text") or "")
                if text:
                    parts.append(text)
                    speaker.feed(text)
        except Exception as exc:  # noqa: BLE001 - сеть/бюджет
            wake_log.error("LLM не ответил: {}: {}", type(exc).__name__, exc)
        finally:
            self.state = "SPEAKING"
            try:
                speaker.finish()
            except Exception as exc:  # noqa: BLE001
                wake_log.error("озвучка стрима упала: {}", exc)
            finally:
                self._speaking = False
                self._speaker = None

        full_text = "".join(parts).strip()
        if full_text:
            # В историю пишем очищенный ответ целиком (без лимита в 40 слов).
            session.add("user", command)
            session.add("assistant", to_speech(full_text, max_words=10000))
            self._open_window()

        self._note_turn()
        self._drain()
        self.vad.reset()
        self.state = "IDLE"
        wake_log.info("вернулся в IDLE")

    def _answer_search(self, command: str, plan: dict | None, started: float) -> None:
        """Поиск по переформулированному запросу; ровно одна озвучка."""
        from kivi.skills.search import NO_DATA_ANSWER, answer_search

        self.state = "THINKING"
        wake_log.info("THINKING -> search: {!r}", command)
        session = self._get_session()
        settings = self.search_config or {}
        try:
            max_results = int(settings.get("max_results") or 3)
            timeout_s = float(settings.get("timeout_s") or 15.0)
        except (TypeError, ValueError):
            max_results, timeout_s = 3, 15.0

        speaker = SentenceSpeaker(started_at=started, context="search")
        self._speaker = speaker
        text = ""
        try:
            # Контекстный поиск: LLM переформулирует команду (ослышки/валюта),
            # ищем только по запросу; при NOT_FOUND — вторая попытка внутри.
            text = answer_search(
                command,
                session.history(),
                router=self.router,
                max_results=max_results,
                timeout=timeout_s,
            )
            text = " ".join(str(text or "").split())
        except Exception as exc:  # noqa: BLE001 - поиск не должен ронять цикл
            wake_log.error("поиск упал: {}: {}", type(exc).__name__, exc)
            text = ""

        if not text or text == NO_DATA_ANSWER:
            self._record_gap(command, plan, why="поиск не дал ответа")
            text = NO_DATA_ANSWER

        try:
            self.state = "SPEAKING"
            self._speaking = True
            speaker.feed(text)
            speaker.finish()
        except Exception as exc:  # noqa: BLE001
            wake_log.error("озвучка поиска упала: {}", exc)
        finally:
            self._speaking = False
            self._speaker = None

        wake_log.info("поиск: ответ {!r}", text[:80])
        self._complete_turn(command, text)

    def _answer_action_gap(self, command: str, plan: dict | None, started: float) -> None:
        """Действие без умения: попытка закрыть пробел или честный короткий ответ."""
        self.state = "THINKING"
        wake_log.info("THINKING -> action_gap: {!r}", command)

        speaker = SentenceSpeaker(started_at=started, context="think")
        self._speaker = speaker
        text = ""
        gap_id = self._frequent_gap_id(plan)
        if gap_id is not None:
            success, how, message = self._gap_message(gap_id)
            wake_log.info("пробел {}: {} -> {}", gap_id, how, success)
            text = " ".join(str(message).split())
        if not text:
            text = "Пока не могу это сделать, но запомнила, что тебе это нужно."

        try:
            self.state = "SPEAKING"
            self._speaking = True
            speaker.feed(text)
            speaker.finish()
        except Exception as exc:  # noqa: BLE001
            wake_log.error("озвучка пробела упала: {}", exc)
        finally:
            self._speaking = False
            self._speaker = None

        self._complete_turn(command, text)

    def _complete_turn(self, command: str, text: str) -> None:
        """Записать реплики в сессию, открыть окно диалога и вернуть IDLE."""
        if text:
            session = self._get_session()
            session.add("user", command)
            session.add("assistant", text)
            self._open_window()
        self._note_turn()
        self._drain()
        self.vad.reset()
        self.state = "IDLE"
        wake_log.info("вернулся в IDLE")

    def _record_gap(self, command: str, plan: dict | None, *, why: str) -> int | None:
        """Записать пробел (для search-пути; action_gap пишет планировщик)."""
        if self.gaps is None:
            return None
        missing = (plan or {}).get("missing") or []
        capability = str(missing[0].get("capability") or "").strip() if missing else ""
        try:
            return self.gaps.record_gap(
                command=command,
                user_intent=capability or command,
                why=why,
            )
        except Exception as exc:  # noqa: BLE001 - журнал не должен ронять ответ
            wake_log.warning("пробелы: не удалось записать пробел: {}", exc)
            return None

    # --- скиллы ---

    def _plan_skill(
        self, command: str, history: list[dict] | None = None
    ) -> dict | None:
        """План умения под команду. None — планировщик недоступен или упал."""
        if self.planner is None:
            return None
        try:
            plan = self.planner.plan(command, history=history)
        except Exception as exc:  # noqa: BLE001 - планировщик не должен ронять цикл
            wake_log.warning("планировщик упал: {}: {}", type(exc).__name__, exc)
            return None
        if plan.get("skill") is not None:
            wake_log.info(
                "планировщик: умение {} (источник {})",
                plan["skill"].name,
                plan.get("source"),
            )
        else:
            wake_log.info(
                "планировщик: маршрут {} (источник {}), пробел: {}",
                plan.get("route") or "chat",
                plan.get("source"),
                "записан" if plan.get("gap_id") else "не нужен",
            )
        return plan

    def _frequent_gap_id(self, plan: dict | None) -> int | None:
        """id пробела, если эту просьбу повторяли достаточно часто (gap_closer)."""
        if not plan or plan.get("route") != "action_gap":
            return None
        gap_id = plan.get("gap_id")
        if not gap_id or self.gaps is None:
            return None
        try:
            threshold = int(self.gap_config.get("min_frequency") or 3)
        except (TypeError, ValueError):
            threshold = 3
        try:
            frequent = self.gaps.get_frequent_gaps(min_count=threshold)
        except Exception as exc:  # noqa: BLE001 - журнал не должен ронять цикл
            wake_log.warning("пробелы: не удалось прочитать частые пробелы: {}", exc)
            return None
        ids = {int(row["id"]) for row in frequent}
        return int(gap_id) if int(gap_id) in ids else None

    def _gap_message(self, gap_id: int) -> tuple[bool, str, str]:
        """Закрыть пробел: (успех, стратегия, текст для озвучки). Ровно один ответ."""
        from kivi.skills.gap_closer import GapCloser

        started = time.perf_counter()
        wake_log.info("THINKING -> закрытие пробела {}: {!r}", gap_id, "action_gap")
        try:
            closer = GapCloser(
                router=self.router,
                registry=self.skills,
                gaps=self.gaps,
                config=self.gap_config,
                search_config=self.search_config,
            )
            success, how, message = closer.attempt_close(gap_id)
            if success and how == "search":
                wake_log.info("пробел {}: ответ найден в интернете", gap_id)
        except Exception as exc:  # noqa: BLE001 - пробел не должен ронять цикл
            wake_log.error(
                "пробел {}: закрытие упало: {}: {}", gap_id, type(exc).__name__, exc
            )
            success, how, message = False, "none", ""
        wake_log.info(
            "пробел {}: {:.0f} мс",
            gap_id,
            (time.perf_counter() - started) * 1000.0,
        )
        return success, how, " ".join(str(message).split())

    def _run_skill(self, command: str, manifest: SkillManifest, args: dict) -> None:
        """Выполнить скилл: озвучить ответ и записать реплики. Router не зовём."""
        from kivi.skills.loader import invoke

        started = time.perf_counter()
        self.state = "THINKING"
        wake_log.info("THINKING -> скилл {}: {!r}", manifest.name, command)
        try:
            answer = invoke(manifest, args, registry=self.skills)
        except Exception as exc:  # noqa: BLE001 - скилл не должен ронять цикл
            wake_log.error(
                "скилл {}: invoke упал: {}: {}", manifest.name, type(exc).__name__, exc
            )
            answer = f"Скилл «{manifest.name}» упал."

        text = " ".join(str(answer).split())
        if text:
            self._say(text)
            session = self._get_session()
            session.add("user", command)
            session.add("assistant", text)
            self._open_window()
        else:
            wake_log.warning("скилл {} вернул пустой ответ", manifest.name)

        self._note_turn()
        self._drain()
        self.vad.reset()
        self.state = "IDLE"
        wake_log.info(
            "скилл {}: {:.0f} мс", manifest.name, (time.perf_counter() - started) * 1000.0
        )

    def _get_session(self) -> Session:
        """История диалога: создаётся лениво, чтобы smoke мог подставить свою БД."""
        if self.session is None:
            self.session = Session()
        return self.session

    # --- долгая память ---

    def _memory_block(self, query: str) -> str:
        """Блок памяти для системного промпта (пусто, если vault пуст)."""
        if not self.memory_enabled:
            return ""
        try:
            from kivi.memory.inject import build_memory_block

            block = build_memory_block(query)
        except Exception as exc:  # noqa: BLE001 - память не должна ломать диалог
            wake_log.warning("память: блок не собран: {}: {}", type(exc).__name__, exc)
            return ""
        if block:
            wake_log.debug("память: блок {} символов", len(block))
        return block

    def _note_turn(self) -> None:
        """Учесть реплику и, если пора, запустить рефлексию в фоне."""
        with self._memory_lock:
            self._turns_since_reflect += 1
        self._last_activity_ts = time.time()
        self._maybe_reflect()

    def _maybe_reflect(self, *, idle_seconds: float = 0.0) -> None:
        if self.reflector is None:
            return
        with self._memory_lock:
            turns = self._turns_since_reflect
        if not self.reflector.should_run(turns_since=turns, idle_seconds=idle_seconds):
            return
        if self._reflect_thread is not None and self._reflect_thread.is_alive():
            wake_log.debug("память: рефлексия уже идёт")
            return
        messages = self._get_session().history(max_tokens=3000)
        self._reflect_thread = threading.Thread(
            target=self._reflect_job,
            args=(messages, turns),
            name="kivi-reflect",
            daemon=True,
        )
        self._reflect_thread.start()

    def _reflect_job(self, messages: list[dict], turns: int) -> None:
        try:
            stats = self.reflector.run(messages)
        except Exception as exc:  # noqa: BLE001 - рефлексия не должна ронять поток
            wake_log.warning("память: рефлексия упала: {}", exc)
            return
        if stats.get("skipped"):
            wake_log.info("память: рефлексия пропущена ({})", stats["skipped"])
            return
        with self._memory_lock:
            self._turns_since_reflect = max(0, self._turns_since_reflect - turns)
        wake_log.info(
            "память: фактов {}, решений {}, стоимость {:.5f} руб",
            stats["facts"],
            stats["decisions"],
            stats["cost_rub"],
        )

    def wait_reflection(self, timeout: float = 8.0) -> bool:
        """Дождаться финальной рефлексии перед остановкой (не дольше timeout).

        True — завершилась (или нечего делать), False — прервана по таймауту.
        """
        if self.reflector is None:
            return True

        thread = self._reflect_thread
        if thread is None or not thread.is_alive():
            with self._memory_lock:
                turns = self._turns_since_reflect
            if turns <= 0:
                wake_log.info("reflection завершён")
                return True
            messages = self._get_session().history(max_tokens=3000)
            thread = threading.Thread(
                target=self._reflect_job,
                args=(messages, turns),
                name="kivi-reflect-stop",
                daemon=True,
            )
            with self._memory_lock:
                self._reflect_thread = thread
            thread.start()

        thread.join(max(0.1, float(timeout)))
        if thread.is_alive():
            wake_log.warning("reflection прерван по таймауту")
            return False
        wake_log.info("reflection завершён")
        return True

    def _say(self, text: str) -> None:
        phrase = " ".join(str(text).split())
        if not phrase:
            return
        self.state = "SPEAKING"
        self._speaking = True
        try:
            self.speak_fn(phrase)
        except Exception as exc:  # noqa: BLE001 - TTS может упасть
            wake_log.error("озвучка упала: {}: {}", type(exc).__name__, exc)
        finally:
            self._speaking = False
            self.state = "IDLE"
