"""Vault долгой памяти: обычные markdown-заметки, которые можно править руками.

Структура:
    memory/profile.md              устойчивые факты о пользователе
    memory/journal/2026-11-20.md   выжимка дня
    memory/topics/<slug>.md        долгоживущие темы
    memory/people/<slug>.md        люди

Факт — строка вида: "- [2026-11-20] у Лёши день рождения 5 марта".
"""

from __future__ import annotations

import os
import re
from datetime import date
from pathlib import Path
from typing import Iterator

from kivi.config import PROJECT_ROOT, get_settings
from kivi.log import logger

__all__ = [
    "VAULT_KINDS",
    "add_fact",
    "add_preference",
    "add_schedule",
    "add_schedule_line",
    "add_task",
    "ensure_vault",
    "fact_exists",
    "iter_facts",
    "iter_schedule",
    "iter_tasks",
    "list_notes",
    "normalize_fact",
    "normalize_item",
    "note_path",
    "read_note",
    "slugify",
    "vault_root",
    "write_note",
]

VAULT_KINDS = (
    "profile",
    "schedule",
    "tasks",
    "journal",
    "topics",
    "people",
)
_FACT_RE = re.compile(r"^\s*[-*]\s*\[(\d{4}-\d{2}-\d{2})\]\s*(.+?)\s*$")
_ENTRY_RE = re.compile(r"^\s*[-*]\s*\[([^\]]+)\]\s*(.+?)\s*$")


def vault_root(path: str | Path | None = None) -> Path:
    """Папка vault: аргумент -> config memory.vault_path -> memory/ в проекте."""
    if path is not None:
        root = Path(path)
    else:
        raw = str(get_settings().section("memory").get("vault_path") or "memory")
        root = Path(raw)
    return root if root.is_absolute() else PROJECT_ROOT / root


def ensure_vault(path: str | Path | None = None) -> Path:
    """Создать папки и базовые markdown-файлы, если vault ещё нет."""
    root = vault_root(path)
    created = False
    for sub in ("journal", "topics", "people"):
        (root / sub).mkdir(parents=True, exist_ok=True)
    for kind, title in (
        ("profile", "# Профиль Лёши"),
        ("schedule", "# Расписание"),
        ("tasks", "# Задачи"),
    ):
        note = root / f"{kind}.md"
        if not note.exists():
            write_note(kind, "", title + "\n", path=root)
            created = True
    if created:
        logger.info("Vault: подготовлен {}", root)
    return root


def slugify(title: str) -> str:
    """Человекочитаемый slug для имени файла."""
    text = str(title).lower().replace("ё", "е")
    text = re.sub(r"[^\w\s-]", "", text, flags=re.UNICODE)
    text = re.sub(r"[\s_-]+", "-", text).strip("-")
    return text or "note"


def note_path(kind: str, name: str = "", path: str | Path | None = None) -> Path:
    root = vault_root(path)
    if kind == "profile":
        return root / "profile.md"
    if kind in ("schedule", "tasks"):
        return root / f"{kind}.md"
    if kind == "journal":
        return root / "journal" / f"{name}.md"
    if kind in ("topic", "topics"):
        return root / "topics" / f"{slugify(name)}.md"
    if kind in ("person", "people"):
        return root / "people" / f"{slugify(name)}.md"
    raise ValueError(f"Vault: неизвестный тип заметки {kind!r}")


def read_note(kind: str, name: str = "", path: str | Path | None = None) -> str:
    note = note_path(kind, name, path)
    if not note.exists():
        return ""
    return note.read_text(encoding="utf-8", errors="replace")


def write_note(kind: str, name: str, content: str, path: str | Path | None = None) -> Path:
    """Записать заметку атомарно: временный .md рядом + os.replace."""
    note = note_path(kind, name, path)
    note.parent.mkdir(parents=True, exist_ok=True)
    tmp = note.with_name(f".tmp_{note.name}")
    tmp.write_text(content, encoding="utf-8", newline="\n")
    os.replace(tmp, note)
    return note


