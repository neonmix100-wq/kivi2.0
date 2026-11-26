"""Каноническое расписание: парсинг, нормализация, миграция, проверка дня.

Формат строки schedule.md:
    - [ДНИ СТАРТ-КОНЕЦ] текст
    - [27.11 19:00-20:00] текст

ДНИ: ПН ВТ СР ЧТ ПТ СБ ВС. Диапазоны через '-' включительно и с переходом
через неделю (ВС-ПТ = вс,пн,вт,ср,чт,пт), список через ','. Ключевые слова:
будни = ПН-ПТ, выходные = СБ,ВС, ежедневно = ПН-ВС.
Время: HH:MM-HH:MM; точечное событие — HH:MM (конец = старт).
Дата ДД.ММ — разовое событие вместо дней недели.

Модуль общий для remember, recall и UI: одна нормализация на всех.
"""

from __future__ import annotations

import re
from dataclasses import dataclass
from datetime import date
from pathlib import Path

from kivi.log import logger

__all__ = [
    "DAY_NAMES",
    "DAY_RU_SHORT",
    "ScheduleRule",
    "canonical_from_text",
    "ensure_canonical",
    "expand_days",
    "format_days",
    "format_days_ru",
    "format_minutes",
    "is_raw",
    "matches",
    "migrate_legacy",
    "migrate_schedule_file",
    "normalize",
    "normalize_date",
    "parse_free",
    "parse_rule",
]

DAY_NAMES = ("ПН", "ВТ", "СР", "ЧТ", "ПТ", "СБ", "ВС")
DAY_INDEX = {name: index for index, name in enumerate(DAY_NAMES)}
DAY_WORDS = {
    "пн": 0, "понедельник": 0,
    "вт": 1, "вторник": 1,
    "ср": 2, "среда": 2, "среду": 2,
    "чт": 3, "четверг": 3,
    "пт": 4, "пятница": 4, "пятницу": 4,
    "сб": 5, "суббота": 5, "субботу": 5,
    "вс": 6, "воскресенье": 6,
}
_DAY_STEMS = (
    ("понедельник", 0),
    ("вторник", 1),
    ("сред", 2),
    ("четверг", 3),
    ("пятниц", 4),
    ("суббот", 5),
    ("воскрес", 6),
)
DAY_RU_SHORT = ("пн", "вт", "ср", "чт", "пт", "сб", "вс")
DAY_RU_FULL = (
    "понедельник", "вторник", "среда", "четверг", "пятница", "суббота", "воскресенье",
)
KEYWORD_DAYS = {
    "будни": "ПН-ПТ",
    "будням": "ПН-ПТ",
    "будний": "ПН-ПТ",
    "будние": "ПН-ПТ",
    "рабочие дни": "ПН-ПТ",
    "выходные": "СБ,ВС",
    "выходным": "СБ,ВС",
    "выходных": "СБ,ВС",
    "ежедневно": "ПН-ВС",
    "каждый день": "ПН-ВС",
    "каждодневно": "ПН-ВС",
}
_ENTRY_RE = re.compile(r"^\s*[-*]?\s*\[([^\]]+)\]\s*(.+?)\s*$")
_TIME_RE = re.compile(r"^([01]?\d|2[0-3])[:.](\d{2})$")
_TIME_RANGE_RE = re.compile(
    r"\b(2[0-3]|[01]?\d)(?:[:.](\d{2}))?\s*(?:-|до)\s*"
    r"(2[0-3]|[01]?\d)(?:[:.](\d{2}))?\b"
)
_TIME_SINGLE_RE = re.compile(r"\b(?:в\s*)?(2[0-3]|[01]?\d)[:.]([0-5]\d)\b")
_BARE_TIME_RE = re.compile(
    r"\bв\s*(2[0-3]|[01]?\d)\s*(утра|дня|вечера|ночи)?\b"
)
_DATE_RE = re.compile(r"(?<!\d)(\d{1,2})\.(\d{1,2})(?:\.(\d{2,4}))?(?!\d)")
_DAY_RANGE_RE = re.compile(r"\bс\s+([а-яё]+)\s+(?:по|до)\s+([а-яё]+)\b", re.IGNORECASE)
_WEEKDAY_RANGE_RE = re.compile(
    r"\b(понедельник\w*|вторник\w*|сред\w*|четверг\w*|пятниц\w*|суббот\w*|воскресень\w*"
    r"|пн|вт|ср|чт|пт|сб|вс)\s*(?:-|до)\s*"
    r"(понедельник\w*|вторник\w*|сред\w*|четверг\w*|пятниц\w*|суббот\w*|воскресень\w*"
    r"|пн|вт|ср|чт|пт|сб|вс)\b",
    re.IGNORECASE,
)
_WEEKDAY_TOKEN_RE = re.compile(
    r"\b(понедельник\w*|вторник\w*|сред\w*|четверг\w*|пятниц\w*|суббот\w*|воскресень\w*"
    r"|пн|вт|ср|чт|пт|сб|вс)\b",
    re.IGNORECASE,
)
_RAW_PREFIX = "[?]"


