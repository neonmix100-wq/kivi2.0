"""Смоук шага 1.7: закрытие пробелов в умениях (без микрофона).

Запуск:
    python tests/smoke_gap_closer.py

Что проверяется:
  1) registry.scan находит временный скилл jokes;
  2) автовыход: 3 ошибки -> should_disable=True;
  3) capability_gaps: attempts_to_close копится, get_frequent_gaps(min_count=3);
  4) ReactivateStrategy включает выключенный jokes (difflib по триггерам);
  5) ComposeStrategy: мок LLM -> {"skills": ["http_get", "file_search"]};
  6) AskUserStrategy: мок LLM -> {"questions": [...]};
  7) GapCloser.attempt_close идёт по порядку и закрывает пробел
     (closed_how = reactivate / compose / ask);
  8) таблица capability_gaps расширена полями closed_at/closed_how/notes.

Весь LLM — ScriptedRouter (0 руб), реальные модели не вызываются.
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

from kivi.config import DATA_DIR
from kivi.log import setup_logging
from kivi.skills.gap_closer import (
    AskUserStrategy,
    ComposeStrategy,
    GapCloser,
    GapCloserConfig,
    ReactivateStrategy,
    SearchStrategy,
)
from kivi.skills.gaps import GapLog
from kivi.skills.registry import SkillRegistry
from kivi.skills.search import SearchConfig

TMP = PROJECT_ROOT / "data" / "tmp"
PROBE_ROOT = TMP / "gap_closer_smoke"
JOKES_DIR = PROBE_ROOT / "jokes"
SKILLS_DB = TMP / "gap_closer_skills.db"
GAPS_DB = TMP / "gap_closer_gaps.db"

JOKES_MANIFEST = """\
name: jokes
version: "0.1.0"
description: "Анекдоты и шутки (временный скилл смоука)"
category: media
triggers:
  - анекдоты
  - расскажи анекдот
  - пошути
intents:
  - рассказать анекдот
requires:
  python: ">=3.12"
  binaries: []
  env: []
  hardware: []
permissions:
  network: []
  filesystem: []
  tools: []
  risk: safe
entry: "handler.py:handle"
enabled: true
source:
  kind: generated
  repo: ""
  commit: ""
