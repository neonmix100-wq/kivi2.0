"""Смоук шага self-reliance: факты из открытых API, deep-read поиска, манифест.

Запуск:
    python tests/smoke_selfreliance.py

Что проверяется (сеть и LLM замоканы):
  1) facts/currency: мок er-api -> ответ с числом и ₽; домены в permissions;
  2) facts/weather: мок геокодинга и прогноза -> короткий текст;
  3) search deep-read: сниппеты без цифр + страница с цифрой -> ответ с цифрой;
  4) retry: второй запрос отличается; при совпадении retry пропущен;
     для второй попытки кэш не используется;
  5) chat system-промпт всегда содержит манифест и правило про «нет доступа»;
  6) мета-вопрос -> capabilities без LLM-планировщика, ответ по манифесту,
     без фразы «нет доступа к сети»;
  7) сбой facts -> fallback в search, а не «не умею».
"""

from __future__ import annotations

import sys
from pathlib import Path
from types import SimpleNamespace
from typing import Any

PROJECT_ROOT = Path(__file__).resolve().parents[1]
if str(PROJECT_ROOT) not in sys.path:
    sys.path.insert(0, str(PROJECT_ROOT))

import kivi.skills.broker as broker_module
import kivi.skills.search as search_mod
import kivi.wake.loop as loop_module
from kivi.brain.selfmodel import build_manifest, is_meta_question
from kivi.config import get_settings
from kivi.log import setup_logging
from kivi.skills.broker import PermissionDenied, SkillContext
from kivi.skills.gaps import GapLog
from kivi.skills.facts_compose import deterministic_answer, parse_payload
from kivi.skills.loader import FALLBACK_MARKER, invoke
from kivi.skills.planner import Planner
from kivi.skills.registry import SkillRegistry
from kivi.wake.loop import WakeLoop

TMP = PROJECT_ROOT / "data" / "tmp"

CURRENCY_JSON = (
    '{"result": "success", "base_code": "USD", '
    '"rates": {"RUB": 92.4, "EUR": 0.92}, '
    '"time_last_update_utc": "Tue, 25 Nov 2026 00:00:01 +0000"}'
)
GEO_JSON = (
    '{"results": [{"name": "Новосибирск", "latitude": 55.03, '
    '"longitude": 82.92, "country": "Россия"}]}'
)
FORECAST_JSON = (
    '{"current": {"temperature_2m": -3.4, "weather_code": 71, '
    '"wind_speed_10m": 4.2}}'
)


def check(label: str, condition: bool, detail: str = "") -> bool:
    mark = "OK  " if condition else "X   "
    print(f"{mark} {label}" + (f" — {detail}" if detail else ""))
    return bool(condition)


class LogCatcher:
    """Перехват INFO/WARNING loguru на время блока."""

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
    """Планировщик с заранее заданным планом (для fallback-теста)."""

    def __init__(self, plan: dict[str, Any]) -> None:
        self.plan_dict = plan
        self.calls = 0

    def plan(self, command: str, history: Any = None) -> dict[str, Any]:
        self.calls += 1
        return dict(self.plan_dict)


class ExplodingRouter:
    """Любой вызов LLM — провал теста (доказываем, что LLM не звали)."""

    def complete(self, messages: Any, task_type: str = "chat", **kwargs: Any) -> dict:
        raise AssertionError(f"LLM вызвана не должна была: task_type={task_type}")

    def complete_stream(self, messages: Any, task_type: str = "chat", **kwargs: Any):
        raise AssertionError(f"LLM вызвана не должна была: task_type={task_type}")


class ScriptedRouter:
    """Отдаёт заранее заданные ответы по очереди; пишет полученные промпты."""

    def __init__(self, replies: list[str]) -> None:
        self.replies = list(replies)
        self.calls: list[dict[str, Any]] = []

    def complete(self, messages: Any, task_type: str = "chat", **kwargs: Any) -> dict:
        self.calls.append({"messages": messages, "task_type": task_type})
        text = self.replies.pop(0) if self.replies else ""
        return {"text": text, "cost_rub": 0.0}


def make_http(routes: dict[str, str]):
    """Мок SkillContext._request_text: JSON по домену (сеть не нужна).

    Сначала вызываем настоящий _check_network — так тест проверяет,
    что permissions.network реально ограничивает домены.
    """

    def fake(self, method: str, url: str, timeout: Any = None, payload: Any = None) -> str:
        self._check_network(url)
        if "open.er-api.com" in url:
            return routes.get("currency", CURRENCY_JSON)
        if "geocoding-api.open-meteo.com" in url:
            return routes.get("geo", GEO_JSON)
        if "api.open-meteo.com" in url:
            return routes.get("forecast", FORECAST_JSON)
        raise AssertionError(f"неожиданный URL: {url}")

    return fake



