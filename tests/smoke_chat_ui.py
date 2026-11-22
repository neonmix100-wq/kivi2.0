"""Смоук секции «Чат»: send_text/get_history/turn events и лок turn'ов.

Запуск:
    python tests/smoke_chat_ui.py

Звук TTS не используется: speak подменён списком, ApiServer запускается на
случайном loopback-порту, session — во временной БД.
"""

from __future__ import annotations

import sys
import threading
import time
import uuid
from pathlib import Path
from typing import Any

PROJECT_ROOT = Path(__file__).resolve().parents[1]
if str(PROJECT_ROOT) not in sys.path:
    sys.path.insert(0, str(PROJECT_ROOT))

from kivi.api.client import ApiClient
from kivi.api.server import ApiServer
from kivi.brain.session import Session
from kivi.config import get_settings
from kivi.log import setup_logging
from kivi.memory.vault import read_note
from kivi.skills.registry import SkillRegistry
from kivi.wake.loop import WakeLoop

TMP = PROJECT_ROOT / "data" / "tmp"


def check(label: str, condition: bool, detail: str = "") -> bool:
    print(("OK  " if condition else "X   ") + label + (f" — {detail}" if detail else ""))
    return bool(condition)


class FakeCapture:
    samplerate = 48000
    block_frames = 1536
    is_running = False

    def read_block(self, timeout: float | None = None) -> None:
        return None

    def stop(self) -> None:
        return None


class FakeVad:
    def reset(self) -> None:
        return None

    def release(self) -> None:
        return None


class FakeRecorder:
    def cancel(self) -> None:
        return None


class FakePlanner:
    def __init__(self, registry: SkillRegistry) -> None:
        self.registry = registry

    def plan(self, command: str, history: Any = None) -> dict[str, Any]:
        if command.lower().startswith("запомни"):
            return {
                "skill": self.registry.get("remember"),
                "args": {
                    "kind": "schedule",
                    "when": "15:00",
                    "what": "стендап",
                },
                "missing": [],
                "source": "test",
                "route": "skill",
                "gap_id": None,
            }
        return {
            "skill": None,
            "args": {},
            "missing": [],
            "source": "test",
            "route": "chat",
            "gap_id": None,
        }


def turn_events(client: ApiClient) -> list[dict[str, Any]]:
    return [item for item in client.events if item.get("event") == "turn"]


