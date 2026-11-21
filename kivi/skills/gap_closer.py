"""Закрытие пробелов в умениях: реактивация, композиция, вопросы.

Пробел (capability_gaps) — просьба, под которую планировщик не нашёл умения.
Если просили часто (gap_closer.min_frequency), пробуем закрыть пробел:

  1. ReactivateStrategy — включить похожее выключенное умение (difflib по имени,
     триггерам и намерениям; порог gap_closer.reactivate_threshold);
  2. ComposeStrategy    — LLM (task_type=intent) предлагает композицию из
     существующих умений: {"skills": [...], "how": "..."};
  3. AskUserStrategy    — LLM (task_type=chat) даёт 1-2 уточняющих вопроса.

Первая успешная стратегия закрывает пробел: closed_how = reactivate|compose.
Если успеха нет — пробел закрывается как ask (вопросы уже заданы), чтобы
не переспрашивать одно и то же каждый раз.

Использование:
    from kivi.skills.gap_closer import GapCloser
    closer = GapCloser(router=router, registry=registry)
    success, how, message = closer.attempt_close(gap_id)
"""

from __future__ import annotations

import json
from difflib import SequenceMatcher
from pathlib import Path
from typing import Any

import yaml
from loguru import logger
from pydantic import BaseModel, ConfigDict, Field, ValidationError

from kivi.config import CONFIG_PATH
from kivi.skills.gaps import GapLog, get_gap_log
from kivi.skills.llm_planner import build_catalog
from kivi.skills.registry import (
    SkillManifest,
    SkillRegistry,
    get_registry,
    normalize_text,
)

__all__ = [
    "AskUserStrategy",
    "ComposeStrategy",
    "DEFAULT_CATALOG_TOKENS",
    "GapCloser",
    "GapCloserConfig",
    "ReactivateStrategy",
    "load_gap_config",
    "skill_similarity",
]

DEFAULT_CATALOG_TOKENS = 400


class GapCloserConfig(BaseModel):
    """Секция gap_closer из config.yaml."""

    model_config = ConfigDict(extra="allow")

    min_frequency: int = Field(default=3, ge=1)
    reactivate_threshold: float = Field(default=0.7, ge=0.0, le=1.0)
    compose_enabled: bool = True
    ask_enabled: bool = True


def load_gap_config(path: Path | None = None) -> dict[str, Any]:
    """Секция gap_closer из config.yaml (битый конфиг — пусто + WARNING)."""
    config_path = Path(path) if path is not None else CONFIG_PATH
    try:
        raw = yaml.safe_load(config_path.read_text(encoding="utf-8")) or {}
    except (OSError, yaml.YAMLError) as exc:
        logger.warning("пробелы: не читается {}: {}", config_path.name, exc)
        return {}
    section = raw.get("gap_closer") if isinstance(raw, dict) else None
    return dict(section) if isinstance(section, dict) else {}


def _parse_json_object(raw: str) -> dict[str, Any] | None:
    """Достать JSON-объект из ответа LLM (снимаем ```json-обёртку)."""
    text = str(raw or "").strip()
    if not text:
        return None
    if text.startswith("```"):
        text = "\n".join(
            line for line in text.splitlines() if not line.strip().startswith("```")
        ).strip()
    candidates = [text]
    if "{" in text and "}" in text:
        candidates.append(text[text.find("{") : text.rfind("}") + 1])
    for candidate in candidates:
        if not candidate:
            continue
        try:
            payload = json.loads(candidate)
        except json.JSONDecodeError:
            continue
        if isinstance(payload, dict):
            return payload
    return None


def _pair_score(left: str, right: str) -> float:
    """Сходство двух фраз: точное > словами > подстрокой > difflib."""
    first, second = normalize_text(left), normalize_text(right)
    if not first or not second:
        return 0.0
    if first == second:
        return 1.0
    if f" {first} " in f" {second} " or f" {second} " in f" {first} ":
        return 0.95
    if (first in second or second in first) and min(len(first), len(second)) >= 4:
        return 0.9
    return SequenceMatcher(None, first, second).ratio()


