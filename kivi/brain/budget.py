"""Учёт расходов на LLM: SQLite data/kivi.db, таблица usage, месячный лимит.

Деньги берутся из ответа Polza (usage.cost_rub). Если провайдер цену не прислал —
считаем по прайсу brain.prices из config.yaml (руб за 1000 токенов).

Месяц считается по местной полуночи 1-го числа (brain.budget.timezone).
При 80% лимита — WARNING в лог, при 100% — check() возвращает False,
и вызывающий обязан поднять BudgetExceeded.
"""

from __future__ import annotations

import sqlite3
import time
from contextlib import closing
from datetime import datetime, timedelta, timezone as dt_timezone
from pathlib import Path
from zoneinfo import ZoneInfo

from kivi.config import DATA_DIR, Settings, get_settings
from kivi.log import logger

__all__ = [
    "DEFAULT_MONTHLY_LIMIT_RUB",
    "DEFAULT_TIMEZONE",
    "DEFAULT_WARN_AT",
    "Budget",
    "BudgetExceeded",
]

DEFAULT_DB_PATH = DATA_DIR / "kivi.db"
DEFAULT_MONTHLY_LIMIT_RUB = 3000.0
DEFAULT_WARN_AT = 0.8
DEFAULT_TIMEZONE = "Asia/Novosibirsk"

# На Windows без пакета tzdata zoneinfo не работает. Для Новосибирска смещение
# постоянное (+07:00, перехода на летнее время нет), поэтому fallback безопасен.
TZ_FIXED_OFFSETS = {"Asia/Novosibirsk": 7, "Asia/Krasnoyarsk": 7, "Europe/Moscow": 3}


def _make_timezone(name: str):
    """ZoneInfo, а при отсутствии базы поясов — фиксированное смещение."""
    try:
        return ZoneInfo(name)
    except Exception:  # noqa: BLE001 - ZoneInfoNotFoundError и подобное
        hours = TZ_FIXED_OFFSETS.get(name)
        if hours is None:
            raise
        logger.warning(
            "Budget: база часовых поясов недоступна (нет пакета tzdata) — "
            "использую фиксированное смещение UTC{:+d} для {}",
            hours,
            name,
        )
        return dt_timezone(timedelta(hours=hours))

SCHEMA = """
CREATE TABLE IF NOT EXISTS usage (
    id         INTEGER PRIMARY KEY AUTOINCREMENT,
    ts         REAL    NOT NULL,           -- epoch-секунды (UTC)
    provider   TEXT    NOT NULL,
    model      TEXT    NOT NULL,
    task_type  TEXT    NOT NULL,
    tokens_in  INTEGER NOT NULL DEFAULT 0,
    tokens_out INTEGER NOT NULL DEFAULT 0,
    cost_rub   REAL    NOT NULL DEFAULT 0,
    ok         INTEGER NOT NULL DEFAULT 1,
    attempt    INTEGER NOT NULL DEFAULT 1
);
CREATE INDEX IF NOT EXISTS idx_usage_ts ON usage(ts);
"""


class BudgetExceeded(RuntimeError):
    """Месячный лимит расходов исчерпан."""


