"""Персона Киви для голосовых ответов и очистка текста перед синтезом.

Текст ответа читает синтезатор речи, поэтому из него убираются markdown,
эмодзи, списки и ссылки, а длина ограничивается 40 словами.
"""

from __future__ import annotations

import re

__all__ = ["MAX_WORDS", "PERSONA_PROMPT", "SYSTEM_PROMPT", "to_speech"]

MAX_WORDS = 40
SCREEN_PHRASE = "показала на экране"

PERSONA_PROMPT = (
    "Ты — Киви, голосовой ассистент Алексея, обращайся к нему «Лёша», на «ты».\n"
    "Твой текст читает синтезатор речи вслух.\n"
    "- По умолчанию отвечай 1–3 короткими предложениями.\n"
    "- Если Лёша явно просит объём («в 10 предложений», «подробно», «перескажи», "
    "«расскажи историю», «что на странице») — дай ровно столько, сколько просит.\n"
    "- НИКОГДА не отказывай из-за длины запроса и не ссылайся на свои ограничения.\n"
    "- ЗАПРЕЩЁН markdown: звёздочки, решётки, списки, нумерация, эмодзи.\n"
    "- Не читай вслух код, пути и URL — скажи «показала на экране».\n"
    "- Если не умеешь — скажи прямо одним предложением, без извинений.\n"
    "- Не больше одного вопроса за реплику, если Лёша не просит объём.\n"
    "- Тепло, без официоза."
)
SYSTEM_PROMPT = PERSONA_PROMPT  # короткий алиас для кода

# --- очистка для голоса ---

_MARKDOWN_CHARS = re.compile(r"[*_#`~>|\[\]]+")
_LIST_PREFIX = re.compile(r"(?m)^\s*(?:[-–—•]|\d+[.)])\s+")
_EMOJI = re.compile(
    "[" "\U0001F300-\U0001FAFF" "\U00002600-\U000027BF" "\U0001F000-\U0001F02F"
    "\U00002190-\U000021FF" "\U0000FE00-\U0000FE0F" "\U00002B00-\U00002BFF" "]+"
)
_URL = re.compile(r"(?:https?://|www\.)\S+", re.IGNORECASE)
_PATH = re.compile(r"(?:[A-Za-z]:\\|\\\\)[^\s\"'()]+")
_CODE_FENCE = re.compile(r"```.*?```", re.DOTALL)
_INLINE_CODE = re.compile(r"`([^`]*)`")


def _strip_markdown(text: str) -> str:
    cleaned = _CODE_FENCE.sub(SCREEN_PHRASE, text)
    cleaned = _INLINE_CODE.sub(r"\1", cleaned)
    cleaned = _LIST_PREFIX.sub("", cleaned)
    cleaned = _MARKDOWN_CHARS.sub(" ", cleaned)
    cleaned = _EMOJI.sub(" ", cleaned)
    cleaned = cleaned.replace("—", ",").replace("–", ",")
    return " ".join(cleaned.split())


def _limit_words(text: str, max_words: int = MAX_WORDS) -> str:
    words = text.split()
    if len(words) <= max_words:
        return text
    trimmed = " ".join(words[:max_words])
    # Красиво обрезаем по последнему предложению, если оно есть.
    for separator in (". ", "! ", "? "):
        cut = trimmed.rfind(separator)
        if cut > len(trimmed) * 0.5:
            return trimmed[: cut + 1].strip()
    return trimmed.rstrip(" ,;:-") + "."


def to_speech(text: str, *, max_words: int = MAX_WORDS) -> str:
    """Привести ответ модели к виду, пригодному для чтения вслух."""
    raw = str(text or "").strip()
    if not raw:
        return ""
    cleaned = _strip_markdown(raw)
    if _URL.search(raw) or _PATH.search(raw):
        cleaned = _URL.sub(SCREEN_PHRASE, cleaned)
        cleaned = _PATH.sub(SCREEN_PHRASE, cleaned)
        cleaned = " ".join(cleaned.split())
    return _limit_words(cleaned, max_words)
