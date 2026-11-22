"""Скилл tasks_list: открытые задачи из memory/tasks.md."""

from __future__ import annotations

import re

_ENTRY_RE = re.compile(r"^\s*[-*]\s*\[([^\]]+)\]\s*(.+?)\s*$")


def handle(ctx, **_args) -> str:
    try:
        text = ctx.fs_read("memory/tasks.md")
    except FileNotFoundError:
        text = ""
    tasks: list[str] = []
    for line in text.splitlines():
        match = _ENTRY_RE.match(line)
        if match and match.group(1).strip().lower() == "open":
            tasks.append(match.group(2).strip())
    try:
        reminders = [
            item for item in ctx.reminders_list() if not item.get("done")
        ]
    except Exception:  # noqa: BLE001 - старый контекст/нет напоминаний
        reminders = []
    if not tasks and not reminders:
        return "Задач пока нет"
    if tasks:
        listed = ", ".join(f"{index}) {what}" for index, what in enumerate(tasks, 1))
        result = f"Открытых задач: {len(tasks)}. {listed}"
    else:
        result = "Задач пока нет."
    if reminders:
        nearest = reminders[0]
        label = nearest.get("when_label") or nearest.get("ts") or ""
        result += (
            f" Напоминаний: {len(reminders)} "
            f"(ближайшее: {label} {nearest.get('what', '')})."
        )
    return result
