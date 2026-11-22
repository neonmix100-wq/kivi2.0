"""Смоук планировщика рефлексии: 10 минут модельного времени, мок сна/часов.

Запуск:
    python tests/smoke_reflect_sched.py

Проверяется:
  1) за 10 минут — не больше 11 проверок и не больше 1 skip-лога;
  2) при новых репликах и простое > порога рефлексия вызвана ровно один раз,
     last_reflected_ts обновлён, повторно в интервале не вызывается;
  3) повторный старт планировщика не создаёт второй поток;
  4) засыпание между проверками обязательно (мок сна считает вызовы).
"""

from __future__ import annotations

import sys
import threading
from pathlib import Path
from typing import Any

PROJECT_ROOT = Path(__file__).resolve().parents[1]
if str(PROJECT_ROOT) not in sys.path:
    sys.path.insert(0, str(PROJECT_ROOT))

from loguru import logger

import kivi.memory.reflect as reflect_module
from kivi.memory.reflect import (
    ReflectScheduler,
    start_reflect_scheduler,
    stop_reflect_scheduler,
)


class LogCatcher:
    def __init__(self) -> None:
        self.messages: list[str] = []
        self._sink = None

    def _write(self, message: Any) -> None:
        self.messages.append(str(message).strip())

    def __enter__(self) -> "LogCatcher":
        self._sink = logger.add(self._write, level="DEBUG")
        return self

    def __exit__(self, *exc_info: Any) -> bool:
        if self._sink is not None:
            logger.remove(self._sink)
            self._sink = None
        return False


class FakeClock:
    """Модельные часы: старт в 60 c, остановка после 660 c (10 минут)."""

    def __init__(self, start_mono: float = 60.0, start_wall: float = 1100.0) -> None:
        self.mono = float(start_mono)
        self.wall = float(start_wall)
        self.sleep_calls = 0
        self.first_sleep = threading.Event()
        self.release = threading.Event()

    def monotonic(self) -> float:
        return self.mono

    def time(self) -> float:
        return self.wall

    def sleep(self, seconds: float) -> bool:
        self.sleep_calls += 1
        if self.sleep_calls == 1:
            self.first_sleep.set()
            self.release.wait(timeout=3.0)
        self.mono += float(seconds)
        self.wall += float(seconds)
        return self.mono >= 660.0


class FakeSession:
    def __init__(self, last_activity: float = 1001.0) -> None:
        self.last_activity = float(last_activity)
        self.items: list[tuple[float, str]] = [(1001.0, "Киви, запомни: люблю кофе")]
        self.lock = threading.Lock()

    def add(self, ts: float, text: str) -> None:
        with self.lock:
            self.items.append((float(ts), str(text)))

    def messages_since(self, since_ts: float, *, max_tokens: int = 3000):
        with self.lock:
            selected = [
                {"role": "user", "content": text}
                for ts, text in self.items
                if ts > float(since_ts)
            ]
            max_ts = max((ts for ts, _text in self.items), default=float(since_ts))
        return selected, max_ts

    def last_activity_ts(self) -> float:
        return self.last_activity


class FakeReflector:
    def __init__(self) -> None:
        self.calls = 0
        self.seen: list[list[dict]] = []

    def run(self, messages: list[dict]) -> dict[str, Any]:
        self.calls += 1
        self.seen.append(list(messages))
        return {"facts": 1, "decisions": 0, "open_loops": 0, "cost_rub": 0.0}


def check(label: str, condition: bool, detail: str = "") -> bool:
    print(("OK  " if condition else "X   ") + label + (f" — {detail}" if detail else ""))
    return bool(condition)


def scheduler_threads() -> int:
    return sum(
        1
        for thread in threading.enumerate()
        if thread.name == "kivi-reflect-scheduler" and thread.is_alive()
    )


def main() -> int:
    print("=== SMOKE: планировщик рефлексии (10 минут модельного времени) ===")
    ok = True
    clock = FakeClock()
    session = FakeSession()
    reflector = FakeReflector()
    scheduler = ReflectScheduler(
        reflector,
        session,
        check_s=60.0,
        min_interval_s=300.0,
        idle_min_s=600.0,
        clock=clock.monotonic,
        wall_clock=clock.time,
        sleep_fn=clock.sleep,
    )

    with LogCatcher() as logs:
        thread = scheduler.start()
        if not clock.first_sleep.wait(timeout=3.0):
            print("X   планировщик не дошёл до первого сна")
            scheduler.stop()
            return 1
        thread_again = scheduler.start()
        ok &= check("повторный start вернул тот же поток", thread_again is thread)
        ok &= check("потоков планировщика ровно один", scheduler_threads() == 1)
        clock.release.set()
        thread.join(timeout=5.0)

        ok &= check("планировщик остановился по модельному времени", not thread.is_alive())
        ok &= check("проверок <= 11", scheduler.checks <= 11, f"checks={scheduler.checks}")
        ok &= check(
            "сон между проверками вызывался",
            clock.sleep_calls >= 10,
            f"sleep_calls={clock.sleep_calls}",
        )
        skip_logs = [line for line in logs.messages if "пропуск" in line.lower()]
        ok &= check("skip-логов <= 1", len(skip_logs) <= 1, f"skip_logs={len(skip_logs)}")
        ok &= check(
            "рефлексия вызвана ровно один раз",
            reflector.calls == 1,
            f"calls={reflector.calls}",
        )
        ok &= check(
            "last_reflected_ts обновлён",
            scheduler.last_reflected_ts == 1001.0,
            f"last_reflected_ts={scheduler.last_reflected_ts}",
        )

        # Новая реплика сразу после прохода не должна дать второй проход:
        # прошло меньше reflect_min_interval_s (300 c).
        session.add(1010.0, "Ещё одна реплика")
        scheduler.check_once()
        ok &= check(
            "внутри 5 минут повторной рефлексии нет",
            reflector.calls == 1,
            f"calls={reflector.calls}",
        )

    # Модульный singleton: второй старт возвращает тот же объект.
    module_scheduler = start_reflect_scheduler(
        FakeReflector(),
        FakeSession(),
        check_s=60.0,
        min_interval_s=300.0,
        idle_min_s=600.0,
        clock=lambda: 0.0,
        wall_clock=lambda: 0.0,
        sleep_fn=lambda _seconds: True,
    )
    module_again = start_reflect_scheduler(
        FakeReflector(),
        FakeSession(),
        check_s=60.0,
        min_interval_s=300.0,
        idle_min_s=600.0,
        clock=lambda: 0.0,
        wall_clock=lambda: 0.0,
        sleep_fn=lambda _seconds: True,
    )
    ok &= check(
        "модульный повторный старт вернул тот же планировщик",
        module_again is module_scheduler,
    )
    ok &= check("модульный флаг _started выставлен", reflect_module._started)
    stop_reflect_scheduler()
    ok &= check(
        "после stop модульный флаг сброшен",
        scheduler_threads() == 0 and not reflect_module._started,
        f"threads={scheduler_threads()}",
    )

    print()
    print("PASS" if ok else "FAIL — скопируй весь вывод и пришли в чат")
    return 0 if ok else 1


if __name__ == "__main__":
    raise SystemExit(main())
