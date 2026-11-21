"""Подсистема скиллов Kivi.

Скилл — папка с манифестом skill.yaml и точкой входа handler.py:handle(ctx, **args).
Прямого доступа к ОС у скилла нет: только методы SkillContext (брокер),
которые сверяют каждое действие с permissions манифеста.
"""

from kivi.skills.broker import PermissionDenied, SkillContext
from kivi.skills.gap_closer import (
    AskUserStrategy,
    ComposeStrategy,
    GapCloser,
    GapCloserConfig,
    ReactivateStrategy,
    SearchStrategy,
    load_gap_config,
)
from kivi.skills.gaps import GapLog, get_gap_log
from kivi.skills.llm_planner import build_catalog, parse_response, plan_with_llm
from kivi.skills.loader import SkillLoadError, invoke, load
from kivi.skills.planner import (
    DEFAULT_MIN_SCORE,
    STRATEGIES,
    Planner,
    get_top_gaps,
)
from kivi.skills.registry import (
    SkillManifest,
    SkillPermissions,
    SkillRegistry,
    SkillRequires,
    SkillSource,
    SkillStats,
    get_registry,
    load_skills_config,
)
from kivi.skills.search import (
    NO_DATA_ANSWER,
    NOT_FOUND,
    SearchConfig,
    answer_search,
    load_search_config,
    looks_factual,
    reformulate_query,
    search_web,
    summarize_with_llm,
)

__all__ = [
    "DEFAULT_MIN_SCORE",
    "STRATEGIES",
    "NO_DATA_ANSWER",
    "NOT_FOUND",
    "AskUserStrategy",
    "ComposeStrategy",
    "GapCloser",
    "GapCloserConfig",
    "GapLog",
    "PermissionDenied",
    "Planner",
    "ReactivateStrategy",
    "SearchStrategy",
    "SkillContext",
    "SkillLoadError",
    "SkillManifest",
    "SkillPermissions",
    "SkillRegistry",
    "SkillRequires",
    "SkillSource",
    "SkillStats",
    "build_catalog",
    "answer_search",
    "get_gap_log",
    "get_registry",
    "get_top_gaps",
    "invoke",
    "load",
    "load_gap_config",
    "load_search_config",
    "load_skills_config",
    "looks_factual",
    "parse_response",
    "plan_with_llm",
    "reformulate_query",
    "search_web",
    "summarize_with_llm",
]

