"""Смоук рутинных команд: remember, reminders, tasks_list, planner, UI markers.

Запуск:
    python tests/smoke_routine.py

LLM не вызывается: Planner использует ScriptedRouter, напоминания — мок времени.
"""

from __future__ import annotations

import json
import sys
import uuid
from datetime import datetime, timedelta
from pathlib import Path
from typing import Any

PROJECT_ROOT = Path(__file__).resolve().parents[1]
if str(PROJECT_ROOT) not in sys.path:
    sys.path.insert(0, str(PROJECT_ROOT))

from kivi.config import get_settings
from kivi.log import setup_logging
from kivi.memory.vault import add_fact, add_preference, add_schedule, add_task, read_note
from kivi.skills.broker import PermissionDenied, SkillContext, project_timezone
from kivi.skills.gaps import GapLog
from kivi.skills.loader import load
from kivi.skills.planner import Planner
from kivi.skills.registry import SkillManifest, SkillPermissions, SkillRegistry
from kivi.skills.reminders import add_reminder, check_due

TMP = PROJECT_ROOT / "data" / "tmp"


def check(label: str, condition: bool, detail: str = "") -> bool:
    print(("OK  " if condition else "X   ") + label + (f" — {detail}" if detail else ""))
    return bool(condition)


class VaultCtx:
    """Контекст для remember/tasks_list: пишет/читает временный vault."""

    def __init__(self, vault: Path) -> None:
        self.vault = vault
        self.called: list[tuple[str, tuple]] = []

    def vault_add_schedule(self, when: str, what: str) -> bool:
        self.called.append(("schedule", (when, what)))
        return add_schedule(when, what, path=self.vault)

    def vault_add_task(self, what: str) -> bool:
        self.called.append(("task", (what,)))
        return add_task(what, "open", path=self.vault)

    def vault_add_preference(self, what: str) -> bool:
        self.called.append(("preference", (what,)))
        return add_preference(what, path=self.vault)

    def vault_add_fact(self, what: str) -> bool:
        self.called.append(("fact", (what,)))
        return add_fact(what, kind="profile", path=self.vault)

    def fs_read(self, path: str) -> str:
        name = Path(path).name
        return read_note(Path(name).stem, "", self.vault)


class ReminderCtx:
    def __init__(self, path: Path, now: datetime) -> None:
        self.path = path
        self.now = now

    def scheduler(self, time: str, what: str) -> str:
        return add_reminder(
            time,
            what,
            now=self.now,
            path=self.path,
            start_scheduler=False,
        )


class ScriptedRouter:
    def __init__(self, replies: list[dict[str, Any]]) -> None:
        self.replies = list(replies)
        self.calls = 0

    def complete(self, messages: Any, task_type: str = "intent", **_kw: Any) -> dict[str, Any]:
        self.calls += 1
        payload = self.replies.pop(0) if len(self.replies) > 1 else self.replies[0]
        return {
            "text": json.dumps(payload, ensure_ascii=False),
            "cost_rub": 0.0,
            "model": "scripted/routine",
            "ok": True,
        }


def plan_reply(skill: str, args: dict[str, Any]) -> dict[str, Any]:
    return {"skill_name": skill, "args": args, "route": "skill", "missing": []}

