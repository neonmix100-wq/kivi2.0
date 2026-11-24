"""Смоук запуска smoke-тестов из API/UI: whitelist, события, busy.

Запуск:
    python tests/smoke_test_runner.py

Проверяется:
  1) list_tests автообнаруживает tests/smoke_*.py и содержит chat_ui/reflect_sched;
  2) run_test("smoke_reflect_sched") даёт test_line и test_done ok=True;
  3) неизвестное имя и ../evil отклоняются;
  4) второй run_test, пока первый держит lock, возвращает busy;
  5) HTML/JS содержат блок «Тесты» и обработку test_line/test_done.
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

from kivi.api.server import ApiServer
from kivi.log import setup_logging


def check(label: str, condition: bool, detail: str = "") -> bool:
    mark = "OK  " if condition else "X   "
    print(f"{mark} {label}" + (f" — {detail}" if detail else ""))
    return bool(condition)


class FakeCore:
    state = "IDLE"
    window_until = 0.0

    def status_snapshot(self) -> dict[str, Any]:
        return {
            "state": "IDLE",
            "budget_used": 0.0,
            "volume": 0.8,
            "window_open": False,
        }


def recv_json(client: Any, timeout: float = 5.0) -> dict[str, Any]:
    return json.loads(client.recv(timeout=timeout))


def request_command(client: Any, payload: dict[str, Any], timeout: float = 5.0) -> dict[str, Any]:
    """Ответ на команду; события (в т.ч. test_done с ok) пропускаем."""
    client.send(json.dumps(payload))
    deadline = time.monotonic() + timeout
    while time.monotonic() < deadline:
        message = recv_json(client, timeout=max(0.1, deadline - time.monotonic()))
        if "event" not in message and "ok" in message:
            return message
    raise TimeoutError(f"нет ответа на {payload.get('command')}")


def wait_test_done(
    client: Any, name: str, timeout: float = 30.0
) -> tuple[dict[str, Any], list[str]]:
    """Собрать test_line и дождаться test_done конкретного теста."""
    lines: list[str] = []
    deadline = time.monotonic() + timeout
    while time.monotonic() < deadline:
        message = recv_json(client, timeout=max(0.1, deadline - time.monotonic()))
        if message.get("event") == "test_line" and message.get("name") == name:
            lines.append(str(message.get("line") or ""))
        if message.get("event") == "test_done" and message.get("name") == name:
            return message, lines


def main() -> int:
    setup_logging()
    print("=== SMOKE: test runner API/UI ===")
    ok = True
    server = ApiServer(port=0, config={"host": "127.0.0.1", "port": 0}, core=FakeCore())
    client = None
    try:
        server.start(timeout=8)
        uri = f"ws://127.0.0.1:{server.port}"
        client = connect(uri, open_timeout=5, legacy=True)
        recv_json(client)  # первичный status

        print("\n--- 1) list_tests ---")
        listed = request_command(client, {"command": "list_tests"})
        names = list(listed.get("tests") or [])
        print("    тестов:", len(names), names[:8], "...")
        ok &= check("list_tests ok", listed.get("ok") is True)
        ok &= check("smoke_chat_ui в списке", "smoke_chat_ui" in names)
        ok &= check("smoke_reflect_sched в списке", "smoke_reflect_sched" in names)
        ok &= check(
            "имена без пути и .py",
            all(not item.endswith(".py") and "/" not in item and "\\" not in item for item in names),
        )
        ok &= check("список отсортирован", names == sorted(names))

        print("\n--- 2) run_test -> test_line/test_done ---")
        started = request_command(client, {"command": "run_test", "name": "smoke_reflect_sched"})
        print("    run_test:", started)
        ok &= check("run_test принят", started.get("ok") is True)
        done, lines = wait_test_done(client, "smoke_reflect_sched", timeout=40)
        print("    test_done:", done)
        print("    test_line:", ascii(lines[:3]), "... всего", len(lines))
        ok &= check("есть test_line", bool(lines))
        ok &= check("ok=True", done.get("ok") is True and done.get("code") == 0)
        ok &= check("есть duration_s", isinstance(done.get("duration_s"), (int, float)))

        print("\n--- 3) чужое имя / путь ---")
        bad = server.run_test("не_существует")
        evil = server.run_test("../../evil")
        print("    не_существует:", bad)
        print("    ../../evil:", evil)
        ok &= check("неизвестное имя отклонено", bad.get("ok") is False)
        ok &= check("path traversal отклонён", evil.get("ok") is False)


        print("\n--- 4) одновременно только один ---")
        release = threading.Event()
        entered = threading.Event()
        original_worker = server._run_test_worker

        def held_worker(name: str) -> None:
            entered.set()
            release.wait(5)
            server._test_running = None
            try:
                server._test_lock.release()
            except RuntimeError:
                pass

        server._run_test_worker = held_worker  # type: ignore[method-assign]
        try:
            first = server.run_test("smoke_reflect_sched")
            entered.wait(2)
            second = server.run_test("smoke_reflect_sched")
            print("    first:", first)
            print("    second:", second)
            ok &= check("первый принят", first.get("ok") is True)
            ok &= check(
                "второй -> busy",
                second.get("ok") is False and second.get("error") == "busy",
            )
        finally:
            release.set()
            time.sleep(0.1)
            server._run_test_worker = original_worker  # type: ignore[method-assign]

        print("\n--- 5) UI-блок и события ---")
        html = (PROJECT_ROOT / "app" / "renderer" / "index.html").read_text(encoding="utf-8")
        js = (PROJECT_ROOT / "app" / "renderer" / "app.js").read_text(encoding="utf-8")
        ok &= check("HTML: tests-list", 'id="tests-list"' in html)
        ok &= check("HTML: tests-output", 'id="tests-output"' in html)
        ok &= check("HTML: tests-caption", 'id="tests-caption"' in html)
        ok &= check("HTML: Прогнать все", "Прогнать все" in html)
        ok &= check("JS: list_tests", "list_tests" in js)
        ok &= check("JS: run_test", "run_test" in js)
        ok &= check("JS: test_line", "test_line" in js)
        ok &= check("JS: test_done", "test_done" in js)
        ok &= check("JS: currentTest", "currentTest" in js)
        ok &= check(
            "JS: очистка консоли при старте",
            "out.textContent = ''" in js and "function startTestUI(name)" in js,
        )
        ok &= check(
            "JS: очистка после accepted run_test",
            "reply.started === true" in js and "startTestUI(name)" in js,
        )
        ok &= check(
            "JS: фильтр test_line по currentTest",
            "msg.name !== currentTest" in js,
        )
        ok &= check(
            "JS: test_done только для currentTest",
            "msg.name !== currentTest" in js,
        )
        ok &= check("JS: заголовок «Вывод: name»", "Вывод: " in js)
        ok &= check("JS: старый testsCurrent удалён", "testsCurrent" not in js)
    finally:
        if client is not None:
            try:
                client.close()
            except Exception:  # noqa: BLE001
                pass
        server.stop()

    print()
    print("PASS" if ok else "FAIL — скопируй вывод и пришли в чат")
    return 0 if ok else 1


if __name__ == "__main__":
    raise SystemExit(main())
