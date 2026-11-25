"""Скилл facts: курс валют и погода из открытых API без ключей.

Источники (все бесплатные, без регистрации):
  * курс      — https://open.er-api.com/v6/latest/{BASE}
  * геокодинг — https://geocoding-api.open-meteo.com/v1/search
  * погода    — https://api.open-meteo.com/v1/forecast

При сбое источника скилл НЕ говорит «не умею»: возвращает FALLBACK_MARKER,
и ядро уходит в обычный поиск (там есть шанс найти цифру в тексте страницы).

Использование (через брокер):
    handle(ctx, kind="currency", base="USD", quote="RUB")
    handle(ctx, kind="weather", city="Новосибирск")
    handle(ctx, command="курс доллара к рублю")
"""

from __future__ import annotations

import json
from typing import Any
from urllib.parse import quote as url_quote

from kivi.skills.facts_intent import DEFAULT_CITY, classify

TIMEOUT_S = 6.0

ER_API = "https://open.er-api.com/v6/latest/{base}"
GEO_API = (
    "https://geocoding-api.open-meteo.com/v1/search"
    "?name={city}&count=1&language=ru&format=json"
)
FORECAST_API = (
    "https://api.open-meteo.com/v1/forecast"
    "?latitude={lat:.4f}&longitude={lon:.4f}"
    "&current=temperature_2m,weather_code,wind_speed_10m&timezone=auto"
)

# WMO weather_code -> короткий русский текст.
WEATHER_CODES: dict[int, str] = {
    0: "ясно",
    1: "преимущественно ясно",
    2: "переменная облачность",
    3: "пасмурно",
    45: "туман",
    48: "изморозь",
    51: "слабая морось",
    53: "морось",
    55: "сильная морось",
    56: "ледяная морось",
    57: "сильная ледяная морось",
    61: "небольшой дождь",
    63: "дождь",
    65: "сильный дождь",
    66: "ледяной дождь",
    67: "сильный ледяной дождь",
    71: "небольшой снег",
    73: "снег",
    75: "сильный снег",
    77: "снежные зёрна",
    80: "небольшой ливень",
    81: "ливень",
    82: "сильный ливень",
    85: "снегопад",
    86: "сильный снегопад",
    95: "гроза",
    96: "гроза с градом",
    99: "сильная гроза с градом",
}

CURRENCY_NAMES = {
    "USD": "доллара",
    "EUR": "евро",
    "RUB": "рубля",
    "CNY": "юаня",
    "JPY": "йены",
    "GBP": "фунта",
    "KZT": "тенге",
    "UAH": "гривны",
    "TRY": "лиры",
}

# Дательный падеж для «курс доллара К рублю».
CURRENCY_DATIVE = {
    "USD": "доллару",
    "EUR": "евро",
    "RUB": "рублю",
    "CNY": "юаню",
    "JPY": "йене",
    "GBP": "фунту",
    "KZT": "тенге",
    "UAH": "гривне",
    "TRY": "лире",
}


def _prepositional(name: str) -> str:
    """«Новосибирск» -> «Новосибирске», «Москва» -> «Москве»."""
    value = " ".join(str(name or "").split())
    if not value:
        return value
    lower = value.lower()
    if lower.endswith(("о", "е", "и", "ы", "у", "ю")):
        return value
    if lower.endswith("а"):
        return value[:-1] + "е"
    if lower.endswith("ь"):
        return value[:-1] + "и"
    return value + "е"


def _fallback() -> str:
    """Маркер для ядра: источник не дал данных, уходим в поиск."""
    from kivi.skills.loader import FALLBACK_MARKER

    return FALLBACK_MARKER


def _format_rate(value: float) -> str:
    """92.4; 3.15; 0.0123 — без хвостовых нулей."""
    if value >= 1000:
        return f"{value:,.0f}".replace(",", " ")
    if value >= 100:
        return f"{value:.1f}".rstrip("0").rstrip(".")
    if value >= 1:
        return f"{value:.2f}".rstrip("0").rstrip(".")
    return f"{value:.4f}".rstrip("0").rstrip(".")


def _quote_suffix(code: str, name: str) -> str:
    """«₽» для рубля, иначе код валюты."""
    return "₽" if code == "RUB" else (code or name)


