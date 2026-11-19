"""Защита от второго экземпляра и гарантированное завершение.

Замок — TCP-сокет на 127.0.0.1:47613. Порт занят = Киви уже работает.
Сокет живёт всё время работы; ОС освобождает его при любом завершении
(включая падение), поэтому устаревших lock-файлов не бывает.

Завершение: колбэки (WakeLoop, AudioCapture), активный ffplay, join потоков
с таймаутом 2 с, закрытие сокета. Если что-то не отпустило — вызывающий
может сделать os._exit(0) (см. shutdown() -> forced).

Использование:
    from kivi.runtime import single_instance, register_cleanup

    with single_instance():
        ...
"""

from __future__ import annotations

import atexit
import os
import signal
import socket
import threading
from collections.abc import Callable, Iterator
from contextlib import contextmanager

from kivi.log import logger

__all__ = [
    "LOCK_PORT",
    "already_running_message",
    "install_signal_handlers",
    "register_cleanup",
    "shutdown",
    "single_instance",
]

LOCK_PORT = 47613
LOCK_HOST = "127.0.0.1"
THREAD_JOIN_TIMEOUT = 2.0
FORCE_EXIT_DELAY = 3.0

_lock_socket: socket.socket | None = None
_cleanup_callbacks: list[Callable[[], object]] = []
_shutdown_done = False
_shutdown_lock = threading.Lock()
_signal_stop_event: threading.Event | None = None


def already_running_message() -> str:
    return (
        "Киви уже запущена. Останови её: scripts\\kivi_stop.ps1\n"
        "Если уверен, что это не она — проверь: scripts\\kivi_status.ps1"
    )


def register_cleanup(callback: Callable[[], object]) -> None:
    """Добавить действие при завершении (выполняется в обратном порядке)."""
    if callback not in _cleanup_callbacks:
        _cleanup_callbacks.append(callback)


def _try_bind() -> socket.socket | None:
    sock = socket.socket(socket.AF_INET, socket.SOCK_STREAM)
    try:
        sock.bind((LOCK_HOST, LOCK_PORT))
        # backlog побольше: проверки статуса могут подключаться, и очередь
        # из одного слота блокировала бы следующие проверки.
        sock.listen(16)
    except OSError:
        try:
            sock.close()
        except OSError:
            pass
        return None
    return sock


@contextmanager
def single_instance() -> Iterator[socket.socket]:
    """Пропустить только один экземпляр; иначе сообщение и SystemExit(1)."""
    global _lock_socket
    sock = _try_bind()
    if sock is None:
        print(already_running_message(), flush=True)
        logger.warning(
            "единственный экземпляр: порт {} занят другим процессом — выходим",
            LOCK_PORT,
        )
        raise SystemExit(1)
    _lock_socket = sock
    logger.info("единственный экземпляр: OK (pid={})", os.getpid())
    atexit.register(shutdown, "atexit")
    try:
        yield sock
    finally:
        shutdown("выход из single_instance")


def _stop_tts_player() -> None:
    """Остановить активный ffplay через stop-механизм модуля озвучки."""
    try:
        from kivi.tts.speak import _PLAYER_LOCK, _stop_player_locked
    except Exception as exc:  # noqa: BLE001 - модуль может быть не импортирован
        logger.debug("завершение: модуль озвучки недоступен: {}", exc)
        return
    with _PLAYER_LOCK:
        _stop_player_locked()


def shutdown(reason: str = "") -> bool:
    """Остановить всё и закрыть сокет. True — остались живые не-daemon потоки."""
    global _shutdown_done, _lock_socket
    with _shutdown_lock:
        if _shutdown_done:
            return False
        _shutdown_done = True

    logger.info("завершение: {}", reason or "штатное")

    # 1) Сначала просим остановиться (прослушка, потоки-читатели).
    for callback in reversed(_cleanup_callbacks):
        try:
            callback()
        except Exception as exc:  # noqa: BLE001 - один колбэк не должен ломать остальные
            logger.warning("завершение: колбэк {} упал: {}", callback, exc)

    # 2) Активный ffplay — через штатный stop модуля озвучки.
    _stop_tts_player()

    # 3) Джойним потоки с таймаутом.
    stuck: list[threading.Thread] = []
    for thread in list(threading.enumerate()):
        if thread is threading.current_thread() or thread.daemon:
            continue
        thread.join(timeout=THREAD_JOIN_TIMEOUT)
        if thread.is_alive():
            stuck.append(thread)
            logger.warning(
                "завершение: поток {} не остановился за {:.0f} c",
                thread.name,
                THREAD_JOIN_TIMEOUT,
            )

    # 4) Сокет-замок освобождаем последним.
    if _lock_socket is not None:
        try:
            _lock_socket.close()
        except OSError:
            pass
        _lock_socket = None
        logger.info("единственный экземпляр: сокет освобождён")

    return bool(stuck)


def install_signal_handlers(stop_event: threading.Event) -> None:
    """SIGINT/SIGTERM -> мягкая остановка, через 3 c — принудительный выход."""
    global _signal_stop_event
    _signal_stop_event = stop_event

    def _force_exit() -> None:
        logger.warning(
            "остановка не завершилась за {:.0f} c — os._exit(0)", FORCE_EXIT_DELAY
        )
        try:
            logger.complete()
        except Exception:  # noqa: BLE001
            pass
        os._exit(0)

    def _handler(signum, frame):  # noqa: ANN001
        logger.info("получен сигнал {} — останавливаюсь", signum)
        stop_event.set()
        timer = threading.Timer(FORCE_EXIT_DELAY, _force_exit)
        timer.daemon = True
        timer.start()

    for sig in (signal.SIGINT, signal.SIGTERM):
        try:
            signal.signal(sig, _handler)
        except (ValueError, OSError) as exc:
            logger.warning("не удалось повесить обработчик {}: {}", sig, exc)
