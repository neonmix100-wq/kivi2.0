"""Скилл listen_control: пауза/возобновление обработки VAD."""
from __future__ import annotations


def handle(ctx, action=None, **_: object) -> str:
    value = " ".join(str(action or "").split()).lower()
    if value not in ("pause", "resume"):
        return "Уточни: поставить прослушку на паузу или включить?"
    try:
        ctx.set_listen(value == "resume")
    except Exception:  # noqa: BLE001 - нет loop в тестах/старом ядре
        return "Не могу переключить прослушку сейчас."
    if value == "pause":
        return "Хорошо, не слушаю. Скажи «Киви, слушай снова», когда нужно."
    return "Слушаю снова."
