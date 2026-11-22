"""Извлечение сущностей для recall: ключевые слова и date_refs.

LLM-вызов идёт через Router.complete(task_type="intent") — cheap-тир без
эскалации. Если LLM недоступна, возвращаем пустые сущности: recall покажет
обзор, как раньше, и не сломает ответ.
"""

from __future__ import annotations

import json
import re
from dataclasses import dataclass
from datetime import date, datetime, timedelta
from typing import Any

from loguru import logger

__all__ = [
    "QueryEntities",
    "as_entities",
    "date_refs_to_dates",
    "extract_entities",
    "extract_keywords",
    "keyword_in_text",
    "reminder_matches_date",
    "schedule_matches_date",
    "task_matches_date",
]

EXTRACT_PROMPT = (
    "Извлеки из запроса пользователя ключевые слова для поиска в памяти.\n"
    "Запрос: {command}. Ответь JSON: {keywords: [...], date_refs: "
    "[today|tomorrow|...]}.\n"
    "Для общих вопросов («что у меня…», «мои задачи», «расписание») "
    "keywords оставь пустым."
)

_DATE_ALIASES = {
    "today": 0,
    "сегодня": 0,
    "tomorrow": 1,
    "завтра": 1,
    "aftertomorrow": 2,
    "послезавтра": 2,
}
_WEEKDAYS = {
    "понедельник": 0,
    "пн": 0,
    "вторник": 1,
    "вт": 1,
    "среда": 2,
    "среду": 2,
    "ср": 2,
    "четверг": 3,
    "чт": 3,
    "пятница": 4,
    "пятницу": 4,
    "пт": 4,
    "суббота": 5,
    "субботу": 5,
    "сб": 5,
    "воскресенье": 6,
    "вс": 6,
}
_WEEKDAY_RE = re.compile(
    r"\b(понедельник|вторник|среда|среду|четверг|пятница|пятницу|"
    r"суббота|субботу|воскресенье|пн|вт|ср|чт|пт|сб|вс)\b",
    re.IGNORECASE,
)
_DOT_DATE_RE = re.compile(r"(?<!\d)(\d{1,2})\.(\d{1,2})(?:\.(\d{2,4}))?(?!\d)")
_ISO_DATE_RE = re.compile(r"\b(\d{4})-(\d{2})-(\d{2})\b")
_GENERAL_RE = re.compile(
    r"^(?:что|какие|какое|покажи)?\s*(?:у меня|завтра|сегодня|послезавтра|"
    r"мои задачи|список дел|что по плану|какое расписание|что на неделе)",
    re.IGNORECASE,
)


@dataclass(frozen=True)
class QueryEntities:
    """Что искать в памяти: ключевые слова и относительные даты."""

    keywords: list[str]
    date_refs: list[str]



def _norm(text: object) -> str:
    return " ".join(str(text or "").lower().replace("ё", "е").split())


def _clean_keywords(values: object) -> list[str]:
    if not isinstance(values, list):
        return []
    result: list[str] = []
    for value in values:
        word = _norm(value).strip(" .,!?:;\"'«»()[]{}")
        if word and word not in result:
            result.append(word)
    return result


def _clean_dates(values: object) -> list[str]:
    if not isinstance(values, list):
        return []
    result: list[str] = []
    for value in values:
        ref = _norm(value).strip(" .,!?:;\"'«»()[]{}")
        if not ref:
            continue
        if ref in ("после завтра", "after tomorrow"):
            ref = "aftertomorrow"
        if ref not in result:
            result.append(ref)
    return result


def _extract_json(raw: str) -> Any:
    text = str(raw or "").strip()
    if text.startswith("```"):
        text = re.sub(r"^```(?:json)?\s*", "", text, flags=re.IGNORECASE)
        text = re.sub(r"\s*```$", "", text)
    try:
        return json.loads(text)
    except ValueError:
        pass
    start = text.find("{")
    end = text.rfind("}")
    if start >= 0 and end > start:
        try:
            return json.loads(text[start : end + 1])
        except ValueError:
            return None
    return None


def as_entities(value: object) -> QueryEntities:
    """Привести ответ LLM/фейкового контекста к QueryEntities."""
    if isinstance(value, QueryEntities):
        return value
    if isinstance(value, dict):
        return QueryEntities(
            keywords=_clean_keywords(value.get("keywords")),
            date_refs=_clean_dates(value.get("date_refs")),
        )
    return QueryEntities([], [])