def main() -> int:
    setup_logging()
    print("=== SMOKE: чат UI / send_text / turn events ===")
    TMP.mkdir(parents=True, exist_ok=True)
    ok = True
    token = uuid.uuid4().hex
    vault = PROJECT_ROOT / "memory" / f"chat_ui_{token}"
    session_db = TMP / f"chat_ui_{token}.db"

    import kivi.memory.vault as vault_mod

    original_root = vault_mod.vault_root

    def test_root(path: Any = None) -> Path:
        return Path(path) if path is not None else vault

    vault_mod.vault_root = test_root  # type: ignore[assignment]

    settings = get_settings()
    session = Session(db_path=session_db, idle_reset_s=10_000)
    spoken: list[str] = []
    loop = WakeLoop(
        settings=settings,
        router=None,
        capture=FakeCapture(),  # type: ignore[arg-type]
        vad=FakeVad(),  # type: ignore[arg-type]
        recorder=FakeRecorder(),  # type: ignore[arg-type]
        speak_fn=lambda text: spoken.append(text),
        session=session,
    )
    loop.think_fn = lambda command: f"Ответ на {command}"
    loop.planner = FakePlanner(loop.skills)  # type: ignore[arg-type]
    server = ApiServer(port=0, config={"text_speaks": False}, core=loop)
    loop.api = server
    server.start()
    client = ApiClient(f"ws://127.0.0.1:{server.port}", request_timeout=20.0)
    try:
        print("\n--- 1) текст: запомни -> skill remember ---")
        reply = client.request("send_text", text="запомни стендап в 15:00")
        print("reply:", reply)
        schedule = read_note("schedule", "", vault)
        history = client.request("get_history", limit=50).get("turns") or []
        print("history:", history)
        ok &= check(
            "send_text -> route skill, remember",
            reply.get("ok") is True and reply.get("route") == "skill",
            str(reply),
        )
        ok &= check("schedule.md пополнился", "стендап" in schedule.lower())
        ok &= check(
            "history содержит пару user+kivi",
            len(history) >= 2
            and history[-2].get("role") == "user"
            and history[-1].get("role") == "kivi",
            str(history[-2:]),
        )
        events = turn_events(client)
        ok &= check(
            "WS turn обеих ролей source=text",
            any(e.get("role") == "user" and e.get("source") == "text" for e in events)
            and any(e.get("role") == "kivi" and e.get("source") == "text" for e in events),
            str(events),
        )


        print("\n--- 2) текст: привет -> chat ---")
        client.events.clear()
        reply_chat = client.request("send_text", text="привет")
        print("reply:", reply_chat)
        ok &= check(
            "привет -> route chat",
            reply_chat.get("ok") is True and reply_chat.get("route") == "chat",
            str(reply_chat),
        )

        print("\n--- 3) голосовой turn source=voice ---")
        client.events.clear()
        response, route = loop.handle_command(
            "голосовая реплика", source="voice", speak=False
        )
        print("voice:", response, route)
        client.request("get_history", limit=1)
        voice_events = turn_events(client)
        ok &= check(
            "voice turn приходит клиенту с source=voice",
            bool(voice_events)
            and all(e.get("source") == "voice" for e in voice_events),
            str(voice_events),
        )

        print("\n--- 4) get_history limit=5 хронологически ---")
        limited = client.request("get_history", limit=5).get("turns") or []
        print("limited:", limited)
        ok &= check("get_history <= 5", 0 < len(limited) <= 5, str(len(limited)))
        ok &= check(
            "history хронологический",
            [float(item.get("ts") or 0) for item in limited]
            == sorted(float(item.get("ts") or 0) for item in limited),
            str([item.get("ts") for item in limited]),
        )

        print("\n--- 5) text_speaks=false -> speak не вызывается ---")
        before_spoken = len(spoken)
        reply_silent = client.request("send_text", text="ещё одна реплика")
        ok &= check(
            "text_speaks=false не озвучивает",
            len(spoken) == before_spoken,
            f"spoken={spoken}",
        )
        ok &= check("ответ всё равно в history", reply_silent.get("ok") is True)

        print("\n--- 6) два send_text не отвечают параллельно ---")
        active = {"now": 0, "max": 0}
        active_lock = threading.Lock()

        def slow_think(command: str) -> str:
            with active_lock:
                active["now"] += 1
                active["max"] = max(active["max"], active["now"])
            time.sleep(0.25)
            with active_lock:
                active["now"] -= 1
            return f"медленный {command}"

        loop.think_fn = slow_think
        clients = [
            ApiClient(f"ws://127.0.0.1:{server.port}", request_timeout=20.0)
            for _ in range(2)
        ]
        results: list[dict[str, Any]] = []
        threads = [
            threading.Thread(
                target=lambda idx=idx: results.append(
                    clients[idx].request("send_text", text=f"конкурент {idx}")
                )
            )
            for idx in range(2)
        ]
        for thread in threads:
            thread.start()
        for thread in threads:
            thread.join(timeout=10)
        for item in clients:
            item.close()
        print("concurrency:", active, results)
        ok &= check(
            "turn lock: не более одного ответа одновременно",
            active["max"] == 1 and len(results) == 2,
            f"max={active['max']}, ответов={len(results)}",
        )
    finally:
        client.close()
        server.stop()
        try:
            loop.stop()
        except Exception:  # noqa: BLE001 - тестовые фейки
            pass
        vault_mod.vault_root = original_root  # type: ignore[assignment]

    print()
    print("PASS" if ok else "FAIL — скопируй весь вывод и пришли в чат")
    return 0 if ok else 1


if __name__ == "__main__":
    raise SystemExit(main())

