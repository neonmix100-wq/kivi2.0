"""Смоук шага latency: fast-path «привет/спасибо», бюджет памяти, первый чанк.

Запуск:
    python tests/smoke_latency.py

Что проверяется:
  1) fast_route: «привет» -> greeting, «какой курс биткоина» -> None;
  2) handle_command("привет") — planner и TTS-синтез не вызываются,
     реплика играется из кэша, first_sound_ms < 300;
  3) handle_command("спасибо") — thanks, кэш, без LLM;
  4) память дольше 0.8 с -> блок пуст, лог «пропуск по бюджету», turn завершён;
  5) первый чанк длиннее 60 символов режется до <= 60;
  6) вводная не играется при fast_kind и при готовом первом чанке, порог 2.5 с;
  7) turn-лог содержит planner_ms / memory_ms / first_sound_ms / tts_first_ms.

Сеть, микрофон, TTS и LLM не нужны: плеер, синтез и память подменены.
"""

from __future__ import annotations

import json
import queue
import sys
import threading
import time
from pathlib import Path
from typing import Any

PROJECT_ROOT = Path(__file__).resolve().parents[1]
if str(PROJECT_ROOT) not in sys.path:
    sys.path.insert(0, str(PROJECT_ROOT))

from loguru import logger

import kivi.memory.inject as inject_module
import kivi.tts.fillers as fillers_module
import kivi.tts.speak as speak_module
import kivi.tts.stream as stream_module
from kivi.brain import fastpath
from kivi.brain.session import Session
from kivi.config import get_settings
from kivi.log import setup_logging
from kivi.tts import replies
from kivi.wake.loop import WakeLoop

TMP = PROJECT_ROOT / "data" / "tmp"
SESSION_DB = TMP / "latency_session.db"
REPLIES_DIR = TMP / "latency_replies"


def check(label: str, condition: bool, detail: str = "") -> bool:
    mark = "OK  " if condition else "X   "
    print(f"{mark} {label}" + (f" — {detail}" if detail else ""))
    return bool(condition)


class LogCatcher:
    """Перехват INFO/WARNING loguru на время блока."""

    def __init__(self) -> None:
        self.messages: list[str] = []
        self._sink = None

    def _write(self, message: Any) -> None:
        self.messages.append(str(message).strip())

    def __enter__(self) -> "LogCatcher":
        self._sink = logger.add(self._write, level="INFO")
        return self

    def __exit__(self, *exc_info: Any) -> bool:
        if self._sink is not None:
            logger.remove(self._sink)
            self._sink = None
        return False

    def has(self, needle: str) -> bool:
        return any(needle in line for line in self.messages)


class FakeCapture:
    """Заглушка AudioCapture: аудио не читаем."""

    is_running = False

    def read_block(self, timeout: float | None = None):
        return None

    def snapshot(self, milliseconds: float):
        return None

    def start(self) -> None:
        self.is_running = True

    def stop(self) -> None:
        self.is_running = False


class FakeVad:
    clock_offset = 0.0

    def reset(self) -> None:
        pass

    def feed_block(self, block: Any):
        return []


class FakeRecorder:
    is_recording = False

    def cancel(self) -> None:
        pass


class SpyPlanner:
    """Планировщик-шпион: считает вызовы и всегда отдаёт chat."""

    def __init__(self) -> None:
        self.calls: list[str] = []
        self.intent_calls = 0

    def plan(self, command: str, history: list[dict] | None = None) -> dict:
        self.calls.append(str(command))
        self.intent_calls += 1
        return {"route": "chat", "source": "spy"}


class FakeRouter:
    """Заглушка LLM: один ответ, без сети."""

    class _Budget:
        def month_spent_rub(self) -> float:
            return 0.0

        def describe(self) -> str:
            return "0.00 руб"

    def __init__(self, text: str = "Ответ заглушки для теста.") -> None:
        self.text = text
        self.budget = self._Budget()
        self.calls = 0

    def complete(self, messages: list[dict], task_type: str = "chat") -> dict:
        self.calls += 1
        return {"text": self.text}



def prepare_replies() -> Path:
    """Индекс реплик в data/tmp + mp3-заглушки: сеть не нужна."""

    REPLIES_DIR.mkdir(parents=True, exist_ok=True)
    index: dict[str, list[dict[str, str]]] = {}
    for kind, text in (
        ("greeting", "Привет, Лёша! Чем займёмся?"),
        ("thanks", "Пожалуйста!"),
        ("smalltalk", "Всё отлично, готова помогать."),
    ):
        mp3 = REPLIES_DIR / f"{kind}.mp3"
        mp3.write_bytes(b"ID3" + b"\x00" * 64)
        index[kind] = [{"text": text, "mp3": str(mp3)}]
    target = REPLIES_DIR / "index.json"
    target.write_text(json.dumps(index, ensure_ascii=False, indent=2), encoding="utf-8")
    return target