def list_notes(kind: str | None = None, path: str | Path | None = None) -> list[Path]:
    """Список заметок (kind: profile|journal|topics|people, None — все)."""
    root = vault_root(path)
    if kind is None:
        found = [p for p in root.rglob("*.md") if not p.name.startswith(".tmp_")]
    elif kind == "profile":
        found = [root / "profile.md"]
    elif kind in ("schedule", "tasks"):
        found = [root / f"{kind}.md"]
    else:
        folder = root / kind
        found = list(folder.glob("*.md")) if folder.is_dir() else []
    return sorted(p for p in found if p.exists() and not p.name.startswith(".tmp_"))


def normalize_fact(text: str) -> str:
    """Нормализация для дедупликации: без даты, регистра и пунктуации."""
    cleaned = re.sub(r"\[\d{4}-\d{2}-\d{2}\]", " ", str(text))
    cleaned = cleaned.lower().replace("ё", "е")
    cleaned = re.sub(r"[^\w\s]", " ", cleaned, flags=re.UNICODE)
    return " ".join(cleaned.split())


def normalize_item(text: str) -> str:
    """Нормализация строк расписания/задач: дата и время сохраняются цифрами."""
    cleaned = str(text).lower().replace("ё", "е")
    cleaned = re.sub(r"[^\w\s]", " ", cleaned, flags=re.UNICODE)
    return " ".join(cleaned.split())


def iter_schedule(path: str | Path | None = None) -> list[tuple[str, str]]:
    """Записи schedule.md как (when, what); файл предварительно канонизируется."""
    from kivi.memory.schedule import ensure_canonical

    note = note_path("schedule", "", path)
    if note.exists():
        ensure_canonical(note)
    text = read_note("schedule", "", path)
    found: list[tuple[str, str]] = []
    for line in text.splitlines():
        match = _ENTRY_RE.match(line)
        if match:
            found.append((match.group(1).strip(), match.group(2).strip()))
    return found


def iter_tasks(path: str | Path | None = None) -> list[tuple[str, str]]:
    """Записи tasks.md как (status, what)."""
    text = read_note("tasks", "", path)
    found: list[tuple[str, str]] = []
    for line in text.splitlines():
        match = _ENTRY_RE.match(line)
        if match:
            found.append((match.group(1).strip().lower(), match.group(2).strip()))
    return found


def _schedule_key(line: str) -> tuple:
    """Ключ дедупликации: у канонических строк — разобранное правило."""
    from kivi.memory.schedule import parse_rule

    rule = parse_rule(line)
    if rule is None:
        return ("raw", normalize_item(line))
    return (
        tuple(sorted(rule.days or [])),
        (rule.date.month, rule.date.day) if rule.date else None,
        rule.start,
        rule.end,
        normalize_item(rule.what),
    )


def add_schedule_line(line: str, path: str | Path | None = None) -> bool:
    """Добавить готовую строку: канон сохраняется как есть, старое мигрируется.

    Если строку не удалось нормализовать, текст НЕ теряется: пишется с
    префиксом «[?] » и WARNING (recall покажет её только в общем запросе).
    """
    from kivi.memory.schedule import ensure_canonical, migrate_legacy

    text = " ".join(str(line or "").split())
    if not text:
        return False
    canonical = migrate_legacy(text)
    if canonical is None:
        stripped = re.sub(r"^\s*[-*]\s*", "", text)
        canonical = f"- {stripped}" if stripped.startswith("[?]") else f"- [?] {stripped}"
        logger.warning("schedule: строка не нормализовалась, сохранил как «[?]»: {}", text[:120])
    root = ensure_vault(path)
    key = _schedule_key(canonical)
    note = note_path("schedule", "", root)
    if note.exists():
        ensure_canonical(note)
    content = read_note("schedule", "", root)
    for existing in content.splitlines():
        if _ENTRY_RE.match(existing) and _schedule_key(existing) == key:
            logger.debug("Vault: расписание уже есть — {}", canonical)
            return False
    if not content.strip():
        content = "# Расписание\n"
    if not content.endswith("\n"):
        content += "\n"
    content += canonical.strip() + "\n"
    write_note("schedule", "", content, root)
    logger.info("Vault: расписание добавлено {}", canonical[:140])
    return True


