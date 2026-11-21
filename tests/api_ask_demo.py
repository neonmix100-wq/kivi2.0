"""Демо-вопрос из оверлея: спрашиваем через OverlayService и печатаем ответ.

Запуск (ядро уже работает, приложение Electron открыто):
    python tests\\api_ask_demo.py

В оверлее появится карточка ask; ответ кликом вернётся в консоль.
"""

from __future__ import annotations

import sys
import threading
from pathlib import Path

PROJECT_ROOT = Path(__file__).resolve().parents[1]
if str(PROJECT_ROOT) not in sys.path:
    sys.path.insert(0, str(PROJECT_ROOT))

from kivi.api.client import ApiClient
from kivi.log import setup_logging
from kivi.ui.overlay import OverlayService

QUESTION = "Какой формат анекдотов любишь?"
OPTIONS = ["про животных", "про программистов"]


def main() -> int:
    setup_logging()
    print(f"Вопрос: {QUESTION} {OPTIONS}")

    try:
        client = ApiClient()
    except Exception as exc:  # noqa: BLE001 - ядро может быть не запущено
        print(f"FAIL — нет связи с ядром (ws://127.0.0.1:8765): {exc}")
        return 1

    overlay = OverlayService(client)
    result: dict[str, str | None] = {}

    def ask_job() -> None:
        result["answer"] = overlay.ask(QUESTION, OPTIONS)

    thread = threading.Thread(target=ask_job, name="ask-demo", daemon=True)
    thread.start()
    thread.join(timeout=90)
    client.close()

    if thread.is_alive():
        print("FAIL — ответа нет за 90 с (оверлей не открыт?)")
        return 1
    answer = result.get("answer")
    print(f"Ответ из оверлея: {answer!r}")
    return 0 if answer is not None else 1


if __name__ == "__main__":
    raise SystemExit(main())
