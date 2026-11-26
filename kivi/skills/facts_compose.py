"""Композиция ответа по данным скилла facts — думающей моделью.

Скилл facts отдаёт только цифры (JSON). Здесь они превращаются в короткую
фразу: сначала пытаемся через Router.complete(task_type="chat" — тир coder),
а если LLM недоступна — собираем честный детерминированный текст, чтобы
никогда не отвечать шаблоном «не умею».

Использование:
    from kivi.skills.facts_compose import compose_answer, parse_payload
    data = parse_payload('{"kind": "crypto", "coin": "bitcoin", ...}')
    text, cost = compose_answer("курс биткоина", data, history, router)
"""

from __future__ import annotations

import json
from typing import Any

from kivi.brain.persona import to_speech
from kivi.log import logger

__all__ = ["COMPOSE_PROMPT", "compose_answer", "deterministic_answer", "parse_payload"]

COMPOSE_PROMPT = """Сформулируй короткий ответ (1-2 предложения) на вопрос, \
используя ТОЛЬКО данные: {data}.
Вопрос: {command}.
История: {history}.
Если пользователь поправляет — учти поправку и ответь именно про исправленную \
сущность, а не про прежнюю.
Не выдумывай чисел и фактов, которых нет в данных.
Без markdown, без перечисления вариантов."""

CURRENCY_GENITIVE = {
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


def parse_payload(text: str) -> dict[str, Any] | None:
    """JSON-данные скилла facts или None (маркер/ошибка/обычный текст)."""
    raw = str(text or "").strip()
    if not raw.startswith("{"):
        return None
    try:
        payload = json.loads(raw)
    except (ValueError, TypeError):
        return None
    if not isinstance(payload, dict) or "kind" not in payload:
        return None
    return payload


def _number(value: Any) -> str:
    """1 234 567; 91 234.5; 92.4 — без хвостовых нулей."""
    try:
        amount = float(value)
    except (TypeError, ValueError):
        return str(value)
    if amount >= 1000:
        return f"{amount:,.0f}".replace(",", " ")
    if amount >= 1:
        return f"{amount:.2f}".rstrip("0").rstrip(".")
    return f"{amount:.6f}".rstrip("0").rstrip(".")


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


def deterministic_answer(data: dict[str, Any]) -> str:
    """Текст без LLM: только то, что пришло от источника."""
    kind = str(data.get("kind") or "")
    values = data.get("values") or {}
    if kind == "currency":
        base = str(data.get("base") or "").upper()
        quote = str(data.get("quote") or "").upper()
        rate = values.get("rate")
        base_name = CURRENCY_GENITIVE.get(base, base)
        quote_name = CURRENCY_DATIVE.get(quote, quote)
        suffix = "₽" if quote == "RUB" else quote
        return f"Курс {base_name} к {quote_name}: {_number(rate)} {suffix}."
    if kind == "crypto":
        name = str(data.get("name") or data.get("coin") or "монета")
        usd = values.get("usd")
        parts = [f"{name.capitalize()} стоит {_number(usd)} $" if usd else f"{name.capitalize()}: данные получены"]
        if values.get("rub"):
            parts.append(f"это {_number(values['rub'])} ₽")
        return ", ".join(parts) + "."
    if kind == "weather":
        city = _prepositional(str(data.get("city") or ""))
        temperature = values.get("temperature_c")
        weather = str(values.get("weather") or "без осадков")
        wind = values.get("wind_ms")
        sentence = f"В {city} сейчас {float(temperature):+.0f} °C, {weather}"
        if wind is not None:
            sentence += f", ветер {float(wind):.0f} м/с"
        return sentence + "."
    return "Данные получены."


def _history_text(history: list[dict[str, Any]] | None, limit: int = 4) -> str:
    lines: list[str] = []
    for item in list(history or [])[-limit:]:
        if not isinstance(item, dict):
            continue
        role = str(item.get("role") or "").strip() or "?"
        content = " ".join(str(item.get("content") or "").split())
        if content:
            lines.append(f"{role}: {content}")
    return "\n".join(lines) or "(пусто)"


def compose_answer(
    command: str,
    data: dict[str, Any],
    history: list[dict[str, Any]] | None = None,
    *,
    router: Any | None = None,
) -> tuple[str, float]:
    """Сформулировать ответ по данным. Возвращает (текст, стоимость руб)."""
    fallback = deterministic_answer(data)
    if router is None:
        return fallback, 0.0
    payload = json.dumps(data, ensure_ascii=False)
    messages = [
        {
            "role": "system",
            "content": COMPOSE_PROMPT.format(
                data=payload,
                command=str(command or ""),
                history=_history_text(history),
            ),
        },
        {"role": "user", "content": str(command or "")},
    ]
    try:
        result = router.complete(messages, task_type="chat")
    except Exception as exc:  # noqa: BLE001 - сеть/бюджет: остаёмся с фактами
        logger.warning("facts: композиция не удалась: {}: {}", type(exc).__name__, exc)
        return fallback, 0.0
    body = result or {}
    text = to_speech(str(body.get("text") or ""))
    cost = float(body.get("cost_rub") or 0.0)
    if not text:
        return fallback, cost
    logger.info("facts: ответ сформулирован моделью ({} симв., {:.5f} руб)", len(text), cost)
    return text, cost
