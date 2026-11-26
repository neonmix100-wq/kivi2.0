"""Скилл recall: конкретный ответ по schedule/tasks/reminders из vault.

Перед чтением памяти LLM (cheap, task_type=intent) извлекает keywords и
date_refs. Фильтр — подстрока по keywords; date_refs ограничивают выдачу
конкретной датой. Пустые keywords/date_refs сохраняют прежний обзор.
"""

from __future__ import annotations

import re
from datetime import date, datetime

from kivi.memory.schedule import (
    DAY_RU_SHORT,
    ScheduleRule,
    format_days_ru,
    format_minutes,
    is_raw,
    matches,
    parse_rule,
)
from kivi.skills.broker import project_timezone
from kivi.skills.recall import (
    QueryEntities,
    as_entities,
    date_refs_from_text,
    date_refs_to_dates,
    keyword_in_text,
    reminder_matches_date,
    task_matches_date,
)

_ENTRY_RE = re.compile(r"^\s*[-*]\s*\[([^\]]+)\]\s*(.+?)\s*$")


def _read(ctx, name: str) -> str:
    try:
        return ctx.fs_read(f"memory/{name}")
    except FileNotFoundError:
        return ""


def _entries(text: str) -> list[tuple[str, str]]:
    found: list[tuple[str, str]] = []
    for line in str(text).splitlines():
        match = _ENTRY_RE.match(line)
        if match:
            found.append((match.group(1).strip(), match.group(2).strip()))
    return found


def _rule_time(rule: ScheduleRule) -> str:
    if rule.start == rule.end:
        return format_minutes(rule.start)
    return f"{format_minutes(rule.start)}–{format_minutes(rule.end)}"


def _rule_days(rule: ScheduleRule) -> str:
    if rule.date is not None:
        return f"{rule.date.day:02d}.{rule.date.month:02d}"
    return format_days_ru(rule.days)


def _date_label(target: date, now: datetime) -> str:
    name = DAY_RU_SHORT[target.weekday()]
    diff = (target - now.date()).days
    if diff == 0:
        return f"Сегодня ({name})"
    if diff == 1:
        return f"Завтра ({name})"
    if diff == 2:
        return f"Послезавтра ({name})"
    return f"{target.day:02d}.{target.month:02d} ({name})"


def _schedule_rules(text: str) -> list[ScheduleRule]:
    """Все строки расписания: канон — как правило, «[?]» — как raw-запись."""
    found: list[ScheduleRule] = []
    for line in str(text).splitlines():
        if not line.strip().startswith(("-", "*")):
            continue
        rule = parse_rule(line)
        if rule is not None:
            found.append(rule)
            continue
        if is_raw(line):
            match = _ENTRY_RE.match(line)
            if match:
                found.append(
                    ScheduleRule(None, None, 0, 0, match.group(2).strip(), "[?]")
                )
    return found


def _now(ctx) -> datetime:
    try:
        current = ctx.now()
    except Exception:  # noqa: BLE001 - старый/тестовый контекст без now
        current = datetime.now(project_timezone())
    if current.tzinfo is None:
        current = current.replace(tzinfo=project_timezone())
    return current


def _not_found_word(keyword: str) -> str:
    """Мягкая форма для фразы «про …»: встреча -> встречу, мама -> маму."""
    word = str(keyword or "").strip()
    if len(word) >= 3 and word[-1] in "ая":
        return word[:-1] + "у"
    return word


def _entities(ctx, command: str) -> QueryEntities:
    """Получить keywords/date_refs через брокер; без LLM — пустой фильтр."""
    if not command:
        return QueryEntities([], [])
    extractor = getattr(ctx, "extract_keywords", None)
    if not callable(extractor):
        return QueryEntities([], [])
    try:
        return as_entities(extractor(command))
    except Exception as exc:  # noqa: BLE001 - LLM не должна ронять recall
        ctx.log(f"recall: извлечение сущностей упало: {type(exc).__name__}")
        return QueryEntities([], [])


