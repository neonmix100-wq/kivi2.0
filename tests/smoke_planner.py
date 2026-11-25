"""Смоук шага 1.6: LLM-планировщик умений + журнал пробелов.

Запуск:
    python tests/smoke_planner.py

Что проверяется:
  1) каталог умений для промпта: имена, triggers, бюджет ~600 токенов;
  2) «который час» и «какая сегодня дата» -> time_date ЧЕРЕЗ LLM (реальный Router);
  3) галлюцинированное имя скилла -> валидация, fallback на difflib, WARNING;
  4) невалидный JSON -> fallback, WARNING в лог;
  5) «напиши функцию fib» -> skill=None (скилла code нет) + пробел в БД;
  6) «расскажи анекдот про кота» x3 -> None, пробелы; stats_by_intent -> «анекдоты»;
  7) стоимость plan_with_llm < 0.02 руб;
  8) таблица capability_gaps есть в боевой data/kivi.db.

Реальный LLM — только в пункте 2 (тир cheap, копейки). Остальные сценарии
детерминированы: ScriptedRouter отдаёт заранее заданный текст.
Пробелы в тесте пишутся в data/tmp/planner_smoke.db, боевая БД только читается.
"""

from __future__ import annotations

import sqlite3
import sys
from contextlib import closing
from pathlib import Path
from typing import Any

PROJECT_ROOT = Path(__file__).resolve().parents[1]
if str(PROJECT_ROOT) not in sys.path:
    sys.path.insert(0, str(PROJECT_ROOT))

from loguru import logger

from kivi.brain.budget import Budget
from kivi.brain.router import Router
from kivi.config import DATA_DIR, get_settings
from kivi.log import setup_logging
from kivi.skills.gaps import GapLog
from kivi.skills.llm_planner import build_catalog, last_call, parse_response
from kivi.skills.planner import Planner, get_top_gaps
from kivi.skills.registry import get_registry

TMP = PROJECT_ROOT / "data" / "tmp"
GAP_DB = TMP / "planner_smoke.db"
COST_LIMIT_RUB = 0.02  # route-правила добавили объём промпта: 1-2 копейки за вызов


def check(label: str, condition: bool, detail: str = "") -> bool:
    mark = "OK " if condition else "X  "
    print(f"{mark} {label}" + (f" — {detail}" if detail else ""))
    return bool(condition)


class ScriptedRouter:
    """Заглушка Router: отдаёт заранее заданный текст, денег не тратит."""

    def __init__(self, replies: list[str], cost_rub: float = 0.0015) -> None:
        self.replies = list(replies)
        self.cost_rub = float(cost_rub)
        self.calls: list[dict[str, Any]] = []

    def complete(
        self,
        messages: Any,
        task_type: str = "chat",
        verify: Any = None,
    ) -> dict[str, Any]:
        self.calls.append({"task_type": task_type, "messages": messages})
        text = self.replies.pop(0) if len(self.replies) > 1 else self.replies[0]
        return {
            "text": text,
            "tokens_in": 200,
            "tokens_out": 80,
            "cost_rub": self.cost_rub,
            "provider": "scripted",
            "model": "scripted/planner",
            "tier": "cheap",
            "task_type": task_type,
            "attempts": 1,
            "ok": True,
        }


class LogCatcher:
    """Перехват WARNING/ERROR из loguru на время блока."""

    def __init__(self) -> None:
        self.messages: list[tuple[str, str]] = []
        self._sink = None

    def _write(self, message: Any) -> None:
        self.messages.append(
            (str(message.record["level"].name), str(message).strip())
        )

    def __enter__(self) -> "LogCatcher":
        self._sink = logger.add(self._write, level="WARNING")
        return self

    def __exit__(self, *exc_info: Any) -> bool:
        if self._sink is not None:
            logger.remove(self._sink)
            self._sink = None
        return False

    def has(self, needle: str) -> bool:
        lowered = needle.lower()
        return any(lowered in text.lower() for _, text in self.messages)


def reset_gaps(db_path: Path) -> GapLog:
    """Чистая таблица пробелов в тестовой БД (файл не удаляем)."""
    gaps = GapLog(db_path)  # создаёт таблицу, если её нет
    with closing(sqlite3.connect(str(db_path))) as connection, connection:
        connection.execute("DELETE FROM capability_gaps")
    return gaps


