"""Смоук шага 0.6: полный цикл разговора голосом.

Микрофон -> VAD -> STT -> LLM -> TTS -> наушники.
Запуск:
    python tests/smoke_conversation.py
"""

from __future__ import annotations

import sys
from pathlib import Path

PROJECT_ROOT = Path(__file__).resolve().parents[1]
if str(PROJECT_ROOT) not in sys.path:
    sys.path.insert(0, str(PROJECT_ROOT))

from kivi.config import ConfigError, DeviceNotFoundError
from kivi.conversation import Conversation, ConversationError
from kivi.log import setup_logging
from kivi.stt.transcribe import TranscribeError
from kivi.tts.speak import SpeakError

TIMEOUT_SECONDS = 60.0


def main() -> int:
    setup_logging()
    print("=== SMOKE 0.6: разговор голосом (микрофон -> VAD -> STT -> LLM -> TTS) ===")
    print(flush=True)

    try:
        conversation = Conversation()
    except (ConfigError, DeviceNotFoundError) as exc:
        print("FAIL — конфиг:", exc)
        return 1

    devices = conversation.settings.devices
    print(f"Вход:  [{devices.input_index}] {devices.input_name}")
    print(f"Выход: [{devices.output_index}] {devices.output_name}")
    print(f"input_gain = {conversation.settings.audio.input_gain}")
    print()
    print(f"СЛУШАЮ... ГОВОРИ ФРАЗУ ~5 СЕКУНД (таймаут {TIMEOUT_SECONDS:.0f} c)")
    print(flush=True)

    try:
        turn = conversation.run_once(timeout=TIMEOUT_SECONDS)
    except (ConversationError, TranscribeError, SpeakError) as exc:
        print("\nFAIL —", exc)
        return 1
    except KeyboardInterrupt:
        print("\nОстановлено вручную.")
        return 1

    if turn is None:
        print()
        print(f"Тишина {TIMEOUT_SECONDS:.0f} c — ничего не услышал, выхожу.")
        print("Если ты говорил: проверь микрофон (python tests\\audio_probe.py 24)")
        print("и уровень входа в Windows, либо подними audio.input_gain в config.yaml.")
        print()
        print("FAIL")
        return 1

    print()
    print("--- Распознано (STT) ---")
    print(turn.heard)
    print()
    print("--- Ответ LLM ---")
    print(turn.reply)
    print()
    print("--- Озвучено (TTS) ---")
    print(
        f"{turn.speech.duration_s:.2f} c, {turn.speech.samplerate} Гц, "
        f"mp3: {turn.speech.mp3_path}"
    )
    print()

    ok = (
        bool(turn.heard.strip())
        and bool(turn.reply.strip())
        and turn.speech.duration_s > 0.3
    )
    print("PASS" if ok else "FAIL — скопируй весь вывод и пришли в чат")
    return 0 if ok else 1


if __name__ == "__main__":
    raise SystemExit(main())
