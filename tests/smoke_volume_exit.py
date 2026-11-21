"""Смоук шага 1.13: громкость Киви, grace-выход, модель эмбеддингов.

Запуск:
    python tests/smoke_volume_exit.py

Что проверяется:
  1) assistant_volume: factor 0.5 -> тише вдвое, level 0.3 -> ровно 30%;
  2) множитель в speak (tts_volume) и prefs.json обновились;
  3) prefs.json создан и перечитывается;
  4) брокер: скилл без права self_volume -> PermissionDenied;
  5) planner маршрутизирует «сделай тише»/«громкость» в assistant_volume;
  6) grace: reflect 2 с -> "reflection завершён"; 20 с -> "прерван по таймауту";
     принудительный выход не раньше 8+2 с;
  7) эмбеддинги берут baai/bge-m3 из config, gemma2 не пробуется.

Без микрофона, без реальных LLM/TTS.
"""

from __future__ import annotations

import json
import sys
import threading
import time
import uuid
from pathlib import Path
from typing import Any

PROJECT_ROOT = Path(__file__).resolve().parents[1]
if str(PROJECT_ROOT) not in sys.path:
    sys.path.insert(0, str(PROJECT_ROOT))

from loguru import logger

import kivi.memory.retrieve as retrieve_mod
import kivi.runtime as runtime
from kivi import prefs
from kivi.config import load_config
from kivi.log import setup_logging
from kivi.skills.broker import PermissionDenied, SkillContext
from kivi.skills.loader import invoke
from kivi.skills.planner import Planner
from kivi.skills.registry import SkillManifest, SkillRegistry
from kivi.tts import speak as speak_mod
from kivi.wake.loop import WakeLoop

TMP = PROJECT_ROOT / "data" / "tmp"
PREFS_SMOKE = TMP / "prefs_smoke.json"
SKILLS_DB = TMP / "volume_skills.db"


def check(label: str, condition: bool, detail: str = "") -> bool:
    mark = "OK  " if condition else "X   "
    print(f"{mark} {label}" + (f" — {detail}" if detail else ""))
    return bool(condition)


class LogCatcher:
    def __init__(self) -> None:
        self.messages: list[str] = []
        self._sink = None

    def _write(self, message: Any) -> None:
        self.messages.append(str(message).strip())

    def __enter__(self) -> "LogCatcher":
        self._sink = logger.add(self._write, level="INFO")
        return self

    def __exit__(self, *exc_info: Any) -> bool:
        if self._sink is not None:
            logger.remove(self._sink)
            self._sink = None
        return False


class FakeReflector:
    """Заглушка рефлексии: спит заданное время, денег не тратит."""

    def __init__(self, sleep_s: float) -> None:
        self.sleep_s = float(sleep_s)
        self.runs = 0

    def run(self, messages: list[dict]) -> dict[str, Any]:
        self.runs += 1
        time.sleep(self.sleep_s)
        return {"facts": 1, "decisions": 0, "cost_rub": 0.0}


class FakeSession:
    def history(self, max_tokens: int = 1800) -> list[dict]:
        return []


def make_grace_loop(sleep_s: float) -> WakeLoop:
    """Лёгкая «оболочка» WakeLoop только для проверки wait_reflection."""
    loop = WakeLoop.__new__(WakeLoop)
    loop.reflector = FakeReflector(sleep_s)
    loop._reflect_thread = None
    loop._turns_since_reflect = 1
    loop._memory_lock = threading.Lock()
    loop._get_session = lambda: FakeSession()  # type: ignore[method-assign]
    return loop


def make_registry() -> SkillRegistry:
    registry = SkillRegistry(
        skills_dir=PROJECT_ROOT / "skills",
        installed_dir=TMP / "volume_installed",
        db_path=SKILLS_DB,
    )
    import sqlite3
    from contextlib import closing

    with closing(sqlite3.connect(str(SKILLS_DB))) as connection, connection:
        connection.execute("DELETE FROM skills_stats")
        connection.execute("DELETE FROM skill_flags")
    registry.scan()
    return registry


