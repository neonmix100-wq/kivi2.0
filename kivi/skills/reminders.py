"""Напоминания ко времени: data/reminders.json + фоновый поток.

Запись: {"id": "...", "ts": ISO с датой, "what": "...", "done": false}.
Старые epoch-записи читаются и мигрируют без потери.
Повторная проверка — раз в 30 с; после озвучки запись помечается done.
Время: "19:00", "завтра в 19:00", "23.11 19:00", "в понедельник 09:00",
"через 30 минут". Без времени напоминание не создаётся.
"""

from __future__ import annotations

import hashlib
import json
import re
import threading
import uuid
from datetime import datetime, timedelta
from pathlib import Path
from typing import Callable, NamedTuple

from loguru import logger

from kivi.config import DATA_DIR
from kivi.skills.broker import project_timezone

__all__ = [
    "CHECK_INTERVAL_S",
    "REMINDERS_PATH",
    "ReminderScheduler",
    "add_reminder",
    "cancel_reminder",
    "check_due",
    "ensure_scheduler",
    "format_reminder_label",
    "list_reminders",
    "parse_time",
    "parse_time_details",
    "stop_scheduler",
]

REMINDERS_PATH = DATA_DIR / "reminders.json"
CHECK_INTERVAL_S = 30.0
_REL_RE = re.compile(r"через\s+(\d+)\s*(секунд|сек|минут|мин|час|ч)", re.IGNORECASE)
_CLOCK_RE = re.compile(r"(?:^|\s)([01]?\d|2[0-3]):(\d{2})(?:\s|$)")
_DATE_RE = re.compile(r"(?:^|\s)(\d{4})-(\d{2})-(\d{2})(?:[ T](\d{1,2})[:.](\d{2}))?")
_DOT_DATE_RE = re.compile(r"(?<!\d)(\d{1,2})\.(\d{1,2})(?:\.(\d{2,4}))?(?!\d)")
_WEEKDAY_RE = re.compile(
    r"\b(понедельник|вторник|среда|среду|четверг|пятница|пятницу|"
    r"суббота|субботу|воскресенье|пн|вт|ср|чт|пт|сб|вс)\b",
    re.IGNORECASE,
)
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
_WEEKDAY_NAMES = (
    "понедельник",
    "вторник",
    "среда",
    "четверг",
    "пятница",
    "суббота",
    "воскресенье",
)
_LOCK = threading.RLock()


class ParsedTime(NamedTuple):
    when: datetime
    kind: str  # time|today|tomorrow|aftertomorrow|weekday|date|relative
    rolled: bool = False


def _path(path: str | Path | None = None) -> Path:
    return Path(path) if path is not None else REMINDERS_PATH


def parse_timestamp(value: object) -> datetime | None:
    """Прочитать ts нового ISO-формата или старого epoch без потери записей."""
    if isinstance(value, (int, float)):
        try:
            return datetime.fromtimestamp(float(value), tz=project_timezone())
        except (OverflowError, OSError, ValueError):
            return None
    text = str(value or "").strip()
    if not text:
        return None
    try:
        parsed = datetime.fromisoformat(text.replace("Z", "+00:00"))
    except ValueError:
        try:
            return datetime.fromtimestamp(float(text), tz=project_timezone())
        except (OverflowError, OSError, ValueError):
            return None
    if parsed.tzinfo is None:
        parsed = parsed.replace(tzinfo=project_timezone())
    return parsed


def _record_id(ts: object, what: str, index: int) -> str:
    raw = f"{ts}|{what}|{index}".encode("utf-8")
    return hashlib.sha1(raw).hexdigest()[:12]


def _normalize_record(item: dict, index: int) -> dict:
    """Мигрировать epoch -> ISO, добавить стабильный id, сохранить поля."""
    record = dict(item)
    ts = parse_timestamp(record.get("ts"))
    record["ts"] = (
        ts.isoformat(timespec="minutes")
        if ts is not None
        else str(record.get("ts") or "")
    )
    record["what"] = str(record.get("what") or "").strip()
    record["done"] = bool(record.get("done", False))
    record["id"] = str(record.get("id") or _record_id(record["ts"], record["what"], index))
    return record


