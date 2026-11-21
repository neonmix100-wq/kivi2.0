"""Смоук шага 1.14: loopback WebSocket API ядра (без микрофона).

Запуск:
    python tests/smoke_api.py

Проверяется:
  1) status при подключении и get_status (state, volume);
  2) set_volume меняет prefs; get_gaps возвращает список; tail_logs непуст;
  3) ask: событие клиенту -> ask_response -> OverlayService.ask вернул ответ;
  4) confirm без подключённых клиентов -> False (headless);
  5) --no-api: сервер не создаётся.

Без tkinter, без реальных LLM/TTS.
"""

from __future__ import annotations

import json
import sys
import threading
import time
from pathlib import Path
from typing import Any

PROJECT_ROOT = Path(__file__).resolve().parents[1]
if str(PROJECT_ROOT) not in sys.path:
    sys.path.insert(0, str(PROJECT_ROOT))

from websockets.sync.client import connect

import kivi.main as main_module
from kivi import prefs
from kivi.api.server import ApiServer
from kivi.log import setup_logging
from kivi.ui.overlay import OverlayService

TMP = PROJECT_ROOT / "data" / "tmp"
PREFS_SMOKE = TMP / "api_prefs_smoke.json"


def check(label: str, condition: bool, detail: str = "") -> bool:
    mark = "OK  " if condition else "X   "
    print(f"{mark} {label}" + (f" — {detail}" if detail else ""))
    return bool(condition)


class FakeCore:
    """Ядро-заглушка: отдаёт status_snapshot без аудио."""

    state = "THINKING"
    window_until = 0.0
    router = None

    def status_snapshot(self) -> dict[str, Any]:
        return {
            "state": "THINKING",
            "budget_used": 1.25,
            "volume": prefs.get_tts_volume(),
            "window_open": False,
        }


def recv_json(client: Any, timeout: float = 5.0) -> dict[str, Any]:
    raw = client.recv(timeout=timeout)
    return json.loads(raw)


def wait_event(client: Any, name: str, timeout: float = 5.0) -> dict[str, Any]:
    """Ждать событие с нужным именем, пропуская логи/статусы."""
    deadline = time.monotonic() + timeout
    while time.monotonic() < deadline:
        message = recv_json(client, timeout=max(0.1, deadline - time.monotonic()))
        if message.get("event") == name:
            return message
    raise TimeoutError(f"событие {name} не пришло")


def request(client: Any, payload: dict[str, Any], timeout: float = 5.0) -> dict[str, Any]:
    """Отправить команду и дождаться ответа с полем ok."""
    client.send(json.dumps(payload))
    deadline = time.monotonic() + timeout
    while time.monotonic() < deadline:
        message = recv_json(client, timeout=max(0.1, deadline - time.monotonic()))
        if "ok" in message:
            return message
    raise TimeoutError(f"нет ответа на {payload.get('command')}")


def wait_ok(client: Any, timeout: float = 5.0) -> dict[str, Any]:
    deadline = time.monotonic() + timeout
    while time.monotonic() < deadline:
        message = recv_json(client, timeout=max(0.1, deadline - time.monotonic()))
        if "ok" in message:
            return message
    raise TimeoutError("нет ok-ответа")


