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
from kivi.skills.recall import extract_entities
from kivi.skills.reminders import (
    add_reminder,
    cancel_reminder,
    check_due,
    list_reminders,
    parse_timestamp,
)

TMP = PROJECT_ROOT / "data" / "tmp"


def check(label: str, condition: bool, detail: str = "") -> bool:
    print(("OK  " if condition else "X   ") + label + (f" — {detail}" if detail else ""))
    return bool(condition)


class VaultCtx:
    """Контекст для remember/tasks_list: пишет/читает временный vault."""

    def __init__(self, vault: Path) -> None:
        self.vault = vault
        self.called: list[tuple[str, tuple]] = []
        self.logs: list[str] = []

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

    def log(self, text: str) -> None:
        self.logs.append(str(text))


class ReminderCtx:
    def __init__(self, path: Path, now: datetime, vault: Path | None = None) -> None:
        self.path = path
        self.now = now
        self.vault = vault

    def scheduler(self, time: str, what: str) -> str:
        return add_reminder(
            time,
            what,
            now=self.now,
            path=self.path,
            start_scheduler=False,
        )

    def vault_add_task(self, what: str) -> bool:
        if self.vault is None:
            return False
        return add_task(what, "open", path=self.vault)


class RecallCtx(VaultCtx):
    def __init__(
        self,
        vault: Path,
        reminders: list[dict[str, Any]],
        entities: dict[str, Any] | None = None,
        now: datetime | None = None,
    ) -> None:
        super().__init__(vault)
        self._reminders = reminders
        self.entities = entities or {"keywords": [], "date_refs": []}
        self.now_value = now or datetime(2026, 11, 22, 18, 0, tzinfo=project_timezone())
        self.extract_calls: list[str] = []

    def reminders_list(self) -> list[dict[str, Any]]:
        return list(self._reminders)

    def extract_keywords(self, command: str) -> dict[str, Any]:
        self.extract_calls.append(command)
        return dict(self.entities)

    def now(self) -> datetime:
        return self.now_value


