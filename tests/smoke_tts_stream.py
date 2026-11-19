"""Смоук шага 1.2: непрерывная озвучка + выбор вводных без повторов.

Запуск:
    python tests/smoke_tts_stream.py

Что проверяется:
  1) ответ режется на крупные чанки (200-300 символов), пунктуация сохранена;
  2) вводная НЕ звучит, если первый чанк готов быстрее 1.5 с;
  3) вводная звучит РОВНО один раз, если первый чанк медленнее 1.5 с;
  4) 12 вводных подряд реально проигрываются: слышны разные, лог — по факту;
  5) если в списке одна фраза — не играем ничего (None), а не повторяем её.

Синтез в сценариях 2-3 подменяется заглушкой на готовом mp3 из tts_cache.
Вводные кэшируются один раз (три короткие фразы), дальше бесплатно.
"""

from __future__ import annotations

import sys
import time
from pathlib import Path

PROJECT_ROOT = Path(__file__).resolve().parents[1]
if str(PROJECT_ROOT) not in sys.path:
    sys.path.insert(0, str(PROJECT_ROOT))

import kivi.tts.stream as stream_mod
from kivi.log import setup_logging
from kivi.tts.cache import synthesize_cached
from kivi.tts.stream import (
    ACK_PHRASES,
    FIRST_CHUNK_MAX,
    FIRST_CHUNK_MIN,
    MAX_CHUNK_CHARS,
    TARGET_CHUNK_CHARS,
    SentenceSpeaker,
    last_ack_phrase,
    pick_ack_phrase,
)

TEXT = (
    "Сегодня я расскажу короткую историю про старый маяк на краю посёлка. "
    "Он стоял там больше ста лет, и каждую ночь его луч обходил море по кругу. "
    "Рыбаки возвращались домой по этому свету и никогда не сбивались с курса. "
    "Однажды осенью лампа погасла, и посёлок остался в полной темноте. "
    "Тогда дети собрали все фонарики, какие нашли дома, и подняли их на башню. "
    "Свет получился слабее, зато тёплый, и лодки всё равно вернулись в бухту. "
    "С тех пор маяк чинят всем миром, а историю эту рассказывают каждую осень."
)


def make_fake_synth(delays: list[float], sample: Path):
    """Заглушка синтеза: спит delays[i] секунд и отдаёт готовый mp3 из кэша."""
    counter = {"n": 0}

    def fake(text, *, model, voice, run_id=None, timeout=0.0):  # noqa: ANN001, ARG001
        index = counter["n"]
        counter["n"] += 1
        delay = delays[index] if index < len(delays) else delays[-1]
        if delay > 0:
            time.sleep(delay)
        return sample

    return fake


def check_chunking(ok: list[bool]) -> None:
    """Нарезка ответа: крупные чанки, пунктуация и порядок сохранены."""
    probe = SentenceSpeaker.__new__(SentenceSpeaker)
    probe._buffer = TEXT
    probe._first = True
    chunks: list[str] = []
    while True:
        piece = probe._extract_chunk()
        if piece is None:
            break
        chunks.append(piece)
    rest = probe._buffer.strip()
    if rest:
        chunks.append(rest)

    print(f"\n[1] нарезка: {len(chunks)} чанк(ов) — {[len(c) for c in chunks]}")
    ok.append(len(chunks) >= 2)
    ok.append(all(len(c) <= MAX_CHUNK_CHARS for c in chunks))
    ok.append(all(len(c) >= 20 for c in chunks))
    # первый чанк короткий (быстрый старт), остальные — 2-3 предложения
    ok.append(FIRST_CHUNK_MIN <= len(chunks[0]) <= FIRST_CHUNK_MAX)
    ok.append(all(TARGET_CHUNK_CHARS <= len(c) <= MAX_CHUNK_CHARS for c in chunks[1:-1]))
    ok.append(" ".join(" ".join(chunks).split()) == " ".join(TEXT.split()))
    ok.append(all(c.rstrip()[-1] in ".!?…" for c in chunks[:-1]))
    for chunk in chunks:
        print(f"    {len(chunk):3d} | {chunk[:70]}…")


