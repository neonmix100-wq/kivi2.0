"""WebSocket API ядра Киви на 127.0.0.1:8765 (threaded, daemon).

События (core -> clients):
    status{state,budget_used,volume,window_open}
    ask{id,question,options}, confirm{id,title,body,details}
    info{text}, log{line,level} (хвост лога, уровень>=INFO, кольцо 200 строк)

Команды (clients -> core):
    ask{question,options}, confirm{title,body,details}, info{text},
    ask_response{id,answer}, confirm_response{id,ok}, set_volume{v},
    get_status, get_gaps{limit}, get_skills, set_skill_enabled{name,enabled},
    test_skill{name}, close_gap{id}, attempt_gap{id}, get_memory{kind},
    reflect_now, tail_logs{n}, shutdown
Ответ на команду: {ok, ...}.

Слушаем СТРОГО 127.0.0.1. Аутентификации нет: доступен только loopback.
"""

from __future__ import annotations

import asyncio
import json
import threading
import time
from collections import deque
from collections.abc import Callable
from pathlib import Path
from typing import Any

import yaml
from loguru import logger
from pydantic import BaseModel, ConfigDict, Field, ValidationError

from kivi.api.pending import PendingRegistry
from kivi.config import CONFIG_PATH
from kivi.prefs import get_tts_volume, set_tts_volume

try:  # websockets >= 13
    from websockets.asyncio.server import serve
except ImportError:  # pragma: no cover - старые версии
    from websockets.server import serve  # type: ignore[no-redef]

from websockets.exceptions import ConnectionClosed

__all__ = [
    "ApiConfig",
    "ApiServer",
    "LOG_RING_SIZE",
    "load_api_config",
]

LOOPBACK_HOST = "127.0.0.1"
DEFAULT_PORT = 8765
DEFAULT_ASK_TIMEOUT_S = 60.0
DEFAULT_CONFIRM_TIMEOUT_S = 120.0
LOG_RING_SIZE = 200
CLIENT_QUEUE_LIMIT = 200


class ApiConfig(BaseModel):
    """Секция api из config.yaml."""

    model_config = ConfigDict(extra="allow")

    host: str = LOOPBACK_HOST
    port: int = Field(default=DEFAULT_PORT, ge=0, le=65535)
    ask_timeout_s: float = Field(default=DEFAULT_ASK_TIMEOUT_S, gt=0)
    confirm_timeout_s: float = Field(default=DEFAULT_CONFIRM_TIMEOUT_S, gt=0)


def load_api_config(path: Path | None = None) -> dict[str, Any]:
    """Секция api из config.yaml (битый конфиг — пусто + WARNING)."""
    config_path = Path(path) if path is not None else CONFIG_PATH
    try:
        raw = yaml.safe_load(config_path.read_text(encoding="utf-8")) or {}
    except (OSError, yaml.YAMLError) as exc:
        logger.warning("api: не читается {}: {}", config_path.name, exc)
        return {}
    section = raw.get("api") if isinstance(raw, dict) else None
    return dict(section) if isinstance(section, dict) else {}