class ExtractionRouter:
    """Фейковый Router: фиксирует task_type и отдаёт JSON сущностей."""

    def __init__(self, payload: dict[str, Any]) -> None:
        self.payload = payload
        self.task_types: list[str] = []
        self.messages: list[dict] = []

    def complete(
        self, messages: Any, task_type: str = "chat", **_kw: Any
    ) -> dict[str, Any]:
        self.task_types.append(task_type)
        self.messages = list(messages)
        return {
            "text": json.dumps(self.payload, ensure_ascii=False),
            "cost_rub": 0.0,
            "model": "scripted/extract",
        }


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
    recall_manifest = registry.get("recall")
    ok &= check("remember найден", remember_manifest is not None)
    ok &= check("reminders найден", reminders_manifest is not None)
    ok &= check("tasks_list найден", tasks_manifest is not None)
    ok &= check("recall найден", recall_manifest is not None)
    if (
        remember_manifest is None
        or reminders_manifest is None
        or tasks_manifest is None
        or recall_manifest is None
    ):
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
        "remember schedule -> канон в schedule.md",
        "[ПН-ПТ 15:00] стендап" in schedule_text,
        schedule_text.strip(),
    )
    ok &= check(
        "ответ remember schedule (дни + время)",
        "Запомнила" in reply_schedule
        and "будни 15:00" in reply_schedule
        and "стендап" in reply_schedule,
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
    ok &= check(
        "ts сохранён как ISO с датой",
        isinstance(records_after[0].get("ts"), str)
        and "T" in records_after[0]["ts"]
        and parse_timestamp(records_after[0]["ts"]) is not None,
        str(records_after[0].get("ts")),
    )

    tomorrow_path = TMP / f"routine_tomorrow_{token}.json"
    tomorrow_reply = add_reminder(
        "завтра в 19:00",
        "позвонить маме",
        now=base,
        path=tomorrow_path,
        start_scheduler=False,
    )
    tomorrow_rec = json.loads(tomorrow_path.read_text(encoding="utf-8"))[0]
    tomorrow_when = parse_timestamp(tomorrow_rec["ts"])
    print("tomorrow reminder:", tomorrow_reply)
    ok &= check(
        "завтра в 19:00 -> ts завтра, не сегодня",
        tomorrow_when is not None
        and tomorrow_when.date() == (base + timedelta(days=1)).date()
        and tomorrow_when.hour == 19
        and "завтра" in tomorrow_reply.lower(),
    )

    late_path = TMP / f"routine_late_{token}.json"
    late_now = base.replace(hour=20, minute=0)
    late_reply = add_reminder(
        "19:00",
        "позвонить маме",
        now=late_now,
        path=late_path,
        start_scheduler=False,
    )
    late_rec = json.loads(late_path.read_text(encoding="utf-8"))[0]
    late_when = parse_timestamp(late_rec["ts"])
    print("rolled reminder:", late_reply)
    ok &= check(
        "19:00 после 20:00 -> завтра и явно в ответе",
        late_when is not None
        and late_when.date() == (base + timedelta(days=1)).date()
        and "завтра" in late_reply.lower(),
        late_reply,
    )

    date_path = TMP / f"routine_date_{token}.json"
    date_reply = add_reminder(
        "23.11 в 19:00",
        "дата",
        now=base,
        path=date_path,
        start_scheduler=False,
    )
    date_when = parse_timestamp(
        json.loads(date_path.read_text(encoding="utf-8"))[0]["ts"]
    )
    ok &= check(
        "ДД.ММ -> дата 23.11",
        date_when is not None
        and date_when.day == 23
        and date_when.month == 11
        and "23.11" in date_reply,
        date_reply,
    )

    weekday_path = TMP / f"routine_weekday_{token}.json"
    weekday_reply = add_reminder(
        "в понедельник в 09:00",
        "понедельник",
        now=base,
        path=weekday_path,
        start_scheduler=False,
    )
    weekday_when = parse_timestamp(
        json.loads(weekday_path.read_text(encoding="utf-8"))[0]["ts"]
    )
    ok &= check(
        "день недели -> следующий понедельник",
        weekday_when is not None
        and weekday_when.weekday() == 0
        and "понедельник" in weekday_reply,
        weekday_reply,
    )

    before_no_time = len(json.loads(reminders_path.read_text(encoding="utf-8")))
    no_time_reply = reminders_handler(
        ReminderCtx(reminders_path, base, vault),
        time="",
        what="купить корм",
    )
    after_no_time = len(json.loads(reminders_path.read_text(encoding="utf-8")))
    print("no-time reminder:", no_time_reply)
    ok &= check(
        "напомни без времени -> задача, не напоминание",
        no_time_reply == "Записала задачу: купить корм"
        and after_no_time == before_no_time
        and "купить корм" in read_note("tasks", "", vault),
    )

    pending = [
        {
            "id": "r1",
            "when_label": "завтра 19:00",
            "what": "позвонить маме",
            "done": False,
        }
    ]
    recall_handler = load(recall_manifest)
    recall_reply = recall_handler(RecallCtx(vault, pending))
    print("recall reminders:", recall_reply)
    ok &= check(
        "recall включает блок Напоминания",
        "Напоминания: завтра 19:00 позвонить маме" in recall_reply,
        recall_reply,
    )
    tasks_reminders_reply = tasks_handler(RecallCtx(vault, pending))
    print("tasks с напоминаниями:", tasks_reminders_reply)
    ok &= check(
        "tasks_list упоминает напоминания",
        "Напоминаний: 1" in tasks_reminders_reply
        and "завтра 19:00" in tasks_reminders_reply,
    )

    extract_router = ExtractionRouter(
        {"keywords": ["мама", "позвонить"], "date_refs": []}
    )
    entities = extract_entities("во сколько позвонить маме", router=extract_router)
    print("entities:", entities)
    ok &= check(
        "recall: LLM вызван с task_type=intent",
        extract_router.task_types == ["intent"],
        str(extract_router.task_types),
    )
    ok &= check(
        "recall: keywords извлечены",
        entities.keywords == ["мама", "позвонить"] and entities.date_refs == [],
    )
    ok &= check(
        "recall: в промпте есть запрос",
        bool(extract_router.messages)
        and "во сколько позвонить маме" in extract_router.messages[-1]["content"],
    )

    recall_ctx = RecallCtx(
        vault,
        pending,
        {"keywords": ["мама", "позвонить"], "date_refs": []},
        base,
    )
    mama_answer = recall_handler(
        recall_ctx, command="во сколько позвонить маме"
    )
    print("recall только мама:", mama_answer)
    ok &= check(
        "recall: вопрос про маму -> только напоминание",
        "завтра 19:00 позвонить маме" in mama_answer
        and "стендап" not in mama_answer.lower()
        and "купить корм" not in mama_answer.lower(),
        mama_answer,
    )
    ok &= check(
        "recall: команда передана в извлечение",
        recall_ctx.extract_calls == ["во сколько позвонить маме"],
    )
    ok &= check(
        "recall: лог фильтрации",
        any("recall: фильтрую по ['мама', 'позвонить']" in line for line in recall_ctx.logs),
        " | ".join(recall_ctx.logs),
    )

    standup_answer = recall_handler(
        RecallCtx(
            vault,
            pending,
            {"keywords": ["стендап"], "date_refs": []},
            base,
        ),
        command="стендап во сколько",
    )
    print("recall стендап:", standup_answer)
    ok &= check(
        "recall: стендап -> только расписание",
        "стендап" in standup_answer.lower()
        and "Напоминания" not in standup_answer
        and "купить корм" not in standup_answer,
        standup_answer,
    )

    tomorrow_answer = recall_handler(
        RecallCtx(
            vault,
            pending,
            {"keywords": [], "date_refs": ["tomorrow"]},
            base,
        ),
        command="что у меня завтра",
    )
    print("recall завтра:", tomorrow_answer)
    ok &= check(
        "recall: что у меня завтра -> всё на завтра",
        "стендап" in tomorrow_answer.lower()
        and "завтра 19:00 позвонить маме" in tomorrow_answer
        and "купить корм" not in tomorrow_answer,
        tomorrow_answer,
    )

    missing_answer = recall_handler(
        RecallCtx(
            vault,
            pending,
            {"keywords": ["встреча"], "date_refs": []},
            base,
        ),
        command="встреча с несуществующим",
    )
    print("recall не найдено:", missing_answer)
    ok &= check(
        "recall: не найдено -> точный ответ",
        missing_answer == "Не нашла в твоих задачах про встречу",
        missing_answer,
    )

    ok &= check(
        "recall manifest разрешает llm_intent",
        "llm_intent" in recall_manifest.permissions.tools,
    )
    no_llm = recall_manifest.model_copy(
        update={"permissions": SkillPermissions(tools=["fs_read", "reminders_read"])}
    )
    try:
        SkillContext(no_llm).extract_keywords("во сколько позвонить маме")
    except PermissionDenied:
        ok &= check("broker extract_keywords без llm_intent -> PermissionDenied", True)
    else:
        ok &= check("broker extract_keywords без llm_intent -> PermissionDenied", False)

    cancel_path = TMP / f"routine_cancel_{token}.json"
    add_reminder(
        "завтра в 19:00",
        "отменить меня",
        now=base,
        path=cancel_path,
        start_scheduler=False,
    )
    cancel_id = json.loads(cancel_path.read_text(encoding="utf-8"))[0]["id"]
    cancelled = cancel_reminder(cancel_id, path=cancel_path)
    remaining = list_reminders(path=cancel_path, days=None)
    ok &= check(
        "cancel_reminder удаляет запись",
        cancelled and remaining == [],
        f"id={cancel_id}, осталось={len(remaining)}",
    )

    legacy_path = TMP / f"routine_legacy_{token}.json"
    legacy_path.write_text(
        json.dumps([{"ts": base.timestamp(), "what": "старое", "done": False}]),
        encoding="utf-8",
    )
    legacy = list_reminders(
        include_done=True, days=None, now=base, path=legacy_path
    )
    ok &= check(
        "старый epoch-формат мигрирует без потери",
        len(legacy) == 1
        and legacy[0].get("what") == "старое"
        and str(legacy[0].get("ts", "")).startswith("2026-11-22"),
        str(legacy),
    )

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
            plan_reply(
                "remember", {"kind": "task", "what": "купить корм"}
            ),
            plan_reply("recall", {}),
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
    plan_no_time = planner.plan("напомни купить корм")
    plan_recall = planner.plan("во сколько мне позвонить маме?")
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
    ok &= check(
        "planner: напомни без времени -> remember(task)",
        plan_no_time["skill"] is not None
        and plan_no_time["skill"].name == "remember"
        and plan_no_time["args"].get("kind") == "task",
    )
    ok &= check(
        "planner: во сколько мне -> recall",
        plan_recall["skill"] is not None
        and plan_recall["skill"].name == "recall"
        and plan_recall["route"] == "skill",
    )
    ok &= check(
        "planner: recall получает исходную команду",
        plan_recall.get("args", {}).get("command")
        == "во сколько мне позвонить маме?",
        str(plan_recall.get("args")),
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
    ok &= check("UI reminders view есть", 'id="reminders-view"' in index_text)
    ok &= check(
        "UI reminders tab есть",
        'data-kind="reminders"' in index_text,
    )
    ok &= check(
        "UI JS загружает скиллы/пробелы/напоминания",
        "function loadSkills" in app_js
        and "function loadGaps" in app_js
        and "function loadReminders" in app_js,
    )

    print()
    print("PASS" if ok else "FAIL — скопируй весь вывод и пришли в чат")
    return 0 if ok else 1


if __name__ == "__main__":
    raise SystemExit(main())

