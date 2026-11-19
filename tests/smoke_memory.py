"""Смоук долгой памяти: reflect -> vault, retrieve, inject.

Запуск:
    python tests/smoke_memory.py
"""

from __future__ import annotations

import sys
import time
import uuid
from pathlib import Path

PROJECT_ROOT = Path(__file__).resolve().parents[1]
if str(PROJECT_ROOT) not in sys.path:
    sys.path.insert(0, str(PROJECT_ROOT))

from kivi.brain.router import Router
from kivi.config import get_settings
from kivi.log import setup_logging
from kivi.memory.inject import build_memory_block
from kivi.memory.reflect import Reflector
from kivi.memory.retrieve import MemoryRetriever
from kivi.memory.vault import iter_facts, read_note

TMP = PROJECT_ROOT / "data" / "tmp"
DIALOG = [
    {"role": "user", "content": "Киви, запомни: у меня день рождения 5 марта."},
    {"role": "assistant", "content": "Запомнила, Лёша: день рождения 5 марта."},
    {"role": "user", "content": "Ещё я люблю кофе без сахара."},
    {"role": "assistant", "content": "Приняла: кофе без сахара."},
]


def main() -> int:
    setup_logging()
    print("=== SMOKE: долгая память (vault + reflect + retrieve) ===")
    vault = TMP / f"memory_smoke_{uuid.uuid4().hex}"
    metadata = TMP / f"memory_index_{uuid.uuid4().hex}.db"
    print(f"vault: {vault}")

    settings = get_settings()
    router = Router(settings)
    ok = True

    spent_before = router.budget.month_spent_rub()
    reflector = Reflector(router, vault_path=vault)
    started = time.perf_counter()
    stats = reflector.run(DIALOG, force=True)
    elapsed = time.perf_counter() - started
    print(f"reflect: {stats} за {elapsed:.2f} c")

    spent_after = router.budget.month_spent_rub()
    cost = spent_after - spent_before
    print(f"стоимость прохода: {cost:.5f} руб (лимит 0.05)")

    if stats.get("skipped"):
        ok = False
        print(f"X   reflect пропущен: {stats['skipped']}")

    profile = read_note("profile", "", vault)
    print("\n--- memory/profile.md ---")
    print(profile.strip() or "(пусто)")
    has_fact = "5 марта" in profile
    print(("OK  " if has_fact else "X   ") + "в profile.md есть «день рождения 5 марта»")
    ok &= has_fact

    facts_all = [fact for _note, _day, fact in iter_facts(vault)]
    print(f"\nвсего фактов в vault: {len(facts_all)}")
    for fact in facts_all:
        print("  -", fact)

    retriever = MemoryRetriever(db_path=metadata, vault_path=vault, k=4)
    found = retriever.retrieve("когда у меня день рождения")
    print(f"\nretrieve(\"когда у меня день рождения\") [{retriever.last_path}]:")
    for fact in found:
        print("  -", fact)
    retrieved_ok = any(
        "день рождения" in fact.lower() and "5 марта" in fact.lower() for fact in found
    )
    print(("OK  " if retrieved_ok else "X   ") + "поиск вернул факт про 5 марта")
    ok &= retrieved_ok

    block = build_memory_block("когда у меня день рождения", vault_path=vault, k=4)
    print("\n--- блок памяти ---")
    print(block or "(пустой)")
    print(("OK  " if block.strip() else "X   ") + "блок памяти непустой")
    ok &= bool(block.strip())

    empty_block = build_memory_block("что-нибудь", vault_path=TMP / "no_such_vault")
    print(("OK  " if empty_block == "" else "X   ") + "пустой vault -> пустой блок")
    ok &= empty_block == ""

    print(("\nOK  " if cost < 0.05 else "X   ") + f"стоимость {cost:.5f} руб < 0.05")
    ok &= cost < 0.05

    print()
    print("PASS" if ok else "FAIL — скопируй весь вывод и пришли в чат")
    return 0 if ok else 1


if __name__ == "__main__":
    raise SystemExit(main())
