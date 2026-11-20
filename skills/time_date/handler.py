"""Скилл time_date: текущее время и дата по Новосибирску.

Ничего, кроме часов, не трогает: permissions пустые, доступен только
безопасный ctx.now().
"""

from __future__ import annotations

from datetime import datetime

WEEKDAYS = (
    "понедельник",
    "вторник",
    "среда",
    "четверг",
    "пятница",
    "суббота",
    "воскресенье",
)
MONTHS_GENITIVE = (
    "января",
    "февраля",
    "марта",
    "апреля",
    "мая",
    "июня",
    "июля",
    "августа",
    "сентября",
    "октября",
    "ноября",
    "декабря",
)


def describe(moment: datetime) -> str:
    """Человекочитаемые время и дата: «Сейчас 14:05, среда, 20 ноября 2026 года.»"""
    date_text = (
        f"{WEEKDAYS[moment.weekday()]}, "
        f"{moment.day} {MONTHS_GENITIVE[moment.month - 1]} {moment.year} года"
    )
    return f"Сейчас {moment:%H:%M}, {date_text}."


def handle(ctx) -> str:
    """Вернуть время и дату в часовом поясе проекта (Asia/Novosibirsk)."""
    moment = ctx.now()
    ctx.log(f"time_date: {moment.isoformat()}")
    return describe(moment)
