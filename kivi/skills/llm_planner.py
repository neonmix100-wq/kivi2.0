"""LLM-планировщик умений: команда -> (skill_name | None, args, missing) + route.

Работает через Router.complete(task_type="intent"): тир cheap, без эскалации
(оркестратор защищён от переходов). Каталог умений собирается из registry
и вставляется в системный промпт.

Ответ модели — строго JSON с полем route:
  skill      — каталог покрывает команду (skill_name обязателен);
  chat       — генеративный/разговорный запрос (анекдот, объяснение, перевод):
               пробел НЕ записывается, missing пустой;
  search     — нужны живые внешние данные (курс, погода, новости, факты);
  action_gap — нужно действие над миром, а инструмента в каталоге нет.

Если JSON невалиден или модель выдумала имя скилла, возвращаем (None, {}, [])
и пишем причину в last_call(): planner.py уходит в difflib-фолбэк.

Использование:
    from kivi.skills.llm_planner import plan_with_llm, last_call
    skill_name, args, missing = plan_with_llm("который час")
    last_call()   # {"ok": True, "route": "skill", "cost_rub": ..., ...}
"""

from __future__ import annotations

import json
import time
from typing import Any

from loguru import logger

from kivi.skills.registry import SkillManifest, SkillRegistry, get_registry

__all__ = [
    "DEFAULT_CATALOG_TOKENS",
    "ROUTES",
    "SYSTEM_PROMPT",
    "build_catalog",
    "last_call",
    "parse_response",
    "plan_with_llm",
]

DEFAULT_CATALOG_TOKENS = 600
MAX_CATALOG_SKILLS = 30
CHARS_PER_TOKEN = 3  # грубая оценка для русского текста (как в Session)
ROUTES = ("skill", "search", "chat", "action_gap")

SYSTEM_PROMPT = """Ты — планировщик умений Киви. По команде и каталогу выбери маршрут.
Ответ — СТРОГО JSON без markdown:
{"skill_name": "имя" | null, "args": {}, "route": "skill|search|chat|action_gap",
 "missing": [{"capability": "...", "why": "..."}]}
Правила:
- skill — каталог покрывает команду (skill_name обязателен, missing=[]);
- chat — разговор/генерация: анекдот, история, объяснение, перевод, совет (missing=[]);
- search — живые данные: курс, погода, новости, цены, результаты, факты \
кто/что/когда/сколько (missing=[]);
- action_gap — действие над миром без инструмента: устройство, письмо, файл, \
покупка (capability=намерение).
- Если команда содержит «запомни/добавь в расписание/поставь задачу/запиши/ \
не забудь что» и в ней есть что запомнить — route=skill, skill_name=remember, \
НЕ action_gap. args: {kind: schedule|task|preference|fact, what, when?}. \
Если есть время/«каждый будний день»/«в 15:00» — kind=schedule, when=это время; \
если «задача/купи/сделать» — kind=task; если «люблю/предпочитаю» — preference; \
иначе fact.
- Если команда «напомни в/поставь напоминание на/разбуди в» — route=skill, \
skill_name=reminders, args: {time, what}.
- Если «напомни/разбуди» БЕЗ времени или даты (например, «напомни купить корм») — \
route=skill, skill_name=remember, args: {kind: task, what}.
- Если вопрос «во сколько ...» или «когда ...» про личные планы/записи/ \
напоминания (в том числе «стендап во сколько»), «что завтра», \
«что у меня завтра/сегодня» — route=skill, skill_name=recall.
- Если команда «мои задачи/что нужно сделать/список дел» — route=skill, \
skill_name=tasks_list.
- Если «что ты умеешь/что ты можешь/твои возможности/список умений» — \
route=skill, skill_name=capabilities.
- Если «выруби/выключи/поставь на паузу прослушку» или «не слушай» — \
route=skill, skill_name=listen_control, args: {action: pause}.
- Если «включи/возобнови прослушку/слушай снова» — route=skill, \
skill_name=listen_control, args: {action: resume}.
- Если «очисти/удали расписание|напоминания|задачу» — route=skill, \
skill_name=memory_manage, args: {target: schedule|reminders|tasks, \
action: clear|delete, what?}. Для «удали расписание/напоминания» action=clear. \
Для «закрой/отметь выполненной задачу» action=done.
Умения вне каталога не выдумывай."""

# Последний вызов: причина fallback, маршрут, стоимость, модель. Нужен planner.py и смоукам.
_LAST_CALL: dict[str, Any] = {
    "ok": False,
    "reason": "not_called",
    "route": None,
    "skill_name": None,
    "skill_attempted": None,
    "cost_rub": 0.0,
    "model": "",
    "ms": 0.0,
    "raw": "",
}


def last_call() -> dict[str, Any]:
    """Копия метаданных последнего вызова plan_with_llm()."""
    return dict(_LAST_CALL)


def _remember(**values: Any) -> None:
    _LAST_CALL.update(values)


