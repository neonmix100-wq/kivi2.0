"""Смоук сохранения кастомной темы: WS-команды, data/theme.json, asar.

Запуск:
    python tests/smoke_theme3.py

Если проверка asar упала — сначала пересобери приложение:
    cd app
    npm run dist
"""

from __future__ import annotations

import json
import sys
from pathlib import Path
from typing import Any

PROJECT_ROOT = Path(__file__).resolve().parents[1]
if str(PROJECT_ROOT) not in sys.path:
    sys.path.insert(0, str(PROJECT_ROOT))

from websockets.sync.client import connect

from kivi.api.server import ApiServer, THEME_PATH
from kivi.log import setup_logging

ROOT = PROJECT_ROOT / "app" / "renderer"
ASAR = PROJECT_ROOT / "app" / "dist" / "win-unpacked" / "resources" / "app.asar"


def check(label: str, condition: bool, detail: str = "") -> bool:
    print(("OK  " if condition else "X   ") + label + (f" — {detail}" if detail else ""))
    return bool(condition)


def recv_json(client: Any, timeout: float = 5.0) -> dict[str, Any]:
    return json.loads(client.recv(timeout=timeout))


class FakeCore:
    state = "IDLE"
    window_until = 0.0

    def status_snapshot(self) -> dict[str, Any]:
        return {"state": "IDLE", "budget_used": 0.0, "volume": 0.8, "window_open": False}


def main() -> int:
    setup_logging()
    print("=== SMOKE: theme3 / saved themes / disk persistence ===")
    ok = True
    original = THEME_PATH.read_bytes() if THEME_PATH.is_file() else None
    theme = {
        "current": {"accent": "#22c55e"},
        "saved": [{"id": "a", "name": "Тест", "state": {"accent": "#22c55e"}}],
    }
    server = ApiServer(port=0, config={"host": "127.0.0.1", "port": 0}, core=FakeCore())
    client = None
    try:
        server.start(timeout=8)
        client = connect(f"ws://127.0.0.1:{server.port}", open_timeout=5, legacy=True)
        recv_json(client)  # initial status
        client.send(json.dumps({"command": "set_theme", "theme": theme}))
        event = None
        reply = None
        for _ in range(20):
            message = recv_json(client)
            if message.get("event") == "theme_changed":
                event = message
            if "ok" in message:
                reply = message
            if event is not None and reply is not None:
                break
        print("set_theme event:", event)
        print("set_theme reply:", reply)
        ok &= check("set_theme ok", bool(reply and reply.get("ok") is True))
        ok &= check("broadcast theme_changed", bool(event and event.get("theme") == theme))

        client.send(json.dumps({"command": "get_theme"}))
        got = None
        for _ in range(8):
            message = recv_json(client)
            if message.get("event") == "theme":
                got = message.get("theme")
                break
        print("get_theme:", got)
        ok &= check("get_theme вернул current", bool(got and got.get("current") == theme["current"]))
        ok &= check("get_theme вернул saved", bool(got and got.get("saved") == theme["saved"]))

        ok &= check("data/theme.json существует", THEME_PATH.is_file(), str(THEME_PATH))
        on_disk = json.loads(THEME_PATH.read_text(encoding="utf-8")) if THEME_PATH.is_file() else {}
        print("theme.json:", json.dumps(on_disk, ensure_ascii=False))
        ok &= check("current на диске", on_disk.get("current") == theme["current"])
        ok &= check("saved на диске", on_disk.get("saved") == theme["saved"])

        # Обновление только current не должно терять saved.
        client.send(
            json.dumps(
                {
                    "command": "set_theme",
                    "theme": {"current": {"accent": "#38bdf8"}},
                }
            )
        )
        for _ in range(8):
            message = recv_json(client)
            if "ok" in message:
                break
        after = json.loads(THEME_PATH.read_text(encoding="utf-8"))
        ok &= check("saved не потерян при обновлении current", after.get("saved") == theme["saved"])

        print("\n--- static ---")
        theme_js = (ROOT / "theme.js").read_text(encoding="utf-8")
        index_html = (ROOT / "index.html").read_text(encoding="utf-8")
        overlay_html = (ROOT / "overlay.html").read_text(encoding="utf-8")
        for marker in ("SAVED_KEY", "saveCurrent", "renderSaved", "queueDisk", "toast", "theme-save"):
            ok &= check(f"theme.js: {marker}", marker in theme_js)
        ok &= check("index: data-window=main", 'data-window="main"' in index_html)
        ok &= check("overlay: data-window=overlay", 'data-window="overlay"' in overlay_html)
        ok &= check("overlay: theme.js", '<script src="theme.js"></script>' in overlay_html)

        print("\n--- packaged app.asar ---")
        if not ASAR.is_file():
            ok &= check("app.asar существует", False, f"нет {ASAR}; сначала cd app && npm run dist")
        else:
            blob = ASAR.read_bytes()
            for marker in (b"theme-save", b"theme-saved", b"theme_changed"):
                ok &= check(f"asar содержит {marker.decode()}", marker in blob)
    finally:
        if client is not None:
            client.close()
        server.stop()
        if original is not None:
            THEME_PATH.write_bytes(original)

    print()
    print("PASS" if ok else "FAIL — пересобери: cd app; npm run dist")
    return 0 if ok else 1


if __name__ == "__main__":
    raise SystemExit(main())