@dataclass(frozen=True)
class ScheduleRule:
    """Разобранное правило расписания."""

    days: set[int] | None
    date: date | None
    start: int
    end: int
    what: str
    raw: str = ""

    @property
    def has_time_range(self) -> bool:
        return self.end != self.start


def _norm(text: object) -> str:
    return " ".join(str(text or "").lower().replace("ё", "е").split())


def _day_index(word: str) -> int | None:
    token = _norm(word).strip(" .,!?:;")
    if token in DAY_WORDS:
        return DAY_WORDS[token]
    for stem, index in _DAY_STEMS:
        if token.startswith(stem):
            return index
    return None


def _expand_pair(start: int, end: int) -> set[int]:
    """Диапазон включительно с переходом через неделю (ВС-ПТ)."""
    days: set[int] = set()
    current = start
    for _ in range(7):
        days.add(current)
        if current == end:
            return days
        current = (current + 1) % 7
    return days


def expand_days(days_text: str) -> set[int]:
    """Развернуть «ВС-ПТ», «будни», «ПН,СР» в набор индексов (ПН=0).

    Неизвестный токен -> ValueError: лучше честно пометить строку «[?]»,
    чем молча потерять расписание.
    """
    text = _norm(days_text).strip(" .,;")
    if not text:
        raise ValueError("пустые дни")
    for keyword, canonical in KEYWORD_DAYS.items():
        if keyword in text:
            text = text.replace(keyword, canonical)
    text = text.replace(" ", "").replace(";", ",")
    days: set[int] = set()
    for chunk in (part for part in text.split(",") if part):
        chunk = chunk.strip(".")
        if not chunk:
            continue
        if "-" in chunk:
            head, _, tail = chunk.partition("-")
            start = DAY_INDEX.get(head.upper())
            end = DAY_INDEX.get(tail.upper())
            if start is None or end is None:
                raise ValueError(f"неизвестный диапазон дней: {chunk!r}")
            days |= _expand_pair(start, end)
            continue
        index = DAY_INDEX.get(chunk.upper())
        if index is None:
            raise ValueError(f"неизвестный день: {chunk!r}")
        days.add(index)
    if not days:
        raise ValueError("дни не распознаны")
    return days


def format_days(days: set[int] | None) -> str:
    """Канонические дни: будни -> ПН-ПТ, все -> ПН-ВС, иначе сжатые диапазоны."""
    if not days:
        return ""
    ordered = sorted(set(days))
    if ordered == list(range(7)):
        return "ПН-ВС"
    if ordered == [0, 1, 2, 3, 4]:
        return "ПН-ПТ"
    if ordered == [5, 6]:
        return "СБ,ВС"
    # Диапазон с переходом через неделю: {вс,пн..пт} -> ВС-ПТ.
    if len(ordered) > 1:
        for start in ordered:
            run = [start]
            current = (start + 1) % 7
            while len(run) < len(ordered) and current in days:
                run.append(current)
                current = (current + 1) % 7
            if len(run) == len(ordered):
                return f"{DAY_NAMES[start]}-{DAY_NAMES[run[-1]]}"
    parts: list[str] = []
    run_start = ordered[0]
    previous = ordered[0]
    for index in ordered[1:] + [None]:
        if index is not None and index == previous + 1:
            previous = index
            continue
        parts.append(
            DAY_NAMES[run_start]
            if run_start == previous
            else f"{DAY_NAMES[run_start]}-{DAY_NAMES[previous]}"
        )
        if index is not None:
            run_start = previous = index
    return ",".join(parts)