def main() -> int:
    setup_logging()
    print("=== SMOKE self-reliance: факты, deep-read поиска, манифест ===")
    TMP.mkdir(parents=True, exist_ok=True)
    ok = True
    token = "selfrel"
    registry = SkillRegistry(
        skills_dir=PROJECT_ROOT / "skills",
        installed_dir=TMP / f"{token}_installed",
        db_path=TMP / f"{token}_skills.db",
    )
    registry.scan()

    facts = registry.get("facts")
    ok &= check("манифест facts найден", facts is not None)
    if facts is None:
        print("FAIL — нет skills/facts/skill.yaml")
        return 1
    ok &= check(
        "facts: permissions.network — 4 домена (включая CoinGecko)",
        set(facts.permissions.network)
        == {
            "open.er-api.com",
            "api.coingecko.com",
            "api.open-meteo.com",
            "geocoding-api.open-meteo.com",
        },
        str(facts.permissions.network),
    )
    ok &= check(
        "facts: tools=[http_get], risk=safe",
        list(facts.permissions.tools) == ["http_get"]
        and facts.permissions.risk == "safe",
    )

    original_request = broker_module.SkillContext._request_text
    broker_module.SkillContext._request_text = make_http({})  # type: ignore[assignment]
    try:
        print("\n--- 1) facts: курс валюты ---")
        raw = invoke(facts, {"command": "курс доллара к рублю"}, registry=registry)
        payload = parse_payload(raw)
        answer = deterministic_answer(payload) if payload else raw
        print(f"    {raw!r} -> {answer!r}")
        ok &= check("скилл вернул структурированные данные", payload is not None, raw[:80])
        ok &= check("в ответе число 92.4", "92.4" in answer, answer)
        ok &= check("в ответе рубль (₽)", "₽" in answer, answer)
        ok &= check("в ответе названа валюта", "доллара" in answer.lower(), answer)

        print("\n--- 1b) facts: запрещённый домен ---")
        try:
            SkillContext(facts).http_get("https://evil.test/rates")
            denied = False
        except PermissionDenied:
            denied = True
        ok &= check("брокер не пускает на чужой домен", denied)

        print("\n--- 2) facts: погода ---")
        weather_raw = invoke(facts, {"command": "погода в Новосибирске"}, registry=registry)
        weather_payload = parse_payload(weather_raw)
        weather = (
            deterministic_answer(weather_payload) if weather_payload else weather_raw
        )
        print(f"    {weather_raw!r} -> {weather!r}")
        ok &= check("город назван", "Новосибирск" in weather, weather)
        ok &= check("есть температура", "°C" in weather and "-3" in weather, weather)
        ok &= check(
            "код погоды переведён по-русски", "снег" in weather.lower(), weather
        )

        print("\n--- 3) поиск: deep-read страницы с цифрой ---")
        original_search = search_mod.search_web
        original_fetch = search_mod.fetch_page
        deep_calls: list[str] = []
        try:
            search_mod.search_web = (  # type: ignore[assignment]
                lambda query, max_results=3, timeout=None, **kw: [
                    {
                        "title": "Курс доллара",
                        "url": "https://example.test/usd",
                        "snippet": "Актуальные курсы валют на сегодня",
                    }
                ]
            )

            def fake_fetch(url: str, *, timeout: float | None = None) -> str:
                deep_calls.append(url)
                return "Доллар США стоит 92.4 рубля по данным ЦБ."

            search_mod.fetch_page = fake_fetch  # type: ignore[assignment]

            class DeepRouter:
                """intent -> запрос, chat -> сводка с цифрой из промпта."""

                def __init__(self) -> None:
                    self.prompts: list[str] = []

                def complete(self, messages: Any, task_type: str = "chat", **kw: Any) -> dict:
                    system = str(messages[0].get("content") or "")
                    user = str(messages[-1].get("content") or "")
                    self.prompts.append(system + "\n" + user)
                    if task_type == "intent":
                        return {"text": "сколько стоит биткоин сегодня", "cost_rub": 0.0}
                    if "92.4" in system or "92.4" in user:
                        return {
                            "text": "Доллар стоит 92.4 рубля (example.test).",
                            "cost_rub": 0.0,
                        }
                    return {"text": search_mod.NOT_FOUND, "cost_rub": 0.0}

            deep_router = DeepRouter()
            deep_answer = search_mod.answer_search(
                "сколько стоит биткоин сегодня", router=deep_router
            )
            print(f"    deep-read: {deep_answer!r}, страниц: {len(deep_calls)}")
            ok &= check("страница прочитана", bool(deep_calls), str(deep_calls))
            ok &= check("в ответе цифра со страницы", "92.4" in deep_answer, deep_answer)
            ok &= check(
                "цифра попала в промпт сводки",
                any("92.4" in prompt for prompt in deep_router.prompts),
            )
        finally:
            search_mod.search_web = original_search  # type: ignore[assignment]
            search_mod.fetch_page = original_fetch  # type: ignore[assignment]


        print("\n--- 4) retry: другой запрос и запрет кэша ---")
        original_search = search_mod.search_web
        original_summary = search_mod.summarize_with_llm
        try:
            searches: list[tuple[str, bool]] = []
            summaries: list[str] = []

            def fake_search(query: str, max_results: int = 3, timeout: Any = None, **kw: Any):
                searches.append((query, bool(kw.get("use_cache", True))))
                if len(searches) == 1:
                    return []
                return [
                    {
                        "title": "Курс",
                        "url": "https://example.test/usd",
                        "snippet": "92.4 рубля",
                    }
                ]

            def fake_summary(
                query: str,
                results: Any,
                history: Any = None,
                router: Any = None,
                **kw: Any,
            ):
                summaries.append(query)
                if len(summaries) == 1:
                    return search_mod.NOT_FOUND
                return "Курс доллара 92.4 рубля (example.test)."

            search_mod.search_web = fake_search  # type: ignore[assignment]
            search_mod.summarize_with_llm = fake_summary  # type: ignore[assignment]
            router = ScriptedRouter(["курс доллара", "курс доллара цб сегодня"])
            retry_answer = search_mod.answer_search("сколько стоит биткоин", router=router)
            print(f"    поиски: {searches}, ответ: {retry_answer!r}")
            ok &= check("две попытки", len(searches) == 2, str(len(searches)))
            ok &= check(
                "второй запрос отличается",
                len(searches) == 2 and searches[1][0] != searches[0][0],
                str(searches),
            )
            ok &= check(
                "вторая попытка без кэша",
                len(searches) == 2 and searches[1][1] is False,
                str(searches),
            )

            print("\n--- 4b) retry: одинаковый запрос -> пропуск ---")
            searches.clear()
            summaries.clear()
            router = ScriptedRouter(["курс доллара", "курс доллара"])
            with LogCatcher() as logs:
                same_answer = search_mod.answer_search("сколько стоит биткоин", router=router)
            print(f"    поиски: {searches}, ответ: {same_answer!r}")
            ok &= check("retry не выполнен", len(searches) == 1, str(searches))
            ok &= check("лог про пропуск retry", logs.has("retry пропущен"))
        finally:
            search_mod.search_web = original_search  # type: ignore[assignment]
            search_mod.summarize_with_llm = original_summary  # type: ignore[assignment]

        print("\n--- 5) манифест в system-промпте чата и суммаризатора ---")
        probe = object.__new__(WakeLoop)
        probe.skills = registry  # type: ignore[attr-defined]
        chat_system = WakeLoop._system_prompt(probe, "")
        print(f"    чат-промпт: {len(chat_system)} символов")
        ok &= check("манифест в чат-промпте", "МОЁ СОСТОЯНИЕ" in chat_system)
        ok &= check(
            "правило про «нет доступа» в промпте",
            "НИКОГДА не говори «нет доступа к сети»" in chat_system,
        )

        original_search = search_mod.search_web
        original_fetch = search_mod.fetch_page
        try:
            search_mod.search_web = (  # type: ignore[assignment]
                lambda query, max_results=3, timeout=None, **kw: [
                    {"title": "Курс", "url": "https://example.test", "snippet": "92.4"}
                ]
            )
            search_mod.fetch_page = lambda url, **kw: ""  # type: ignore[assignment]
            summary_router = ScriptedRouter(["Курс 92.4 рубля."])
            search_mod.summarize_with_llm(
                "курс доллара",
                [{"title": "Курс", "url": "https://example.test", "snippet": "92.4"}],
                [],
                router=summary_router,
            )
            system_prompt = str(summary_router.calls[0]["messages"][0]["content"])
            ok &= check("манифест в промпте суммаризатора", "МОЁ СОСТОЯНИЕ" in system_prompt)
        finally:
            search_mod.search_web = original_search  # type: ignore[assignment]
            search_mod.fetch_page = original_fetch  # type: ignore[assignment]

        print("\n--- 6) мета-вопрос -> capabilities (без LLM) ---")
        question = "что тебе требуется чтобы был доступ к поиску?"
        ok &= check("вопрос распознан как мета", is_meta_question(question))
        gaps = GapLog(TMP / f"{token}_gaps.db")
        planner = Planner(
            registry=registry,
            strategy="llm_only",
            router=ExplodingRouter(),  # type: ignore[arg-type]
            gaps=gaps,
        )
        plan = planner.plan(question)
        skill_name = getattr(plan.get("skill"), "name", None)
        print(f"    план: {skill_name}, source={plan.get('source')}")
        ok &= check("маршрут capabilities без LLM", skill_name == "capabilities")
        ok &= check("источник плана fast", plan.get("source") == "fast")
        meta_reply = invoke(plan["skill"], plan.get("args") or {}, registry=registry)
        print(f"    ответ: {meta_reply!r}")
        ok &= check("ответ про доступ есть", "доступ" in meta_reply.lower(), meta_reply)
        ok &= check(
            "нет выдуманного «нет доступа к сети»",
            "нет доступа" not in meta_reply.lower(),
            meta_reply,
        )

        facts_plan = planner.plan("курс доллара к рублю")
        facts_name = getattr(facts_plan.get("skill"), "name", None)
        # Жадных keyword-правил больше нет: курс решает LLM-планировщик,
        # поэтому при недоступной LLM здесь не должно быть fast-маршрута facts.
        ok &= check(
            "курс не уходит в facts по keyword (нет fast)",
            facts_plan.get("source") != "fast" and facts_name is None,
            f"source={facts_plan.get('source')}, skill={facts_name}",
        )

        print("\n--- 7) сбой facts -> fallback в search ---")

        def failing_request(
            self, method: str, url: str, timeout: Any = None, payload: Any = None
        ) -> str:
            raise RuntimeError(f"HTTP 503 от {url}")

        broker_module.SkillContext._request_text = failing_request  # type: ignore[assignment]

        class KnowledgeRouter:
            """intent -> запрос, chat -> короткий ответ с цифрой."""

            def complete(self, messages: Any, task_type: str = "chat", **kw: Any) -> dict:
                if task_type == "intent":
                    return {"text": "курс доллара к рублю", "cost_rub": 0.0}
                return {"text": "Курс доллара 92.4 рубля (example.test).", "cost_rub": 0.0}

        loop = WakeLoop(
            settings=get_settings(),
            router=KnowledgeRouter(),  # type: ignore[arg-type]
            capture=FakeCapture(),  # type: ignore[arg-type]
            vad=FakeVad(),  # type: ignore[arg-type]
            recorder=FakeRecorder(),  # type: ignore[arg-type]
            speak_fn=lambda text: None,
            session=None,
        )
        loop.skills = registry
        loop.planner = FakePlanner(  # type: ignore[assignment]
            {
                "skill": facts,
                "args": {
                    "kind": "currency",
                    "base": "USD",
                    "quote": "RUB",
                    "command": "курс доллара к рублю",
                },
                "missing": [],
                "source": "fast",
                "route": "skill",
                "gap_id": None,
            }
        )
        original_search = search_mod.search_web
        try:
            search_mod.search_web = (  # type: ignore[assignment]
                lambda query, max_results=3, timeout=None, **kw: [
                    {
                        "title": "Курс доллара",
                        "url": "https://example.test/usd",
                        "snippet": "92.4 рубля за доллар",
                    }
                ]
            )
            with LogCatcher() as logs:
                fallback_answer, fallback_route = loop.handle_command(
                    "курс доллара к рублю", source="text", speak=False
                )
            print(f"    route={fallback_route}, ответ={fallback_answer!r}")
            ok &= check("маршрут переключился на search", fallback_route == "search")
            ok &= check("лог про skill->search", logs.has("маршрут skill->search"))
            ok &= check(
                "ответ из поиска, а не «не умею»", "92.4" in fallback_answer, fallback_answer
            )
            ok &= check(
                "фразы «не умею» нет",
                "не умею" not in fallback_answer.lower(),
                fallback_answer,
            )
        finally:
            search_mod.search_web = original_search  # type: ignore[assignment]
            try:
                loop.stop()
            except Exception:  # noqa: BLE001 - тестовые фейки
                pass
    finally:
        broker_module.SkillContext._request_text = original_request  # type: ignore[assignment]

    print()
    print("PASS" if ok else "FAIL — скопируй весь вывод и пришли в чат")
    return 0 if ok else 1


if __name__ == "__main__":
    raise SystemExit(main())

