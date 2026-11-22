"""Поиск по смыслу в vault: эмбеддинги Polza + keyword-фолбэк.

Индекс: SQLite (data/memory_index.db), hash факта -> вектор (float32 BLOB).
Если эмбеддинги недоступны (модель отключена, нет сети) — работает
keyword-скоринг по пересечению слов, чтобы память не отваливалась офлайн.
"""

from __future__ import annotations

import hashlib
import math
import os
import re
import sqlite3
from array import array
from contextlib import closing
from pathlib import Path

import requests
from dotenv import load_dotenv

from kivi.config import DATA_DIR
from kivi.log import logger
from kivi.memory.vault import iter_facts, iter_schedule, iter_tasks

__all__ = ["INDEX_DB", "MemoryRetriever", "cosine"]

INDEX_DB = DATA_DIR / "memory_index.db"
DEFAULT_MODEL = "baai/bge-m3"
FALLBACK_MODELS = ("openai/text-embedding-3-small",)
EMBED_TIMEOUT = 30.0
MAX_KIND_TOKENS = 500
CHARS_PER_TOKEN = 3
SCHEDULE_HINTS = (
    "сегодня",
    "завтра",
    "недел",
    "план",
    "расписан",
    "встреч",
)
TASK_HINTS = ("просил", "не забывай", "задач", "сделать", "сделай")
STOPWORDS = {
    "и", "в", "во", "не", "что", "он", "на", "я", "с", "со", "как", "а", "то",
    "все", "она", "так", "его", "но", "да", "ты", "к", "у", "же", "вы", "за",
    "бы", "по", "только", "ее", "мне", "было", "вот", "от", "меня", "еще",
    "нет", "о", "из", "ему", "теперь", "когда", "даже", "ну", "вдруг", "ли",
    "если", "уже", "или", "ни", "быть", "был", "него", "до", "вас", "нибудь",
    "опять", "уж", "вам", "ведь", "там", "потом", "себя", "ничего", "ей",
    "может", "они", "тут", "где", "есть", "надо", "ней", "для", "мы", "тебя",
    "их", "чем", "была", "сам", "чтоб", "без", "будто", "чего", "раз", "тоже",
    "себе", "под", "будет", "ж", "тогда", "кто", "этот", "того", "потому",
    "этого", "какой", "совсем", "ним", "здесь", "этом", "один", "почти",
    "мой", "тем", "чтобы", "нее", "были", "куда", "зачем", "всех", "никогда",
    "можно", "при", "наконец", "два", "об", "другой", "хоть", "после", "над",
    "больше", "тот", "через", "эти", "нас", "про", "всего", "них", "какая",
    "много", "разве", "три", "эту", "моя", "впрочем", "хорошо", "свою",
    "этой", "перед", "иногда", "лучше", "чуть", "том", "нельзя", "такой",
    "им", "более", "всегда", "конечно", "всю", "между",
}
_WORD_RE = re.compile(r"\w{4,}", re.UNICODE)


def cosine(a: list[float], b: list[float]) -> float:
    if not a or not b or len(a) != len(b):
        return 0.0
    dot = sum(x * y for x, y in zip(a, b))
    na = math.sqrt(sum(x * x for x in a))
    nb = math.sqrt(sum(y * y for y in b))
    return dot / (na * nb) if na and nb else 0.0


def _words(text: str) -> set[str]:
    lowered = str(text).lower().replace("ё", "е")
    return {w for w in _WORD_RE.findall(lowered) if w not in STOPWORDS}


def _hash(text: str) -> str:
    return hashlib.sha256(" ".join(str(text).lower().split()).encode("utf-8")).hexdigest()


def _entry_lines(kind: str, vault_path: str | Path | None = None) -> list[str]:
    """Строки структурированной памяти для блока в промпт."""
    if kind == "schedule":
        entries = [f"[{when}] {what}" for when, what in iter_schedule(vault_path)]
    elif kind == "tasks":
        entries = [
            f"[{status}] {what}"
            for status, what in iter_tasks(vault_path)
            if status == "open"
        ]
    else:
        return []
    return entries


