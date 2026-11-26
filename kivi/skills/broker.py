"""Брокер скиллов: единственный способ дотянуться до внешнего мира.

Скилл НЕ получает os, subprocess, environ, socket и прямой open().
В handler передаётся только SkillContext, и каждый его метод сверяется
с permissions манифеста:

    permissions.network     → какие домены можно (http_get/http_post)
    permissions.filesystem  → какие пути-префиксы можно (fs_read/fs_write/fs_list)
    permissions.tools       → какие методы брокера вообще разрешены

Дополнительно и безусловно запрещены .env* и базы данных проекта
(data/*.db: kivi.db, skills.db) — даже если папка разрешена целиком.

Использование в handler.py:
    def handle(ctx, **args) -> str:
        return ctx.now().strftime("%H:%M")
"""

from __future__ import annotations

import os
from collections.abc import Callable
from datetime import datetime, timedelta, timezone
from pathlib import Path
from typing import Any
from urllib.parse import urlparse
from zoneinfo import ZoneInfo, ZoneInfoNotFoundError

import requests
from loguru import logger

from kivi.config import PROJECT_ROOT
from kivi.skills.registry import SkillManifest

__all__ = [
    "MAX_HTTP_BYTES",
    "MAX_LIST_ENTRIES",
    "MAX_READ_BYTES",
    "PermissionDenied",
    "SkillContext",
    "TZ_NAME",
    "project_timezone",
    "set_listen_callback",
    "set_overlay_service",
    "set_vision_router",
]

TZ_NAME = "Asia/Novosibirsk"
HTTP_TIMEOUT_DEFAULT = 15.0
MAX_HTTP_BYTES = 1_000_000  # 1 МБ на ответ сети
MAX_READ_BYTES = 1_000_000  # 1 МБ на чтение файла
MAX_LIST_ENTRIES = 5000  # предохранитель от обхода огромных папок
_DB_SUFFIXES = {".db", ".sqlite", ".sqlite3"}
_listen_callback: Callable[[bool], object] | None = None
_overlay_service: Any | None = None
_vision_router: Any | None = None


def set_listen_callback(callback: Callable[[bool], object] | None) -> None:
    """Подключить loop.set_listen к брокеру (для skills/listen_control)."""
    global _listen_callback
    _listen_callback = callback


def set_overlay_service(service: Any | None) -> None:
    """Подключить OverlayService для confirm-карточек."""
    global _overlay_service
    _overlay_service = service


def set_vision_router(router: Any | None) -> None:
    """Подключить Router ядра для skills/screen_look."""
    global _vision_router
    _vision_router = router


def project_timezone() -> timezone | ZoneInfo:
    """Часовой пояс проекта: Asia/Novosibirsk, при отсутствии tzdata — UTC+7."""
    try:
        return ZoneInfo(TZ_NAME)
    except ZoneInfoNotFoundError:  # noqa: PERF203 - без tzdata на Windows
        return timezone(timedelta(hours=7))


class PermissionDenied(PermissionError):
    """Скилл попытался сделать то, чего нет в его permissions."""


