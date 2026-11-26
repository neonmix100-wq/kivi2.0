"""Смоук шага cost2: обрезка не провал, полная стоимость, локальное ядро памяти.

Запуск:
    python tests/smoke_cost2.py

Проверяется (моки Router/HTTP):
  1) finish_reason=length -> ответ обрезан по границе предложения, 0 ретраев, 0 эскалаций;
  2) пусто после обрезки -> ровно 1 ретрай с max_tokens*2;
  3) провал 0.4 + финал 0.6 -> turn cost 1.0, breakdown.chat 1.0, WARNING;
  4) routing chat/intent = cheap;
  5) медленный embed -> ядро profile/schedule/tasks на месте, semantic skip;
  6) пустой vault -> пустой блок без WARNING-спама.
"""

from __future__ import annotations

import sys
import time
from pathlib import Path
from typing import Any

PROJECT_ROOT = Path(__file__).resolve().parents[1]
if str(PROJECT_ROOT) not in sys.path:
    sys.path.insert(0, str(PROJECT_ROOT))

import kivi.memory.inject as inject_module
from kivi.brain.router import Router, default_verify
from kivi.config import get_settings, load_config
from kivi.log import setup_logging
from kivi.memory.inject import build_memory_block
from kivi.wake.loop import WakeLoop

TMP = PROJECT_ROOT / "data" / "tmp"
VAULT = TMP / "cost2_vault"
EMPTY_VAULT = TMP / "cost2_empty"


def check(label: str, condition: bool, detail: str = "") -> bool:
    mark = "OK  " if condition else "X   "
    print(f"{mark} {label}" + (f" — {detail}" if detail else ""))
    return bool(condition)


class LogCatcher:
    """Перехват INFO/WARNING loguru."""

    def __init__(self) -> None:
        self.messages: list[str] = []

    def _write(self, message: Any) -> None:
        self.messages.append(str(message).strip())

    def __enter__(self) -> "LogCatcher":
        from loguru import logger

        self._logger = logger
        self._sink = logger.add(self._write, level="INFO")
        return self

    def __exit__(self, *exc_info: Any) -> bool:
        if getattr(self, "_sink", None) is not None:
            self._logger.remove(self._sink)
            self._sink = None
        return False

    def has(self, needle: str) -> bool:
        return any(needle in line for line in self.messages)


class FakeCapture:
    samplerate = 48000
    block_frames = 1536
    is_running = False

    def read_block(self, timeout: float | None = None) -> None:
        return None

    def stop(self) -> None:
        return None


class FakeVad:
    def reset(self) -> None:
        return None

    def release(self) -> None:
        return None


class FakeRecorder:
    def cancel(self) -> None:
        return None


class FakePlanner:
    def plan(self, command: str, history: Any = None) -> dict[str, Any]:
        return {
            "skill": None,
            "args": {},
            "missing": [],
            "source": "test",
            "route": "chat",
            "gap_id": None,
        }


class FakeBudget:
    """Бюджет без SQLite: помнит сумму записанных попыток."""

    def __init__(self) -> None:
        self.total = 0.0

    def check(self) -> bool:
        return True

    def record(
        self,
        *,
        provider: str,
        model: str,
        task_type: str,
        tokens_in: int = 0,
        tokens_out: int = 0,
        cost_rub: float | None = None,
        ok: bool = True,
        attempt: int = 1,
    ) -> float:
        written = float(cost_rub or 0.0)
        self.total += written
        return written

    def month_spent_rub(self) -> float:
        return self.total

    def describe(self) -> str:
        return f"{self.total:.2f} руб"


class FakeProvider:
    """Скриптованный провайдер: отдаёт ответы по очереди."""

    name = "fake"

    def __init__(self, script: list[dict[str, Any]]) -> None:
        self.script = list(script)
        self.calls: list[dict[str, Any]] = []

    def complete(
        self,
        messages: Any,
        model: str,
        tools: Any = None,
        timeout: Any = None,
        *,
        max_tokens: int | None = None,
    ) -> dict[str, Any]:
        self.calls.append({"model": model, "max_tokens": max_tokens})
        item = self.script.pop(0) if len(self.script) > 1 else self.script[0]
        return {
            "text": str(item.get("text") or ""),
            "tokens_in": 10,
            "tokens_out": 20,
            "cost_rub": float(item.get("cost_rub") or 0.0),
            "finish_reason": str(item.get("finish_reason") or "stop"),
        }


