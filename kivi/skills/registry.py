"""Реестр скиллов: манифесты skills/*/skill.yaml и статистика в data/skills.db.

Скилл — папка с манифестом skill.yaml и точкой входа handler.py:handle.
Реестр читает манифесты, валидирует обязательные поля, ведёт статистику
использования и ищет скилл под команду БЕЗ LLM (triggers/intents + difflib).

Использование:
    from kivi.skills.registry import SkillRegistry
    registry = SkillRegistry.from_config()
    registry.scan()                     # [SkillManifest, ...]
    registry.get("time_date")           # SkillManifest | None
    registry.search("который час")      # [(SkillManifest, score), ...]
    registry.record_use("time_date", ok=True)
    registry.should_disable("time_date")  # True после N ошибок подряд

Статистика живёт в data/skills.db (таблица skills_stats), в git не попадает.
fail_count — число ошибок ПОДРЯД: успешный вызов сбрасывает его в ноль,
поэтому should_disable() = fail_count >= auto_disable_after_fails.
"""

from __future__ import annotations

import difflib
import sqlite3
import time
from contextlib import closing
from pathlib import Path
from typing import Any, Literal

import yaml
from loguru import logger
from pydantic import BaseModel, ConfigDict, Field, ValidationError, field_validator

from kivi.config import CONFIG_PATH, DATA_DIR, PROJECT_ROOT

__all__ = [
    "DEFAULT_AUTO_DISABLE_FAILS",
    "DEFAULT_CALL_TIMEOUT_S",
    "DEFAULT_DB_PATH",
    "DEFAULT_INSTALLED_DIR",
    "DEFAULT_SKILLS_DIR",
    "FUZZY_THRESHOLD",
    "SkillCategory",
    "SkillManifest",
    "SkillPermissions",
    "SkillRegistry",
    "SkillRequires",
    "SkillSource",
    "SkillStats",
    "get_registry",
    "load_skills_config",
    "normalize_text",
    "phrase_score",
]

DEFAULT_SKILLS_DIR = "skills"
DEFAULT_INSTALLED_DIR = "skills/installed"
DEFAULT_DB_PATH = DATA_DIR / "skills.db"
DEFAULT_AUTO_DISABLE_FAILS = 3
DEFAULT_CALL_TIMEOUT_S = 30.0

# Порог difflib: ниже 0.7 совпадение не считается даже нечётким.
FUZZY_THRESHOLD = 0.7
# Оценки: точное совпадение фразы целиком > фраза отдельными словами >
# вхождение подстрокой > нечёткое совпадение (0.7 ... 0.85).
EXACT_SCORE = 1.0
WORD_SCORE = 0.9
PHRASE_SCORE = 0.85

SkillCategory = Literal[
    "media", "files", "browser", "code", "comms", "home", "vision", "system", "data"
]
RiskLevel = Literal["safe", "confirm", "forbidden"]
SourceKind = Literal["builtin", "github", "generated"]

STATS_SCHEMA = """
CREATE TABLE IF NOT EXISTS skills_stats (
    name          TEXT PRIMARY KEY,
    use_count     INTEGER NOT NULL DEFAULT 0,
    success_count INTEGER NOT NULL DEFAULT 0,
    fail_count    INTEGER NOT NULL DEFAULT 0,
    last_used_ts  REAL
);
"""

# Ручное включение/выключение скилла (например, после реактивации пробела).
# Живёт в data/skills.db рядом со статистикой.
FLAGS_SCHEMA = """
CREATE TABLE IF NOT EXISTS skill_flags (
    name       TEXT PRIMARY KEY,
    enabled    INTEGER NOT NULL DEFAULT 1,
    updated_ts REAL
);
"""


# --- модели манифеста ---


