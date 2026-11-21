"""Смоук шага 1.10: маршрут ответа (ровно одна озвучка) + библиотека вводных.

Запуск:
    python tests/smoke_flow.py

Что проверяется:
  1) planner: анекдот -> route=chat и пробел НЕ записан;
  2) planner: курс -> route=search, без пробела;
  3) planner: «включи свет» -> route=action_gap, пробел записан;
  4) loop: search -> ровно одна озвучка, Router.chat вторым не вызывается;
  5) loop: пустой поиск -> одна короткая реплика + пробел в журнале;
  6) loop: action_gap без частоты -> одна короткая реплика, без Router;
  7) fillers: ensure_library создаёт >=4 фраз (мок TTS), pick не повторяет
     подряд, все фразы заканчиваются . или !, кэп 12, пустая библиотека -> None.

Весь LLM и TTS — заглушки (0 руб). Микрофон не нужен.
"""

from __future__ import annotations

import json
import sys
import uuid
from pathlib import Path
from typing import Any

PROJECT_ROOT = Path(__file__).resolve().parents[1]
if str(PROJECT_ROOT) not in sys.path:
    sys.path.insert(0, str(PROJECT_ROOT))

import kivi.skills.search as search_module
import kivi.wake.loop as loop_module
from kivi.brain.session import Session
from kivi.config import get_settings
from kivi.log import setup_logging
from kivi.skills.gaps import GapLog
from kivi.skills.planner import Planner
from kivi.skills.registry import get_registry
from kivi.tts import fillers
from kivi.wake.loop import WakeLoop

TMP = PROJECT_ROOT / "data" / "tmp"
GAPS_DB = TMP / "flow_smoke.db"
SESSION_DB = TMP / "flow_session.db"


def check(label: str, condition: bool, detail: str = "") -> bool:
    mark = "OK  " if condition else "X   "
    print(f"{mark} {label}" + (f" — {detail}" if detail else ""))
    return bool(condition)


class ScriptedRouter:
    """Заглушка Router: отдаёт заданный текст, помнит task_type."""

    def __init__(self, replies: list[str]) -> None:
        self.replies = list(replies)
        self.calls: list[str] = []

    def complete(
        self,
        messages: Any,
        task_type: str = "chat",
        verify: Any = None,
    ) -> dict[str, Any]:
        self.calls.append(task_type)
        text = self.replies.pop(0) if len(self.replies) > 1 else self.replies[0]
        return {"text": text, "cost_rub": 0.0, "model": "scripted", "ok": True}


class LoopRouter:
    """Заглушка Router для loop: считает вызовы, умеет стрим."""

    def __init__(self, text: str = "обычный ответ") -> None:
        self.text = text
        self.calls: list[str] = []

    def complete(self, messages: Any, task_type: str = "chat", verify: Any = None):
        self.calls.append(f"complete:{task_type}")
        return {"text": self.text, "cost_rub": 0.0, "model": "fake", "ok": True}

    def complete_stream(self, messages: Any, task_type: str = "chat"):
        self.calls.append(f"stream:{task_type}")
        yield self.text


class FakeSpeaker:
    """Заглушка SentenceSpeaker: запоминает контекст, текст и finish."""

    instances: list["FakeSpeaker"] = []

    def __init__(self, started_at: float | None = None, context: str = "think", **kwargs: Any) -> None:
        self.context = context
        self.started_at = started_at
        self.fed: list[str] = []
        self.finished = 0
        self.stopped = 0
        FakeSpeaker.instances.append(self)

    def feed(self, text: str) -> None:
        self.fed.append(text)

    def finish(self) -> float:
        self.finished += 1
        return 0.0

    def stop(self) -> None:
        self.stopped += 1


class FakePlanner:
    """Планировщик с заранее заданным планом."""

    def __init__(self, plan: dict[str, Any]) -> None:
        self.plan_value = dict(plan)

    def plan(self, command: str, history: Any = None) -> dict[str, Any]:
        return dict(self.plan_value)


