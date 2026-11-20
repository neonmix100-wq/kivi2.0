"""Подсистема скиллов Kivi.

Скилл — папка с манифестом skill.yaml и точкой входа handler.py:handle(ctx, **args).
Прямого доступа к ОС у скилла нет: только методы SkillContext (брокер),
которые сверяют каждое действие с permissions манифеста.
"""

from kivi.skills.broker import PermissionDenied, SkillContext
from kivi.skills.loader import SkillLoadError, invoke, load
from kivi.skills.planner import DEFAULT_MIN_SCORE, Planner
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

__all__ = [
    "DEFAULT_MIN_SCORE",
    "PermissionDenied",
    "Planner",
    "SkillContext",
    "SkillLoadError",
    "SkillManifest",
    "SkillPermissions",
    "SkillRegistry",
    "SkillRequires",
    "SkillSource",
    "SkillStats",
    "get_registry",
    "invoke",
    "load",
    "load_skills_config",
]