class SkillRequires(BaseModel):
    """Что скиллу нужно от окружения (пока справочно, при скане не проверяется)."""

    model_config = ConfigDict(extra="allow")

    python: str = ">=3.12"
    binaries: list[str] = Field(default_factory=list)
    env: list[str] = Field(default_factory=list)
    hardware: list[str] = Field(default_factory=list)


class SkillPermissions(BaseModel):
    """Разрешения скилла. Всё, чего здесь нет, брокер запрещает."""

    model_config = ConfigDict(extra="allow")

    network: list[str] = Field(default_factory=list)
    filesystem: list[str] = Field(default_factory=list)
    tools: list[str] = Field(default_factory=list)
    risk: RiskLevel = "safe"


class SkillSource(BaseModel):
    """Происхождение скилла: встроенный, из github или сгенерированный."""

    model_config = ConfigDict(extra="allow")

    kind: SourceKind = "builtin"
    repo: str = ""
    commit: str = ""


class SkillManifest(BaseModel):
    """Манифест skill.yaml. Лишние ключи не ломают загрузку (extra=allow)."""

    model_config = ConfigDict(extra="allow")

    name: str
    version: str
    description: str
    category: SkillCategory
    triggers: list[str]
    intents: list[str]
    requires: SkillRequires
    permissions: SkillPermissions
    entry: str
    enabled: bool = True
    source: SkillSource
    path: Path | None = None  # папка скилла, заполняет registry.scan()

    @field_validator("name")
    @classmethod
    def _name_is_slug(cls, value: str) -> str:
        slug = str(value).strip()
        if not slug or not all(ch.isalnum() or ch in "_-" for ch in slug):
            raise ValueError(f"name={value!r}: разрешены буквы, цифры, '_' и '-'")
        return slug

    @field_validator("triggers", "intents")
    @classmethod
    def _non_empty_list(cls, value: list[str]) -> list[str]:
        cleaned = [" ".join(str(item).split()) for item in value if str(item).strip()]
        if not cleaned:
            raise ValueError("список фраз не может быть пустым")
        return cleaned

    @field_validator("entry")
    @classmethod
    def _entry_looks_right(cls, value: str) -> str:
        file_part, _, func_part = str(value).partition(":")
        if not file_part.endswith(".py") or not func_part.isidentifier():
            raise ValueError(f"entry={value!r}: ожидалось 'handler.py:handle'")
        return value


class SkillStats(BaseModel):
    """Строка таблицы skills_stats."""

    name: str
    use_count: int = 0
    success_count: int = 0
    fail_count: int = 0  # ошибок подряд; успех сбрасывает в ноль
    last_used_ts: float | None = None


# --- нормализация и скоринг ---


def normalize_text(text: str) -> str:
    """Нижний регистр, ё->е, пунктуация -> пробелы, лишние пробелы убраны."""
    value = str(text).replace("ё", "е").replace("Ё", "Е").lower()
    cleaned = "".join(ch if ch.isalnum() or ch.isspace() else " " for ch in value)
    return " ".join(cleaned.split())


def _fuzzy_ratio(phrase_norm: str, command_norm: str) -> float:
    """Нечёткое сходство: целиком по символам плюс среднее по словам фразы."""
    whole = difflib.SequenceMatcher(None, phrase_norm, command_norm).ratio()
    words = phrase_norm.split()
    if len(words) < 2:
        return whole
    command_words = command_norm.split()
    per_word: list[float] = []
    for word in words:
        best = 0.0
        for candidate in command_words:
            ratio = difflib.SequenceMatcher(None, word, candidate).ratio()
            if ratio > best:
                best = ratio
        per_word.append(best if best >= FUZZY_THRESHOLD else 0.0)
    return max(whole, sum(per_word) / len(per_word))