class Budget:
    """Копилка расходов и проверка лимита."""

    def __init__(
        self,
        settings: Settings | None = None,
        *,
        db_path: str | Path | None = None,
        monthly_limit_rub: float | None = None,
        warn_at: float | None = None,
        timezone_name: str | None = None,
    ) -> None:
        self.settings = settings if settings is not None else get_settings()
        brain = self.settings.section("brain")
        budget_cfg = brain.get("budget") or {}

        self.db_path = Path(db_path) if db_path is not None else DEFAULT_DB_PATH
        limit = budget_cfg.get("monthly_rub", DEFAULT_MONTHLY_LIMIT_RUB)
        self.monthly_limit_rub = float(
            monthly_limit_rub if monthly_limit_rub is not None else limit
        )
        warn = budget_cfg.get("warn_at", DEFAULT_WARN_AT)
        self.warn_at = float(warn_at if warn_at is not None else warn)
        self.timezone_name = str(
            timezone_name or budget_cfg.get("timezone") or DEFAULT_TIMEZONE
        )
        self.timezone = _make_timezone(self.timezone_name)
        self.prices: dict = brain.get("prices") or {}

        self._warned_ratio = False
        self._exceeded_logged = False
        self._warned_no_price: set[str] = set()
        self._ensure_schema()

    # --- инфраструктура ---

    def _connect(self) -> sqlite3.Connection:
        self.db_path.parent.mkdir(parents=True, exist_ok=True)
        connection = sqlite3.connect(str(self.db_path))
        connection.row_factory = sqlite3.Row
        return connection

    def _ensure_schema(self) -> None:
        with closing(self._connect()) as connection, connection:
            connection.executescript(SCHEMA)

    # --- запись ---

    def estimate_cost(self, model: str, tokens_in: int, tokens_out: int) -> float:
        """Стоимость по прайсу config.yaml (руб за 1000 токенов)."""
        price = self.prices.get(model) or self.prices.get("default") or {}
        price_in = float(price.get("in", 0.0) or 0.0)
        price_out = float(price.get("out", 0.0) or 0.0)
        if not price_in and not price_out and model not in self._warned_no_price:
            self._warned_no_price.add(model)
            logger.warning(
                "Budget: ни usage.cost_rub, ни прайса для {} — расход записан как 0. "
                "Заполни brain.prices в config.yaml, если нужна оценка.",
                model,
            )
        return (tokens_in * price_in + tokens_out * price_out) / 1000.0

    def record(
        self,
        *,
        provider: str,
        model: str,
        task_type: str,
        tokens_in: int = 0,
        tokens_out: int = 0,
        cost_rub: float | None = None,
        ok: bool = True,
        attempt: int = 1,
    ) -> float:
        """Записать один вызов. Возвращает учтённую стоимость в рублях."""
        cost = (
            self.estimate_cost(model, tokens_in, tokens_out)
            if cost_rub is None
            else float(cost_rub)
        )
        with closing(self._connect()) as connection, connection:
            connection.execute(
                """
                INSERT INTO usage
                    (ts, provider, model, task_type, tokens_in, tokens_out,
                     cost_rub, ok, attempt)
                VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?)
                """,
                (
                    time.time(),
                    str(provider),
                    str(model),
                    str(task_type),
                    int(tokens_in),
                    int(tokens_out),
                    cost,
                    1 if ok else 0,
                    int(attempt),
                ),
            )
        logger.debug(
            "Budget: {} | {} | {} | {} ток. | {:.6f} руб | ok={} | попытка {}",
            provider,
            model,
            task_type,
            int(tokens_in) + int(tokens_out),
            cost,
            ok,
            attempt,
        )
        self._warn_if_needed()
        return cost

# --- ЛИМИТ И ПЕРИОД ---

    def month_start(self, moment: datetime | None = None) -> datetime:
        """Начало текущего месяца по местному времени (полночь 1-го числа)."""
        local = (moment or datetime.now(tz=self.timezone)).astimezone(self.timezone)
        return local.replace(day=1, hour=0, minute=0, second=0, microsecond=0)

    def month_spent_rub(self) -> float:
        """Сколько потрачено с местной полуночи 1-го числа текущего месяца."""
        start_ts = self.month_start().timestamp()
        with closing(self._connect()) as connection:
            row = connection.execute(
                "SELECT COALESCE(SUM(cost_rub), 0) AS total FROM usage WHERE ts >= ?",
                (start_ts,),
            ).fetchone()
        return float(row["total"] or 0.0)

    def percent_used(self) -> float:
        """Процент израсходованного лимита (0..100 и больше)."""
        if self.monthly_limit_rub <= 0:
            return 0.0
        return self.month_spent_rub() / self.monthly_limit_rub * 100.0

    def _ratio(self) -> float:
        if self.monthly_limit_rub <= 0:
            return 0.0
        return self.month_spent_rub() / self.monthly_limit_rub

    def _warn_if_needed(self) -> None:
        """WARNING при пересечении порога warn_at, ERROR при перерасходе."""
        ratio = self._ratio()
        if ratio >= 1.0:
            if not self._exceeded_logged:
                self._exceeded_logged = True
                logger.error(
                    "Budget: месячный лимит исчерпан — {:.2f} из {:.2f} руб (100%)",
                    self.month_spent_rub(),
                    self.monthly_limit_rub,
                )
        elif ratio >= self.warn_at and not self._warned_ratio:
            self._warned_ratio = True
            logger.warning(
                "Budget: израсходовано {:.0f}% лимита ({:.2f} из {:.2f} руб)",
                ratio * 100.0,
                self.month_spent_rub(),
                self.monthly_limit_rub,
            )

    def check(self) -> bool:
        """True — тратить можно; False — месячный лимит исчерпан."""
        self._warn_if_needed()
        return self._ratio() < 1.0

    def reset_month(self) -> None:
        """Начало нового месяца: сбросить флаги предупреждений.

        Сумма месяца не хранится отдельно — она всегда считается по дате записи,
        поэтому с местной полуночи 1-го числа счётчик начинается с нуля сам.
        """
        self._warned_ratio = False
        self._exceeded_logged = False
        logger.info(
            "Budget: новый период {} (часовой пояс {}), расход {:.2f} руб",
            self.month_start().strftime("%Y-%m"),
            self.timezone_name,
            self.month_spent_rub(),
        )

    def describe(self) -> str:
        """Короткая сводка для логов и смоуков."""
        return (
            f"израсходовано {self.month_spent_rub():.2f} из {self.monthly_limit_rub:.2f} руб "
            f"({self.percent_used():.1f}%)"
        )
