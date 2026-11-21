"""Смоук шага 0.8: прослушка и детекция имени «Киви».

Запуск:
    python tests/smoke_wakeword.py            # 60 секунд (нужен микрофон)
    python tests/smoke_wakeword.py --seconds 20
    python tests/smoke_wakeword.py --simulate # без микрофона: фейковый STT

Режим dry-run: имя ловим и озвучиваем «Слушаю» (из кэша, бесплатно),
но в LLM команды не отправляем — только печатаем, что отправили бы.
"""

from __future__ import annotations

import argparse
import sys
from pathlib import Path

PROJECT_ROOT = Path(__file__).resolve().parents[1]
if str(PROJECT_ROOT) not in sys.path:
    sys.path.insert(0, str(PROJECT_ROOT))

from kivi.config import ConfigError, DeviceNotFoundError, get_settings
from kivi.log import setup_logging
from kivi.brain.session import Session
from kivi.tts.cache import speak_cached
from kivi.wake.loop import WakeLoop, WakeResult

DEFAULT_SECONDS = 60.0
TMP_SESSION_DB = PROJECT_ROOT / "data" / "tmp" / "test_session.db"


class DryRouter:
    """Заглушка LLM: печатает команду и ничего не тратит."""

    def complete(self, messages, task_type: str = "chat", verify=None) -> dict:  # noqa: ANN001
        text = messages if isinstance(messages, str) else str(messages)
        print(f"    -> [dry-run] ушло бы в LLM (task={task_type}): {text!r}", flush=True)
        return {"text": ""}


class _FakeCapture:
    is_running = False

    def read_block(self, timeout: float | None = None):
        return None

    def snapshot(self, milliseconds: float):
        return None


class _FakeVad:
    clock_offset = 0.0

    def reset(self) -> None:
        pass

    def feed_block(self, block):  # noqa: ANN001
        return []


class _FakeRecorder:
    is_recording = False

    def cancel(self) -> None:
        pass


def run_simulated(loop: WakeLoop, results: list[WakeResult]) -> None:
    """Сценарий без микрофона: имя, приветствие, продолжение, «спасибо»."""
    from types import SimpleNamespace

    import kivi.wake.loop as loop_module

    script = ["Киви", "Киви, привет", "который час", "спасибо"]

    def fake_transcribe(path):  # noqa: ANN001, ARG001
        return script.pop(0) if script else ""

    loop.capture = _FakeCapture()
    loop.vad = _FakeVad()
    loop.recorder = _FakeRecorder()
    loop.cooldown_s = 0.0
    loop.speak_fn = lambda text: print(f"    сказала: {text}", flush=True)
    loop._think_and_speak = lambda command: loop._complete_turn(  # type: ignore[method-assign]
        command, f"ответ: {command}"
    )
    loop._answer_chat = lambda command, plan, started: loop._complete_turn(  # type: ignore[method-assign]
        command, "Привет, Лёша!"
    )
    loop_module.transcribe_wav = fake_transcribe  # type: ignore[assignment]

    print("--- Симуляция без микрофона (фейковый STT) ---")
    for _ in range(4):
        loop._handle_utterance(
            SimpleNamespace(path=PROJECT_ROOT / "data" / "tmp" / "fake.wav", speech_s=2.0)
        )


def report(results: list[WakeResult], *, simulated: bool = False) -> int:
    total = len(results)
    wake_hits = [item for item in results if item.is_wake]
    fuzzy_hits = [item for item in wake_hits if item.fuzzy]
    listens = [item for item in wake_hits if item.action == "listen"]
    thinks = [item for item in results if item.action in ("think", "followup")]
    idle = [item for item in results if item.action == "idle"]
    cooldown = [item for item in results if item.action == "cooldown"]

    print()
    print("--- Сводка ---")
    print(f"распознано высказываний: {total}")
    print(f"срабатываний по имени:   {len(wake_hits)}")
    print(f"  из них нечётких (могут быть ложными): {len(fuzzy_hits)}")
    print(f"  «Слушаю» + окно:       {len(listens)}")
    print(f"команд отправлено в LLM (dry-run): {len(thinks)}")
    print(f"фраз без имени (молча):  {len(idle)}")
    print(f"отсечено cooldown:       {len(cooldown)}")
    print()
    print(f"ложных (нечётких) срабатываний: {len(fuzzy_hits)}")

    ok = True
    if total == 0:
        print("X   ничего не распознано — проверь микрофон и уровень входа")
        ok = False
    if not wake_hits:
        print("X   ни одного срабатывания по имени «Киви»")
        ok = False
    if simulated and (len(listens) < 1 or len(thinks) < 2 or len(idle) < 1):
        print("X   симуляция: ожидались имя, приветствие, продолжение и «спасибо»")
        ok = False
    print()
    print("PASS" if ok else "FAIL — скопируй весь вывод и пришли в чат")
    return 0 if ok else 1


def main() -> int:
    parser = argparse.ArgumentParser(description="Смоук wake-word")
    parser.add_argument("--seconds", type=float, default=DEFAULT_SECONDS)
    parser.add_argument(
        "--simulate",
        action="store_true",
        help="без микрофона: прогнать фразы через loop с фейковым STT",
    )
    args = parser.parse_args()

    setup_logging()
    print("=== SMOKE: прослушка и имя «Киви» ===")

    try:
        settings = get_settings()
    except (ConfigError, DeviceNotFoundError) as exc:
        print("FAIL — конфиг:", exc)
        return 1

    results: list[WakeResult] = []

    def on_result(result: WakeResult) -> None:
        results.append(result)
        print(f"  {result}", flush=True)

    loop = WakeLoop(
        settings=settings,
        router=DryRouter(),
        speak_fn=speak_cached,
        on_result=on_result,
        session=Session(db_path=TMP_SESSION_DB),
    )

    if args.simulate:
        run_simulated(loop, results)
        return report(results, simulated=True)

    devices = settings.devices
    print(f"Вход:  [{devices.input_index}] {devices.input_name}")
    print(f"Выход: [{devices.output_index}] {devices.output_name}")
    print(f"Слушаю {args.seconds:.0f} c. Скажи: «Киви», «Киви, привет», «Киви, <команда>».")
    print()
    print(flush=True)

    try:
        loop.run(max_seconds=args.seconds)
    except KeyboardInterrupt:
        print("\nОстановлено вручную.")

    return report(results)


if __name__ == "__main__":
    raise SystemExit(main())
