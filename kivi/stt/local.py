"""Локальное распознавание речи: faster-whisper (large-v3-turbo) на CUDA.

Модель качается в data/models/ при первом запуске.
Если CUDA недоступна (нет cuBLAS/cuDNN DLL) — модель уезжает на CPU,
и об этом ОБЯЗАТЕЛЬНО пишется WARNING: на CPU large-v3-turbo заметно медленнее.

Использование:
    from kivi.stt.local import transcribe_wav
    text = transcribe_wav("data/last_utterance.wav")
"""

from __future__ import annotations

import os as _os, sys as _sys, sysconfig as _sysconfig
from pathlib import Path as _Path


def _bootstrap_cuda_dlls() -> None:
    """Подключить DLL из колёс nvidia-* ДО загрузки ctranslate2/faster-whisper.

    Путь берём через sysconfig (purelib = .venv/Lib/site-packages), а не от
    sys.executable (там .venv/Scripts). Меняем только PATH этого процесса.
    """
    if _sys.platform != "win32":
        return
    sp = _Path(_sysconfig.get_paths()["purelib"]) / "nvidia"
    if not sp.exists():
        return
    for pat in ("cublas64_12.dll", "cudnn_ops64_9.dll", "cudnn64_9.dll",
                "cudart64_12.dll", "nvrtc64*.dll"):
        for hit in sp.rglob(pat):
            d = str(hit.parent)
            _os.add_dll_directory(d)
            _os.environ["PATH"] = d + _os.pathsep + _os.environ.get("PATH", "")


_bootstrap_cuda_dlls()

import time
import traceback
from pathlib import Path

import numpy as np
import soundfile as sf
import soxr

from kivi.config import MODELS_DIR, get_settings
from kivi.log import logger

__all__ = [
    "CPU_COMPUTE_TYPE",
    "DEFAULT_COMPUTE_TYPE",
    "DEFAULT_MODEL",
    "HALLUCINATION_MARKERS",
    "LocalTranscribeError",
    "MIN_RMS",
    "get_model_info",
    "is_hallucination",
    "transcribe_wav",
    "warmup",
]

DEFAULT_MODEL = "large-v3-turbo"
DEFAULT_COMPUTE_TYPE = "int8_float16"
CPU_COMPUTE_TYPE = "int8"  # float16 на CPU не поддерживается
DEFAULT_LANGUAGE = "ru"
TARGET_SR = 16000
MIN_RMS = 0.002  # тише — считаем тишиной и в Whisper не идём

# Фразы-галлюцинации Whisper на тишине/музыке/ТВ.
HALLUCINATION_MARKERS: tuple[str, ...] = (
    "субтитры",
    "dimatorzok",
    "продолжение следует",
    "спасибо за просмотр",
    "подписывайтесь",
    "ставьте лайки",
    "редактор субтитров",
    "корректор",
)


def is_hallucination(text: str) -> bool:
    """Текст — известная галлюцинация Whisper? (регистронезависимо, по подстроке)"""
    lowered = str(text or "").lower()
    return any(marker in lowered for marker in HALLUCINATION_MARKERS)


# Загруженная модель кэшируется: повторный вызов не читает 1.5 ГБ с диска.
_model = None
_actual_device = "cpu"
_actual_compute_type = CPU_COMPUTE_TYPE
_actual_model_name = DEFAULT_MODEL


class LocalTranscribeError(RuntimeError):
    """Локальное распознавание не удалось."""


def _cuda_device_count() -> int:
    try:
        import ctranslate2

        return int(ctranslate2.get_cuda_device_count())
    except Exception as exc:  # noqa: BLE001 - библиотека может быть не установлена
        logger.warning("STT local: не удалось проверить CUDA ({})", exc)
        return 0


def _build_model(model_name: str, device: str, compute_type: str):
    """Создать WhisperModel; при проблемах с CUDA — откат на CPU с WARNING."""
    global _actual_device, _actual_compute_type

    from faster_whisper import WhisperModel

    MODELS_DIR.mkdir(parents=True, exist_ok=True)
    if device == "cuda":
        if _cuda_device_count() <= 0:
            logger.warning(
                "STT local: CUDA не видна (нет cuBLAS/cuDNN или драйвера) — "
                "работаю на CPU, будет медленно"
            )
            device, compute_type = "cpu", CPU_COMPUTE_TYPE
        else:
            try:
                model = WhisperModel(
                    model_name,
                    device="cuda",
                    compute_type=compute_type,
                    download_root=str(MODELS_DIR),
                )
                _actual_device, _actual_compute_type = "cuda", compute_type
                return model
            except Exception as exc:  # noqa: BLE001 - нет DLL cuDNN/cuBLAS и т.п.
                logger.warning(
                    "STT local: CUDA есть, но модель на GPU не поднялась ({}: {}). "
                    "Похоже, не хватает cuBLAS/cuDNN DLL — откат на CPU, будет медленно",
                    type(exc).__name__,
                    exc,
                )
                device, compute_type = "cpu", CPU_COMPUTE_TYPE

    model = WhisperModel(
        model_name,
        device=device,
        compute_type=compute_type,
        download_root=str(MODELS_DIR),
    )
    _actual_device, _actual_compute_type = device, compute_type
    return model