def add_schedule(when: str, what: str, path: str | Path | None = None) -> bool:
    """Добавить строку расписания; False — пусто или дубликат.

    `when` может быть каноническим («ПН-ПТ 15:00») или свободным
    («каждый будний день в 15:00»); нормализация — в kivi/memory/schedule.
    """
    when_text = " ".join(str(when or "").split())
    what_text = " ".join(str(what or "").split())
    if not when_text or not what_text:
        return False
    return add_schedule_line(f"{when_text} {what_text}", path)


def add_task(what: str, status: str = "open", path: str | Path | None = None) -> bool:
    """Добавить/закрыть задачу; done помечает существующую open."""
    what_text = " ".join(str(what or "").split())
    status_text = str(status or "open").strip().lower()
    if status_text not in ("open", "done"):
        status_text = "open"
    if not what_text:
        return False
    root = ensure_vault(path)
    target = normalize_item(what_text)
    content = read_note("tasks", "", root)
    lines = content.splitlines()
    for index, line in enumerate(lines):
        match = _ENTRY_RE.match(line)
        if not match:
            continue
        current_status = match.group(1).strip().lower()
        if normalize_item(match.group(2)) != target:
            continue
        if status_text == "done" and current_status != "done":
            lines[index] = f"- [done] {match.group(2).strip()}"
            write_note("tasks", "", "\n".join(lines).rstrip() + "\n", root)
            logger.info("Vault: задача закрыта — {}", what_text[:100])
            return True
        logger.debug("Vault: задача уже есть — [{}] {}", current_status, what_text)
        return False
    if not content.strip():
        content = "# Задачи\n"
    if not content.endswith("\n"):
        content += "\n"
    content += f"- [{status_text}] {what_text}\n"
    write_note("tasks", "", content, root)
    logger.info("Vault: задача добавлена [{}] {}", status_text, what_text[:100])
    return True


def add_preference(text: str, path: str | Path | None = None) -> bool:
    """Предпочтение в profile.md; повтор нормализованного текста не пишется."""
    preference = " ".join(str(text or "").split())
    if not preference:
        return False
    root = ensure_vault(path)
    if fact_exists(preference, root):
        logger.debug("Vault: предпочтение уже есть — {}", preference[:80])
        return False
    return add_fact(f"предпочтение: {preference}", kind="profile", path=root)


def iter_facts(
    path: str | Path | None = None, kind: str | None = None
) -> Iterator[tuple[Path, str, str]]:
    """Перебрать факты: (файл, дата, текст). Journal — хроника, а не источник фактов."""
    for note in list_notes(kind, path):
        if kind is None and note.parent.name == "journal":
            continue
        for line in note.read_text(encoding="utf-8", errors="replace").splitlines():
            match = _FACT_RE.match(line)
            if match:
                yield note, match.group(1), match.group(2).strip()


def fact_exists(text: str, path: str | Path | None = None) -> bool:
    """Такой факт уже записан (сравнение по нормализованной строке)?"""
    target = normalize_fact(text)
    if not target:
        return True
    return any(normalize_fact(fact) == target for _note, _day, fact in iter_facts(path))


def add_fact(
    text: str,
    *,
    kind: str = "profile",
    who: str = "",
    when: str | None = None,
    path: str | Path | None = None,
) -> bool:
    """Добавить факт в нужную заметку. False — если дубликат или пустая строка."""
    fact = " ".join(str(text).split())
    if not fact:
        return False
    root = ensure_vault(path)
    if fact_exists(fact, root):
        logger.debug("Vault: факт уже есть — {}", fact[:80])
        return False

    day = when or date.today().isoformat()
    if kind == "profile":
        note_kind, note_name = "profile", ""
    elif kind in ("person", "people"):
        note_kind, note_name = "person", who or "неизвестный"
    elif kind in ("topic", "topics"):
        note_kind, note_name = "topic", who or "общее"
    else:
        logger.warning("Vault: неизвестный kind={} — пишу в профиль", kind)
        note_kind, note_name = "profile", ""

    current = read_note(note_kind, note_name, root)
    if not current.strip():
        titles = {
            "profile": "# Профиль Лёши",
            "person": f"# {note_name}",
            "topic": f"# {note_name}",
        }
        current = titles[note_kind] + "\n"
    if not current.endswith("\n"):
        current += "\n"
    current += f"- [{day}] {fact}\n"
    write_note(note_kind, note_name, current, root)
    logger.info("Vault: факт записан [{}] {}", day, fact[:100])
    return True
