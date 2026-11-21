"""Скилл assistant_volume: своя громкость Киви через брокер (self_volume).

args: {level: 0..1} (можно проценты, 40 = 40%) либо {factor: float}
(0.5 — тише вдвое, 1.2 — громче). Без аргументов — тише до 80% от текущей.
"""

from __future__ import annotations


def _to_float(value: object) -> float | None:
    if value is None:
        return None
    text = str(value).replace(",", ".").strip().rstrip("%").strip()
    try:
        return float(text)
    except ValueError:
        return None


def handle(ctx, level=None, factor=None, **_: object) -> str:
    """Изменить громкость голоса и вернуть короткое подтверждение."""
    current = ctx.get_self_volume()

    raw_level = _to_float(level)
    if raw_level is not None:
        # 40 или «40%» понимаем как долю, 0.4 — как долю напрямую.
        target = raw_level / 100.0 if raw_level > 1.0 else raw_level
        percent_mode = True
    else:
        raw_factor = _to_float(factor)
        target = current * (raw_factor if raw_factor is not None else 0.8)
        percent_mode = False

    volume = ctx.set_self_volume(target)
    if percent_mode or abs(volume - current) < 0.01:
        return f"Громкость {round(volume * 100)}%."
    return "Сделала тише." if volume < current else "Сделала громче."