def _get_model(
    model_name: str | None = None,
    *,
    device: str | None = None,
    compute_type: str | None = None,
):
    """Ленивая загрузка модели с кэшем."""
    global _model, _actual_model_name

    settings = get_settings()
    stt_cfg = settings.section("stt")
    name = str(model_name or stt_cfg.get("model") or DEFAULT_MODEL)
    wanted_device = str(device or "cuda").lower()
    wanted_compute = str(compute_type or stt_cfg.get("compute_type") or DEFAULT_COMPUTE_TYPE)

    if _model is not None and _actual_model_name == name:
        return _model

    logger.info(
        "STT local: загружаю {} (device={}, compute_type={}, папка {})",
        name,
        wanted_device,
        wanted_compute,
        MODELS_DIR,
    )
    started = time.perf_counter()
    _model = _build_model(name, wanted_device, wanted_compute)
    _actual_model_name = name
    logger.info(
        "STT local: модель готова за {:.1f} c, фактическое устройство: {} ({})",
        time.perf_counter() - started,
        _actual_device,
        _actual_compute_type,
    )
    return _model


def get_model_info() -> dict:
    """Что фактически используется: модель, устройство, тип вычислений."""
    return {
        "model": _actual_model_name,
        "device": _actual_device,
        "compute_type": _actual_compute_type,
        "loaded": _model is not None,
        "models_dir": str(MODELS_DIR),
    }


def _reset_model() -> None:
    """Выгрузить модель (нужно после падения CUDA)."""
    global _model, _actual_device, _actual_compute_type
    _model = None
    _actual_device = "cpu"
    _actual_compute_type = CPU_COMPUTE_TYPE


def warmup() -> None:
    """Загрузить модель и прогнать тишину, чтобы инициализировать CUDA заранее."""
    started = time.perf_counter()
    try:
        whisper = _get_model()
        silent = np.zeros(TARGET_SR // 2, dtype=np.float32)
        segments, _info = whisper.transcribe(
            silent,
            language=DEFAULT_LANGUAGE,
            beam_size=1,
            vad_filter=False,
            condition_on_previous_text=False,
        )
        list(segments)  # генератор ленивый — вычитываем, чтобы прогрев прошёл
    except Exception as exc:  # noqa: BLE001 - прогрев не должен ломать запуск
        logger.warning("STT local: прогрев не удался: {}: {}", type(exc).__name__, exc)
        return
    logger.info(
        "STT local: прогрев готов за {:.2f} c, устройство {} ({})",
        time.perf_counter() - started,
        _actual_device,
        _actual_compute_type,
    )


def _load_audio_16k(path: Path) -> np.ndarray:
    """Прочитать файл в float32 моно 16 кГц.

    Декодируем сами (soundfile + soxr), чтобы не зависеть от PyAV:
    в av>=19 убран параметр metadata_errors, на котором спотыкается
    faster-whisper при чтении файла по пути.
    """
    data, samplerate = sf.read(str(path), dtype="float32", always_2d=False)
    if data.ndim > 1:
        data = data.mean(axis=1)
    if samplerate != TARGET_SR:
        data = soxr.resample(data, samplerate, TARGET_SR, quality="HQ")
    return np.ascontiguousarray(data, dtype=np.float32)


def transcribe_wav(
    path: str | Path,
    *,
    model: str | None = None,
    language: str | None = None,
    beam_size: int | None = None,
    vad_filter: bool = False,
    device: str | None = None,
    compute_type: str | None = None,
) -> str:
    """Распознать wav локально. Сигнатура совпадает с kivi/stt/transcribe.py."""
    wav_path = Path(path)
    if not wav_path.exists():
        raise LocalTranscribeError(f"Нет файла для распознавания: {wav_path}")

    settings = get_settings()
    stt_cfg = settings.section("stt")
    lang = str(language or stt_cfg.get("language") or DEFAULT_LANGUAGE)
    beams = int(beam_size if beam_size is not None else (stt_cfg.get("beam_size") or 1))

    whisper = _get_model(model, device=device, compute_type=compute_type)
    audio = _load_audio_16k(wav_path)

    rms = float(np.sqrt(np.mean(np.square(audio)))) if audio.size else 0.0
    if rms < MIN_RMS:
        logger.info("STT local: пропуск: тихо (RMS {:.5f} < {:.3f})", rms, MIN_RMS)
        return ""

    started = time.perf_counter()
    try:
        segments, info = whisper.transcribe(
            audio,
            language=lang,
            beam_size=beams,
            vad_filter=vad_filter,  # у нас свой Silero VAD
            # Антигаллюцинации: не тащить контекст из предыдущего окна,
            # отбрасывать окна с высокой вероятностью «нет речи» и мусором.
            condition_on_previous_text=False,
            no_speech_threshold=0.6,
            log_prob_threshold=-1.0,
            compression_ratio_threshold=2.4,
        )
        text = " ".join(segment.text.strip() for segment in segments).strip()
    except Exception as exc:  # noqa: BLE001 - возможен отказ CUDA на инференсе
        if _actual_device == "cuda":
            logger.warning(
                "STT local: инференс на CUDA упал ({}: {}), откатываюсь на CPU",
                type(exc).__name__,
                exc,
            )
            logger.debug("STT local: полный traceback:\n{}", traceback.format_exc())
            _reset_model()
            return transcribe_wav(
                wav_path,
                model=model,
                language=lang,
                beam_size=beams,
                vad_filter=vad_filter,
                device="cpu",
                compute_type=CPU_COMPUTE_TYPE,
            )
        raise LocalTranscribeError(f"faster-whisper не смог распознать: {exc}") from exc

    elapsed = time.perf_counter() - started
    if is_hallucination(text):
        logger.warning("STT local: отброшена галлюцинация: {!r}", text[:120])
        return ""
    if not text:
        logger.info("STT local: пусто после фильтров (RMS {:.4f})", rms)
        return ""

    audio_seconds = float(getattr(info, "duration", 0.0) or 0.0)
    logger.info(
        "STT local: {:.2f} c аудио распознано за {:.2f} c на {} ({:.1f}x)",
        audio_seconds,
        elapsed,
        _actual_device,
        (audio_seconds / elapsed) if elapsed else 0.0,
    )
    logger.info("STT local: {}", text)
    return text
