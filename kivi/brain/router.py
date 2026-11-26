"""Каскадный роутер LLM: тиры, эскалация по провалам, учёт бюджета.

task_type: chat | intent | compose | summarize | memory | code | vision
Маршрут берётся из brain.routing, модели — из brain.tiers, лимиты эскалации —
из brain.escalation, деньги — в Budget (data/kivi.db).

Порядок эскалации: cheap (max_attempts_cheap) -> coder (max_attempts_coder) -> strong.
Каждый переход пишется в лог: "эскалация: cheap -> coder, причина: ...".
"""

from __future__ import annotations

from collections.abc import Callable
from pathlib import Path
from typing import Any

from kivi.brain.budget import Budget, BudgetExceeded
from kivi.brain.providers import (
    OpenCodeCLI,
    PolzaProvider,
    ProviderError,
    ProviderUnavailable,
)
from kivi.config import Settings, get_settings
from kivi.log import logger

__all__ = ["Router", "RouterError", "TASK_TYPES", "complete_safe", "stream_safe"]

TASK_TYPES = ("chat", "intent", "compose", "summarize", "memory", "code", "vision")

DEFAULT_TIERS = {
    "cheap": "deepseek/deepseek-v4.1-flash-1017",
    "coder": "google/gemini-4-hydrogen",
    "vision": "openai/gpt-6.1-terra",
    "strong": "anthropic/claude-sonnet-5.5",
}
DEFAULT_ROUTING = {
    "chat": "cheap",
    "intent": "cheap",
    "compose": "cheap",
    "summarize": "cheap",
    "memory": "cheap",
    "code": "opencode_cli",
    "vision": "vision",
}
DEFAULT_ESCALATION = {
    "max_attempts_cheap": 3,
    "then": "coder",
    "max_attempts_coder": 2,
    "then_next": "strong",
}

Verify = Callable[[str], bool]

# Обрезка по max_tokens — не провал: режем до границы предложения.
SENTENCE_END = ".!?…"
MIN_ANSWER_CHARS = 2
CYRILLIC_MIN_RATIO = 0.3


def _trim_to_sentence(text: str) -> str:
    """Обрезать текст до последней границы предложения (иначе до пробела)."""
    value = str(text or "").rstrip()
    if not value:
        return ""
    cut = max(value.rfind(ch) for ch in SENTENCE_END)
    if cut >= 0:
        return value[: cut + 1].strip()
    space = value.rfind(" ")
    if space > 0:
        return value[:space].strip()
    return value


def _is_russian_enough(value: str) -> bool:
    letters = [ch for ch in value if ch.isalpha()]
    if not letters:
        return False
    cyrillic = sum(1 for ch in letters if ch.lower() in "абвгдеёжзийклмнопрстуфхцчшщъыьэюя")
    return cyrillic / len(letters) >= CYRILLIC_MIN_RATIO


def default_verify(text: str, task_type: str = "chat") -> bool:
    """Провал только на пустом/мусорном/не-русском ответе (chat, summarize)."""
    value = " ".join(str(text or "").split())
    if len(value) < MIN_ANSWER_CHARS:
        return False
    if not any(ch.isalpha() for ch in value):
        return False
    if task_type in ("chat", "summarize"):
        return _is_russian_enough(value)
    return True


def complete_safe(
    router: Any,
    messages: Any,
    *,
    task_type: str = "chat",
    max_tokens: int | None = None,
) -> dict:
    """router.complete с max_tokens; старые/мок-роутеры вызываются без него."""
    try:
        return router.complete(messages, task_type=task_type, max_tokens=max_tokens)
    except TypeError as exc:
        if "max_tokens" not in str(exc):
            raise
        return router.complete(messages, task_type=task_type)


def stream_safe(
    router: Any,
    messages: Any,
    *,
    task_type: str = "chat",
    max_tokens: int | None = None,
):
    """router.complete_stream с max_tokens; без поддержки — обычный вызов."""
    try:
        return router.complete_stream(
            messages, task_type=task_type, max_tokens=max_tokens
        )
    except TypeError as exc:
        if "max_tokens" not in str(exc):
            raise
        return router.complete_stream(messages, task_type=task_type)


class RouterError(RuntimeError):
    """Все попытки провалены (или оркестратор недоступен)."""

    def __init__(
        self,
        message: str,
        *,
        tier: str | None = None,
        attempts: int = 0,
        last_error: BaseException | None = None,
    ) -> None:
        super().__init__(message)
        self.tier = tier
        self.attempts = attempts
        self.last_error = last_error


