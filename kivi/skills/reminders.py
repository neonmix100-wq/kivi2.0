"""Напоминания ко времени: data/reminders.json + фоновый поток.

Запись: {"ts": epoch, "what": "...", "done": false}.
Повторная проверка — раз в 30 с; после озвучки запись помечается done.
Время: "19:00", "через 30 минут", "2026-11-22 19:00".
"""

from __future__ import annotations

import json
import re
import threading
from datetime import datetime, timedelta
from pathlib import Path
from typing import Callable

from loguru import logger

from kivi.config import DATA_DIR
from kivi.skills.broker import project_timezone

__all__ = [
    "CHECK_INTERVAL_S",
    "REMINDERS_PATH",
    "ReminderScheduler",
    "add_reminder",
    "check_due",
    "ensure_scheduler",
    "parse_time",
    "stop_scheduler",
]

REMINDERS_PATH = DATA_DIR / "reminders.json"
CHECK_INTERVAL_S = 30.0
_REL_RE = re.compile(r"через\s+(\d+)\s*(секунд|сек|минут|мин|час|ч)", re.IGNORECASE)
_CLOCK_RE = re.compile(r"(?:^|\s)([01]?\d|2[0-3])[:.](\d{2})(?:\s|$)")
_DATE_RE = re.compile(r"(\d{4})-(\d{2})-(\d{2})[ T](\d{1,2})[:.](\d{2})")
_LOCK = threading.RLock()


def _path(path: str | Path | None = None) -> Path:
    return Path(path) if path is not None else REMINDERS_PATH


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
    return [item for item in raw if isinstance(item, dict)]


def _write(items: list[dict], path: str | Path | None = None) -> None:
    file_path = _path(path)
    file_path.parent.mkdir(parents=True, exist_ok=True)
    tmp = file_path.with_name(f".tmp_{file_path.name}")
    tmp.write_text(json.dumps(items, ensure_ascii=False, indent=2), encoding="utf-8")
    tmp.replace(file_path)


def parse_time(raw: str, *, now: datetime | None = None) -> datetime | None:
    """Разобрать «19:00», «через 30 минут» или дату; вернуть aware datetime."""
    text = " ".join(str(raw or "").split())
    if not text:
        return None
    current = now or datetime.now(project_timezone())
    if current.tzinfo is None:
        current = current.replace(tzinfo=project_timezone())

    lowered = text.lower().replace("ё", "е")
    if "через минуту" in lowered:
        return current + timedelta(minutes=1)
    if "через полчаса" in lowered:
        return current + timedelta(minutes=30)
    if "через час" in lowered:
        return current + timedelta(hours=1)

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
        return current + delta

    absolute = _DATE_RE.search(text)
    if absolute:
        year, month, day, hour, minute = (int(value) for value in absolute.groups())
        try:
            return datetime(year, month, day, hour, minute, tzinfo=project_timezone())
        except ValueError:
            return None

    clock = _CLOCK_RE.search(f" {text} ")
    if clock:
        hour = int(clock.group(1))
        minute = int(clock.group(2))
        candidate = current.replace(hour=hour, minute=minute, second=0, microsecond=0)
        if candidate <= current:
            candidate += timedelta(days=1)
        return candidate
    return None

def _display_time(when: datetime, source: str, now: datetime) -> str:
    """Короткое представление для ответа пользователю."""
    lowered = source.lower().replace("ё", "е")
    if "через" in lowered:
        return " ".join(source.split())
    if when.date() == now.date():
        return when.strftime("%H:%M")
    return when.strftime("%Y-%m-%d %H:%M")


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
    when = parse_time(time_text, now=current)
    if when is None:
        return ""
    record = {"ts": float(when.timestamp()), "what": what_text, "done": False}
    with _LOCK:
        items = _read(path)
        items.append(record)
        _write(items, path)
    display = _display_time(when, str(time_text), current)
    logger.info("напоминание добавлено: {} — {}", display, what_text[:100])
    if start_scheduler:
        ensure_scheduler()
    if display.lower().startswith("через"):
        return f"Напомню {display}: {what_text}"
    return f"Напомню в {display}: {what_text}"


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
                due = float(item.get("ts") or 0)
            except (TypeError, ValueError):
                continue
            if due > current.timestamp():
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

