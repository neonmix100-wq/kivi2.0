"""Постоянная прослушка и отзыв на имя «Киви».

Конечный автомат:
    IDLE -> (VAD speech_end) -> локальный Whisper -> is_wake?
      имя + команда   -> THINKING (one-shot)
      только имя      -> сказать «Слушаю», открыть окно 8 с на команду без имени
      нет имени       -> IDLE, молча
    THINKING -> planner.plan(command)   # LLM-планировщик + difflib-фолбэк
      умение найдено  -> loader.invoke -> SPEAKING (Router не зовём)
      умения нет      -> «Пока не умею: X» + Router.chat о пробеле -> SPEAKING

Никаких облачных вызовов для проверки имени: распознаёт локальный faster-whisper.
Каждое распознанное высказывание пишется в data/logs/wake.log.
"""

from __future__ import annotations

import threading
import time
from collections.abc import Callable
from dataclasses import dataclass
from typing import TYPE_CHECKING, Literal

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
    from kivi.skills.planner import Planner
    from kivi.skills.registry import SkillManifest, SkillRegistry

__all__ = [
    "COOLDOWN_S",
    "FOLLOWUP_WINDOW_S",
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
FOLLOWUP_WINDOW_S = 8.0
ACK_PHRASE = "Слушаю"

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
        followup_window_s: float = FOLLOWUP_WINDOW_S,
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
        self.followup_window_s = float(followup_window_s)
        self.cooldown_s = float(cooldown_s)

        self.pre_roll_ms = float(audio.pre_roll_ms)
        self.state: State = "IDLE"
        self.followup_deadline = 0.0
        self.last_wake_ts = 0.0
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
        try:
            from kivi.skills.planner import Planner as _Planner
            from kivi.skills.registry import SkillRegistry as _SkillRegistry

            registry = _SkillRegistry.from_config(self.settings.section("skills"))
            registry.scan()
            self.skills = registry
            # Тот же router: в dry-run смоуке планировщик ничего не потратит.
            self.planner = _Planner(registry, router=router)
        except Exception as exc:  # noqa: BLE001 - скиллы не должны блокировать запуск
            logger.warning("Скиллы: недоступны: {}: {}", type(exc).__name__, exc)

        logger.info(
            "WakeLoop готов: вход [{}] {}, окно {:.0f} c, cooldown {:.1f} c, память: {}",
            devices.input_index,
            devices.input_name,
            self.followup_window_s,
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
        self._reflect_on_stop()  # память: доделываем проход при корректной остановке

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

                self._expire_followup()

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

    def _expire_followup(self) -> None:
        if self.followup_deadline and time.monotonic() > self.followup_deadline:
            self.followup_deadline = 0.0
            wake_log.info("окно ожидания команды истекло — снова IDLE")

    def _drain(self, max_blocks: int = 32) -> None:
        """Выбросить накопленное аудио, чтобы не разбирать старый звук."""
        for _ in range(max_blocks):
            if self.capture.read_block(timeout=0.01) is None:
                return

    def _handle_utterance(self, utterance: Utterance) -> None:
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
            wake_log.info("пропуск: пустое распознавание ({:.2f} c)", duration)
            return

        match = detect(heard)
        now = time.monotonic()
        in_followup = bool(self.followup_deadline) and now <= self.followup_deadline

        if match.is_wake:
            if now - self.last_wake_ts < self.cooldown_s:
                result = WakeResult(
                    heard, True, match.command, "cooldown", match.fuzzy, duration
                )
                wake_log.info("{}", result)
                self._emit(result)
                return
            self.last_wake_ts = now
            if match.command:
                action: Action = "think"
                command = match.command
            else:
                action = "listen"
                command = ""
        elif in_followup:
            # Окно открыто — фраза без имени считается командой.
            action = "followup"
            command = heard
        else:
            action = "idle"
            command = ""

        result = WakeResult(heard, match.is_wake, command, action, match.fuzzy, duration)
        wake_log.info("{}", result)
        self._emit(result)

        if action == "listen":
            self.followup_deadline = time.monotonic() + self.followup_window_s
            self._say(ACK_PHRASE)
            self._drain()
            self.vad.reset()
            wake_log.info("окно ожидания команды: {:.0f} c", self.followup_window_s)
        elif action in ("think", "followup"):
            self.followup_deadline = 0.0
            self._think_and_speak(command)

    def _emit(self, result: WakeResult) -> None:
        if self.on_result is not None:
            try:
                self.on_result(result)
            except Exception as exc:  # noqa: BLE001 - колбэк не должен ломать цикл
                wake_log.error("колбэк on_result упал: {}", exc)

    def _think_and_speak(self, command: str) -> None:
        """Стрим от LLM: предложения синтезируются и играются по мере поступления."""
        started = time.perf_counter()
        session = self._get_session()

        # --- умения: LLM-планировщик (+difflib-фолбэк), Router не зовём ---
        plan = self._plan_skill(command, session.history())
        if plan is not None and plan.get("skill") is not None:
            self._run_skill(command, plan["skill"], plan.get("args") or {})
            return

        notice = self._missing_notice(plan)
        if notice:
            wake_log.info("THINKING: пробел в умениях — {!r}", notice)
            self._say(notice)

        memory = self._memory_block(command)
        hint = self._gap_hint(plan)
        system_text = PERSONA_PROMPT + (f"\n\n{memory}" if memory else "") + hint
        messages = (
            [{"role": "system", "content": system_text}]
            + session.history()
            + [{"role": "user", "content": command}]
        )
        wake_log.info(
            "THINKING: {!r} (в контексте {} реплик, память {} символов)",
            command,
            len(messages) - 2,
            len(memory),
        )
        self.state = "THINKING"

        speaker = SentenceSpeaker(started_at=started)
        self._speaker = speaker
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
                self._speaker = None

        full_text = "".join(parts).strip()
        if full_text:
            # В историю пишем очищенный ответ целиком (без лимита в 40 слов).
            session.add("user", command)
            session.add("assistant", to_speech(full_text, max_words=10000))

        self._note_turn()
        self._drain()
        self.vad.reset()
        self.state = "IDLE"
        wake_log.info("вернулся в IDLE")

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
                "планировщик: умения нет (источник {}), пробел записан",
                plan.get("source"),
            )
        return plan

    @staticmethod
    def _missing_notice(plan: dict | None) -> str:
        """Короткая фраза «Пока не умею: X» для озвучки (пусто, если умение есть)."""
        if not plan or plan.get("skill") is not None:
            return ""
        missing = plan.get("missing") or []
        if not missing:
            return ""
        capability = str(missing[0].get("capability") or "").strip()
        if not capability:
            return ""
        if len(capability) > 60:
            capability = capability[:57].rstrip() + "…"
        return f"Пока не умею: {capability}."

    @staticmethod
    def _gap_hint(plan: dict | None) -> str:
        """Подсказка модели: чего просит пользователь, а умения пока нет."""
        if not plan or plan.get("skill") is not None:
            return ""
        missing = plan.get("missing") or []
        if not missing:
            return ""
        capability = str(missing[0].get("capability") or "").strip()
        if not capability:
            return ""
        return (
            "\n\nУмения под эту просьбу пока нет: "
            f"{capability[:120]}. Скажи честно и коротко, что пока не умеешь."
        )

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
        stats = self.reflector.run(messages)
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

    def _reflect_on_stop(self) -> None:
        """Проход рефлексии при корректной остановке (не чаще 5 минут)."""
        if self.reflector is None:
            return
        with self._memory_lock:
            turns = self._turns_since_reflect
        if turns <= 0:
            return
        try:
            stats = self.reflector.run(self._get_session().history(max_tokens=3000))
        except Exception as exc:  # noqa: BLE001
            wake_log.warning("память: рефлексия при остановке упала: {}", exc)
            return
        if stats.get("skipped"):
            wake_log.info("память: при остановке пропуск ({})", stats["skipped"])
        else:
            wake_log.info(
                "память: при остановке фактов {}, стоимость {:.5f} руб",
                stats["facts"],
                stats["cost_rub"],
            )

    def _say(self, text: str) -> None:
        phrase = " ".join(str(text).split())
        if not phrase:
            return
        self.state = "SPEAKING"
        try:
            self.speak_fn(phrase)
        except Exception as exc:  # noqa: BLE001 - TTS может упасть
            wake_log.error("озвучка упала: {}: {}", type(exc).__name__, exc)
        finally:
            self.state = "IDLE"
