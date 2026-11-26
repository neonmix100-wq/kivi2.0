"""Смоук канонического расписания: парсер, remember, recall, миграция, UI API.

Запуск:
    python tests/smoke_schedule_canon.py

Проверяется:
  1) expand_days: ВС-ПТ с переходом через неделю, будни, выходные, ежедневно;
  2) remember «с воскресенья по пятницу … с 19:00 до 23:00» -> канон + полное
     подтверждение;
  3) recall «что у меня завтра» (мок сегодня=чт) — работа 19:00–23:00;
     «что в субботу» — работы нет, субботнее есть;
  4) миграция свободной строки без потери текста; непарсируемое -> «[?] »;
  5) «[?]» видно в общем запросе и не видно в датированном;
  6) API add_schedule нормализует (и отвергает мусор);
  7) различаются «vault пуст» и «на дату ничего нет».
"""

from __future__ import annotations

import sys
import uuid
from datetime import datetime
from pathlib import Path
from typing import Any

PROJECT_ROOT = Path(__file__).resolve().parents[1]
if str(PROJECT_ROOT) not in sys.path:
    sys.path.insert(0, str(PROJECT_ROOT))

from kivi.api.server import ApiServer
from kivi.log import setup_logging
from kivi.memory.schedule import (
    expand_days,
    format_days,
    migrate_legacy,
    migrate_schedule_file,
    parse_rule,
)
from kivi.memory.vault import (
    add_schedule,
    add_schedule_line,
    add_task,
    iter_schedule,
    read_note,
    write_note,
)
from kivi.skills.broker import project_timezone
from kivi.skills.loader import load
from kivi.skills.registry import SkillRegistry

TMP = PROJECT_ROOT / "data" / "tmp"
NOW = datetime(2026, 11, 26, 12, 0, tzinfo=project_timezone())


def check(label: str, condition: bool, detail: str = "") -> bool:
    print(("OK  " if condition else "X   ") + label + (f" — {detail}" if detail else ""))
    return bool(condition)


class LogCatcher:
    def __init__(self) -> None:
        self.messages: list[str] = []
        self._sink = None

    def _write(self, message: Any) -> None:
        self.messages.append(str(message).strip())

    def __enter__(self) -> "LogCatcher":
        from loguru import logger

        self._logger = logger
        self._sink = logger.add(self._write, level="INFO")
        return self

    def __exit__(self, *exc_info: Any) -> bool:
        if self._sink is not None:
            self._logger.remove(self._sink)
            self._sink = None
        return False

    def has(self, needle: str) -> bool:
        return any(needle in line for line in self.messages)


class VaultCtx:
    """Контекст remember/recall поверх временного vault."""

    def __init__(
        self,
        vault: Path,
        *,
        entities: dict[str, Any] | None = None,
        reminders: list[dict[str, Any]] | None = None,
    ) -> None:
        self.vault = vault
        self.entities = entities or {"keywords": [], "date_refs": []}
        self._reminders = reminders or []
        self.logs: list[str] = []
        self.extract_calls: list[str] = []

    def vault_add_schedule_line(self, line: str) -> bool:
        return add_schedule_line(line, path=self.vault)

    def vault_add_schedule(self, when: str, what: str) -> bool:
        return add_schedule(when, what, path=self.vault)

    def vault_add_task(self, what: str) -> bool:
        return add_task(what, "open", path=self.vault)

    def vault_add_preference(self, what: str) -> bool:
        return False

    def vault_add_fact(self, what: str) -> bool:
        return False

    def fs_read(self, path: str) -> str:
        return read_note(Path(path).name.split(".")[0], "", self.vault)

    def reminders_list(self) -> list[dict[str, Any]]:
        return list(self._reminders)

    def extract_keywords(self, command: str) -> dict[str, Any]:
        self.extract_calls.append(command)
        return dict(self.entities)

    def now(self) -> datetime:
        return NOW

    def log(self, text: str) -> None:
        self.logs.append(str(text))


def load_handler(registry: SkillRegistry, name: str):
    manifest = registry.get(name)
    if manifest is None:
        raise RuntimeError(f"скилл {name} не найден")
    return load(manifest)


