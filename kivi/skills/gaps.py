"""Журнал пробелов в умениях Киви: таблица capability_gaps в data/kivi.db.

Зачем: когда планировщик не нашёл умение под команду, важно запомнить,
ЧТО именно хотел пользователь (намерение), а не только дословную фразу.
По этим записям закрываем пробелы в порядке частоты.

Повторная просьба с той же нормализованной командой не плодит строки:
у открытого пробела растёт attempts_to_close (сколько раз просили).

Использование:
    from kivi.skills.gaps import get_gap_log
    gaps = get_gap_log()
    gap_id = gaps.record_gap("расскажи анекдот", user_intent="анекдоты",
                             why="нет скилла с шутками")
    gaps.stats_by_intent()                  # топ-10 по частоте запросов
    gaps.close(gap_id, how="skill: jokes")  # умение появилось — закрыть
"""

from __future__ import annotations

import sqlite3
import time
from contextlib import closing
from pathlib import Path
from typing import Any

from loguru import logger

from kivi.config import DATA_DIR
from kivi.skills.registry import normalize_text

__all__ = [
    "DEFAULT_GAPS_DB_PATH",
    "GapLog",
    "get_gap_log",
]

DEFAULT_GAPS_DB_PATH = DATA_DIR / "kivi.db"

SCHEMA = """
CREATE TABLE IF NOT EXISTS capability_gaps (
    id                INTEGER PRIMARY KEY AUTOINCREMENT,
    ts                REAL    NOT NULL,            -- когда просили (epoch, UTC)
    command           TEXT    NOT NULL,            -- нормализованная команда
    user_intent       TEXT    NOT NULL,            -- что хотел пользователь
    skill_attempted   TEXT,                        -- какой скилл пробовали
    why               TEXT    NOT NULL DEFAULT '', -- короткое объяснение
    attempts_to_close INTEGER NOT NULL DEFAULT 1,  -- сколько раз просили
    closed_at         REAL,                        -- когда умение появилось
    closed_how        TEXT,                        -- чем закрыли (skill: ...)
    notes             TEXT    NOT NULL DEFAULT ''
);
CREATE INDEX IF NOT EXISTS idx_gaps_ts ON capability_gaps(ts);
CREATE INDEX IF NOT EXISTS idx_gaps_intent ON capability_gaps(user_intent);
"""


class GapLog:
    """Запись и статистика пробелов. Живёт рядом с usage в data/kivi.db."""

    def __init__(self, db_path: str | Path | None = None) -> None:
        self.db_path = Path(db_path) if db_path is not None else DEFAULT_GAPS_DB_PATH
        self._ensure_schema()

    # --- соединение ---

    def _connect(self) -> sqlite3.Connection:
        self.db_path.parent.mkdir(parents=True, exist_ok=True)
        connection = sqlite3.connect(str(self.db_path), timeout=5.0)
        connection.row_factory = sqlite3.Row
        return connection

    def _ensure_schema(self) -> None:
        with closing(self._connect()) as connection, connection:
            connection.executescript(SCHEMA)

    # --- запись ---

    def record_gap(
        self,
        command: str,
        user_intent: str,
        *,
        skill_attempted: str | None = None,
        why: str = "",
        notes: str = "",
    ) -> int:
        """Записать пробел. Повтор той же просьбы увеличивает attempts_to_close."""
        normalized = normalize_text(command) or " ".join(str(command or "").split())
        intent = " ".join(str(user_intent or "").split()) or normalized
        reason = " ".join(str(why or "").split())
        comment = " ".join(str(notes or "").split())
        attempted = None if skill_attempted is None else str(skill_attempted)
        now = time.time()

        with closing(self._connect()) as connection, connection:
            row = connection.execute(
                "SELECT id FROM capability_gaps "
                "WHERE closed_at IS NULL AND command = ? AND user_intent = ? "
                "ORDER BY id DESC LIMIT 1",
                (normalized, intent),
            ).fetchone()
            if row is not None:
                gap_id = int(row["id"])
                connection.execute(
                    "UPDATE capability_gaps SET ts = ?, "
                    "attempts_to_close = attempts_to_close + 1, "
                    "why = COALESCE(NULLIF(?, ''), why), "
                    "skill_attempted = COALESCE(?, skill_attempted) "
                    "WHERE id = ?",
                    (now, reason, attempted, gap_id),
                )
                return gap_id

            cursor = connection.execute(
                "INSERT INTO capability_gaps "
                "(ts, command, user_intent, skill_attempted, why, "
                " attempts_to_close, notes) "
                "VALUES (?, ?, ?, ?, ?, 1, ?)",
                (now, normalized, intent, attempted, reason, comment),
            )
            gap_id = int(cursor.lastrowid or 0)

        logger.info(
            "пробел в умениях: {} (хочет: {!r})",
            normalized[:60],
            intent[:60],
        )
        return gap_id

    # --- чтение ---

    def list_recent(self, limit: int = 20) -> list[dict[str, Any]]:
        """Последние пробелы, новые сверху."""
        count = max(1, int(limit))
        with closing(self._connect()) as connection:
            rows = connection.execute(
                "SELECT id, ts, command, user_intent, skill_attempted, why, "
                "attempts_to_close, closed_at, closed_how, notes "
                "FROM capability_gaps ORDER BY ts DESC, id DESC LIMIT ?",
                (count,),
            ).fetchall()
        return [dict(row) for row in rows]

    def stats_by_intent(self, top: int = 10) -> list[dict[str, Any]]:
        """Топ пробелов по частоте запросов: intent, gaps, requests, last_ts."""
        count = max(1, int(top))
        with closing(self._connect()) as connection:
            rows = connection.execute(
                "SELECT user_intent, "
                "       COUNT(*) AS gaps, "
                "       SUM(attempts_to_close) AS requests, "
                "       MAX(ts) AS last_ts, "
                "       SUM(CASE WHEN closed_at IS NULL THEN 0 ELSE 1 END) AS closed "
                "FROM capability_gaps "
                "GROUP BY user_intent "
                "ORDER BY requests DESC, gaps DESC, user_intent "
                "LIMIT ?",
                (count,),
            ).fetchall()
        return [dict(row) for row in rows]

    def get(self, gap_id: int) -> dict[str, Any] | None:
        """Одна запись по id (для тестов и отчётов)."""
        with closing(self._connect()) as connection:
            row = connection.execute(
                "SELECT id, ts, command, user_intent, skill_attempted, why, "
                "attempts_to_close, closed_at, closed_how, notes "
                "FROM capability_gaps WHERE id = ?",
                (int(gap_id),),
            ).fetchone()
        return dict(row) if row is not None else None

    # --- закрытие ---

    def close(self, gap_id: int, how: str = "") -> bool:
        """Отметить пробел закрытым (появилось умение). True — если запись была."""
        with closing(self._connect()) as connection, connection:
            cursor = connection.execute(
                "UPDATE capability_gaps SET closed_at = ?, closed_how = ? "
                "WHERE id = ? AND closed_at IS NULL",
                (time.time(), " ".join(str(how or "").split()), int(gap_id)),
            )
            closed = cursor.rowcount > 0
        if closed:
            logger.info("пробел {} закрыт: {}", gap_id, how or "без пояснения")
        return closed


_default_gap_log: GapLog | None = None


def get_gap_log(
    db_path: str | Path | None = None, *, refresh: bool = False
) -> GapLog:
    """Общий журнал пробелов (по умолчанию data/kivi.db)."""
    global _default_gap_log
    if db_path is not None:
        return GapLog(db_path)
    if _default_gap_log is None or refresh:
        _default_gap_log = GapLog()
    return _default_gap_log

