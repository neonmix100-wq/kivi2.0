"""Скилл memory_manage: очистка/закрытие/удаление записей памяти."""
from __future__ import annotations

import re

_ENTRY_RE = re.compile(r"^\s*[-*]\s*\[([^\]]+)\]\s*(.+?)\s*$")
_TARGETS = {
    "schedule": "schedule",
    "расписание": "schedule",
    "tasks": "tasks",
    "task": "tasks",
    "задачи": "tasks",
    "reminders": "reminders",
    "reminders.json": "reminders",
    "напоминания": "reminders",
}
_LABELS = {"schedule": "расписание", "tasks": "задачи", "reminders": "напоминания"}
_FILES = {"schedule": "schedule.md", "tasks": "tasks.md"}


def _clean(value: object) -> str:
    return " ".join(str(value or "").split())


def _target(value: object) -> str | None:
    key = _clean(value).lower().replace("ё", "е")
    return _TARGETS.get(key)


def _read(ctx, target: str) -> str:
    try:
        return ctx.fs_read(f"memory/{_FILES[target]}")
    except FileNotFoundError:
        return ""


def _write(ctx, target: str, text: str) -> None:
    ctx.fs_write(f"memory/{_FILES[target]}", text)


def _header(text: str, default: str) -> str:
    for line in str(text or "").splitlines():
        if line.strip().startswith("#"):
            return line.strip()
    return default


def _confirmed(ctx, title: str, body: str) -> bool:
    try:
        return bool(ctx.confirm(title, body))
    except Exception:  # noqa: BLE001 - нет UI = отказ по безопасности
        return False


def _clear(ctx, target: str) -> str:
    label = _LABELS.get(target, target)
    if not _confirmed(ctx, f"Очистить {label}?", f"Будут удалены все записи: {label}."):
        return "Хорошо, не трогаю."
    if target == "reminders":
        removed = int(ctx.reminders_clear())
        return f"Очистила напоминания (удалено: {removed})."
    text = _read(ctx, target)
    header = _header(text, "# Расписание" if target == "schedule" else "# Задачи")
    _write(ctx, target, header + "\n")
    return f"Очистила {label}."


def handle(ctx, target=None, action=None, what=None, **_: object) -> str:
    target_key = _target(target)
    action_key = _clean(action).lower()
    if action_key not in ("clear", "done", "delete"):
        return "Уточни: очистить, закрыть или удалить задачу?"
    if target_key is None:
        return "Уточни: расписание, задачи или напоминания?"

    if action_key == "clear" or (target_key != "tasks" and action_key == "delete"):
        return _clear(ctx, target_key)
    if target_key != "tasks":
        return "Закрыть или удалить можно только задачу."

    what_text = _clean(what)
    if not what_text:
        return "Уточни: какую задачу?"
    needle = what_text.lower().replace("ё", "е")
    text = _read(ctx, "tasks")
    lines = text.splitlines()
    matched = [
        line
        for line in lines
        if (match := _ENTRY_RE.match(line))
        and needle in match.group(2).strip().lower().replace("ё", "е")
    ]
    if not matched:
        return f"Не нашла задачу про {what_text}."

    if action_key == "delete":
        if not _confirmed(
            ctx,
            "Удалить задачу?",
            f"Будут удалены задачи со словами «{what_text}»: {len(matched)}.",
        ):
            return "Хорошо, не трогаю."
        updated = [line for line in lines if line not in matched]
        _write(ctx, "tasks", "\n".join(updated).rstrip() + "\n")
        return f"Удалила задачу: {what_text}."

    updated_lines: list[str] = []
    for line in lines:
        if line in matched:
            line = re.sub(r"^(\s*[-*]\s*\[)open(\])", r"\1done\2", line, flags=re.IGNORECASE)
        updated_lines.append(line)
    _write(ctx, "tasks", "\n".join(updated_lines).rstrip() + "\n")
    return f"Отметила задачу выполненной: {what_text}."