def main() -> int:
    setup_logging()
    print("=== SMOKE 1.13: громкость + grace-выход + эмбеддинги ===")
    TMP.mkdir(parents=True, exist_ok=True)
    ok = True

    original_prefs_path = prefs.PREFS_PATH
    original_request = retrieve_mod.MemoryRetriever._request_embedding
    prefs.PREFS_PATH = PREFS_SMOKE
    PREFS_SMOKE.write_text(json.dumps({"tts_volume": 0.8}), encoding="utf-8")
    prefs.reload_prefs()

    try:
        print("\n--- 1) assistant_volume: factor и level ---")
        registry = make_registry()
        manifest = registry.get("assistant_volume")
        ok &= check("скилл assistant_volume найден", manifest is not None)
        if manifest is None:
            raise SystemExit(1)

        reply = invoke(manifest, {"factor": 0.5}, registry=registry)
        volume = prefs.get_tts_volume()
        print(f"    factor 0.5 -> {reply!r}, tts_volume={volume:.2f}")
        ok &= check("тише вдвое (0.8 -> 0.4)", abs(volume - 0.4) < 1e-6)
        ok &= check("подтверждение «Сделала тише.»", reply == "Сделала тише.")

        reply = invoke(manifest, {"level": 0.3}, registry=registry)
        volume = prefs.get_tts_volume()
        print(f"    level 0.3 -> {reply!r}, tts_volume={volume:.2f}")
        ok &= check("level 0.3 -> ровно 0.3", abs(volume - 0.3) < 1e-6)
        ok &= check("подтверждение «Громкость 30%.»", reply == "Громкость 30%.")

        reply = invoke(manifest, {"level": "40%"}, registry=registry)
        print(f"    level 40% -> {reply!r}, tts_volume={prefs.get_tts_volume():.2f}")
        ok &= check("проценты «40%» -> 0.4", abs(prefs.get_tts_volume() - 0.4) < 1e-6)

        print("\n--- 2) множитель в speak и prefs.json ---")
        ok &= check(
            "speak.tts_volume читает настройку",
            abs(speak_mod.tts_volume() - 0.4) < 1e-6,
            f"{speak_mod.tts_volume():.2f}",
        )
        ok &= check("prefs.json создан", PREFS_SMOKE.is_file())
        payload = json.loads(PREFS_SMOKE.read_text(encoding="utf-8"))
        ok &= check(
            "в prefs.json tts_volume=0.4",
            abs(float(payload.get("tts_volume", -1)) - 0.4) < 1e-6,
            str(payload),
        )

        PREFS_SMOKE.write_text(json.dumps({"tts_volume": 0.55}), encoding="utf-8")
        reloaded = prefs.reload_prefs()
        print(f"    после ручной правки: {reloaded:.2f}")
        ok &= check("prefs перечитывается", abs(reloaded - 0.55) < 1e-6)

        print("\n--- 3) брокер: запрет без self_volume ---")
        denied_manifest = SkillManifest.model_validate(
            {
                "name": "volume_probe",
                "version": "0.0.1",
                "description": "Проверка запрета громкости",
                "category": "system",
                "triggers": ["проверить громкость"],
                "intents": ["проверить громкость"],
                "requires": {},
                "permissions": {
                    "network": [],
                    "filesystem": [],
                    "tools": [],
                    "risk": "safe",
                },
                "entry": "handler.py:handle",
                "enabled": True,
                "source": {"kind": "generated"},
            }
        )
        denied = False
        try:
            SkillContext(denied_manifest).set_self_volume(0.5)
        except PermissionDenied as exc:
            denied = True
            print(f"    PermissionDenied: {exc}")
        ok &= check("set_self_volume без права -> PermissionDenied", denied)

        denied_get = False
        try:
            SkillContext(denied_manifest).get_self_volume()
        except PermissionDenied:
            denied_get = True
        ok &= check("get_self_volume без права -> PermissionDenied", denied_get)

        print("\n--- 4) planner маршрутизирует громкость ---")
        planner = Planner(registry, strategy="fuzzy_only")
        for command in ("сделай тише", "убавь громкость", "громкость"):
            found = planner.match(command)
            name = found[0].name if found else None
            print(f"    {command!r} -> {name}")
            ok &= check(f"{command!r} -> assistant_volume", name == "assistant_volume")

        print("\n--- 5) grace: reflect 2 с и 20 с ---")
        fast_loop = make_grace_loop(2.0)
        with LogCatcher() as logs:
            started = time.perf_counter()
            fast_done = fast_loop.wait_reflection(8.0)
            fast_elapsed = time.perf_counter() - started
        print(f"    reflect 2 c: done={fast_done}, {fast_elapsed:.2f} c")
        ok &= check("2 с уложились", fast_done is True and 1.5 <= fast_elapsed < 7.0)
        ok &= check(
            "лог «reflection завершён»",
            any("reflection завершён" in line for line in logs.messages),
        )

        slow_loop = make_grace_loop(20.0)
        with LogCatcher() as logs:
            started = time.perf_counter()
            slow_done = slow_loop.wait_reflection(8.0)
            slow_elapsed = time.perf_counter() - started
        print(f"    reflect 20 c: done={slow_done}, {slow_elapsed:.2f} c")
        ok &= check("20 с прерваны таймаутом", slow_done is False)
        ok &= check("выход <= 12 с", 7.5 <= slow_elapsed < 12.0, f"{slow_elapsed:.2f} c")
        ok &= check(
            "лог «reflection прерван по таймауту»",
            any("reflection прерван по таймауту" in line for line in logs.messages),
        )
        ok &= check(
            "принудительный выход 8+2 с",
            abs(runtime.force_exit_delay(8.0) - 10.0) < 1e-6
            and abs(runtime.FORCE_EXIT_DELAY - 10.0) < 1e-6,
            f"{runtime.force_exit_delay(8.0):.0f} c",
        )

        print("\n--- 6) эмбеддинги из config ---")
        memory_cfg = load_config().model_dump().get("memory") or {}
        ok &= check(
            "config memory.embed_model = baai/bge-m3",
            memory_cfg.get("embed_model") == "baai/bge-m3",
            str(memory_cfg.get("embed_model")),
        )
        seen: list[str] = []

        def fake_embedding(self, texts, model):  # noqa: ANN001, ARG001
            seen.append(model)
            return [[0.1, 0.2] for _ in texts]

        retrieve_mod.MemoryRetriever._request_embedding = fake_embedding  # type: ignore[assignment]
        retriever = retrieve_mod.MemoryRetriever(
            db_path=TMP / f"volume_mem_{uuid.uuid4().hex[:8]}.db", k=2
        )
        print(f"    модели: {retriever.models}")
        ok &= check("первая модель baai/bge-m3", retriever.models[0] == "baai/bge-m3")
        ok &= check(
            "gemma2 не пробуется",
            "bge-multilingual-gemma2" not in retriever.models,
            str(retriever.models),
        )
        vectors, used = retriever.embed(["тест"])
        ok &= check(
            "запрос ушёл с bge-m3",
            seen == ["baai/bge-m3"] and used == "baai/bge-m3",
            str(seen),
        )
    finally:
        prefs.PREFS_PATH = original_prefs_path
        retrieve_mod.MemoryRetriever._request_embedding = original_request  # type: ignore[assignment]
        try:
            prefs.reload_prefs()
        except Exception:  # noqa: BLE001
            pass

    print()
    print("PASS" if ok else "FAIL — скопируй весь вывод и пришли в чат")
    return 0 if ok else 1


if __name__ == "__main__":
    raise SystemExit(main())
