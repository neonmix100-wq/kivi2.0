"""Смоук шага 0.5: озвучка фразы в наушники.

Запуск:
    python tests/smoke_speak.py
"""

from __future__ import annotations

import sys
from pathlib import Path

PROJECT_ROOT = Path(__file__).resolve().parents[1]
if str(PROJECT_ROOT) not in sys.path:
    sys.path.insert(0, str(PROJECT_ROOT))

from kivi.config import ConfigError, DeviceNotFoundError, get_settings
from kivi.log import setup_logging
from kivi.tts.speak import SpeakError, speak

PHRASE = "Привет, Лёша. Я Киви. Я тебя слышу и говорю."


def main() -> int:
    setup_logging()
    print("=== SMOKE 0.5: озвучка (TTS) в наушники ===", flush=True)
    print(f"Фраза: {PHRASE}")

    try:
        settings = get_settings()
    except (ConfigError, DeviceNotFoundError) as exc:
        print("\nFAIL — конфиг:", exc)
        return 1

    devices = settings.devices
    print(
        f"Вывод: [{devices.output_index}] {devices.output_name}, "
        f"{settings.audio.playback_samplerate} Гц, моно, s16le"
    )
    print("\n>>> Слушай наушники <<<", flush=True)

    try:
        result = speak(PHRASE)
    except SpeakError as exc:
        print("\nFAIL —", exc)
        return 1

    print("\n--- Результат ---")
    print(f"mp3: {result.mp3_path} ({result.mp3_path.stat().st_size} байт)")
    print(f"pcm: {result.pcm_path} ({result.pcm_path.stat().st_size} байт)")
    print(f"Длительность: {result.duration_s:.2f} c | {result.samplerate} Гц | пик {result.peak:.2f}")
    ok = result.duration_s > 0.5 and result.peak > 0.01
    if not ok:
        print("X   звук пустой или слишком тихий")
    print()
    print("PASS" if ok else "FAIL — скопируй весь вывод и пришли в чат")
    return 0 if ok else 1


if __name__ == "__main__":
    raise SystemExit(main())
