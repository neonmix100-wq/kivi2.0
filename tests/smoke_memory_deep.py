"""Смоук структурированной памяти: profile/schedule/tasks/preferences.

Запуск:
    python tests/smoke_memory_deep.py

Проверяется:
  1) reflection раскладывает schedule/tasks/preferences по файлам;
  2) повторная реплика не создаёт дублей;
  3) retrieve("что у меня сегодня") подмешивает schedule.md;
  4) recall — builtin-скилл с permissions.filesystem на memory;
  5) reflect_now через WebSocket API отвечает ok.
"""

from __future__ import annotations

import importlib.util
import json
import sys
import uuid
from datetime import datetime
from pathlib import Path
from typing import Any

PROJECT_ROOT = Path(__file__).resolve().parents[1]
if str(PROJECT_ROOT) not in sys.path:
    sys.path.insert(0, str(PROJECT_ROOT))

from kivi.api.client import ApiClient
from kivi.api.server import ApiServer
from kivi.config import get_settings
from kivi.log import setup_logging
from kivi.memory.reflect import Reflector
from kivi.memory.retrieve import MemoryRetriever
from kivi.memory.vault import iter_schedule, iter_tasks, read_note
from kivi.skills.loader import load
from kivi.skills.registry import SkillRegistry

TMP = PROJECT_ROOT / "data" / "tmp"
DIALOG = [
    {"role": "user", "content": "каждый будний день в 09:00 стендап; купи корм; люблю короткий ответ"},
    {"role": "assistant", "content": "Запомнила: будни 09:00 стендап, купить корм, короткий ответ."},
]
PAYLOAD = {
    "facts": [{"text": "Лёша любит кофе", "kind": "profile"}],
    "schedule": [{"when": "каждый будний день в 9 утра", "what": "стендап"}],
    "tasks": [{"what": "купить корм", "status": "open"}],
    "preferences": [{"text": "люблю короткий ответ"}],
    "decisions": [],
    "open_loops": [],
}


def check(label: str, condition: bool, detail: str = "") -> bool:
    print(("OK  " if condition else "X   ") + label + (f" — {detail}" if detail else ""))
    return bool(condition)


class FakeRouter:
    def __init__(self, payload: dict[str, Any]) -> None:
        self.payload = payload
        self.calls = 0

    def complete(self, messages: list[dict], **_kwargs: Any) -> dict[str, Any]:
        self.calls += 1
        return {"text": json.dumps(self.payload, ensure_ascii=False), "cost_rub": 0.0}


class FakeSession:
    def messages_since(self, since_ts: float, *, max_tokens: int = 3000):
        return [{"role": "user", "content": "тестовая реплика"}], 1234.0

    def history(self, max_tokens: int = 1800) -> list[dict]:
        return [{"role": "user", "content": "тестовая реплика"}]


class FakeReflector:
    def __init__(self) -> None:
        self.calls = 0

    def run(self, messages: list[dict], *, force: bool = False) -> dict[str, Any]:
        self.calls += 1
        return {"facts": 0, "schedule": 0, "tasks": 0, "preferences": 0, "skipped": ""}


class FakeCore:
    state = "IDLE"
    window_until = 0.0
    router = None

    def __init__(self) -> None:
        self.reflector = FakeReflector()
        self.reflect_scheduler = None
        self._last_reflected_ts = 0.0

    def _get_session(self) -> FakeSession:
        return FakeSession()

    def _on_reflected(self, ts: float) -> None:
        self._last_reflected_ts = float(ts)

    def status_snapshot(self) -> dict[str, Any]:
        return {"state": "IDLE", "budget_used": 0.0, "window_open": False}


class FakeCtx:
    def __init__(self, files: dict[str, str]) -> None:
        self.files = files
        self.logs: list[str] = []

    def fs_read(self, path: str) -> str:
        key = str(path).replace("\\", "/")
        if key not in self.files:
            raise FileNotFoundError(key)
        return self.files[key]

    def now(self) -> datetime:
        return datetime(2026, 11, 22, 9, 30)

    def log(self, text: str) -> None:
        self.logs.append(str(text))


