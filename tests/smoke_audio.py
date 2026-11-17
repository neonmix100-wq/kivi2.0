"""Смоук шага 0.2: слушаем микрофон, печатаем события VAD, сохраняем фразу в WAV.

Запуск:
    python tests/smoke_audio.py              # слушать 15 секунд
    python tests/smoke_audio.py --seconds 8  # слушать 8 секунд

Результат: data/last_utterance.wav (16000 Гц, моно) — только что сказанная фраза.
"""

from __future__ import annotations

import argparse
import sys
import time
from pathlib import Path

PROJECT_ROOT = Path(__file__).resolve().parents[1]
if str(PROJECT_ROOT) not in sys.path:
    sys.path.insert(0, str(PROJECT_ROOT))

import numpy as np
import soundfile as sf

from kivi.audio.pipeline import VoicePipeline
from kivi.audio.recorder import DEFAULT_UTTERANCE_PATH
from kivi.audio.vad import MIN_SPEECH_MS
from kivi.config import ConfigError, DeviceNotFoundError, get_settings
from kivi.log import setup_logging

DEFAULT_SECONDS = 15.0


def main() -> int:
    parser = argparse.ArgumentParser(description="Смоук голосового ядра (шаг 0.2)")
    parser.add_argument(
        "--seconds", type=float, default=DEFAULT_SECONDS, help="сколько секунд слушать"
    )
    args = parser.parse_args()

    setup_logging()
    print("=== SMOKE 0.2: захват микрофона + Silero VAD ===", flush=True)

    try:
        settings = get_settings()
        pipeline = VoicePipeline(settings)
    except (ConfigError, DeviceNotFoundError, RuntimeError) as exc:
        print("\nFAIL — не удалось стартовать:", flush=True)
        print(exc, flush=True)
        return 1

    devices = settings.devices
    audio = settings.audio
    print(f"Вход:  [{devices.input_index}] {devices.input_name} ({devices.hostapi})")
    print(f"Выход: [{devices.output_index}] {devices.output_name}")
    print(
        f"Захват: {audio.capture_samplerate} Гц, моно, int16, блок {audio.block_ms} мс "
        f"({audio.block_frames} сэмплов), кольцо 1 с"
    )
    print(
        f"VAD: порог 0.5, min_speech {MIN_SPEECH_MS:.0f} мс, "
        f"min_silence {audio.vad_end_silence_ms} мс, pre-roll {audio.pre_roll_ms} мс"
    )
    print(f"Ресемплинг: {audio.capture_samplerate} -> {audio.target_samplerate} Гц (soxr)")
    print()
    print(f">>> ГОВОРИ СЕЙЧАС фразу ~5 секунд, потом помолчи. Слушаю {args.seconds:.0f} c. <<<")
    print()

    events: list[dict] = []

    def on_event(event: dict) -> None:
        events.append(dict(event))
        mark = "speech_start" if event["type"] == "speech_start" else "speech_end  "
        print(f"[{event['t']:6.2f} c] {mark}", flush=True)

    def on_utterance(utterance) -> None:  # noqa: ANN001
        print(
            f"           -> записано: {utterance.duration_s:.2f} c, "
            f"речь {utterance.speech_s:.2f} c, пик {utterance.peak:.2f}",
            flush=True,
        )

    try:
        run_started = time.time()
        pipeline.run(max_seconds=args.seconds, on_event=on_event, on_utterance=on_utterance)
    except RuntimeError as exc:
        print("\nFAIL —", exc, flush=True)
        return 1
    except KeyboardInterrupt:
        print("\nОстановлено вручную.", flush=True)

    print("\n--- Проверка ---", flush=True)
    ok = True
    starts = [event for event in events if event["type"] == "speech_start"]
    ends = [event for event in events if event["type"] == "speech_end"]

    if starts:
        print(f"OK  speech_start: {len(starts)} шт, первый на {starts[0]['t']:.2f} c")
    else:
        ok = False
        print("X   речь не обнаружена (нет speech_start): микрофон молчит или выбран не тот вход")
    if ends:
        print(f"OK  speech_end: {len(ends)} шт")
    elif starts:
        ok = False
        print("X   речь началась, но не закончилась — говори с паузой в конце")

    wav_path = DEFAULT_UTTERANCE_PATH
    if not wav_path.exists():
        ok = False
        print(f"X   нет файла {wav_path}")
    elif wav_path.stat().st_mtime < run_started - 1:
        ok = False
        print(
            f"X   {wav_path.name} остался от предыдущего запуска — "
            "в этот раз речь не записалась"
        )
    else:
        data, samplerate = sf.read(str(wav_path), dtype="float32")
        duration = data.size / samplerate if samplerate else 0.0
        peak = float(np.max(np.abs(data))) if data.size else 0.0
        rms = float(np.sqrt(np.mean(np.square(data)))) if data.size else 0.0
        print(
            f"OK  WAV: {wav_path} | {samplerate} Гц | {duration:.2f} c | "
            f"пик {peak:.3f} | RMS {rms:.4f}"
        )
        if duration < 0.5:
            ok = False
            print("X   файл короче 0.5 c — фраза не записалась")
        if peak < 0.02:
            ok = False
            print("X   в файле почти тишина — проверь микрофон")
        edge = int(0.05 * samplerate)
        if data.size > 4 * edge and rms > 0.01:
            head = float(np.sqrt(np.mean(np.square(data[:edge]))))
            tail = float(np.sqrt(np.mean(np.square(data[-edge:]))))
            print(f"    громкость первых 50 мс {head:.4f}, последних 50 мс {tail:.4f}")
            if head > 0.3 * rms:
                print("    ! начало записи громкое — возможно, обрезано первое слово")
            if tail > 0.3 * rms:
                print("    ! конец записи громкий — возможно, обрезано последнее слово")

    stats = pipeline.stats
    print(
        f"Захват: xruns={stats['xruns']}, потеряно блоков={stats['dropped_blocks']}, "
        f"переполнений кольца={stats['overflow_samples']}"
    )
    tail = pipeline.capture.snapshot(1000.0)
    if tail.size:
        level = float(np.max(np.abs(tail))) / 32768.0
        print(f"Уровень последней секунды записи: пик {level:.4f}")
        if level < 0.01:
            print("    ! микрофон почти ничего не слышит — проверь уровень входа в Windows")
    print()
    print("PASS" if ok else "FAIL — скопируй весь вывод и пришли в чат")
    return 0 if ok else 1


if __name__ == "__main__":
    raise SystemExit(main())
