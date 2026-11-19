"""Провайдеры LLM: Polza (OpenAI-совместимый HTTP) и CLI opencode.

Ключ берётся из окружения (.env подгружается штатно, как в tests/check_api.py);
значения ключей НЕ читаются в код логов и НЕ печатаются.
"""

from __future__ import annotations

import json
import os
import shutil
import subprocess
from dataclasses import dataclass
from pathlib import Path
from typing import Iterator

import requests
from dotenv import load_dotenv

from kivi.log import logger

__all__ = [
    "OPCODE_TIMEOUT",
    "OpenCodeCLI",
    "PolzaProvider",
    "ProviderError",
    "ProviderUnavailable",
]

RETRYABLE_STATUSES = {402, 429, 503}  # нет денег / лимит / сервис недоступен
OPCODE_TIMEOUT = 600.0
OPCODE_BIN = "opencode"
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
        payload: dict = {"model": model, "messages": messages}
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
            "messages": messages,
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


class OpenCodeCLI:
    """Локальный агент opencode: `opencode run <prompt>` в указанной папке."""

    name = "opencode_cli"

    def __init__(
        self, *, executable: str = OPCODE_BIN, timeout: float = OPCODE_TIMEOUT
    ) -> None:
        self.executable = executable
        self.timeout = float(timeout)

    @property
    def available(self) -> bool:
        return shutil.which(self.executable) is not None

    def complete_code(self, prompt: str, workdir: str | Path) -> dict:
        """Выполнить задачу в папке workdir. Возвращает text/tokens/cost_rub/raw."""
        text = str(prompt).strip()
        if not text:
            raise ProviderError("opencode: пустой промпт")

        binary = shutil.which(self.executable)
        if binary is None:
            raise ProviderUnavailable(
                f"{self.executable} не найден в PATH. Установи opencode (или добавь его "
                "в PATH) — до этого задачи code будут уходить в облако по brain.escalation."
            )

        directory = Path(workdir)
        if not directory.exists():
            raise ProviderError(f"opencode: рабочая папка не найдена: {directory}")

        command = [binary, "run", text]
        logger.debug("opencode: запуск в {}", directory)
        try:
            completed = subprocess.run(
                command,
                cwd=str(directory),
                capture_output=True,
                timeout=self.timeout,
                check=False,
            )
        except subprocess.TimeoutExpired as exc:
            raise ProviderUnavailable(
                f"opencode не ответил за {self.timeout:.0f} c"
            ) from exc
        except OSError as exc:
            raise ProviderUnavailable(f"не удалось запустить opencode: {exc}") from exc

        stdout = completed.stdout.decode("utf-8", errors="replace").strip()
        stderr = completed.stderr.decode("utf-8", errors="replace").strip()
        if completed.returncode != 0:
            raise ProviderError(
                f"opencode завершился с кодом {completed.returncode}: "
                f"{(stderr or stdout)[:300]}"
            )
        if not stdout:
            raise ProviderError("opencode ничего не вывел")

        return {
            "text": stdout,
            "tokens_in": 0,
            "tokens_out": 0,
            "cost_rub": 0.0,
            "raw": {"returncode": completed.returncode, "stderr": stderr},
        }