def format_days_ru(days: set[int] | None) -> str:
    """Человеческие дни для подтверждения: вс–пт, будни, сб–вс."""
    if not days:
        return ""
    ordered = sorted(set(days))
    if ordered == list(range(7)):
        return "ежедневно"
    if ordered == [0, 1, 2, 3, 4]:
        return "будни"
    if ordered == [5, 6]:
        return "сб–вс"
    result: list[str] = []
    for part in format_days(days).split(","):
        if "-" in part:
            head, _, tail = part.partition("-")
            result.append(f"{DAY_RU_SHORT[DAY_INDEX[head]]}–{DAY_RU_SHORT[DAY_INDEX[tail]]}")
        else:
            result.append(DAY_RU_SHORT[DAY_INDEX[part]])
    return ",".join(result)


def format_minutes(minutes: int) -> str:
    value = int(minutes) % (24 * 60 + 1)
    return f"{value // 60:02d}:{value % 60:02d}"


def _parse_minutes(text: str) -> int:
    match = _TIME_RE.match(_norm(text))
    if not match:
        raise ValueError(f"время не в формате HH:MM: {text!r}")
    return int(match.group(1)) * 60 + int(match.group(2))


def _parse_date(text: str) -> date | None:
    match = _DATE_RE.search(_norm(text))
    if not match:
        return None
    day, month = int(match.group(1)), int(match.group(2))
    year = int(match.group(3)) if match.group(3) else date.today().year
    if year < 100:
        year += 2000
    try:
        return date(year, month, day)
    except ValueError:
        return None


def _time_text(start_minutes: int, end_minutes: int) -> str:
    if start_minutes == end_minutes:
        return format_minutes(start_minutes)
    return f"{format_minutes(start_minutes)}-{format_minutes(end_minutes)}"


def normalize(days_text: str, start: str, end: str, what: str) -> str:
    """Собрать каноническую строку: `- [ДНИ HH:MM-HH:MM] текст`."""
    title = " ".join(str(what or "").split())
    if not title:
        raise ValueError("пустой текст события")
    days = expand_days(days_text)
    start_minutes = _parse_minutes(start)
    end_minutes = _parse_minutes(end) if str(end or "").strip() else start_minutes
    return f"- [{format_days(days)} {_time_text(start_minutes, end_minutes)}] {title}"


def normalize_date(day: date, start: str, end: str, what: str) -> str:
    """Каноническая строка разового события: `- [27.11 19:00-20:00] текст`."""
    title = " ".join(str(what or "").split())
    if not title:
        raise ValueError("пустой текст события")
    start_minutes = _parse_minutes(start)
    end_minutes = _parse_minutes(end) if str(end or "").strip() else start_minutes
    return f"- [{day.day:02d}.{day.month:02d} {_time_text(start_minutes, end_minutes)}] {title}"



def parse_rule(line: str) -> ScheduleRule | None:
    """Разобрать строку schedule.md; None — строка не каноническая."""
    match = _ENTRY_RE.match(str(line or ""))
    if not match:
        return None
    head = " ".join(match.group(1).split())
    what = match.group(2).strip()
    if not head or not what or head.startswith(("?", "[?]")):
        return None
    time_match = re.search(r"(\d{1,2}[:.]\d{2})(?:\s*-\s*(\d{1,2}[:.]\d{2}))?$", head)
    if not time_match:
        return None
    prefix = head[: time_match.start()].strip()
    start = _parse_minutes(time_match.group(1))
    end = _parse_minutes(time_match.group(2)) if time_match.group(2) else start
    if not prefix:
        return None
    parsed_date = _parse_date(prefix)
    if parsed_date is not None:
        return ScheduleRule(None, parsed_date, start, end, what, head)
    try:
        days = expand_days(prefix)
    except ValueError:
        return None
    return ScheduleRule(days, None, start, end, what, head)


