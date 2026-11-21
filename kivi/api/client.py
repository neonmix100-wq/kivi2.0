"""Синхронный WS-клиент к loopback API ядра (для демо и внешних утилит)."""

from __future__ import annotations

import json
import threading
from typing import Any

from websockets.sync.client import connect

__all__ = ["ApiClient", "DEFAULT_URI"]

DEFAULT_URI = "ws://127.0.0.1:8765"


class ApiClient:
    """Запрос/ответ по WS; входящие события (status/log/...) пропускает."""

    def __init__(
        self,
        uri: str = DEFAULT_URI,
        *,
        open_timeout: float = 5.0,
        request_timeout: float = 10.0,
        ask_timeout: float = 90.0,
    ) -> None:
        self.uri = uri
        self.request_timeout = float(request_timeout)
        self.ask_timeout = float(ask_timeout)
        self._lock = threading.RLock()
        self._ws = connect(uri, open_timeout=open_timeout, legacy=True)

    def request(self, command: str, timeout: float | None = None, **fields: Any) -> dict[str, Any]:
        payload = {"command": command, **fields}
        limit = float(timeout if timeout is not None else self.request_timeout)
        with self._lock:
            self._ws.send(json.dumps(payload, ensure_ascii=False))
            while True:
                raw = self._ws.recv(timeout=limit)
                message = json.loads(raw)
                if isinstance(message, dict) and "ok" in message:
                    return message

    def ask(self, question: str, options: list[str] | None = None) -> str | None:
        reply = self.request(
            "ask",
            timeout=self.ask_timeout,
            question=str(question),
            options=[str(item) for item in (options or [])],
        )
        answer = reply.get("answer")
        return None if answer is None else str(answer)

    def confirm(self, title: str, body: str = "", details: str = "") -> bool:
        reply = self.request(
            "confirm",
            timeout=self.ask_timeout,
            title=str(title),
            body=str(body),
            details=str(details),
        )
        return bool(reply.get("confirmed"))

    def info(self, text: str) -> None:
        self.request("info", text=str(text))

    def close(self) -> None:
        with self._lock:
            try:
                self._ws.close()
            except Exception:  # noqa: BLE001
                pass

    def __enter__(self) -> "ApiClient":
        return self

    def __exit__(self, *exc_info: Any) -> bool:
        self.close()
        return False