def skill_similarity(gap: dict[str, Any], manifest: SkillManifest) -> float:
    """Похоже ли умение на пробел: сравниваем с именем, триггерами и намерениями."""
    needles = [
        str(gap.get("command") or ""),
        str(gap.get("user_intent") or ""),
    ]
    needles = [item for item in needles if normalize_text(item)]
    haystacks = [manifest.name, *manifest.triggers, *manifest.intents, manifest.description]
    best = 0.0
    for needle in needles:
        for haystack in haystacks:
            score = _pair_score(needle, haystack)
            if score > best:
                best = score
    return best


# --- стратегии ---


class ReactivateStrategy:
    """Включить похожее выключенное умение (после автовыхода или ручного off)."""

    name = "reactivate"

    def __init__(self, registry: SkillRegistry, *, threshold: float = 0.7) -> None:
        self.registry = registry
        self.threshold = float(threshold)

    def attempt(self, gap: dict[str, Any]) -> tuple[bool, str, str]:
        best: SkillManifest | None = None
        best_score = 0.0
        for manifest in self.registry.list_all():
            disabled = not manifest.enabled or self.registry.should_disable(manifest.name)
            if not disabled:
                continue
            score = skill_similarity(gap, manifest)
            if score > best_score:
                best, best_score = manifest, score

        if best is None or best_score < self.threshold:
            logger.debug(
                "пробел {}: похожего выключенного умения нет (лучшее {:.2f})",
                gap.get("id"),
                best_score,
            )
            return False, self.name, ""
        if not self.registry.enable(best.name):
            return False, self.name, ""
        message = f"Включила умение «{best.name}» — попробуй ещё раз."
        logger.info("пробел {}: реактивировано умение {}", gap.get("id"), best.name)
        return True, self.name, message


COMPOSE_PROMPT = """Ты — сборщик умений ассистента Киви. Предложи композицию из \
существующих умений для задачи: {task}.
Каталог умений:
{catalog}
Ответь СТРОГО JSON без markdown:
{{"skills": ["имя_умения", ...], "how": "как использовать по шагам"}}
Если композиция невозможна — skills: []."""


class ComposeStrategy:
    """LLM предлагает собрать задачу из существующих умений."""

    name = "compose"

    def __init__(
        self,
        router: Any | None,
        registry: SkillRegistry,
        *,
        enabled: bool = True,
        max_catalog_tokens: int = DEFAULT_CATALOG_TOKENS,
    ) -> None:
        self.router = router
        self.registry = registry
        self.enabled = bool(enabled)
        self.max_catalog_tokens = int(max_catalog_tokens)

    def attempt(self, gap: dict[str, Any]) -> tuple[bool, str, str]:
        if not self.enabled or self.router is None:
            return False, self.name, ""
        task = str(gap.get("user_intent") or gap.get("command") or "").strip()
        if not task:
            return False, self.name, ""

        catalog = build_catalog(self.registry, self.max_catalog_tokens)
        system = COMPOSE_PROMPT.format(task=task, catalog=catalog or "(каталог пуст)")
        messages = [
            {"role": "system", "content": system},
            {"role": "user", "content": task},
        ]
        try:
            result = self.router.complete(messages, task_type="intent")
        except Exception as exc:  # noqa: BLE001 - сеть/бюджет/провайдер
            logger.warning(
                "пробелы: композиция недоступна: {}: {}", type(exc).__name__, exc
            )
            return False, self.name, ""

        payload = _parse_json_object(str((result or {}).get("text") or ""))
        if payload is None:
            logger.warning("пробелы: композиция — невалидный JSON от LLM")
            return False, self.name, ""
        skills = [
            str(item).strip()
            for item in payload.get("skills") or []
            if str(item).strip()
        ]
        if not skills:
            logger.info("пробел {}: композиция не найдена", gap.get("id"))
            return False, self.name, ""

        how = " ".join(str(payload.get("how") or "").split())
        message = "Могу собрать из умений: " + ", ".join(skills[:5]) + "."
        if how:
            message += f" Как: {how[:200]}"
        logger.info("пробел {}: композиция {}", gap.get("id"), ", ".join(skills[:5]))
        return True, self.name, message


