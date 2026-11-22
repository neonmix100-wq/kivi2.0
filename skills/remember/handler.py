"""Скилл remember: запись в структурированную память через брокер."""

from __future__ import annotations

import re

_TIME_RE = re.compile(r"\b([01]?\d|2[0-3])[:.](\d{2})")


def _clean(value) -> str:
    return " ".join(str(value or "").split())


def _schedule_phrase(when: str, what: str) -> str:
    lowered = when.lower().replace("ё", "е")
    match = _TIME_RE.search(when)
    time_text = f"{int(match.group(1)):02d}:{match.group(2)}" if match else ""
    if time_text and "будн" in lowered:
        return f"{what} по будням в {time_text}"
    if time_text and ("каждый день" in lowered or "ежедневно" in lowered):
        return f"{what} ежедневно в {time_text}"
    if time_text:
        return f"{what} в {time_text}"
    return f"{what} — {when}"


def handle(ctx, **args) -> str:
    kind = _clean(args.get("kind")).lower()
    what = _clean(args.get("what"))
    when = _clean(args.get("when"))

    if not what or kind not in ("schedule", "task", "preference", "fact"):
        return "Уточни: что запомнить?"

    if kind == "schedule":
        if not when:
            return "Уточни: когда?"
        ctx.vault_add_schedule(when, what)
        return f"Запомнила: {_schedule_phrase(when, what)}"
    if kind == "task":
        ctx.vault_add_task(what)
        return f"Записала задачу: {what}"
    if kind == "preference":
        ctx.vault_add_preference(what)
        return f"Запомнила предпочтение: {what}"
    ctx.vault_add_fact(what)
    return f"Запомнила: {what}"