def main() -> int:
    setup_logging()
    print("=== SMOKE: рутинные голосовые команды ===")
    TMP.mkdir(parents=True, exist_ok=True)
    token = uuid.uuid4().hex
    vault = TMP / f"routine_vault_{token}"
    reminders_path = TMP / f"routine_reminders_{token}.json"
    skills_db = TMP / f"routine_skills_{token}.db"
    gaps_db = TMP / f"routine_gaps_{token}.db"
    ok = True

    registry = SkillRegistry(
        skills_dir=PROJECT_ROOT / "skills",
        installed_dir=TMP / f"routine_installed_{token}",
        db_path=skills_db,
    )
    registry.scan()
    remember_manifest = registry.get("remember")
    reminders_manifest = registry.get("reminders")
    tasks_manifest = registry.get("tasks_list")
    ok &= check("remember найден", remember_manifest is not None)
    ok &= check("reminders найден", reminders_manifest is not None)
    ok &= check("tasks_list найден", tasks_manifest is not None)
    if remember_manifest is None or reminders_manifest is None or tasks_manifest is None:
        print("FAIL — нет манифестов")
        return 1

    ctx = VaultCtx(vault)
    remember = load(remember_manifest)
    reply_schedule = remember(
        ctx, kind="schedule", what="стендап", when="каждый будний день в 15:00"
    )
    print("remember schedule:", reply_schedule)
    schedule_text = read_note("schedule", "", vault)
    ok &= check(
        "remember schedule -> schedule.md",
        "будни 15:00" in schedule_text and "стендап" in schedule_text.lower(),
    )
    ok &= check(
        "ответ remember schedule",
        "Запомнила" in reply_schedule and "по будням" in reply_schedule,
        reply_schedule,
    )

    reply_task = remember(ctx, kind="task", what="купить корм")
    print("remember task:", reply_task)
    tasks_text = read_note("tasks", "", vault)
    ok &= check(
        "remember task -> tasks.md",
        "[open] купить корм" in tasks_text,
    )
    ok &= check("ответ remember task", "Записала задачу" in reply_task)

    preference_reply = remember(
        ctx, kind="preference", what="люблю короткий ответ"
    )
    profile_text = read_note("profile", "", vault)
    ok &= check(
        "remember preference -> profile.md",
        "короткий ответ" in profile_text.lower(),
        preference_reply,
    )
    clarify = remember(ctx, kind="", what="")
    ok &= check("remember без kind -> уточнение", clarify == "Уточни: что запомнить?")

    tasks_handler = load(tasks_manifest)
    task_reply = tasks_handler(ctx)
    print("tasks_list:", task_reply)
    ok &= check(
        "tasks_list возвращает open-задачу",
        "Открытых задач: 1" in task_reply and "купить корм" in task_reply.lower(),
    )
    empty_task_reply = tasks_handler(VaultCtx(TMP / f"empty_{token}"))
    ok &= check("tasks_list пусто", empty_task_reply == "Задач пока нет")


    base = datetime(2026, 11, 22, 18, 0, tzinfo=project_timezone())
    reminder_ctx = ReminderCtx(reminders_path, base)
    reminders_handler = load(reminders_manifest)
    reminder_reply = reminders_handler(
        reminder_ctx, time="19:00", what="позвонить маме"
    )
    print("reminders:", reminder_reply)
    records = json.loads(reminders_path.read_text(encoding="utf-8"))
    ok &= check(
        "reminder записан в reminders.json",
        len(records) == 1
        and records[0].get("what") == "позвонить маме"
        and records[0].get("done") is False,
    )
    ok &= check(
        "ответ reminders",
        reminder_reply.startswith("Напомню в 19:00"),
        reminder_reply,
    )
    spoken: list[str] = []
    due = check_due(
        now=base + timedelta(hours=1, seconds=31),
        speak_fn=spoken.append,
        path=reminders_path,
    )
    records_after = json.loads(reminders_path.read_text(encoding="utf-8"))
    ok &= check(
        "через 31с напоминание озвучено",
        due == ["Напоминание: позвонить маме"],
        str(due),
    )
    ok &= check("reminder помечен done", records_after[0].get("done") is True)

    # Брокер: без permissions.tools self_memory запись запрещена.
    plain = remember_manifest.model_copy(
        update={"permissions": SkillPermissions()}
    )
    try:
        SkillContext(plain).vault_add_schedule("ПН 09:00", "тест")
    except PermissionDenied:
        ok &= check("broker vault_add_schedule без права -> PermissionDenied", True)
    else:
        ok &= check("broker vault_add_schedule без права -> PermissionDenied", False)

    planner_router = ScriptedRouter(
        [
            plan_reply(
                "remember",
                {"kind": "schedule", "when": "15:00", "what": "стендап"},
            ),
            plan_reply(
                "reminders", {"time": "19:00", "what": "позвонить маме"}
            ),
            plan_reply("tasks_list", {}),
        ]
    )
    planner = Planner(
        registry=registry,
        router=planner_router,
        strategy="llm_only",
        gaps=GapLog(gaps_db),
    )
    plan_remember = planner.plan("запомни стендап в 15:00")
    plan_remind = planner.plan("напомни в 19:00 позвонить")
    plan_tasks = planner.plan("мои задачи")
    ok &= check(
        "planner: запомни -> remember",
        plan_remember["skill"] is not None
        and plan_remember["skill"].name == "remember"
        and plan_remember["route"] == "skill",
        str(plan_remember.get("args")),
    )
    ok &= check(
        "planner: напомни -> reminders",
        plan_remind["skill"] is not None
        and plan_remind["skill"].name == "reminders"
        and plan_remind["route"] == "skill",
    )
    ok &= check(
        "planner: мои задачи -> tasks_list",
        plan_tasks["skill"] is not None
        and plan_tasks["skill"].name == "tasks_list"
        and plan_tasks["route"] == "skill",
    )

    index_text = (PROJECT_ROOT / "app" / "renderer" / "index.html").read_text(
        encoding="utf-8"
    )
    app_js = (PROJECT_ROOT / "app" / "renderer" / "app.js").read_text(
        encoding="utf-8"
    )
    ok &= check("UI skills section есть", 'id="skills-list"' in index_text)
    ok &= check("UI gaps section есть", 'id="gaps-body"' in index_text)
    ok &= check("UI schedule form есть", 'id="schedule-form"' in index_text)
    ok &= check(
        "UI JS загружает скиллы/пробелы",
        "function loadSkills" in app_js and "function loadGaps" in app_js,
    )

    print()
    print("PASS" if ok else "FAIL — скопируй весь вывод и пришли в чат")
    return 0 if ok else 1


if __name__ == "__main__":
    raise SystemExit(main())