def main() -> int:
    setup_logging()
    print("=== SMOKE: непрерывная озвучка (кроссфейд + вводная) ===")

    ok: list[bool] = []

    # Кэш вводных: один раз синтезируем, дальше бесплатно.
    warm: dict[str, Path] = {}
    for phrase in ACK_PHRASES:
        warm[phrase] = synthesize_cached(phrase)
    sample = warm[ACK_PHRASES[0]]
    print(f"образец для заглушки: {sample.name} ({sample.stat().st_size} байт)")

    check_chunking(ok)

    original = stream_mod.synthesize_mp3
    try:
        # --- сценарий A: быстрый синтез, вводная не нужна ---
        print("\n[2] быстрый ответ (0.2 с на чанк) — вводной быть не должно")
        stream_mod.synthesize_mp3 = make_fake_synth([0.2], sample)  # type: ignore[assignment]
        fast = SentenceSpeaker(started_at=time.perf_counter())
        started = time.perf_counter()
        for chunk in TEXT.split(". "):
            fast.feed(chunk + ". ")
        total_fast = fast.finish()
        elapsed_fast = time.perf_counter() - started
        print(f"    ack_played={fast._ack_played}, речь {total_fast:.2f} с, всего {elapsed_fast:.2f} с")
        ok.append(fast._ack_played is False)
        ok.append(total_fast > 0)
        ok.append(fast._first_sound_at is not None)
        ok.append((fast._first_sound_at or 0) - started < 1.5)

        # --- сценарий B: первый чанк медленный, вводная обязательна ---
        print("\n[3] медленный ответ (2.2 с на первый чанк) — ждём одну вводную")
        stream_mod.synthesize_mp3 = make_fake_synth([2.2, 0.2], sample)  # type: ignore[assignment]
        slow = SentenceSpeaker(started_at=time.perf_counter())
        for chunk in TEXT.split(". "):
            slow.feed(chunk + ". ")
        # LLM ещё «генерирует»: finish() наступит позже порога вводной
        time.sleep(1.8)
        ack_before_finish = slow._ack_played
        total_slow = slow.finish()
        print(
            f"    ack_played={slow._ack_played} (до finish: {ack_before_finish}), "
            f"речь {total_slow:.2f} с"
        )
        ok.append(slow._ack_played is True)
        ok.append(ack_before_finish is True)
        ok.append(total_slow > total_fast)  # вводная добавила звука
    finally:
        stream_mod.synthesize_mp3 = original  # type: ignore[assignment]

    # --- сценарий C: 12 вводных подряд, каждая реально звучит ---
    print("\n[4] 12 вводных подряд — слушаем; повторов быть не должно")
    player = SentenceSpeaker(started_at=time.perf_counter(), ack_delay_s=600.0)
    played: list[str] = []
    for index in range(1, 13):
        phrase = player.play_ack()
        played.append(phrase or "")
        print(
            f"    {index:2d}. сыграно: {phrase!r} (id={player.run_id}, "
            f"last={last_ack_phrase()!r})",
            flush=True,
        )
    player.stop()
    print(f"    всего звучало {player.played_seconds:.2f} с")
    ok.append(all(played))
    ok.append(all(a != b for a, b in zip(played, played[1:])))
    ok.append(set(played) == set(ACK_PHRASES))
    # выбор всегда отличается от последней СЫГРАННОЙ фразы
    ok.append(all(pick_ack_phrase() != last_ack_phrase() for _ in range(20)))

    # --- сценарий D: одна фраза в списке — играем молча ---
    print("\n[5] в списке одна вводная — молчим (никаких повторов)")
    saved = stream_mod.ACK_PHRASES
    stream_mod.ACK_PHRASES = (ACK_PHRASES[0],)
    single: SentenceSpeaker | None = None
    try:
        single = SentenceSpeaker(started_at=time.perf_counter(), ack_delay_s=600.0)
        result = single.play_ack()
    finally:
        stream_mod.ACK_PHRASES = saved
    if single is not None:
        single.stop()
    print(f"    play_ack() -> {result!r}, звука {single.played_seconds if single else 0:.2f} с")
    ok.append(result is None)
    ok.append(single is not None and single.played_seconds == 0)

    passed = all(ok)
    print("\n" + ("PASS — непрерывная озвучка работает" if passed else "FAIL — см. пункты выше"))
    if not passed:
        for index, value in enumerate(ok, 1):
            if not value:
                print(f"    провал проверки №{index}")
    return 0 if passed else 1


if __name__ == "__main__":
    raise SystemExit(main())
