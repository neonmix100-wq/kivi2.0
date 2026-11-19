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
    "ensure_vault",
    "fact_exists",
    "iter_facts",
    "list_notes",
    "normalize_fact",
    "note_path",
    "read_note",
    "slugify",
    "vault_root",
    "write_note",
]

VAULT_KINDS = ("profile", "journal", "topics", "people")
_FACT_RE = re.compile(r"^\s*[-*]\s*\[(\d{4}-\d{2}-\d{2})\]\s*(.+?)\s*$")


def vault_root(path: str | Path | None = None) -> Path:
    """Папка vault: аргумент -> config memory.vault_path -> memory/ в проекте."""
    if path is not None:
        root = Path(path)
    else:
        raw = str(get_settings().section("memory").get("vault_path") or "memory")
        root = Path(raw)
    return root if root.is_absolute() else PROJECT_ROOT / root


def ensure_vault(path: str | Path | None = None) -> Path:
    """Создать папки и пустой profile.md, если vault ещё нет."""
    root = vault_root(path)
    for sub in ("journal", "topics", "people"):
        (root / sub).mkdir(parents=True, exist_ok=True)
    profile = root / "profile.md"
    if not profile.exists():
        write_note("profile", "", "# Профиль Лёши\n", path=root)
        logger.info("Vault: создан {}", root)
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
