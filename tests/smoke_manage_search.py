"""Смоук четырёх умений: listen_control, memory_manage, capabilities, search."""
from __future__ import annotations

import json
import sys
import uuid
from pathlib import Path
from types import SimpleNamespace
from typing import Any

PROJECT_ROOT = Path(__file__).resolve().parents[1]
if str(PROJECT_ROOT) not in sys.path:
    sys.path.insert(0, str(PROJECT_ROOT))

import kivi.skills.search as search_mod
import kivi.wake.loop as loop_module
from kivi.config import get_settings
from kivi.log import setup_logging
from kivi.skills.broker import SkillContext, set_overlay_service
from kivi.skills.gaps import GapLog
from kivi.skills.loader import load
from kivi.skills.planner import Planner
from kivi.skills.registry import SkillRegistry
from kivi.wake.loop import WakeLoop

TMP = PROJECT_ROOT / "data" / "tmp"


def check(label: str, condition: bool, detail: str = "") -> bool:
    print(("OK  " if condition else "X   ") + label + (f" — {detail}" if detail else ""))
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


class FakePlanner:
    def __init__(self, registry: SkillRegistry) -> None:
        self.registry = registry

    def plan(self, command: str, history: Any = None) -> dict[str, Any]:
        lower = command.lower().replace("ё", "е")
        if "прослуш" in lower or "не слушай" in lower or "слушай снова" in lower or "снова слушай" in lower:
            action = "resume" if any(word in lower for word in ("включи", "снова", "возобнов")) else "pause"
            return _skill_plan(self.registry.get("listen_control"), {"action": action})
        if "что ты умеешь" in lower or "список умений" in lower:
            return _skill_plan(self.registry.get("capabilities"), {})
        return {
            "skill": None,
            "args": {},
            "missing": [],
            "source": "test",
            "route": "chat",
            "gap_id": None,
        }


def _skill_plan(manifest: Any, args: dict[str, Any]) -> dict[str, Any]:
    return {
        "skill": manifest,
        "args": args,
        "missing": [],
        "source": "test",
        "route": "skill",
        "gap_id": None,
    }



class ManageCtx:
    def __init__(self, files: dict[str, str], confirm: bool = True) -> None:
        self.files = dict(files)
        self.confirm_value = confirm
        self.confirm_calls = 0
        self.reminders_clear_calls = 0

    def fs_read(self, path: str) -> str:
        key = str(path).replace("\\", "/")
        if key not in self.files:
            raise FileNotFoundError(key)
        return self.files[key]

    def fs_write(self, path: str, text: str) -> str:
        key = str(path).replace("\\", "/")
        self.files[key] = str(text)
        return key

    def confirm(self, title: str, body: str = "", details: str = "") -> bool:
        self.confirm_calls += 1
        return bool(self.confirm_value)

    def reminders_clear(self) -> int:
        self.reminders_clear_calls += 1
        return 2

    def log(self, _text: str) -> None:
        return None


class FakeCapCtx:
    def skills_list(self):
        return [
            {"name": "time_date", "description": "время", "category": "data"},
            {"name": "file_search", "description": "файлы", "category": "files"},
            {"name": "remember", "description": "память", "category": "data"},
        ]


class FakeDDGS:
    """api падает, html возвращает результат, lite — пусто."""

    constructors = 0
    backend_calls: list[str] = []

    def __init__(self, timeout: Any = None, **kwargs: Any) -> None:
        FakeDDGS.constructors += 1

    def __enter__(self) -> "FakeDDGS":
        return self

    def __exit__(self, *exc_info: Any) -> bool:
        return False

    def text(self, query: str, max_results: int | None = None, backend: str = "api"):
        FakeDDGS.backend_calls.append(backend)
        if backend == "api":
            raise RuntimeError("api backend down")
        if backend == "html":
            return [
                {"title": "Курс", "href": "https://example.test/course", "body": "90000"}
            ]
        return []