ASK_PROMPT = """Ты — Киви, голосовой ассистент. Задай пользователю уточняющие \
вопросы, чтобы понять, как закрыть пробел: {task}.
Максимум 2 вопроса, короткие, на русском, без нумерации.
Ответь СТРОГО JSON без markdown:
{{"questions": ["...", "..."]}}"""


class AskUserStrategy:
    """Уточняющие вопросы пользователю через голос (ответа пока не ждём)."""

    name = "ask"

    def __init__(self, router: Any | None, *, enabled: bool = True) -> None:
        self.router = router
        self.enabled = bool(enabled)

    def attempt(self, gap: dict[str, Any]) -> tuple[bool, str, str]:
        task = str(gap.get("user_intent") or gap.get("command") or "").strip()
        questions: list[str] = []
        if self.enabled and self.router is not None and task:
            system = ASK_PROMPT.format(task=task)
            messages = [
                {"role": "system", "content": system},
                {"role": "user", "content": task},
            ]
            try:
                result = self.router.complete(messages, task_type="chat")
            except Exception as exc:  # noqa: BLE001 - сеть/бюджет/провайдер
                logger.warning(
                    "пробелы: вопросы недоступны: {}: {}", type(exc).__name__, exc
                )
            else:
                payload = _parse_json_object(str((result or {}).get("text") or ""))
                if payload is not None:
                    questions = [
                        " ".join(str(item).split())
                        for item in payload.get("questions") or []
                        if str(item).strip()
                    ][:2]
                else:
                    logger.warning("пробелы: вопросы — невалидный JSON от LLM")

        if not questions:
            questions = [
                "Уточни, пожалуйста, что именно нужно и как это должно работать?"
            ]
        message = "Задала вопросы: " + " ".join(questions)
        logger.info("пробел {}: задано вопросов {}", gap.get("id"), len(questions))
        return False, self.name, message


# --- оркестратор ---


class GapCloser:
    """Пробует стратегии по порядку и закрывает пробел первой успешной."""

    def __init__(
        self,
        router: Any | None = None,
        registry: SkillRegistry | None = None,
        gaps: GapLog | None = None,
        config: dict[str, Any] | GapCloserConfig | None = None,
        *,
        max_catalog_tokens: int = DEFAULT_CATALOG_TOKENS,
    ) -> None:
        if isinstance(config, GapCloserConfig):
            self.config = config
        else:
            raw = dict(config) if config is not None else load_gap_config()
            try:
                self.config = GapCloserConfig.model_validate(raw)
            except ValidationError as exc:
                logger.warning(
                    "пробелы: конфиг gap_closer невалиден ({}), беру умолчания", exc
                )
                self.config = GapCloserConfig()

        self.router = router
        self.registry = registry if registry is not None else get_registry()
        self.gaps = gaps if gaps is not None else get_gap_log()
        self.strategies = [
            ReactivateStrategy(
                self.registry, threshold=self.config.reactivate_threshold
            ),
            ComposeStrategy(
                self.router,
                self.registry,
                enabled=self.config.compose_enabled,
                max_catalog_tokens=max_catalog_tokens,
            ),
            AskUserStrategy(self.router, enabled=self.config.ask_enabled),
        ]

    def attempt_close(self, gap_id: int) -> tuple[bool, str, str]:
        """Закрыть пробел: (успех, чем закрыли, текст для озвучки)."""
        gap = self.gaps.get(gap_id)
        if gap is None:
            logger.warning("пробел {}: записи нет", gap_id)
            return False, "none", f"Пробел {gap_id} не найден."

        last: tuple[bool, str, str] = (False, "none", "")
        for strategy in self.strategies:
            success, how, message = strategy.attempt(gap)
            logger.info("пробел {}: стратегия {} -> {}", gap_id, strategy.name, success)
            last = (success, how, message)
            if success:
                self.gaps.close(gap_id, how=how, notes=message)
                return success, how, message

        # Успеха нет: вопросы уже заданы (ask) — закрываем, чтобы не повторяться.
        if last[1] != "none" or last[2]:
            self.gaps.close(gap_id, how=last[1], notes=last[2])
        return last
