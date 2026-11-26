"""Смоук шага cost: озвучка по источнику, дешёвая композиция, лимиты, кэш.

Запуск:
    python tests/smoke_cost.py

Проверяется (моки Router/TTS/prefs):
  1) source=text + text_speaks=false -> TTS не вызван, cost_breakdown.tts=0;
  2) source=voice -> TTS вызван всегда;
  3) тумблер set_text_speaks меняет prefs и следующий turn;
  4) compose -> task_type=compose, max_tokens=120, тир cheap;
  5) planner max_tokens=200, IN промпта < 800 токенов (len/4);
  6) повторный ответ скилла -> TTS из кэша (синтез 0);
  7) turn дороже per_turn_warn_rub -> WARNING.
"""

from __future__ import annotations

import json
import sys
import time
from pathlib import Path
from typing import Any

PROJECT_ROOT = Path(__file__).resolve().parents[1]
if str(PROJECT_ROOT) not in sys.path:
    sys.path.insert(0, str(PROJECT_ROOT))

import kivi.prefs as prefs_module
import kivi.tts.cache as cache_module
import kivi.wake.loop as loop_module
from kivi.api.server import ApiServer
from kivi.brain.router import Router
from kivi.config import get_settings, load_config
from kivi.log import setup_logging
from kivi.skills import facts_compose
from kivi.skills.llm_planner import plan_with_llm
from kivi.skills.loader import invoke
from kivi.skills.registry import SkillRegistry
from kivi.wake.loop import WakeLoop

TMP = PROJECT_ROOT / "data" / "tmp"
PREFS = TMP / "cost_prefs.json"


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


class FakeSpeaker:
    """Заглушка SentenceSpeaker: факт создания = TTS был вызван."""

    instances: list["FakeSpeaker"] = []

    def __init__(self, *args: Any, context: str = "think", **kwargs: Any) -> None:
        self.context = context
        self.fed: list[str] = []
        self.finished = False
        FakeSpeaker.instances.append(self)

    def feed(self, chunk: str) -> None:
        self.fed.append(str(chunk))

    def finish(self) -> float:
        self.finished = True
        return 0.0

    def stop(self) -> None:
        return None

    @property
    def first_sound_ms(self) -> int:
        return 0


class FakePlan:
    """Планировщик-заглушка: chat без LLM."""

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
    def month_spent_rub(self) -> float:
        return 1.23

    def describe(self) -> str:
        return "1.23 руб"


class FakeRouter:
    """Заглушка Router: пишет вызовы (task_type, max_tokens) и отдаёт текст."""

    def __init__(self, text: str = "Ответ заглушки.") -> None:
        self.text = text
        self.calls: list[dict[str, Any]] = []
        self.budget = FakeBudget()

    def complete(self, messages: Any, task_type: str = "chat", **kwargs: Any) -> dict:
        self.calls.append(
            {
                "task_type": task_type,
                "max_tokens": kwargs.get("max_tokens"),
                "messages": messages,
            }
        )
        return {"text": self.text, "cost_rub": 0.01, "model": "fake"}


class FakePlanRouter:
    """Заглушка Router для planning-промпта: отдаёт JSON-план."""

    def __init__(self, payload: dict[str, Any]) -> None:
        self.payload = payload
        self.calls: list[dict[str, Any]] = []

    def complete(self, messages: Any, task_type: str = "intent", **kwargs: Any) -> dict:
        self.calls.append(
            {
                "task_type": task_type,
                "max_tokens": kwargs.get("max_tokens"),
                "messages": messages,
            }
        )
        return {
            "text": json.dumps(self.payload, ensure_ascii=False),
            "cost_rub": 0.002,
            "model": "fake",
        }


class FakeSynth:
    """Заглушка synthesize_mp3: считает синтезы и пишет файл."""

    def __init__(self) -> None:
        self.count = 0

    def __call__(self, text: str, **kwargs: Any) -> Path:
        self.count += 1
        path = TMP / f"cost_synth_{self.count}.mp3"
        path.write_bytes(b"ID3" + b"\x00" * 128)
        return path