def main() -> int:
    setup_logging()
    print("=== SMOKE 1.14: loopback WebSocket API ===")
    TMP.mkdir(parents=True, exist_ok=True)
    ok = True

    original_prefs_path = prefs.PREFS_PATH
    prefs.PREFS_PATH = PREFS_SMOKE
    PREFS_SMOKE.write_text(json.dumps({"tts_volume": 0.8}), encoding="utf-8")
    prefs.reload_prefs()

    server = ApiServer(
        port=0,
        config={
            "host": "127.0.0.1",
            "port": 0,
            "ask_timeout_s": 5,
            "confirm_timeout_s": 0.5,
        },
        core=FakeCore(),
    )
    client = None
    try:
        print("\n--- 1) запуск сервера ---")
        server.start(timeout=8)
        uri = f"ws://127.0.0.1:{server.port}"
        print(f"    {uri}, loopback={server.host == '127.0.0.1'}")
        ok &= check("сервер слушает 127.0.0.1", server.host == "127.0.0.1")

        client = connect(uri, open_timeout=5, legacy=True)
        first = recv_json(client)
        print(f"    первое событие: {first}")
        ok &= check("при подключении пришёл status", first.get("event") == "status")
        ok &= check("в status есть state", "state" in first)
        ok &= check("в status есть volume", "volume" in first)

        print("\n--- 2) get_status / set_volume / get_gaps / tail_logs ---")
        status = request(client, {"command": "get_status"})
        print(f"    get_status: {status}")
        ok &= check("get_status ok", status.get("ok") is True)
        ok &= check("state = THINKING", status.get("state") == "THINKING")
        ok &= check(
            "volume = 0.8", abs(float(status.get("volume", -1)) - 0.8) < 1e-6
        )

        volume_reply = request(client, {"command": "set_volume", "v": 0.42})
        print(f"    set_volume: {volume_reply}, prefs={prefs.get_tts_volume():.2f}")
        ok &= check("set_volume ok", volume_reply.get("ok") is True)
        ok &= check("prefs изменились", abs(prefs.get_tts_volume() - 0.42) < 1e-6)
        ok &= check("в prefs.json 0.42", PREFS_SMOKE.is_file())

        gaps = request(client, {"command": "get_gaps", "limit": 5})
        ok &= check(
            "get_gaps возвращает список",
            gaps.get("ok") is True and isinstance(gaps.get("gaps"), list),
            f"{len(gaps.get('gaps') or [])} строк",
        )

        logs = request(client, {"command": "tail_logs", "n": 50})
        lines = logs.get("lines") or []
        print(f"    tail_logs: {len(lines)} строк, последняя: {lines[-1] if lines else '—'}")
        ok &= check(
            "tail_logs непуст",
            logs.get("ok") is True and isinstance(lines, list) and bool(lines),
        )

        print("\n--- 2b) смена состояния ядра -> status ---")
        from kivi.wake.loop import WakeLoop

        core = WakeLoop.__new__(WakeLoop)
        core._state = "IDLE"
        core.window_until = 0.0
        core.router = None
        core.api = server
        server.attach(core)
        core.state = "THINKING"
        state_event = wait_event(client, "status", timeout=3)
        print(f"    status: {state_event}")
        ok &= check(
            "state-событие с THINKING",
            state_event.get("state") == "THINKING",
        )
        ok &= check(
            "в status есть volume/window_open",
            "volume" in state_event and "window_open" in state_event,
        )

        print("\n--- 3) ask -> ask_response ---")
        overlay = OverlayService(server)
        answer: dict[str, Any] = {}

        def ask_job() -> None:
            answer["value"] = overlay.ask("Продолжить?", ["да", "нет"])

        thread = threading.Thread(target=ask_job, name="smoke-ask", daemon=True)
        thread.start()
        ask_event = wait_event(client, "ask", timeout=5)
        print(f"    событие ask: {ask_event}")
        ok &= check(
            "ask содержит вопрос и варианты",
            ask_event.get("question") == "Продолжить?"
            and ask_event.get("options") == ["да", "нет"],
        )
        client.send(
            json.dumps(
                {
                    "command": "ask_response",
                    "id": ask_event.get("id"),
                    "answer": "да",
                }
            )
        )
        wait_ok(client)
        thread.join(timeout=5)
        print(f"    OverlayService.ask -> {answer.get('value')!r}")
        ok &= check("ask вернул ответ", answer.get("value") == "да")

        print("\n--- 4) headless confirm -> False ---")
        client.close()
        client = None
        for _ in range(20):
            if not server.has_clients:
                break
            time.sleep(0.05)
        started = time.perf_counter()
        confirmed = overlay.confirm("Точно?", "Проверка headless")
        elapsed = time.perf_counter() - started
        print(f"    confirm без клиентов -> {confirmed}, {elapsed:.2f} c")
        ok &= check("confirm = False", confirmed is False)
        ok &= check("сразу, без ожидания", elapsed < 1.5, f"{elapsed:.2f} c")

        print("\n--- 5) --no-api ---")
        args = main_module.parse_args(["--no-api"])
        ok &= check("--no-api распознан", args.no_api is True)
        api = main_module.create_api_server(threading.Event(), no_api=args.no_api)
        ok &= check("сервер не создаётся", api is None)
    finally:
        if client is not None:
            try:
                client.close()
            except Exception:  # noqa: BLE001
                pass
        server.stop()
        prefs.PREFS_PATH = original_prefs_path
        try:
            prefs.reload_prefs()
        except Exception:  # noqa: BLE001
            pass

    print()
    print("PASS" if ok else "FAIL — скопируй весь вывод и пришли в чат")
    return 0 if ok else 1


if __name__ == "__main__":
    raise SystemExit(main())
