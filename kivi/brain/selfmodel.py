"""Манифест возможностей Киви: правда о себе для промптов и мета-вопросов.

Зачем: модель без манифеста начинает выдумывать ограничения («у меня нет
доступа к сети», «нужно чьё-то разрешение»). Манифест собирается из реального
реестра скиллов и config.yaml и вставляется в system-промпт ВСЕГДА —
и в чат, и в суммаризатор поиска.

Использование:
    from kivi.brain.selfmodel import build_manifest, is_meta_question, meta_answer
    build_manifest()               # текст для system-промпта
    is_meta_question("есть ли доступ к поиску")   # True
    meta_answer()                  # короткий ответ по манифесту
"""

from __future__ import annotations

from typing import Any

__all__ = [
    "MANIFEST_HEADER",
    "META_TRIGGERS",
    "build_manifest",
    "is_meta_question",
    "meta_answer",
]

MANIFEST_HEADER = "МОЁ СОСТОЯНИЕ (факты, а не догадки):"

# Короткие метки возможностей (как в skills/capabilities).
SKILL_LABELS: dict[str, str] = {
    "time_date": "время и дата",
    "file_search": "поиск файлов",
    "facts": "курс валют и погода по открытым API",
    "recall": "память и расписание",
    "remember": "запоминание фактов и задач",
    "reminders": "напоминания",
    "tasks_list": "список задач",
    "assistant_volume": "громкость голоса",
    "listen_control": "пауза прослушки",
    "memory_manage": "управление памятью",
    "capabilities": "список возможностей",
    "screen_look": "зрение (скриншот экрана)",
}

# Фразы-триггеры мета-вопросов о себе: их нельзя отдавать LLM-планировщику,
# иначе он ответит «не умею» вместо правды из манифеста.
META_TRIGGERS: tuple[str, ...] = (
    "что тебе нужно",
    "что тебе требуется",
    "что нужно чтобы",
    "что нужно для",
    "что требуется для",
    "как включить",
    "как получить доступ",
    "почему не можешь",
    "почему не умеешь",
    "почему не работает",
    "есть ли доступ",
    "есть ли у тебя доступ",
    "нет доступа",
    "какие права",
    "какие разрешения",
    "что тебе мешает",
    "доступ к поиску",
    "доступ в интернет",
    "доступ к интернету",
)

_META_TOPICS = ("поиск", "интернет", "сеть", "погод", "курс", "умеешь", "можешь")


def _norm(text: str) -> str:
    value = str(text or "").replace("ё", "е").replace("Ё", "Е").lower()
    return " ".join(value.split())


def is_meta_question(command: str) -> bool:
    """Спрашивают про мои возможности/доступ, а не про факт из мира."""
    text = _norm(command)
    if not text:
        return False
    if any(trigger in text for trigger in META_TRIGGERS):
        return True
    has_access_word = any(word in text for word in ("доступ", "разрешени", "права", "сеть"))
    has_topic = any(topic in text for topic in _META_TOPICS)
    has_ask = any(word in text for word in ("есть", "нужно", "нужен", "требует", "почему", "как"))
    return has_access_word and has_topic and has_ask



def _config_section(name: str) -> dict[str, Any]:
    """Секция config.yaml без разрешения аудиоустройств."""
    try:
        from kivi.config import load_config

        raw = load_config().model_dump()
    except Exception:  # noqa: BLE001 - манифест не должен падать из-за конфига
        return {}
    section = raw.get(name) if isinstance(raw, dict) else None
    return dict(section) if isinstance(section, dict) else {}


def _enabled_names(registry: Any | None) -> list[str]:
    """Имена включённых скиллов (пусто, если реестр недоступен)."""
    if registry is None:
        try:
            from kivi.skills.registry import get_registry

            registry = get_registry()
        except Exception:  # noqa: BLE001 - без реестра манифест статичный
            return []
    try:
        return [str(item.name) for item in registry.list_enabled()]
    except Exception:  # noqa: BLE001
        return []


def build_manifest(registry: Any | None = None) -> str:
    """Текст манифеста (~200 токенов) для system-промпта."""
    names = _enabled_names(registry)
    search_enabled = bool(_config_section("search").get("enabled", True))
    has_facts = "facts" in names

    lines: list[str] = [MANIFEST_HEADER]
    if search_enabled:
        lines.append(
            "- Поиск в интернете: ВКЛЮЧЁН. DuckDuckGo, чтение страниц и открытые "
            "API без ключей (курсы валют open.er-api.com, погода open-meteo.com)."
        )
    else:
        lines.append("- Поиск в интернете: выключен в конфиге (search.enabled=false).")
    if has_facts:
        lines.append(
            "- Курс валют и погода: отдельный скилл facts — цифра приходит от источника, "
            "а не из головы."
        )
    labels = [SKILL_LABELS[name] for name in names if name in SKILL_LABELS]
    if labels:
        lines.append("- Включённые умения: " + ", ".join(dict.fromkeys(labels)) + ".")
    lines.append("- Пока нет: почта, умный дом, покупки, звонки.")
    lines.append("Правила:")
    lines.append(
        "1) Всё перечисленное выше у меня ЕСТЬ. НИКОГДА не говори «нет доступа к сети», "
        "«нужно чьё-то разрешение», «я не могу искать», если это есть в списке."
    )
    lines.append(
        "2) Если инструмент не сработал — называй конкретный сбой: «источник не отдал "
        "цифры», «страница не открылась». Не выдумывай отсутствие доступа."
    )
    lines.append("3) На вопросы о себе отвечай по этому списку, без домыслов.")
    return "\n".join(lines)


def meta_answer(registry: Any | None = None) -> str:
    """Короткий честный ответ на мета-вопрос (без «нет доступа»)."""
    names = _enabled_names(registry)
    search_enabled = bool(_config_section("search").get("enabled", True))
    parts: list[str] = []
    if search_enabled:
        parts.append(
            "Интернет и поиск у меня есть: DuckDuckGo, чтение страниц и открытые API "
            "курсов валют и погоды — ключи не нужны."
        )
    else:
        parts.append("Поиск выключен в настройках: search.enabled=false.")
    if "facts" in names:
        parts.append("Курс и погоду беру из открытых API, без регистрации.")
    parts.append(
        "Память, расписание, задачи, напоминания, время и управление громкостью "
        "работают локально."
    )
    parts.append("Почта и умный дом пока не подключены.")
    parts.append(
        "Чтобы что-то включить, достаточно сказать, что именно нужно: отдельного "
        "разрешения или доступа от тебя не требуется."
    )
    return " ".join(parts)