def _currency(ctx, base: str, quote: str, base_name: str, quote_name: str) -> str:
    """Курс base -> quote из open.er-api.com."""
    code_base = str(base or "USD").upper()
    code_quote = str(quote or "RUB").upper()
    url = ER_API.format(base=url_quote(code_base))
    raw = ctx.http_get(url, timeout=TIMEOUT_S)
    payload = json.loads(raw)
    result = str(payload.get("result") or "success")
    if result != "success":
        ctx.log(f"facts: er-api result={result}")
        return _fallback()
    rates = payload.get("rates") or {}
    if code_quote not in rates:
        ctx.log(f"facts: в ответе нет {code_quote}")
        return _fallback()
    rate = float(rates[code_quote])
    suffix = _quote_suffix(code_quote, quote_name)
    ctx.log(f"facts: {code_base}->{code_quote} = {rate}")
    return (
        f"Курс {base_name} к {quote_name}: {_format_rate(rate)} {suffix}"
        f" (обновлено сегодня)."
    )



def _city_variants(city: str) -> list[str]:
    """«Новосибирске» -> «Новосибирск», «Москве» -> «Москва»."""
    base = " ".join(str(city or "").split())
    if not base:
        return [DEFAULT_CITY]
    variants = [base]
    if len(base) > 4 and base[-1].lower() in "еиыуюяа":
        short = base[:-1]
        variants.append(short)
        variants.append(short + "а")
    unique: list[str] = []
    for item in variants:
        if item and item not in unique:
            unique.append(item)
    return unique


def _geocode(ctx, city: str) -> tuple[float, float, str] | None:
    """Координаты и нормальное имя города."""
    for candidate in _city_variants(city):
        url = GEO_API.format(city=url_quote(candidate))
        try:
            payload = json.loads(ctx.http_get(url, timeout=TIMEOUT_S))
        except Exception as exc:  # noqa: BLE001 - пробуем следующий вариант
            ctx.log(f"facts: геокодинг {candidate} упал: {exc}")
            continue
        results = payload.get("results") or []
        if not results:
            continue
        first = results[0]
        try:
            lat = float(first.get("latitude"))
            lon = float(first.get("longitude"))
        except (TypeError, ValueError):
            continue
        name = str(first.get("name") or candidate)
        return lat, lon, name
    return None


def _weather(ctx, city: str) -> str:
    """Короткая сводка погоды по городу."""
    place = _geocode(ctx, city)
    if place is None:
        ctx.log(f"facts: город {city!r} не найден")
        return _fallback()
    lat, lon, name = place
    url = FORECAST_API.format(lat=lat, lon=lon)
    payload = json.loads(ctx.http_get(url, timeout=TIMEOUT_S))
    current = payload.get("current") or {}
    if "temperature_2m" not in current:
        ctx.log("facts: в ответе погоды нет temperature_2m")
        return _fallback()
    try:
        temperature = float(current.get("temperature_2m"))
    except (TypeError, ValueError):
        return _fallback()
    try:
        wind = float(current.get("wind_speed_10m") or 0.0)
    except (TypeError, ValueError):
        wind = 0.0
    try:
        code = int(current.get("weather_code") or 0)
    except (TypeError, ValueError):
        code = 0
    description = WEATHER_CODES.get(code, "без осадков")
    ctx.log(f"facts: погода {name}: {temperature} °C, code={code}")
    return (
        f"В {_prepositional(name)} сейчас {temperature:+.0f} °C, {description}, "
        f"ветер {wind:.0f} м/с."
    )


def handle(ctx, **args: Any) -> str:
    """Ответ о курсе валюты или погоде; при сбое — маркер fallback."""
    command = " ".join(str(args.get("command") or args.get("query") or "").split())
    params: dict[str, str] = {}
    kind = str(args.get("kind") or "").strip().lower()
    if kind in ("currency", "weather"):
        params = {"kind": kind}
        for key in ("base", "quote", "base_name", "quote_name", "city"):
            value = str(args.get(key) or "").strip()
            if value:
                params[key] = value.upper() if key in ("base", "quote") else value
    elif command:
        detected = classify(command)
        if detected is not None:
            params = dict(detected)

    if not params:
        ctx.log(f"facts: не понял запрос {command!r}")
        return _fallback()

    try:
        if params.get("kind") == "currency":
            base = params.get("base", "USD")
            quote_code = params.get("quote", "RUB")
            answer = _currency(
                ctx,
                base,
                quote_code,
                params.get("base_name") or CURRENCY_NAMES.get(base, base),
                CURRENCY_DATIVE.get(quote_code)
                or params.get("quote_name")
                or quote_code,
            )
        else:
            answer = _weather(ctx, params.get("city") or DEFAULT_CITY)
    except Exception as exc:  # noqa: BLE001 - сеть/JSON: уходим в поиск
        ctx.log(f"facts: сбой источника: {type(exc).__name__}: {exc}")
        return _fallback()
    return answer