def matches(rule: ScheduleRule | None, target_date: date) -> bool:
    """Попадает ли правило на конкретную дату."""
    if rule is None:
        return False
    if rule.date is not None:
        return (rule.date.month, rule.date.day) == (target_date.month, target_date.day)
    if rule.days:
        return target_date.weekday() in rule.days
    return False


def is_raw(line: str) -> bool:
    """Строка, сохранённая как «[?] …» (не нормализовалась)."""
    match = _ENTRY_RE.match(str(line or ""))
    return bool(match and match.group(1).strip().startswith(("?", "[?]")))


def _strip_fragments(text: str, fragments: list[str]) -> str:
    cleaned = text
    for fragment in fragments:
        cleaned = cleaned.replace(fragment, " ")
    cleaned = re.sub(r"\b(в|во|с|со|до|по|на|к|и)\b", " ", cleaned)
    cleaned = re.sub(
        r"\b(будн\w*|рабочи\w*|выходн\w*|ежедневн\w*|каждодневн\w*|каждый|каждую"
        r"|день|дня|дней)\b",
        " ",
        cleaned,
    )
    cleaned = re.sub(r"[^0-9a-zа-яё\s-]", " ", cleaned)
    tokens = [token for token in cleaned.split() if len(token) > 2 or token.isdigit()]
    return " ".join(tokens).strip(" -")


def _merge_what(parsed_what: str, fallback: str) -> str:
    if parsed_what and fallback and _norm(fallback) not in _norm(parsed_what):
        return f"{parsed_what} {fallback}".strip()
    return parsed_what or " ".join(str(fallback or "").split())


def parse_free(text: str, fallback_what: str = "") -> ScheduleRule | None:
    """Разобрать свободную фразу («по будням в 15:00», «с 19:00 до 23:00»).

    Возвращает дни ИЛИ дату + время. Если чего-то не хватает — None:
    такие строки сохраняются как «[?] » без потери текста.
    """
    raw = " ".join(str(text or "").split())
    if not raw:
        return None
    lowered = _norm(raw).replace("–", "-").replace("—", "-")
    fragments: list[str] = []
    start = end = None

    # Дату вырезаем ДО поиска времени: «27.11» не должно стать «27:11».
    work = lowered
    parsed_date = _parse_date(work)
    if parsed_date is not None:
        date_match = _DATE_RE.search(work)
        if date_match:
            fragments.append(date_match.group(0))
            work = (
                work[: date_match.start()]
                + " " * (date_match.end() - date_match.start())
                + work[date_match.end() :]
            )

    range_match = _TIME_RANGE_RE.search(work)
    if range_match:
        start = int(range_match.group(1)) * 60 + int(range_match.group(2) or 0)
        end = int(range_match.group(3)) * 60 + int(range_match.group(4) or 0)
        fragments.append(range_match.group(0))
    else:
        single = _TIME_SINGLE_RE.search(work)
        if single:
            start = int(single.group(1)) * 60 + int(single.group(2))
            end = start
            fragments.append(single.group(0))
        else:
            bare = _BARE_TIME_RE.search(work)
            if bare:
                hour = int(bare.group(1))
                part = bare.group(2) or ""
                if part in ("вечера", "ночи") and hour < 12:
                    hour += 12
                start = hour * 60
                end = start
                fragments.append(bare.group(0))
    if start is None:
        return None

    days: set[int] | None = None
    day_range = _DAY_RANGE_RE.search(lowered)
    if day_range:
        first = _day_index(day_range.group(1))
        second = _day_index(day_range.group(2))
        if first is None or second is None:
            return None
        days = _expand_pair(first, second)
        fragments.append(day_range.group(0))
    else:
        weekday_range = _WEEKDAY_RANGE_RE.search(lowered)
        if weekday_range:
            first = _day_index(weekday_range.group(1))
            second = _day_index(weekday_range.group(2))
            if first is None or second is None:
                return None
            days = _expand_pair(first, second)
            fragments.append(weekday_range.group(0))
        else:
            day_tokens: set[int] = set()
            for match in _WEEKDAY_TOKEN_RE.finditer(lowered):
                index = _day_index(match.group(0))
                if index is not None:
                    day_tokens.add(index)
                    fragments.append(match.group(0))
            if day_tokens:
                days = day_tokens
        if days is None:
            for keyword, canonical in KEYWORD_DAYS.items():
                if keyword in lowered:
                    days = expand_days(canonical)
                    fragments.append(keyword)
                    break

    if days is None and parsed_date is None:
        return None

    what = _merge_what(_strip_fragments(lowered, fragments), fallback_what)
    if not what:
        return None
    return ScheduleRule(days, parsed_date, int(start), int(end), what, raw)