def make_loop(router: FakeRouter, registry: SkillRegistry, spoken: list[str]) -> WakeLoop:
    loop = WakeLoop(
        settings=get_settings(),
        router=router,  # type: ignore[arg-type]
        capture=FakeCapture(),  # type: ignore[arg-type]
        vad=FakeVad(),  # type: ignore[arg-type]
        recorder=FakeRecorder(),  # type: ignore[arg-type]
        speak_fn=lambda text: spoken.append(text),
        session=None,
    )
    loop.skills = registry
    loop.planner = FakePlan()  # type: ignore[assignment]
    return loop



def main() -> int:
    setup_logging()
    print("=== SMOKE cost: озвучка, композиция, лимиты, кэш ===")
    TMP.mkdir(parents=True, exist_ok=True)
    ok = True

    original_speaker = loop_module.SentenceSpeaker
    original_prefs_path = prefs_module.PREFS_PATH
    original_synth = cache_module.synthesize_mp3
    original_play = cache_module.play_file
    original_cache_dir = cache_module.SKILLS_CACHE_DIR
    loop_module.SentenceSpeaker = FakeSpeaker  # type: ignore[assignment]
    prefs_module.PREFS_PATH = PREFS
    PREFS.write_text("{}", encoding="utf-8")
    prefs_module.reload_prefs()
    synth = FakeSynth()
    try:
        registry = SkillRegistry(
            skills_dir=PROJECT_ROOT / "skills",
            installed_dir=TMP / "cost_installed",
            db_path=TMP / "cost_skills.db",
        )
        registry.scan()
        router = FakeRouter("Ответ по существу.")
        spoken: list[str] = []
        loop = make_loop(router, registry, spoken)
        api = ApiServer(port=0, config={"text_speaks": False}, core=loop)
        api.set_text_speaks(False)

        print("\n--- 1) source=text + text_speaks=false ---")
        FakeSpeaker.instances.clear()
        with LogCatcher() as logs:
            reply, route = loop.handle_command("расскажи что-нибудь", source="text")
        print(f"    ответ={reply!r}, route={route}, cost={loop._cost}")
        ok &= check("ответ получен", bool(reply), reply)
        ok &= check("TTS не вызван", FakeSpeaker.instances == [], str(FakeSpeaker.instances))
        ok &= check("cost_breakdown.tts = 0", loop._cost.get("tts") == 0.0, str(loop._cost))
        ok &= check("в логе есть cost_breakdown", logs.has("cost_breakdown={"))
        ok &= check(
            "chat max_tokens=600",
            bool(router.calls) and router.calls[-1]["max_tokens"] == 600,
            str(router.calls[-1]["max_tokens"] if router.calls else None),
        )

        print("\n--- 2) source=voice -> TTS всегда ---")
        FakeSpeaker.instances.clear()
        loop.handle_command("расскажи что-нибудь", source="voice")
        ok &= check(
            "voice: TTS вызван",
            len(FakeSpeaker.instances) == 1,
            str(len(FakeSpeaker.instances)),
        )

        print("\n--- 3) тумблер set_text_speaks ---")
        flag = api.set_text_speaks(True)
        prefs_data = json.loads(PREFS.read_text(encoding="utf-8"))
        print(f"    reply={flag}, prefs={prefs_data}")
        ok &= check("prefs.text_speaks=true", prefs_data.get("text_speaks") is True)
        FakeSpeaker.instances.clear()
        loop.handle_command("расскажи что-нибудь", source="text")
        ok &= check("после включения text озвучен", len(FakeSpeaker.instances) == 1)
        api.set_text_speaks(False)
        FakeSpeaker.instances.clear()
        loop.handle_command("расскажи что-нибудь", source="text")
        ok &= check("после выключения text молчит", FakeSpeaker.instances == [])

        print("\n--- 4) композиция facts на дешёвом тире ---")
        payload = {
            "source": "coingecko",
            "kind": "crypto",
            "coin": "bitcoin",
            "name": "биткоин",
            "values": {"usd": 91234.5, "rub": 8900000},
            "updated": "2026-11-26T15:00+07:00",
        }
        compose_router = FakeRouter("Биткоин стоит 91 234 доллара.")
        text, cost = facts_compose.compose_answer(
            "курс биткоина к рублю", payload, [], router=compose_router
        )
        call = compose_router.calls[-1]
        print(
            f"    текст={text!r}, task={call['task_type']}, max_tokens={call['max_tokens']}"
        )
        ok &= check("task_type=compose", call["task_type"] == "compose")
        ok &= check("max_tokens=120", call["max_tokens"] == 120)
        routing = Router(get_settings()).routing
        ok &= check("compose -> cheap", routing.get("compose") == "cheap", str(routing))
        ok &= check(
            "chat/intent -> cheap (эскалация для сложного)",
            routing.get("chat") == "cheap" and routing.get("intent") == "cheap",
            str(routing),
        )
        ok &= check("compose вернул текст", bool(text) and cost >= 0.0)

        print("\n--- 5) планировщик: max_tokens и компактный промпт ---")
        plan_router = FakePlanRouter(
            {
                "skill_name": None,
                "args": {},
                "route": "chat",
                "confidence": 0.9,
                "missing": [],
            }
        )
        plan_with_llm("что нового", registry=registry, router=plan_router)
        plan_call = plan_router.calls[-1]
        system = "".join(
            str(item.get("content") or "")
            for item in plan_call["messages"]
            if isinstance(item, dict) and item.get("role") == "system"
        )
        tokens_est = len(system) / 4.0
        print(f"    max_tokens={plan_call['max_tokens']}, IN≈{tokens_est:.0f} токенов")
        ok &= check("planner max_tokens=200", plan_call["max_tokens"] == 200)
        ok &= check("IN промпта < 800 токенов", tokens_est < 800, f"{tokens_est:.0f}")
        ok &= check("история только пара реплик", "ПОСЛЕДНИЕ РЕПЛИКИ" in system)
        ok &= check("каталог без описаний", "triggers:" not in system)

        print("\n--- 6) кэш TTS ответов скиллов (sha256, без лимита) ---")
        cache_module.SKILLS_CACHE_DIR = TMP / f"cost_skills_cache_{int(time.time() * 1000)}"
        cache_module.synthesize_mp3 = synth  # type: ignore[assignment]
        cache_module.play_file = lambda path, **kw: 0.2  # type: ignore[assignment]
        capabilities = registry.get("capabilities")
        ok &= check("скилл capabilities найден", capabilities is not None)
        first_text = invoke(capabilities, {}, registry=registry)  # type: ignore[arg-type]
        synth.count = 0
        cache_module.speak_skill_cached(first_text)
        cache_module.speak_skill_cached(first_text)
        cached_files = list(cache_module.SKILLS_CACHE_DIR.glob("*.mp3"))
        print(f"    синтезов={synth.count}, файлов в кэше={len(cached_files)}")
        ok &= check("повторный ответ: синтез 0 (кэш)", synth.count == 1, str(synth.count))
        ok &= check("кэш лежит в data/tts_cache/skills", len(cached_files) == 1)

        print("\n--- 7) WARNING при дорогом turn ---")
        budget_cfg = (load_config().model_dump().get("brain") or {}).get("budget") or {}
        ok &= check(
            "config per_turn_warn_rub = 0.5",
            float(budget_cfg.get("per_turn_warn_rub") or 0) == 0.5,
            str(budget_cfg.get("per_turn_warn_rub")),
        )
        loop._planner_cost_rub = 0.6
        loop._cost = {key: 0.0 for key in loop._cost}
        loop._cost["planner"] = 0.6
        with LogCatcher() as logs:
            loop._log_turn("text", "skill", time.perf_counter(), "дорогой ответ", {})
        ok &= check("WARNING per_turn_warn", logs.has("per_turn_warn"))
    finally:
        loop_module.SentenceSpeaker = original_speaker  # type: ignore[assignment]
        prefs_module.PREFS_PATH = original_prefs_path
        cache_module.synthesize_mp3 = original_synth  # type: ignore[assignment]
        cache_module.play_file = original_play  # type: ignore[assignment]
        cache_module.SKILLS_CACHE_DIR = original_cache_dir
        try:
            prefs_module.reload_prefs()
        except Exception:  # noqa: BLE001 - тестовые файлы
            pass
        try:
            loop.stop()  # type: ignore[possibly-undefined]
        except Exception:  # noqa: BLE001 - тестовые фейки
            pass

    print()
    print("PASS" if ok else "FAIL — скопируй весь вывод и пришли в чат")
    return 0 if ok else 1


if __name__ == "__main__":
    raise SystemExit(main())

