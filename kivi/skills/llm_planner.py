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

from kivi.brain.router import complete_safe
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
 "confidence": 0.0, "missing": [{"capability": "...", "why": "..."}]}
Правила:
- skill — каталог покрывает команду; chat — разговор/генерация;
  search — живые данные (курс, погода, новости, цены, факты кто/что/когда/сколько);
  action_gap — действие без инструмента (устройство, письмо, файл, покупка).
- «запомни/в расписание/поставь задачу/запиши» -> remember, args {kind:
  schedule|task|preference|fact, what, when?}. Для schedule давай days
  (ПН-ПТ/ВС-ПТ/будни/СБ,ВС/ДД.ММ), start и end (HH:MM), what. «с X по Y» ->
  days "ВС-ПТ", «по будням» -> "будни". Со временем — schedule,
  «купи/сделать» — task, «люблю/предпочитаю» — preference, иначе fact.
- «напомни в/разбуди в» -> reminders {time, what}; без времени -> remember {kind: task, what}.
- «во сколько/когда/что завтра/что у меня сегодня» про планы -> recall;
  «мои задачи/список дел» -> tasks_list.
- «что ты умеешь/возможности» и вопросы про доступ/разрешения/«почему не можешь»
  -> capabilities.
- курс валюты/погода -> facts + СУЩНОСТИ + confidence:
  {"kind":"crypto","coin":"bitcoin|ethereum|solana|dogecoin|toncoin|ripple|litecoin"};
  {"kind":"currency","base":"USD|EUR|RUB|CNY|...","quote":"RUB|..."};
  {"kind":"weather","city":"Новосибирск"}.
  «курс биткоина к доллару» — crypto, не currency. Сущность названа, но не из списка
  -> search; confidence < 0.6 -> search.
- Поправка («я просил X, а не Y», «не то», «опять», «снова») — бери исправленную
  сущность и не повторяй прежнюю ошибку.
- «что на экране/посмотри на экран/прочитай ошибку» -> screen_look.
- «выруби/пауза прослушку/не слушай» -> listen_control {action: pause};
  «включи/слушай снова» -> resume.
- «очисти/удали расписание|напоминания|задачу» -> memory_manage {target:
  schedule|reminders|tasks, action: clear|delete|done, what?}.
Умения вне каталога не выдумывай."""

# Последний вызов: причина fallback, маршрут, стоимость, модель. Нужен planner.py и смоукам.
_LAST_CALL: dict[str, Any] = {
    "ok": False,
    "reason": "not_called",
    "route": None,
    "skill_name": None,
    "skill_attempted": None,
    "confidence": None,
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
    """Компактный каталог: name + 3 triggers, без описаний (экономия IN-токенов).

    Формат строки: "- {name}: {trigger1}, {trigger2}, {trigger3}".
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
        triggers = ", ".join(manifest.triggers[:3])
        line = f"- {manifest.name}: {triggers}".rstrip(": ")
        if not triggers:
            line = f"- {manifest.name}"
        if used + len(line) + 1 > budget_chars:
            break  # бюджет каталога исчерпан
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

    confidence_raw = payload.get("confidence")
    try:
        confidence = float(confidence_raw) if confidence_raw is not None else 1.0
    except (TypeError, ValueError):
        confidence = 1.0
    confidence = max(0.0, min(1.0, confidence))

    return {
        "skill_name": skill_name,
        "args": args,
        "missing": missing,
        "route": route,
        "confidence": confidence,
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


def _history_lines(history: list[dict] | None, limit: int = 4) -> str:
    """Последние реплики диалога компактно — нужны для поправок."""
    lines: list[str] = []
    for item in list(history or [])[-limit:]:
        if not isinstance(item, dict):
            continue
        role = str(item.get("role") or "").strip() or "?"
        content = " ".join(str(item.get("content") or "").split())
        if content:
            lines.append(f"{role}: {content}")
    return "\n".join(lines) or "(пусто)"


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
    # Для поправок хватает последней пары реплик: экономия IN-токенов.
    history_text = _history_lines(history, limit=2)
    try:
        from kivi.brain.selfmodel import build_manifest

        manifest = build_manifest(compact=True)
    except Exception:  # noqa: BLE001 - без манифеста планировщик работает
        manifest = ""
    system = SYSTEM_PROMPT
    if manifest:
        system += "\n\nМОЁ СОСТОЯНИЕ (кратко):\n" + manifest
    system += (
        "\n\nПОСЛЕДНИЕ РЕПЛИКИ (для поправок):\n"
        + history_text
        + "\n\nКАТАЛОГ:\n"
        + (catalog or "(каталог пуст)")
    )
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
        result = complete_safe(
            active_router, messages, task_type="intent", max_tokens=200
        )
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
        stripped = raw.strip()
        truncated = (not stripped) or (
            stripped.startswith("{") and not stripped.endswith("}")
        )
        if truncated:
            # Модель ушла в рассуждения и JSON обрезался по max_tokens=200.
            # Один повтор с двойным лимитом (400): дешевле, чем безлимит.
            logger.warning(
                "планировщик: ответ обрезан по max_tokens=200 — повтор с max_tokens=400"
            )
            try:
                retry = complete_safe(
                    active_router, messages, task_type="intent", max_tokens=400
                )
            except Exception as exc:  # noqa: BLE001 - сеть/бюджет
                logger.warning(
                    "планировщик: повтор не удался: {}: {}",
                    type(exc).__name__,
                    exc,
                )
            else:
                retry_body = retry or {}
                raw = str(retry_body.get("text") or "")
                cost += float(retry_body.get("cost_rub") or 0.0)
                model = str(retry_body.get("model") or model)
                elapsed = (time.perf_counter() - started) * 1000.0
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

    skill_name = parsed["skill_name"]
    plan_args = dict(parsed["args"])
    route = str(parsed["route"])
    confidence = float(parsed.get("confidence") or 1.0)

    # Сущности facts проверяем здесь: LLM могла назвать неизвестную монету.
    if skill_name == "facts":
        from kivi.skills.facts_intent import resolve as resolve_facts

        entities = resolve_facts(plan_args, text)
        if entities is None:
            logger.info(
                "планировщик: сущность facts не маппится ({!r}) -> search", plan_args
            )
            skill_name, plan_args, route = None, {}, "search"
        else:
            plan_args = {**entities, "command": text}
    elif skill_name is not None and confidence < 0.6:
        logger.info(
            "планировщик: confidence {:.2f} < 0.6 для {} -> search",
            confidence,
            skill_name,
        )
        skill_name, plan_args, route = None, {}, "search"

    _remember(
        ok=True,
        reason="ok",
        route=route,
        skill_name=skill_name,
        skill_attempted=None,
        confidence=confidence,
        cost_rub=cost,
        model=model,
        ms=round(elapsed, 1),
        raw=raw[:300],
    )
    logger.info(
        "планировщик: {!r} -> {} (route {}, confidence {:.2f}, {:.0f} мс, {:.4f} руб)",
        text[:60],
        skill_name or "нет умения",
        route,
        confidence,
        elapsed,
        cost,
    )
    return skill_name, plan_args, list(parsed["missing"])