def step_catalog(registry) -> bool:
    """Каталог умений и разбор ответов LLM (без сети)."""
    print("\n--- 1) каталог умений для промпта ---")
    catalog = build_catalog(registry)
    for line in catalog.splitlines():
        print(f"    {line}")
    tokens_est = max(1, len(catalog) // 3)
    ok = True
    ok &= check("каталог содержит time_date", "- time_date:" in catalog)
    ok &= check("у каждой строки есть triggers", "triggers:" in catalog)
    ok &= check(
        "каталог в бюджете ~600 токенов",
        tokens_est <= 660,
        f"~{tokens_est} токенов, {len(catalog)} символов",
    )

    print("\n--- 1b) parse_response: JSON в ``` и с текстом вокруг ---")
    fenced = (
        'Вот план:\n```json\n'
        '{"skill_name": "time_date", "args": {"fmt": "short"}, "missing": []}\n'
        "```\nГотово."
    )
    parsed = parse_response(fenced, registry)
    ok &= check(
        "JSON из markdown вытащен и провалидирован",
        parsed is not None
        and parsed["skill_name"] == "time_date"
        and parsed["args"] == {"fmt": "short"},
        str(parsed),
    )
    bogus = parse_response('{"skill_name": "teleport", "args": {}}', registry)
    ok &= check("неизвестное имя скилла -> None", bogus is None)
    broken = parse_response("никакого JSON тут нет", registry)
    ok &= check("текст без JSON -> None", broken is None)
    return ok


def step_live(registry, planner: Planner) -> bool:
    """Реальные вызовы LLM: синонимы, которых difflib бы не поймал."""
    print("\n--- 2) живой LLM-планировщик (Router task_type=intent) ---")
    ok = True
    cases = [("который час", "time_date"), ("какая сегодня дата", "time_date")]
    for command, expected in cases:
        plan = planner.plan(command)
        meta = last_call()
        name = plan["skill"].name if plan["skill"] is not None else None
        print(
            f"    {command!r} -> {name} (источник {plan['source']}, "
            f"{meta['ms']:.0f} мс, {meta['cost_rub']:.5f} руб, {meta['model']})"
        )
        ok &= check(
            f"{command!r} -> {expected} через LLM",
            name == expected and plan["source"] == "llm",
            f"причина: {meta['reason']}, raw: {meta['raw'][:80]!r}",
        )
        ok &= check(
            "стоимость вызова plan_with_llm < 0.02 руб",
            float(meta["cost_rub"]) < COST_LIMIT_RUB,
            f"{float(meta['cost_rub']):.5f} руб",
        )
    return ok


def step_fallbacks(registry, gaps: GapLog) -> bool:
    """Битый JSON и выдуманное умение: валидация + difflib-фолбэк."""
    ok = True

    print("\n--- 3) галлюцинированное умение -> fallback ---")
    scripted = ScriptedRouter(['{"skill_name": "weather_now", "args": {}}'])
    planner = Planner(registry, router=scripted, gaps=gaps)
    with LogCatcher() as logs:
        # Команда нарочно не курс и не погода: fast-правило facts её не перехватит.
        plan = planner.plan("включи свет в гостиной")
    meta = last_call()
    name = plan["skill"].name if plan["skill"] is not None else None
    print(f"    ответ LLM: {scripted.replies[0][:80]!r} -> {name} (источник {plan['source']})")
    ok &= check("выдуманное умение не принято", plan["skill"] is None)
    ok &= check(
        "причина fallback = unknown_skill",
        meta["reason"] == "unknown_skill",
        meta["reason"],
    )
    ok &= check("WARNING про неизвестное умение", logs.has("неизвестное умение"))
    recent = gaps.list_recent(limit=1)
    ok &= check(
        "в пробеле сохранено имя выдуманного скилла",
        bool(recent) and recent[0]["skill_attempted"] == "weather_now",
        str(recent[0]["skill_attempted"]) if recent else "пусто",
    )

    print("\n--- 4) невалидный JSON -> fallback + WARNING ---")
    scripted = ScriptedRouter(["Конечно! Вот план: skill_name = time_date"])
    planner = Planner(registry, router=scripted, gaps=gaps)
    with LogCatcher() as logs:
        plan = planner.plan("который час")
    meta = last_call()
    name = plan["skill"].name if plan["skill"] is not None else None
    print(f"    ответ LLM: {scripted.replies[0]!r} -> {name} (источник {plan['source']})")
    ok &= check("причина fallback = parse_error", meta["reason"] == "parse_error", meta["reason"])
    ok &= check("WARNING про невалидный JSON", logs.has("невалидный JSON"))
    ok &= check(
        "difflib-фолбэк нашёл time_date",
        name == "time_date" and plan["source"] == "fuzzy",
        f"источник {plan['source']}",
    )
    return ok



def step_gaps(registry, gaps: GapLog) -> bool:
    """Пробелы: fib (нет умения code), анекдоты, статистика и закрытие."""
    ok = True

    print("\n--- 5) «напиши функцию fib» -> умения code нет, пробел в БД ---")
    scripted = ScriptedRouter(
        [
            '{"skill_name": null, "args": {}, "missing": '
            '[{"capability": "написание кода", "why": "в каталоге нет умения code"}]}'
        ]
    )
    planner = Planner(registry, router=scripted, gaps=gaps)
    plan = planner.plan("напиши функцию fib")
    meta = last_call()
    print(
        f"    skill={plan['skill']}, источник {plan['source']}, "
        f"missing={plan['missing']}, {meta['cost_rub']:.5f} руб"
    )
    ok &= check(
        "валидный ответ LLM без умения",
        plan["skill"] is None and plan["source"] == "llm",
        plan["source"],
    )
    ok &= check(
        "missing описывает намерение, а не команду",
        bool(plan["missing"])
        and plan["missing"][0]["capability"] == "написание кода",
        str(plan["missing"]),
    )
    fib_rows = [
        row
        for row in gaps.list_recent(limit=20)
        if row["user_intent"] == "написание кода"
    ]
    ok &= check(
        "пробел записан в capability_gaps",
        bool(fib_rows),
        f"id={fib_rows[0]['id']}" if fib_rows else "нет строки",
    )

    print("\n--- 6) «расскажи анекдот про кота» x3 -> топ по частоте ---")
    scripted = ScriptedRouter(
        [
            '{"skill_name": null, "args": {}, "missing": '
            '[{"capability": "анекдоты", "why": "нет умения с шутками"}]}'
        ]
    )
    planner = Planner(registry, router=scripted, gaps=gaps)
    plans = [planner.plan("расскажи анекдот про кота") for _ in range(3)]
    ok &= check("все три раза skill=None", all(p["skill"] is None for p in plans))

    stats = gaps.stats_by_intent(top=10)
    for row in stats:
        print(
            f"    {row['user_intent']}: пробелов {row['gaps']}, "
            f"запросов {row['requests']}, закрыто {row['closed']}"
        )
    top = stats[0] if stats else {}
    ok &= check(
        "stats_by_intent: «анекдоты» в топе",
        top.get("user_intent") == "анекдоты",
        str(top.get("user_intent")),
    )
    ok &= check(
        "частота >= 3 (повторы копятся)",
        int(top.get("requests") or 0) >= 3,
        str(top.get("requests")),
    )

    print("\n--- 6b) close() закрывает пробел ---")
    joke_ids = [
        row["id"]
        for row in gaps.list_recent(limit=20)
        if row["user_intent"] == "анекдоты"
    ]
    gap_id = int(joke_ids[0]) if joke_ids else -1
    ok &= check("пробел найден в списке", gap_id > 0, f"id={gap_id}")
    ok &= check("close() вернул True", gaps.close(gap_id, how="skill: jokes"))
    closed = gaps.get(gap_id)
    ok &= check(
        "closed_how записан",
        closed is not None and closed["closed_how"] == "skill: jokes",
        str(closed["closed_how"]) if closed else "нет строки",
    )
    ok &= check("повторное close() -> False", gaps.close(gap_id, how="ещё раз") is False)

    print("\n--- 6c) get_top_gaps() для отчётов ---")
    listed = get_top_gaps(5)
    ok &= check(
        "get_top_gaps возвращает список",
        isinstance(listed, list),
        f"{len(listed)} строк",
    )
    return ok


def step_database() -> bool:
    """Таблица capability_gaps должна жить в боевой data/kivi.db."""
    print("\n--- 7) боевая БД data/kivi.db ---")
    prod = GapLog()
    print(f"    БД: {prod.db_path}")
    ok = True
    ok &= check("по умолчанию data/kivi.db", prod.db_path == DATA_DIR / "kivi.db")
    with closing(sqlite3.connect(str(prod.db_path))) as connection:
        row = connection.execute(
            "SELECT name FROM sqlite_master "
            "WHERE type='table' AND name='capability_gaps'"
        ).fetchone()
    ok &= check("таблица capability_gaps создана", row is not None)
    return ok


def main() -> int:
    setup_logging()
    print("=== SMOKE 1.6: LLM-планировщик умений + capability_gaps ===")
    TMP.mkdir(parents=True, exist_ok=True)

    registry = get_registry()
    print("умения в каталоге:", ", ".join(m.name for m in registry.list_enabled()))

    ok = step_catalog(registry)

    gaps = reset_gaps(GAP_DB)
    print(f"\nтестовая БД пробелов: {gaps.db_path}")

    settings = get_settings()
    router = Router(settings, budget=Budget(settings))
    print(f"бюджет: {router.budget.describe()}")
    planner = Planner(registry, router=router, gaps=gaps)
    print(
        f"стратегия: {planner.strategy}, каталог {planner.catalog_max_tokens} токенов, "
        f"fallback_on_parse_error={planner.fallback_on_parse_error}"
    )
    ok &= step_live(registry, planner)

    ok &= step_fallbacks(registry, gaps)
    ok &= step_gaps(registry, gaps)
    ok &= step_database()

    print()
    print("PASS" if ok else "FAIL — скопируй весь вывод и пришли в чат")
    return 0 if ok else 1


if __name__ == "__main__":
    raise SystemExit(main())

