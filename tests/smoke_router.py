"""Смоук шага 0.7: бюджет, роутер, эскалация, отсутствие opencode.

Запуск:
    python tests/smoke_router.py

Тратит несколько копеек на реальные вызовы LLM (эскалация — до 6 запросов).
"""

from __future__ import annotations

import sys
import uuid
from datetime import timedelta
from pathlib import Path

PROJECT_ROOT = Path(__file__).resolve().parents[1]
if str(PROJECT_ROOT) not in sys.path:
    sys.path.insert(0, str(PROJECT_ROOT))

from kivi.brain.budget import Budget, BudgetExceeded
from kivi.brain.providers import OpenCodeCLI, ProviderUnavailable
from kivi.brain.router import Router, RouterError
from kivi.config import DATA_DIR, get_settings
from kivi.log import setup_logging

TMP_DIR = DATA_DIR / "tmp"


def check(label: str, condition: bool, detail: str = "") -> bool:
    mark = "OK " if condition else "X  "
    print(f"{mark} {label}" + (f" — {detail}" if detail else ""))
    return bool(condition)


def test_budget_fake() -> bool:
    """Budget на фиктивных записях: порог 80% и исчерпание лимита."""
    print("\n--- 1) Budget на фиктивных записях ---")
    TMP_DIR.mkdir(parents=True, exist_ok=True)
    db_path = TMP_DIR / f"budget_smoke_{uuid.uuid4().hex}.db"
    budget = Budget(db_path=db_path, monthly_limit_rub=0.10, warn_at=0.8)
    ok = True
    ok &= check("новая БД пустая", abs(budget.month_spent_rub()) < 1e-9)
    ok &= check("файл БД создан", db_path.exists())

    budget.record(
        provider="test", model="fake", task_type="chat",
        tokens_in=1000, tokens_out=1000, cost_rub=0.05, ok=True, attempt=1,
    )
    ok &= check(
        "одна запись = 0.05 руб",
        abs(budget.month_spent_rub() - 0.05) < 1e-9,
        f"потрачено {budget.month_spent_rub():.2f}",
    )
    ok &= check(
        "percent_used = 50%",
        abs(budget.percent_used() - 50.0) < 0.01,
        f"{budget.percent_used():.1f}%",
    )
    ok &= check("check() до лимита = True", budget.check() is True)

    budget.record(
        provider="test", model="fake", task_type="chat",
        tokens_in=500, tokens_out=500, cost_rub=0.04, ok=True, attempt=1,
    )
    ok &= check(
        "порог 80% пройден (0.09 руб)",
        budget.percent_used() >= 80.0,
        f"{budget.percent_used():.1f}% — в логе должен быть WARNING",
    )
    ok &= check("check() на 90% = True", budget.check() is True)

    budget.record(
        provider="test", model="fake", task_type="chat",
        tokens_in=500, tokens_out=500, cost_rub=0.02, ok=False, attempt=2,
    )
    ok &= check(
        "лимит исчерпан (0.11 > 0.10)",
        budget.check() is False,
        f"потрачено {budget.month_spent_rub():.2f}",
    )

    month_start = budget.month_start()
    offset = month_start.utcoffset()
    ok &= check(
        "месяц начинается с местной полуночи (UTC+7, Новосибирск)",
        month_start.day == 1
        and month_start.hour == 0
        and month_start.minute == 0
        and offset is not None
        and offset == timedelta(hours=7),
        f"{month_start.isoformat()} (UTC{offset})",
    )

    zero_price = budget.record(
        provider="test", model="no-price-model", task_type="chat",
        tokens_in=100, tokens_out=100, cost_rub=None,
    )
    ok &= check("нет цены -> стоимость 0 (WARNING в лог)", zero_price == 0.0)
    budget.reset_month()
    return bool(ok)


def test_budget_exceeded_blocks_call(settings) -> bool:
    """Исчерпанный лимит обязан остановить вызов ещё ДО сети."""
    print("\n--- 3) исчерпанный бюджет блокирует вызов ---")
    db_path = TMP_DIR / f"budget_smoke_{uuid.uuid4().hex}.db"
    tiny = Budget(db_path=db_path, monthly_limit_rub=0.01, warn_at=0.8)
    tiny.record(provider="test", model="fake", task_type="chat", cost_rub=0.02)
    blocked_router = Router(settings, budget=tiny)
    try:
        blocked_router.complete("Этот запрос не должен уйти в сеть", task_type="chat")
        print("X   вызов прошёл, хотя лимит исчерпан")
        return False
    except BudgetExceeded as exc:
        return check("BudgetExceeded до вызова сети", True, str(exc)[:90])
    except Exception as exc:  # noqa: BLE001
        print(f"X   неожиданная ошибка: {type(exc).__name__}: {exc}")
        return False


