"""Провайдеры LLM: Polza (OpenAI-совместимый HTTP) и CLI opencode.

Ключ берётся из окружения (.env подгружается штатно, как в tests/check_api.py);
значения ключей НЕ читаются в код логов и НЕ печатаются.
"""

from __future__ import annotations

import json
import os
import re
import shutil
import subprocess
import time
import uuid
from dataclasses import dataclass
from pathlib import Path
from typing import Iterator

import requests
from dotenv import load_dotenv

from kivi.log import logger

__all__ = [
    "DEFAULT_OPCODE_WORKDIR",
    "OPCODE_TIMEOUT",
    "OpenCodeCLI",
    "PolzaProvider",
    "ProviderError",
    "ProviderUnavailable",
    "clean_opencode_stdout",
]

RETRYABLE_STATUSES = {402, 429, 503}  # нет денег / лимит / сервис недоступен
OPCODE_TIMEOUT = 600.0
OPCODE_BIN = "opencode"
DEFAULT_OPCODE_WORKDIR = "data/opencode_jobs"
SERVICE_LINE_PREFIXES = ("> ", "$ ")  # приглашение и эхо команды самого CLI
_ANSI_RE = re.compile(r"\x1b\[[0-9;]*[A-Za-z]")
DEFAULT_TIMEOUT = 120.0


class ProviderUnavailable(RuntimeError):
    """Провайдер недоступен: 402/429/503, сеть, таймаут, нет бинарника."""


class ProviderError(RuntimeError):
    """Провайдер ответил ошибкой или мусором."""


@dataclass(frozen=True)
class StreamUsage:
    """Расход одного стримингового запроса."""

    tokens_in: int = 0
    tokens_out: int = 0
    cost_rub: float | None = None
    estimated: bool = False


def _credentials() -> tuple[str, str]:
    """Адрес API и ключ из окружения. Значения ключа не печатаем никогда."""
    load_dotenv()
    base = (os.getenv("LLM_BASE_URL") or "").strip()
    key = (os.getenv("LLM_API_KEY") or "").strip()
    if not base or not key:
        raise ProviderUnavailable(
            "Не заданы LLM_BASE_URL и/или LLM_API_KEY в окружении (.env)."
        )
    return base.rstrip("/"), key


def _copy_messages(messages: list[dict]) -> list[dict]:
    """Скопировать messages, сохранив мультимодальный content-массив.

    Для vision допускаются части ``{"type": "text", "text": ...}`` и
    ``{"type": "image_url", "image_url": {"url": "data:image/png;base64,..."}}``.
    Содержимое изображения не логируется.
    """
    prepared: list[dict] = []
    for item in messages:
        if not isinstance(item, dict):
            raise ProviderError("сообщение должно быть объектом")
        message = dict(item)
        content = message.get("content")
        if isinstance(content, list):
            parts: list[dict] = []
            for part in content:
                if not isinstance(part, dict):
                    raise ProviderError("часть content должна быть объектом")
                kind = str(part.get("type") or "")
                if kind == "text":
                    parts.append({"type": "text", "text": str(part.get("text") or "")})
                elif kind == "image_url":
                    image = part.get("image_url")
                    url = image.get("url") if isinstance(image, dict) else ""
                    if not isinstance(url, str) or not url.strip():
                        raise ProviderError("image_url.url пуст")
                    parts.append(
                        {"type": "image_url", "image_url": {"url": url.strip()}}
                    )
                else:
                    raise ProviderError(f"неизвестный тип content: {kind!r}")
            if not parts:
                raise ProviderError("пустой content-массив")
            message["content"] = parts
        prepared.append(message)
    return prepared


