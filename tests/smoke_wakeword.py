"""Смоук шага 0.8: прослушка и детекция имени «Киви».

Запуск:
    python tests/smoke_wakeword.py            # 60 секунд
    python tests/smoke_wakeword.py --seconds 20

Режим dry-run: имя ловим и озвучиваем «Слушаю» (из кэша, бесплатно),
но в LLM команды не отправляем — только печатаем, что отправили бы.
"""

from __future__ import annotations

import argparse
import sys
from pathlib import Path

PROJECT_ROOT = Path(__file__).resolve().parents[1]
if str(PROJECT_ROOT) not in sys.path:
    sys.path.insert(0, str(PROJECT_ROOT))

from kivi.config import ConfigError, DeviceNotFoundError, get_settings
from kivi.log import setup_logging
from kivi.brain.session import Session
from kivi.tts.cache import speak_cached
from kivi.wake.loop import WakeLoop, WakeResult

DEFAULT_SECONDS = 60.0
TMP_SESSION_DB = PROJECT_ROOT / "data" / "tmp" / "test_session.db"


class DryRouter:
    """Заглушка LLM: печатает команду и ничего не тратит."""

    def complete(self, messages, task_type: str = "chat", verify=None) -> dict:  # noqa: ANN001
        text = messages if isinstance(messages, str) else str(messages)
        print(f"    -> [dry-run] ушло бы в LLM (task={task_type}): {text!r}", flush=True)
        return {"text": ""}


def main() -> int:
    parser = argparse.ArgumentParser(description="Смоук wake-word")
    parser.add_argument("--seconds", type=float, default=DEFAULT_SECONDS)
    args = parser.parse_args()

    setup_logging()
    print("=== SMOKE: прослушка и имя «Киви» ===")

    try:
        settings = get_settings()
    except (ConfigError, DeviceNotFoundError) as exc:
        print("FAIL — конфиг:", exc)
        return 1

    results: list[WakeResult] = []

    def on_result(result: WakeResult) -> None:
        results.append(result)
        print(f"  {result}", flush=True)

    loop = WakeLoop(
        settings=settings,
        router=DryRouter(),
        speak_fn=speak_cached,
        on_result=on_result,
        session=Session(db_path=TMP_SESSION_DB),
    )

    devices = settings.devices
    print(f"Вход:  [{devices.input_index}] {devices.input_name}")
    print(f"Выход: [{devices.output_index}] {devices.output_name}")
    print(f"Слушаю {args.seconds:.0f} c. Скажи: «Киви», «Киви, привет», «Киви, <команда>».")
    print()
    print(flush=True)

    try:
        loop.run(max_seconds=args.seconds)
    except KeyboardInterrupt:
        print("\nОстановлено вручную.")

    total = len(results)
    wake_hits = [item for item in results if item.is_wake]
    fuzzy_hits = [item for item in wake_hits if item.fuzzy]
    listens = [item for item in wake_hits if item.action == "listen"]
    thinks = [item for item in results if item.action in ("think", "followup")]
    idle = [item for item in results if item.action == "idle"]
    cooldown = [item for item in results if item.action == "cooldown"]

    print()
    print("--- Сводка ---")
    print(f"распознано высказываний: {total}")
    print(f"срабатываний по имени:   {len(wake_hits)}")
    print(f"  из них нечётких (могут быть ложными): {len(fuzzy_hits)}")
    print(f"  «Слушаю» + окно:       {len(listens)}")
    print(f"команд отправлено в LLM (dry-run): {len(thinks)}")
    print(f"фраз без имени (молча):  {len(idle)}")
    print(f"отсечено cooldown:       {len(cooldown)}")
    print()
    print(f"ложных (нечётких) срабатываний: {len(fuzzy_hits)}")

    ok = True
    if total == 0:
        print("X   ничего не распознано — проверь микрофон и уровень входа")
        ok = False
    if not wake_hits:
        print("X   ни одного срабатывания по имени «Киви»")
        ok = False
    print()
    print("PASS" if ok else "FAIL — скопируй весь вывод и пришли в чат")
    return 0 if ok else 1


if __name__ == "__main__":
    raise SystemExit(main())