def _canonical_from_rule(rule: ScheduleRule) -> str:
    if rule.date is not None:
        return normalize_date(
            rule.date,
            format_minutes(rule.start),
            format_minutes(rule.end),
            rule.what,
        )
    return normalize(
        format_days(rule.days),
        format_minutes(rule.start),
        format_minutes(rule.end),
        rule.what,
    )


def canonical_from_text(text: str, fallback_what: str = "") -> str | None:
    """Свободная фраза -> каноническая строка (или None)."""
    rule = parse_free(text, fallback_what)
    if rule is None:
        return None
    try:
        return _canonical_from_rule(rule)
    except ValueError:
        return None


def migrate_legacy(line: str) -> str | None:
    """Старая свободная строка -> канон. None — не получилось (сохраняем «[?]»)."""
    text = " ".join(str(line or "").split())
    if not text:
        return None
    if is_raw(text):
        return text
    parsed = parse_rule(text)
    if parsed is not None:
        return _canonical_from_rule(parsed)
    match = _ENTRY_RE.match(text)
    raw = f"{match.group(1)} {match.group(2)}" if match else re.sub(
        r"^\s*[-*]\s*", "", text
    )
    return canonical_from_text(raw)


def _raw_line(text: str) -> str:
    cleaned = " ".join(str(text or "").split())
    cleaned = re.sub(r"^\s*[-*]\s*", "", cleaned)
    if cleaned.startswith("["):
        return f"- {cleaned}"
    return "- [?] " + cleaned


def migrate_schedule_file(path: str | Path) -> int:
    """Переписать schedule.md каноническими строками; вернуть число изменённых.

    Непарсируемые строки не теряются: текст сохраняется с префиксом «[?] ».
    """
    note = Path(path)
    if not note.exists():
        return 0
    text = note.read_text(encoding="utf-8", errors="replace")
    updated: list[str] = []
    changed = 0
    for line in text.splitlines():
        stripped = line.strip()
        if not stripped or stripped.startswith("#") or not re.match(r"^\s*[-*]", line):
            updated.append(line)
            continue
        canonical = migrate_legacy(line)
        if canonical is None:
            canonical = _raw_line(line)
            logger.warning(
                "schedule: не удалось нормализовать строку, сохранил как «[?]»: {}",
                stripped[:120],
            )
        if canonical.strip() != stripped:
            changed += 1
        updated.append(canonical)
    if changed:
        new_text = "\n".join(updated).rstrip() + "\n"
        tmp = note.with_name(f".tmp_{note.name}")
        tmp.write_text(new_text, encoding="utf-8", newline="\n")
        tmp.replace(note)
        logger.info("schedule: мигрировано {} строк", changed)
    return changed


_MIGRATED: set[str] = set()


def ensure_canonical(path: str | Path) -> int:
    """Один раз за процесс привести файл к канону (без повторных перезаписей)."""
    key = str(Path(path).resolve())
    if key in _MIGRATED:
        return 0
    _MIGRATED.add(key)
    try:
        return migrate_schedule_file(path)
    except OSError as exc:
        logger.warning("schedule: миграция не удалась: {}", exc)
        return 0

