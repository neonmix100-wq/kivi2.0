"""История диалога: SQLite data/kivi.db, таблицы messages и sessions.

Новая сессия начинается автоматически после 10 минут тишины.
history() отдаёт последние реплики в пределах бюджета токенов,
обрезая с начала и всегда сохраняя system.

Использование:
    from kivi.brain.session import Session
    session = Session()
    session.add("user", "привет")
    messages = session.history()          # [{"role": ..., "content": ...}, ...]
"""

from __future__ import annotations

import sqlite3
import time
import uuid
from contextlib import closing
from pathlib import Path

from kivi.config import DATA_DIR
from kivi.log import logger

__all__ = ["DEFAULT_HISTORY_TOKENS", "IDLE_RESET_S", "Session"]

DEFAULT_DB_PATH = DATA_DIR / "kivi.db"
IDLE_RESET_S = 600.0  # 10 минут тишины -> новая сессия
DEFAULT_HISTORY_TOKENS = 1800

SCHEMA = """
CREATE TABLE IF NOT EXISTS messages (
    id         INTEGER PRIMARY KEY AUTOINCREMENT,
    session_id TEXT    NOT NULL,
    ts         REAL    NOT NULL,
    role       TEXT    NOT NULL,
    content    TEXT    NOT NULL
);
CREATE INDEX IF NOT EXISTS idx_messages_session ON messages(session_id, ts);
CREATE TABLE IF NOT EXISTS sessions (
    id         TEXT PRIMARY KEY,
    started_ts REAL NOT NULL,
    last_ts    REAL NOT NULL
);
"""


