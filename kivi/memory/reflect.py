"""Проход извлечения фактов из диалога в vault.

Один вызов LLM (task_type="memory") со строгим JSON-ответом:
    {"facts":[{"text","kind","who"}], "decisions":[{"what","why"}], "open_loops":[{"what"}]}
Факты раскладываются по заметкам с датой, решения и открытые вопросы — в journal дня.
Не чаще раза в 5 минут.
"""

from __future__ import annotations

import json
import re
import time
from datetime import date
from pathlib import Path

from kivi.config import get_settings
from kivi.log import logger
from kivi.memory.vault import add_fact, read_note, vault_root, write_note

__all__ = ["MIN_INTERVAL_S", "REFLECT_PROMPT", "Reflector"]

MIN_INTERVAL_S = 300.0  # не чаще раза в 5 минут
MAX_DIALOG_CHARS = 6000

REFLECT_PROMPT = (
    "Ты — хранитель долгой памяти голосового ассистента Киви.\n"
    "Разбери диалог и верни СТРОГО один JSON без markdown и без пояснений:\n"
    '{"facts":[{"text":"...","kind":"profile|person|topic","who":"..."}],'
    '"decisions":[{"what":"...","why":"..."}],'
    '"open_loops":[{"what":"..."}]}\n'
    "Правила:\n"
    "- facts: только устойчивые факты о Лёше и его окружении (имена, даты, привычки, "
    "предпочтения, техника). Не выдумывай, не пиши мелочи диалога.\n"
    "- kind=profile — про Лёшу; person — про другого человека (who = имя); "
    "topic — долгоживущая тема/проект (who = тема).\n"
    "- decisions: что решили и почему. open_loops: что осталось сделать.\n"
    "- Если нечего — верни пустые списки.\n"
)