def _planner_config() -> dict[str, Any]:
    """Секция planner из config.yaml (пусто, если конфиг недоступен)."""
    try:
        from kivi.config import load_config

        section = load_config().model_dump().get("planner") or {}
        return dict(section)
    except Exception as exc:  # noqa: BLE001 - конфиг не должен ронять планировщик
        logger.debug("планировщик: planner.* не прочитан: {}", exc)
        return {}


def _catalog_tokens() -> int:
    """Лимит каталога в токенах: planner.catalog_max_tokens или 600."""
    raw = _planner_config().get("catalog_max_tokens")
    try:
        value = int(raw) if raw is not None else DEFAULT_CATALOG_TOKENS
    except (TypeError, ValueError):
        value = DEFAULT_CATALOG_TOKENS
    return max(120, value)


def _use_count(registry: SkillRegistry, manifest: SkillManifest) -> int:
    """Сколько раз скилл уже вызывали (для приоритета в каталоге)."""
    try:
        stats = registry.stats(manifest.name)
    except Exception:  # noqa: BLE001 - статистика не критична
        return 0
    return int(stats.use_count) if stats is not None else 0


def build_catalog(
    registry: SkillRegistry | None = None, max_tokens: int | None = None
) -> str:
    """Каталог для промпта: по строке на скилл, top-30 по use_count.

    Формат строки: "- {name}: {description}. triggers: {первые 5}".
    Если строки не влезают в бюджет токенов — описание обрезается.
    """
    active = registry if registry is not None else get_registry()
    limit_tokens = max(60, int(max_tokens or _catalog_tokens()))
    budget_chars = limit_tokens * CHARS_PER_TOKEN

    skills = sorted(
        active.list_enabled(),
        key=lambda item: (-_use_count(active, item), item.name),
    )[:MAX_CATALOG_SKILLS]

    lines: list[str] = []
    used = 0
    for manifest in skills:
        triggers = ", ".join(manifest.triggers[:5])
        description = " ".join(manifest.description.split())
        tail = f". triggers: {triggers}"
        line = f"- {manifest.name}: {description}{tail}"
        if used + len(line) + 1 > budget_chars:
            room = budget_chars - used - len(manifest.name) - len(tail) - 8
            if room < 12:
                break  # бюджет каталога исчерпан
            line = f"- {manifest.name}: {description[:room].rstrip()}…{tail}"
        lines.append(line)
        used += len(line) + 1
    return "\n".join(lines)


def _extract_json(text: str) -> Any | None:
    """Достать JSON из ответа: снять ```json, найти первую {...} с балансом скобок."""
    raw = str(text or "").strip()
    if not raw:
        return None
    if raw.startswith("```"):
        raw = raw.strip("`").strip()
        if raw[:4].lower() == "json":
            raw = raw[4:].strip()
    try:
        return json.loads(raw)
    except (ValueError, TypeError):
        pass

    start = raw.find("{")
    if start < 0:
        return None
    depth = 0
    in_string = False
    escaped = False
    for index in range(start, len(raw)):
        char = raw[index]
        if in_string:
            if escaped:
                escaped = False
            elif char == "\\":
                escaped = True
            elif char == '"':
                in_string = False
            continue
        if char == '"':
            in_string = True
        elif char == "{":
            depth += 1
        elif char == "}":
            depth -= 1
            if depth == 0:
                try:
                    return json.loads(raw[start : index + 1])
                except (ValueError, TypeError):
                    return None
    return None


def _derive_route(
    route_raw: Any, skill_name: str | None, missing: list[dict[str, str]]
) -> str:
    """Валидный маршрут: skill побеждает; неизвестный route -> chat/action_gap."""
    if skill_name is not None:
        return "skill"
    route = str(route_raw).strip().lower() if isinstance(route_raw, str) else ""
    if route in ("chat", "search", "action_gap"):
        return route
    if route == "skill":
        # route=skill без валидного имени бессмыслен: действие или разговор.
        return "action_gap" if missing else "chat"
    return "action_gap" if missing else "chat"


def _validate(
    payload: Any, registry: SkillRegistry
) -> tuple[dict[str, Any] | None, str]:
    """Проверить поля ответа и наличие скилла. Возвращает (план | None, причина)."""
    if not isinstance(payload, dict) or "skill_name" not in payload:
        return None, "parse_error"

    skill_raw = payload.get("skill_name")
    if skill_raw is None:
        skill_name: str | None = None
    elif isinstance(skill_raw, str) and skill_raw.strip():
        skill_name = skill_raw.strip()
    else:
        return None, "parse_error"

    args = payload.get("args") or {}
    if not isinstance(args, dict):
        return None, "parse_error"

    missing_raw = payload.get("missing") or []
    if not isinstance(missing_raw, list):
        return None, "parse_error"

    missing: list[dict[str, str]] = []
    for item in missing_raw:
        if isinstance(item, str):
            capability = item.strip()
            if capability:
                missing.append({"capability": capability, "why": ""})
        elif isinstance(item, dict):
            capability = str(item.get("capability") or "").strip()
            if not capability:
                continue
            missing.append(
                {
                    "capability": capability,
                    "why": str(item.get("why") or "").strip(),
                }
            )
        else:
            return None, "parse_error"

    if skill_name is not None:
        manifest = registry.get(skill_name)
        if manifest is None or not manifest.enabled:
            return None, "unknown_skill"

    route = _derive_route(payload.get("route"), skill_name, missing)
    if route in ("chat", "search"):
        # Разговор и поиск закрываются другими путями — пробел не нужен.
        missing = []

    return {
        "skill_name": skill_name,
        "args": args,
        "missing": missing,
        "route": route,
    }, "ok"


