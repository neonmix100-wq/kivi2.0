"""Пользовательские настройки Киви: data/prefs.json (в git не попадает).

Сейчас здесь только громкость собственного голоса (tts_volume, 0..1).
Файл читается при старте и перечитывается при изменении (по mtime),
запись — атомарная замена файла.
"""

from __future__ import annotations

import json
import threading
from pathlib import Path
from typing import Any

from kivi.config import DATA_DIR
from kivi.log import logger

__all__ = [
    "DEFAULT_PREFS",
    "DEFAULT_TTS_VOLUME",
    "PREFS_PATH",
    "clamp_volume",
    "get_tts_volume",
    "load_prefs",
    "reload_prefs",
    "set_tts_volume",
]

PREFS_PATH = DATA_DIR / "prefs.json"
DEFAULT_TTS_VOLUME = 0.8
DEFAULT_PREFS: dict[str, Any] = {"tts_volume": DEFAULT_TTS_VOLUME}

_LOCK = threading.RLock()
_cache: dict[str, Any] = dict(DEFAULT_PREFS)
_loaded_mtime: float | None = None


def clamp_volume(value: Any) -> float:
    """Привести громкость к диапазону 0..1 (битое значение -> умолчание)."""
    try:
        number = float(value)
    except (TypeError, ValueError):
        return DEFAULT_TTS_VOLUME
    return max(0.0, min(1.0, number))


def _mtime() -> float | None:
    try:
        return PREFS_PATH.stat().st_mtime
    except OSError:
        return None


def _read_file() -> dict[str, Any]:
    try:
        raw = json.loads(Path(PREFS_PATH).read_text(encoding="utf-8"))
    except (OSError, ValueError):
        return {}
    return dict(raw) if isinstance(raw, dict) else {}


def load_prefs(*, force: bool = False) -> dict[str, Any]:
    """Прочитать prefs.json (с кэшем по mtime). Битый файл -> умолчания."""
    global _cache, _loaded_mtime
    with _LOCK:
        mtime = _mtime()
        if force or _loaded_mtime != mtime:
            data = dict(DEFAULT_PREFS)
            data.update(_read_file())
            _cache = data
            _loaded_mtime = mtime
        return dict(_cache)


def get_tts_volume() -> float:
    """Текущая громкость голоса Киви (0..1). Перечитывает файл при изменении."""
    return clamp_volume(load_prefs().get("tts_volume", DEFAULT_TTS_VOLUME))


def set_tts_volume(value: Any) -> float:
    """Сохранить громкость голоса в prefs.json и вернуть её (0..1)."""
    global _cache, _loaded_mtime
    volume = clamp_volume(value)
    with _LOCK:
        data = dict(DEFAULT_PREFS)
        data.update(_read_file())
        data["tts_volume"] = volume
        path = Path(PREFS_PATH)
        path.parent.mkdir(parents=True, exist_ok=True)
        temp = path.with_suffix(".json.tmp")
        temp.write_text(
            json.dumps(data, ensure_ascii=False, indent=2), encoding="utf-8"
        )
        temp.replace(path)
        _cache = data
        _loaded_mtime = _mtime()
    logger.info("настройки: tts_volume = {:.2f}", volume)
    return volume


def reload_prefs() -> float:
    """Принудительно перечитать файл (для тестов и ручной правки)."""
    load_prefs(force=True)
    return get_tts_volume()
