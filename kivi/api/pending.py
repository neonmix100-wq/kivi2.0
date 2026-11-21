"""Ожидание ответов оверлея: ask/confirm с threading.Event и таймаутом.

Если ни один клиент не подключён — headless-режим: ask сразу None,
confirm сразу False (как раньше без оверлея).
"""

from __future__ import annotations

import threading
import uuid
from collections.abc import Callable
from typing import Any

from loguru import logger

__all__ = [
    "ASK_TIMEOUT_DEFAULT",
    "CONFIRM_TIMEOUT_DEFAULT",
    "PendingRegistry",
]

ASK_TIMEOUT_DEFAULT = 60.0
CONFIRM_TIMEOUT_DEFAULT = 120.0


class PendingRegistry:
    """Реестр ожидающих ask/confirm; ответ приходит из WS-команды."""

    def __init__(
        self,
        *,
        connected: Callable[[], bool],
        send: Callable[[dict[str, Any]], bool],
        ask_timeout_s: float = ASK_TIMEOUT_DEFAULT,
        confirm_timeout_s: float = CONFIRM_TIMEOUT_DEFAULT,
    ) -> None:
        self._connected = connected
        self._send = send
        self.ask_timeout_s = max(1.0, float(ask_timeout_s))
        self.confirm_timeout_s = max(0.1, float(confirm_timeout_s))
        self._lock = threading.Lock()
        self._pending: dict[str, dict[str, Any]] = {}

    @property
    def pending_count(self) -> int:
        with self._lock:
            return len(self._pending)

    def _register(self, kind: str, event: threading.Event) -> str:
        request_id = uuid.uuid4().hex[:12]
        with self._lock:
            self._pending[request_id] = {
                "kind": kind,
                "event": event,
                "answer": None,
                "ok": False,
            }
        return request_id

    def _drop(self, request_id: str) -> dict[str, Any] | None:
        with self._lock:
            return self._pending.pop(request_id, None)

    def _dispatch(self, request_id: str, event: dict[str, Any]) -> bool:
        try:
            sent = bool(self._send(event))
        except Exception as exc:  # noqa: BLE001 - оверлей не должен ронять скилл
            logger.warning("оверлей: не удалось отправить {}: {}", event.get("event"), exc)
            sent = False
        if not sent:
            self._drop(request_id)
        return sent

    def ask(self, question: str, options: list[str] | None = None) -> str | None:
        """Задать вопрос; None — нет клиентов, таймаут или ошибка отправки."""
        if not self._connected():
            logger.info("оверлей: нет клиентов — ask пропущен")
            return None
        event = threading.Event()
        request_id = self._register("ask", event)
        payload = {
            "event": "ask",
            "id": request_id,
            "question": str(question),
            "options": [str(item) for item in (options or [])],
        }
        if not self._dispatch(request_id, payload):
            return None
        answered = event.wait(self.ask_timeout_s)
        entry = self._drop(request_id)
        if not answered or entry is None:
            logger.warning(
                "оверлей: ask {} без ответа за {:.0f} с",
                request_id,
                self.ask_timeout_s,
            )
            return None
        answer = entry.get("answer")
        return None if answer is None else str(answer)

    def confirm(
        self,
        title: str,
        body: str = "",
        details: str = "",
    ) -> bool:
        """Запросить подтверждение; False — нет клиентов/таймаут/отказ."""
        if not self._connected():
            logger.info("оверлей: нет клиентов — confirm отклонён")
            return False
        event = threading.Event()
        request_id = self._register("confirm", event)
        payload = {
            "event": "confirm",
            "id": request_id,
            "title": str(title),
            "body": str(body),
            "details": str(details),
        }
        if not self._dispatch(request_id, payload):
            return False
        answered = event.wait(self.confirm_timeout_s)
        entry = self._drop(request_id)
        if not answered or entry is None:
            logger.warning(
                "оверлей: confirm {} без ответа за {:.0f} с",
                request_id,
                self.confirm_timeout_s,
            )
            return False
        return bool(entry.get("ok"))

    def resolve_ask(self, request_id: str, answer: Any) -> bool:
        """Клиент ответил на ask. True — запрос существовал."""
        with self._lock:
            entry = self._pending.get(str(request_id))
            if entry is None or entry.get("kind") != "ask":
                return False
            entry["answer"] = "" if answer is None else str(answer)
            entry["event"].set()
        return True

    def resolve_confirm(self, request_id: str, ok: bool) -> bool:
        """Клиент ответил на confirm. True — запрос существовал."""
        with self._lock:
            entry = self._pending.get(str(request_id))
            if entry is None or entry.get("kind") != "confirm":
                return False
            entry["ok"] = bool(ok)
            entry["event"].set()
        return True