def main() -> int:
    setup_logging()
    print("=== SMOKE: каноническое расписание ===")
    TMP.mkdir(parents=True, exist_ok=True)
    token = uuid.uuid4().hex
    vault = TMP / f"schedule_canon_{token}"
    ok = True

    print("\n--- 1) expand_days / канон дней ---")
    work_days = expand_days("ВС-ПТ")
    ok &= check(
        "ВС-ПТ = вс,пн,вт,ср,чт,пт",
        work_days == {6, 0, 1, 2, 3, 4},
        str(sorted(work_days)),
    )
    ok &= check("будни = ПН-ПТ", expand_days("будни") == {0, 1, 2, 3, 4})
    ok &= check("выходные = СБ,ВС", expand_days("выходные") == {5, 6})
    ok &= check("ежедневно = ПН-ВС", expand_days("ежедневно") == set(range(7)))
    ok &= check("wrap ПТ-ПН = пт,сб,вс,пн", expand_days("ПТ-ПН") == {4, 5, 6, 0})
    ok &= check("format_days(ВС-ПТ) = ВС-ПТ", format_days(work_days) == "ВС-ПТ")

    registry = SkillRegistry(
        skills_dir=PROJECT_ROOT / "skills",
        installed_dir=TMP / f"schedule_canon_installed_{token}",
        db_path=TMP / f"schedule_canon_skills_{token}.db",
    )
    registry.scan()
    remember = load_handler(registry, "remember")
    recall = load_handler(registry, "recall")

    print("\n--- 2) remember: свободная фраза -> канон + полное подтверждение ---")
    ctx = VaultCtx(vault)
    command = "с воскресенья по пятницу работа над проектом с 19:00 до 23:00"
    reply = remember(
        ctx,
        kind="schedule",
        what="работа над проектом",
        when="с воскресенья по пятницу с 19:00 до 23:00",
        command=command,
    )
    schedule_text = read_note("schedule", "", vault)
    print("remember:", reply)
    print("schedule.md:\n" + schedule_text.strip())
    ok &= check(
        "канон в файле",
        "- [ВС-ПТ 19:00-23:00] работа над проектом" in schedule_text,
        schedule_text.strip(),
    )
    ok &= check(
        "подтверждение: текст + вс–пт + 19:00–23:00",
        "Запомнила" in reply
        and "работа над проектом" in reply
        and "вс–пт" in reply
        and "19:00–23:00" in reply,
        reply,
    )
    ok &= check(
        "parse_rule видит дни и оба времени",
        (lambda rule: bool(
            rule and rule.days == {6, 0, 1, 2, 3, 4} and rule.start == 19 * 60
            and rule.end == 23 * 60
        ))(parse_rule("- [ВС-ПТ 19:00-23:00] работа над проектом")),
    )

    add_schedule_line("- [СБ 10:00-11:00] уборка", path=vault)
    add_schedule_line("- [?] странная запись про гараж", path=vault)
    add_task("купить корм", "open", path=vault)

    print("\n--- 3) recall: завтра / суббота ---")
    tomorrow = recall(
        VaultCtx(vault, entities={"keywords": [], "date_refs": ["tomorrow"]}),
        command="что у меня завтра",
    )
    print("завтра:", tomorrow)
    ok &= check(
        "завтра (пт): работа 19:00–23:00",
        "Завтра (пт)" in tomorrow
        and "работа над проектом 19:00–23:00" in tomorrow,
        tomorrow,
    )
    ok &= check("завтра без уборки", "уборка" not in tomorrow, tomorrow)
    ok &= check("завтра без «[?]»", "странная запись" not in tomorrow, tomorrow)

    saturday = recall(
        VaultCtx(vault, entities={"keywords": [], "date_refs": ["суббота"]}),
        command="что в субботу",
    )
    print("суббота:", saturday)
    ok &= check(
        "суббота: уборка есть, работы нет",
        "уборка 10:00–11:00" in saturday and "работа над проектом" not in saturday,
        saturday,
    )

    print("\n--- 4) общий запрос: «[?]» и открытые задачи отдельным блоком ---")
    general = recall(
        VaultCtx(vault, entities={"keywords": [], "date_refs": []}),
        command="что у меня в расписании",
    )
    print("общий:", general)
    ok &= check("общий видит «[?]»", "странная запись" in general, general)
    ok &= check("общий: открытые задачи блоком", "Открытых задач: 1" in general, general)

    only_vault = TMP / f"schedule_only_{token}"
    add_schedule_line("- [ВС-ПТ 19:00-23:00] работа над проектом", path=only_vault)
    empty_day = recall(
        VaultCtx(only_vault, entities={"keywords": [], "date_refs": ["26.12"]}),
        command="что 26.12",
    )
    print("25.12:", empty_day)
    ok &= check(
        "на дату ничего нет, но записи есть",
        "ничего нет" in empty_day and "Пока пусто" not in empty_day,
        empty_day,
    )
    empty_vault_query = recall(
        VaultCtx(TMP / f"schedule_empty_{token}"),
        command="что у меня в расписании",
    )
    print("пустой vault:", empty_vault_query)
    ok &= check(
        "пустой vault -> Пока пусто",
        empty_vault_query == "Пока пусто.",
        empty_vault_query,
    )


    print("\n--- 5) миграция legacy без потери текста ---")
    legacy_vault = TMP / f"schedule_legacy_{token}"
    write_note(
        "schedule",
        "",
        "# Расписание\n"
        "- [с воскресенья по пятницу с 19:00 до 23:00] работа над проектом\n"
        "- завтра в 19:00 позвонить маме\n",
        legacy_vault,
    )
    migrated_inline = migrate_legacy(
        "- [с воскресенья по пятницу с 19:00 до 23:00] работа над проектом"
    )
    ok &= check(
        "migrate_legacy -> канон",
        migrated_inline == "- [ВС-ПТ 19:00-23:00] работа над проектом",
        str(migrated_inline),
    )
    with LogCatcher() as logs:
        changed = migrate_schedule_file(legacy_vault / "schedule.md")
    legacy_text = read_note("schedule", "", legacy_vault)
    print(f"мигрировано={changed}, файл:\n" + legacy_text.strip())
    ok &= check("миграция переписала 2 строки", changed == 2, str(changed))
    ok &= check("лог «schedule: мигрировано N»", logs.has("schedule: мигрировано 2"))
    ok &= check(
        "канон после миграции",
        "- [ВС-ПТ 19:00-23:00] работа над проектом" in legacy_text,
        legacy_text.strip(),
    )
    ok &= check(
        "непарсируемая строка не потеряна",
        "- [?]" in legacy_text and "завтра в 19:00 позвонить маме" in legacy_text,
        legacy_text.strip(),
    )
    ok &= check(
        "iter_schedule отдаёт канон",
        any(when == "ВС-ПТ 19:00-23:00" for when, _what in iter_schedule(legacy_vault)),
    )

    print("\n--- 6) remember: непарсируемое -> «[?]» без потери текста ---")
    raw_vault = TMP / f"schedule_raw_{token}"
    raw_ctx = VaultCtx(raw_vault)
    raw_reply = remember(
        raw_ctx,
        kind="schedule",
        what="позвонить маме",
        when="завтра в 19:00",
        command="запомни завтра в 19:00 позвонить маме",
    )
    raw_text = read_note("schedule", "", raw_vault)
    print("ответ:", raw_reply)
    print("файл:\n" + raw_text.strip())
    ok &= check(
        "«[?]» сохранён с текстом",
        "- [?]" in raw_text and "завтра в 19:00" in raw_text and "позвонить маме" in raw_text,
        raw_text.strip(),
    )
    raw_general = recall(
        VaultCtx(raw_vault, entities={"keywords": [], "date_refs": []}),
        command="что у меня в расписании",
    )
    raw_dated = recall(
        VaultCtx(raw_vault, entities={"keywords": [], "date_refs": ["завтра"]}),
        command="что завтра",
    )
    print("общий:", raw_general, "| датированный:", raw_dated)
    ok &= check("«[?]» видно в общем", "позвонить маме" in raw_general, raw_general)
    ok &= check(
        "«[?]» не видно в датированном",
        "позвонить маме" not in raw_dated,
        raw_dated,
    )

    print("\n--- 7) API add_schedule: нормализация и валидация ---")
    import kivi.memory.vault as vault_module

    server = ApiServer(config={})
    api_vault = TMP / f"schedule_api_{token}"
    original_root = vault_module.vault_root
    vault_module.vault_root = lambda path=None: (  # type: ignore[assignment]
        Path(path) if path is not None else api_vault
    )
    try:
        api_ok = server.add_schedule_event("будни", "19:00", "23:00", "работа")
        api_bad = server.add_schedule_event("ПН-ПТ", "25:99", "", "ошибка")
    finally:
        vault_module.vault_root = original_root  # type: ignore[assignment]
    print("api ok:", api_ok)
    print("api bad:", api_bad)
    ok &= check(
        "API нормализовал в канон",
        api_ok.get("ok") is True
        and api_ok.get("line") == "- [ПН-ПТ 19:00-23:00] работа"
        and "- [ПН-ПТ 19:00-23:00] работа" in (api_ok.get("lines") or []),
        str(api_ok),
    )
    ok &= check(
        "API отверг мусорное время",
        api_bad.get("ok") is False and bool(api_bad.get("error")),
        str(api_bad),
    )
    ok &= check(
        "API-файл содержит канон",
        "- [ПН-ПТ 19:00-23:00] работа" in read_note("schedule", "", api_vault),
        read_note("schedule", "", api_vault).strip(),
    )

    print()
    print("PASS" if ok else "FAIL — скопируй весь вывод и пришли в чат")
    return 0 if ok else 1


if __name__ == "__main__":
    raise SystemExit(main())

