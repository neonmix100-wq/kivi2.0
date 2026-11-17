"""Единый конфиг Kivi: config.yaml -> pydantic, плюс разрешение имён устройств в индексы.

Использование:
    from kivi.config import get_settings
    settings = get_settings()          # падает с понятной ошибкой, если устройств нет
    settings.audio.block_frames        # 1536
    settings.devices.input_index       # индекс микрофона в PortAudio

Индексы устройств ищутся по ИМЕНИ + hostapi (индексы между запусками съезжают).
"""

from __future__ import annotations

from pathlib import Path
from typing import Any

import sounddevice as sd
import yaml
from pydantic import BaseModel, ConfigDict, Field, ValidationError, model_validator

from kivi.log import logger

__all__ = [
    "CONFIG_PATH",
    "DATA_DIR",
    "LOG_DIR_PATH",
    "MODELS_DIR",
    "PROJECT_ROOT",
    "AudioConfig",
    "AudioDevices",
    "Config",
    "ConfigError",
    "DeviceNotFoundError",
    "Settings",
    "find_device",
    "get_settings",
    "load_config",
    "resolve_audio_devices",
]

PROJECT_ROOT = Path(__file__).resolve().parents[1]
CONFIG_PATH = PROJECT_ROOT / "config.yaml"
DATA_DIR = PROJECT_ROOT / "data"
MODELS_DIR = DATA_DIR / "models"
LOG_DIR_PATH = DATA_DIR / "logs"


class ConfigError(RuntimeError):
    """config.yaml не читается или не проходит проверку."""


class DeviceNotFoundError(RuntimeError):
    """Аудиоустройство из config.yaml не найдено в системе."""


class AudioConfig(BaseModel):
    """Секция audio из config.yaml. Лишние ключи не ломают загрузку."""

    model_config = ConfigDict(extra="allow")

    hostapi: str = "Windows WASAPI"
    input_device_name: str
    input_fallback_name: str = ""
    output_device_name: str
    capture_samplerate: int = Field(default=48000, gt=0)
    target_samplerate: int = Field(default=16000, gt=0)
    playback_samplerate: int = Field(default=48000, gt=0)
    resampler: str = "soxr"
    channels: int = Field(default=1, ge=1)
    block_ms: int = Field(default=32, gt=0)
    aec: bool = False
    vad_end_silence_ms: int = Field(default=400, gt=0)
    barge_in_ms: int = Field(default=250, ge=0)
    pre_roll_ms: int = Field(default=300, gt=0)
    mute_hotkey: str = "Ctrl+Alt+M"
    push_to_talk: str = "Ctrl+Space"

    @model_validator(mode="after")
    def _check_supported(self) -> "AudioConfig":
        if self.capture_samplerate % self.target_samplerate != 0:
            raise ValueError(
                f"capture_samplerate={self.capture_samplerate} должно делиться на "
                f"target_samplerate={self.target_samplerate} (нужен целый коэффициент)"
            )
        if self.channels != 1:
            raise ValueError("на этом шаге поддерживается только моно (audio.channels: 1)")
        if self.resampler.strip().lower() != "soxr":
            raise ValueError(f"audio.resampler={self.resampler!r}: поддерживается только 'soxr'")
        return self

    @property
    def block_frames(self) -> int:
        """Длина блока захвата в сэмплах: 48000 Гц * 32 мс = 1536."""
        return int(round(self.capture_samplerate * self.block_ms / 1000))

    @property
    def resample_ratio(self) -> int:
        """Целочисленный коэффициент ресемплинга: 48000 -> 16000 = 3."""
        return self.capture_samplerate // self.target_samplerate


class Config(BaseModel):
    """Корень config.yaml. Незнакомые секции сохраняются как есть."""

    model_config = ConfigDict(extra="allow")

    audio: AudioConfig


class AudioDevices(BaseModel):
    """Результат разрешения имён устройств в индексы PortAudio."""

    hostapi: str
    input_index: int
    input_name: str
    input_used_fallback: bool = False
    output_index: int
    output_name: str


# --- УСТРОЙСТВА: ПОИСК ПО ИМЕНИ ---


def _norm(text: str) -> str:
    """Нормализация имени для сравнения: регистр, ё, лишние пробелы."""
    return " ".join(text.replace("ё", "е").replace("Ё", "Е").lower().split())


def _hostapi_index(hostapi_name: str) -> int | None:
    """Индекс хост-API по имени (например, 'Windows WASAPI')."""
    wanted = _norm(hostapi_name)
    for index, api in enumerate(sd.query_hostapis()):
        if _norm(str(api["name"])) == wanted:
            return index
    return None


def _match_score(device_name: str, wanted: str) -> int | None:
    """0 — точное совпадение, 1 — имя начинается так же, 2 — вхождение. None — не подходит."""
    name = _norm(device_name)
    if name == wanted:
        return 0
    if name.startswith(wanted) or wanted.startswith(name):
        return 1
    if wanted in name or name in wanted:
        return 2
    return None


def find_device(
    name: str, *, want_input: bool, hostapi: str | None = None
) -> tuple[int, str] | None:
    """Найти устройство по имени. Возвращает (индекс, реальное имя) или None.

    Сначала ищем внутри нужного hostapi, потом — среди остальных API.
    """
    api_index = _hostapi_index(hostapi) if hostapi else None
    wanted = _norm(name)
    best_key: tuple[int, int, int] | None = None
    best: tuple[int, str] | None = None

    for index, device in enumerate(sd.query_devices()):
        channels = (
            device["max_input_channels"] if want_input else device["max_output_channels"]
        )
        if channels <= 0:
            continue
        score = _match_score(str(device["name"]), wanted)
        if score is None:
            continue
        api_rank = 0 if (api_index is not None and device["hostapi"] == api_index) else 1
        key = (api_rank, score, index)
        if best_key is None or key < best_key:
            best_key = key
            best = (index, str(device["name"]))
    return best


