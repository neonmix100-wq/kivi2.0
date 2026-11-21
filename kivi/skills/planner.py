"""Планировщик умений: команда -> скилл, аргументы и пробелы.

Стратегии (config.yaml -> planner.strategy):
  * llm_with_fallback — сначала LLM-планировщик, при сбое -> старый difflib;
  * llm_only          — только LLM, без difflib;
  * fuzzy_only        — только difflib (офлайн, тесты).

Если умения нет, план получает route:
  * chat       — разговор/генерация, пробел НЕ пишется;
  * search     — живые данные, пробел пишет loop только если поиск пуст;
  * action_gap — действие без инструмента, пробел пишется здесь.

Использование:
    from kivi.skills.planner import Planner, get_top_gaps
    planner = Planner()                       # общий реестр проекта
    found = planner.match("который час")      # (manifest, {}) | None
    plan = planner.plan("который час")
    # {"skill": manifest|None, "args": {...}, "missing": [...],
    #  "route": "skill|chat|search|action_gap", "source": "llm"}
    get_top_gaps(10)                          # топ пробелов по частоте
"""

from __future__ import annotations

from typing import Any

from loguru import logger

from kivi.skills.gaps import GapLog, get_gap_log
from kivi.skills.llm_planner import (
    DEFAULT_CATALOG_TOKENS,
    last_call,
    plan_with_llm,
)
from kivi.skills.registry import SkillManifest, SkillRegistry, get_registry

__all__ = [
    "DEFAULT_MIN_SCORE",
    "DEFAULT_STRATEGY",
    "STRATEGIES",
    "Planner",
    "get_top_gaps",
]

# Порог для planner: нечёткие совпадения слабее 0.75 не берём,
# чтобы «покажи фильм» не улетало в file_search.
DEFAULT_MIN_SCORE = 0.75

STRATEGIES = ("llm_with_fallback", "llm_only", "fuzzy_only")
DEFAULT_STRATEGY = "llm_with_fallback"


def _planner_config() -> dict[str, Any]:
    """Секция planner из config.yaml (пусто, если конфиг недоступен)."""
    try:
        from kivi.config import load_config

        return dict(load_config().model_dump().get("planner") or {})
    except Exception as exc:  # noqa: BLE001 - конфиг не должен ронять планировщик
        logger.debug("планировщик: planner.* не прочитан: {}", exc)
        return {}


def get_top_gaps(limit: int = 10) -> list[dict[str, Any]]:
    """Топ пробелов в умениях по частоте — для отчётов и будущего UI."""
    return get_gap_log().stats_by_intent(top=max(1, int(limit)))



