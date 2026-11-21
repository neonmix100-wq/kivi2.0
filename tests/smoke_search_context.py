"""Смоук шага 1.12: контекстный поиск (без сети и без реальной LLM).

Запуск:
    python tests/smoke_search_context.py

Что проверяется:
  1) reformulate_query исправляет ослышку и добавляет валюту/период;
  2) в промпт переформулировки уходит история диалога;
  3) summarize_with_llm получает историю и запрещает уточняющие вопросы;
  4) NOT_FOUND -> ровно вторая попытка с уточнённым запросом;
  5) поиск по сырой команде не выполняется ни разу;
  6) оба запроса и число результатов пишутся в лог;
  7) если данных нет и во второй раз — честный ответ NO_DATA_ANSWER.
"""

from __future__ import annotations

import sys
from pathlib import Path
from typing import Any

PROJECT_ROOT = Path(__file__).resolve().parents[1]
if str(PROJECT_ROOT) not in sys.path:
    sys.path.insert(0, str(PROJECT_ROOT))

from loguru import logger

import kivi.skills.search as search_module
from kivi.log import setup_logging
from kivi.skills.search import (
    NO_DATA_ANSWER,
    NOT_FOUND,
    answer_search,
    reformulate_query,
    summarize_with_llm,
)

HISTORY = [
    {"role": "user", "content": "какой курс биткоина"},
    {"role": "assistant", "content": "Курс биткоина около 90 тысяч рублей."},
]
RESULT = {
    "title": "Курс биткоина сегодня",
    "url": "https://example.com/btc",
    "snippet": "BTC торгуется около 90 000 рублей.",
}


def check(label: str, condition: bool, detail: str = "") -> bool:
    mark = "OK  " if condition else "X   "
    print(f"{mark} {label}" + (f" — {detail}" if detail else ""))
    return bool(condition)


class LogCatcher:
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


class ScriptedRouter:
    """Заглушка Router: отдаёт заданные ответы и помнит промпты."""

    def __init__(self, replies: list[str]) -> None:
        self.replies = list(replies)
        self.calls: list[dict[str, Any]] = []

    def complete(
        self, messages: Any, task_type: str = "chat", verify: Any = None
    ) -> dict[str, Any]:
        self.calls.append({"task_type": task_type, "messages": messages})
        text = self.replies.pop(0) if len(self.replies) > 1 else self.replies[0]
        return {"text": text, "cost_rub": 0.0, "model": "scripted", "ok": True}


def system_text(call: dict[str, Any]) -> str:
    messages = call.get("messages") or []
    return " ".join(
        str(item.get("content") or "")
        for item in messages
        if isinstance(item, dict) and item.get("role") == "system"
    )


