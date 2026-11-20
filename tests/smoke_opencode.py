"""Смоук шага 1.5: интеграция OpenCode CLI (opencode 1.19.6) в роутер.

Запуск:
    python tests/smoke_opencode.py          # mock: subprocess.run подменён, 0 руб
    python tests/smoke_opencode.py --live   # настоящий `opencode run` (free-модель)

Mock проверяет:
  1) команда ровно ["opencode", "run", prompt], без флага -m, env не переопределяется;
  2) cwd — новая папка <uuid8> внутри data/opencode_jobs, timeout=600, text=True;
  3) чистка stdout: строки "> " и "$ " отброшены, пустые края убраны;
  4) returncode != 0, таймаут и пустой ответ -> ProviderUnavailable.

--live проверяет:
  5) в ответе есть "def fib", в data/opencode_jobs появилась папка задачи;
  6) в usage (data/kivi.db) запись provider=opencode_cli с cost_rub=0, печатает время.

Модель берётся из ~/.config/opencode/opencode.json (флаг -m не передаётся).
Если opencode в --live сломается, роутер по своей логике уйдёт в облако —
тогда тест покажет тир cheap и ненулевой расход и всё равно упадёт.
"""

from __future__ import annotations

import argparse
import sqlite3
import subprocess
import sys
import time
from contextlib import closing
from pathlib import Path
from unittest import mock

PROJECT_ROOT = Path(__file__).resolve().parents[1]
if str(PROJECT_ROOT) not in sys.path:
    sys.path.insert(0, str(PROJECT_ROOT))

import kivi.brain.providers as providers_mod
from kivi.brain.providers import (
    OpenCodeCLI,
    ProviderUnavailable,
    clean_opencode_stdout,
)
from kivi.config import PROJECT_ROOT as KIVI_ROOT
from kivi.config import load_config
from kivi.log import setup_logging

PROMPT = "напиши функцию fib(n) на python, только код"

# Так выглядит вывод CLI вперемешку со служебными строками (в 1.19.6 служебные
# строки идут в stderr, но чистка нужна на случай, если версия их смешает).
RAW_STDOUT = (
    "> opencode 1.19.6\n"
    "\n"
    '$ opencode run "..."\n'
    "def fib(n):\n"
    "    return n if n < 2 else fib(n - 1) + fib(n - 2)\n"
    "\n"
    "> build · deepseek-v4.1-flash-free\n"
    "\n"
)
EXPECTED_TEXT = (
    "def fib(n):\n"
    "    return n if n < 2 else fib(n - 1) + fib(n - 2)"
)


def check(label: str, condition: bool, detail: str = "") -> bool:
    mark = "OK " if condition else "X  "
    print(f"{mark} {label}" + (f" — {detail}" if detail else ""))
    return bool(condition)


def jobs_root() -> Path:
    """Папка задач из brain.opencode_workdir (как её видит роутер)."""
    brain = load_config().model_dump().get("brain") or {}
    raw = str(brain.get("opencode_workdir") or "").strip()
    path = Path(raw) if raw else Path("data/opencode_jobs")
    return path if path.is_absolute() else KIVI_ROOT / path


def is_job_dir(path: Path) -> bool:
    """Папка задачи: 8 hex-символов (uuid4().hex[:8])."""
    name = path.name
    return len(name) == 8 and all(char in "0123456789abcdef" for char in name)


def expect_unavailable(action, label: str) -> bool:
    """Действие обязано упасть именно в ProviderUnavailable."""
    try:
        action()
    except ProviderUnavailable as exc:
        return check(label, True, str(exc)[:90])
    except Exception as exc:  # noqa: BLE001 - смоук должен показать причину
        return check(label, False, f"неожиданно {type(exc).__name__}: {exc}")
    return check(label, False, "ожидалась ProviderUnavailable")