"""

JOKES_HANDLER = '''\
"""Временный скилл смоука: отдаёт заглушку анекдота."""

from __future__ import annotations


def handle(ctx) -> str:
    return "Шутка: программист ставит себе будильник на 7:59."
'''

COMPOSE_REPLY = '{"skills": ["http_get", "file_search"], "how": "найти сайт и файл"}'
ASK_REPLY = '{"questions": ["текстом или голосом?"]}'


def check(label: str, condition: bool, detail: str = "") -> bool:
    mark = "OK  " if condition else "X   "
    print(f"{mark} {label}" + (f" — {detail}" if detail else ""))
    return bool(condition)


class ScriptedRouter:
    """Заглушка Router: отдаёт заранее заданный JSON, денег не тратит."""

    def __init__(self, replies: list[str]) -> None:
        self.replies = list(replies)
        self.calls: list[dict[str, Any]] = []

    def complete(
        self,
        messages: Any,
        task_type: str = "chat",
        verify: Any = None,
    ) -> dict[str, Any]:
        self.calls.append({"task_type": task_type, "messages": messages})
        text = self.replies.pop(0) if len(self.replies) > 1 else self.replies[0]
        return {"text": text, "cost_rub": 0.0, "model": "scripted", "ok": True}


class FakeSearcher:
    """Заглушка search_web: отдаёт заданные результаты, помнит запросы."""

    def __init__(self, results: list[dict[str, str]] | None = None) -> None:
        self.results = list(results or [])
        self.calls: list[dict[str, Any]] = []

    def __call__(
        self,
        query: str,
        max_results: int = 3,
        timeout: float | None = None,
        **kwargs: Any,
    ) -> list[dict[str, str]]:
        self.calls.append({"query": query, "max_results": max_results, "timeout": timeout})
        return list(self.results)


class FakeSummarizer:
    """Заглушка summarize_with_llm: фиксированный ответ, помнит вызовы."""

    def __init__(self, answer: str = "Матч выиграла «Заря» со счётом 2:1.") -> None:
        self.answer = answer
        self.calls: list[str] = []

    def __call__(
        self,
        query: str,
        results: list[dict[str, Any]],
        *,
        router: Any = None,
        **kwargs: Any,
    ) -> str:
        self.calls.append(query)
        return self.answer


def prepare_skill() -> None:
    JOKES_DIR.mkdir(parents=True, exist_ok=True)
    (JOKES_DIR / "skill.yaml").write_text(JOKES_MANIFEST, encoding="utf-8")
    (JOKES_DIR / "handler.py").write_text(JOKES_HANDLER, encoding="utf-8")


def make_registry() -> SkillRegistry:
    registry = SkillRegistry(
        skills_dir=PROJECT_ROOT / "skills",
        installed_dir=PROBE_ROOT,
        db_path=SKILLS_DB,
        auto_disable_after_fails=3,
    )
    with closing(sqlite3.connect(str(SKILLS_DB))) as connection, connection:
        connection.execute("DELETE FROM skills_stats")
        connection.execute("DELETE FROM skill_flags")
    registry.scan()
    return registry


def make_gaps() -> GapLog:
    gaps = GapLog(GAPS_DB)
    with closing(sqlite3.connect(str(GAPS_DB))) as connection, connection:
        connection.execute("DELETE FROM capability_gaps")
    return gaps


def add_gap(gaps: GapLog, command: str, intent: str, times: int = 3) -> int:
    gap_id = 0
    for _ in range(max(1, times)):
        gap_id = gaps.record_gap(command, user_intent=intent, why="нет умения смоука")
    return gap_id


def main() -> int:
    setup_logging()
    print("=== SMOKE 1.7: закрытие пробелов в умениях ===")
    TMP.mkdir(parents=True, exist_ok=True)
    prepare_skill()

    registry = make_registry()
    gaps = make_gaps()
    ok = True

    print("\n--- 1) registry.scan ---")
    names = [item.name for item in registry.list_all()]
    print(f"    умения: {', '.join(names)}")
    jokes = registry.get("jokes")
    ok &= check("временный скилл jokes найден", jokes is not None)

    print("\n--- 2) автовыход jokes ---")
    for _ in range(3):
        registry.record_use("jokes", ok=False)
    ok &= check("should_disable(jokes) = True", registry.should_disable("jokes") is True)

    print("\n--- 3) журнал пробелов ---")
    gap_id = add_gap(gaps, "анекдоты", "анекдоты", times=3)
    gap = gaps.get(gap_id)
    print(f"    пробел id={gap_id}: {gap}")
    ok &= check(
        "attempts_to_close = 3",
        gap is not None and int(gap["attempts_to_close"]) == 3,
        str(gap["attempts_to_close"]) if gap else "нет строки",
    )

    rare_id = add_gap(gaps, "переведи текст", "перевод текста", times=2)
    frequent = gaps.get_frequent_gaps(min_count=3)
    frequent_ids = {int(row["id"]) for row in frequent}
    for row in frequent:
        print(f"    частый: {row['user_intent']} ({row['attempts_to_close']} раз)")
    ok &= check("частый пробел в выдаче", gap_id in frequent_ids)
    ok &= check("редкий пробел (2 раза) не в выдаче", rare_id not in frequent_ids)

    print("\n--- 4) ReactivateStrategy ---")
    reactivate = ReactivateStrategy(registry, threshold=0.7)
    success, how, message = reactivate.attempt(gap)
    print(f"    -> {success}, {how}, {message!r}")
    ok &= check("похожее выключенное умение включено", success and how == "reactivate")
    ok &= check("в сообщении имя jokes", "jokes" in message)
    ok &= check("jokes включён в реестре", registry.get("jokes").enabled is True)
    ok &= check("серия ошибок сброшена", registry.should_disable("jokes") is False)

    print("\n--- 5) ComposeStrategy ---")
    compose_router = ScriptedRouter([COMPOSE_REPLY])
    compose = ComposeStrategy(compose_router, registry)
    success, how, message = compose.attempt(gap)
    print(f"    -> {success}, {how}, {message!r}")
    ok &= check("композиция найдена", success and how == "compose")
    ok &= check(
        "в сообщении умения композиции",
        "http_get" in message and "file_search" in message,
        message,
    )
    ok &= check(
        "task_type=intent",
        bool(compose_router.calls) and compose_router.calls[0]["task_type"] == "intent",
    )

    print("\n--- 6) AskUserStrategy ---")
    ask_router = ScriptedRouter([ASK_REPLY])
    ask = AskUserStrategy(ask_router)
    success, how, message = ask.attempt(gap)
    print(f"    -> {success}, {how}, {message!r}")
    ok &= check("вопросы не считаются закрытием", success is False and how == "ask")
    ok &= check("вопрос из LLM в сообщении", "текстом или голосом?" in message)
    ok &= check(
        "task_type=chat",
        bool(ask_router.calls) and ask_router.calls[0]["task_type"] == "chat",
    )

    print("\n--- 7) GapCloser: реактивация по порядку ---")
    for _ in range(3):
        registry.record_use("jokes", ok=False)
    ok &= check("jokes снова выключен", registry.should_disable("jokes") is True)
    order_router = ScriptedRouter([COMPOSE_REPLY])
    closer = GapCloser(
        router=order_router,
        registry=registry,
        gaps=gaps,
        config=GapCloserConfig(min_frequency=3, reactivate_threshold=0.7),
        search_config={"enabled": False},
    )
    success, how, message = closer.attempt_close(gap_id)
    print(f"    -> {success}, {how}, {message!r}")
    ok &= check("первая успешная стратегия — reactivate", success and how == "reactivate")
    ok &= check("compose не вызывался (порядок)", order_router.calls == [])
    closed = gaps.get(gap_id)
    ok &= check(
        'пробел помечен closed_how="reactivate"',
        closed is not None
        and closed["closed_how"] == "reactivate"
        and closed["closed_at"] is not None,
        str(closed["closed_how"]) if closed else "нет строки",
    )
    ok &= check("notes записаны", bool(closed and closed["notes"]))

    print("\n--- 7b) GapCloser: композиция ---")
    compose_gap = add_gap(gaps, "переведи текст", "перевод текста", times=3)
    compose_router = ScriptedRouter([COMPOSE_REPLY])
    closer = GapCloser(
        router=compose_router,
        registry=registry,
        gaps=gaps,
        config=GapCloserConfig(min_frequency=3),
        search_config={"enabled": False},
    )
    success, how, message = closer.attempt_close(compose_gap)
    print(f"    -> {success}, {how}, {message!r}")
    ok &= check("закрыто композицией", success and how == "compose")
    ok &= check(
        "closed_how=compose",
        (gaps.get(compose_gap) or {}).get("closed_how") == "compose",
    )

    print("\n--- 7c) GapCloser: вопросы пользователю ---")
    ask_gap = add_gap(gaps, "поставь напоминание", "напоминания", times=3)
    ask_closer_router = ScriptedRouter([ASK_REPLY])
    closer = GapCloser(
        router=ask_closer_router,
        registry=registry,
        gaps=gaps,
        config=GapCloserConfig(min_frequency=3, compose_enabled=False, ask_enabled=True),
        search_config={"enabled": False},
    )
    success, how, message = closer.attempt_close(ask_gap)
    print(f"    -> {success}, {how}, {message!r}")
    ok &= check("вернулась последняя стратегия ask", success is False and how == "ask")
    ok &= check(
        "closed_how=ask (вопросы заданы)",
        (gaps.get(ask_gap) or {}).get("closed_how") == "ask",
    )
    ok &= check(
        "compose отключён конфигом — один вызов LLM (chat)",
        [call["task_type"] for call in ask_closer_router.calls] == ["chat"],
        str([call["task_type"] for call in ask_closer_router.calls]),
    )
    ok &= check(
        "несуществующий пробел -> none",
        closer.attempt_close(999999)[1] == "none",
    )

    print("\n--- 8) SearchStrategy: факты из интернета ---")
    fact_results = [
        {
            "title": "Заря победила в матче",
            "url": "https://example.com/news",
            "snippet": "Матч завершился со счётом 2:1.",
        }
    ]
    search_gap = add_gap(gaps, "кто выиграл матч вчера", "результат матча", times=3)
    searcher = FakeSearcher(fact_results)
    summarizer = FakeSummarizer("Матч выиграла «Заря» со счётом 2:1.")
    search_strategy = SearchStrategy(
        router=ScriptedRouter([COMPOSE_REPLY]),
        config=SearchConfig(enabled=True, max_results=3, timeout_s=15),
        searcher=searcher,
        summarizer=summarizer,
    )
    success, how, message = search_strategy.attempt(gaps.get(search_gap))
    print(f"    -> {success}, {how}, {message!r}")
    ok &= check("фактический пробел найден в интернете", success and how == "search")
    ok &= check("в сообщении «Нашла в интернете»", "Нашла в интернете" in message)
    ok &= check(
        "поиск вызван с командой пробела",
        bool(searcher.calls) and searcher.calls[0]["query"] == "кто выиграл матч вчера",
    )
    ok &= check("сводка LLM вызвана", summarizer.calls == ["кто выиграл матч вчера"])

    print("\n--- 8b) порядок стратегий reactivate -> search -> compose -> ask ---")
    order_search_router = ScriptedRouter([COMPOSE_REPLY])
    order_searcher = FakeSearcher(fact_results)
    order_closer = GapCloser(
        router=order_search_router,
        registry=registry,
        gaps=gaps,
        config=GapCloserConfig(min_frequency=3),
        search_config={"enabled": True, "max_results": 3, "timeout_s": 15},
        searcher=order_searcher,
        summarizer=FakeSummarizer("Ответ из интернета."),
    )
    names_order = [item.name for item in order_closer.strategies]
    print(f"    стратегии: {' -> '.join(names_order)}")
    ok &= check(
        "порядок reactivate -> search -> compose -> ask",
        names_order == ["reactivate", "search", "compose", "ask"],
    )
    success, how, message = order_closer.attempt_close(search_gap)
    print(f"    -> {success}, {how}, {message!r}")
    ok &= check("закрыто поиском до композиции", success and how == "search")
    ok &= check("compose не вызывался (search раньше)", order_search_router.calls == [])
    ok &= check(
        "closed_how=search",
        (gaps.get(search_gap) or {}).get("closed_how") == "search",
    )

    print("\n--- 8c) пустой поиск -> compose ---")
    empty_search_gap = add_gap(gaps, "кто выиграл матч вчера", "результат матча", times=3)
    empty_searcher = FakeSearcher([])
    empty_compose_router = ScriptedRouter([COMPOSE_REPLY])
    empty_closer = GapCloser(
        router=empty_compose_router,
        registry=registry,
        gaps=gaps,
        config=GapCloserConfig(min_frequency=3),
        search_config={"enabled": True},
        searcher=empty_searcher,
        summarizer=FakeSummarizer("не должно использоваться"),
    )
    success, how, message = empty_closer.attempt_close(empty_search_gap)
    print(f"    -> {success}, {how}, {message!r}")
    ok &= check("пустой поиск -> compose", success and how == "compose", how)
    ok &= check("поиск вызывался один раз", len(empty_searcher.calls) == 1)
    ok &= check(
        "compose-роутер получил intent",
        [call["task_type"] for call in empty_compose_router.calls] == ["intent"],
    )

    print("\n--- 8d) не фактическая команда -> поиск пропущен ---")
    not_fact_gap = add_gap(gaps, "поставь напоминание", "напоминания", times=3)
    skip_searcher = FakeSearcher(fact_results)
    skip_strategy = SearchStrategy(
        router=None,
        config=SearchConfig(enabled=True),
        searcher=skip_searcher,
        summarizer=FakeSummarizer(),
    )
    success, how, message = skip_strategy.attempt(gaps.get(not_fact_gap))
    print(f"    -> {success}, {how}, {message!r}")
    ok &= check("search не вызывался", skip_searcher.calls == [])
    ok &= check("search вернул False", success is False and how == "search")

    disabled_strategy = SearchStrategy(
        router=None,
        config=SearchConfig(enabled=False),
        searcher=skip_searcher,
        summarizer=FakeSummarizer(),
    )
    success, how, message = disabled_strategy.attempt(gaps.get(search_gap))
    ok &= check("search.enabled=false -> пропуск", success is False and how == "search")

    print("\n--- 9) схема capability_gaps ---")
    with closing(sqlite3.connect(str(GAPS_DB))) as connection:
        columns = {
            str(row[1]) for row in connection.execute("PRAGMA table_info(capability_gaps)")
        }
    print(f"    колонки: {', '.join(sorted(columns))}")
    for column in ("closed_how", "closed_at", "notes", "attempts_to_close"):
        ok &= check(f"колонка {column}", column in columns)

    prod_db = DATA_DIR / "kivi.db"
    with closing(sqlite3.connect(str(prod_db))) as connection:
        prod_columns = {
            str(row[1]) for row in connection.execute("PRAGMA table_info(capability_gaps)")
        }
    print(f"    боевая {prod_db.name}: {len(prod_columns)} колонок")
    ok &= check(
        "в data/kivi.db есть closed_how/closed_at/notes",
        {"closed_how", "closed_at", "notes"} <= prod_columns,
    )

    print(f"\nумений в реестре: {len(names)} ({', '.join(names)})")
    print(f"пробелов в журнале: {len(gaps.list_recent(limit=50))}")
    print()
    print("PASS" if ok else "FAIL — скопируй весь вывод и пришли в чат")
    return 0 if ok else 1


if __name__ == "__main__":
    raise SystemExit(main())