def parse_response(
    text: str, registry: SkillRegistry | None = None
) -> dict[str, Any] | None:
    """JSON из ответа LLM -> план, либо None (битый JSON / чужое имя скилла)."""
    active = registry if registry is not None else get_registry()
    parsed, reason = _validate(_extract_json(text), active)
    if parsed is None:
        logger.debug("планировщик: ответ не принят ({})", reason)
    return parsed



_router = None


def _default_router():
    """Router проекта создаётся один раз и только при первом LLM-вызове."""
    global _router
    if _router is None:
        from kivi.brain.router import Router
        from kivi.config import get_settings

        _router = Router(get_settings())
    return _router


def plan_with_llm(
    command: str,
    history: list[dict] | None = None,
    *,
    registry: SkillRegistry | None = None,
    router: Any | None = None,
    max_tokens: int | None = None,
) -> tuple[str | None, dict[str, Any], list[dict[str, str]]]:
    """Спросить LLM, какое умение подходит. Возвращает (skill_name, args, missing).

    history пока не уходит в промпт (схема: [system(промпт+каталог), user(команда)]),
    но принимается — пригодится для уточняющих вопросов на следующем шаге.
    """
    text = str(command or "").strip()
    if not text:
        _remember(ok=False, reason="empty_command", raw="", route=None)
        return None, {}, []

    active = registry if registry is not None else get_registry()
    catalog = build_catalog(active, max_tokens)
    system = SYSTEM_PROMPT + "\n\nКАТАЛОГ:\n" + (catalog or "(каталог пуст)")
    messages = [
        {"role": "system", "content": system},
        {"role": "user", "content": text},
    ]
    logger.debug(
        "планировщик: команда {!r}, каталог {} симв., история {} реплик",
        text[:80],
        len(catalog),
        len(history or []),
    )

    started = time.perf_counter()
    try:
        active_router = router if router is not None else _default_router()
        result = active_router.complete(messages, task_type="intent")
    except Exception as exc:  # noqa: BLE001 - сеть/бюджет/провайдер
        elapsed = (time.perf_counter() - started) * 1000.0
        _remember(
            ok=False,
            reason=f"llm_error:{type(exc).__name__}",
            cost_rub=0.0,
            ms=round(elapsed, 1),
            raw="",
            route=None,
        )
        logger.warning(
            "планировщик: LLM недоступна ({}: {}) — fallback на difflib",
            type(exc).__name__,
            exc,
        )
        return None, {}, []

    elapsed = (time.perf_counter() - started) * 1000.0
    body = result or {}
    raw = str(body.get("text") or "")
    cost_raw = body.get("cost_rub")
    cost = float(cost_raw) if cost_raw is not None else 0.0
    model = str(body.get("model") or "")

    parsed, reason = _validate(_extract_json(raw), active)
    if parsed is None:
        attempted = None
        payload = _extract_json(raw)
        if isinstance(payload, dict):
            value = payload.get("skill_name")
            if isinstance(value, str) and value.strip():
                attempted = value.strip()
        _remember(
            ok=False,
            reason=reason,
            skill_name=None,
            skill_attempted=attempted,
            cost_rub=cost,
            model=model,
            ms=round(elapsed, 1),
            raw=raw[:300],
            route=None,
        )
        if reason == "unknown_skill":
            logger.warning(
                "планировщик: LLM предложила неизвестное умение {!r} — "
                "fallback на difflib",
                attempted,
            )
        else:
            logger.warning(
                "планировщик: невалидный JSON от LLM ({} симв.) — fallback на difflib",
                len(raw),
            )
        return None, {}, []

    _remember(
        ok=True,
        reason="ok",
        route=parsed["route"],
        skill_name=parsed["skill_name"],
        skill_attempted=None,
        cost_rub=cost,
        model=model,
        ms=round(elapsed, 1),
        raw=raw[:300],
    )
    logger.info(
        "планировщик: {!r} -> {} (route {}, {:.0f} мс, {:.4f} руб)",
        text[:60],
        parsed["skill_name"] or "нет умения",
        parsed["route"],
        elapsed,
        cost,
    )
    return parsed["skill_name"], dict(parsed["args"]), list(parsed["missing"])