def run_mock() -> bool:
    """subprocess.run подменён: проверяем контракт вызова и чистку вывода."""
    ok = True
    root = jobs_root()
    print(f"Папка задач (brain.opencode_workdir): {root}")

    print("\n--- 1) чистка stdout ---")
    cleaned = clean_opencode_stdout(RAW_STDOUT)
    print(f"    вход {len(RAW_STDOUT)} симв. -> выход {len(cleaned)} симв.")
    ok &= check('строки "> " и "$ " отброшены', cleaned == EXPECTED_TEXT, repr(cleaned))
    ok &= check("пустые края убраны", cleaned == cleaned.strip())

    print("\n--- 2) команда, cwd, timeout (subprocess.run подменён) ---")
    seen: dict = {}

    def fake_run(command, **kwargs):
        seen["command"] = list(command)
        seen["kwargs"] = dict(kwargs)
        return subprocess.CompletedProcess(command, 0, RAW_STDOUT, stderr="")

    cli = OpenCodeCLI()
    before = {item.name for item in root.iterdir()} if root.exists() else set()
    with mock.patch.object(providers_mod.subprocess, "run", fake_run):
        result = cli.complete_code(PROMPT)

    command = seen.get("command", [])
    kwargs = seen.get("kwargs", {})
    cwd = Path(str(kwargs.get("cwd", "")))
    print(f"    команда: {command}")
    print(f"    cwd: {cwd}")
    print(f"    timeout: {kwargs.get('timeout')} c")
    ok &= check(
        "команда = [opencode, run, prompt]", command == ["opencode", "run", PROMPT]
    )
    ok &= check("флаг -m не передаётся", "-m" not in command)
    ok &= check("env не переопределяется", "env" not in kwargs)
    ok &= check(
        "cwd внутри data/opencode_jobs",
        cwd.parent == root and is_job_dir(cwd),
        f"родитель {cwd.parent}",
    )
    ok &= check("папка задачи создана на диске", cwd.is_dir())
    ok &= check("timeout = 600 c", kwargs.get("timeout") == 600.0)
    ok &= check(
        "capture_output и text включены",
        kwargs.get("capture_output") is True and kwargs.get("text") is True,
    )
    ok &= check("ответ очищен и непуст", result["text"] == EXPECTED_TEXT)
    ok &= check("cost_rub = 0", result["cost_rub"] == 0.0)
    ok &= check(
        "raw.workdir совпадает с cwd",
        str(result["raw"]["workdir"]) == str(cwd),
        str(result["raw"]["workdir"]),
    )

    print("\n--- 3) каждая задача — в своей папке ---")
    second: dict = {}

    def fake_run_2(command, **kwargs2):
        second["cwd"] = kwargs2.get("cwd")
        return subprocess.CompletedProcess(command, 0, "print('ok')\n", stderr="")

    with mock.patch.object(providers_mod.subprocess, "run", fake_run_2):
        cli.complete_code("Ответь одной строкой: print('ok')")
    second_cwd = Path(str(second.get("cwd", "")))
    after = {item.name for item in root.iterdir()}
    print(f"    вторая папка: {second_cwd.name}")
    ok &= check("папки задач разные", second_cwd != cwd)
    ok &= check(
        "вторая папка тоже внутри jobs root",
        second_cwd.parent == root and is_job_dir(second_cwd),
    )
    ok &= check(
        "обе папки появились на диске",
        after - before >= {cwd.name, second_cwd.name},
    )

    print("\n--- 4) ошибки -> ProviderUnavailable ---")
    try:
        cli.complete_code("   ")
        ok &= check("пустой промпт отклоняется (ProviderError)", False)
    except providers_mod.ProviderError:
        ok &= check("пустой промпт отклоняется (ProviderError)", True)
    except Exception as exc:  # noqa: BLE001
        ok &= check(
            "пустой промпт отклоняется (ProviderError)",
            False,
            f"{type(exc).__name__}: {exc}",
        )

    def run_with(rc: int, stdout: str = "", stderr: str = ""):
        def fake(command, **kwargs_run):
            return subprocess.CompletedProcess(command, rc, stdout, stderr=stderr)

        return fake

    with mock.patch.object(
        providers_mod.subprocess, "run", run_with(7, stderr="boom: model not found")
    ):
        ok &= expect_unavailable(
            lambda: cli.complete_code(PROMPT), "returncode 7 -> ProviderUnavailable"
        )

    def fake_timeout(command, **kwargs_timeout):
        raise subprocess.TimeoutExpired(command, kwargs_timeout.get("timeout", 600))

    with mock.patch.object(providers_mod.subprocess, "run", fake_timeout):
        ok &= expect_unavailable(
            lambda: cli.complete_code(PROMPT), "таймаут -> ProviderUnavailable"
        )

    with mock.patch.object(
        providers_mod.subprocess,
        "run",
        run_with(0, stdout="> only service\n$ echo\n\n"),
    ):
        ok &= expect_unavailable(
            lambda: cli.complete_code(PROMPT), "пусто после чистки -> ProviderUnavailable"
        )

    return bool(ok)