def _read(path: str | Path | None = None) -> list[dict]:
    file_path = _path(path)
    try:
        raw = json.loads(file_path.read_text(encoding="utf-8"))
    except FileNotFoundError:
        return []
    except (OSError, ValueError):
        logger.warning("напоминания: не читается {} — начинаю с пустого", file_path)
        return []
    if not isinstance(raw, list):
        return []
    return [
        _normalize_record(item, index)
        for index, item in enumerate(raw)
        if isinstance(item, dict)
    ]


def _write(items: list[dict], path: str | Path | None = None) -> None:
    file_path = _path(path)
    file_path.parent.mkdir(parents=True, exist_ok=True)
    tmp = file_path.with_name(f".tmp_{file_path.name}")
    tmp.write_text(json.dumps(items, ensure_ascii=False, indent=2), encoding="utf-8")
    tmp.replace(file_path)


def parse_time_details(raw: str, *, now: datetime | None = None) -> ParsedTime | None:
    """Разобрать день+время. Поддерживает сегодня/завтра/ДД.ММ/день недели."""
    text = " ".join(str(raw or "").split())
    if not text:
        return None
    current = now or datetime.now(project_timezone())
    if current.tzinfo is None:
        current = current.replace(tzinfo=project_timezone())
    lowered = text.lower().replace("ё", "е")

    if "через минуту" in lowered:
        return ParsedTime(current + timedelta(minutes=1), "relative")
    if "через полчаса" in lowered:
        return ParsedTime(current + timedelta(minutes=30), "relative")
    if "через час" in lowered:
        return ParsedTime(current + timedelta(hours=1), "relative")
    relative = _REL_RE.search(lowered)
    if relative:
        amount = int(relative.group(1))
        unit = relative.group(2).lower()
        if unit.startswith("час"):
            delta = timedelta(hours=amount)
        elif unit.startswith("сек"):
            delta = timedelta(seconds=amount)
        else:
            delta = timedelta(minutes=amount)
        return ParsedTime(current + delta, "relative")

    clock = _CLOCK_RE.search(f" {text} ")
    hour = minute = None
    if clock:
        hour = int(clock.group(1))
        minute = int(clock.group(2))
    else:
        # Точка — обычно дата ДД.ММ; как время её берём только при
        # невалидном месяце (например, «19.00»).
        for dot_clock in _DOT_DATE_RE.finditer(text):
            if 1 <= int(dot_clock.group(2)) <= 12:
                continue
            dot_hour = int(dot_clock.group(1))
            if dot_hour > 23:
                continue
            hour, minute = dot_hour, int(dot_clock.group(2))
            break

    absolute = _DATE_RE.search(text)
    if absolute:
        year, month, day = (int(absolute.group(i)) for i in range(1, 4))
        if absolute.group(4) is not None:
            hour, minute = int(absolute.group(4)), int(absolute.group(5))
        if hour is None or minute is None:
            return None
        try:
            return ParsedTime(
                datetime(year, month, day, hour, minute, tzinfo=project_timezone()),
                "date",
            )
        except ValueError:
            return None

    dot_date = _DOT_DATE_RE.search(text)
    if (
        dot_date
        and hour is not None
        and minute is not None
        and 1 <= int(dot_date.group(2)) <= 12
    ):
        day = int(dot_date.group(1))
        month = int(dot_date.group(2))
        year_raw = dot_date.group(3)
        year = int(year_raw) if year_raw else current.year
        if year < 100:
            year += 2000
        try:
            candidate = datetime(
                year, month, day, hour, minute, tzinfo=project_timezone()
            )
        except ValueError:
            return None
        if not year_raw and candidate.date() < current.date():
            candidate = candidate.replace(year=year + 1)
        return ParsedTime(candidate, "date")

    explicit_day: tuple[int, str] | None = None
    if "послезавтра" in lowered or "после завтра" in lowered:
        explicit_day = (2, "aftertomorrow")
    elif "завтра" in lowered:
        explicit_day = (1, "tomorrow")
    elif "сегодня" in lowered:
        explicit_day = (0, "today")

    if explicit_day is not None:
        if hour is None or minute is None:
            return None
        days, kind = explicit_day
        day = current + timedelta(days=days)
        return ParsedTime(
            day.replace(hour=hour, minute=minute, second=0, microsecond=0),
            kind,
        )

    weekday = _WEEKDAY_RE.search(lowered)
    if weekday:
        if hour is None or minute is None:
            return None
        target = _WEEKDAYS[weekday.group(1).lower()]
        days = (target - current.weekday()) % 7
        candidate = (current + timedelta(days=days)).replace(
            hour=hour, minute=minute, second=0, microsecond=0
        )
        if candidate <= current:
            candidate += timedelta(days=7)
        return ParsedTime(candidate, "weekday")

    if hour is not None and minute is not None:
        candidate = current.replace(hour=hour, minute=minute, second=0, microsecond=0)
        rolled = candidate <= current
        if rolled:
            candidate += timedelta(days=1)
        return ParsedTime(candidate, "time", rolled)
    return None