def count_tokens(text: str) -> int:
    """Грубая оценка токенов: ~3 символа на токен для русского текста."""
    return max(1, len(str(text)) // 3)


class Session:
    """Текущая сессия диалога и её история."""

    def __init__(
        self,
        db_path: str | Path | None = None,
        *,
        idle_reset_s: float = IDLE_RESET_S,
        max_tokens: int = DEFAULT_HISTORY_TOKENS,
    ) -> None:
        self.db_path = Path(db_path) if db_path is not None else DEFAULT_DB_PATH
        self.idle_reset_s = float(idle_reset_s)
        self.max_tokens = int(max_tokens)
        self._session_id: str | None = None
        self._last_activity: float = 0.0
        self._ensure_schema()
        self._open_or_start()

    # --- схема и соединение ---

    def _connect(self) -> sqlite3.Connection:
        self.db_path.parent.mkdir(parents=True, exist_ok=True)
        connection = sqlite3.connect(str(self.db_path))
        connection.row_factory = sqlite3.Row
        return connection

    def _ensure_schema(self) -> None:
        with closing(self._connect()) as connection, connection:
            connection.executescript(SCHEMA)

    # --- сессии ---

    @property
    def session_id(self) -> str:
        self._refresh_if_idle()
        return self._session_id or ""

    def _open_or_start(self) -> None:
        """Взять последнюю сессию, если она свежая, иначе начать новую."""
        now = time.time()
        with closing(self._connect()) as connection:
            row = connection.execute(
                "SELECT id, last_ts FROM sessions ORDER BY last_ts DESC LIMIT 1"
            ).fetchone()
        if row is not None and now - float(row["last_ts"]) <= self.idle_reset_s:
            self._session_id = str(row["id"])
            self._last_activity = float(row["last_ts"])
            logger.info(
                "Session: продолжаю {} (тишина {:.0f} c)",
                self._session_id[:8],
                now - self._last_activity,
            )
            return
        self._new_session()

    def _new_session(self) -> None:
        now = time.time()
        self._session_id = str(uuid.uuid4())
        self._last_activity = now
        with closing(self._connect()) as connection, connection:
            connection.execute(
                "INSERT INTO sessions (id, started_ts, last_ts) VALUES (?, ?, ?)",
                (self._session_id, now, now),
            )
        logger.info("Session: новая {} (после тишины)", self._session_id[:8])

    def _refresh_if_idle(self) -> None:
        if self._session_id is None:
            self._open_or_start()
            return
        if self._last_activity and (time.time() - self._last_activity) > self.idle_reset_s:
            self._new_session()

    # --- реплики ---

    def add(self, role: str, content: str) -> None:
        """Добавить реплику в текущую сессию."""
        text = str(content).strip()
        if not text:
            return
        self._refresh_if_idle()
        now = time.time()
        with closing(self._connect()) as connection, connection:
            connection.execute(
                "INSERT INTO messages (session_id, ts, role, content) VALUES (?, ?, ?, ?)",
                (self.session_id, now, str(role), text),
            )
            connection.execute(
                "UPDATE sessions SET last_ts = ? WHERE id = ?", (now, self.session_id)
            )
        self._last_activity = now
        logger.debug("Session: {} {} ({} симв.)", role, self.session_id[:8], len(text))

    def history(self, max_tokens: int | None = None) -> list[dict]:
        """Последние реплики сессии в пределах бюджета токенов (system сохраняется)."""
        limit = int(max_tokens if max_tokens is not None else self.max_tokens)
        self._refresh_if_idle()
        with closing(self._connect()) as connection:
            rows = connection.execute(
                "SELECT role, content FROM messages WHERE session_id = ? "
                "ORDER BY ts ASC, id ASC",
                (self.session_id,),
            ).fetchall()

        system_messages = [row for row in rows if row["role"] == "system"]
        dialogue = [row for row in rows if row["role"] != "system"]

        budget = limit
        selected: list[tuple[str, str]] = []
        for row in reversed(dialogue):
            content = str(row["content"])
            cost = count_tokens(content)
            if cost > budget:
                # Самая свежая реплика сама не влезает — усечём её, старые просто отбрасываем.
                if not selected and budget > 0:
                    keep_chars = max(40, budget * 3)
                    selected.append((str(row["role"]), "…" + content[-keep_chars:]))
                    budget = 0
                break
            selected.append((str(row["role"]), content))
            budget -= cost
        selected.reverse()

        result: list[dict] = [
            {"role": "system", "content": row["content"]} for row in system_messages
        ]
        result.extend({"role": role, "content": content} for role, content in selected)
        return result

    def messages_since(
        self,
        since_ts: float,
        *,
        max_tokens: int | None = None,
    ) -> tuple[list[dict], float]:
        """Реплики текущей сессии с ts > since_ts и максимальный ts.

        Не вызывает _refresh_if_idle: планировщик рефлексии проверяет сессию
        ровно в момент простоя, когда авто-переход на новую сессию уже не нужен.
        System-реплики в диалог рефлексии не попадают.
        """
        if self._session_id is None:
            self._open_or_start()
        limit = int(max_tokens if max_tokens is not None else self.max_tokens)
        with closing(self._connect()) as connection:
            rows = connection.execute(
                "SELECT role, content, ts FROM messages "
                "WHERE session_id = ? AND ts > ? ORDER BY ts ASC, id ASC",
                (self._session_id, float(since_ts)),
            ).fetchall()

        dialogue = [row for row in rows if row["role"] != "system"]
        if not dialogue:
            return [], float(since_ts)
        max_ts = max(float(row["ts"]) for row in dialogue)

        budget = limit
        selected: list[tuple[str, str]] = []
        for row in reversed(dialogue):
            content = str(row["content"])
            cost = count_tokens(content)
            if cost > budget:
                if not selected and budget > 0:
                    keep_chars = max(40, budget * 3)
                    selected.append((str(row["role"]), "…" + content[-keep_chars:]))
                    budget = 0
                break
            selected.append((str(row["role"]), content))
            budget -= cost
        selected.reverse()
        return (
            [{"role": role, "content": content} for role, content in selected],
            max_ts,
        )

    def last_activity_ts(self) -> float:
        """Время последней реплики (без запуска новой сессии)."""
        if self._last_activity:
            return float(self._last_activity)
        if self._session_id is None:
            return 0.0
        with closing(self._connect()) as connection:
            row = connection.execute(
                "SELECT last_ts FROM sessions WHERE id = ?", (self._session_id,)
            ).fetchone()
        return float(row["last_ts"]) if row is not None else 0.0

    def clear(self) -> None:
        """Начать новую сессию (старые сообщения остаются в БД)."""
        self._new_session()