def phrase_score(phrase: str, command: str) -> float:
    """Оценка одной фразы (trigger/intent) против команды. 0.0 — не совпало."""
    phrase_norm = normalize_text(phrase)
    command_norm = normalize_text(command)
    if not phrase_norm or not command_norm:
        return 0.0
    if phrase_norm == command_norm:
        return EXACT_SCORE
    if f" {phrase_norm} " in f" {command_norm} ":
        return WORD_SCORE
    if phrase_norm in command_norm:
        return PHRASE_SCORE
    ratio = _fuzzy_ratio(phrase_norm, command_norm)
    if ratio < FUZZY_THRESHOLD:
        return 0.0
    # 0.7 ... 0.85: нечёткое совпадение всегда слабее вхождения подстрокой.
    scale = (PHRASE_SCORE - FUZZY_THRESHOLD) / (1.0 - FUZZY_THRESHOLD)
    return FUZZY_THRESHOLD + (ratio - FUZZY_THRESHOLD) * scale


# --- конфиг секции skills ---


def load_skills_config(path: Path | None = None) -> dict[str, Any]:
    """Секция skills из config.yaml. Битый конфиг — пустой словарь и WARNING.

    Читаем YAML напрямую: подсистема скиллов не должна зависеть от того,
    нашлись ли аудиоустройства (их разрешает get_settings()).
    """
    config_path = Path(path) if path is not None else CONFIG_PATH
    try:
        raw = yaml.safe_load(config_path.read_text(encoding="utf-8")) or {}
    except (OSError, yaml.YAMLError) as exc:
        logger.warning("скиллы: не читается {}: {}", config_path.name, exc)
        return {}
    section = raw.get("skills") if isinstance(raw, dict) else None
    return dict(section) if isinstance(section, dict) else {}


def _resolve_dir(value: str | Path | None, default: str) -> Path:
    """Относительный путь считается от корня проекта."""
    if value is None or not str(value).strip():
        return PROJECT_ROOT / default
    candidate = Path(str(value))
    return candidate if candidate.is_absolute() else PROJECT_ROOT / candidate


# --- реестр ---


