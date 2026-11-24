"""Точка входа Kivi: постоянная прослушка с отзывом на имя «Киви».

Запуск:
    python -m kivi.main

Только один экземпляр (замок на 127.0.0.1:47613).
Ctrl+C — корректная остановка: микрофон, ffplay и сокет освобождаются,
python-процесс не остаётся.
"""

from __future__ import annotations

import argparse
import os
import threading

from kivi.brain.router import Router
from kivi.brain.session import Session
from kivi.config import ConfigError, DeviceNotFoundError, get_settings
from kivi.log import logger, setup_logging
from kivi.runtime import (
    install_signal_handlers,
    register_cleanup,
    shutdown,
    single_instance,
)
from kivi.stt.local import warmup as warmup_stt
from kivi.tts.speak import warm_connection as warmup_tts
from kivi.wake.loop import WakeLoop

__all__ = ["create_api_server", "main", "parse_args"]

REPORT_INTERVAL_S = 300.0  # раз в 5 минут пишем состояние и бюджет
REFLECTION_GRACE_S = 8.0  # при выходе ждём финальную рефлексию не дольше 8 с


def parse_args(argv: list[str] | None = None) -> argparse.Namespace:
    """Аргументы запуска: --no-api для headless-режима."""
    parser = argparse.ArgumentParser(description="Kivi — голосовой ассистент")
    parser.add_argument(
        "--no-api",
        action="store_true",
        help="не поднимать WebSocket API (headless-тесты)",
    )
    return parser.parse_args(argv)


def create_api_server(stop_event: threading.Event, *, no_api: bool = False):
    """Поднять WS-сервер (None при --no-api или ошибке)."""
    if no_api:
        logger.info("api: отключён (--no-api)")
        return None
    try:
        from kivi.api.server import ApiServer

        server = ApiServer()
        server.set_shutdown_callback(stop_event.set)
        server.start()
        return server
    except Exception as exc:  # noqa: BLE001 - API не должен мешать голосу
        logger.warning("api: не удалось поднять сервер: {}: {}", type(exc).__name__, exc)
        return None


def _warm_fillers() -> None:
    """Фоновый прогрев библиотеки вводных для всех контекстов (TTS + LLM, один раз)."""
    try:
        from kivi.tts.fillers import CONTEXTS, ensure_library

        for context in CONTEXTS:
            library = ensure_library(context)
            logger.info("вводные: {} готовы ({} фраз)", context, len(library))
    except Exception as exc:  # noqa: BLE001 - вводные не критичны для запуска
        logger.warning("вводные: прогрев не удался: {}: {}", type(exc).__name__, exc)


def _reporter(loop: WakeLoop, router: Router, stop: threading.Event) -> None:
    """Фоновый отчёт о состоянии и бюджете (daemon)."""
    while not stop.wait(REPORT_INTERVAL_S):
        logger.info(
            "состояние: {} | бюджет: {} | окно диалога: {}",
            loop.state,
            router.budget.describe(),
            "да" if loop.window_until else "нет",
        )


def _run(*, enable_api: bool = True) -> int:
    try:
        settings = get_settings()
    except (ConfigError, DeviceNotFoundError) as exc:
        print("FAIL — конфиг:", exc)
        return 1

    try:
        router = Router(settings)
    except Exception as exc:  # noqa: BLE001 - роутер без ключей тоже должен дать понятный текст
        print(f"FAIL — роутер: {type(exc).__name__}: {exc}")
        return 1

    stop = threading.Event()
    install_signal_handlers(stop)

    if settings.section("memory").get("enabled", True):
        try:
            from kivi.memory.vault import ensure_vault

            ensure_vault()
        except Exception as exc:  # noqa: BLE001 - память не должна блокировать запуск
            logger.warning("Память: не удалось подготовить vault: {}", exc)

    loop = WakeLoop(settings=settings, router=router, session=Session())
    try:
        from kivi.skills.reminders import ensure_scheduler, stop_scheduler

        ensure_scheduler()
        register_cleanup(stop_scheduler)
    except Exception as exc:  # noqa: BLE001 - напоминания не должны мешать голосу
        logger.warning("напоминания: scheduler не запущен: {}: {}", type(exc).__name__, exc)
    register_cleanup(loop.stop)  # WakeLoop + AudioCapture
    register_cleanup(stop.set)  # сначала просим цикл выйти

    api_server = create_api_server(stop, no_api=not enable_api)
    if api_server is not None:
        loop.api = api_server
        api_server.attach(loop)
        logger.info("api: сервер привязан к ядру (порт {})", api_server.port)
    try:
        from kivi.skills.broker import set_overlay_service
        from kivi.ui.overlay import OverlayService

        set_overlay_service(OverlayService(api_server) if api_server is not None else None)
    except Exception as exc:  # noqa: BLE001 - confirm просто будет недоступен
        logger.warning("overlay: сервис не подключён: {}", exc)

    devices = settings.devices
    print(f"Вход:  [{devices.input_index}] {devices.input_name}")
    print(f"Выход: [{devices.output_index}] {devices.output_name}")
    print(f"Бюджет: {router.budget.describe()}")
    print()
    print("Прогреваю распознавание (Whisper на CUDA)...", flush=True)
    warmup_stt()
    warmup_tts()  # keep-alive до TTS: первый синтез без TLS handshake
    threading.Thread(
        target=_warm_fillers, name="kivi-fillers-warmup", daemon=True
    ).start()
    print(flush=True)

    reporter = threading.Thread(
        target=_reporter, args=(loop, router, stop), name="kivi-reporter", daemon=True
    )
    reporter.start()

    forced = False
    try:
        loop.run(stop_event=stop)
    except KeyboardInterrupt:
        logger.info("остановка по Ctrl+C")
    finally:
        try:
            loop.wait_reflection(REFLECTION_GRACE_S)
        except Exception as exc:  # noqa: BLE001 - выход не должен падать
            logger.warning("reflection: ожидание упало: {}: {}", type(exc).__name__, exc)
        if api_server is not None:
            try:
                api_server.stop()
            except Exception as exc:  # noqa: BLE001 - API не должен мешать выходу
                logger.warning("api: остановка упала: {}: {}", type(exc).__name__, exc)
        forced = shutdown("выход из main")
        logger.info("Kivi остановлена. Бюджет: {}", router.budget.describe())
        try:
            logger.complete()  # дописать очередь loguru до конца
        except Exception:  # noqa: BLE001
            pass
        if forced:
            logger.warning("остались живые потоки — принудительный os._exit(0)")
            os._exit(0)
    return 0


def main(argv: list[str] | None = None) -> int:
    setup_logging()
    args = parse_args(argv)
    print("=== Kivi: постоянная прослушка (python -m kivi.main) ===")
    print("Скажи: «Киви» или «Киви, <команда>». Ctrl+C — выход.")
    try:
        with single_instance():
            return _run(enable_api=not args.no_api)
    except SystemExit:
        raise


if __name__ == "__main__":
    raise SystemExit(main())
