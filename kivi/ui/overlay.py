"""OverlayService: вопросы/подтверждения/инфо через WS-API (без tkinter).

Интерфейс прежний: ask(question, options) -> str|None,
confirm(title, body, details) -> bool, info(text) -> None.
Реализация — server.broadcast + pending.wait (headless -> None/False).
"""

from __future__ import annotations

from typing import Any

from loguru import logger

__all__ = ["OverlayService"]


class OverlayService:
    """Тонкая обёртка над ApiServer для скиллов и пробелов."""

    def __init__(self, api: Any | None = None) -> None:
        self.api = api

    def attach(self, api: Any) -> None:
        self.api = api

    def ask(self, question: str, options: list[str] | None = None) -> str | None:
        if self.api is None:
            return None
        return self.api.ask(str(question), list(options or []))

    def confirm(self, title: str, body: str = "", details: str = "") -> bool:
        if self.api is None:
            return False
        return bool(self.api.confirm(str(title), str(body), str(details)))

    def info(self, text: str) -> None:
        if self.api is None:
            logger.debug("оверлей: клиентов нет — info пропущен: {}", str(text)[:120])
            return
        self.api.info(str(text))
