"""Логи Kivi: DEBUG в файл data/logs/kivi.log (ротация 10 МБ), INFO в консоль.

Использование:
    from kivi.log import logger
    logger.info("текст")

Настройка выполняется один раз при импорте модуля, повторные вызовы
setup_logging() ничего не дублируют.
"""

from __future__ import annotations

import os
import sys
from pathlib import Path

from loguru import logger

__all__ = [
    "LOG_DIR",
    "LOG_FILE",
    "PROJECT_ROOT",
    "handler_count",
    "is_configured",
    "logger",
    "setup_logging",
]

PROJECT_ROOT = Path(__file__).resolve().parents[1]
LOG_DIR = PROJECT_ROOT / "data" / "logs"
LOG_FILE = LOG_DIR / "kivi.log"

CONSOLE_FORMAT = (
    "<green>{time:HH:mm:ss.SSS}</green> | <level>{level: <7}</level> | <level>{message}</level>"
)
FILE_FORMAT = (
    "{time:YYYY-MM-DD HH:mm:ss.SSS} | {level: <7} | {name}:{function}:{line} | {message}"
)

_CONFIGURED = False


def is_configured() -> bool:
    """Настроен ли логгер (для проверок идемпотентности)."""
    return _CONFIGURED


def handler_count() -> int:
    """Сколько хендлеров сейчас у loguru (должно быть 2: консоль и файл)."""
    try:
        from loguru import logger as _loguru

        return len(_loguru._core.handlers)  # noqa: SLF001 - другого API нет
    except Exception:  # noqa: BLE001
        return -1


def setup_logging(console_level: str = "INFO", file_level: str = "DEBUG") -> None:
    """DEBUG -> data/logs/kivi.log (ротация 10 МБ), INFO -> консоль.

    Идемпотентна: повторный вызов (в т.ч. после повторного импорта) не добавляет
    второй хендлер и не дублирует строки в файле.

    Цвета в консоли выключены по умолчанию, чтобы в обычном окне PowerShell
    не появлялись служебные последовательности. Включить: KIVI_LOG_COLOR=1.
    """
    global _CONFIGURED
    if _CONFIGURED:
        return

    LOG_DIR.mkdir(parents=True, exist_ok=True)

    # Консоль Windows может не знать отдельный символ — заменяем, а не падаем.
    try:
        sys.stderr.reconfigure(errors="replace")
    except Exception:  # noqa: BLE001 - stderr может быть подменён (IDE, pytest)
        pass

    colorize = os.environ.get("KIVI_LOG_COLOR", "").strip() in {"1", "true", "yes"}

    # Убираем все прошлые хендлеры (стандартный loguru + возможные дубли).
    logger.remove()
    logger.add(
        sys.stderr,
        level=console_level,
        format=CONSOLE_FORMAT,
        colorize=colorize,
        backtrace=False,
        diagnose=False,
    )
    logger.add(
        LOG_FILE,
        level=file_level,
        format=FILE_FORMAT,
        rotation="10 MB",
        retention=10,
        encoding="utf-8",
        enqueue=True,
        backtrace=True,
        diagnose=False,
    )
    _CONFIGURED = True


# Настраиваем при импорте: `from kivi.log import logger` всегда готов к работе.
setup_logging()