class Router:
    """Выбирает модель под задачу, эскалирует по провалам и пишет расходы."""

    def __init__(
        self,
        settings: Settings | None = None,
        *,
        budget: Budget | None = None,
        provider: PolzaProvider | None = None,
        cli: OpenCodeCLI | None = None,
        workdir: str | Path | None = None,
    ) -> None:
        self.settings = settings if settings is not None else get_settings()
        brain = self.settings.section("brain")

        self.tiers: dict[str, str] = dict(DEFAULT_TIERS)
        self.tiers.update({str(k): str(v) for k, v in (brain.get("tiers") or {}).items()})
        self.routing: dict[str, str] = dict(DEFAULT_ROUTING)
        self.routing.update(
            {str(k): str(v) for k, v in (brain.get("routing") or {}).items()}
        )
        self.escalation: dict[str, Any] = dict(DEFAULT_ESCALATION)
        self.escalation.update(brain.get("escalation") or {})
        self.orchestrator_model = str(
            brain.get("orchestrator") or self.tiers.get("cheap", DEFAULT_TIERS["cheap"])
        )

        self.budget = budget if budget is not None else Budget(self.settings)
        self.provider = provider if provider is not None else PolzaProvider()
        self.cli = cli if cli is not None else OpenCodeCLI()
        # None — папку задачи создаёт OpenCodeCLI в brain.opencode_workdir
        # (data/opencode_jobs/<uuid8>), чтобы каждая задача шла в своей папке.
        self.workdir = Path(workdir) if workdir is not None else None
        logger.info(
            "Router готов: tiers={}, routing={}, бюджет: {}",
            self.tiers,
            self.routing,
            self.budget.describe(),
        )

    @staticmethod
    def _prepare(messages: str | list[dict] | dict) -> list[dict]:
        """Привести вход к списку сообщений OpenAI."""
        if isinstance(messages, str):
            text = messages.strip()
            if not text:
                raise ValueError("Router: пустой текст запроса")
            return [{"role": "user", "content": text}]
        if isinstance(messages, dict):
            return [dict(messages)]
        if isinstance(messages, (list, tuple)) and messages:
            prepared = [dict(item) for item in messages]
            if not all("role" in item and "content" in item for item in prepared):
                raise ValueError("Router: каждое сообщение должно содержать role и content")
            for item in prepared:
                content = item.get("content")
                if isinstance(content, list):
                    if not content:
                        raise ValueError("Router: пустой content-массив")
                    for part in content:
                        if not isinstance(part, dict):
                            raise ValueError("Router: часть content должна быть объектом")
                        kind = str(part.get("type") or "")
                        if kind == "text":
                            if not isinstance(part.get("text"), str):
                                raise ValueError("Router: text-часть без строки text")
                        elif kind == "image_url":
                            image = part.get("image_url")
                            url = image.get("url") if isinstance(image, dict) else None
                            if not isinstance(url, str) or not url.strip():
                                raise ValueError("Router: image_url.url пуст")
                        else:
                            raise ValueError(
                                f"Router: неизвестный тип content {kind!r}"
                            )
            return prepared
        raise ValueError("Router: ожидались строка, dict или непустой список сообщений")

