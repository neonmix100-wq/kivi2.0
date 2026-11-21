"""Смоук шага 1.11: окно диалога без имени + защита от самослушания.

Запуск:
    python tests/smoke_dialogue.py

Что проверяется:
  1) после ответа window_until > now; следующая фраза без имени -> команда;
  2) после истечения окна фраза без имени -> idle (нужно имя);
  3) «спасибо» в окне закрывает окно, ответа нет;
  4) «Киви» без команды -> «Слушаю» + окно;
  5) «Киви, привет» -> маршрут chat + окно;
  6) пока _speaking=True, фраза отбрасывается;
  7) логи режима и окна: «диалог» / «ожидание имени» / «окно открыто (25с)».

Микрофон и STT не нужны: capture/VAD — заглушки, transcribe_wav подменён.
"""

from __future__ import annotations

import sys
import time
from pathlib import Path
from types import SimpleNamespace
from typing import Any

PROJECT_ROOT = Path(__file__).resolve().parents[1]
if str(PROJECT_ROOT) not in sys.path:
    sys.path.insert(0, str(PROJECT_ROOT))

from loguru import logger

import kivi.wake.loop as loop_module
from kivi.brain.session import Session
from kivi.config import get_settings
from kivi.log import setup_logging
from kivi.wake.loop import WakeLoop

TMP = PROJECT_ROOT / "data" / "tmp"
SESSION_DB = TMP / "dialogue_session.db"


def check(label: str, condition: bool, detail: str = "") -> bool:
    mark = "OK  " if condition else "X   "
    print(f"{mark} {label}" + (f" — {detail}" if detail else ""))
    return bool(condition)


class LogCatcher:
    """Перехват INFO-сообщений loguru на время блока."""

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
    """Заглушка VAD."""

    clock_offset = 0.0

    def reset(self) -> None:
        pass

    def feed_block(self, block: Any):
        return []


class FakeRecorder:
    """Заглушка Recorder."""

    is_recording = False

    def cancel(self) -> None:
        pass