def parse_time(raw: str, *, now: datetime | None = None) -> datetime | None:
    """Совместимый API: вернуть только aware datetime."""
    parsed = parse_time_details(raw, now=now)
    return parsed.when if parsed is not None else None


def format_reminder_label(when: datetime, *, now: datetime | None = None) -> str:
    """Человеческое «завтра 19:00» / «сегодня 19:00» для recall и UI."""
    current = now or datetime.now(project_timezone())
    if current.tzinfo is None:
        current = current.replace(tzinfo=project_timezone())
    days = (when.date() - current.date()).days
    time_text = when.strftime("%H:%M")
    if days == 0:
        return f"сегодня {time_text}"
    if days == 1:
        return f"завтра {time_text}"
    if days == 2:
        return f"послезавтра {time_text}"
    if 0 < days <= 7:
        return f"{_WEEKDAY_NAMES[when.weekday()]} {time_text}"
    return f"{when.strftime('%d.%m')} {time_text}"


def _response_label(parsed: ParsedTime, source: str, now: datetime) -> str:
    """Метка для голосового ответа; при переносе — явно «завтра»."""
    if parsed.kind == "relative":
        return " ".join(source.split()).lower().replace("ё", "е")
    if parsed.kind == "tomorrow" or parsed.rolled:
        return f"завтра в {parsed.when:%H:%M}"
    if parsed.kind == "aftertomorrow":
        return f"послезавтра в {parsed.when:%H:%M}"
    if parsed.kind == "today":
        return f"сегодня в {parsed.when:%H:%M}"
    if parsed.kind == "weekday":
        return f"в {_WEEKDAY_NAMES[parsed.when.weekday()]} в {parsed.when:%H:%M}"
    if parsed.kind == "date":
        return f"{parsed.when:%d.%m} в {parsed.when:%H:%M}"
    return f"в {parsed.when:%H:%M}"


def add_reminder(
    time_text: str,
    what: str,
    *,
    now: datetime | None = None,
    path: str | Path | None = None,
    start_scheduler: bool = True,
) -> str:
    """Добавить напоминание; вернуть ответ для озвучки или пустую строку."""
    what_text = " ".join(str(what or "").split())
    if not what_text:
        return ""
    current = now or datetime.now(project_timezone())
    if current.tzinfo is None:
        current = current.replace(tzinfo=project_timezone())
    parsed = parse_time_details(time_text, now=current)
    if parsed is None:
        return ""
    record = {
        "id": uuid.uuid4().hex[:12],
        "ts": parsed.when.isoformat(timespec="minutes"),
        "what": what_text,
        "done": False,
    }
    with _LOCK:
        items = _read(path)
        items.append(record)
        _write(items, path)
    label = _response_label(parsed, str(time_text), current)
    logger.info(
        "напоминание добавлено: {} — {}",
        parsed.when.strftime("%Y-%m-%d %H:%M"),
        what_text[:100],
    )
    if start_scheduler:
        ensure_scheduler()
    return f"Напомню {label}: {what_text}"


