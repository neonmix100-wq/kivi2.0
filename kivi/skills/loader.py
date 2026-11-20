"""Загрузка и вызов скиллов с изоляцией ошибок.

Скилл — папка с handler.py и функцией handle(ctx, **args) -> str.
Загружаем через importlib.util.spec_from_file_location с уникальным именем
модуля, кэшируем по mtime, вызываем в отдельном потоке с таймаутом.

Использование:
    from kivi.skills.loader import invoke
    text = invoke(manifest, {"pattern": "*.yaml"}, timeout=30)

Ошибка/таймаут скилла НЕ роняет процесс: пишем статистику (record_use ok=False)
и возвращаем понятный пользователю текст.
"""

from __future__ import annotations

import importlib.util
import sys
import threading
import time
from collections.abc import Callable
from typing import Any

from loguru import logger

from kivi.skills.broker import PermissionDenied, SkillContext
from kivi.skills.registry import SkillManifest, SkillRegistry, get_registry

__all__ = ["SkillLoadError", "invoke", "load"]

_HANDLERS: dict[tuple[str, str, float], Callable[..., Any]] = {}


class SkillLoadError(RuntimeError):
    """Точка входа скилла не загрузилась."""


def load(skill: SkillManifest) -> Callable[..., Any]:
    """Импортировать handler.py скилла и вернуть функцию из поля entry."""
    if skill.path is None:
        raise SkillLoadError(f"скилл {skill.name}: не известна папка (нужен registry.scan)")
    entry_file, _, entry_func = skill.entry.partition(":")
    if not entry_file or not entry_func:
        raise SkillLoadError(
            f"скилл {skill.name}: entry={skill.entry!r}, ожидалось 'handler.py:handle'"
        )

    folder = skill.path.resolve()
    handler_path = (folder / entry_file).resolve()
    if not handler_path.is_file():
        raise SkillLoadError(f"скилл {skill.name}: нет файла {handler_path}")
    if not (handler_path == folder or handler_path.is_relative_to(folder)):
        raise SkillLoadError(f"скилл {skill.name}: точка входа вне папки скилла")

    mtime = handler_path.stat().st_mtime
    key = (skill.name, str(handler_path), mtime)
    cached = _HANDLERS.get(key)
    if cached is not None:
        return cached

    module_name = f"kivi_skill_{skill.name}_{int(mtime)}"
    spec = importlib.util.spec_from_file_location(module_name, handler_path)
    if spec is None or spec.loader is None:
        raise SkillLoadError(f"скилл {skill.name}: не удалось создать модуль для {handler_path}")
    module = importlib.util.module_from_spec(spec)
    sys.modules[module_name] = module
    try:
        spec.loader.exec_module(module)
    except Exception:
        sys.modules.pop(module_name, None)
        raise
    func = getattr(module, entry_func, None)
    if not callable(func):
        raise SkillLoadError(
            f"скилл {skill.name}: в {entry_file} нет функции {entry_func}()"
        )
    _HANDLERS[key] = func
    return func


def _error_text(name: str, error: BaseException) -> str:
    """Текст ошибки для пользователя (коротко и по-русски)."""
    if isinstance(error, PermissionDenied):
        return f"Скилл «{name}» не смог: {error}"
    if isinstance(error, FileNotFoundError):
        return f"Скилл «{name}»: {error}"
    return f"Скилл «{name}» упал: {type(error).__name__}: {error}"


def invoke(
    skill: SkillManifest,
    args: dict[str, Any] | None = None,
    timeout: float | None = None,
    *,
    registry: SkillRegistry | None = None,
) -> str:
    """Вызвать скилл: SkillContext + handle(ctx, **args) в потоке с таймаутом."""
    active_registry = registry if registry is not None else get_registry()
    limit = float(timeout if timeout is not None else active_registry.call_timeout_s)
    call_args = dict(args or {})
    started = time.perf_counter()

    stats = active_registry.stats(skill.name)
    if stats is not None and stats.fail_count >= active_registry.auto_disable_after_fails:
        logger.warning(
            "скилл: {} -> отключён после {} ошибок подряд",
            skill.name,
            stats.fail_count,
        )
        return f"Скилл «{skill.name}» отключён после {stats.fail_count} ошибок подряд."

    context = SkillContext(skill)
    box: dict[str, Any] = {}

    def target() -> None:
        try:
            box["result"] = load(skill)(context, **call_args)
        except BaseException as exc:  # noqa: BLE001 - ошибку скилла не отдаём наружу
            box["error"] = exc

    thread = threading.Thread(target=target, name=f"skill-{skill.name}", daemon=True)
    thread.start()
    thread.join(limit)
    elapsed_ms = (time.perf_counter() - started) * 1000.0

    if thread.is_alive():
        active_registry.record_use(skill.name, ok=False)
        logger.warning(
            "скилл: {} -> fail ({:.0f} мс): таймаут {:.0f} с",
            skill.name,
            elapsed_ms,
            limit,
        )
        return f"Скилл «{skill.name}» не уложился в {limit:.0f} секунд."

    error = box.get("error")
    if error is not None:
        active_registry.record_use(skill.name, ok=False)
        message = _error_text(skill.name, error)
        logger.warning("скилл: {} -> fail ({:.0f} мс): {}", skill.name, elapsed_ms, message)
        return message

    active_registry.record_use(skill.name, ok=True)
    logger.info("скилл: {} -> ok ({:.0f} мс)", skill.name, elapsed_ms)
    result = box.get("result")
    return "" if result is None else str(result)