class SkillRegistry:
    """Скиллы из skills/ и skills/installed/ плюс их статистика."""

    def __init__(
        self,
        *,
        skills_dir: str | Path | None = None,
        installed_dir: str | Path | None = None,
        db_path: str | Path | None = None,
        auto_disable_after_fails: int = DEFAULT_AUTO_DISABLE_FAILS,
        call_timeout_s: float = DEFAULT_CALL_TIMEOUT_S,
    ) -> None:
        self.skills_dir = _resolve_dir(skills_dir, DEFAULT_SKILLS_DIR)
        self.installed_dir = _resolve_dir(installed_dir, DEFAULT_INSTALLED_DIR)
        self.db_path = Path(db_path) if db_path is not None else DEFAULT_DB_PATH
        self.auto_disable_after_fails = max(1, int(auto_disable_after_fails))
        self.call_timeout_s = max(1.0, float(call_timeout_s))
        self._skills: dict[str, SkillManifest] = {}
        self._scanned = False
        self._ensure_stats()

    @classmethod
    def from_config(cls, config: dict[str, Any] | None = None) -> "SkillRegistry":
        """Собрать реестр из секции skills конфига (по умолчанию из config.yaml)."""
        section = dict(config) if config is not None else load_skills_config()
        return cls(
            skills_dir=section.get("dir"),
            installed_dir=section.get("installed_dir"),
            auto_disable_after_fails=int(
                section.get("auto_disable_after_fails") or DEFAULT_AUTO_DISABLE_FAILS
            ),
            call_timeout_s=float(section.get("call_timeout_s") or DEFAULT_CALL_TIMEOUT_S),
        )

    # --- манифесты ---

    def scan(self) -> list[SkillManifest]:
        """Перечитать папки со скиллами. Битые манифесты — WARNING и пропуск."""
        found: dict[str, SkillManifest] = {}
        flags = self._enabled_flags()
        for directory in (self.skills_dir, self.installed_dir):
            if not directory.is_dir():
                logger.debug("скиллы: папки {} нет — пропускаю", directory)
                continue
            for entry in sorted(directory.iterdir(), key=lambda item: item.name.lower()):
                if not entry.is_dir():
                    continue
                manifest_path = entry / "skill.yaml"
                if not manifest_path.is_file():
                    logger.debug("скиллы: в {} нет skill.yaml — пропускаю", entry.name)
                    continue
                try:
                    manifest = self._read_manifest(manifest_path)
                except (OSError, yaml.YAMLError, ValidationError, ValueError) as exc:
                    logger.warning(
                        "скиллы: {} пропущен — битый манифест: {}", entry.name, exc
                    )
                    continue
                if manifest.name in found:
                    logger.warning(
                        "скиллы: дубликат имени {} (папка {}) — оставляю первый",
                        manifest.name,
                        entry.name,
                    )
                    continue
                if manifest.name in flags:
                    # Ручной флаг из skills.db перекрывает enabled из манифеста.
                    manifest = manifest.model_copy(
                        update={"enabled": bool(flags[manifest.name])}
                    )
                found[manifest.name] = manifest

        self._skills = found
        self._scanned = True
        names = ", ".join(sorted(found)) or "пусто"
        logger.info(
            "скиллы: найдено {} ({} + {}): {}",
            len(found),
            self.skills_dir.name,
            self.installed_dir.name,
            names,
        )
        return [found[name] for name in sorted(found)]

    def _read_manifest(self, manifest_path: Path) -> SkillManifest:
        raw = yaml.safe_load(manifest_path.read_text(encoding="utf-8"))
        if not isinstance(raw, dict):
            raise ValueError("ожидался словарь на верхнем уровне")
        manifest = SkillManifest.model_validate(raw)
        entry_file = manifest.entry.partition(":")[0]
        if not (manifest_path.parent / entry_file).is_file():
            raise ValueError(f"нет файла точки входа {entry_file}")
        return manifest.model_copy(update={"path": manifest_path.parent})

    def _ensure_scanned(self) -> None:
        if not self._scanned:
            self.scan()

    def get(self, name: str) -> SkillManifest | None:
        """Манифест по имени (None, если такого скилла нет)."""
        self._ensure_scanned()
        return self._skills.get(str(name))

    def list_enabled(self) -> list[SkillManifest]:
        """Все включённые скиллы, по алфавиту."""
        self._ensure_scanned()
        return [
            self._skills[name] for name in sorted(self._skills) if self._skills[name].enabled
        ]

    def list_all(self) -> list[SkillManifest]:
        """Все скиллы, включая выключенные (для реактивации пробелов)."""
        self._ensure_scanned()
        return [self._skills[name] for name in sorted(self._skills)]

    # --- включение/выключение ---

    def _enabled_flags(self) -> dict[str, bool]:
        """Флаги включения из skills.db (перекрывают enabled манифеста)."""
        with closing(self._connect()) as connection:
            rows = connection.execute("SELECT name, enabled FROM skill_flags").fetchall()
        return {str(row["name"]): bool(row["enabled"]) for row in rows}

    def enable(self, name: str) -> bool:
        """Включить скилл: флаг в skills.db + сброс серии ошибок подряд.

        Нужно для закрытия пробелов: скилл мог быть отключён автовыходом
        после auto_disable_after_fails ошибок. True — если скилл существует.
        """
        manifest = self.get(str(name))
        if manifest is None:
            logger.warning("скиллы: включить {!r} нельзя — такого скилла нет", name)
            return False
        try:
            with closing(self._connect()) as connection, connection:
                connection.execute(
                    "INSERT INTO skill_flags (name, enabled, updated_ts) VALUES (?, 1, ?) "
                    "ON CONFLICT(name) DO UPDATE SET enabled = 1, "
                    "updated_ts = excluded.updated_ts",
                    (str(name), time.time()),
                )
                connection.execute(
                    "UPDATE skills_stats SET fail_count = 0 WHERE name = ?", (str(name),)
                )
        except sqlite3.Error as exc:  # noqa: BLE001 - флаг не критичнее диалога
            logger.warning("скиллы: не удалось включить {}: {}", name, exc)
            return False
        self._skills[str(name)] = manifest.model_copy(update={"enabled": True})
        logger.info("скиллы: {} включён, серия ошибок сброшена", name)
        return True

    # --- статистика ---

    def _connect(self) -> sqlite3.Connection:
        self.db_path.parent.mkdir(parents=True, exist_ok=True)
        connection = sqlite3.connect(str(self.db_path), timeout=5.0)
        connection.row_factory = sqlite3.Row
        return connection

    def _ensure_stats(self) -> None:
        with closing(self._connect()) as connection, connection:
            connection.executescript(STATS_SCHEMA)
            connection.executescript(FLAGS_SCHEMA)

    def stats(self, name: str) -> SkillStats | None:
        """Статистика скилла (None, если он ещё ни разу не вызывался)."""
        with closing(self._connect()) as connection:
            row = connection.execute(
                "SELECT name, use_count, success_count, fail_count, last_used_ts "
                "FROM skills_stats WHERE name = ?",
                (str(name),),
            ).fetchone()
        if row is None:
            return None
        return SkillStats(
            name=str(row["name"]),
            use_count=int(row["use_count"]),
            success_count=int(row["success_count"]),
            fail_count=int(row["fail_count"]),
            last_used_ts=(
                None if row["last_used_ts"] is None else float(row["last_used_ts"])
            ),
        )

    def record_use(self, name: str, ok: bool) -> SkillStats | None:
        """Учесть вызов: use_count +1, успех сбрасывает fail_count, ошибка копит."""
        used_at = time.time()
        try:
            with closing(self._connect()) as connection, connection:
                connection.execute(
                    "INSERT OR IGNORE INTO skills_stats (name) VALUES (?)", (str(name),)
                )
                if ok:
                    connection.execute(
                        "UPDATE skills_stats SET use_count = use_count + 1, "
                        "success_count = success_count + 1, fail_count = 0, "
                        "last_used_ts = ? WHERE name = ?",
                        (used_at, str(name)),
                    )
                else:
                    connection.execute(
                        "UPDATE skills_stats SET use_count = use_count + 1, "
                        "fail_count = fail_count + 1, last_used_ts = ? WHERE name = ?",
                        (used_at, str(name)),
                    )
        except sqlite3.Error as exc:  # noqa: BLE001 - статистика не должна ломать вызов
            logger.warning("скиллы: не удалось записать статистику {}: {}", name, exc)
            return None
        return self.stats(name)

    def should_disable(self, name: str) -> bool:
        """Пора ли отключать скилл: слишком много ошибок подряд."""
        stats = self.stats(name)
        return stats is not None and stats.fail_count >= self.auto_disable_after_fails

    # --- поиск ---

    def search(self, command: str) -> list[tuple[SkillManifest, float]]:
        """Скиллы под команду: [(манифест, оценка)], по убыванию оценки."""
        text = str(command or "")
        if not normalize_text(text):
            return []
        results: list[tuple[SkillManifest, float]] = []
        for manifest in self.list_enabled():
            best = 0.0
            for phrase in (*manifest.triggers, *manifest.intents):
                score = phrase_score(phrase, text)
                if score > best:
                    best = score
            if best > 0.0:
                results.append((manifest, round(best, 3)))
        results.sort(key=lambda item: (-item[1], item[0].name))
        return results


_default_registry: SkillRegistry | None = None


def get_registry(*, refresh: bool = False) -> SkillRegistry:
    """Общий реестр проекта (создаётся один раз, при создании сканирует папки)."""
    global _default_registry
    if _default_registry is None or refresh:
        registry = SkillRegistry.from_config()
        registry.scan()
        _default_registry = registry
    return _default_registry
