"""Смоук шага reasoning: сущности, поправки, композиция ответа думающей моделью.

Запуск:
    python tests/smoke_reasoning.py

Что проверяется (сеть и LLM замоканы):
  1) «курс биткоина к доллару» -> facts/crypto, CoinGecko с ids=bitcoin,
     ответ содержит число BTC в USD и НЕ «доллара к рублю»;
  2) поправка «я просил биткоина а не доллара» -> args coin=bitcoin,
     ответ отличается от предыдущего;
  3) незнакомая монета -> facts не маппит -> route=search;
  4) planner fast НЕ срабатывает на «курс ...» (идёт LLM intent);
  5) config brain.routing chat/intent = coder;
  6) guard: одинаковый ответ на поправку -> search со сводкой на тире strong.
"""

from __future__ import annotations

import json
import sys
from pathlib import Path
from typing import Any

PROJECT_ROOT = Path(__file__).resolve().parents[1]
if str(PROJECT_ROOT) not in sys.path:
    sys.path.insert(0, str(PROJECT_ROOT))

import kivi.skills.broker as broker_module
import kivi.skills.search as search_mod
from kivi.config import get_settings, load_config
from kivi.log import setup_logging
from kivi.skills.facts_intent import resolve
from kivi.skills.planner import Planner
from kivi.skills.registry import SkillRegistry
from kivi.wake.loop import WakeLoop

TMP = PROJECT_ROOT / "data" / "tmp"

COINGECKO_JSON = (
    '{"bitcoin": {"usd": 91234.5, "rub": 8900000, "eur": 84000, '
    '"last_updated_at": 1795598019}}'
)
ER_JSON = '{"result": "success", "rates": {"RUB": 92.4}}'
GEO_JSON = '{"results": [{"name": "Новосибирск", "latitude": 55.03, "longitude": 82.92}]}'
FORECAST_JSON = (
    '{"current": {"temperature_2m": -1.4, "weather_code": 3, "wind_speed_10m": 14.0}}'
)


def check(label: str, condition: bool, detail: str = "") -> bool:
    mark = "OK  " if condition else "X   "
    print(f"{mark} {label}" + (f" — {detail}" if detail else ""))
    return bool(condition)


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


class ScriptedLLM:
    """Мок Router: планировщик, композитор и суммаризатор в одном лице."""

    COMPOSE_TEXT = "Биткоин сейчас стоит 91 234 доллара."

    def __init__(self) -> None:
        self.calls: list[dict[str, Any]] = []
        self.compose_text = self.COMPOSE_TEXT

    # --- планировщик ---
    @staticmethod
    def _plan_for(command: str) -> dict[str, Any]:
        lower = command.lower().replace("ё", "е")
        if "шмэкоин" in lower:
            return {
                "skill_name": "facts",
                "args": {"kind": "crypto", "coin": "shmecoin"},
                "route": "skill",
                "confidence": 0.9,
                "missing": [],
            }
        if "биткоин" in lower:
            return {
                "skill_name": "facts",
                "args": {"kind": "crypto", "coin": "bitcoin"},
                "route": "skill",
                "confidence": 0.95,
                "missing": [],
            }
        if "доллар" in lower:
            return {
                "skill_name": "facts",
                "args": {"kind": "currency", "base": "USD", "quote": "RUB"},
                "route": "skill",
                "confidence": 0.9,
                "missing": [],
            }
        if "погод" in lower:
            return {
                "skill_name": "facts",
                "args": {"kind": "weather", "city": "Новосибирск"},
                "route": "skill",
                "confidence": 0.9,
                "missing": [],
            }
        return {"skill_name": None, "args": {}, "route": "chat", "confidence": 0.8, "missing": []}

    def complete(self, messages: Any, task_type: str = "chat", **kwargs: Any) -> dict:
        system = str(messages[0].get("content") or "")
        user = str(messages[-1].get("content") or "")
        self.calls.append({"task_type": task_type, "system": system, "user": user})
        if "Ты — планировщик умений Киви" in system:
            return {"text": json.dumps(self._plan_for(user), ensure_ascii=False), "cost_rub": 0.002}
        if "используя ТОЛЬКО данные" in system:
            if '"kind": "weather"' in system:
                return {
                    "text": "В Новосибирске сейчас -1 °C, пасмурно.",
                    "cost_rub": 0.003,
                }
            return {"text": self.compose_text, "cost_rub": 0.003}
        if "результаты поиска" in system:
            if task_type == "strong":
                return {"text": "По свежим данным: биткоин около 91 200 долларов.", "cost_rub": 0.02}
            return {"text": "По данным поиска: биткоин около 91 000 долларов.", "cost_rub": 0.01}
        if "поисковый запрос" in system:
            return {"text": "курс биткоина", "cost_rub": 0.001}
        return {"text": "ок", "cost_rub": 0.0}