class Planner:
    """Выбирает умение под команду: LLM-планировщик + difflib-фолбэк."""

    def __init__(
        self,
        registry: SkillRegistry | None = None,
        *,
        min_score: float = DEFAULT_MIN_SCORE,
        strategy: str | None = None,
        router: Any | None = None,
        gaps: GapLog | None = None,
        catalog_max_tokens: int | None = None,
        fallback_on_parse_error: bool | None = None,
    ) -> None:
        config = _planner_config()
        self.registry = registry if registry is not None else get_registry()
        self.min_score = float(min_score)

        chosen = str(strategy or config.get("strategy") or DEFAULT_STRATEGY).strip()
        if chosen not in STRATEGIES:
            raise ValueError(
                f"planner.strategy={chosen!r}: ожидалось одно из {STRATEGIES}"
            )
        self.strategy = chosen

        try:
            tokens = int(config.get("catalog_max_tokens") or DEFAULT_CATALOG_TOKENS)
        except (TypeError, ValueError):
            tokens = DEFAULT_CATALOG_TOKENS
        self.catalog_max_tokens = int(catalog_max_tokens or tokens)

        if fallback_on_parse_error is None:
            fallback_on_parse_error = bool(config.get("fallback_on_parse_error", True))
        self.fallback_on_parse_error = bool(fallback_on_parse_error)

        self.router = router
        self.gaps = gaps if gaps is not None else get_gap_log()

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

    def plan(
        self, command: str, history: list[dict] | None = None
    ) -> dict[str, Any]:
        """План обработки команды: скилл+аргументы или missing с причиной."""
        text = str(command or "").strip()
        if not text:
            return {
                "skill": None,
                "args": {},
                "missing": [],
                "source": "none",
                "route": "chat",
                "gap_id": None,
            }

        missing: list[dict[str, str]] = []
        attempted: str | None = None

        if self.strategy != "fuzzy_only":
            skill_name, args, llm_missing = plan_with_llm(
                text,
                history=history,
                registry=self.registry,
                router=self.router,
                max_tokens=self.catalog_max_tokens,
            )
            meta = last_call()
            if skill_name is not None:
                manifest = self.registry.get(skill_name)
                if manifest is not None and manifest.enabled:
                    return {
                        "skill": manifest,
                        "args": dict(args or {}),
                        "missing": [],
                        "source": "llm",
                        "route": "skill",
                        "gap_id": None,
                    }
                logger.warning(
                    "планировщик: скилл {!r} пропал из реестра — ответ невалиден",
                    skill_name,
                )

            attempted = meta.get("skill_attempted")
            if meta.get("ok"):
                route = str(meta.get("route") or "")
                if route not in ("search", "action_gap", "chat"):
                    route = "action_gap" if llm_missing else "chat"
                if route == "action_gap":
                    missing = list(llm_missing) or [
                        {"capability": text, "why": "нет умения для действия"}
                    ]
                else:
                    # chat и search закрываются без записи пробела.
                    missing = []
                return self._missing_plan(
                    text, missing, attempted, source="llm", route=route
                )
            if self.strategy == "llm_only" or not self.fallback_on_parse_error:
                reason = str(meta.get("reason") or "llm_error")
                missing = list(llm_missing) or [
                    {"capability": text, "why": f"планировщик не ответил: {reason}"}
                ]
                return self._missing_plan(
                    text, missing, attempted, source="none", route="action_gap"
                )
            logger.info(
                "планировщик: LLM не дала валидный план ({}) — пробую difflib",
                meta.get("reason"),
            )

        found = self.match(text)
        if found is not None:
            manifest, args = found
            return {
                "skill": manifest,
                "args": args,
                "missing": [],
                "source": "fuzzy",
                "route": "skill",
                "gap_id": None,
            }

        if not missing:
            missing = [{"capability": text, "why": "нет подходящего скилла"}]
        return self._missing_plan(
            text, missing, attempted, source="none", route="action_gap"
        )

    def _missing_plan(
        self,
        command: str,
        missing: list[dict[str, str]],
        attempted: str | None,
        *,
        source: str,
        route: str,
    ) -> dict[str, Any]:
        """Разговор/поиск — без пробела; action_gap — записываем и возвращаем id."""
        gap_id: int | None = None
        if route == "action_gap":
            if not missing:
                missing = [{"capability": command, "why": "нет умения для действия"}]
            gap_id = self._record_gap(command, missing, attempted)
        else:
            missing = []
        return {
            "skill": None,
            "args": {},
            "missing": missing,
            "source": source,
            "route": route,
            "gap_id": gap_id,
        }

    def _record_gap(
        self,
        command: str,
        missing: list[dict[str, str]],
        attempted: str | None = None,
    ) -> int | None:
        """Записать пробел в capability_gaps. Возвращает id строки (None при сбое)."""
        entry = missing[0] if missing else {}
        capability = str(entry.get("capability") or command).strip() or command
        why = str(entry.get("why") or "нет подходящего скилла").strip()
        try:
            return self.gaps.record_gap(
                command=command,
                user_intent=capability,
                skill_attempted=attempted,
                why=why,
            )
        except Exception as exc:  # noqa: BLE001 - журнал не критичен для ответа
            logger.warning("планировщик: не удалось записать пробел: {}", exc)
            return None
