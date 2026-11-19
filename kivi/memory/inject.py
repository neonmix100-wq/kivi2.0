"""Сборка блока памяти для системного промпта.

Профиль (до ~400 токенов) + top-k релевантных фактов (до ~400 токенов).
Пустой vault -> пустая строка, без ошибок.
"""

from __future__ import annotations

from pathlib import Path

from kivi.config import get_settings
from kivi.log import logger
from kivi.memory.retrieve import MemoryRetriever
from kivi.memory.vault import iter_facts, list_notes, read_note, vault_root

__all__ = ["MAX_FACTS_TOKENS", "MAX_PROFILE_TOKENS", "build", "build_memory_block"]

MAX_PROFILE_TOKENS = 400
MAX_FACTS_TOKENS = 400
CHARS_PER_TOKEN = 3  # та же грубая оценка, что в session.py


def _trim(text: str, max_tokens: int) -> str:
    limit = max_tokens * CHARS_PER_TOKEN
    if len(text) <= limit:
        return text.strip()
    return text[:limit].rsplit("\n", 1)[0].strip()


def build_memory_block(
    query: str | None = None,
    *,
    vault_path: str | Path | None = None,
    k: int | None = None,
) -> str:
    """Собрать текстовый блок памяти (или пустую строку, если vault пуст)."""
    try:
        root = vault_root(vault_path)
        if not root.exists():
            return ""
        profile = read_note("profile", "", root).strip()
        # В профиле держим только строки-факты и заголовок, без мусора.
        profile_lines = [line for line in profile.splitlines() if line.strip()]
        profile_text = _trim("\n".join(profile_lines), MAX_PROFILE_TOKENS)

        facts: list[str] = []
        if query:
            cfg = get_settings().section("memory")
            limit = int(k if k is not None else (cfg.get("retrieve_k") or 4))
            retriever = MemoryRetriever(vault_path=root, k=limit)
            facts = retriever.retrieve(query, k=limit)
            if facts:
                logger.debug(
                    "Память: {} фактов ({})", len(facts), retriever.last_path
                )

        if not profile_text and not facts:
            if not list_notes(None, root):
                return ""
            # vault есть, но фактов нет — тоже пустой блок
            if not any(True for _ in iter_facts(root)):
                return ""

        parts: list[str] = []
        if profile_text:
            parts.append(profile_text)
        if facts:
            block = "\n".join(f"- {fact}" for fact in facts)
            parts.append(_trim(block, MAX_FACTS_TOKENS))
        return "Память о Лёше:\n" + "\n".join(parts) if parts else ""
    except Exception as exc:  # noqa: BLE001 - память не должна ломать диалог
        logger.warning("Память: не удалось собрать блок: {}: {}", type(exc).__name__, exc)
        return ""


build = build_memory_block  # короткий алиас
