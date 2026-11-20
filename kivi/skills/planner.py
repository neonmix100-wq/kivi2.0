"""Планировщик: команда пользователя -> скилл и аргументы. Без LLM.

Использование:
    from kivi.skills.planner import Planner
    planner = Planner()                       # общий реестр проекта
    found = planner.match("который час")      # (manifest, {}) | None
    plan = planner.plan("который час")
    # {"skill": manifest|None, "args": {...}, "missing": [...]}

Аргументы пока пустые: скиллы сами разбирают фразу (pattern и т.п. появятся
на следующем шаге). Если скилла нет — plan["missing"] описывает, чего не хватает,
чтобы Router (LLM) мог честно ответить.
"""

from __future__ import annotations

from typing import Any

from loguru import logger

from kivi.skills.registry import SkillManifest, SkillRegistry, get_registry

__all__ = ["DEFAULT_MIN_SCORE", "Planner"]

# Порог для planner: нечёткие совпадения слабее 0.75 не берём,
# чтобы «покажи фильм» не улетало в file_search.
DEFAULT_MIN_SCORE = 0.75


class Planner:
    """Выбирает скилл под команду по реестру (triggers/intents + difflib)."""

    def __init__(
        self,
        registry: SkillRegistry | None = None,
        *,
        min_score: float = DEFAULT_MIN_SCORE,
    ) -> None:
        self.registry = registry if registry is not None else get_registry()
        self.min_score = float(min_score)

    def match(self, command: str) -> tuple[SkillManifest, dict[str, Any]] | None:
        """Топ-1 скилл, если оценка не ниже порога и он не отключён. Иначе None."""
        for manifest, score in self.registry.search(command):
            if score < self.min_score:
                break  # список отсортирован по убыванию оценки
            if self.registry.should_disable(manifest.name):
                logger.warning(
                    "скилл {} пропущен: отключён после ошибок подряд", manifest.name
                )
                continue
            return manifest, {}
        return None

    def plan(self, command: str) -> dict[str, Any]:
        """План обработки команды: скилл+аргументы или missing с причиной."""
        text = str(command or "").strip()
        found = self.match(text) if text else None
        if found is None:
            return {
                "skill": None,
                "args": {},
                "missing": [{"capability": text, "why": "нет подходящего скилла"}],
            }
        manifest, args = found
        return {"skill": manifest, "args": args, "missing": []}