class PolzaProvider:
    """Облачный провайдер, OpenAI-совместимый /chat/completions."""

    name = "polza"

    def __init__(
        self, *, base_url: str | None = None, timeout: float = DEFAULT_TIMEOUT
    ) -> None:
        self._base_url = base_url.rstrip("/") if base_url else None
        self.timeout = float(timeout)

    @property
    def base_url(self) -> str:
        return self._base_url or _credentials()[0]

    def complete(
        self,
        messages: list[dict],
        model: str,
        tools: list[dict] | None = None,
        timeout: float | None = None,
    ) -> dict:
        """Один запрос к модели. Возвращает text/tokens/cost_rub/raw."""
        _, key = _credentials()
        url = f"{self.base_url}/chat/completions"
        prepared = _copy_messages(messages)
        payload: dict = {"model": model, "messages": prepared}
        if tools:
            payload["tools"] = tools

        logger.debug("Polza: model={}, сообщений={}", model, len(messages))
        try:
            response = requests.post(
                url,
                headers={"Authorization": f"Bearer {key}"},
                json=payload,
                timeout=timeout if timeout is not None else self.timeout,
            )
        except requests.RequestException as exc:
            raise ProviderUnavailable(f"нет связи с {url}: {exc}") from exc

        if response.status_code in RETRYABLE_STATUSES:
            raise ProviderUnavailable(
                f"HTTP {response.status_code} от {url}: {response.text[:200]}"
            )
        if response.status_code != 200:
            raise ProviderError(
                f"HTTP {response.status_code} от {url}: {response.text[:200]}"
            )

        try:
            body = response.json()
            choice = body["choices"][0]
            message = choice.get("message") or {}
            text = message.get("content") or ""
            usage = body.get("usage") or {}
        except (ValueError, KeyError, IndexError, TypeError, AttributeError) as exc:
            raise ProviderError(
                f"неожиданный ответ {url}: {response.text[:200]}"
            ) from exc

        cost = usage.get("cost_rub")
        if cost is None:
            cost = usage.get("cost")
        return {
            "text": str(text).strip(),
            "tokens_in": int(usage.get("prompt_tokens") or 0),
            "tokens_out": int(usage.get("completion_tokens") or 0),
            "cost_rub": float(cost) if cost is not None else None,
            "raw": body,
        }

    def stream(
        self,
        messages: list[dict],
        model: str,
        *,
        timeout: float | None = None,
    ) -> Iterator[str]:
        """SSE-стрим ответа: yield дельт текста, в конце return StreamUsage."""
        _, key = _credentials()
        url = f"{self.base_url}/chat/completions"
        payload = {
            "model": model,
            "messages": _copy_messages(messages),
            "stream": True,
            "stream_options": {"include_usage": True},
        }
        logger.debug("Polza stream: model={}, сообщений={}", model, len(messages))

        collected: list[str] = []
        usage_raw: dict | None = None

        try:
            response = requests.post(
                url,
                headers={"Authorization": f"Bearer {key}"},
                json=payload,
                timeout=timeout if timeout is not None else self.timeout,
                stream=True,
            )
        except requests.RequestException as exc:
            raise ProviderUnavailable(f"нет связи с {url}: {exc}") from exc

        try:
            if response.status_code in RETRYABLE_STATUSES:
                raise ProviderUnavailable(
                    f"HTTP {response.status_code} от {url}: {response.text[:200]}"
                )
            if response.status_code != 200:
                raise ProviderError(
                    f"HTTP {response.status_code} от {url}: {response.text[:200]}"
                )

            for raw_line in response.iter_lines(decode_unicode=False):
                if not raw_line:
                    continue
                line = raw_line.decode("utf-8", errors="replace").strip()
                if not line.startswith("data:"):
                    continue
                data = line[len("data:") :].strip()
                if data == "[DONE]":
                    break
                try:
                    event = json.loads(data)
                except json.JSONDecodeError:
                    logger.debug("Polza stream: пропускаю не-JSON строку: {}", line[:120])
                    continue

                if isinstance(event, dict) and event.get("usage"):
                    usage_raw = event["usage"]

                choices = (event or {}).get("choices") or []
                if not choices:
                    continue
                delta = choices[0].get("delta") or {}
                piece = delta.get("content")
                if piece:
                    collected.append(piece)
                    yield piece
        finally:
            try:
                response.close()
            except Exception:  # noqa: BLE001
                pass

        text = "".join(collected)
        if usage_raw is None:
            tokens_in = max(1, len(str(messages)) // 4)
            tokens_out = max(1, len(text) // 3)
            logger.warning(
                "Polza stream: usage не пришёл — оценил по длине текста "
                "(in={}, out={}); расход будет примерным",
                tokens_in,
                tokens_out,
            )
            return StreamUsage(tokens_in, tokens_out, None, estimated=True)

        cost = usage_raw.get("cost_rub")
        if cost is None:
            cost = usage_raw.get("cost")
        return StreamUsage(
            tokens_in=int(usage_raw.get("prompt_tokens") or 0),
            tokens_out=int(usage_raw.get("completion_tokens") or 0),
            cost_rub=float(cost) if cost is not None else None,
            estimated=False,
        )


# --- opencode CLI: вспомогательное ---


def _strip_ansi(text: str) -> str:
    """Убрать ANSI-цвета: они лезут в stderr CLI и портят текст ошибок."""
    return _ANSI_RE.sub("", text)


def _as_text(value: str | bytes | None) -> str:
    """stdout/stderr как строка (bytes бывают при ручном запуске и в моках)."""
    if value is None:
        return ""
    if isinstance(value, bytes):
        return value.decode("utf-8", errors="replace")
    return str(value)


def clean_opencode_stdout(raw: str | bytes | None) -> str:
    """Ответ модели из stdout: без служебных строк ("> ", "$ ") и пустых краёв."""
    text = _strip_ansi(_as_text(raw)).replace("\r\n", "\n").replace("\r", "\n")
    kept: list[str] = []
    for line in text.split("\n"):
        stripped = line.lstrip()
        if stripped.startswith(SERVICE_LINE_PREFIXES) or stripped in (">", "$"):
            continue  # приглашение/эхо команды — не ответ
        kept.append(line.rstrip())
    while kept and not kept[0].strip():
        kept.pop(0)
    while kept and not kept[-1].strip():
        kept.pop()
    return "\n".join(kept)


def _jobs_root_from_config() -> Path:
    """Папка задач: brain.opencode_workdir из config.yaml, иначе data/opencode_jobs."""
    root = Path(__file__).resolve().parents[2]
    raw = ""
    try:
        from kivi.config import PROJECT_ROOT, load_config

        root = PROJECT_ROOT
        brain = load_config().model_dump().get("brain") or {}
        raw = str(brain.get("opencode_workdir") or "").strip()
    except Exception as exc:  # noqa: BLE001 - конфиг не должен валить провайдера
        logger.debug(
            "opencode: brain.opencode_workdir не прочитан ({}), беру {}",
            exc,
            DEFAULT_OPCODE_WORKDIR,
        )
    path = Path(raw) if raw else Path(DEFAULT_OPCODE_WORKDIR)
    return path if path.is_absolute() else root / path


def _resolve_executable(name: str) -> str | None:
    """Путь к исполняемому файлу.

    На Windows npm ставит рядом .cmd-обёртку: CreateProcess не запускает .cmd по
    «голому» имени, поэтому предпочитаем файл с расширением.
    """
    if os.name == "nt":
        for suffix in (".cmd", ".exe", ".bat"):
            found = shutil.which(name + suffix)
            if found:
                return found
    return shutil.which(name)


class OpenCodeCLI:
    """Локальный агент opencode: `opencode run <prompt>` в папке задачи.

    Папка задачи по умолчанию — brain.opencode_workdir/<uuid8>, то есть
    data/opencode_jobs/<uuid8>: у каждой задачи свой рабочий каталог.
    """

    name = "opencode_cli"

    def __init__(
        self,
        *,
        executable: str = OPCODE_BIN,
        timeout: float = OPCODE_TIMEOUT,
        jobs_root: str | Path | None = None,
    ) -> None:
        self.executable = executable
        self.timeout = float(timeout)
        self.jobs_root = (
            Path(jobs_root) if jobs_root is not None else _jobs_root_from_config()
        )

    @property
    def available(self) -> bool:
        return _resolve_executable(self.executable) is not None

    def _make_job_dir(self, workdir: str | Path | None) -> Path:
        """Папка задачи: явно заданная или <jobs_root>/<uuid8> на каждый запуск."""
        directory = (
            Path(workdir)
            if workdir is not None
            else self.jobs_root / uuid.uuid4().hex[:8]
        )
        try:
            directory.mkdir(parents=True, exist_ok=True)
        except OSError as exc:
            raise ProviderUnavailable(
                f"не удалось создать папку задачи {directory}: {exc}"
            ) from exc
        return directory

    def complete_code(self, prompt: str, workdir: str | Path | None = None) -> dict:
        """Выполнить задачу кодом. Возвращает text/tokens/cost_rub/raw (cost = 0)."""
        text = str(prompt).strip()
        if not text:
            raise ProviderError("opencode: пустой промпт")

        binary = _resolve_executable(self.executable)
        if binary is None:
            raise ProviderUnavailable(
                f"{self.executable} не найден в PATH. Установи opencode (или добавь "
                "его в PATH) — до этого задачи code будут уходить в облако по "
                "brain.escalation."
            )

        directory = self._make_job_dir(workdir)
        # argv[0] остаётся "opencode" (как в документации CLI), а .cmd-обёртку
        # Windows запускает через lpApplicationName — параметр executable.
        command = [self.executable, "run", text]
        run_kwargs: dict = {
            "cwd": str(directory),
            "capture_output": True,
            "text": True,
            "encoding": "utf-8",
            "errors": "replace",
            "timeout": self.timeout,
        }
        if binary != self.executable:
            run_kwargs["executable"] = binary
        # env не передаём: процесс наследует окружение и берёт модель из
        # ~/.config/opencode/opencode.json. Значения ключей не логируем.

        logger.debug(
            "opencode: запуск в {} (timeout {:.0f} c)", directory, self.timeout
        )
        started = time.perf_counter()
        try:
            completed = subprocess.run(command, **run_kwargs)
        except subprocess.TimeoutExpired as exc:
            detail = _strip_ansi(_as_text(getattr(exc, "stderr", None))).strip()[:300]
            suffix = f": {detail}" if detail else ""
            raise ProviderUnavailable(
                f"opencode не ответил за {self.timeout:.0f} c{suffix}"
            ) from exc
        except OSError as exc:
            raise ProviderUnavailable(f"не удалось запустить opencode: {exc}") from exc
        elapsed = time.perf_counter() - started

        stdout = _as_text(completed.stdout)
        stderr = _strip_ansi(_as_text(completed.stderr)).strip()
        if completed.returncode != 0:
            detail = (stderr or _strip_ansi(stdout).strip())[:300]
            raise ProviderUnavailable(
                f"opencode завершился с кодом {completed.returncode}: {detail}"
            )

        answer = clean_opencode_stdout(stdout)
        if not answer.strip():
            raise ProviderUnavailable(
                f"opencode ничего не вывел (папка {directory}, stderr: {stderr[:300]})"
            )

        logger.info(
            "opencode: ответ получен за {:.1f} c, символов {}, папка {}",
            elapsed,
            len(answer),
            directory.name,
        )
        return {
            "text": answer,
            "tokens_in": 0,
            "tokens_out": 0,
            "cost_rub": 0.0,
            "raw": {
                "returncode": completed.returncode,
                "stderr": stderr[:300],
                "workdir": str(directory),
                "seconds": round(elapsed, 2),
            },
        }
