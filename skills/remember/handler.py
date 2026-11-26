"""Скилл remember: запись в структурированную память через брокер.

Расписание хранится канонически: «- [ВС-ПТ 19:00-23:00] работа над проектом».
Свободные формулировки нормализует kivi.memory.schedule; что не удалось —
сохраняется как «- [?] …» без потери текста (recall покажет только в общем).
"""

from __future__ import annotations

from typing import Any

from loguru import logger

from kivi.memory.schedule import (
    ScheduleRule,
    canonical_from_text,
    format_days_ru,
    format_minutes,
    normalize,
    parse_rule,
)


def _clean(value) -> str:
    return " ".join(str(value or "").split())


def _time_text(rule: ScheduleRule) -> str:
    if rule.end == rule.start:
        return format_minutes(rule.start)
    return f"{format_minutes(rule.start)}–{format_minutes(rule.end)}"


def _when_text(rule: ScheduleRule) -> str:
    if rule.date is not None:
        return f"{rule.date.day:02d}.{rule.date.month:02d}"
    return format_days_ru(rule.days)


def _confirmation(rule: ScheduleRule) -> str:
    """Полное подтверждение: текст + дни + оба времени."""
    return f"Запомнила: {rule.what}, {_when_text(rule)} {_time_text(rule)}."


def _line_from_structured(days: str, start: str, end: str, what: str) -> str | None:
    """Канон из полей UI/LLM (ДНИ, СТАРТ, КОНЕЦ, ТЕКСТ)."""
    title = _clean(what)
    if not title or not _clean(start):
        return None
    if not _clean(days):
        return None
    try:
        return normalize(days, start, end or start, title)
    except ValueError:
        # ДД.ММ вместо дней — разовое событие.
        return canonical_from_text(f"{days} {start} {end or ''} {title}")


def _event_line(args: dict[str, Any], when: str, what: str, command: str) -> str | None:
    """Собрать каноническую строку из аргументов планировщика/реплики."""
    structured = _line_from_structured(
        _clean(args.get("days")),
        _clean(args.get("start")),
        _clean(args.get("end")),
        _clean(args.get("what")) or what,
    )
    if structured is not None:
        return structured
    variants: list[tuple[str, str]] = []
    if when:
        variants.append((when, what))
        if what and what not in when:
            variants.append((f"{when} {what}", ""))
    if command:
        variants.append((command, ""))
        if what and what not in command:
            variants.append((f"{when} {command}", ""))
    for text, fallback in variants:
        candidate = canonical_from_text(text, fallback)
        if candidate:
            return candidate
    return None


def _llm_event(ctx, command: str) -> str | None:
    """Дозапрос у cheap-LLM, если локальный парсер не справился."""
    extractor = getattr(ctx, "extract_schedule", None)
    if not callable(extractor) or not command:
        return None
    try:
        payload = extractor(command) or {}
    except Exception as exc:  # noqa: BLE001 - LLM не должна ронять запись
        logger.warning("remember: извлечение расписания упало: {}", type(exc).__name__)
        return None
    if not isinstance(payload, dict):
        return None
    return _line_from_structured(
        _clean(payload.get("days")),
        _clean(payload.get("start")),
        _clean(payload.get("end")),
        _clean(payload.get("what")),
    )


def _save_line(ctx, line: str) -> bool:
    """Записать строку через брокер; в тестах — через старый vault_add_schedule."""
    saver = getattr(ctx, "vault_add_schedule_line", None)
    if callable(saver):
        return bool(saver(line))
    rule = parse_rule(line)
    if rule is None:
        return False
    return bool(
        ctx.vault_add_schedule(
            f"{format_days_ru(rule.days) if rule.days else rule.date} "
            f"{format_minutes(rule.start)}",
            rule.what,
        )
    )


def _save_raw(ctx, raw: str) -> None:
    line = f"- [?] {raw}"
    saver = getattr(ctx, "vault_add_schedule_line", None)
    if callable(saver):
        saver(line)
    else:
        ctx.vault_add_schedule("?", raw)
    logger.warning("remember: расписание не нормализовалось, сохранил «[?]»: {}", raw[:120])


def handle(ctx, **args) -> str:
    kind = _clean(args.get("kind")).lower()
    what = _clean(args.get("what"))
    when = _clean(args.get("when"))
    command = _clean(args.get("command"))

    if not what and not (kind == "schedule" and (when or command)):
        return "Уточни: что запомнить?"
    if kind not in ("schedule", "task", "preference", "fact"):
        return "Уточни: что запомнить?"

    if kind == "schedule":
        if not (what or when or command):
            return "Уточни: когда?"
        line = _event_line(args, when, what, command)
        if line is None:
            line = _llm_event(ctx, command or f"{when} {what}")
        if line is not None:
            rule = parse_rule(line)
            _save_line(ctx, line)
            if rule is not None:
                return _confirmation(rule)
            return f"Запомнила: {line.lstrip('- ').strip()}"
        raw = f"{when} {what}".strip() or command
        _save_raw(ctx, raw)
        return f"Записала как есть: {raw}. Время и дни не распознала."
    if kind == "task":
        ctx.vault_add_task(what)
        return f"Записала задачу: {what}"
    if kind == "preference":
        ctx.vault_add_preference(what)
        return f"Запомнила предпочтение: {what}"
    ctx.vault_add_fact(what)
    return f"Запомнила: {what}"

