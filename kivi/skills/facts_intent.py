"""Сущности «курс валюты», «курс крипты» и «погода» — без LLM.

Один источник правды для:
  * planner (LLM извлекает args, здесь — валидация сущностей);
  * скилла skills/facts (какие валюты/монета/город подразумеваются);
  * kivi/skills/search.py (перед поиском попробовать facts).

Главное правило: если сущность НАЗВАНА, но не маппится (нет такой валюты или
монеты) — возвращаем None, и ядро уходит в поиск. Дефолтная пара USD→RUB
допустима только для общего вопроса «какой курс валют».

Использование:
    from kivi.skills.facts_intent import resolve
    resolve({"kind": "crypto", "coin": "bitcoin"})
    # {"kind": "crypto", "coin": "bitcoin", "vs": "usd,rub,eur"}
    resolve(None, "курс биткоина к доллару")     # тоже crypto (coin раньше валюты)
    resolve(None, "курс доллара к рублю")        # {"kind": "currency", ...}
    resolve(None, "курс шмэкоина")               # None -> поиск
"""

from __future__ import annotations

import re
from typing import Any

__all__ = [
    "COIN_IDS",
    "COIN_NAMES",
    "CURRENCY_CODES",
    "CURRENCY_MARKERS",
    "DEFAULT_CITY",
    "WEATHER_MARKERS",
    "classify",
    "is_currency_command",
    "is_weather_command",
    "resolve",
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

# Криптовалюта: основа слова -> id CoinGecko.
COIN_IDS: dict[str, str] = {
    "биткоин": "bitcoin",
    "биткоина": "bitcoin",
    "биткойн": "bitcoin",
    "bitcoin": "bitcoin",
    "btc": "bitcoin",
    "эфириум": "ethereum",
    "эфир": "ethereum",
    "ethereum": "ethereum",
    "eth": "ethereum",
    "solana": "solana",
    "солана": "solana",
    "sol": "solana",
    "dogecoin": "dogecoin",
    "доги": "dogecoin",
    "додж": "dogecoin",
    "doge": "dogecoin",
    "toncoin": "toncoin",
    "тон": "toncoin",
    "ton": "toncoin",
    "ripple": "ripple",
    "рипл": "ripple",
    "xrp": "ripple",
    "litecoin": "litecoin",
    "лайткоин": "litecoin",
    "ltc": "litecoin",
}

COIN_NAMES: dict[str, str] = {
    "bitcoin": "биткоин",
    "ethereum": "эфир",
    "solana": "solana",
    "dogecoin": "dogecoin",
    "toncoin": "ton",
    "ripple": "xrp",
    "litecoin": "litecoin",
}

# «курс валют», «какой курс» — сущность не названа, можно дефолт.
GENERIC_CURRENCY_WORDS = {
    "валют",
    "валюты",
    "валюту",
    "сегодня",
    "сейчас",
    "вообще",
    "какой",
    "какая",
}

# «курс <сущность>» — чтобы поймать незнакомое слово после маркера.
_ENTITY_AFTER_MARKER = re.compile(
    r"(?:курс[а-я]*|стоит|стоимость|сколько)\s+([а-яёa-z]{3,})"
)
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


def _code(value: Any) -> str:
    """Нормализовать код/слово валюты: 'доллар'/'usd' -> 'USD'."""
    text = _norm(str(value or ""))
    if not text:
        return ""
    for stem, codes in CURRENCY_CODES.items():
        if stem in text:
            return codes[0]
    upper = str(value or "").strip().upper()
    return upper if len(upper) == 3 else ""


def _coin_id(value: Any) -> str:
    """Нормализовать монету: 'биткоина'/'BTC' -> 'bitcoin'."""
    text = _norm(str(value or ""))
    if not text:
        return ""
    if text in COIN_IDS:
        return COIN_IDS[text]
    for stem, coin in COIN_IDS.items():
        if stem in text:
            return coin
    return ""


def _unknown_entity(normalized: str) -> bool:
    """После «курс/стоит» названо слово, которого нет в справочниках."""
    match = _ENTITY_AFTER_MARKER.search(normalized)
    if not match:
        return False
    word = match.group(1)
    if word in GENERIC_CURRENCY_WORDS:
        return False
    if any(stem in word or word in stem for stem in CURRENCY_CODES):
        return False
    if any(stem in word or word in stem for stem in COIN_IDS):
        return False
    return True


def _weather_city(command: str, normalized: str) -> str | None:
    """Город из фразы «погода в <город>»; None — город не назван."""
    match = _CITY_AFTER_PREPOSITION.search(command or "")
    if match:
        return match.group(1)
    match = _CITY_AFTER_PREPOSITION.search(normalized)
    if match:
        return match.group(1).capitalize()
    cleaned = normalized
    for stop_word in ("погода", "погоду", "погоде", "температура", "дождь", "снег"):
        cleaned = cleaned.replace(stop_word, " ")
    for marker in WEATHER_MARKERS:
        cleaned = cleaned.replace(marker, " ")
    match = _CITY_WORD.search(command or "") or _CITY_WORD.search(cleaned)
    if match and match.group(1).lower() not in ("сегодня", "завтра", "сейчас", "будет"):
        return match.group(1)
    return None


def is_weather_command(command: str) -> bool:
    """Это запрос про погоду/температуру."""
    normalized = _norm(command)
    return any(marker in normalized for marker in WEATHER_MARKERS)


def is_currency_command(command: str) -> bool:
    """Это запрос про валютный курс (не крипта)."""
    params = resolve(None, command)
    return params is not None and params.get("kind") == "currency"


def resolve(args: dict[str, Any] | None, command: str = "") -> dict[str, str] | None:
    """Сущности для скилла facts или None (тогда ядро уходит в поиск).

    args от планировщика приоритетнее текста команды.
    """
    data = dict(args or {})
    text = " ".join(str(command or data.get("command") or "").split())
    normalized = _norm(text)
    kind = str(data.get("kind") or "").strip().lower()
    if kind not in ("currency", "crypto", "weather"):
        kind = ""

    # 1) Крипта раньше валют: «курс биткоина к доллару» — это крипта.
    coin = _coin_id(data.get("coin")) or _coin_id(normalized)
    if coin:
        vs = _code(data.get("quote") or data.get("vs")) or "usd"
        return {"kind": "crypto", "coin": coin, "vs": f"{vs.lower()},rub,eur"}
    if kind == "crypto":
        return None  # монета названа, но не маппится

    # 2) Валютная пара.
    base = _code(data.get("base"))
    quote = _code(data.get("quote"))
    mentions = _currency_mentions(normalized)
    if not base and mentions:
        foreign = [item for item in mentions if item[1] != "RUB"]
        base = (foreign[0] if foreign else mentions[0])[1]
        if len(foreign) > 1 and not quote:
            quote = foreign[1][1]
    if base:
        return {"kind": "currency", "base": base, "quote": quote or "RUB"}
    if kind == "currency" or any(marker in normalized for marker in CURRENCY_MARKERS):
        if _unknown_entity(normalized):
            return None  # «курс тугрика»: дефолтную пару не подставляем
        if "валют" in normalized or not _ENTITY_AFTER_MARKER.search(normalized):
            return {"kind": "currency", "base": "USD", "quote": "RUB"}

    # 3) Погода.
    if kind == "weather" or is_weather_command(text):
        city = str(data.get("city") or "").strip() or _weather_city(text, normalized)
        if city or kind == "weather" or is_weather_command(text):
            return {"kind": "weather", "city": city or DEFAULT_CITY}
    return None


def classify(command: str) -> dict[str, str] | None:
    """Совместимость: сущности только из текста команды."""
    return resolve(None, command)