def make_speaker() -> stream_module.SentenceSpeaker:
    """SentenceSpeaker без устройства вывода: только логика чанков и вводной."""
    speaker = object.__new__(stream_module.SentenceSpeaker)
    speaker._buffer = ""
    speaker._first = True
    speaker._lock = threading.Lock()
    speaker._text_q = queue.Queue()
    speaker._audio_q = queue.Queue()
    speaker._stopping = threading.Event()
    speaker._first_ready = threading.Event()
    speaker.fast_kind = None
    speaker.context = "think"
    speaker.ack_delay_s = stream_module.ACK_DELAY_S
    return speaker


def main() -> int:
    setup_logging()
    print("=== SMOKE latency: fast-path «привет/спасибо», память, первый чанк ===")
    TMP.mkdir(parents=True, exist_ok=True)
    ok = True

    try:
        settings = get_settings()
    except Exception as exc:  # noqa: BLE001 - без конфига смоук бессмыслен
        print(f"FAIL — конфиг/устройства недоступны: {exc}")
        return 1

    index_path = prepare_replies()
    replies.INDEX = index_path
    replies.reset_state()
    ok &= check("библиотека реплик подготовлена", index_path.is_file(), str(index_path))

    played: list[str] = []
    synth_calls: list[str] = []
    pick_calls: list[str] = []

    original = {
        "play_file": speak_module.play_file,
        "synthesize_mp3": speak_module.synthesize_mp3,
        "build_memory_block": inject_module.build_memory_block,
        "fillers_pick": fillers_module.pick,
    }

    def fake_play_file(path: Any, **kwargs: Any) -> float:
        played.append(str(path))
        return 1.0

    def fake_synthesize(*args: Any, **kwargs: Any) -> Any:
        synth_calls.append(str(args[0]) if args else "")
        raise AssertionError("fast-path не должен синтезировать TTS в рантайме")

    speak_module.play_file = fake_play_file  # type: ignore[assignment]
    speak_module.synthesize_mp3 = fake_synthesize  # type: ignore[assignment]

    try:
        from kivi.audio.output import resolve_output_device

        try:
            resolve_output_device()  # прогреваем выбор устройства (кэш 2 с)
        except Exception as exc:  # noqa: BLE001 - смоук живёт и без звука
            print(f"    (устройство вывода не подтверждено: {exc})")

        spy = SpyPlanner()
        router = FakeRouter()
        loop = WakeLoop(
            settings=settings,
            router=router,
            capture=FakeCapture(),
            vad=FakeVad(),
            recorder=FakeRecorder(),
            speak_fn=lambda text: None,
            session=Session(db_path=SESSION_DB),
            silence_s=8,
        )
        loop.planner = spy  # type: ignore[assignment]

        print("\n--- 1) fast_route ---")
        greeting = fastpath.fast_route("привет")
        ok &= check(
            "«привет» -> greeting",
            greeting is not None and greeting.get("fast") == "greeting",
            str(greeting),
        )
        ok &= check(
            "«Спасибо!» -> thanks",
            (fastpath.fast_route("Спасибо!") or {}).get("fast") == "thanks",
        )
        ok &= check(
            "«какой курс биткоина» -> обычный путь",
            fastpath.fast_route("какой курс биткоина") is None,
        )

        print("\n--- 2) handle_command(«привет»): без LLM, из кэша ---")
        with LogCatcher() as logs:
            answer, route = loop.handle_command("привет", source="text", speak=True)
        timing = dict(loop._last_fast_timing)
        print(f"    ответ={answer!r}, route={route}, timing={timing}")
        ok &= check("ответ получен", bool(answer) and route == "chat")
        ok &= check(
            "planner (LLM) не вызывался", spy.intent_calls == 0, f"вызовов: {spy.intent_calls}"
        )
        ok &= check("TTS-синтез не вызывался", synth_calls == [], f"вызовов: {len(synth_calls)}")
        ok &= check("реплика играется из кэша", len(played) == 1, str(played[-1:]))
        ok &= check(
            "first_sound_ms < 300",
            0 <= timing.get("first_sound_ms", 999) < 300,
            f"{timing.get('first_sound_ms')} мс",
        )

        print("\n--- 3) handle_command(«спасибо») ---")
        with LogCatcher():
            answer_thanks, _ = loop.handle_command("спасибо", source="text", speak=True)
        print(f"    ответ={answer_thanks!r}, timing={loop._last_fast_timing}")
        ok &= check(
            "ответ из библиотеки thanks",
            answer_thanks == "Пожалуйста!",
            repr(answer_thanks),
        )
        ok &= check("LLM не вызывался и здесь", spy.intent_calls == 0 and router.calls == 0)
        ok &= check("синтеза по-прежнему нет", synth_calls == [])

        print("\n--- 4) память дольше бюджета 0.8 с ---")

        def slow_memory(query: str | None = None, **kwargs: Any) -> str:
            time.sleep(2.0)
            return "Память о Лёше:\n- тестовый факт"

        inject_module.build_memory_block = slow_memory  # type: ignore[assignment]
        loop.memory_enabled = True
        memory_started = time.perf_counter()
        with LogCatcher() as logs:
            answer_mem, route_mem = loop.handle_command(
                "расскажи про мою видеокарту", source="text", speak=False
            )
        memory_elapsed = time.perf_counter() - memory_started
        print(
            f"    ответ={answer_mem!r}, memory_ms={loop._memory_ms}, "
            f"всего {memory_elapsed * 1000:.0f} мс"
        )
        ok &= check("лог «пропуск по бюджету»", logs.has("пропуск по бюджету"))
        ok &= check(
            "блок памяти не ждали (memory_ms ≈ 800)",
            700 <= loop._memory_ms < 1900,
            f"{loop._memory_ms} мс",
        )
        ok &= check(
            "turn завершён без ожидания 2 с",
            bool(answer_mem) and route_mem == "chat" and memory_elapsed < 1.9,
            f"{memory_elapsed:.2f} с",
        )
        ok &= check(
            "turn-лог: planner_ms/memory_ms/first_sound_ms/tts_first_ms",
            any(
                "turn: source=" in line
                and "planner_ms=" in line
                and "memory_ms=" in line
                and "first_sound_ms=" in line
                and "tts_first_ms=" in line
                for line in logs.messages
            ),
        )
        inject_module.build_memory_block = original["build_memory_block"]  # type: ignore[assignment]

        print("\n--- 5) первый чанк не длиннее 60 символов ---")
        long_text = (
            "Это длинное предложение без единой точки которое точно длиннее "
            "шестидесяти символов и проверяет резку по словам"
        )
        speaker = make_speaker()
        speaker.feed(long_text)
        first_chunk = speaker._text_q.get_nowait()
        print(f"    первый чанк ({len(first_chunk)}): {first_chunk!r}")
        ok &= check(
            "чанк не пустой и <= 60", 0 < len(first_chunk) <= 60, f"{len(first_chunk)} симв."
        )

        short_speaker = make_speaker()
        short_speaker.feed("Привет, Лёша! Чем займёмся сегодня вечером? И ещё немного текста.")
        sentence_chunk = short_speaker._text_q.get_nowait()
        print(f"    предложение ({len(sentence_chunk)}): {sentence_chunk!r}")
        ok &= check("первое предложение тоже <= 60", len(sentence_chunk) <= 60)

        print("\n--- 6) вводная: порог 2.5 с и пропуски ---")
        ok &= check(
            "ACK_DELAY_S = 2.5",
            float(stream_module.ACK_DELAY_S) == 2.5,
            str(stream_module.ACK_DELAY_S),
        )
        ok &= check(
            "новый SentenceSpeaker берёт порог 2.5",
            make_speaker().ack_delay_s == 2.5,
        )

        fillers_module.pick = lambda context: pick_calls.append(context) or None  # type: ignore[assignment]
        fast_speaker = make_speaker()
        fast_speaker.fast_kind = "greeting"
        fast_speaker._maybe_ack()
        ok &= check("fast_kind: вводная не играется", pick_calls == [], str(pick_calls))

        ready_speaker = make_speaker()
        ready_speaker._text_q.put("Первый чанк уже готов и ждёт синтеза.")
        ready_speaker._maybe_ack()
        ok &= check(
            "первый чанк в очереди: вводная не играется", pick_calls == [], str(pick_calls)
        )

        plain_speaker = make_speaker()
        plain_speaker._maybe_ack()
        ok &= check(
            "без чанка и без fast_kind: вводная выбирается",
            pick_calls == ["think"],
            str(pick_calls),
        )
        fillers_module.pick = original["fillers_pick"]  # type: ignore[assignment]
    finally:
        speak_module.play_file = original["play_file"]  # type: ignore[assignment]
        speak_module.synthesize_mp3 = original["synthesize_mp3"]  # type: ignore[assignment]
        inject_module.build_memory_block = original["build_memory_block"]  # type: ignore[assignment]
        fillers_module.pick = original["fillers_pick"]  # type: ignore[assignment]

    print()
    print("PASS" if ok else "FAIL — скопируй весь вывод и пришли в чат")
    return 0 if ok else 1


if __name__ == "__main__":
    raise SystemExit(main())


