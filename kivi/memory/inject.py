"""Сборка блока памяти для системного промпта.

Порядок:
  1) ЛОКАЛЬНОЕ ЯДРО всегда и сразу (без сети): profile.md + schedule.md +
     tasks.md (open), обрезка ~600 токенов. При непустом vault блок не пустой;
  2) семантическое обогащение (top-k фактов по эмбеддингам) — только если
     укладывается в SEMANTIC_BUDGET_S (0.8 с). Иначе пропускается ТОЛЬКО
     обогащение, ядро остаётся.

Пустой vault -> пустая строка, без ошибок и WARNING-спама.
"""

from __future__ import annotations

from concurrent.futures import ThreadPoolExecutor
from concurrent.futures import TimeoutError as FuturesTimeoutError
from pathlib import Path

from kivi.config import get_settings
from kivi.log import logger
from kivi.memory.retrieve import MemoryRetriever
from kivi.memory.vault import iter_facts, iter_schedule, iter_tasks, read_note, vault_root

__all__ = [
    "CORE_TOKENS",
    "MAX_FACTS_TOKENS",
    "MAX_PROFILE_TOKENS",
    "SEMANTIC_BUDGET_S",
    "build",
    "build_block",
    "build_memory_block",
]

MAX_PROFILE_TOKENS = 400
MAX_FACTS_TOKENS = 400
CORE_TOKENS = 600  # ядро: профиль + расписание + открытые задачи
SEMANTIC_BUDGET_S = 0.8
CHARS_PER_TOKEN = 3  # та же грубая оценка, что в session.py


def _trim(text: str, max_tokens: int) -> str:
    limit = max_tokens * CHARS_PER_TOKEN
    if len(text) <= limit:
        return text.strip()
    return text[:limit].rsplit("\n", 1)[0].strip()


def _core_block(root: Path) -> str:
    """Локальное ядро памяти без сети: профиль, расписание, открытые задачи."""
    lines: list[str] = []
    profile = read_note("profile", "", root).strip()
    for line in profile.splitlines():
        text = line.strip()
        if not text or text.startswith("#"):
            continue
        lines.append(text)

    schedule = [f"[{when}] {what}" for when, what in iter_schedule(root)]
    if schedule:
        lines.append("Расписание:")
        lines.extend(f"- {item}" for item in schedule)

    tasks = [what for status, what in iter_tasks(root) if str(status) == "open"]
    if tasks:
        lines.append("Открытые задачи:")
        lines.extend(f"- {item}" for item in tasks)

    if not lines:
        # Профиль пуст, но факты есть: всё равно не отдаём пустой контекст.
        facts = [fact for _note, _day, fact in iter_facts(root)]
        lines = [f"- {fact}" for fact in facts if str(fact).strip()]

    if not lines:
        return ""
    return _trim("\n".join(lines), CORE_TOKENS)


def _retrieve_sync(query: str, root: Path, limit: int) -> list[str]:
    retriever = MemoryRetriever(vault_path=root, k=limit)
    return retriever.retrieve(query, k=limit)


def _semantic(query: str, root: Path, limit: int, budget_s: float) -> tuple[list[str], str]:
    """Семантика с жёстким бюджетом; не успели — skip только обогащения."""
    pool = ThreadPoolExecutor(max_workers=1, thread_name_prefix="kivi-memory-sem")
    try:
        future = pool.submit(_retrieve_sync, query, root, limit)
        try:
            facts = future.result(timeout=max(0.05, float(budget_s)))
        except FuturesTimeoutError:
            logger.info(
                "память: semantic skip (бюджет {:.1f}с) — остаётся локальное ядро",
                float(budget_s),
            )
            return [], "skip"
    finally:
        pool.shutdown(wait=False, cancel_futures=True)
    return [str(item) for item in list(facts or [])], "ok"


def build_memory_block(
    query: str | None = None,
    *,
    vault_path: str | Path | None = None,
    k: int | None = None,
    budget_s: float | None = None,
) -> str:
    """Блок памяти: локальное ядро + (если успело) семантическое обогащение."""
    try:
        root = vault_root(vault_path)
        if not root.exists():
            return ""
        core = _core_block(root)
        if not core:
            logger.debug("память: vault пуст — блок не нужен")
            return ""

        semantic: list[str] = []
        text = str(query or "").strip()
        limit_s = SEMANTIC_BUDGET_S if budget_s is None else float(budget_s)
        if text and limit_s > 0:
            try:
                cfg = get_settings().section("memory")
                limit = int(k if k is not None else (cfg.get("retrieve_k") or 4))
            except Exception:  # noqa: BLE001 - конфиг не должен ломать память
                limit = int(k or 4)
            semantic, _state = _semantic(text, root, limit, limit_s)

        parts: list[str] = [core]
        if semantic:
            block = "\n".join(f"- {fact}" for fact in semantic)
            trimmed = _trim(block, MAX_FACTS_TOKENS)
            if trimmed:
                parts.append("По смыслу к вопросу:\n" + trimmed)

        logger.info(
            "память: ядро {} симв, semantic {}",
            len(core),
            "ok" if semantic else "skip",
        )
        return "Память о Лёше:\n" + "\n".join(parts)
    except Exception as exc:  # noqa: BLE001 - память не должна ломать диалог
        logger.warning("Память: не удалось собрать блок: {}: {}", type(exc).__name__, exc)
        return ""


build = build_memory_block  # короткий алиас
build_block = build_memory_block  # имя из ТЗ

