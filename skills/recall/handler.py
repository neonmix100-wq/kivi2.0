"""Скилл recall: конкретный ответ по schedule/tasks/reminders из vault.

Перед чтением памяти LLM (cheap, task_type=intent) извлекает keywords и
date_refs. Фильтр — подстрока по keywords; date_refs ограничивают выдачу
конкретной датой. Пустые keywords/date_refs сохраняют прежний обзор.
"""

from __future__ import annotations

import re
from datetime import datetime

from kivi.skills.broker import project_timezone
from kivi.skills.recall import (
    QueryEntities,
    as_entities,
    date_refs_to_dates,
    keyword_in_text,
    reminder_matches_date,
    schedule_matches_date,
    task_matches_date,
)

_ENTRY_RE = re.compile(r"^\s*[-*]\s*\[([^\]]+)\]\s*(.+?)\s*$")
_TIME_RE = re.compile(r"\b([01]?\d|2[0-3])[:.](\d{2})")


def _read(ctx, name: str) -> str:
    try:
        return ctx.fs_read(f"memory/{name}")
    except FileNotFoundError:
        return ""


def _entries(text: str) -> list[tuple[str, str]]:
    found: list[tuple[str, str]] = []
    for line in str(text).splitlines():
        match = _ENTRY_RE.match(line)
        if match:
            found.append((match.group(1).strip(), match.group(2).strip()))
    return found


def _schedule_phrase(when: str, what: str) -> str:
    match = _TIME_RE.search(when)
    if match:
        hour = int(match.group(1))
        minute = match.group(2)
        return f"{what} в {hour:02d}:{minute}"
    return f"{what} ({when})"


def _now(ctx) -> datetime:
    try:
        current = ctx.now()
    except Exception:  # noqa: BLE001 - старый/тестовый контекст без now
        current = datetime.now(project_timezone())
    if current.tzinfo is None:
        current = current.replace(tzinfo=project_timezone())
    return current


def _not_found_word(keyword: str) -> str:
    """Мягкая форма для фразы «про …»: встреча -> встречу, мама -> маму."""
    word = str(keyword or "").strip()
    if len(word) >= 3 and word[-1] in "ая":
        return word[:-1] + "у"
    return word


def _entities(ctx, command: str) -> QueryEntities:
    """Получить keywords/date_refs через брокер; без LLM — пустой фильтр."""
    if not command:
        return QueryEntities([], [])
    extractor = getattr(ctx, "extract_keywords", None)
    if not callable(extractor):
        return QueryEntities([], [])
    try:
        return as_entities(extractor(command))
    except Exception as exc:  # noqa: BLE001 - LLM не должна ронять recall
        ctx.log(f"recall: извлечение сущностей упало: {type(exc).__name__}")
        return QueryEntities([], [])


def handle(ctx, **args) -> str:
    """Ответить по релевантным записям, а не перечислять весь vault."""
    command = " ".join(str(args.get("command") or "").split())
    entities = _entities(ctx, command)
    keywords = list(entities.keywords)
    current = _now(ctx)
    targets = date_refs_to_dates(entities.date_refs, current)
    filtered = bool(keywords or targets)

    schedule_all = _entries(_read(ctx, "schedule.md"))
    tasks_all = [
        what
        for status, what in _entries(_read(ctx, "tasks.md"))
        if status.lower() == "open"
    ]
    try:
        reminders_all = [
            item
            for item in ctx.reminders_list()
            if not item.get("done")
        ]
    except Exception:  # noqa: BLE001 - старый контекст/нет напоминаний
        reminders_all = []

    schedule = [
        (when, what)
        for when, what in schedule_all
        if (not keywords or keyword_in_text(f"{when} {what}", keywords))
        and (not targets or schedule_matches_date(when, targets, current))
    ]
    tasks = [
        what
        for what in tasks_all
        if (not keywords or keyword_in_text(what, keywords))
        and (not targets or task_matches_date(what, targets, current))
    ]
    reminders = [
        item
        for item in reminders_all
        if (
            not keywords
            or keyword_in_text(
                f"{item.get('when_label', '')} {item.get('ts', '')} "
                f"{item.get('what', '')}",
                keywords,
            )
        )
        and (not targets or reminder_matches_date(item, targets, current))
    ]

    found = len(schedule) + len(tasks) + len(reminders)
    ctx.log(f"recall: фильтрую по {keywords} -> найдено {found} записей")

    if not found:
        if keywords:
            return f"Не нашла в твоих задачах про {_not_found_word(keywords[0])}"
        return "Пока пусто."

    parts: list[str] = []
    if schedule:
        phrases = "; ".join(_schedule_phrase(when, what) for when, what in schedule)
        parts.append(f"Расписание: {phrases}.")
    if tasks:
        if filtered:
            parts.append("Задачи: " + "; ".join(tasks) + ".")
        else:
            parts.append(f"Открытых задач: {len(tasks)}.")
    if reminders:
        phrases = "; ".join(
            f"{item.get('when_label') or item.get('ts', '')} {item.get('what', '')}".strip()
            for item in reminders
        )
        parts.append(f"Напоминания: {phrases}.")
    if not filtered:
        if not schedule:
            parts.insert(0, "Расписание пусто.")
        if not tasks:
            parts.insert(-1 if reminders else len(parts), "Открытых задач: 0.")
    return " ".join(parts)
