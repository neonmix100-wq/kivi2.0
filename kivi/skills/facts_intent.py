"""Распознавание запросов «курс валюты» и «погода» без LLM.

Один источник правды сразу для трёх мест:
  * Planner — fast-правило: currency/weather -> скилл facts (без LLM-планировщика);
  * скилл skills/facts — какие валюты/город подразумеваются;
  * kivi/skills/search.py — перед поиском попробовать facts.

Важно: «курс биткоина» НЕ относится к валютам (это криптовалюта), поэтому
такие запросы уходят в обычный поиск.

Использование:
    from kivi.skills.facts_intent import classify
    classify("курс доллара к рублю")
    # {"kind": "currency", "base": "USD", "quote": "RUB",
    #  "base_name": "доллара", "quote_name": "рубля"}
    classify("погода в Новосибирске")
    # {"kind": "weather", "city": "Новосибирске"}
    classify("какой курс биткоина")   # None
"""

from __future__ import annotations

import re

__all__ = [
    "CURRENCY_CODES",
    "CURRENCY_MARKERS",
    "DEFAULT_CITY",
    "WEATHER_MARKERS",
    "classify",
    "is_currency_command",
    "is_weather_command",
]

# Город по умолчанию: проект живёт в Новосибирске (см. config.brain.budget.timezone).
DEFAULT_CITY = "Новосибирск"

# Нормализованная основа слова -> (код валюты, родительный падеж для ответа).
CURRENCY_CODES: dict[str, tuple[str, str]] = {
    "доллар": ("USD", "доллара"),
    "бакс": ("USD", "доллара"),
    "usd": ("USD", "доллара"),
    "евро": ("EUR", "евро"),
    "eur": ("EUR", "евро"),
    "рубл": ("RUB", "рубля"),
    "rub": ("RUB", "рубля"),
    "юан": ("CNY", "юаня"),
    "cny": ("CNY", "юаня"),
    "йен": ("JPY", "йены"),
    "jpy": ("JPY", "йены"),
    "фунт": ("GBP", "фунта"),
    "gbp": ("GBP", "фунта"),
    "тенге": ("KZT", "тенге"),
    "kzt": ("KZT", "тенге"),
    "гривн": ("UAH", "гривны"),
    "uah": ("UAH", "гривны"),
    "лир": ("TRY", "лиры"),
    "try": ("TRY", "лиры"),
}

CURRENCY_MARKERS = ("курс", "стоит", "стоимость", "сколько", "переведи")
WEATHER_MARKERS = (
    "погод",
    "температур",
    "дожд",
    "снег",
    "ветер",
    "облачн",
    "градус",
    "тепло ли",
    "холодно ли",
)

# «в Новосибирске», «во Владивостоке», «для Москвы».
_CITY_AFTER_PREPOSITION = re.compile(r"(?:^|\s)(?:в|во|для|по)\s+([А-ЯЁA-Z][\w\-]+)")
_CITY_WORD = re.compile(r"\b([А-ЯЁ][а-яё\-]{2,})\b")


def _norm(text: str) -> str:
    """Нижний регистр, ё->е, лишние пробелы убраны (пунктуация сохраняется)."""
    value = str(text or "").replace("ё", "е").replace("Ё", "Е").lower()
    return " ".join(value.split())


def _currency_mentions(normalized: str) -> list[tuple[int, str, str]]:
    """Найденные валюты в порядке упоминания: [(позиция, код, падеж)]."""
    found: list[tuple[int, str, str]] = []
    for stem, (code, genitive) in CURRENCY_CODES.items():
        position = normalized.find(stem)
        if position >= 0:
            found.append((position, code, genitive))
    found.sort(key=lambda item: item[0])
    # Дубликаты одного кода (например, «доллар» и «usd») не нужны.
    unique: list[tuple[int, str, str]] = []
    for item in found:
        if any(existing[1] == item[1] for existing in unique):
            continue
        unique.append(item)
    return unique


def _currency_params(normalized: str) -> dict[str, str] | None:
    """Параметры курса, если это запрос про валюту; иначе None."""
    if not any(marker in normalized for marker in CURRENCY_MARKERS):
        return None
    mentions = _currency_mentions(normalized)
    if not mentions:
        return None
    codes = [(code, genitive) for _pos, code, genitive in mentions]
    foreign = [(code, genitive) for code, genitive in codes if code != "RUB"]
    if foreign:
        base_code, base_name = foreign[0]
        quote_code, quote_name = "RUB", "рубля"
        if len(foreign) > 1:
            quote_code, quote_name = foreign[1]
    else:
        # «курс рубля» — показываем доллар к рублю.
        base_code, base_name = "USD", "доллара"
        quote_code, quote_name = "RUB", "рубля"
    return {
        "kind": "currency",
        "base": base_code,
        "quote": quote_code,
        "base_name": base_name,
        "quote_name": quote_name,
    }


def _weather_city(command: str, normalized: str) -> str:
    """Город из фразы «погода в <город>» (или DEFAULT_CITY)."""
    match = _CITY_AFTER_PREPOSITION.search(command)
    if match:
        return match.group(1)
    match = _CITY_AFTER_PREPOSITION.search(normalized)
    if match:
        return match.group(1).capitalize()
    for stop_word in ("погода", "погоду", "погоде", "температура", "дождь", "снег"):
        normalized = normalized.replace(stop_word, " ")
    for marker in WEATHER_MARKERS:
        normalized = normalized.replace(marker, " ")
    match = _CITY_WORD.search(command) or _CITY_WORD.search(normalized)
    if match and match.group(1).lower() not in ("сегодня", "завтра", "сейчас", "будет"):
        return match.group(1)
    return DEFAULT_CITY


def is_currency_command(command: str) -> bool:
    """Это запрос про курс валюты (а не криптовалюты)."""
    return _currency_params(_norm(command)) is not None


def is_weather_command(command: str) -> bool:
    """Это запрос про погоду/температуру."""
    normalized = _norm(command)
    return any(marker in normalized for marker in WEATHER_MARKERS)


def classify(command: str) -> dict[str, str] | None:
    """Параметры для скилла facts или None, если запрос не про курс/погоду."""
    text = " ".join(str(command or "").split())
    if not text:
        return None
    normalized = _norm(text)
    currency = _currency_params(normalized)
    if currency is not None:
        return currency
    if is_weather_command(text):
        return {"kind": "weather", "city": _weather_city(text, normalized)}
    return None