def main() -> int:
    setup_logging()
    print("=== SMOKE: структурированная память ===")
    TMP.mkdir(parents=True, exist_ok=True)
    vault = TMP / f"memory_deep_{uuid.uuid4().hex}"
    index_db = TMP / f"memory_deep_index_{uuid.uuid4().hex}.db"
    ok = True

    router = FakeRouter(PAYLOAD)
    reflector = Reflector(
        router,
        vault_path=vault,
        min_interval_s=0.0,
    )
    stats = reflector.run(DIALOG, force=True)
    print("первый reflect:", stats)
    schedule_text = read_note("schedule", "", vault)
    tasks_text = read_note("tasks", "", vault)
    profile_text = read_note("profile", "", vault)
    print("schedule.md:\n" + schedule_text.strip())
    print("tasks.md:\n" + tasks_text.strip())

    ok &= check(
        "schedule содержит будни 09:00 и стендап",
        "будни 09:00" in schedule_text and "стендап" in schedule_text.lower(),
    )
    ok &= check(
        "tasks содержит open-задачу",
        "[open]" in tasks_text and "купить корм" in tasks_text.lower(),
    )
    ok &= check(
        "profile содержит предпочтение",
        "короткий ответ" in profile_text.lower(),
    )

    schedule_before = len(iter_schedule(vault))
    tasks_before = len(iter_tasks(vault))
    preferences_before = profile_text.lower().count("короткий ответ")
    stats_again = reflector.run(DIALOG, force=True)
    print("повторный reflect:", stats_again)
    ok &= check(
        "повторная реплика не дублирует schedule",
        len(iter_schedule(vault)) == schedule_before,
    )
    ok &= check(
        "повторная реплика не дублирует tasks",
        len(iter_tasks(vault)) == tasks_before,
    )
    ok &= check(
        "повторная реплика не дублирует preference",
        read_note("profile", "", vault).lower().count("короткий ответ")
        == preferences_before,
    )

    retriever = MemoryRetriever(db_path=index_db, vault_path=vault)
    retriever._request_embedding = lambda _texts, _model: None  # type: ignore[method-assign]
    found = retriever.retrieve("что у меня сегодня")
    print("retrieve:", found)
    ok &= check(
        "retrieve сегодня включает schedule",
        any("стендап" in item.lower() and "09:00" in item for item in found),
    )

    from kivi.memory.vault import add_task

    closed = add_task("купить корм", "done", path=vault)
    tasks_after_done = read_note("tasks", "", vault)
    print("tasks после done:\n" + tasks_after_done.strip())
    ok &= check(
        "done помечает существующую open-задачу",
        closed and "[done]" in tasks_after_done and "[open] купить" not in tasks_after_done,
    )

    registry = SkillRegistry(
        skills_dir=PROJECT_ROOT / "skills",
        installed_dir=TMP / f"memory_deep_installed_{uuid.uuid4().hex}",
        db_path=TMP / f"memory_deep_skills_{uuid.uuid4().hex}.db",
    )
    registry.scan()
    recall = registry.get("recall")
    ok &= check("найден builtin-скилл recall", recall is not None)
    if recall is not None:
        ok &= check(
            "recall filesystem = memory",
            [str(item).lower() for item in recall.permissions.filesystem]
            == ["d:\\kivi\\memory"],
            str(recall.permissions.filesystem),
        )
        recall_handler = load(recall)
        schedule_now = read_note("schedule", "", vault)
        tasks_now = read_note("tasks", "", vault)
        answer = recall_handler(
            FakeCtx(
                {
                    "memory/schedule.md": schedule_now,
                    "memory/tasks.md": tasks_now,
                }
            )
        )
        print("recall:", answer)
        ok &= check(
            "recall отвечает строкой расписания",
            "стендап" in answer.lower() and "09:00" in answer,
        )
        empty_answer = recall_handler(FakeCtx({}))
        print("recall пусто:", empty_answer)
        ok &= check("recall пустой vault -> Пока пусто.", empty_answer == "Пока пусто.")

    core = FakeCore()
    server = ApiServer(port=0, core=core)
    server.start()
    client = None
    try:
        client = ApiClient(f"ws://127.0.0.1:{server.port}", request_timeout=10.0)
        reply = client.request("reflect_now", timeout=10.0)
        print("reflect_now:", reply)
        ok &= check("reflect_now API -> ok", reply.get("ok") is True)
        ok &= check("reflect_now вызвал рефлексию", core.reflector.calls == 1)
    finally:
        if client is not None:
            client.close()
        server.stop()

    print()
    print("PASS" if ok else "FAIL — скопируй весь вывод и пришли в чат")
    return 0 if ok else 1


if __name__ == "__main__":
    raise SystemExit(main())
