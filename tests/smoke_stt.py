"""Смоук шага 0.3: распознавание речи из готового wav.

Запуск:
    python tests/smoke_stt.py                     # data/last_utterance.wav
    python tests/smoke_stt.py data/какой-то.wav   # другой файл
"""

from __future__ import annotations

import sys
from pathlib import Path

PROJECT_ROOT = Path(__file__).resolve().parents[1]
if str(PROJECT_ROOT) not in sys.path:
    sys.path.insert(0, str(PROJECT_ROOT))

import numpy as np
import soundfile as sf

from kivi.log import setup_logging
from kivi.stt.transcribe import STT_MODEL, TranscribeError, transcribe_wav

DEFAULT_WAV = PROJECT_ROOT / "data" / "last_utterance.wav"


def main() -> int:
    setup_logging()
    wav_path = Path(sys.argv[1]) if len(sys.argv) > 1 else DEFAULT_WAV
    if not wav_path.is_absolute():
        wav_path = PROJECT_ROOT / wav_path

    print("=== SMOKE 0.3: распознавание речи (облако, whisper) ===", flush=True)
    print(f"Файл: {wav_path}")
    print(f"Модель: {STT_MODEL}", flush=True)

    if not wav_path.exists():
        print(f"\nFAIL — нет файла {wav_path}")
        print("Сначала запиши фразу: python tests\\smoke_audio.py")
        return 1

    data, samplerate = sf.read(str(wav_path), dtype="float32")
    duration = data.size / samplerate if samplerate else 0.0
    peak = float(np.max(np.abs(data))) if data.size else 0.0
    print(f"Длительность: {duration:.2f} c | {samplerate} Гц | пик {peak:.4f}")
    if peak < 0.01:
        print("    ! запись очень тихая — распознавание может ошибаться (это не баг)")
    print()

    try:
        text = transcribe_wav(wav_path)
    except TranscribeError as exc:
        print("\nFAIL —", exc)
        return 1

    print("\n--- Результат ---")
    print(f"Текст: {text!r}")
    ok = any(ch.isalpha() for ch in text)
    if not ok:
        print("X   текст пустой или без букв")
    print()
    print("PASS" if ok else "FAIL — скопируй весь вывод и пришли в чат")
    return 0 if ok else 1


if __name__ == "__main__":
    raise SystemExit(main())
