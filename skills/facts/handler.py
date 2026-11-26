"""Скилл facts: курс валют, курс крипты и погода — данные из открытых API.

Источники (бесплатные, без ключей):
  * валюта    — https://open.er-api.com/v6/latest/{BASE}
  * крипта    — https://api.coingecko.com/api/v3/simple/price
  * геокодинг — https://geocoding-api.open-meteo.com/v1/search
  * погода    — https://api.open-meteo.com/v1/forecast

Скилл НЕ сочиняет текст: возвращает СТРУКТУРИРОВАННЫЙ JSON
{"source", "kind", "values", "updated", ...}. Формулировкой ответа занимается
ядро (Router.complete, тир coder), поэтому шаблонных фраз здесь нет.

Если сущность названа, но не маппится (неизвестная монета/валюта) — возвращаем
FALLBACK_MARKER, и ядро уходит в поиск. Дефолты — только для общего вопроса.

Использование (через брокер):
    handle(ctx, kind="crypto", coin="bitcoin")
    handle(ctx, kind="currency", base="USD", quote="RUB")
    handle(ctx, kind="weather", city="Новосибирск")
    handle(ctx, command="курс биткоина к доллару")
"""

from __future__ import annotations

import json
from typing import Any
from urllib.parse import quote as url_quote

from kivi.skills.facts_intent import COIN_NAMES, DEFAULT_CITY, resolve

TIMEOUT_S = 6.0

ER_API = "https://open.er-api.com/v6/latest/{base}"
COINGECKO_API = (
    "https://api.coingecko.com/api/v3/simple/price"
    "?ids={ids}&vs_currencies={vs}&include_last_updated_at=true"
)
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

def _fallback() -> str:
    """Маркер для ядра: данных нет или сущность не маппится — уходим в поиск."""
    from kivi.skills.loader import FALLBACK_MARKER

    return FALLBACK_MARKER


def _updated() -> str:
    """Метка времени получения данных (часовой пояс проекта, ISO)."""
    from datetime import datetime

    from kivi.skills.broker import project_timezone

    return datetime.now(project_timezone()).isoformat(timespec="minutes")


def _currency(ctx, base: str, quote: str) -> dict[str, Any] | None:
    """Курс base -> quote из open.er-api.com. None — данных нет."""
    code_base = str(base or "").upper()
    code_quote = str(quote or "").upper()
    if not code_base or not code_quote:
        return None
    url = ER_API.format(base=url_quote(code_base))
    raw = ctx.http_get(url, timeout=TIMEOUT_S)
    payload = json.loads(raw)
    result = str(payload.get("result") or "success")
    if result != "success":
        ctx.log(f"facts: er-api result={result}")
        return None
    rates = payload.get("rates") or {}
    if code_quote not in rates:
        ctx.log(f"facts: в ответе нет {code_quote}")
        return None
    rate = float(rates[code_quote])
    ctx.log(f"facts: {code_base}->{code_quote} = {rate}")
    return {
        "source": "er-api",
        "kind": "currency",
        "base": code_base,
        "quote": code_quote,
        "values": {"rate": rate},
        "updated": _updated(),
    }


def _crypto(ctx, coin: str, vs: str) -> dict[str, Any] | None:
    """Курс монеты из CoinGecko: values = {usd, rub, eur, ...}."""
    url = COINGECKO_API.format(ids=url_quote(coin), vs=url_quote(vs))
    raw = ctx.http_get(url, timeout=TIMEOUT_S)
    payload = json.loads(raw)
    row = payload.get(coin) if isinstance(payload, dict) else None
    if not isinstance(row, dict):
        ctx.log(f"facts: coingecko не вернул {coin}")
        return None
    values = {
        str(code).lower(): float(value)
        for code, value in row.items()
        if code != "last_updated_at" and isinstance(value, (int, float))
    }
    if not values:
        ctx.log(f"facts: coingecko пустые значения для {coin}")
        return None
    ctx.log(f"facts: {coin} = {values}")
    return {
        "source": "coingecko",
        "kind": "crypto",
        "coin": coin,
        "name": COIN_NAMES.get(coin, coin),
        "values": values,
        "vs": vs,
        "updated": _updated(),
    }



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


def _weather(ctx, city: str) -> dict[str, Any] | None:
    """Короткая сводка погоды по городу (структурированные данные)."""
    place = _geocode(ctx, city)
    if place is None:
        ctx.log(f"facts: город {city!r} не найден")
        return None
    lat, lon, name = place
    url = FORECAST_API.format(lat=lat, lon=lon)
    payload = json.loads(ctx.http_get(url, timeout=TIMEOUT_S))
    current = payload.get("current") or {}
    if "temperature_2m" not in current:
        ctx.log("facts: в ответе погоды нет temperature_2m")
        return None
    try:
        temperature = float(current.get("temperature_2m"))
    except (TypeError, ValueError):
        return None
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
    return {
        "source": "open-meteo",
        "kind": "weather",
        "city": name,
        "values": {
            "temperature_c": temperature,
            "weather": description,
            "weather_code": code,
            "wind_ms": wind,
        },
        "updated": _updated(),
    }


def handle(ctx, **args: Any) -> str:
    """Структурированные данные о курсе/погоде (JSON) или FALLBACK_MARKER.

    FALLBACK_MARKER — сигнал ядру «иди в поиск»: сущность названа, но не
    маппится, либо источник не ответил. Дефолтную пару вместо названной
    сущности НЕ подставляем.
    """
    command = " ".join(str(args.get("command") or args.get("query") or "").split())
    params = resolve(args, command)
    if params is None:
        ctx.log(f"facts: сущность не распознана в {command!r} — уходим в поиск")
        return _fallback()

    try:
        kind = params.get("kind")
        if kind == "crypto":
            data = _crypto(ctx, params["coin"], params.get("vs") or "usd,rub,eur")
        elif kind == "currency":
            data = _currency(ctx, params.get("base", ""), params.get("quote", ""))
        else:
            data = _weather(ctx, params.get("city") or DEFAULT_CITY)
    except Exception as exc:  # noqa: BLE001 - сеть/JSON: уходим в поиск
        ctx.log(f"facts: сбой источника: {type(exc).__name__}: {exc}")
        return _fallback()
    if not data:
        return _fallback()
    return json.dumps(data, ensure_ascii=False)
