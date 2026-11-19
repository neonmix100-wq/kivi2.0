"""Смоук шага 0.6b: локальный STT (faster-whisper) и сравнение с облаком.

Запуск:
    python tests/smoke_stt_local.py
"""

from __future__ import annotations

import sys
import time
from pathlib import Path

PROJECT_ROOT = Path(__file__).resolve().parents[1]
if str(PROJECT_ROOT) not in sys.path:
    sys.path.insert(0, str(PROJECT_ROOT))

import numpy as np
import soundfile as sf

from kivi.config import DATA_DIR, get_settings
from kivi.log import setup_logging
from kivi.stt import local as local_stt
from kivi.stt.transcribe import transcribe_wav as transcribe_cloud

WAV_PATH = DATA_DIR / "last_utterance.wav"
MAX_SECONDS_CUDA = 1.0


def main() -> int:
    setup_logging()
    print("=== SMOKE: локальный STT (faster-whisper) ===")

    if not WAV_PATH.exists():
        print(f"FAIL — нет файла {WAV_PATH}. Сначала запиши фразу: python tests\\smoke_audio.py")
        return 1

    data, samplerate = sf.read(str(WAV_PATH), dtype="float32")
    duration = data.size / samplerate if samplerate else 0.0
    peak = float(np.max(np.abs(data))) if data.size else 0.0
    print(f"Файл: {WAV_PATH}")
    print(f"Длительность: {duration:.2f} c | {samplerate} Гц | пик {peak:.4f}")

    settings = get_settings()
    print(f"Конфиг stt: {settings.section('stt')}")
    print()

    # --- локально ---
    print("--- Локальный движок ---")
    started = time.perf_counter()
    try:
        local_text = local_stt.transcribe_wav(WAV_PATH)
        cold_seconds = time.perf_counter() - started
    except Exception as exc:  # noqa: BLE001
        print(f"FAIL — локальный STT упал: {type(exc).__name__}: {exc}")
        return 1

    info = local_stt.get_model_info()
    print(f"Устройство: {info['device']} | compute_type: {info['compute_type']}")
    print(f"Текст: {local_text!r}")

    # --- повторный прогон для честного времени (модель уже в памяти) ---
    warm_started = time.perf_counter()
    local_stt.transcribe_wav(WAV_PATH)
    warm_seconds = time.perf_counter() - warm_started
    print(f"Время: {warm_seconds:.2f} c (первый запуск с загрузкой модели: {cold_seconds:.2f} c)")
    print()

    # --- облако для сравнения ---
    print("--- Облачный движок (polza) ---")
    cloud_text = ""
    try:
        cloud_started = time.perf_counter()
        cloud_text = transcribe_cloud(WAV_PATH)
        cloud_seconds = time.perf_counter() - cloud_started
        print(f"Текст: {cloud_text!r}")
        print(f"Время (включая сеть): {cloud_seconds:.2f} c")
    except Exception as exc:  # noqa: BLE001
        print(f"Облако недоступно: {type(exc).__name__}: {exc}")
    print()

    # --- итог ---
    print("--- Проверка ---")
    ok = True
    if local_text.strip():
        print("OK  локальный текст не пустой")
    else:
        ok = False
        print("X   локальный текст пустой")

    if cloud_text.strip():
        same = local_text.strip().lower() == cloud_text.strip().lower()
        print(f"    локально: {local_text!r}")
        print(f"    облако:   {cloud_text!r}")
        print(f"    совпадение строк: {'да' if same else 'нет (нормально: разные движки)'}")

    if info["device"] == "cuda":
        print(f"OK  устройство cuda ({info['compute_type']})")
        if warm_seconds < MAX_SECONDS_CUDA:
            print(f"OK  время {warm_seconds:.2f} c < {MAX_SECONDS_CUDA:.1f} c")
        else:
            ok = False
            print(f"X   время {warm_seconds:.2f} c >= {MAX_SECONDS_CUDA:.1f} c")
    else:
        ok = False
        print(
            "X   устройство cpu — CUDA не поднялась. На Windows CTranslate2 нужны "
            "cuBLAS (cublas64_12.dll) и cuDNN 9 (cudnn_ops64_9.dll) в PATH."
        )
        print("    Сейчас используется CPU — работает, но медленно.")

    print()
    print("PASS" if ok else "FAIL — скопируй весь вывод и пришли в чат")
    return 0 if ok else 1


if __name__ == "__main__":
    raise SystemExit(main())
