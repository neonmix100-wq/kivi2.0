"""Смоук подсистемы скиллов (шаг 1.0). Без микрофона и без LLM.

Запуск:
    python tests/smoke_skills.py

Проверяется: скан манифестов, планировщик, вызов time_date/file_search,
запреты брокера (сеть, .env, базы данных), статистика и автовыход.
"""

from __future__ import annotations

import sqlite3
import sys
from contextlib import closing
from datetime import datetime
from pathlib import Path

PROJECT_ROOT = Path(__file__).resolve().parents[1]
if str(PROJECT_ROOT) not in sys.path:
    sys.path.insert(0, str(PROJECT_ROOT))

from kivi.log import setup_logging
from kivi.skills.broker import PermissionDenied, SkillContext, project_timezone
from kivi.skills.loader import invoke
from kivi.skills.planner import Planner
from kivi.skills.registry import (
    SkillManifest,
    SkillRegistry,
    load_skills_config,
)

TMP = PROJECT_ROOT / "data" / "tmp"
PROBE_ROOT = TMP / "skills_smoke"
PROBE_DIR = PROBE_ROOT / "probe_net"
STATS_DB = TMP / "skills_smoke.db"

PROBE_MANIFEST = """\
name: probe_net
version: "0.1.0"
description: "Временный скилл смоука: сеть не разрешена"
category: system
triggers:
  - тестовый сетевой скилл
intents:
  - проверить запрет сети
requires:
  python: ">=3.12"
  binaries: []
  env: []
  hardware: []
permissions:
  network: []
  filesystem: []
  tools: [http_get]
  risk: safe
entry: "handler.py:handle"
enabled: true
source:
  kind: generated
  repo: ""
  commit: ""
"""

PROBE_HANDLER = '''\
"""Временный скилл для смоука: манифест запрещает сеть."""

from __future__ import annotations


def handle(ctx) -> str:
    ctx.notify("пробую http_get")
    return ctx.http_get("https://example.com", timeout=5)
'''

# Битый манифест: нет category/triggers/permissions/entry — scan обязан пропустить.
BROKEN_MANIFEST = """\
name: broken_skill
version: "0.1.0"
description: "Манифест без обязательных полей"
"""


def check(label: str, condition: bool, detail: str = "") -> bool:
    mark = "OK  " if condition else "X   "
    print(f"{mark} {label}" + (f" — {detail}" if detail else ""))
    return bool(condition)


def prepare_probe() -> None:
    """Разложить временные скиллы в data/tmp/skills_smoke (рабочий и битый)."""
    PROBE_DIR.mkdir(parents=True, exist_ok=True)
    (PROBE_DIR / "skill.yaml").write_text(PROBE_MANIFEST, encoding="utf-8")
    (PROBE_DIR / "handler.py").write_text(PROBE_HANDLER, encoding="utf-8")
    broken_dir = PROBE_ROOT / "broken_skill"
    broken_dir.mkdir(parents=True, exist_ok=True)
    (broken_dir / "skill.yaml").write_text(BROKEN_MANIFEST, encoding="utf-8")


def reset_stats(db_path: Path) -> None:
    """Очистить статистику тестовой БД (файл не удаляем — только строки)."""
    if not db_path.exists():
        return
    with closing(sqlite3.connect(str(db_path))) as connection, connection:
        connection.execute("DELETE FROM skills_stats")


def fs_manifest() -> SkillManifest:
    """Манифест для проверки брокера: весь D:\\Kivi разрешён, .env — нет."""
    return SkillManifest.model_validate(
        {
            "name": "env_probe",
            "version": "0.0.1",
            "description": "Проверка защиты секретов и баз данных",
            "category": "files",
            "triggers": ["проверить защиту путей"],
            "intents": ["прочитать запрещённый файл"],
            "requires": {},
            "permissions": {
                "network": [],
                "filesystem": [str(PROJECT_ROOT)],
                "tools": ["fs_read", "fs_write", "fs_list"],
                "risk": "safe",
            },
            "entry": "handler.py:handle",
            "enabled": True,
            "source": {"kind": "generated"},
        }
    )