class Reflector:
    """Проход рефлексии: диалог -> факты/решения/вопросы -> vault."""

    def __init__(
        self,
        router,
        *,
        vault_path: str | Path | None = None,
        min_interval_s: float = MIN_INTERVAL_S,
        enabled: bool = True,
    ) -> None:
        self.router = router
        self.vault_path = vault_path
        self.min_interval_s = float(min_interval_s)
        cfg = get_settings().section("memory")
        self.enabled = bool(enabled and cfg.get("enabled", True))
        self._last_run = 0.0
        self.last_cost_rub = 0.0

    def should_run(
        self,
        *,
        turns_since: int = 0,
        turns_threshold: int | None = None,
        idle_seconds: float = 0.0,
        idle_min: float | None = None,
    ) -> bool:
        """Пора ли делать проход (каждые N реплик или после простоя)."""
        if not self.enabled or turns_since <= 0:
            return False
        cfg = get_settings().section("memory")
        threshold = int(turns_threshold or cfg.get("reflect_every_turns") or 20)
        idle = float(
            idle_min if idle_min is not None else (cfg.get("reflect_on_idle_min") or 10)
        ) * 60.0
        if time.monotonic() - self._last_run < self.min_interval_s:
            return False
        return turns_since >= threshold or idle_seconds >= idle

    # --- сам проход ---

    def run(self, messages: list[dict] | None = None, *, force: bool = False) -> dict:
        """Извлечь факты. Возвращает статистику прохода (и стоимость)."""
        stats = {"facts": 0, "decisions": 0, "open_loops": 0, "cost_rub": 0.0, "skipped": ""}
        if not self.enabled:
            stats["skipped"] = "выключено в конфиге"
            return stats
        if not force and time.monotonic() - self._last_run < self.min_interval_s:
            stats["skipped"] = "не чаще раза в 5 минут"
            logger.debug("Reflect: пропуск — {}", stats["skipped"])
            return stats
        if not messages:
            stats["skipped"] = "нет реплик"
            return stats

        dialog = "\n".join(
            f"{item.get('role')}: {item.get('content')}"
            for item in messages
            if item.get("content")
        )[-MAX_DIALOG_CHARS:]
        if not dialog.strip():
            stats["skipped"] = "пустой диалог"
            return stats

        self._last_run = time.monotonic()
        try:
            result = self.router.complete(
                [
                    {"role": "system", "content": REFLECT_PROMPT},
                    {"role": "user", "content": dialog},
                ],
                task_type="memory",
            )
        except Exception as exc:  # noqa: BLE001 - рефлексия не должна ломать диалог
            logger.warning("Reflect: LLM не ответил: {}: {}", type(exc).__name__, exc)
            stats["skipped"] = f"ошибка LLM: {exc}"
            return stats

        stats["cost_rub"] = float((result or {}).get("cost_rub") or 0.0)
        self.last_cost_rub = stats["cost_rub"]
        payload = self._parse_json(str((result or {}).get("text") or ""))
        if payload is None:
            logger.warning("Reflect: не удалось разобрать JSON: {!r}", str(result)[:200])
            stats["skipped"] = "не JSON"
            return stats

        stats["facts"] = self._save_facts(payload.get("facts") or [])
        stats["decisions"] = len(payload.get("decisions") or [])
        stats["open_loops"] = len(payload.get("open_loops") or [])
        self._write_journal(payload)
        logger.info(
            "Reflect: фактов {}, решений {}, вопросов {}, стоимость {:.5f} руб",
            stats["facts"],
            stats["decisions"],
            stats["open_loops"],
            stats["cost_rub"],
        )
        return stats

    # --- внутреннее ---

    @staticmethod
    def _parse_json(raw: str) -> dict | None:
        text = str(raw).strip()
        fenced = re.search(r"```(?:json)?\s*(\{.*?\})\s*```", text, re.DOTALL)
        if fenced:
            text = fenced.group(1)
        start, end = text.find("{"), text.rfind("}")
        if start < 0 or end <= start:
            return None
        try:
            payload = json.loads(text[start : end + 1])
        except json.JSONDecodeError:
            return None
        return payload if isinstance(payload, dict) else None

    def _save_facts(self, facts: list) -> int:
        saved = 0
        for item in facts:
            if not isinstance(item, dict):
                continue
            text = str(item.get("text") or "").strip()
            if not text:
                continue
            if add_fact(
                text,
                kind=str(item.get("kind") or "profile"),
                who=str(item.get("who") or ""),
                path=self.vault_path,
            ):
                saved += 1
        return saved

    def _write_journal(self, payload: dict) -> None:
        day = date.today().isoformat()
        root = vault_root(self.vault_path)
        existing = read_note("journal", day, root)
        blocks: list[str] = []

        summary = self._summary(payload)
        if summary:
            blocks.append("## Выжимка\n" + summary)

        decisions = payload.get("decisions") or []
        if decisions:
            lines = [f"## Решения"]
            for item in decisions:
                what = str((item or {}).get("what") or "").strip()
                why = str((item or {}).get("why") or "").strip()
                if what:
                    line = f"- [{day}] {what}" + (f" — {why}" if why else "")
                    if line not in existing:
                        lines.append(line)
            if len(lines) > 1:
                blocks.append("\n".join(lines))

        loops = payload.get("open_loops") or []
        if loops:
            lines = ["## Открытые вопросы"]
            for item in loops:
                what = str((item or {}).get("what") or "").strip()
                if what:
                    line = f"- [{day}] {what}"
                    if line not in existing:
                        lines.append(line)
            if len(lines) > 1:
                blocks.append("\n".join(lines))

        if not blocks:
            return
        header = "" if existing.strip() else f"# {day}\n\n"
        content = (existing.rstrip("\n") + "\n\n" if existing.strip() else header)
        content += "\n\n".join(blocks) + "\n"
        write_note("journal", day, content, root)
        logger.info("Reflect: journal обновлён — {}", day)

    @staticmethod
    def _summary(payload: dict) -> str:
        sentences: list[str] = []
        for item in payload.get("decisions") or []:
            what = str((item or {}).get("what") or "").strip()
            if what:
                sentences.append(f"Решили: {what}.")
            if len(sentences) >= 4:
                break
        for item in payload.get("open_loops") or []:
            what = str((item or {}).get("what") or "").strip()
            if what:
                sentences.append(f"Осталось: {what}.")
            if len(sentences) >= 6:
                break
        return " ".join(sentences[:6])
