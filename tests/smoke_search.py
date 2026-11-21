"""Смоук поиска в интернете: DuckDuckGo + короткая сводка через LLM.

Запуск:
    python tests/smoke_search.py

Что проверяется:
  1) секция search в config.yaml (enabled, max_results, timeout_s, fact_keywords);
  2) looks_factual: «кто выиграл…» — да, «расскажи анекдот» — нет;
  3) живой поиск DDG: непустой список [{title, url, snippet}] + лог
     «поиск: '<query>' -> N результатов»;
  4) мусорный запрос -> короткий список или [];
  5) summarize_with_llm: мок Router, task_type=chat, результаты в <untrusted>;
  6) таймаут поиска -> [] (DDGS-заглушка спит дольше таймаута);
  7) ошибка DDGS -> [] (процесс не падает).

Внешний LLM не вызывается, ключи не нужны. Живой запрос — один.
"""

from __future__ import annotations

import sys
import time
from pathlib import Path
from typing import Any

PROJECT_ROOT = Path(__file__).resolve().parents[1]
if str(PROJECT_ROOT) not in sys.path:
    sys.path.insert(0, str(PROJECT_ROOT))

from loguru import logger

from kivi.log import setup_logging
from kivi.skills.search import (
    NOT_FOUND,
    SearchConfig,
    load_search_config,
    looks_factual,
    search_web,
    summarize_with_llm,
)

LIVE_QUERY = "курс биткоина сегодня"
TRASH_QUERY = "несуществующий запрос xyz123"
MAX_RESULTS = 3


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


class FakeRouter:
    """Заглушка Router: помнит вызовы, денег не тратит."""

    def __init__(self, text: str) -> None:
        self.text = text
        self.calls: list[dict[str, Any]] = []

    def complete(
        self,
        messages: Any,
        task_type: str = "chat",
        verify: Any = None,
    ) -> dict[str, Any]:
        self.calls.append({"task_type": task_type, "messages": messages})
        return {"text": self.text, "cost_rub": 0.0, "model": "fake", "ok": True}


class BrokenRouter:
    """Заглушка Router, которая всегда падает."""

    def complete(self, messages: Any, task_type: str = "chat", verify: Any = None):
        raise RuntimeError("LLM недоступна")


class SlowDDGS:
    """Заглушка DDGS: спит дольше таймаута."""

    def __init__(self, timeout: Any = None, **kwargs: Any) -> None:
        pass

    def __enter__(self) -> "SlowDDGS":
        return self

    def __exit__(self, *exc_info: Any) -> bool:
        return False

    def text(self, query: str, max_results: int | None = None) -> list[dict[str, str]]:
        time.sleep(5.0)
        return []


class BrokenDDGS:
    """Заглушка DDGS: падает при создании."""

    def __init__(self, timeout: Any = None, **kwargs: Any) -> None:
        raise RuntimeError("DDG недоступен")


def live_search() -> list[dict[str, str]]:
    """Живой запрос с двумя повторами на случай rate-limit DuckDuckGo."""
    results: list[dict[str, str]] = []
    for attempt in range(1, 4):
        results = search_web(LIVE_QUERY, max_results=MAX_RESULTS)
        if results:
            return results
        print(f"    попытка {attempt}: пусто, повтор через 2 с")
        time.sleep(2.0)
    return results