def list_reminders(
    *,
    include_done: bool = False,
    days: int | None = 7,
    now: datetime | None = None,
    path: str | Path | None = None,
) -> list[dict]:
    """Список напоминаний (pending, ближайшие days дней) с id/меткой."""
    current = now or datetime.now(project_timezone())
    if current.tzinfo is None:
        current = current.replace(tzinfo=project_timezone())
    result: list[dict] = []
    with _LOCK:
        items = _read(path)
    for item in items:
        when = parse_timestamp(item.get("ts"))
        if when is None:
            continue
        if not include_done and item.get("done"):
            continue
        if days is not None and when > current + timedelta(days=int(days)):
            continue
        enriched = dict(item)
        enriched["when_label"] = format_reminder_label(when, now=current)
        enriched["status"] = "done" if item.get("done") else "pending"
        result.append(enriched)
    result.sort(key=lambda item: str(item.get("ts") or ""))
    return result


def cancel_reminder(
    reminder_id: str, *, path: str | Path | None = None
) -> bool:
    """Удалить запись по id. True — если запись была."""
    target = str(reminder_id or "").strip()
    if not target:
        return False
    with _LOCK:
        items = _read(path)
        kept = [item for item in items if str(item.get("id")) != target]
        if len(kept) == len(items):
            return False
        _write(kept, path)
    logger.info("напоминание отменено: {}", target)
    return True


def _default_speak(text: str) -> None:
    from kivi.tts.speak import speak

    speak(text)


def check_due(
    *,
    now: datetime | None = None,
    speak_fn: Callable[[str], object] | None = None,
    path: str | Path | None = None,
) -> list[str]:
    """Озвучить наступившие напоминания и пометить done. Возвращает тексты."""
    current = now or datetime.now(project_timezone())
    if current.tzinfo is None:
        current = current.replace(tzinfo=project_timezone())
    speaker = speak_fn or _default_speak
    spoken: list[str] = []
    with _LOCK:
        items = _read(path)
        changed = False
        for item in items:
            if item.get("done"):
                continue
            try:
                due = parse_timestamp(item.get("ts"))
            except Exception:  # noqa: BLE001 - битую старую запись не теряем
                due = None
            if due is None:
                continue
            if due > current:
                continue
            text = str(item.get("what") or "").strip()
            if not text:
                item["done"] = True
                changed = True
                continue
            message = f"Напоминание: {text}"
            try:
                speaker(message)
            except Exception as exc:  # noqa: BLE001 - попробуем в следующий тик
                logger.warning("напоминание: озвучка упала: {}: {}", type(exc).__name__, exc)
                continue
            item["done"] = True
            changed = True
            spoken.append(message)
        if changed:
            _write(items, path)
    return spoken


class ReminderScheduler:
    """Один daemon-поток: каждые 30 с проверяет reminders.json."""

    def __init__(self, interval: float = CHECK_INTERVAL_S) -> None:
        self.interval = max(1.0, float(interval))
        self._stop = threading.Event()
        self._thread: threading.Thread | None = None
        self._started = False

    @property
    def is_running(self) -> bool:
        return self._thread is not None and self._thread.is_alive()

    def _loop(self) -> None:
        while not self._stop.is_set():
            try:
                check_due()
            except Exception as exc:  # noqa: BLE001 - планировщик не должен умирать
                logger.warning("напоминания: проверка упала: {}: {}", type(exc).__name__, exc)
            self._stop.wait(self.interval)

    def start(self) -> None:
        if self._started:
            return
        self._started = True
        self._stop.clear()
        self._thread = threading.Thread(
            target=self._loop,
            name="kivi-reminders",
            daemon=True,
        )
        self._thread.start()
        logger.info("напоминания: фоновый поток запущен (проверка {:.0f} с)", self.interval)

    def stop(self, timeout: float = 3.0) -> None:
        self._stop.set()
        if self._thread is not None and self._thread is not threading.current_thread():
            self._thread.join(max(0.1, float(timeout)))
        self._started = False


_scheduler_lock = threading.Lock()
_scheduler: ReminderScheduler | None = None


def ensure_scheduler() -> ReminderScheduler:
    """Запустить единственный фоновый поток напоминаний."""
    global _scheduler
    with _scheduler_lock:
        if _scheduler is None or not _scheduler.is_running:
            _scheduler = ReminderScheduler()
            _scheduler.start()
        return _scheduler


def stop_scheduler(timeout: float = 3.0) -> None:
    """Остановить фоновый поток (выход/тесты)."""
    global _scheduler
    with _scheduler_lock:
        if _scheduler is not None:
            _scheduler.stop(timeout=timeout)
        _scheduler = None

