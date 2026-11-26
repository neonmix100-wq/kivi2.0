"""Извлечение события расписания из реплики (cheap, task_type=intent).

Локальный парсер kivi.memory.schedule покрывает типовые фразы; LLM нужна
только для нестандартных формулировок. Ответ строго JSON:
    {"days": "ВС-ПТ", "start": "19:00", "end": "23:00", "what": "работа"}
days может быть ключевым словом (будни/выходные/ежедневно) или ДД.ММ.
"""

from __future__ import annotations

import json
import re
from typing import Any

from loguru import logger

__all__ = ["PROMPT", "extract_schedule"]

PROMPT = (
    "Извлеки событие расписания из реплики.\n"
    "Реплика: {command}\n"
    "Ответь только JSON: {{days, start, end, what}}.\n"
    "days — дни недели (ПН-ПТ, ВС-ПТ, СБ,ВС, будни, выходные, ежедневно) "
    "или дата ДД.ММ для разового события.\n"
    "start/end — время HH:MM; what — короткий текст события без служебных слов."
)


def _json(raw: str) -> Any:
    text = str(raw or "").strip()
    if text.startswith("```"):
        text = re.sub(r"^```(?:json)?\s*", "", text, flags=re.IGNORECASE)
        text = re.sub(r"\s*```$", "", text)
    try:
        return json.loads(text)
    except ValueError:
        pass
    start = text.find("{")
    end = text.rfind("}")
    if 0 <= start < end:
        try:
            return json.loads(text[start : end + 1])
        except ValueError:
            return None
    return None


def extract_schedule(command: str, *, router: Any | None = None) -> dict[str, str] | None:
    """Спросить cheap-LLM о days/start/end/what. Ошибка -> None."""
    text = " ".join(str(command or "").split())
    if not text:
        return None
    try:
        if router is None:
            from kivi.brain.router import Router

            router = Router()
        messages = [
            {"role": "system", "content": "Ты извлекатель расписания. Отвечай только JSON."},
            {"role": "user", "content": PROMPT.format(command=text)},
        ]
        result = router.complete(messages, task_type="intent", max_tokens=120)
    except Exception as exc:  # noqa: BLE001 - remember не должен падать без LLM
        logger.warning("schedule: извлечение через LLM не удалось: {}", type(exc).__name__)
        return None
    payload = _json(str((result or {}).get("text") or ""))
    if not isinstance(payload, dict):
        return None
    cleaned = {
        key: " ".join(str(payload.get(key) or "").split())
        for key in ("days", "start", "end", "what")
    }
    return cleaned if cleaned["what"] else None