def main() -> int:
    setup_logging()
    print("=== SMOKE 1.12: контекстный поиск ===")
    ok = True

    print("\n--- 1) переформулировка ослышки ---")
    router = ScriptedRouter(["курс биткоина сегодня"])
    query = reformulate_query("Какой сейчас курс под коина?", [], router=router)
    print(f"    запрос: {query!r}")
    ok &= check("в запросе есть «биткоин»", "биткоин" in query.lower())
    ok &= check(
        "Router вызван с task_type=intent",
        bool(router.calls) and router.calls[0]["task_type"] == "intent",
    )
    prompt = system_text(router.calls[0]) if router.calls else ""
    ok &= check("сырая реплика в промпте", "под коина" in prompt)
    ok &= check("инструкция про ослышки", "ослышки" in prompt)

    print("\n--- 2) история диалога в переформулировке ---")
    router = ScriptedRouter(["курс биткоина"])
    query = reformulate_query("Я имел в виду биткоин", HISTORY, router=router)
    print(f"    запрос: {query!r}")
    ok &= check("в запросе есть «курс»", "курс" in query.lower())
    prompt = system_text(router.calls[0]) if router.calls else ""
    ok &= check(
        "история ушла в промпт",
        "какой курс биткоина" in prompt and "90 тысяч" in prompt,
    )

    print("\n--- 3) summarize получает историю ---")
    router = ScriptedRouter(["Курс биткоина около 90 000 рублей."])
    answer = summarize_with_llm("курс биткоина", [RESULT], HISTORY, router=router)
    print(f"    ответ: {answer!r}")
    ok &= check("ответ непустой", bool(answer))
    ok &= check(
        "task_type=chat",
        bool(router.calls) and router.calls[0]["task_type"] == "chat",
    )
    prompt = system_text(router.calls[0]) if router.calls else ""
    ok &= check("история в промпте сводки", "90 тысяч" in prompt)
    ok &= check("запрет уточняющих вопросов", "уточняющих" in prompt)
    ok &= check("маркер NOT_FOUND в промпте", NOT_FOUND in prompt)

    original_search = search_module.search_web
    original_summary = search_module.summarize_with_llm
    try:
        print("\n--- 4) NOT_FOUND -> вторая попытка ---")
        searches: list[str] = []

        def fake_search(query, max_results=3, timeout=None, **kwargs):  # noqa: ANN001, ARG001
            searches.append(query)
            return [] if len(searches) == 1 else [RESULT]

        summaries: list[str] = []

        def fake_summary(query, results, history=None, router=None, **kwargs):  # noqa: ANN001, ARG001
            summaries.append(query)
            if history is not None and not history:
                raise AssertionError("history должна передаваться")
            return NOT_FOUND if len(summaries) == 1 else "Курс биткоина 90 000 рублей."

        search_module.search_web = fake_search  # type: ignore[assignment]
        search_module.summarize_with_llm = fake_summary  # type: ignore[assignment]
        router = ScriptedRouter(["курс биткоина", "курс биткоина сегодня"])

        with LogCatcher() as logs:
            answer = answer_search(
                "какой курс под коина", HISTORY, router=router
            )
        print(f"    поиски: {searches}")
        print(f"    ответ: {answer!r}")
        ok &= check("вторая попытка выполнена", len(searches) == 2)
        ok &= check(
            "второй запрос уточнён",
            len(searches) == 2 and searches[1] != searches[0],
            str(searches),
        )
        ok &= check(
            "по сырой команде не искали",
            all("под коина" not in item for item in searches),
            str(searches),
        )
        ok &= check("ответ из второй попытки", "90 000" in answer)
        ok &= check(
            "лог обоих запросов",
            any("попытка 1" in line for line in logs.messages)
            and any("попытка 2" in line for line in logs.messages),
            " / ".join(line for line in logs.messages if "попытка" in line),
        )

        print("\n--- 5) данных нет и во второй раз ---")
        searches.clear()
        summaries.clear()

        def empty_search(query, max_results=3, timeout=None, **kwargs):  # noqa: ANN001, ARG001
            searches.append(query)
            return []

        def always_not_found(query, results, history=None, router=None, **kwargs):  # noqa: ANN001, ARG001
            summaries.append(query)
            return NOT_FOUND

        search_module.search_web = empty_search  # type: ignore[assignment]
        search_module.summarize_with_llm = always_not_found  # type: ignore[assignment]
        router = ScriptedRouter(["курс биткоина", "курс биткоина сегодня"])
        answer = answer_search("какой курс под коина", HISTORY, router=router)
        print(f"    ответ: {answer!r}, попыток: {len(searches)}")
        ok &= check("честный ответ", answer == NO_DATA_ANSWER, answer)
        ok &= check("ровно две попытки поиска", len(searches) == 2, str(len(searches)))
    finally:
        search_module.search_web = original_search  # type: ignore[assignment]
        search_module.summarize_with_llm = original_summary  # type: ignore[assignment]

    print()
    print("PASS" if ok else "FAIL — скопируй весь вывод и пришли в чат")
    return 0 if ok else 1


if __name__ == "__main__":
    raise SystemExit(main())
