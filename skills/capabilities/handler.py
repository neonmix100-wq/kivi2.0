"""Скилл capabilities: честный список умений и ответы о себе по манифесту."""
from __future__ import annotations

from typing import Any

_LABELS = {
    "time_date": "время и дата",
    "file_search": "поиск файлов",
    "search": "поиск в интернете",
    "facts": "курс валют и погода по открытым API",
    "code": "код через OpenCode",
    "recall": "память и расписание",
    "remember": "память и расписание",
    "reminders": "напоминания",
    "tasks_list": "задачи",
    "assistant_volume": "громкость",
    "listen_control": "управление прослушкой",
    "memory_manage": "управление памятью",
    "capabilities": "возможности",
    "screen_look": "зрение (скриншот экрана)",
}
_ORDER = {
    "time_date": 10,
    "file_search": 20,
    "search": 25,
    "facts": 27,
    "code": 65,
    "recall": 30,
    "remember": 30,
    "reminders": 40,
    "tasks_list": 50,
    "assistant_volume": 60,
    "listen_control": 70,
    "memory_manage": 80,
    "screen_look": 75,
    "capabilities": 90,
}


def _fallback_skills() -> list[dict[str, str]]:
    try:
        from kivi.skills.registry import get_registry

        return [
            {
                "name": item.name,
                "description": item.description,
                "category": item.category,
            }
            for item in get_registry().list_enabled()
        ]
    except Exception:  # noqa: BLE001 - старый/тестовый контекст
        return []


def handle(ctx, **args: Any) -> str:
    command = " ".join(str(args.get("command") or args.get("question") or "").split())
    if command:
        # Мета-вопрос («что тебе нужно», «есть ли доступ») — отвечаем по манифесту,
        # а не списком умений: пользователю нужен факт о доступе.
        try:
            from kivi.brain.selfmodel import is_meta_question, meta_answer

            if is_meta_question(command):
                return meta_answer()
        except Exception:  # noqa: BLE001 - манифест не должен ломать скилл
            pass
    getter = getattr(ctx, "skills_list", None)
    try:
        skills = getter() if callable(getter) else _fallback_skills()
    except Exception:  # noqa: BLE001 - каталог не должен ронять ответ
        skills = []
    labels: list[str] = []
    ordered: list[tuple[int, str]] = []
    for item in skills or []:
        if not isinstance(item, dict):
            continue
        name = str(item.get("name") or "").strip()
        if not name:
            continue
        label = _LABELS.get(name) or _clean(item.get("description") or name)
        if label and label not in labels:
            labels.append(label)
            ordered.append((_ORDER.get(name, 100), label))
    if not labels:
        return "Сейчас не вижу включённых умений."
    ordered.sort(key=lambda pair: pair[0])
    return "Сейчас умею: " + ", ".join(label for _, label in ordered) + "."


def _clean(value: object) -> str:
    return " ".join(str(value or "").split()).strip(" .,")