# --- ВЫЗОВЫ ---

    def complete(
        self,
        messages: str | list[dict] | dict,
        task_type: str = "chat",
        verify: Verify | None = None,
        *,
        workdir: str | Path | None = None,
        tools: list[dict] | None = None,
        max_tokens: int | None = None,
    ) -> dict:
        """Выполнить запрос по правилам роутера. Возвращает dict с текстом и метаданными.

        max_tokens ограничивает OUT-токены (None — как решит провайдер).
        """
        task = str(task_type).strip().lower()
        if task not in TASK_TYPES:
            raise ValueError(
                f"Router: неизвестный task_type={task_type!r}, ожидалось одно из {TASK_TYPES}"
            )
        prepared = self._prepare(messages)
        route = str(self.routing.get(task, "cheap"))
        logger.info(
            "Router: task={} -> маршрут {} (max_tokens={})", task, route, max_tokens
        )

        if task == "intent":
            # Оркестратор всегда на Polza cheap, без эскалации вниз.
            return self._call_orchestrator(prepared, verify, max_tokens=max_tokens)

        if task == "code" or route == "opencode_cli":
            return self._run_code(prepared, task, verify, workdir, tools)

        start_tier = route if route in self.tiers else "cheap"
        return self._escalate(
            prepared, start_tier, task, verify, tools, max_tokens=max_tokens
        )

    def complete_stream(
        self,
        messages: str | list[dict] | dict,
        task_type: str = "chat",
        *,
        max_tokens: int | None = None,
    ):
        """Стрим ответа текстом. Бюджет пишется один раз в конце, эскалации нет."""
        task = str(task_type).strip().lower()
        if task not in TASK_TYPES:
            raise ValueError(
                f"Router: неизвестный task_type={task_type!r}, ожидалось одно из {TASK_TYPES}"
            )
        prepared = self._prepare(messages)
        route = str(self.routing.get(task, "cheap"))
        tier = route if route in self.tiers else "cheap"
        model = self.tiers.get(tier)
        if not model:
            raise RouterError(f"тир {tier} не настроен в brain.tiers", tier=tier)

        self._ensure_budget()
        logger.info("Router stream: task={} -> тир {} ({})", task, tier, model)

        stream_usage = None
        collected: list[str] = []
        try:
            generator = self.provider.stream(prepared, model, max_tokens=max_tokens)
            while True:
                try:
                    piece = next(generator)
                except StopIteration as stop:
                    stream_usage = stop.value
                    break
                collected.append(piece)
                yield piece
        finally:
            text = "".join(collected)
            tokens_in = int(getattr(stream_usage, "tokens_in", 0) or 0)
            tokens_out = int(getattr(stream_usage, "tokens_out", 0) or 0)
            cost = getattr(stream_usage, "cost_rub", None)
            if not tokens_out:
                tokens_out = max(1, len(text) // 3)
            if not tokens_in:
                tokens_in = max(1, len(str(prepared)) // 4)
            written = self.budget.record(
                provider=self.provider.name,
                model=model,
                task_type=task,
                tokens_in=tokens_in,
                tokens_out=tokens_out,
                cost_rub=cost,
                ok=bool(text.strip()),
                attempt=1,
            )
            logger.debug(
                "Router stream: бюджет записан — {} ток., {:.6f} руб", tokens_out, written
            )

    def _ensure_budget(self) -> None:
        """Перед каждым платным вызовом: лимит не исчерпан?"""
        if not self.budget.check():
            raise BudgetExceeded(
                f"Месячный лимит {self.budget.monthly_limit_rub:.2f} руб исчерпан "
                f"(израсходовано {self.budget.month_spent_rub():.2f} руб). "
                "Пополни brain.budget.monthly_rub в config.yaml."
            )

    @staticmethod
    def _annotate(
        result: dict,
        *,
        provider: str,
        model: str,
        tier: str,
        task_type: str,
        attempts: int,
        cost_rub: float,
        ok: bool = True,
        attempt_costs: list[float] | None = None,
    ) -> dict:
        annotated = dict(result)
        annotated.update(
            {
                "provider": provider,
                "model": model,
                "tier": tier,
                "task_type": task_type,
                "attempts": attempts,
                "cost_rub": float(cost_rub),
                "attempt_costs": [float(item) for item in (attempt_costs or [])],
                "ok": ok,
            }
        )
        return annotated

    def _call_orchestrator(
        self,
        messages: list[dict],
        verify: Verify | None,
        *,
        max_tokens: int | None = None,
    ) -> dict:
        """Оркестратор (intent): одна попытка на brain.orchestrator, без эскалации."""
        self._ensure_budget()
        model = self.orchestrator_model
        try:
            result = self.provider.complete(messages, model, max_tokens=max_tokens)
        except (ProviderUnavailable, ProviderError) as exc:
            self.budget.record(
                provider=self.provider.name,
                model=model,
                task_type="intent",
                cost_rub=0.0,
                ok=False,
                attempt=1,
            )
            raise RouterError(
                f"оркестратор недоступен: {exc}",
                tier="cheap",
                attempts=1,
                last_error=exc,
            ) from exc

        cost = self.budget.record(
            provider=self.provider.name,
            model=model,
            task_type="intent",
            tokens_in=result["tokens_in"],
            tokens_out=result["tokens_out"],
            cost_rub=result["cost_rub"],
            ok=True,
            attempt=1,
        )
        passed_text = str(result.get("text") or "")
        checker = verify or (lambda value: default_verify(value, "intent"))
        passed = checker(passed_text)
        if not passed:
            logger.warning(
                "Router: оркестратор не прошёл проверку — эскалация для intent запрещена"
            )
        return self._annotate(
            result,
            provider=self.provider.name,
            model=model,
            tier="cheap",
            task_type="intent",
            attempts=1,
            cost_rub=cost,
            ok=passed,
            attempt_costs=[float(cost)],
        )

    def _chain(self, start_tier: str) -> list[tuple[str, int]]:
        """Лестница (тир, лимит попыток) по brain.escalation."""
        max_cheap = int(self.escalation.get("max_attempts_cheap", 3) or 1)
        max_coder = int(self.escalation.get("max_attempts_coder", 2) or 1)
        after_cheap = str(self.escalation.get("then") or "coder")
        after_coder = str(self.escalation.get("then_next") or "strong")
        limits = {"cheap": max_cheap, "coder": max_coder, "strong": 1, "vision": 1}

        if start_tier == "vision":
            return [("vision", 1), ("strong", 1)]
        if start_tier == "coder":
            return [("coder", max_coder), (after_coder, 1)]
        if start_tier == "strong":
            return [("strong", 1)]

        chain: list[tuple[str, int]] = [("cheap", max_cheap), (after_cheap, limits.get(after_cheap, 1))]
        if after_cheap != after_coder and after_coder in self.tiers:
            chain.append((after_coder, limits.get(after_coder, 1)))
        return chain

    def _attempt(
        self,
        messages: list[dict],
        model: str,
        tier: str,
        task_type: str,
        tools: list[dict] | None,
        max_tokens: int | None,
        attempt_no: int,
    ) -> tuple[dict | None, float, BaseException | None]:
        """Одна попытка вызова модели: цена пишется всегда, даже при провале."""
        self._ensure_budget()
        try:
            result = self.provider.complete(
                messages, model, tools=tools, max_tokens=max_tokens
            )
        except (ProviderUnavailable, ProviderError) as exc:
            written = self.budget.record(
                provider=self.provider.name,
                model=model,
                task_type=task_type,
                cost_rub=0.0,
                ok=False,
                attempt=attempt_no,
            )
            logger.warning(
                "Router: {} ({}) попытка {} провалилась: {} ({:.5f} руб)",
                tier,
                model,
                attempt_no,
                exc,
                float(written),
            )
            return None, float(written), exc
        written = self.budget.record(
            provider=self.provider.name,
            model=model,
            task_type=task_type,
            tokens_in=result["tokens_in"],
            tokens_out=result["tokens_out"],
            cost_rub=result["cost_rub"],
            ok=True,
            attempt=attempt_no,
        )
        logger.info(
            "Router: {} ({}) попытка {} — {:.5f} руб, OUT={}, finish={}",
            tier,
            model,
            attempt_no,
            float(written),
            result.get("tokens_out"),
            result.get("finish_reason") or "-",
        )
        return result, float(written), None

    def _escalate(
        self,
        messages: list[dict],
        start_tier: str,
        task_type: str,
        verify: Verify | None,
        tools: list[dict] | None,
        *,
        attempt_offset: int = 0,
        entry_reason: str | None = None,
        max_tokens: int | None = None,
    ) -> dict:
        """Пройти лестницу тиров; обрезка по max_tokens — НЕ провал.

        Цена суммируется по ВСЕМ попыткам (провалы и эскалации включительно).
        """
        chain = self._chain(start_tier)
        attempts_total = attempt_offset
        attempt_costs: list[float] = []
        total_cost = 0.0
        last_error: BaseException | None = None
        checker = verify or (lambda value: default_verify(value, task_type))

        for index, (tier, limit) in enumerate(chain):
            model = self.tiers.get(tier)
            if not model:
                logger.warning("Router: тир {} не настроен в brain.tiers — пропускаю", tier)
                continue

            for _attempt in range(1, max(1, limit) + 1):
                attempts_total += 1
                result, written, error = self._attempt(
                    messages, model, tier, task_type, tools, max_tokens, attempts_total
                )
                attempt_costs.append(written)
                total_cost += written
                if result is None:
                    last_error = error
                    continue

                text = str(result.get("text") or "")
                if str(result.get("finish_reason") or "") == "length":
                    trimmed = _trim_to_sentence(text)
                    if trimmed:
                        if trimmed != text.strip():
                            trimmed = f"{trimmed} …"
                        result = dict(result)
                        result["text"] = trimmed
                        result["truncated"] = True
                        text = trimmed
                        logger.info(
                            "Router: {} обрезан по max_tokens — режу по границе предложения",
                            tier,
                        )
                    else:
                        # Пусто после обрезки: ровно один повтор с двойным лимитом.
                        retry_limit = int(max_tokens) * 2 if max_tokens else None
                        logger.warning(
                            "Router: {} после обрезки пусто — повтор (max_tokens={})",
                            tier,
                            retry_limit,
                        )
                        attempts_total += 1
                        retry, written, error = self._attempt(
                            messages,
                            model,
                            tier,
                            task_type,
                            tools,
                            retry_limit,
                            attempts_total,
                        )
                        attempt_costs.append(written)
                        total_cost += written
                        if retry is None:
                            last_error = error
                            continue
                        text = str(retry.get("text") or "")
                        if str(retry.get("finish_reason") or "") == "length":
                            trimmed = _trim_to_sentence(text)
                            if trimmed:
                                if trimmed != text.strip():
                                    trimmed = f"{trimmed} …"
                                retry = dict(retry)
                                retry["text"] = trimmed
                                retry["truncated"] = True
                                text = trimmed
                        result = retry

                if checker(text):
                    logger.info(
                        "Router: ответ получен на тире {} ({}) после {} попыток, "
                        "итого {:.5f} руб",
                        tier,
                        model,
                        attempts_total,
                        total_cost,
                    )
                    return self._annotate(
                        result,
                        provider=self.provider.name,
                        model=model,
                        tier=tier,
                        task_type=task_type,
                        attempts=attempts_total,
                        cost_rub=total_cost,
                        attempt_costs=attempt_costs,
                    )
                last_error = RouterError(
                    f"проверка не пройдена на тире {tier} ({model})"
                )
                logger.warning(
                    "Router: {} ({}) попытка {} — проверка не пройдена",
                    tier,
                    model,
                    attempts_total,
                )
            if index + 1 < len(chain):
                next_tier = chain[index + 1][0]
                if last_error is not None:
                    cause = last_error
                elif index == 0 and entry_reason:
                    cause = entry_reason
                else:
                    cause = "попытки исчерпаны"
                logger.warning(
                    "эскалация: {} -> {}, причина: {}",
                    tier,
                    next_tier,
                    cause,
                )

        raise RouterError(
            f"все попытки провалены (последний тир {chain[-1][0]})",
            tier=chain[-1][0],
            attempts=attempts_total,
            last_error=last_error,
        )

# --- КОД ---

    def _run_code(
        self,
        messages: list[dict],
        task_type: str,
        verify: Verify | None,
        workdir: str | Path | None,
        tools: list[dict] | None,
    ) -> dict:
        """code: сначала локальный opencode, при провале — эскалация в облако."""
        prompt = "\n".join(str(item.get("content", "")) for item in messages)
        directory = Path(workdir) if workdir is not None else self.workdir
        attempts_total = 0
        last_error: BaseException | None = None

        self._ensure_budget()
        attempts_total += 1
        try:
            result = self.cli.complete_code(prompt, directory)
        except (ProviderUnavailable, ProviderError) as exc:
            last_error = exc
            self.budget.record(
                provider=self.cli.name,
                model=self.cli.executable,
                task_type=task_type,
                cost_rub=0.0,
                ok=False,
                attempt=attempts_total,
            )
            logger.warning("Router: {} недоступен: {}", self.cli.name, exc)
        else:
            cost = self.budget.record(
                provider=self.cli.name,
                model=self.cli.executable,
                task_type=task_type,
                cost_rub=0.0,
                ok=True,
                attempt=attempts_total,
            )
            text = result["text"]
            if verify is None or verify(text):
                logger.info(
                    "Router: ответ получен через {} за {} попыток",
                    self.cli.name,
                    attempts_total,
                )
                return self._annotate(
                    result,
                    provider=self.cli.name,
                    model=self.cli.executable,
                    tier="opencode_cli",
                    task_type=task_type,
                    attempts=attempts_total,
                    cost_rub=cost,
                )
            last_error = RouterError(
                f"проверка результата {self.cli.name} не пройдена"
            )
            logger.warning("Router: {} — результат не прошёл проверку", self.cli.name)

        chain = self._chain("cheap")
        logger.warning(
            "эскалация: {} -> {}, причина: {}",
            self.cli.name,
            chain[0][0],
            last_error,
        )
        return self._escalate(
            messages,
            "cheap",
            task_type,
            verify,
            tools,
            attempt_offset=attempts_total,
            entry_reason=str(last_error) if last_error is not None else None,
        )