def handle(ctx, **args) -> str:
    """Ответить по релевантным записям, а не перечислять весь vault."""
    command = " ".join(str(args.get("command") or "").split())
    entities = _entities(ctx, command)
    keywords = list(entities.keywords)
    refs = list(entities.date_refs)
    # LLM могла не вернуть date_refs — добираем детерминированно из текста.
    for ref in date_refs_from_text(command):
        if ref not in refs:
            refs.append(ref)
    current = _now(ctx)
    targets = date_refs_to_dates(refs, current)
    filtered = bool(keywords or targets)

    schedule_all = _schedule_rules(_read(ctx, "schedule.md"))
    tasks_all = [
        what
        for status, what in _entries(_read(ctx, "tasks.md"))
        if status.lower() == "open"
    ]
    try:
        reminders_all = [item for item in ctx.reminders_list() if not item.get("done")]
    except Exception:  # noqa: BLE001 - старый контекст/нет напоминаний
        reminders_all = []

    def keyword_ok(rule: ScheduleRule) -> bool:
        if not keywords:
            return True
        return keyword_in_text(f"{_rule_days(rule)} {rule.what}", keywords)

    schedule_rules = [rule for rule in schedule_all if keyword_ok(rule)]

    dated: list[tuple[date, ScheduleRule]] = []
    if targets:
        for rule in schedule_rules:
            if rule.days is None and rule.date is None:
                continue  # «[?]» показываем только в общем запросе
            for target in sorted(targets):
                if matches(rule, target):
                    dated.append((target, rule))

    tasks = [
        what
        for what in tasks_all
        if (not keywords or keyword_in_text(what, keywords))
        and (not targets or task_matches_date(what, targets, current))
    ]
    reminders = [
        item
        for item in reminders_all
        if (
            not keywords
            or keyword_in_text(
                f"{item.get('when_label', '')} {item.get('ts', '')} "
                f"{item.get('what', '')}",
                keywords,
            )
        )
        and (not targets or reminder_matches_date(item, targets, current))
    ]

    vault_has_records = bool(schedule_all or tasks_all or reminders_all)
    found = len(dated or schedule_rules) + len(tasks) + len(reminders)
    if targets:
        found = len(dated) + len(tasks) + len(reminders)
    ctx.log(f"recall: фильтрую по {keywords}/{sorted(targets)} -> найдено {found} записей")

    if not found:
        if keywords and not targets:
            return f"Не нашла в твоих задачах про {_not_found_word(keywords[0])}"
        if not vault_has_records:
            return "Пока пусто."
        if targets:
            if len(targets) == 1:
                head = _date_label(next(iter(targets)), current)
                return f"{head}: ничего нет."
            return "На этот период ничего нет."
        return "Пока пусто."

    parts: list[str] = []
    if targets:
        groups: dict[date, list[ScheduleRule]] = {}
        for target, rule in dated:
            groups.setdefault(target, []).append(rule)
        for target in sorted(groups):
            items = "; ".join(
                f"{rule.what} {_rule_time(rule)}" for rule in groups[target]
            )
            parts.append(f"{_date_label(target, current)}: {items}.")
    else:
        canonical = [rule for rule in schedule_rules if rule.days or rule.date]
        raw_only = [rule for rule in schedule_rules if not rule.days and not rule.date]
        if canonical:
            phrases = "; ".join(
                f"{rule.what} — {_rule_days(rule)} {_rule_time(rule)}"
                for rule in canonical
            )
            parts.append(f"Расписание: {phrases}.")
        if raw_only:
            phrases = "; ".join(f"{rule.what} (без времени)" for rule in raw_only)
            parts.append(f"Не разобрала: {phrases}.")
        if not canonical and not raw_only:
            parts.insert(0, "Расписание пусто.")

    if tasks:
        if filtered:
            parts.append("Задачи: " + "; ".join(tasks) + ".")
        else:
            parts.append(f"Открытых задач: {len(tasks)}.")
    if reminders:
        phrases = "; ".join(
            f"{item.get('when_label') or item.get('ts', '')} {item.get('what', '')}".strip()
            for item in reminders
        )
        parts.append(f"Напоминания: {phrases}.")
    if not filtered:
        if not tasks:
            parts.append("Открытых задач: 0.")
    return " ".join(parts)