class FailDDGS:
    def __init__(self, timeout: Any = None, **kwargs: Any) -> None:
        raise RuntimeError("DDGS down")


class FakeSearchRouter:
    def complete(self, messages: Any, task_type: str = "chat", **kwargs: Any):
        return {"text": "курс биткоина", "cost_rub": 0.0}


class JsonRouter:
    """Мок Router для планировщика: всегда отдаёт заданный JSON."""

    def __init__(self, payload: dict[str, Any]) -> None:
        self.payload = payload

    def complete(self, messages: Any, task_type: str = "intent", **kwargs: Any):
        return {
            "text": json.dumps(self.payload, ensure_ascii=False),
            "cost_rub": 0.0,
            "ok": True,
        }


def main() -> int:
    setup_logging()
    print("=== SMOKE: listen_control / memory_manage / capabilities / search ===")
    TMP.mkdir(parents=True, exist_ok=True)
    ok = True
    token = uuid.uuid4().hex
    registry = SkillRegistry(
        skills_dir=PROJECT_ROOT / "skills",
        installed_dir=TMP / f"manage_installed_{token}",
        db_path=TMP / f"manage_skills_{token}.db",
    )
    registry.scan()
    manifests = {
        name: registry.get(name)
        for name in ("listen_control", "memory_manage", "capabilities")
    }
    ok &= check(
        "манифесты новых скиллов найдены",
        all(item is not None for item in manifests.values()),
        str({name: item is not None for name, item in manifests.items()}),
    )
    if not all(manifests.values()):
        print("FAIL — нет манифестов")
        return 1

    print("\n--- 1) listen_control / пауза и resume ---")
    settings = get_settings()
    spoken: list[str] = []
    loop = WakeLoop(
        settings=settings,
        router=None,
        capture=FakeCapture(),  # type: ignore[arg-type]
        vad=FakeVad(),  # type: ignore[arg-type]
        recorder=FakeRecorder(),  # type: ignore[arg-type]
        speak_fn=lambda text: spoken.append(text),
        session=None,
    )
    loop.think_fn = lambda command: f"Ответ на {command}"
    loop.planner = FakePlanner(loop.skills)  # type: ignore[arg-type]
    script: list[str] = []

    def fake_detect(text: str):
        lowered = text.lower()
        if "киви" in lowered:
            index = lowered.find("киви")
            command = text[index + 4 :].strip(" ,.!")
            return SimpleNamespace(is_wake=True, command=command, fuzzy=False)
        return SimpleNamespace(is_wake=False, command="", fuzzy=False)

    def fake_transcribe(_path: Any) -> str:
        return script.pop(0) if script else ""

    original_detect = loop_module.detect
    original_transcribe = loop_module.transcribe_wav
    loop_module.detect = fake_detect  # type: ignore[assignment]
    loop_module.transcribe_wav = fake_transcribe  # type: ignore[assignment]

    def feed(text: str) -> None:
        script.append(text)
        loop._handle_utterance(SimpleNamespace(path=Path("fake.wav"), speech_s=2.0))  # type: ignore[arg-type]

    try:
        feed("Киви, выруби прослушку")
        print("pause:", loop.paused, spoken[-1:])
        ok &= check("listen_control pause -> loop.paused True", loop.paused is True)
        ok &= check(
            "ответ паузы",
            spoken and "Хорошо, не слушаю" in spoken[-1],
            str(spoken[-1:]),
        )
        before = len(spoken)
        feed("Киви, который час")
        ok &= check(
            "прочая VAD-фраза в паузе игнорируется",
            len(spoken) == before and loop.paused is True,
            f"spoken={spoken[before:]}",
        )
        text_reply, text_route = loop.handle_command(
            "привет", source="text", speak=False
        )
        ok &= check(
            "текст/API работают в паузе",
            bool(text_reply) and text_route == "chat" and loop.paused is True,
            f"{text_reply!r}, paused={loop.paused}",
        )
        feed("Киви, слушай снова")
        print("resume:", loop.paused, spoken[-1:])
        ok &= check(
            "resume-команда в паузе обрабатывается",
            loop.paused is False and "Слушаю снова" in spoken[-1],
            f"paused={loop.paused}, spoken={spoken[-1:]}",
        )
    finally:
        loop_module.detect = original_detect  # type: ignore[assignment]
        loop_module.transcribe_wav = original_transcribe  # type: ignore[assignment]
        try:
            loop.stop()
        except Exception:  # noqa: BLE001 - тестовые фейки
            pass

    print("\n--- 2) memory_manage: confirm и очистка ---")
    manage = load(manifests["memory_manage"])  # type: ignore[arg-type]
    base_schedule = "# Расписание\n- [будни 15:00] стендап\n"
    ctx_yes = ManageCtx({"memory/schedule.md": base_schedule}, confirm=True)
    clear_reply = manage(ctx_yes, target="schedule", action="clear")
    print("clear reply:", clear_reply, "file:", repr(ctx_yes.files["memory/schedule.md"]))
    ok &= check(
        "clear schedule с confirm=True -> только заголовок",
        ctx_yes.files["memory/schedule.md"] == "# Расписание\n"
        and ctx_yes.confirm_calls == 1,
        repr(ctx_yes.files["memory/schedule.md"]),
    )
    ctx_no = ManageCtx({"memory/schedule.md": base_schedule}, confirm=False)
    no_reply = manage(ctx_no, target="schedule", action="clear")
    ok &= check(
        "confirm=False -> файл не тронут",
        no_reply == "Хорошо, не трогаю."
        and ctx_no.files["memory/schedule.md"] == base_schedule,
        repr(ctx_no.files["memory/schedule.md"]),
    )
    ctx_absent = ManageCtx({"memory/schedule.md": base_schedule}, confirm=False)
    absent_reply = manage(ctx_absent, target="schedule", action="clear")
    ok &= check(
        "без UI (confirm=False) -> файл не тронут",
        absent_reply == "Хорошо, не трогаю."
        and ctx_absent.files["memory/schedule.md"] == base_schedule,
    )
    ctx_done = ManageCtx({"memory/tasks.md": "# Задачи\n- [open] купить корм\n"})
    done_reply = manage(ctx_done, target="tasks", action="done", what="корм")
    ok &= check(
        "done task по подстроке",
        "[done] купить корм" in ctx_done.files["memory/tasks.md"],
        done_reply,
    )
    ctx_clear_rem = ManageCtx({}, confirm=True)
    rem_reply = manage(ctx_clear_rem, target="reminders", action="clear")
    ok &= check(
        "clear reminders вызывает reminders_write",
        ctx_clear_rem.reminders_clear_calls == 1 and "напоминания" in rem_reply.lower(),
        rem_reply,
    )
    previous_overlay = None
    try:
        from kivi.skills.broker import set_overlay_service

        set_overlay_service(None)
        broker_ctx = SkillContext(manifests["memory_manage"])  # type: ignore[arg-type]
        ok &= check(
            "broker.confirm без UI -> False",
            broker_ctx.confirm("Очистить?", "все записи") is False,
        )
    finally:
        set_overlay_service(previous_overlay)

    print("\n--- 3) capabilities ---")
    capabilities = load(manifests["capabilities"])  # type: ignore[arg-type]
    cap_reply = capabilities(FakeCapCtx())
    print("capabilities:", cap_reply)
    ok &= check(
        "capabilities содержит реальные умения",
        "время и дата" in cap_reply
        and "поиск файлов" in cap_reply
        and "память и расписание" in cap_reply,
        cap_reply,
    )
    ok &= check("capabilities не упоминает музыку", "музык" not in cap_reply.lower())

    print("\n--- 3b) planner routes ---")
    planner_cases = [
        (
            "что ты умеешь",
            {
                "skill_name": "capabilities",
                "args": {},
                "route": "skill",
                "missing": [],
            },
            "capabilities",
        ),
        (
            "выруби прослушку",
            {
                "skill_name": "listen_control",
                "args": {"action": "pause"},
                "route": "skill",
                "missing": [],
            },
            "listen_control",
        ),
        (
            "очисти расписание",
            {
                "skill_name": "memory_manage",
                "args": {"target": "schedule", "action": "clear"},
                "route": "skill",
                "missing": [],
            },
            "memory_manage",
        ),
    ]
    planner_gaps = GapLog(TMP / f"manage_gaps_{token}.db")
    for command, payload, expected in planner_cases:
        planner = Planner(
            registry=registry,
            strategy="llm_only",
            router=JsonRouter(payload),
            gaps=planner_gaps,
        )
        plan = planner.plan(command)
        actual = getattr(plan.get("skill"), "name", None)
        print(f"planner {command!r}: {actual} {plan.get('args')}")
        ok &= check(
            f"planner «{command}» -> {expected}",
            plan.get("route") == "skill" and actual == expected,
            str(actual),
        )

    print("\n--- 4) search: backends + cache + outage ---")
    try:
        import duckduckgo_search
    except ImportError:
        print("FAIL — нет duckduckgo_search")
        return 1
    original_ddgs = duckduckgo_search.DDGS
    cache_path = TMP / f"search_cache_{token}.json"
    old_cache_path = search_mod.SEARCH_CACHE_PATH
    search_mod.SEARCH_CACHE_PATH = cache_path
    try:
        FakeDDGS.constructors = 0
        FakeDDGS.backend_calls.clear()
        duckduckgo_search.DDGS = FakeDDGS  # type: ignore[assignment]
        backend_results = search_mod.search_web(
            "курс биткоина", use_cache=False, cache_path=cache_path
        )
        print("backends:", FakeDDGS.backend_calls, backend_results)
        ok &= check(
            "api падает -> html возвращает результат",
            bool(backend_results)
            and FakeDDGS.backend_calls[:2] == ["api", "html"],
            str(FakeDDGS.backend_calls),
        )

        FakeDDGS.constructors = 0
        FakeDDGS.backend_calls.clear()
        first = search_mod.search_web(
            "уникальный запрос для кэша", cache_path=cache_path
        )
        second = search_mod.search_web(
            "Уникальный запрос для кэша", cache_path=cache_path
        )
        print("cache:", first, second, "constructors=", FakeDDGS.constructors)
        ok &= check(
            "повторный запрос из кэша без сети",
            bool(first)
            and first == second
            and FakeDDGS.constructors == 2,
            f"constructors={FakeDDGS.constructors}",
        )

        duckduckgo_search.DDGS = FailDDGS  # type: ignore[assignment]
        search_mod.clear_search_cache(cache_path)
        unavailable_results = search_mod.search_web(
            "все упали", use_cache=False, cache_path=cache_path
        )
        ok &= check(
            "все бэкенды упали -> [] и outage",
            unavailable_results == []
            and search_mod.search_unavailable() is True,
        )
        search_mod.clear_search_cache(cache_path)
        answer = search_mod.answer_search(
            "какой курс биткоина",
            router=FakeSearchRouter(),
            timeout=0.2,
        )
        print("answer_search outage:", answer)
        ok &= check(
            "answer_search -> временно недоступен",
            answer == search_mod.SEARCH_UNAVAILABLE_ANSWER,
            answer,
        )
    finally:
        duckduckgo_search.DDGS = original_ddgs  # type: ignore[assignment]
        search_mod.SEARCH_CACHE_PATH = old_cache_path

    print()
    print("PASS" if ok else "FAIL — скопируй весь вывод и пришли в чат")
    return 0 if ok else 1


if __name__ == "__main__":
    raise SystemExit(main())
