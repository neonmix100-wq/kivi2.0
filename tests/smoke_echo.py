"""Смоук мини-AEC и выбора устройства вывода (только numpy, без звука).

Запуск:
    python tests/smoke_echo.py
"""

from __future__ import annotations

import sys
from pathlib import Path
from types import SimpleNamespace
from unittest.mock import patch

import numpy as np
from loguru import logger

PROJECT_ROOT = Path(__file__).resolve().parents[1]
if str(PROJECT_ROOT) not in sys.path:
    sys.path.insert(0, str(PROJECT_ROOT))

from kivi.audio import output
from kivi.audio.echo import EchoSuppressor, PlaybackRef
from kivi.log import setup_logging

SR = 48000
FRAMES = 1536


def check(label: str, condition: bool, detail: str = "") -> bool:
    print(("OK  " if condition else "X   ") + label + (f" — {detail}" if detail else ""))
    return bool(condition)


def _signal(seed: int, scale: float = 0.12) -> np.ndarray:
    rng = np.random.default_rng(seed)
    return rng.normal(0.0, scale, FRAMES).astype(np.float32)


def _noise(seed: int, scale: float = 0.003) -> np.ndarray:
    rng = np.random.default_rng(seed)
    return rng.normal(0.0, scale, FRAMES).astype(np.float32)


def _pcm(values: np.ndarray) -> np.ndarray:
    return np.clip(values * 32768.0, -32768, 32767).astype(np.int16)


def _energy(values: np.ndarray) -> float:
    data = values.astype(np.float32)
    if np.issubdtype(values.dtype, np.integer):
        data = data / 32768.0
    return float(np.dot(data, data))


def main() -> int:
    setup_logging()
    ok = True
    t0 = 100.0

    ref = PlaybackRef(SR)
    signal = _signal(1)
    ref.write(signal, samplerate=SR, at=t0)
    suppressor = EchoSuppressor(
        ref=ref, samplerate=SR, corr_threshold=0.6, tail_ms=600, max_delay_ms=400
    )
    mic_echo = _pcm(signal * 0.3 + _noise(2))
    out_echo = suppressor.process(mic_echo, now=t0 + FRAMES / SR)
    print("echo:", int(np.max(np.abs(out_echo))), suppressor.echo_dropped, suppressor.echo_cleaned)
    ok &= check(
        "эхо гасится, счётчик echo_dropped растёт",
        suppressor.echo_dropped == 1 and not np.any(out_echo),
        f"dropped={suppressor.echo_dropped}, cleaned={suppressor.echo_cleaned}",
    )

    ref_other = PlaybackRef(SR)
    ref_other.write(signal, samplerate=SR, at=t0)
    other_suppressor = EchoSuppressor(ref=ref_other, samplerate=SR)
    other = _pcm(_signal(3, 0.08))
    out_other = other_suppressor.process(other, now=t0 + FRAMES / SR)
    print("other:", np.max(np.abs(out_other)))
    ok &= check(
        "некоррелированный сигнал проходит без изменений",
        other_suppressor.echo_dropped == 0
        and other_suppressor.echo_cleaned == 0
        and np.array_equal(out_other, other),
        f"dropped={other_suppressor.echo_dropped}, cleaned={other_suppressor.echo_cleaned}",
    )

    voice = _signal(4, 0.04)
    ref_voice = PlaybackRef(SR)
    ref_voice.write(signal, samplerate=SR, at=t0)
    voice_suppressor = EchoSuppressor(ref=ref_voice, samplerate=SR)
    mic_voice = _pcm(voice + signal * 0.3)
    out_voice = voice_suppressor.process(mic_voice, now=t0 + FRAMES / SR)
    mic_voice_f = mic_voice.astype(np.float32) / 32768.0
    out_voice_f = out_voice.astype(np.float32) / 32768.0
    echo_before = _energy(mic_voice_f - voice)
    echo_after = _energy(out_voice_f - voice)
    print("voice/echo energy:", echo_before, echo_after, voice_suppressor.echo_cleaned)
    ok &= check(
        "голос поверх речи остаётся, эхо падает >4x",
        voice_suppressor.echo_cleaned == 1
        and echo_after * 4.0 < echo_before
        and np.dot(out_voice.astype(np.float32), voice.astype(np.float32)) > 0,
        f"before={echo_before:.6f}, after={echo_after:.6f}",
    )

    ref_delay = PlaybackRef(SR)
    ref_delay.write(signal, samplerate=SR, at=t0)
    delay_suppressor = EchoSuppressor(ref=ref_delay, samplerate=SR)
    delayed = _pcm(signal * 0.3 + _noise(5))
    out_delay = delay_suppressor.process(delayed, now=t0 + 0.120 + FRAMES / SR)
    print("delay 120ms:", delay_suppressor.echo_dropped, np.max(np.abs(out_delay)))
    ok &= check(
        "задержка 120 мс находится и гасится",
        delay_suppressor.echo_dropped == 1 and not np.any(out_delay),
        f"dropped={delay_suppressor.echo_dropped}",
    )

    ref_tail = PlaybackRef(SR)
    ref_tail.write(signal, samplerate=SR, at=t0)
    tail_suppressor = EchoSuppressor(ref=ref_tail, samplerate=SR, tail_ms=600)
    tail_mic = _pcm(signal * 0.3 + _noise(6))
    tail_now = t0 + FRAMES / SR + 0.120
    out_tail = tail_suppressor.process(tail_mic, now=tail_now)
    print("tail:", tail_suppressor.echo_dropped, np.max(np.abs(out_tail)))
    ok &= check(
        "tail_ms подавляет хвост после конца ref",
        tail_suppressor.echo_dropped == 1 and not np.any(out_tail),
        f"dropped={tail_suppressor.echo_dropped}",
    )

    # --- выбор системного выхода ---
    devices = [
        {"name": "input", "max_input_channels": 1, "max_output_channels": 0, "default_samplerate": 48000},
        {"name": "Колонки A", "max_input_channels": 0, "max_output_channels": 2, "default_samplerate": 48000},
        {"name": "Наушники B", "max_input_channels": 0, "max_output_channels": 2, "default_samplerate": 48000},
    ]

    def fake_devices(index=None):
        return list(devices) if index is None else devices[int(index)]

    def fake_config():
        return "", ""

    logs: list[str] = []
    sink = logger.add(lambda message: logs.append(str(message)), level="INFO")
    try:
        with patch.object(output, "_config", fake_config):
            with patch.object(output.sd, "query_devices", fake_devices):
                with patch.object(output.sd, "default", SimpleNamespace(device=(0, 1))):
                    output._OUTPUT_CACHE = None
                    output._OUTPUT_LAST = None
                    first = output.resolve_output_device(force=True)
                with patch.object(output.sd, "default", SimpleNamespace(device=(0, 2))):
                    second = output.resolve_output_device(force=True)
    finally:
        try:
            logger.remove(sink)
        except ValueError:  # noqa: BLE001 - sink уже удалён
            pass
    print("captured logs:", len(logs), logs)
    print("output devices:", first, "->", second)
    ok &= check(
        "пустое имя -> системный default на момент вызова",
        first[0] == 1 and first[1] == "Колонки A",
        str(first),
    )
    ok &= check(
        "смена default видна и логируется",
        second[0] == 2
        and second[1] == "Наушники B"
        and any("аудио: выход сменился на [2] Наушники B" in item for item in logs),
        " | ".join(logs),
    )

    print()
    print("PASS" if ok else "FAIL — скопируй весь вывод и пришли в чат")
    return 0 if ok else 1


if __name__ == "__main__":
    raise SystemExit(main())