def main() -> int:
    setup_logging()
    print("=== SMOKE 1.8: поиск в интернете (DuckDuckGo) ===")
    ok = True

    print("\n--- 1) конфиг search ---")
    cfg = load_search_config()
    try:
        parsed = SearchConfig.model_validate(cfg)
    except Exception as exc:  # noqa: BLE001 - битый конфиг виден по проверкам ниже
        print(f"    конфиг невалиден: {exc}")
        parsed = SearchConfig()
    print(f"    enabled={parsed.enabled}, max_results={parsed.max_results}, "
          f"timeout_s={parsed.timeout_s}, keywords={len(parsed.fact_keywords)}")
    ok &= check("search.enabled = true", parsed.enabled is True)
    ok &= check("search.max_results = 3", parsed.max_results == MAX_RESULTS)
    ok &= check("search.timeout_s = 15", abs(parsed.timeout_s - 15.0) < 0.01)
    ok &= check("в fact_keywords есть «кто»", "кто" in parsed.fact_keywords)

    print("\n--- 2) looks_factual ---")
    ok &= check(
        "«кто выиграл матч вчера» — фактический вопрос",
        looks_factual("кто выиграл матч вчера"),
    )
    ok &= check(
        "«расскажи анекдот про кота» — не фактический",
        not looks_factual("расскажи анекдот про кота"),
    )
    ok &= check("пустая команда — не фактическая", not looks_factual(""))

    print("\n--- 3) живой поиск ---")
    with LogCatcher() as logs:
        results = live_search()
    print(f"    результатов: {len(results)}")
    for item in results[:3]:
        print(f"    - {item['title'][:60]!r} | {item['url']}")
    ok &= check("список непустой", bool(results))
    ok &= check("не больше 3 результатов", len(results) <= MAX_RESULTS, str(len(results)))
    ok &= check(
        "у элементов есть title/url/snippet",
        bool(results)
        and all({"title", "url", "snippet"} <= set(item) for item in results),
    )
    expected = f"поиск: '{LIVE_QUERY}' -> {len(results)} результатов"
    ok &= check(
        "лог с числом результатов",
        any(expected in line for line in logs.messages),
        expected,
    )
    ok &= check("пустой запрос -> [] без сети", search_web("", max_results=3) == [])

    print("\n--- 4) мусорный запрос ---")
    trash = search_web(TRASH_QUERY, max_results=MAX_RESULTS)
    print(f"    результатов: {len(trash)}")
    ok &= check(
        "короткий список или []",
        isinstance(trash, list) and len(trash) <= MAX_RESULTS,
        str(len(trash)),
    )

    print("\n--- 5) summarize_with_llm (мок Router) ---")
    sample = results[:2] or [
        {"title": "Пример", "url": "https://example.com", "snippet": "текст"}
    ]
    router = FakeRouter("Курс биткоина держится около девяноста тысяч долларов.")
    answer = summarize_with_llm(LIVE_QUERY, sample, router=router)
    print(f"    ответ: {answer!r}")
    ok &= check("ответ непустой", bool(answer))
    ok &= check("Router вызван один раз", len(router.calls) == 1)
    ok &= check(
        "task_type=chat",
        bool(router.calls) and router.calls[0]["task_type"] == "chat",
    )
    system_text = str(router.calls[0]["messages"][0]["content"]) if router.calls else ""
    user_text = str(router.calls[0]["messages"][1]["content"]) if router.calls else ""
    ok &= check("в промпте есть запрос", LIVE_QUERY in system_text)
    ok &= check("результаты обёрнуты в <untrusted>", "<untrusted>" in user_text)
    ok &= check(
        "без результатов -> NOT_FOUND",
        summarize_with_llm(LIVE_QUERY, [], router=router) == NOT_FOUND,
    )
    ok &= check(
        "ошибка LLM -> NOT_FOUND",
        summarize_with_llm(LIVE_QUERY, sample, router=BrokenRouter()) == NOT_FOUND,
    )

    print("\n--- 6) таймаут и ошибка поиска ---")
    import duckduckgo_search

    original = duckduckgo_search.DDGS
    try:
        duckduckgo_search.DDGS = SlowDDGS
        started = time.perf_counter()
        timed_out = search_web("таймаут", max_results=1, timeout=0.2)
        elapsed = time.perf_counter() - started
        print(f"    таймаут 0.2 с -> {timed_out!r} за {elapsed:.2f} с")
        ok &= check("таймаут -> []", timed_out == [])
        ok &= check("вернулся быстро (<3 c)", elapsed < 3.0, f"{elapsed:.2f} c")

        duckduckgo_search.DDGS = BrokenDDGS
        broken = search_web("ошибка поиска", max_results=1, timeout=5)
        print(f"    ошибка DDGS -> {broken!r}")
        ok &= check("ошибка DDGS -> []", broken == [])
    finally:
        duckduckgo_search.DDGS = original

    print()
    print("PASS" if ok else "FAIL — скопируй весь вывод и пришли в чат")
    return 0 if ok else 1


if __name__ == "__main__":
    raise SystemExit(main())