def make_gaps() -> GapLog:
    gaps = GapLog(GAPS_DB)
    with gaps._connect() as connection, connection:
        connection.execute("DELETE FROM capability_gaps")
    return gaps


def gap_count(gaps: GapLog) -> int:
    return len(gaps.list_recent(limit=100))


CHAT_JSON = '{"skill_name": null, "args": {}, "route": "chat", "missing": []}'
SEARCH_JSON = '{"skill_name": null, "args": {}, "route": "search", "missing": []}'
ACTION_JSON = (
    '{"skill_name": null, "args": {}, "route": "action_gap", '
    '"missing": [{"capability": "управление светом", "why": "нет умения умного дома"}]}'
)
UNKNOWN_ROUTE_JSON = '{"skill_name": null, "args": {}, "route": "weather", "missing": []}'


def main() -> int:
    setup_logging()
    print("=== SMOKE 1.10: маршрут ответа + библиотека вводных ===")
    TMP.mkdir(parents=True, exist_ok=True)
    ok = True

    print("\n--- 1) planner: route ---")
    gaps = make_gaps()
    registry = get_registry()
    router = ScriptedRouter([CHAT_JSON, SEARCH_JSON, ACTION_JSON, UNKNOWN_ROUTE_JSON])
    planner = Planner(registry, router=router, gaps=gaps, strategy="llm_with_fallback")

    before = gap_count(gaps)
    plan_chat = planner.plan("расскажи анекдот")
    after = gap_count(gaps)
    print(
        f"    chat: route={plan_chat.get('route')}, gap_id={plan_chat.get('gap_id')}, "
        f"пробелов {before}->{after}"
    )
    ok &= check("анекдот -> route=chat", plan_chat.get("route") == "chat")
    ok &= check("пробел НЕ записан", after == before and plan_chat.get("gap_id") is None)
    ok &= check("missing пустой", plan_chat.get("missing") == [])

    plan_search = planner.plan("какой курс биткоина")
    print(
        f"    search: route={plan_search.get('route')}, gap_id={plan_search.get('gap_id')}"
    )
    ok &= check("курс -> route=search", plan_search.get("route") == "search")
    ok &= check("search не пишет пробел", plan_search.get("gap_id") is None)

    plan_action = planner.plan("включи свет")
    print(
        f"    action_gap: route={plan_action.get('route')}, "
        f"gap_id={plan_action.get('gap_id')}, missing={plan_action.get('missing')}"
    )
    ok &= check("включи свет -> route=action_gap", plan_action.get("route") == "action_gap")
    ok &= check(
        "action_gap записал пробел",
        bool(plan_action.get("gap_id")) and gap_count(gaps) == after + 1,
    )

    plan_unknown = planner.plan("какая-то непонятная команда")
    print(f"    неизвестный route -> {plan_unknown.get('route')}")
    ok &= check("неизвестный route -> chat", plan_unknown.get("route") == "chat")

    print("\n--- 2) loop: search -> ровно одна озвучка ---")
    try:
        settings = get_settings()
    except Exception as exc:  # noqa: BLE001 - без устройств смоук не имеет смысла
        print(f"FAIL — конфиг/устройства недоступны: {exc}")
        return 1

    original_speaker = loop_module.SentenceSpeaker
    loop_module.SentenceSpeaker = FakeSpeaker  # type: ignore[assignment]
    spoken: list[str] = []
    loop = WakeLoop(
        settings=settings,
        router=LoopRouter(),
        speak_fn=spoken.append,
        session=Session(db_path=SESSION_DB),
    )
    loop.gaps = make_gaps()
    loop.planner = FakePlanner(
        {
            "skill": None,
            "args": {},
            "missing": [],
            "source": "llm",
            "route": "search",
            "gap_id": None,
        }
    )

    original_search = search_module.search_web
    original_summary = search_module.summarize_with_llm
    summary_text = "Курс биткоина около девяноста тысяч долларов."
    try:
        search_module.search_web = lambda query, max_results=3, timeout=None, **kw: [
            {"title": "Курс", "url": "https://example.com", "snippet": "90000"}
        ]
        search_module.summarize_with_llm = (
            lambda query, results, router=None, **kw: summary_text
        )

        FakeSpeaker.instances.clear()
        loop._think_and_speak("какой курс биткоина")
        speaker = FakeSpeaker.instances[-1] if FakeSpeaker.instances else None
        print(
            f"    speaker: context={getattr(speaker, 'context', None)}, "
            f"fed={getattr(speaker, 'fed', None)}"
        )
        ok &= check("создан ровно один speaker", len(FakeSpeaker.instances) == 1)
        ok &= check("контекст search", speaker is not None and speaker.context == "search")
        ok &= check(
            "одна реплика в speaker",
            speaker is not None and speaker.fed == [summary_text],
            str(len(speaker.fed)) if speaker else "нет",
        )
        ok &= check(
            "Router.chat не вызывался вторым",
            loop.router.calls == [],
            str(loop.router.calls),
        )
        ok &= check("speak_fn не дублирует озвучку", spoken == [], str(spoken))

        print("\n--- 3) loop: пустой поиск -> одна короткая реплика + пробел ---")
        loop.gaps = make_gaps()
        search_module.search_web = lambda query, max_results=3, timeout=None, **kw: []
        FakeSpeaker.instances.clear()
        loop._think_and_speak("какой курс биткоина")
        speaker = FakeSpeaker.instances[-1]
        rows = loop.gaps.list_recent(limit=10)
        print(f"    сказано: {speaker.fed}, пробелов: {len(rows)}")
        ok &= check(
            "одна реплика про свежие данные",
            speaker.fed == ["Не нашла свежих данных по этому вопросу."],
            str(speaker.fed),
        )
        ok &= check(
            "пробел записан",
            any(row["command"] == "какой курс биткоина" for row in rows),
            str(len(rows)),
        )
        ok &= check("Router не вызывался и здесь", loop.router.calls == [])

        print("\n--- 4) loop: action_gap -> одна реплика без Router ---")
        loop.gaps = make_gaps()
        gap_id = loop.gaps.record_gap("включи свет", user_intent="управление светом")
        loop.planner = FakePlanner(
            {
                "skill": None,
                "args": {},
                "missing": [
                    {"capability": "управление светом", "why": "нет умения"}
                ],
                "source": "llm",
                "route": "action_gap",
                "gap_id": gap_id,
            }
        )
        FakeSpeaker.instances.clear()
        loop._think_and_speak("включи свет")
        speaker = FakeSpeaker.instances[-1]
        print(f"    сказано: {speaker.fed}")
        ok &= check(
            "одна короткая честная реплика",
            len(speaker.fed) == 1 and "Пока не могу" in speaker.fed[0],
            str(speaker.fed),
        )
        ok &= check("Router не вызывался", loop.router.calls == [])
    finally:
        search_module.search_web = original_search
        search_module.summarize_with_llm = original_summary
        loop_module.SentenceSpeaker = original_speaker  # type: ignore[assignment]

    print("\n--- 5) fillers: ensure_library / pick ---")
    temp_root = TMP / f"fillers_smoke_{uuid.uuid4().hex[:8]}"
    original_dir = fillers.FILLERS_DIR
    original_last = dict(fillers._LAST)
    fillers.FILLERS_DIR = temp_root
    fillers.reset_state()
    synth_calls: list[str] = []

    def fake_synth(text: str) -> Path:
        synth_calls.append(text)
        raw_dir = temp_root / "_raw"
        raw_dir.mkdir(parents=True, exist_ok=True)
        path = raw_dir / f"{uuid.uuid4().hex[:12]}.mp3"
        path.write_bytes(b"ID3fake:" + text.encode("utf-8"))
        return path

    phrases_json = json.dumps(
        {
            "phrases": [
                "Дай-ка подумать.",
                "Секундочку.",
                "Сейчас соображу!",
                "Момент, пожалуйста.",
                "Уже думаю.",
                "Почти готова.",
            ]
        },
        ensure_ascii=False,
    )

    class CapturingRouter(ScriptedRouter):
        def __init__(self, replies: list[str]) -> None:
            super().__init__(replies)
            self.messages: list[Any] = []

        def complete(
            self, messages: Any, task_type: str = "chat", verify: Any = None
        ) -> dict[str, Any]:
            self.messages.append((task_type, messages))
            return super().complete(messages, task_type, verify)

    original_ask = fillers._ask_llm
    try:
        for context in fillers.CONTEXTS:
            capture = CapturingRouter([phrases_json])
            library = fillers.ensure_library(
                context, min_count=4, router=capture, synth=fake_synth
            )
            print(f"    {context}: {len(library)} фраз — {library[:3]}")
            ok &= check(f"{context}: >= 4 фраз", len(library) >= 4, str(len(library)))
            ok &= check(
                f"{context}: все фразы с . или !",
                all(item.rstrip().endswith((".", "!")) for item in library),
            )
            ok &= check(
                f"{context}: mp3 на каждую фразу",
                all(fillers.audio_path(context, item) is not None for item in library),
            )
            ok &= check(
                f"{context}: index.json есть",
                (temp_root / context / "index.json").is_file(),
            )
            ok &= check(
                f"{context}: task_type=chat",
                bool(capture.calls) and capture.calls[0] == "chat",
            )
            prompt = (
                str(capture.messages[0][1][0]["content"]) if capture.messages else ""
            )
            ok &= check(
                f"{context}: контекст в промпте", f"Контекст: {context}" in prompt
            )
            if context == "search":
                ok &= check(
                    "search: примеры тона в промпте",
                    "Уже ищу информацию." in prompt,
                )

        calls_snapshot = len(synth_calls)
        think_router = ScriptedRouter([phrases_json])
        again = fillers.ensure_library(
            "think", min_count=4, router=think_router, synth=fake_synth
        )
        ok &= check(
            "повторный ensure без LLM и синтеза",
            len(again) >= 4
            and len(think_router.calls) == 0
            and len(synth_calls) == calls_snapshot,
        )

        played: list[str] = []
        fillers.reset_state("think")
        for _ in range(10):
            phrase = fillers.pick("think")
            if phrase is None:
                break
            played.append(phrase)
            fillers.mark_played("think", phrase)
        ok &= check(
            "pick не повторяет подряд",
            len(played) == 10
            and all(a != b for a, b in zip(played, played[1:])),
            str(len(played)),
        )
        ok &= check(
            "pick только из библиотеки",
            all(item in fillers.phrases("think") for item in played),
        )

        many = json.dumps(
            {"phrases": [f"Фраза номер {i}." for i in range(1, 16)]},
            ensure_ascii=False,
        )
        fillers.FILLERS_DIR = temp_root / "cap"
        cap_library = fillers.ensure_library(
            "code", min_count=4, router=ScriptedRouter([many]), synth=fake_synth
        )
        ok &= check("кэп 12 фраз", len(cap_library) <= 12, str(len(cap_library)))

        fillers.FILLERS_DIR = temp_root / "empty"
        fillers._ask_llm = lambda context, router=None: []
        ok &= check("пустая библиотека -> pick None", fillers.pick("search") is None)
    finally:
        fillers._ask_llm = original_ask
        fillers.FILLERS_DIR = original_dir
        fillers._LAST.clear()
        fillers._LAST.update(original_last)

    print()
    print("PASS" if ok else "FAIL — скопируй весь вывод и пришли в чат")
    return 0 if ok else 1


if __name__ == "__main__":
    raise SystemExit(main())