def main() -> int:
    setup_logging()
    print("=== SMOKE 1.0: подсистема скиллов (без микрофона) ===")
    TMP.mkdir(parents=True, exist_ok=True)
    prepare_probe()

    registry = SkillRegistry(
        skills_dir=PROJECT_ROOT / "skills",
        installed_dir=PROBE_ROOT,
        db_path=STATS_DB,
    )
    reset_stats(registry.db_path)

    ok = True
    print("\n--- 1) registry.scan ---")
    manifests = registry.scan()
    names = [item.name for item in manifests]
    print(f"    найдено скиллов: {len(manifests)} — {', '.join(names)}")
    ok &= check("time_date найден", "time_date" in names)
    ok &= check("file_search найден", "file_search" in names)
    ok &= check("временный probe_net найден", "probe_net" in names)
    ok &= check("битый манифест пропущен с WARNING", "broken_skill" not in names)

    cfg = load_skills_config()
    ok &= check(
        "config.yaml: skills.call_timeout_s = 30",
        int(cfg.get("call_timeout_s") or 0) == 30,
        str(cfg.get("call_timeout_s")),
    )
    ok &= check(
        "config.yaml: skills.auto_disable_after_fails = 3",
        int(cfg.get("auto_disable_after_fails") or 0) == 3,
        str(cfg.get("auto_disable_after_fails")),
    )

    print("\n--- 2) planner ---")
    planner = Planner(registry, strategy="fuzzy_only")  # смоук без сети и LLM
    found = planner.match("который час сейчас")
    ok &= check(
        "'который час сейчас' -> time_date",
        found is not None and found[0].name == "time_date",
        found[0].name if found is not None else "нет совпадения",
    )
    plan = planner.plan("расскажи анекдот про кота")
    ok &= check(
        "неизвестная команда -> skill=None + missing",
        plan["skill"] is None and bool(plan["missing"]),
        str(plan["missing"]),
    )

    print("\n--- 3) time_date ---")
    answer = invoke(registry.get("time_date"), {}, timeout=10, registry=registry)
    print(f"    ответ: {answer!r}")
    expected_year = str(datetime.now(project_timezone()).year)
    ok &= check("ответ непустой", bool(answer.strip()))
    ok &= check("в ответе сегодняшний год и дата", expected_year in answer and "года" in answer)
    ok &= check("в ответе время ЧЧ:ММ", ":" in answer)

    print("\n--- 4) file_search ---")
    files = invoke(registry.get("file_search"), {"pattern": "*.yaml"}, timeout=20, registry=registry)
    print("    ответ:")
    for line in files.splitlines():
        print(f"      {line}")
    matches = [line for line in files.splitlines() if line.startswith("  ")]
    ok &= check("список непустой", bool(matches), f"{len(matches)} совпадений")
    ok &= check("в списке есть skill.yaml", "skill.yaml" in files)
    ok &= check("не больше 5 совпадений", len(matches) <= 5)
    ok &= check("пути относительные", "D:" not in files)

    print("\n--- 5) брокер: сеть запрещена ---")
    probe = registry.get("probe_net")
    denied_directly = False
    try:
        SkillContext(probe).http_get("https://example.com")
    except PermissionDenied as exc:
        denied_directly = True
        print(f"    PermissionDenied: {exc}")
    ok &= check("http_get без домена в permissions -> PermissionDenied", denied_directly)
    probe_answer = invoke(probe, {}, timeout=10, registry=registry)
    print(f"    invoke вернул: {probe_answer!r}")
    ok &= check(
        "invoke не роняет процесс и объясняет запрет",
        "probe_net" in probe_answer and "example.com" in probe_answer,
    )

    print("\n--- 6) брокер: .env и базы данных защищены явно ---")
    ctx = SkillContext(fs_manifest())
    env_denied = False
    try:
        ctx.fs_read(PROJECT_ROOT / ".env")
    except PermissionDenied as exc:
        env_denied = True
        print(f"    PermissionDenied: {exc}")
    ok &= check("fs_read(.env) -> PermissionDenied при разрешённом D:\\Kivi", env_denied)

    db_denied = False
    try:
        ctx.fs_read(PROJECT_ROOT / "data" / "kivi.db")
    except PermissionDenied as exc:
        db_denied = True
        print(f"    PermissionDenied: {exc}")
    ok &= check("fs_read(data/kivi.db) -> PermissionDenied", db_denied)

    listed = ctx.fs_list(PROJECT_ROOT / "data", recursive=True)
    ok &= check(
        "fs_list(data) не показывает *.db",
        not any(item.lower().endswith(".db") for item in listed),
        f"{len(listed)} файлов",
    )

    allowed_file = PROBE_ROOT / "hello.txt"
    ctx.fs_write(allowed_file, "привет")
    ok &= check("разрешённая папка: запись и чтение работают", ctx.fs_read(allowed_file) == "привет")

    print("\n--- 7) статистика ---")
    stats_time = registry.stats("time_date")
    stats_files = registry.stats("file_search")
    stats_probe = registry.stats("probe_net")
    ok &= check(
        "use_count(time_date) вырос после invoke",
        stats_time is not None and stats_time.use_count >= 1 and stats_time.success_count >= 1,
        str(stats_time),
    )
    ok &= check(
        "use_count(file_search) вырос после invoke",
        stats_files is not None and stats_files.use_count >= 1,
        str(stats_files),
    )
    ok &= check(
        "ошибка probe_net записана",
        stats_probe is not None and stats_probe.fail_count == 1,
        str(stats_probe),
    )

    print("\n--- 8) автовыход после 3 ошибок подряд ---")
    for _ in range(2):
        invoke(probe, {}, timeout=10, registry=registry)
    stats_probe = registry.stats("probe_net")
    ok &= check(
        "fail_count = 3",
        stats_probe is not None and stats_probe.fail_count == 3,
        str(stats_probe),
    )
    ok &= check("should_disable(probe_net) = True", registry.should_disable("probe_net") is True)
    ok &= check("should_disable(time_date) = False", registry.should_disable("time_date") is False)
    blocked = invoke(probe, {}, timeout=10, registry=registry)
    print(f"    вызов отключённого скилла: {blocked!r}")
    ok &= check("отключённый скилл больше не выполняется", "отключён" in blocked)
    ok &= check("planner больше не выбирает probe_net", planner.match("тестовый сетевой скилл") is None)

    print(f"\nнайдено скиллов: {len(manifests)} ({', '.join(names)})")
    print()
    print("PASS" if ok else "FAIL — скопируй весь вывод и пришли в чат")
    return 0 if ok else 1


if __name__ == "__main__":
    raise SystemExit(main())
