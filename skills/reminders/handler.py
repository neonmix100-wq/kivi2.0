"""Скилл reminders: запись напоминания через брокер."""

from __future__ import annotations


def handle(ctx, **args) -> str:
    time_text = " ".join(str(args.get("time") or args.get("when") or "").split())
    what = " ".join(str(args.get("what") or "").split())
    if not time_text or not what:
        return "Уточни: когда и что напомнить?"
    reply = ctx.scheduler(time_text, what)
    return reply or "Уточни: когда и что напомнить?"