def extract_entities(command: str, *, router: Any | None = None) -> QueryEntities:
    """Спросить cheap-LLM, что искать. Ошибка LLM -> пустые сущности."""
    text = " ".join(str(command or "").split())
    if not text:
        return QueryEntities([], [])
    try:
        if router is None:
            from kivi.brain.router import Router

            router = Router()
        messages = [
            {
                "role": "system",
                "content": "Ты извлекатель сущностей. Отвечай только JSON.",
            },
            {"role": "user", "content": EXTRACT_PROMPT.replace("{command}", text)},
        ]
        result = router.complete(messages, task_type="intent")
    except Exception as exc:  # noqa: BLE001 - recall не должен падать без LLM
        logger.warning("recall: извлечение сущностей не удалось: {}", type(exc).__name__)
        return QueryEntities([], [])

    raw = str((result or {}).get("text") or "")
    payload = _extract_json(raw)
    if not isinstance(payload, dict):
        logger.warning("recall: LLM вернула не JSON при извлечении сущностей")
        return QueryEntities([], [])
    entities = as_entities(payload)
    if _GENERAL_RE.search(text):
        # «что у меня завтра» — это date_refs, а не поиск слова «завтра».
        entities = QueryEntities([], entities.date_refs)
    return entities


def extract_keywords(
    command: str, *, router: Any | None = None
) -> dict[str, list[str]]:
    """Совместимый dict-API для брокера и тестов."""
    entities = extract_entities(command, router=router)
    return {"keywords": entities.keywords, "date_refs": entities.date_refs}


def keyword_in_text(text: object, keywords: list[str]) -> bool:
    """Подстрочный поиск с мягким стеммингом для русских окончаний.

    «мама» должна находить «позвонить маме», «встреча» — «встречу».
    """
    haystack = _norm(text)
    if not haystack or not keywords:
        return False
    for keyword in keywords:
        needle = _norm(keyword)
        if not needle:
            continue
        if needle in haystack:
            return True
        stem = needle[:-1] if len(needle) >= 4 else needle
        if stem != needle and stem in haystack:
            return True
    return False



def date_refs_to_dates(refs: list[str], now: datetime) -> set[date]:
    """Преобразовать today/tomorrow/понедельник в конкретные даты."""
    today = now.date()
    result: set[date] = set()
    for raw in refs:
        ref = _norm(raw)
        if ref in _DATE_ALIASES:
            result.add(today + timedelta(days=_DATE_ALIASES[ref]))
            continue
        iso = _ISO_DATE_RE.search(ref)
        if iso:
            try:
                result.add(date(int(iso.group(1)), int(iso.group(2)), int(iso.group(3))))
            except ValueError:
                continue
            continue
        dot = _DOT_DATE_RE.search(ref)
        if dot and 1 <= int(dot.group(2)) <= 12:
            year = int(dot.group(3)) if dot.group(3) else today.year
            if year < 100:
                year += 2000
            try:
                result.add(date(year, int(dot.group(2)), int(dot.group(1))))
            except ValueError:
                pass
            continue
        weekday = _WEEKDAY_RE.search(ref)
        if weekday:
            target = _WEEKDAYS[weekday.group(1).lower()]
            days = (target - today.weekday()) % 7
            result.add(today + timedelta(days=days))
    return result


def _date_in_text(text: object, targets: set[date], now: datetime) -> bool | None:
    """True/False по date_refs; None — в тексте даты нет."""
    lowered = _norm(text)
    if not lowered:
        return None
    if "будн" in lowered:
        return any(item.weekday() < 5 for item in targets)
    if "каждый день" in lowered or "ежедневно" in lowered:
        return True
    today = now.date()
    found = False
    for alias, offset in sorted(
        _DATE_ALIASES.items(), key=lambda item: len(item[0]), reverse=True
    ):
        if alias in lowered and len(alias) > 2:
            found = True
            if today + timedelta(days=offset) in targets:
                return True
    iso = _ISO_DATE_RE.search(lowered)
    if iso:
        found = True
        try:
            if date(int(iso.group(1)), int(iso.group(2)), int(iso.group(3))) in targets:
                return True
        except ValueError:
            pass
    dot = _DOT_DATE_RE.search(lowered)
    if dot and 1 <= int(dot.group(2)) <= 12:
        found = True
        year = int(dot.group(3)) if dot.group(3) else today.year
        if year < 100:
            year += 2000
        try:
            if date(year, int(dot.group(2)), int(dot.group(1))) in targets:
                return True
        except ValueError:
            pass
    weekday = _WEEKDAY_RE.search(lowered)
    if weekday:
        found = True
        target = _WEEKDAYS[weekday.group(1).lower()]
        if any(item.weekday() == target for item in targets):
            return True
    return False if found else None


def schedule_matches_date(when: str, targets: set[date], now: datetime) -> bool:
    matched = _date_in_text(when, targets, now)
    return True if matched is None else matched


def task_matches_date(text: str, targets: set[date], now: datetime) -> bool:
    return bool(_date_in_text(text, targets, now))


def reminder_matches_date(item: dict[str, Any], targets: set[date], now: datetime) -> bool:
    ts = str(item.get("ts") or "").strip()
    if ts:
        try:
            parsed = datetime.fromisoformat(ts.replace("Z", "+00:00"))
        except ValueError:
            parsed = None
        if parsed is not None and parsed.date() in targets:
            return True
    label = str(item.get("when_label") or "")
    matched = _date_in_text(label, targets, now)
    return True if matched is None else matched