class ApiServer:
    """WS-сервер в отдельном потоке; broadcast можно звать из любого потока."""

    def __init__(
        self,
        *,
        host: str | None = None,
        port: int | None = None,
        config: dict[str, Any] | ApiConfig | None = None,
        core: Any | None = None,
    ) -> None:
        if isinstance(config, ApiConfig):
            parsed = config
        else:
            raw = dict(config) if config is not None else load_api_config()
            try:
                parsed = ApiConfig.model_validate(raw)
            except ValidationError as exc:
                logger.warning("api: конфиг невалиден ({}), беру умолчания", exc)
                parsed = ApiConfig()

        requested_host = str(host or parsed.host or LOOPBACK_HOST)
        if requested_host != LOOPBACK_HOST:
            logger.warning(
                "api: host={!r} запрещён — слушаю только {}", requested_host, LOOPBACK_HOST
            )
        self.host = LOOPBACK_HOST
        self.port = int(port if port is not None else parsed.port)
        self._core = core
        self._clients: set[Any] = set()
        self._queues: dict[Any, asyncio.Queue[str]] = {}
        self._clients_lock = threading.Lock()
        self._ring: deque[str] = deque(maxlen=LOG_RING_SIZE)
        self._ring_lock = threading.Lock()
        self._thread: threading.Thread | None = None
        self._async_loop: asyncio.AbstractEventLoop | None = None
        self._server: Any | None = None
        self._stopped: asyncio.Event | None = None
        self._ready = threading.Event()
        self._log_sink: int | None = None
        self._shutdown_callback: Callable[[], object] | None = None
        self.pending = PendingRegistry(
            connected=lambda: self.has_clients,
            send=self.broadcast,
            ask_timeout_s=parsed.ask_timeout_s,
            confirm_timeout_s=parsed.confirm_timeout_s,
        )

    # --- жизненный цикл ---

    def attach(self, core: Any) -> None:
        """Привязать ядро (WakeLoop): из него берём status."""
        self._core = core

    def set_shutdown_callback(self, callback: Callable[[], object]) -> None:
        """Что вызвать по команде shutdown (например, stop_event.set)."""
        self._shutdown_callback = callback

    @property
    def is_running(self) -> bool:
        return self._thread is not None and self._thread.is_alive()

    @property
    def has_clients(self) -> bool:
        with self._clients_lock:
            return bool(self._clients)

    def _install_log_sink(self) -> None:
        if self._log_sink is not None:
            return
        self._log_sink = logger.add(
            self._on_log,
            level="INFO",
            format="{time:HH:mm:ss} | {level: <7} | {message}",
            enqueue=False,
            catch=True,
        )

    def _remove_log_sink(self) -> None:
        if self._log_sink is None:
            return
        try:
            logger.remove(self._log_sink)
        except ValueError:
            pass
        self._log_sink = None

    def _on_log(self, message: Any) -> None:
        line = str(message).rstrip()
        if not line:
            return
        try:
            level = str(message.record["level"].name)
        except Exception:  # noqa: BLE001 - sink может получить не Message
            level = "INFO"
        with self._ring_lock:
            self._ring.append(line)
        self.broadcast({"event": "log", "level": level, "line": line})

    def start(self, *, timeout: float = 5.0) -> "ApiServer":
        """Поднять сервер и дождаться готовности (не дольше timeout)."""
        if self.is_running:
            return self
        self._install_log_sink()
        self._ready.clear()
        self._thread = threading.Thread(
            target=self._run, name="kivi-api", daemon=True
        )
        self._thread.start()
        if not self._ready.wait(timeout):
            self._remove_log_sink()
            raise RuntimeError(f"api: сервер не поднялся за {timeout:.0f} с")
        return self

    def _run(self) -> None:
        try:
            asyncio.run(self._serve())
        except Exception as exc:  # noqa: BLE001 - сервер не должен ронять ядро
            logger.error("api: сервер упал: {}: {}", type(exc).__name__, exc)
        finally:
            self._ready.set()

    async def _serve(self) -> None:
        async with serve(
            self._handler,
            self.host,
            self.port,
            ping_interval=20,
            ping_timeout=20,
        ) as server:
            self._server = server
            self._async_loop = asyncio.get_running_loop()
            self._stopped = asyncio.Event()
            socket = None
            sockets = getattr(server, "sockets", None)
            if sockets:
                socket = sockets[0]
            if socket is not None:
                self.port = int(socket.getsockname()[1])
            logger.info("api: слушаю ws://{}:{}", self.host, self.port)
            self._ready.set()
            await self._stopped.wait()

    async def _shutdown(self) -> None:
        for websocket in list(self._clients):
            try:
                await websocket.close()
            except Exception:  # noqa: BLE001
                pass
        with self._clients_lock:
            self._clients.clear()
            self._queues.clear()
        if self._stopped is not None:
            self._stopped.set()

    def stop(self, *, timeout: float = 3.0) -> None:
        """Остановить сервер и закрыть клиентов."""
        loop = self._async_loop
        if loop is not None and loop.is_running():
            try:
                future = asyncio.run_coroutine_threadsafe(self._shutdown(), loop)
                future.result(timeout=timeout)
            except Exception as exc:  # noqa: BLE001 - остановка не должна падать
                logger.warning("api: остановка: {}: {}", type(exc).__name__, exc)
        if self._thread is not None:
            self._thread.join(timeout=timeout)
        self._thread = None
        self._async_loop = None
        self._remove_log_sink()
        if self._ready.is_set():
            logger.info("api: сервер остановлен")

    # --- события ---

    def broadcast(self, event: dict[str, Any]) -> bool:
        """Отправить событие всем клиентам. False — клиентов/цикла нет."""
        loop = self._async_loop
        with self._clients_lock:
            clients = list(self._clients)
        if not clients or loop is None or not loop.is_running():
            return False
        payload = json.dumps(event, ensure_ascii=False)
        level = str(event.get("level") or "").upper()
        skip_if_full = event.get("event") == "log" and level not in (
            "WARNING",
            "ERROR",
            "CRITICAL",
        )
        try:
            asyncio.run_coroutine_threadsafe(
                self._enqueue_all(payload, skip_if_full=skip_if_full), loop
            )
        except RuntimeError:
            return False
        return True

    async def _enqueue_all(self, payload: str, *, skip_if_full: bool) -> None:
        """Разложить событие по очередям клиентов (INFO при переполнении режем)."""
        with self._clients_lock:
            queues = list(self._queues.values())
        for queue in queues:
            if skip_if_full and queue.qsize() > CLIENT_QUEUE_LIMIT:
                continue
            try:
                queue.put_nowait(payload)
            except asyncio.QueueFull:  # запасной случай, очередь без maxsize
                continue

    async def _writer(self, websocket: Any, queue: asyncio.Queue[str]) -> None:
        """Единственный писатель клиента: сериализует send и не блокирует broadcast."""
        try:
            while True:
                payload = await queue.get()
                await websocket.send(payload)
        except ConnectionClosed:
            pass
        except asyncio.CancelledError:
            raise
        except Exception as exc:  # noqa: BLE001 - мёртвый клиент убираем
            logger.debug("api: клиент отключён при отправке: {}", exc)
            try:
                await websocket.close()
            except Exception:  # noqa: BLE001
                pass

    def status(self) -> dict[str, Any]:
        """Снимок состояния ядра для события/команды status."""
        core = self._core
        data: dict[str, Any] = {}
        snapshot = getattr(core, "status_snapshot", None)
        if callable(snapshot):
            try:
                data = dict(snapshot())
            except Exception as exc:  # noqa: BLE001
                logger.debug("api: status ядра недоступен: {}", exc)
        if not data:
            data = {
                "state": str(getattr(core, "state", "IDLE")) if core is not None else "IDLE",
                "budget_used": 0.0,
                "window_open": bool(getattr(core, "window_until", 0.0)),
            }
        return {
            "state": str(data.get("state") or "IDLE"),
            "budget_used": float(data.get("budget_used") or 0.0),
            "volume": float(data.get("volume", get_tts_volume())),
            "window_open": bool(data.get("window_open")),
        }

    def broadcast_status(self, **fields: Any) -> bool:
        payload = self.status()
        payload.update(fields)
        return self.broadcast({"event": "status", **payload})

    def info(self, text: str) -> bool:
        return self.broadcast({"event": "info", "text": str(text)})

    def ask(self, question: str, options: list[str] | None = None) -> str | None:
        return self.pending.ask(question, options)

    def confirm(self, title: str, body: str = "", details: str = "") -> bool:
        return self.pending.confirm(title, body, details)

    def tail_logs(self, n: int = 20) -> list[str]:
        limit = max(1, min(LOG_RING_SIZE, int(n)))
        with self._ring_lock:
            return list(self._ring)[-limit:]

    def gaps(self, limit: int = 10) -> list[dict[str, Any]]:
        from kivi.skills.gaps import get_gap_log

        return get_gap_log().list_recent(limit=max(1, int(limit)))

    def memory_kinds(self) -> list[str]:
        """Список видов памяти: memory.kinds из config, иначе базовый набор."""
        fallback = ["profile", "schedule", "tasks", "topics", "people"]
        try:
            raw = yaml.safe_load(CONFIG_PATH.read_text(encoding="utf-8")) or {}
            kinds = (raw.get("memory") or {}).get("kinds")
            if isinstance(kinds, list) and kinds:
                return [str(item).strip().lower() for item in kinds if str(item).strip()]
        except (OSError, yaml.YAMLError):
            pass
        return fallback

    def _memory_kind(self, kind: str) -> str:
        valid = str(kind or "profile").strip().lower()
        return valid if valid in self.memory_kinds() else "profile"

    def memory_lines(self, kind: str = "profile") -> list[str]:
        """Строки заметки/папки для UI и внешних клиентов."""
        from kivi.memory.vault import list_notes, read_note, vault_root

        valid = self._memory_kind(kind)
        root = vault_root()
        if valid in ("profile", "schedule", "tasks"):
            text = read_note(valid, "", root)
            return [
                line.strip()
                for line in text.splitlines()
                if line.strip() and not line.lstrip().startswith("#")
            ]
        lines: list[str] = []
        for path in list_notes(valid)[:20]:
            if not path.is_file():
                continue
            try:
                text = path.read_text(encoding="utf-8", errors="replace")
            except OSError:
                continue
            name = path.name
            for line in text.splitlines():
                cleaned = line.strip()
                if cleaned and not cleaned.startswith("#"):
                    lines.append(f"{name}: {cleaned}")
        return lines

    def memory_items(self, kind: str = "profile") -> list[dict[str, str]]:
        from kivi.memory.vault import list_notes, vault_root

        valid = self._memory_kind(kind)
        if valid in ("profile", "schedule", "tasks"):
            return [{"name": valid, "text": line} for line in self.memory_lines(valid)]
        root = vault_root()
        items: list[dict[str, str]] = []
        for path in list_notes(valid)[:20]:
            if not path.is_file():
                continue
            try:
                text = path.read_text(encoding="utf-8", errors="replace")
            except OSError:
                continue
            try:
                name = str(path.relative_to(root)).replace("\\", "/")
            except ValueError:
                name = path.name
            items.append({"name": name, "text": text[:2000]})
        return items

    def _skill_registry(self):
        """Реестр скиллов из ядра или общий (для UI)."""
        core = self._core
        registry = getattr(core, "skills", None)
        if registry is not None:
            return registry
        from kivi.skills.registry import get_registry

        return get_registry()

    def skills_payload(self) -> list[dict[str, Any]]:
        """Список скиллов со статистикой для UI."""
        registry = self._skill_registry()
        items: list[dict[str, Any]] = []
        for manifest in registry.list_all():
            stats = registry.stats(manifest.name)
            items.append(
                {
                    "name": manifest.name,
                    "description": manifest.description,
                    "category": manifest.category,
                    "enabled": bool(manifest.enabled),
                    "use_count": int(stats.use_count) if stats is not None else 0,
                    "success_count": int(stats.success_count) if stats is not None else 0,
                    "fail_count": int(stats.fail_count) if stats is not None else 0,
                }
            )
        return items

    def set_skill_enabled(self, name: str, enabled: bool) -> dict[str, Any]:
        registry = self._skill_registry()
        ok = registry.enable(name) if enabled else registry.disable(name)
        return {"ok": bool(ok), "name": name, "enabled": bool(enabled)}

    def test_skill(self, name: str) -> dict[str, Any]:
        manifest = self._skill_registry().get(name)
        if manifest is None:
            return {"ok": False, "error": f"скилл {name!r} не найден"}
        return {"ok": True, "name": name, "message": f"Тест «{name}»: заглушка"}

    def close_gap(self, gap_id: int) -> dict[str, Any]:
        from kivi.skills.gaps import get_gap_log

        closed = get_gap_log().close(int(gap_id), how="manual", notes="закрыто в UI")
        return {"ok": bool(closed), "id": int(gap_id)}

    def attempt_gap(self, gap_id: int) -> dict[str, Any]:
        from kivi.skills.gap_closer import GapCloser
        from kivi.skills.gaps import get_gap_log

        core = self._core
        closer = GapCloser(
            router=getattr(core, "router", None),
            registry=self._skill_registry(),
            gaps=get_gap_log(),
        )
        success, how, message = closer.attempt_close(int(gap_id))
        return {
            "ok": True,
            "success": bool(success),
            "how": str(how),
            "message": str(message),
        }

    def reflect_now(self) -> dict[str, Any]:
        """Принудительная рефлексия по текущей истории (для кнопки UI)."""
        core = self._core
        reflector = getattr(core, "reflector", None)
        if reflector is None:
            return {"ok": False, "error": "рефлексия недоступна"}
        session = None
        getter = getattr(core, "_get_session", None)
        messages: list[dict] = []
        max_ts = 0.0
        try:
            if callable(getter):
                session = getter()
            if session is not None and hasattr(session, "messages_since"):
                messages, max_ts = session.messages_since(0, max_tokens=3000)
            elif session is not None and hasattr(session, "history"):
                messages = session.history(max_tokens=3000)
            try:
                stats = reflector.run(messages, force=True)
            except TypeError:
                stats = reflector.run(messages)
            if isinstance(stats, dict) and not stats.get("skipped") and max_ts:
                if hasattr(core, "_on_reflected"):
                    core._on_reflected(max_ts)
                scheduler = getattr(core, "reflect_scheduler", None)
                if scheduler is not None:
                    scheduler.last_reflected_ts = max(
                        float(getattr(scheduler, "last_reflected_ts", 0.0)),
                        float(max_ts),
                    )
            return {"ok": True, "stats": stats}
        except Exception as exc:  # noqa: BLE001 - UI не должен падать
            logger.warning("api: reflect_now упал: {}: {}", type(exc).__name__, exc)
            return {"ok": False, "error": f"{type(exc).__name__}: {exc}"}

    # --- обработка клиентов ---

    async def _handler(self, websocket: Any) -> None:
        peer = getattr(websocket, "remote_address", None)
        peer_host = str(peer[0]) if peer else ""
        if peer_host not in ("127.0.0.1", "::1"):
            logger.warning("api: клиент {} не loopback — закрываю", peer_host)
            await websocket.close()
            return
        queue: asyncio.Queue[str] = asyncio.Queue()
        with self._clients_lock:
            self._clients.add(websocket)
            self._queues[websocket] = queue
        writer: asyncio.Task[None] | None = None
        try:
            await websocket.send(
                json.dumps({"event": "status", **self.status()}, ensure_ascii=False)
            )
            writer = asyncio.create_task(self._writer(websocket, queue))
            async for raw in websocket:
                await self._handle_message(websocket, raw)
        except ConnectionClosed:
            pass
        finally:
            if writer is not None:
                writer.cancel()
                try:
                    await writer
                except (asyncio.CancelledError, ConnectionClosed):
                    pass
                except Exception:  # noqa: BLE001 - закрытие writer не критично
                    pass
            with self._clients_lock:
                self._clients.discard(websocket)
                self._queues.pop(websocket, None)

    async def _reply(self, websocket: Any, payload: dict[str, Any]) -> None:
        try:
            await websocket.send(json.dumps(payload, ensure_ascii=False))
        except Exception:  # noqa: BLE001 - клиент мог отвалиться
            pass

    async def _handle_message(self, websocket: Any, raw: str | bytes) -> None:
        try:
            message = json.loads(raw)
        except (ValueError, TypeError):
            await self._reply(websocket, {"ok": False, "error": "bad json"})
            return
        if not isinstance(message, dict):
            await self._reply(websocket, {"ok": False, "error": "expected object"})
            return
        command = str(message.get("command") or message.get("cmd") or "").strip()

        if command == "get_status":
            await self._reply(websocket, {"ok": True, **self.status()})
        elif command == "set_volume":
            volume = set_tts_volume(message.get("v"))
            self.broadcast_status(volume=volume)
            await self._reply(websocket, {"ok": True, "volume": volume})
        elif command == "get_gaps":
            try:
                limit = int(message.get("limit") or 10)
            except (TypeError, ValueError):
                limit = 10
            await self._reply(websocket, {"ok": True, "gaps": self.gaps(limit)})
        elif command == "get_skills":
            await self._reply(websocket, {"ok": True, "skills": self.skills_payload()})
        elif command == "set_skill_enabled":
            result = self.set_skill_enabled(
                str(message.get("name") or ""), bool(message.get("enabled"))
            )
            await self._reply(websocket, result)
        elif command == "test_skill":
            result = self.test_skill(str(message.get("name") or ""))
            await self._reply(websocket, result)
        elif command == "close_gap":
            try:
                gap_id = int(message.get("id"))
            except (TypeError, ValueError):
                await self._reply(websocket, {"ok": False, "error": "bad gap id"})
                return
            await self._reply(websocket, self.close_gap(gap_id))
        elif command == "attempt_gap":
            try:
                gap_id = int(message.get("id"))
            except (TypeError, ValueError):
                await self._reply(websocket, {"ok": False, "error": "bad gap id"})
                return
            result = await asyncio.to_thread(self.attempt_gap, gap_id)
            await self._reply(websocket, result)
        elif command == "get_memory":
            kind = str(message.get("kind") or "profile")
            await self._reply(
                websocket,
                {
                    "ok": True,
                    "kind": self._memory_kind(kind),
                    "items": self.memory_items(kind),
                    "lines": self.memory_lines(kind),
                },
            )
        elif command == "reflect_now":
            result = await asyncio.to_thread(self.reflect_now)
            if "ok" not in result:
                result = {"ok": True, **result}
            await self._reply(websocket, result)
        elif command == "ask":
            question = str(message.get("question") or "")
            options = [
                str(item) for item in (message.get("options") or []) if str(item).strip()
            ]
            answer = await asyncio.to_thread(self.pending.ask, question, options)
            await self._reply(websocket, {"ok": True, "answer": answer})
        elif command == "confirm":
            answer = await asyncio.to_thread(
                self.pending.confirm,
                str(message.get("title") or ""),
                str(message.get("body") or ""),
                str(message.get("details") or ""),
            )
            await self._reply(websocket, {"ok": True, "confirmed": bool(answer)})
        elif command == "info":
            self.info(str(message.get("text") or ""))
            await self._reply(websocket, {"ok": True})
        elif command == "tail_logs":
            try:
                n = int(message.get("n") or 20)
            except (TypeError, ValueError):
                n = 20
            await self._reply(websocket, {"ok": True, "lines": self.tail_logs(n)})
        elif command == "ask_response":
            resolved = self.pending.resolve_ask(
                str(message.get("id") or ""), message.get("answer")
            )
            await self._reply(websocket, {"ok": resolved})
        elif command == "confirm_response":
            resolved = self.pending.resolve_confirm(
                str(message.get("id") or ""), bool(message.get("ok"))
            )
            await self._reply(websocket, {"ok": resolved})
        elif command == "shutdown":
            await self._reply(websocket, {"ok": True})
            callback = self._shutdown_callback
            if callback is not None:
                threading.Thread(
                    target=callback, name="kivi-api-shutdown", daemon=True
                ).start()
        else:
            await self._reply(
                websocket, {"ok": False, "error": f"unknown command {command!r}"}
            )