class SkillContext:
    """Узкий API брокера для скилла. Создаётся загрузчиком, не скиллом."""

    def __init__(self, manifest: SkillManifest, *, timeout: float = HTTP_TIMEOUT_DEFAULT) -> None:
        self.manifest = manifest
        self.project_root = PROJECT_ROOT
        self.notifications: list[str] = []
        self._http_timeout = float(timeout)

    # --- служебное ---

    @property
    def name(self) -> str:
        return self.manifest.name

    @property
    def risk(self) -> str:
        return self.manifest.permissions.risk

    def _require_tool(self, tool: str) -> None:
        allowed = list(self.manifest.permissions.tools)
        if tool not in allowed:
            raise PermissionDenied(
                f"скиллу «{self.name}» запрещён метод {tool}: "
                f"в permissions.tools {allowed or 'пусто'}"
            )

    @staticmethod
    def _resolve_path(path: str | Path) -> Path:
        candidate = Path(path)
        if not candidate.is_absolute():
            candidate = PROJECT_ROOT / candidate
        return candidate.resolve()

    @staticmethod
    def _secret_reason(resolved: Path) -> str | None:
        """Почему путь нельзя трогать даже при разрешённом префиксе."""
        for part in resolved.parts:
            lowered = part.lower()
            if lowered == ".env" or lowered.startswith(".env."):
                return "секреты (.env) недоступны скиллам"
        try:
            relative = resolved.relative_to(PROJECT_ROOT)
        except ValueError:
            relative = None
        if relative is not None and relative.parts and relative.parts[0].lower() == "data":
            if resolved.suffix.lower() in _DB_SUFFIXES:
                return "базы данных проекта закрыты для скиллов"
        return None

    @staticmethod
    def _within(resolved: Path, prefix: Path) -> bool:
        """Путь внутри префикса (Windows: без учёта регистра)."""
        target = os.path.normcase(str(resolved))
        base = os.path.normcase(str(prefix)).rstrip("\\/")
        return target == base or target.startswith(base + os.sep)

    def _check_fs(self, path: str | Path, tool: str) -> Path:
        """Проверить инструмент, запретные пути и префикс из permissions.filesystem."""
        self._require_tool(tool)
        resolved = self._resolve_path(path)
        reason = self._secret_reason(resolved)
        if reason is not None:
            raise PermissionDenied(f"запрещено обращаться к {resolved}: {reason}")
        allowed_paths = list(self.manifest.permissions.filesystem)
        for prefix in allowed_paths:
            if self._within(resolved, self._resolve_path(prefix)):
                return resolved
        raise PermissionDenied(
            f"скиллу «{self.name}» запрещён путь {resolved}: "
            f"разрешены только {allowed_paths or 'пусто'}"
        )

    def _check_network(self, url: str) -> str:
        """Домен из permissions.network (поддомены разрешённого домена тоже)."""
        parsed = urlparse(str(url))
        scheme = parsed.scheme.lower()
        if scheme not in ("http", "https"):
            raise PermissionDenied(
                f"скиллу «{self.name}» разрешены только http/https, а не {scheme or 'пусто'}"
            )
        host = (parsed.hostname or "").lower()
        if not host:
            raise PermissionDenied(f"в адресе {url!r} нет домена")
        allowed = [item.lower().lstrip(".") for item in self.manifest.permissions.network]
        if not any(host == domain or host.endswith("." + domain) for domain in allowed):
            raise PermissionDenied(
                f"скиллу «{self.name}» запрещён домен {host}: "
                f"разрешены {allowed or 'пусто'}"
            )
        return host

    # --- сеть ---

    def _request_text(
        self,
        method: str,
        url: str,
        timeout: float | None,
        payload: dict | None,
    ) -> str:
        host = self._check_network(url)
        limit = float(timeout if timeout is not None else self._http_timeout)
        try:
            with requests.request(
                method,
                url,
                json=payload,
                timeout=limit,
                allow_redirects=False,
                stream=True,
            ) as response:
                if 300 <= response.status_code < 400:
                    raise PermissionDenied(
                        f"{host} ответил редиректом {response.status_code} — переходы запрещены"
                    )
                if response.status_code >= 400:
                    raise RuntimeError(f"HTTP {response.status_code} от {host}")
                total = 0
                chunks: list[bytes] = []
                for chunk in response.iter_content(chunk_size=65536):
                    total += len(chunk)
                    if total > MAX_HTTP_BYTES:
                        raise RuntimeError(
                            f"ответ {host} больше {MAX_HTTP_BYTES} байт — обрезаю запрос"
                        )
                    chunks.append(chunk)
        except requests.RequestException as exc:
            raise RuntimeError(f"сеть недоступна ({host}): {type(exc).__name__}: {exc}") from exc
        return b"".join(chunks).decode("utf-8", errors="replace")

    def http_get(self, url: str, timeout: float | None = None) -> str:
        """GET только на домен из permissions.network. Возвращает текст ответа."""
        self._require_tool("http_get")
        return self._request_text("GET", url, timeout, None)

    def http_post(self, url: str, json: dict | None = None, timeout: float | None = None) -> str:
        """POST на домен из permissions.network. Возвращает текст ответа."""
        self._require_tool("http_post")
        return self._request_text("POST", url, timeout, json)

    # --- файловая система ---

    def fs_read(self, path: str | Path) -> str:
        """Прочитать текстовый файл внутри разрешённого префикса."""
        resolved = self._check_fs(path, "fs_read")
        if not resolved.is_file():
            raise FileNotFoundError(f"файл не найден: {resolved}")
        size = resolved.stat().st_size
        if size > MAX_READ_BYTES:
            raise RuntimeError(f"файл {resolved.name} слишком большой ({size} байт)")
        return resolved.read_text(encoding="utf-8", errors="replace")

    def fs_write(self, path: str | Path, text: str) -> str:
        """Записать текст в файл внутри разрешённого префикса (папки создаст)."""
        resolved = self._check_fs(path, "fs_write")
        resolved.parent.mkdir(parents=True, exist_ok=True)
        resolved.write_text(str(text), encoding="utf-8")
        logger.info("скилл {}: записал {} ({} симв.)", self.name, resolved, len(str(text)))
        return str(resolved)

    def fs_list(self, dir: str | Path, *, recursive: bool = False) -> list[str]:  # noqa: A002
        """Список файлов в разрешённой папке (абсолютные пути), секреты скрыты."""
        resolved = self._check_fs(dir, "fs_list")
        if not resolved.is_dir():
            raise NotADirectoryError(f"не папка: {resolved}")

        files: list[str] = []
        if recursive:
            for root, _dirs, names in os.walk(resolved):
                for name in sorted(names):
                    candidate = Path(root) / name
                    if self._secret_reason(candidate) is not None:
                        continue
                    files.append(str(candidate))
                    if len(files) >= MAX_LIST_ENTRIES:
                        return sorted(files)
        else:
            for candidate in sorted(resolved.iterdir()):
                if not candidate.is_file():
                    continue
                if self._secret_reason(candidate) is not None:
                    continue
                files.append(str(candidate))
        return sorted(files)

    # --- своя громкость ---

    def get_self_volume(self) -> float:
        """Текущая громкость голоса Киви (0..1). Нужен tool self_volume."""
        self._require_tool("self_volume")
        from kivi.prefs import get_tts_volume

        return get_tts_volume()

    def set_self_volume(self, value: float) -> float:
        """Изменить громкость голоса Киви (0..1). Нужен tool self_volume."""
        self._require_tool("self_volume")
        from kivi.prefs import set_tts_volume

        return set_tts_volume(value)

    # --- память через vault (skills/remember) ---

    def _memory_root(self) -> Path:
        """Проверить, что скиллу разрешён vault, и вернуть его корень."""
        self._require_tool("self_memory")
        from kivi.memory.vault import vault_root

        root = vault_root().resolve()
        allowed = [self._resolve_path(item) for item in self.manifest.permissions.filesystem]
        if not any(self._within(root, prefix) for prefix in allowed):
            raise PermissionDenied(
                f"скиллу «{self.name}» запрещён vault {root}: "
                f"разрешены только {allowed or 'пусто'}"
            )
        return root

    def vault_add_schedule(self, when: str, what: str) -> bool:
        """Добавить событие в memory/schedule.md. Нужен tool self_memory."""
        root = self._memory_root()
        from kivi.memory.vault import add_schedule

        return bool(add_schedule(when, what, path=root))

    def vault_add_schedule_line(self, line: str) -> bool:
        """Добавить готовую каноническую строку расписания. Нужен tool self_memory."""
        root = self._memory_root()
        from kivi.memory.vault import add_schedule_line

        return bool(add_schedule_line(line, path=root))

    def vault_add_task(self, what: str) -> bool:
        """Добавить задачу в memory/tasks.md. Нужен tool self_memory."""
        root = self._memory_root()
        from kivi.memory.vault import add_task

        return bool(add_task(what, "open", path=root))

    def vault_add_preference(self, what: str) -> bool:
        """Добавить предпочтение в memory/profile.md. Нужен tool self_memory."""
        root = self._memory_root()
        from kivi.memory.vault import add_preference

        return bool(add_preference(what, path=root))

    def vault_add_fact(self, what: str) -> bool:
        """Добавить факт в memory/profile.md. Нужен tool self_memory."""
        root = self._memory_root()
        from kivi.memory.vault import add_fact

        return bool(add_fact(what, kind="profile", path=root))

    # --- напоминания ---

    def scheduler(self, time: str, what: str) -> str:
        """Записать напоминание. Нужен tool scheduler."""
        self._require_tool("scheduler")
        from kivi.skills.reminders import add_reminder

        return add_reminder(str(time), str(what))

    def reminders_list(self) -> list[dict]:
        """Ближайшие pending-напоминания. Нужен tool reminders_read."""
        self._require_tool("reminders_read")
        from kivi.skills.reminders import list_reminders

        return list_reminders()

    def extract_keywords(self, command: str) -> dict[str, list[str]]:
        """LLM cheap/intent: keywords и date_refs для recall. Нужен llm_intent."""
        self._require_tool("llm_intent")
        from kivi.skills.recall import extract_entities

        entities = extract_entities(str(command))
        return {"keywords": entities.keywords, "date_refs": entities.date_refs}

    def extract_schedule(self, command: str) -> dict[str, str]:
        """LLM cheap/intent: days/start/end/what события. Нужен llm_intent."""
        self._require_tool("llm_intent")
        from kivi.skills.schedule_extract import extract_schedule

        return dict(extract_schedule(str(command)) or {})

    def reminders_clear(self) -> int:
        """Удалить pending-напоминания. Нужен tool reminders_write."""
        self._require_tool("reminders_write")
        from kivi.skills.reminders import clear_reminders

        return int(clear_reminders())

    # --- управление прослушкой / подтверждения / каталог ---

    def set_listen(self, enabled: bool) -> bool:
        """Включить/выключить обработку VAD-фраз. Нужен tool listen."""
        self._require_tool("listen")
        callback = _listen_callback
        if callback is None:
            raise RuntimeError("loop.set_listen недоступен")
        return bool(callback(bool(enabled)))

    def confirm(self, title: str, body: str = "", details: str = "") -> bool:
        """Показать confirm-карточку. Нет UI -> False. Нужен tool confirm."""
        self._require_tool("confirm")
        service = _overlay_service
        if service is None:
            return False
        try:
            return bool(service.confirm(str(title), str(body), str(details)))
        except Exception as exc:  # noqa: BLE001 - отсутствие UI не должно падать
            logger.warning("скилл {}: confirm недоступен: {}", self.name, exc)
            return False

    def screenshot(self):
        """Снять основной монитор. Нужен tool screenshot."""
        self._require_tool("screenshot")
        from kivi.vision.screen import capture_screen

        return capture_screen()

    def vision(self, image_png: bytes, prompt: str) -> dict[str, Any]:
        """Отправить PNG в vision-тир Router. Нужен tool vision."""
        self._require_tool("vision")
        router = _vision_router
        if router is None:
            raise RuntimeError("vision Router недоступен")
        import base64

        data_url = "data:image/png;base64," + base64.b64encode(bytes(image_png)).decode(
            "ascii"
        )
        messages = [
            {
                "role": "user",
                "content": [
                    {"type": "text", "text": str(prompt)},
                    {"type": "image_url", "image_url": {"url": data_url}},
                ],
            }
        ]
        result = router.complete(messages, task_type="vision")
        return dict(result or {})

    def skills_list(self) -> list[dict[str, str]]:
        """Включённые скиллы для capabilities. Нужен tool skills_read."""
        self._require_tool("skills_read")
        from kivi.skills.registry import get_registry

        return [
            {
                "name": manifest.name,
                "description": manifest.description,
                "category": manifest.category,
            }
            for manifest in get_registry().list_enabled()
        ]

    # --- безобидные методы: доступны всегда ---

    def notify(self, text: str) -> None:
        """Показать пользователю сообщение (лог; позже — оверлей)."""
        message = " ".join(str(text).split())
        if not message:
            return
        self.notifications.append(message)
        logger.info("скилл {}: {}", self.name, message)

    def ask(self, question: str) -> None:
        """Вопрос пользователю. Оверлея ещё нет, ответа тоже — возвращает None."""
        logger.debug("скилл {}: вопрос остался без ответа: {}", self.name, question)
        return None

    def now(self) -> datetime:
        """Текущее время в часовом поясе проекта."""
        return datetime.now(project_timezone())

    def log(self, msg: str) -> None:
        """Отладочная запись в лог от имени скилла."""
        logger.debug("[{}] {}", self.name, msg)