def make_http(urls: list[str]):
    """Мок SkillContext._request_text: JSON по домену, проверка доменов брокером."""

    def fake(self, method: str, url: str, timeout: Any = None, payload: Any = None) -> str:
        self._check_network(url)
        urls.append(url)
        if "api.coingecko.com" in url:
            return COINGECKO_JSON
        if "open.er-api.com" in url:
            return ER_JSON
        if "geocoding-api.open-meteo.com" in url:
            return GEO_JSON
        if "api.open-meteo.com" in url:
            return FORECAST_JSON
        raise AssertionError(f"неожиданный URL: {url}")

    return fake



def make_loop(router: ScriptedLLM, registry: SkillRegistry) -> WakeLoop:
    """WakeLoop без звука: реальный Planner поверх мок-Router."""
    loop = WakeLoop(
        settings=get_settings(),
        router=router,  # type: ignore[arg-type]
        capture=FakeCapture(),  # type: ignore[arg-type]
        vad=FakeVad(),  # type: ignore[arg-type]
        recorder=FakeRecorder(),  # type: ignore[arg-type]
        speak_fn=lambda text: None,
        session=None,
    )
    loop.skills = registry
    loop.planner = Planner(registry=registry, router=router, gaps=loop.gaps)
    return loop


def main() -> int:
    setup_logging()
    print("=== SMOKE reasoning: сущности, поправки, композиция ===")
    TMP.mkdir(parents=True, exist_ok=True)
    ok = True
    registry = SkillRegistry(
        skills_dir=PROJECT_ROOT / "skills",
        installed_dir=TMP / "reasoning_installed",
        db_path=TMP / "reasoning_skills.db",
    )
    registry.scan()

    print("\n--- 0) сущности без LLM ---")
    crypto = resolve(None, "курс биткоина к доллару")
    print(f"    resolve('курс биткоина к доллару') = {crypto}")
    ok &= check(
        "биткоин -> crypto, а не currency",
        crypto is not None
        and crypto.get("kind") == "crypto"
        and crypto.get("coin") == "bitcoin",
        str(crypto),
    )
    ok &= check("незнакомая монета -> None", resolve(None, "курс шмэкоина") is None)
    general = resolve(None, "какой курс валют")
    ok &= check(
        "общий вопрос -> дефолтная пара USD/RUB",
        general is not None
        and general.get("base") == "USD"
        and general.get("quote") == "RUB",
        str(general),
    )

    urls: list[str] = []
    original_request = broker_module.SkillContext._request_text
    broker_module.SkillContext._request_text = make_http(urls)  # type: ignore[assignment]
    original_search = search_mod.search_web
    search_mod.search_web = (  # type: ignore[assignment]
        lambda query, max_results=3, timeout=None, **kw: [
            {
                "title": "Курс биткоина",
                "url": "https://example.test/btc",
                "snippet": "BTC торгуется около 91 200 долларов",
            }
        ]
    )
    router = ScriptedLLM()
    try:
        print("\n--- 1) «курс биткоина к доллару» ---")
        loop = make_loop(router, registry)
        first, first_route = loop.handle_command(
            "курс биткоина к доллару", source="text", speak=False
        )
        coingecko_urls = [url for url in urls if "api.coingecko.com" in url]
        print(f"    route={first_route}, ответ={first!r}")
        print(f"    coingecko: {coingecko_urls[:1]}")
        ok &= check("маршрут skill", first_route == "skill", first_route)
        ok &= check("CoinGecko вызван", bool(coingecko_urls), str(len(coingecko_urls)))
        ok &= check(
            "запрос ids=bitcoin и vs_currencies=usd",
            bool(coingecko_urls)
            and "ids=bitcoin" in coingecko_urls[0]
            and "vs_currencies=usd" in coingecko_urls[0],
            coingecko_urls[0] if coingecko_urls else "нет",
        )
        ok &= check("в ответе число биткоина", "91 234" in first, first)
        ok &= check(
            "нет шаблона «доллара к рублю»",
            "доллара к рублю" not in first.lower(),
            first,
        )
        ok &= check(
            "ответ сформулирован моделью по данным",
            any(
                call["task_type"] == "compose"
                and "используя ТОЛЬКО данные" in call["system"]
                and '"coin": "bitcoin"' in call["system"]
                for call in router.calls
            ),
        )

        print("\n--- 2) поправка: одинаковый ответ -> guard search/strong ---")
        second, second_route = loop.handle_command(
            "я просил биткоина а не доллара", source="text", speak=False
        )
        strong_calls = [call for call in router.calls if call["task_type"] == "strong"]
        print(f"    route={second_route}, ответ={second!r}, strong={len(strong_calls)}")
        ok &= check("маршрут переключён на search", second_route == "search", second_route)
        ok &= check("сводка ушла на тир strong", bool(strong_calls), str(len(strong_calls)))
        ok &= check("ответ отличается от прежнего", second != first, second)
        try:
            loop.stop()
        except Exception:  # noqa: BLE001 - тестовые фейки
            pass


        print("\n--- 3) незнакомая монета -> search ---")
        loop2 = make_loop(router, registry)
        urls.clear()
        unknown_answer, unknown_route = loop2.handle_command(
            "курс шмэкоина", source="text", speak=False
        )
        print(f"    route={unknown_route}, ответ={unknown_answer!r}, urls={urls}")
        ok &= check("факты не отвечают дефолтом", unknown_route == "search", unknown_route)
        ok &= check(
            "CoinGecko не вызывался с выдуманной монетой",
            not any("shmecoin" in url for url in urls),
            str(urls),
        )
        try:
            loop2.stop()
        except Exception:  # noqa: BLE001 - тестовые фейки
            pass

        print("\n--- 4) planner без fast-триггеров на «курс ...» ---")
        plan = Planner(registry=registry, router=router).plan("курс доллара к рублю")
        name = getattr(plan.get("skill"), "name", None)
        print(f"    skill={name}, source={plan.get('source')}, args={plan.get('args')}")
        ok &= check("маршрут через LLM (source=llm)", plan.get("source") == "llm")
        ok &= check("умение facts выбрано LLM", name == "facts", str(name))
        args = plan.get("args") or {}
        ok &= check(
            "args содержат сущность валюты",
            args.get("kind") == "currency" and args.get("base") == "USD",
            str(args),
        )

        print("\n--- 5) config: chat/intent -> cheap ---")
        routing = (load_config().model_dump().get("brain") or {}).get("routing") or {}
        print(f"    routing={routing}")
        ok &= check("chat = cheap", routing.get("chat") == "cheap", str(routing.get("chat")))
        ok &= check(
            "intent = cheap", routing.get("intent") == "cheap", str(routing.get("intent"))
        )

        print("\n--- 6) погода: факт по названному городу ---")
        loop3 = make_loop(router, registry)
        weather_answer, weather_route = loop3.handle_command(
            "погода в Новосибирске", source="text", speak=False
        )
        print(f"    route={weather_route}, ответ={weather_answer!r}")
        ok &= check("маршрут skill", weather_route == "skill", weather_route)
        ok &= check("город в ответе", "Новосибирск" in weather_answer, weather_answer)
        try:
            loop3.stop()
        except Exception:  # noqa: BLE001 - тестовые фейки
            pass
    finally:
        search_mod.search_web = original_search  # type: ignore[assignment]
        broker_module.SkillContext._request_text = original_request  # type: ignore[assignment]

    print()
    print("PASS" if ok else "FAIL — скопируй весь вывод и пришли в чат")
    return 0 if ok else 1


if __name__ == "__main__":
    raise SystemExit(main())
