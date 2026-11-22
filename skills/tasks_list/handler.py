"""Скилл tasks_list: открытые задачи из memory/tasks.md."""

from __future__ import annotations

import re

_ENTRY_RE = re.compile(r"^\s*[-*]\s*\[([^\]]+)\]\s*(.+?)\s*$")


def handle(ctx, **_args) -> str:
    try:
        text = ctx.fs_read("memory/tasks.md")
    except FileNotFoundError:
        return "Задач пока нет"
    tasks: list[str] = []
    for line in text.splitlines():
        match = _ENTRY_RE.match(line)
        if match and match.group(1).strip().lower() == "open":
            tasks.append(match.group(2).strip())
    if not tasks:
        return "Задач пока нет"
    listed = ", ".join(f"{index}) {what}" for index, what in enumerate(tasks, 1))
    return f"Открытых задач: {len(tasks)}. {listed}"
