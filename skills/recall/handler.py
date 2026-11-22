"""Скилл recall: короткий ответ по schedule.md и tasks.md из vault.

Читает только memory/ через ctx.fs_read; ничего не пишет.
"""

from __future__ import annotations

import re

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


def handle(ctx, **_args) -> str:
    """Ответить по расписанию и открытым задачам; пустой vault -> «Пока пусто.»"""
    schedule = _entries(_read(ctx, "schedule.md"))
    tasks = [
        what
        for status, what in _entries(_read(ctx, "tasks.md"))
        if status.lower() == "open"
    ]
    if not schedule and not tasks:
        return "Пока пусто."

    parts: list[str] = []
    if schedule:
        phrases = "; ".join(_schedule_phrase(when, what) for when, what in schedule)
        parts.append(f"Расписание: {phrases}.")
    else:
        parts.append("Расписание пусто.")
    if tasks:
        parts.append(f"Открытых задач: {len(tasks)}.")
    else:
        parts.append("Открытых задач: 0.")
    ctx.log(f"recall: schedule={len(schedule)}, open_tasks={len(tasks)}")
    return " ".join(parts)