def main() -> int:
    setup_logging()
    print("=== SMOKE 0.7: роутер LLM + бюджет ===")
    settings = get_settings()

    ok = test_budget_fake()

    router = Router(settings)
    print(f"\nКонфиг: tiers={router.tiers}")
    print(f"        routing={router.routing}, escalation={router.escalation}")
    print(f"        бюджет: {router.budget.describe()}")

    spent_before = router.budget.month_spent_rub()
    test_cost = 0.0

    # --- 2) обычный chat-запрос ---
    print("\n--- 2) chat: «Скажи: Киви на связи» ---")
    try:
        answer = router.complete("Скажи: Киви на связи", task_type="chat")
    except Exception as exc:  # noqa: BLE001 - смоук должен показать причину
        print(f"X   chat упал: {exc}")
        return 1
    test_cost += float(answer["cost_rub"])
    print(f"    текст: {answer['text']!r}")
    print(
        f"    тир: {answer['tier']}, модель: {answer['model']}, "
        f"попыток: {answer['attempts']}, стоимость: {answer['cost_rub']:.6f} руб"
    )
    ok &= check("chat вернул непустой текст", bool(answer["text"].strip()))
    ok &= check("chat обслужен тиром cheap", answer["tier"] == "cheap")

    # --- 2b) оркестратор (intent) без эскалации ---
    print("\n--- 2b) intent: оркестратор, проверка не пройдена -> без эскалации ---")
    try:
        intent = router.complete("Ответь: Киви", task_type="intent", verify=lambda _r: False)
    except Exception as exc:  # noqa: BLE001
        print(f"X   intent упал: {exc}")
        ok = False
    else:
        test_cost += float(intent["cost_rub"])
        print(
            f"    тир: {intent['tier']}, модель: {intent['model']}, "
            f"попыток: {intent['attempts']}, ok={intent['ok']}"
        )
        ok &= check(
            "intent: одна попытка на cheap, эскалации нет",
            intent["tier"] == "cheap" and intent["attempts"] == 1 and intent["ok"] is False,
        )

    # --- 3) исчерпанный бюджет ---
    ok &= test_budget_exceeded_blocks_call(settings)

    # --- 3) эскалация: verify всегда False ---
    print("\n--- 4) эскалация: verify=lambda r: False (cheap -> coder -> strong) ---")
    escalation_ok = False
    try:
        router.complete("Скажи: Киви на связи", task_type="chat", verify=lambda _r: False)
        print("X   ожидалась ошибка после исчерпания всех тиров, но пришёл ответ")
    except RouterError as exc:
        max_cheap = int(router.escalation.get("max_attempts_cheap", 3))
        max_coder = int(router.escalation.get("max_attempts_coder", 2))
        expected = max_cheap + max_coder + 1
        print(f"    ошибка: {exc}")
        print(f"    последний тир: {exc.tier}, попыток: {exc.attempts} (ожидалось {expected})")
        escalation_ok = exc.tier == "strong" and exc.attempts == expected
    ok &= check("эскалация дошла до тира strong", escalation_ok)

    # --- 4) opencode не в PATH ---
    print("\n--- 5) OpenCodeCLI без бинарника ---")
    cli = OpenCodeCLI()
    if cli.available:
        print("    opencode найден в PATH — проверка недоступности пропущена")
    else:
        try:
            cli.complete_code("Скажи: Киви на связи", TMP_DIR)
            print("X   ожидалась ProviderUnavailable, но вызов прошёл")
            ok = False
        except ProviderUnavailable as exc:
            print(f"    ProviderUnavailable: {str(exc)[:120]}")
            ok &= check("opencode отсутствует -> ProviderUnavailable", True)
        except Exception as exc:  # noqa: BLE001
            print(f"X   неожиданная ошибка: {type(exc).__name__}: {exc}")
            ok = False

    # --- 5b) code без opencode -> эскалация в облако ---
    print("\n--- 5b) code: opencode нет -> эскалация в облако ---")
    try:
        code_answer = router.complete(
            "Ответь одной строкой: print('ok')", task_type="code"
        )
    except Exception as exc:  # noqa: BLE001
        print(f"X   code упал: {exc}")
        ok = False
    else:
        test_cost += float(code_answer["cost_rub"])
        print(
            f"    тир: {code_answer['tier']}, попыток: {code_answer['attempts']}, "
            f"текст: {code_answer['text']!r}"
        )
        ok &= check(
            "code ушёл в облако после недоступного opencode",
            code_answer["tier"] == "cheap" and code_answer["attempts"] >= 2,
        )

    # --- 6) деньги ---
    spent_after = router.budget.month_spent_rub()
    print("\n--- 6) Затраты ---")
    print(f"    расход теста по ответам роутера: {test_cost:.4f} руб")
    print(f"    дельта по БД (data/kivi.db):     {spent_after - spent_before:.4f} руб")
    print(
        f"    всего за месяц:                  {spent_after:.4f} руб "
        f"({router.budget.percent_used():.2f}% от {router.budget.monthly_limit_rub:.0f})"
    )
    ok &= check(
        "расходы записаны в БД",
        (spent_after - spent_before) > 0 or test_cost > 0,
    )

    print()
    print("PASS" if ok else "FAIL — скопируй весь вывод и пришли в чат")
    return 0 if ok else 1


if __name__ == "__main__":
    raise SystemExit(main())
