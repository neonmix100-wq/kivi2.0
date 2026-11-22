"""Скилл reminders: запись напоминания через брокер."""

from __future__ import annotations


def handle(ctx, **args) -> str:
    time_text = " ".join(str(args.get("time") or args.get("when") or "").split())
    what = " ".join(str(args.get("what") or "").split())
    if not what:
        return "Уточни: когда и что напомнить?"
    if not time_text:
        add_task = getattr(ctx, "vault_add_task", None)
        if callable(add_task):
            add_task(what)
            return f"Записала задачу: {what}"
        return "Уточни: когда и что напомнить?"
    reply = ctx.scheduler(time_text, what)
    return reply or "Уточни: когда и что напомнить?"