def usage_rows(db_path: Path) -> tuple[int, tuple | None]:
    """(сколько записей opencode_cli, последняя запись) в таблице usage."""
    if not db_path.exists():
        return 0, None
    with closing(sqlite3.connect(str(db_path))) as connection:
        total = connection.execute(
            "SELECT COUNT(*) FROM usage WHERE provider = 'opencode_cli'"
        ).fetchone()[0]
        last = connection.execute(
            "SELECT provider, model, task_type, cost_rub, ok FROM usage "
            "WHERE provider = 'opencode_cli' ORDER BY id DESC LIMIT 1"
        ).fetchone()
    return int(total), last


def run_live() -> bool:
    """Настоящий `opencode run` через роутер: ответ, папка задачи и запись в usage."""
    from kivi.brain.budget import Budget
    from kivi.brain.router import Router
    from kivi.config import get_settings

    ok = True
    cli = OpenCodeCLI()
    print(f"opencode в PATH: {'да' if cli.available else 'НЕТ'}")
    print(f"папка задач: {cli.jobs_root}")
    if not cli.available:
        return check("opencode найден", False, "установи opencode 1.19.6 и повтори")

    settings = get_settings()
    budget = Budget(settings)
    router = Router(settings, budget=budget, cli=cli)
    db_path = Path(budget.db_path)

    root = cli.jobs_root
    before = (
        {item.name for item in root.iterdir() if item.is_dir()}
        if root.exists()
        else set()
    )
    rows_before, _ = usage_rows(db_path)

    print(f"\n--- 5) живой вызов: {PROMPT!r}")
    started = time.perf_counter()
    try:
        answer = router.complete(PROMPT, task_type="code")
    except Exception as exc:  # noqa: BLE001 - смоук должен показать причину
        print(f"X   роутер упал: {type(exc).__name__}: {exc}")
        return False
    elapsed = time.perf_counter() - started

    text = str(answer.get("text") or "")
    now_dirs = (
        {item.name for item in root.iterdir() if item.is_dir()} if root.exists() else set()
    )
    new_dirs = sorted(now_dirs - before)
    rows_after, last_row = usage_rows(db_path)

    print(f"    длительность: {elapsed:.1f} c")
    print(
        f"    провайдер/тир: {answer.get('provider')} / {answer.get('tier')}, "
        f"cost_rub: {answer.get('cost_rub')}"
    )
    print(f"    новая папка задачи: {new_dirs or '—'}")
    print(f"    ответ ({len(text)} симв.):")
    for line in text.splitlines()[:12]:
        print(f"      {line}")
    print(
        f"    usage: записей opencode_cli {rows_before} -> {rows_after}, "
        f"последняя: {last_row}"
    )

    ok &= check('в ответе есть "def fib"', "def fib" in text)
    ok &= check(
        "ответ пришёл от opencode_cli",
        answer.get("provider") == "opencode_cli"
        and answer.get("tier") == "opencode_cli",
    )
    ok &= check(
        "стоимость ответа = 0 руб", float(answer.get("cost_rub") or 0.0) == 0.0
    )
    ok &= check(
        "в data/opencode_jobs появилась папка задачи",
        bool(new_dirs),
        ", ".join(new_dirs),
    )
    ok &= check(
        "в usage добавилась запись opencode_cli",
        rows_after == rows_before + 1,
        f"{rows_before} -> {rows_after}",
    )
    ok &= check(
        "запись в usage с cost_rub = 0",
        last_row is not None and float(last_row[3]) == 0.0,
        str(last_row),
    )
    return bool(ok)


def main() -> int:
    parser = argparse.ArgumentParser(description="Смоук интеграции OpenCode CLI")
    parser.add_argument(
        "--live",
        action="store_true",
        help="настоящий вызов opencode (free-модель, 0 руб)",
    )
    args = parser.parse_args()

    setup_logging()
    mode = "live" if args.live else "mock"
    print(f"=== SMOKE 1.5: OpenCode CLI в роутере (режим: {mode}) ===")
    ok = run_live() if args.live else run_mock()
    print()
    print(
        "PASS — OpenCode CLI интегрирован"
        if ok
        else "FAIL — см. отметки X выше"
    )
    return 0 if ok else 1


if __name__ == "__main__":
    raise SystemExit(main())