def make_router(script: list[dict[str, Any]]) -> tuple[Router, FakeProvider, FakeBudget]:
    provider = FakeProvider(script)
    budget = FakeBudget()
    router = Router(get_settings(), budget=budget, provider=provider)  # type: ignore[arg-type]
    return router, provider, budget


def write_vault() -> None:
    VAULT.mkdir(parents=True, exist_ok=True)
    (VAULT / "profile.md").write_text(
        "# Профиль\n- Любит кофе по утрам\n- Живёт в Новосибирске\n",
        encoding="utf-8",
    )
    (VAULT / "schedule.md").write_text(
        "# Расписание\n- [пн 09:00] стендап\n", encoding="utf-8"
    )
    (VAULT / "tasks.md").write_text(
        "# Задачи\n- [open] купить корм\n- [done] старая задача\n", encoding="utf-8"
    )


def main() -> int:
    setup_logging()
    print("=== SMOKE cost2: обрезка, полная стоимость, ядро памяти ===")
    TMP.mkdir(parents=True, exist_ok=True)
    ok = True

    print("\n--- 1) finish_reason=length -> граница предложения, без ретраев ---")
    router, provider, _budget = make_router(
        [
            {
                "text": "Это первое предложение. А второе обрезано на полуслове",
                "finish_reason": "length",
                "cost_rub": 0.4,
            }
        ]
    )
    result = router.complete(
        [{"role": "user", "content": "расскажи"}], task_type="chat", max_tokens=600
    )
    text = str(result.get("text") or "")
    print(
        f"    text={text!r}, attempts={result.get('attempts')}, "
        f"tier={result.get('tier')}, cost={result.get('cost_rub')}"
    )
    ok &= check(
        "обрезано по границе предложения",
        text.startswith("Это первое предложение.")
        and text.endswith("…")
        and "второе" not in text,
        text,
    )
    ok &= check("ретраев 0", len(provider.calls) == 1, str(len(provider.calls)))
    ok &= check(
        "эскалаций 0 (tier=cheap, attempts=1)",
        result.get("tier") == "cheap" and int(result.get("attempts") or 0) == 1,
        f"{result.get('tier')}/{result.get('attempts')}",
    )
    ok &= check(
        "цена попытки учтена",
        abs(float(result.get("cost_rub") or 0.0) - 0.4) < 1e-9,
        str(result.get("cost_rub")),
    )

    print("\n--- 2) пусто после обрезки -> 1 ретрай с max_tokens*2 ---")
    router, provider, _budget = make_router(
        [
            {"text": "", "finish_reason": "length", "cost_rub": 0.1},
            {"text": "Полный русский ответ.", "finish_reason": "stop", "cost_rub": 0.2},
        ]
    )
    result = router.complete(
        [{"role": "user", "content": "расскажи"}], task_type="chat", max_tokens=200
    )
    limits = [call["max_tokens"] for call in provider.calls]
    print(f"    calls={limits}, text={result.get('text')!r}, cost={result.get('cost_rub')}")
    ok &= check("ровно один ретрай", len(provider.calls) == 2, str(len(provider.calls)))
    ok &= check("ретрай с max_tokens*2", limits[-1] == 400, str(limits))
    ok &= check("ответ получен", result.get("text") == "Полный русский ответ.")
    ok &= check(
        "сумма обеих попыток 0.3",
        abs(float(result.get("cost_rub") or 0.0) - 0.3) < 1e-9,
        str(result.get("cost_rub")),
    )

    print("\n--- 3) провал 0.4 + финал 0.6 -> turn 1.0 и WARNING ---")
    router, provider, budget = make_router(
        [
            {"text": "OK", "cost_rub": 0.4, "finish_reason": "stop"},
            {"text": "Хороший русский ответ.", "cost_rub": 0.6, "finish_reason": "stop"},
        ]
    )
    router.escalation = {
        "max_attempts_cheap": 1,
        "max_attempts_coder": 1,
        "then": "coder",
        "then_next": "strong",
    }
    loop = WakeLoop(
        settings=get_settings(),
        router=router,  # type: ignore[arg-type]
        capture=FakeCapture(),  # type: ignore[arg-type]
        vad=FakeVad(),  # type: ignore[arg-type]
        recorder=FakeRecorder(),  # type: ignore[arg-type]
        speak_fn=lambda text: None,
        session=None,
    )
    loop.planner = FakePlanner()  # type: ignore[assignment]
    try:
        with LogCatcher() as logs:
            answer, route = loop.handle_command(
                "расскажи что-нибудь", source="text", speak=False
            )
        print(
            f"    ответ={answer!r}, route={route}, "
            f"chat_cost={loop._cost.get('chat')}, budget_total={budget.total}"
        )
        ok &= check("ответ получен", bool(answer), answer)
        ok &= check(
            "turn cost = 1.0 (все попытки)",
            abs(float(loop._cost.get("chat") or 0.0) - 1.0) < 1e-9,
            str(loop._cost),
        )
        ok &= check(
            "цена каждой попытки в логе",
            logs.has("0.40000") and logs.has("0.60000"),
        )
        ok &= check("WARNING per_turn_warn", logs.has("per_turn_warn"))
    finally:
        try:
            loop.stop()
        except Exception:  # noqa: BLE001 - тестовые фейки
            pass


    print("\n--- 4) config: chat/intent -> cheap ---")
    routing = (load_config().model_dump().get("brain") or {}).get("routing") or {}
    print(f"    routing={routing}")
    ok &= check("chat = cheap", routing.get("chat") == "cheap", str(routing.get("chat")))
    ok &= check(
        "intent = cheap", routing.get("intent") == "cheap", str(routing.get("intent"))
    )

    print("\n--- 5) медленный embed -> ядро на месте, semantic skip ---")
    write_vault()
    original_retrieve = inject_module.MemoryRetriever.retrieve

    def slow_retrieve(self: Any, query: str, k: int | None = None) -> list[str]:
        time.sleep(2.0)
        return ["семантический факт"]

    inject_module.MemoryRetriever.retrieve = slow_retrieve  # type: ignore[assignment]
    try:
        started = time.perf_counter()
        with LogCatcher() as logs:
            block = build_memory_block("что у меня сегодня", vault_path=VAULT)
        elapsed = time.perf_counter() - started
    finally:
        inject_module.MemoryRetriever.retrieve = original_retrieve  # type: ignore[assignment]
    print(f"    блок {len(block)} симв., {elapsed * 1000:.0f} мс")
    print(f"    {block[:160]!r}")
    ok &= check("ядро: профиль", "Любит кофе" in block)
    ok &= check("ядро: расписание", "стендап" in block)
    ok &= check("ядро: открытые задачи", "купить корм" in block)
    ok &= check("semantic skip в логе", logs.has("semantic skip"))
    ok &= check("обогащение не попало в блок", "семантический факт" not in block)
    ok &= check("уложились в бюджет 0.8 с", elapsed < 1.5, f"{elapsed:.2f} с")

    print("\n--- 6) пустой vault -> пустой блок без WARNING-спама ---")
    empty = TMP / f"cost2_empty_{int(time.time() * 1000)}"
    empty.mkdir(parents=True, exist_ok=True)
    with LogCatcher() as logs:
        empty_block = build_memory_block("привет", vault_path=empty)
    warnings = [line for line in logs.messages if "WARNING" in line and "Память" in line]
    print(f"    блок={empty_block!r}, warnings={len(warnings)}")
    ok &= check("пустой vault -> пустой блок", empty_block == "", empty_block)
    ok &= check("без WARNING-спама", warnings == [], str(warnings[:2]))

    print()
    print("PASS" if ok else "FAIL — скопируй весь вывод и пришли в чат")
    return 0 if ok else 1


if __name__ == "__main__":
    raise SystemExit(main())

