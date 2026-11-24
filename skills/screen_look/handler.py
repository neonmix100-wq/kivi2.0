"""Скилл screen_look: скриншот экрана -> vision-описание."""
from __future__ import annotations

from kivi.vision.screen import prepare

PROMPT = (
    "Опиши коротко (2-4 предложения) что на экране пользователя: "
    "приложение, активное окно, видимые ошибки или текст. "
    "На русском, без markdown."
)


def handle(ctx, **_: object) -> str:
    image = ctx.screenshot()
    try:
        from kivi.config import get_settings

        max_side = int(get_settings().section("vision").get("max_side") or 1280)
    except Exception:  # noqa: BLE001 - конфиг не должен ломать команду
        max_side = 1280
    png = prepare(image, max_side=max_side)
    result = ctx.vision(png, PROMPT)
    text = " ".join(str((result or {}).get("text") or "").split())
    return text or "Не смогла разобрать, что на экране."