def main() -> int:
    setup_logging()
    print("=== SMOKE 1.11: окно диалога (без микрофона) ===")
    TMP.mkdir(parents=True, exist_ok=True)
    ok = True

    try:
        settings = get_settings()
    except Exception as exc:  # noqa: BLE001 - без устройств смоук бессмыслен
        print(f"FAIL — конфиг/устройства недоступны: {exc}")
        return 1

    spoken: list[str] = []
    loop = WakeLoop(
        settings=settings,
        router=None,
        capture=FakeCapture(),
        vad=FakeVad(),
        recorder=FakeRecorder(),
        speak_fn=spoken.append,
        session=Session(db_path=SESSION_DB),
        silence_s=8,
    )

    results: list[Any] = []
    loop.on_result = results.append

    script: list[str] = []
    think_calls: list[str] = []
    chat_calls: list[tuple[str, dict]] = []

    def fake_transcribe(path: Any) -> str:
        return script.pop(0) if script else ""

    def fake_think(command: str) -> None:
        think_calls.append(command)
        loop._complete_turn(command, f"ответ: {command}")

    def fake_chat(command: str, plan: dict | None, started: float) -> None:
        chat_calls.append((command, dict(plan or {})))
        loop._complete_turn(command, f"Привет, Лёша! Это ответ на «{command}».")

    original_transcribe = loop_module.transcribe_wav
    loop_module.transcribe_wav = fake_transcribe  # type: ignore[assignment]
    loop._think_and_speak = fake_think  # type: ignore[method-assign]
    loop._answer_chat = fake_chat  # type: ignore[method-assign]

    def feed(text: str, speech_s: float = 2.0) -> None:
        script.append(text)
        loop._handle_utterance(
            SimpleNamespace(path=Path("fake.wav"), speech_s=speech_s)
        )

    try:
        print("\n--- 1) ответ открывает окно, фраза без имени -> команда ---")
        with LogCatcher() as logs:
            feed("Киви, который час")
        print(f"    action={results[-1].action}, think={think_calls}")
        ok &= check("имя + команда -> think", results[-1].action == "think")
        ok &= check("команда дошла до ответа", think_calls == ["который час"])
        ok &= check(
            "window_until > now после ответа",
            loop.window_until > time.monotonic(),
            f"{loop.window_until - time.monotonic():.1f} c",
        )
        ok &= check(
            "лог «окно открыто (8с тишины)»",
            any("диалог: окно открыто (8с тишины)" in line for line in logs.messages),
        )

        with LogCatcher() as logs:
            feed("а сколько времени")
        print(f"    action={results[-1].action}, think={think_calls}")
        ok &= check(
            "фраза без имени в окне -> followup",
            results[-1].action == "followup"
            and think_calls[-1] == "а сколько времени",
        )
        ok &= check(
            "лог режима «диалог»",
            any("диалог: режим диалог" in line for line in logs.messages),
        )

        print("\n--- 2) окно истекло -> нужно имя ---")
        loop.window_until = time.monotonic() - 0.1
        before_think = len(think_calls)
        with LogCatcher() as logs:
            feed("а сколько времени")
        print(f"    action={results[-1].action}, command={results[-1].command!r}")
        ok &= check(
            "без имени вне окна -> idle",
            results[-1].action == "idle" and results[-1].command == "",
        )
        ok &= check("ответ не запускался", len(think_calls) == before_think)
        ok &= check(
            "лог режима «ожидание имени»",
            any("диалог: режим ожидание имени" in line for line in logs.messages),
        )

        print("\n--- 3) «конец диалога» закрывает окно без ответа ---")
        loop._open_window()
        before_think = len(think_calls)
        with LogCatcher() as logs:
            feed("конец диалога")
        print(f"    window_until={loop.window_until}, think={len(think_calls)}")
        ok &= check("окно закрыто", loop.window_until == 0.0)
        ok &= check("ответа нет", len(think_calls) == before_think)
        ok &= check(
            "лог «окно закрыто по фразе»",
            any("диалог: окно закрыто по фразе" in line for line in logs.messages),
        )

        print("\n--- 3b) «спасибо» НЕ закрывает окно ---")
        loop._open_window()
        before_think = len(think_calls)
        feed("спасибо")
        print(f"    window_until={loop.window_until}, think={think_calls[-1:]}")
        ok &= check(
            "«спасибо» стало командой",
            len(think_calls) == before_think + 1 and think_calls[-1] == "спасибо",
        )
        ok &= check("окно осталось открытым", loop.window_until > time.monotonic())

        print("\n--- 3c) тишина > 8 с закрывает окно ---")
        loop.window_until = time.monotonic() - 0.1
        with LogCatcher() as logs:
            loop._expire_window()
        print(f"    window_until={loop.window_until}")
        ok &= check("окно закрыто по тишине", loop.window_until == 0.0)
        ok &= check(
            "лог «окно закрыто по тишине»",
            any("диалог: окно закрыто по тишине" in line for line in logs.messages),
        )

        print("\n--- 4) «Киви» без команды -> «Слушаю» + окно ---")
        loop.last_wake_ts = 0.0
        spoken.clear()
        feed("Киви")
        print(f"    action={results[-1].action}, spoken={spoken}")
        ok &= check(
            "action=listen + «Слушаю»",
            results[-1].action == "listen" and spoken == ["Слушаю"],
        )
        ok &= check("окно открыто", loop.window_until > time.monotonic())

        print("\n--- 5) приветствие -> route chat + окно ---")
        loop.last_wake_ts = 0.0
        loop.window_until = 0.0
        feed("Киви, привет")
        print(f"    chat={chat_calls}")
        ok &= check(
            "приветствие ушло в chat",
            bool(chat_calls) and chat_calls[-1][0] == "привет",
        )
        ok &= check(
            "план route=chat",
            bool(chat_calls) and chat_calls[-1][1].get("route") == "chat",
        )
        ok &= check(
            "окно открыто после приветствия", loop.window_until > time.monotonic()
        )

        print("\n--- 6) пока говорю — фраза отброшена ---")
        loop.window_until = 0.0
        loop.last_wake_ts = 0.0
        before_results = len(results)
        before_think = len(think_calls)
        loop._speaking = True
        feed("Киви, который час")
        loop._speaking = False
        print(
            f"    results={len(results) - before_results}, "
            f"think={len(think_calls) - before_think}"
        )
        ok &= check("фраза не обработана", len(results) == before_results)
        ok &= check("ответ не запускался", len(think_calls) == before_think)

        print("\n--- 7) фразы завершения из конфига ---")
        ok &= check(
            "«конец диалога» в end_phrases", "конец диалога" in loop.end_phrases
        )
        ok &= check("«спасибо» больше не закрывает", "спасибо" not in loop.end_phrases)
        ok &= check(
            "greeting_phrases из config.yaml", "привет" in loop.greeting_phrases
        )
        ok &= check("silence_s = 8", loop.silence_s == 8.0, str(loop.silence_s))
    finally:
        loop_module.transcribe_wav = original_transcribe  # type: ignore[assignment]

    print()
    print("PASS" if ok else "FAIL — скопируй весь вывод и пришли в чат")
    return 0 if ok else 1


if __name__ == "__main__":
    raise SystemExit(main())