def describe_devices(want_input: bool | None = None) -> str:
    """Список устройств для текста ошибки."""
    apis = sd.query_hostapis()
    lines: list[str] = []
    for index, device in enumerate(sd.query_devices()):
        if want_input is True and device["max_input_channels"] <= 0:
            continue
        if want_input is False and device["max_output_channels"] <= 0:
            continue
        kind = ("IN " if device["max_input_channels"] else "   ") + (
            "OUT" if device["max_output_channels"] else ""
        )
        api = apis[device["hostapi"]]["name"]
        lines.append(f"  [{index:2}] {kind:<6} {api:<22} {device['name']}")
    return "\n".join(lines) if lines else "  (устройств нет)"

# --- РАЗРЕШЕНИЕ И ЗАГРУЗКА ---


def resolve_audio_devices(audio: AudioConfig) -> AudioDevices:
    """Имена из конфига -> индексы PortAudio. Не найдено — DeviceNotFoundError."""
    hostapi: str | None = audio.hostapi
    if _hostapi_index(hostapi) is None:
        available = ", ".join(str(api["name"]) for api in sd.query_hostapis())
        logger.warning(
            "Хост-API {!r} не найден, ищу устройство среди всех API. Доступно: {}",
            hostapi,
            available,
        )
        hostapi = None

    candidates = [audio.input_device_name]
    fallback = (audio.input_fallback_name or "").strip()
    if fallback and _norm(fallback) != _norm(audio.input_device_name):
        candidates.append(fallback)

    found: tuple[int, str] | None = None
    used_fallback = False
    for position, candidate in enumerate(candidates):
        found = find_device(candidate, want_input=True, hostapi=hostapi)
        if found is not None:
            used_fallback = position > 0
            break

    if found is None:
        raise DeviceNotFoundError(
            "Не найден микрофон {!r} (hostapi {!r}).\n"
            "Проверь, что он подключён и не занят другой программой, "
            "и поправь audio.input_device_name в config.yaml.\n"
            "Доступные устройства ввода:\n{}".format(
                audio.input_device_name, audio.hostapi, describe_devices(True)
            )
        )
    if used_fallback:
        logger.warning(
            "Микрофон {!r} недоступен — использую запасной {!r}",
            audio.input_device_name,
            found[1],
        )

    output = find_device(audio.output_device_name, want_input=False, hostapi=hostapi)
    if output is None:
        raise DeviceNotFoundError(
            "Не найдены наушники {!r} (hostapi {!r}).\n"
            "Проверь, что они подключены, и поправь audio.output_device_name в config.yaml.\n"
            "Доступные устройства вывода:\n{}".format(
                audio.output_device_name, audio.hostapi, describe_devices(False)
            )
        )

    return AudioDevices(
        hostapi=audio.hostapi,
        input_index=found[0],
        input_name=found[1],
        input_used_fallback=used_fallback,
        output_index=output[0],
        output_name=output[1],
    )


class Settings:
    """Конфиг + уже разрешённые индексы устройств."""

    def __init__(self, config: Config, devices: AudioDevices, path: Path = CONFIG_PATH) -> None:
        self.config = config
        self.devices = devices
        self.path = path

    @property
    def audio(self) -> AudioConfig:
        return self.config.audio

    @property
    def raw(self) -> dict[str, Any]:
        return self.config.model_dump()

    def section(self, name: str) -> dict[str, Any]:
        """Сырая секция config.yaml как словарь (stt, brain, tts и т.д.)."""
        value = self.raw.get(name)
        return dict(value) if isinstance(value, dict) else {}

    def __repr__(self) -> str:
        devices = self.devices
        return (
            f"Settings(in=[{devices.input_index}] {devices.input_name!r}, "
            f"out=[{devices.output_index}] {devices.output_name!r})"
        )


def load_config(path: Path | None = None) -> Config:
    """Прочитать и провалидировать config.yaml."""
    config_path = Path(path) if path is not None else CONFIG_PATH
    if not config_path.exists():
        raise ConfigError(f"Не найден файл конфига: {config_path}")
    try:
        raw = yaml.safe_load(config_path.read_text(encoding="utf-8")) or {}
    except yaml.YAMLError as exc:
        raise ConfigError(f"{config_path.name}: сломанный YAML — {exc}") from exc
    if not isinstance(raw, dict):
        raise ConfigError(f"{config_path.name}: ожидался словарь на верхнем уровне")
    try:
        return Config.model_validate(raw)
    except ValidationError as exc:
        raise ConfigError(f"{config_path.name} не прошёл проверку:\n{exc}") from exc


_settings: Settings | None = None


def get_settings(*, refresh: bool = False) -> Settings:
    """Единый объект настроек. Устройства разрешаются при первом вызове."""
    global _settings
    if _settings is None or refresh:
        config = load_config()
        devices = resolve_audio_devices(config.audio)
        logger.info(
            "Конфиг загружен: вход [{idx_in}] {name_in}; выход [{idx_out}] {name_out}; "
            "{sr} Гц, блок {block} мс",
            idx_in=devices.input_index,
            name_in=devices.input_name,
            idx_out=devices.output_index,
            name_out=devices.output_name,
            sr=config.audio.capture_samplerate,
            block=config.audio.block_ms,
        )
        _settings = Settings(config, devices)
    return _settings