def _trim_lines(lines: list[str], max_tokens: int = MAX_KIND_TOKENS) -> list[str]:
    """Обрезка структурированного блока примерно до max_tokens."""
    limit = max_tokens * CHARS_PER_TOKEN
    selected: list[str] = []
    used = 0
    for line in lines:
        if used + len(line) + 1 > limit and selected:
            break
        selected.append(line[:limit])
        used += len(line) + 1
    return selected


def _memory_embed_model() -> str:
    """Модель эмбеддингов из config.yaml (memory.embed_model)."""
    try:
        from kivi.config import load_config

        memory = load_config().model_dump().get("memory") or {}
        return " ".join(str(memory.get("embed_model") or "").split())
    except Exception as exc:  # noqa: BLE001 - конфиг не должен ронять память
        logger.debug("Память: memory.embed_model не прочитан: {}", exc)
        return ""


class MemoryRetriever:
    """Поиск релевантных фактов с кэшем векторов и офлайн-фолбэком."""

    def __init__(
        self,
        *,
        db_path: str | Path | None = None,
        model: str | None = None,
        k: int = 4,
        vault_path: str | Path | None = None,
    ) -> None:
        self.db_path = Path(db_path) if db_path is not None else INDEX_DB
        self.vault_path = vault_path
        self.k = int(k)
        load_dotenv()
        self.base = (os.getenv("LLM_BASE_URL") or "").strip().rstrip("/")
        self.key = (os.getenv("LLM_API_KEY") or "").strip()
        cfg_model = " ".join(str(model or _memory_embed_model() or DEFAULT_MODEL).split())
        self.models = [cfg_model] + [m for m in FALLBACK_MODELS if m != cfg_model]
        self.last_path = "none"
        self._ensure_schema()

    def _connect(self) -> sqlite3.Connection:
        self.db_path.parent.mkdir(parents=True, exist_ok=True)
        return sqlite3.connect(str(self.db_path))

    def _ensure_schema(self) -> None:
        with closing(self._connect()) as connection, connection:
            connection.execute(
                """
                CREATE TABLE IF NOT EXISTS embeddings (
                    hash   TEXT PRIMARY KEY,
                    text   TEXT NOT NULL,
                    vector BLOB NOT NULL,
                    model  TEXT NOT NULL
                )
                """
            )

    # --- эмбеддинги ---

    def _request_embedding(self, texts: list[str], model: str) -> list[list[float]] | None:
        if not self.base or not self.key:
            return None
        try:
            response = requests.post(
                f"{self.base}/embeddings",
                headers={"Authorization": f"Bearer {self.key}"},
                json={"model": model, "input": texts},
                timeout=EMBED_TIMEOUT,
            )
        except requests.RequestException as exc:
            logger.debug("Эмбеддинги недоступны ({}): {}", model, exc)
            return None
        if response.status_code != 200:
            logger.debug("Эмбеддинги {} -> HTTP {}", model, response.status_code)
            return None
        try:
            data = response.json()["data"]
            return [list(map(float, item["embedding"])) for item in data]
        except (ValueError, KeyError, TypeError):
            return None

    def _cached_vector(self, text: str) -> list[float] | None:
        key = _hash(text)
        with closing(self._connect()) as connection:
            row = connection.execute(
                "SELECT vector FROM embeddings WHERE hash = ?", (key,)
            ).fetchone()
        if row is None:
            return None
        vector = array("f")
        vector.frombytes(row[0])
        return list(vector)

    def _store_vector(self, text: str, vector: list[float], model: str) -> None:
        payload = array("f", vector).tobytes()
        with closing(self._connect()) as connection, connection:
            connection.execute(
                "INSERT OR REPLACE INTO embeddings (hash, text, vector, model) "
                "VALUES (?, ?, ?, ?)",
                (_hash(text), text, payload, model),
            )

    def embed(self, texts: list[str]) -> tuple[list[list[float]], str] | None:
        """Векторы для текстов. Возвращает (векторы, модель) или None офлайн."""
        if not texts:
            return [], self.models[0]
        for model in self.models:
            vectors = self._request_embedding(texts, model)
            if vectors is not None and len(vectors) == len(texts):
                if model != self.models[0]:
                    logger.warning(
                        "Память: {} недоступна, использую {}", self.models[0], model
                    )
                return vectors, model
        logger.warning("Память: эмбеддинги недоступны — ищу по словам (fallback)")
        return None

    # --- поиск ---

    def _facts(self) -> list[str]:
        return [fact for _note, _day, fact in iter_facts(self.vault_path)]

    def _rank_facts(self, text: str, facts: list[str], limit: int) -> list[str]:
        """Top-k фактов по эмбеддингам или keyword-фолбэку."""
        if not text or not facts:
            return []
        embedded = self.embed([text])
        if embedded is not None:
            missing = [fact for fact in facts if self._cached_vector(fact) is None]
            if missing:
                batch = self.embed(missing)
                if batch is not None:
                    vectors, used_model = batch
                    for fact, vector in zip(missing, vectors):
                        self._store_vector(fact, vector, used_model)
                else:
                    embedded = None

        if embedded is not None:
            query_vector = embedded[0][0]
            scored: list[tuple[float, str]] = []
            for fact in facts:
                vector = self._cached_vector(fact)
                if vector is not None:
                    scored.append((cosine(query_vector, vector), fact))
            if scored:
                scored.sort(key=lambda item: item[0], reverse=True)
                self.last_path = "embeddings"
                logger.info(
                    "Память: поиск по смыслу ({} фактов, top-{})", len(facts), limit
                )
                return [fact for score, fact in scored[:limit] if score > 0.05]

        query_words = _words(text)
        scored_kw: list[tuple[float, str]] = []
        for fact in facts:
            fact_words = _words(fact)
            if not fact_words or not query_words:
                continue
            overlap = len(query_words & fact_words)
            if overlap:
                scored_kw.append((overlap / len(query_words | fact_words), fact))
        scored_kw.sort(key=lambda item: item[0], reverse=True)
        self.last_path = "keyword"
        logger.warning(
            "Память: keyword-fallback, совпало {} из {} фактов",
            len(scored_kw),
            len(facts),
        )
        return [fact for _score, fact in scored_kw[:limit]]

    def retrieve(self, query: str, k: int | None = None) -> list[str]:
        """Структурированные виды + top-k фактов по смыслу."""
        limit = int(k if k is not None else self.k)
        text = str(query or "").strip()
        lowered = text.lower().replace("ё", "е")
        kinds: list[str] = []
        selected: list[str] = []

        if any(hint in lowered for hint in SCHEDULE_HINTS):
            schedule = _trim_lines(_entry_lines("schedule", self.vault_path))
            if schedule:
                selected.extend(schedule)
                kinds.append("schedule")

        if any(hint in lowered for hint in TASK_HINTS):
            tasks = _trim_lines(_entry_lines("tasks", self.vault_path))
            if tasks:
                selected.extend(tasks)
                kinds.append("tasks")

        facts = self._facts()
        if text and facts:
            selected.extend(self._rank_facts(text, facts, limit))
        elif kinds and not selected:
            self.last_path = "empty"

        if not selected:
            self.last_path = "empty"
            return []

        # Один и тот же факт мог попасть и в structured-блок, и в top-k.
        unique: list[str] = []
        seen: set[str] = set()
        for item in selected:
            key = " ".join(str(item).lower().split())
            if key in seen:
                continue
            seen.add(key)
            unique.append(str(item))
        if kinds:
            logger.info(
                "память: подобрано видов {} ({})",
                len(kinds),
                "/".join(kinds),
            )
        if not facts and kinds:
            self.last_path = "kinds"
        return unique

    def index_all(self) -> int:
        """Переиндексировать все факты vault. Возвращает число записей в кэше."""
        facts = self._facts()
        missing = [fact for fact in facts if self._cached_vector(fact) is None]
        if missing:
            batch = self.embed(missing)
            if batch is not None:
                vectors, model = batch
                for fact, vector in zip(missing, vectors):
                    self._store_vector(fact, vector, model)
        with closing(self._connect()) as connection:
            row = connection.execute("SELECT COUNT(*) FROM embeddings").fetchone()
        return int(row[0])
