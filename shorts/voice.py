"""Озвучка сценария и тайминги слов для субтитров.

Основной голос — нейросетевые голоса Microsoft через edge-tts (бесплатно, с точными
таймингами каждого слова). Если сервис недоступен — запасной голос Google (gTTS),
а если и он недоступен — ролик без голоса, чтобы день не пропал.
"""
from __future__ import annotations

import asyncio
import json
import logging
import subprocess
import time
from dataclasses import dataclass
from pathlib import Path

import config
from .textutil import normalize_word

log = logging.getLogger(__name__)


@dataclass
class Word:
    text: str       # слово так, как оно будет в субтитрах (с пунктуацией)
    start: float
    end: float


@dataclass
class Speech:
    audio: Path
    duration: float
    words: list[Word]
    engine: str     # edge | gtts | silent


# ─── вспомогательное ─────────────────────────────────────────────────────
def probe_duration(path: Path) -> float:
    out = subprocess.run(
        ["ffprobe", "-v", "error", "-show_entries", "format=duration", "-of", "json", str(path)],
        capture_output=True, text=True, check=True,
    ).stdout
    return float(json.loads(out)["format"]["duration"])


def display_tokens(text: str) -> list[str]:
    """Слова для субтитров; одиночные «—» и знаки приклеиваются к предыдущему слову."""
    tokens: list[str] = []
    for raw in text.split():
        if not normalize_word(raw) and tokens:
            tokens[-1] += " " + raw
        else:
            tokens.append(raw)
    return tokens


def _pause_weight(token: str) -> float:
    if token.endswith((".", "!", "?", "…")):
        return 3.0
    if token.endswith((",", ":", ";", "—")):
        return 1.5
    return 0.0


def estimate_timings(tokens: list[str], start: float, end: float) -> list[Word]:
    """Равномерная раскладка слов по времени с учётом длины слова и пауз на знаках."""
    weights = [max(len(normalize_word(t)), 2) + 1.5 for t in tokens]
    pauses = [_pause_weight(t) for t in tokens]
    total = sum(weights) + sum(pauses) or 1.0
    unit = (end - start) / total
    words, cursor = [], start
    for token, weight, pause in zip(tokens, weights, pauses):
        words.append(Word(token, cursor, cursor + weight * unit))
        cursor += (weight + pause) * unit
    return words


def align(tokens: list[str], marks: list[tuple[str, float, float]]) -> list[Word] | None:
    """Сопоставляет слова сценария с метками синтезатора (там нет пунктуации и регистра)."""
    norm = [(normalize_word(t), s, e) for t, s, e in marks]
    norm = [m for m in norm if m[0]]
    if not norm:
        return None
    result: list[Word | None] = []
    j = 0
    matched = 0
    for token in tokens:
        target = normalize_word(token)
        rest, start, end, k = target, None, None, j
        while k < len(norm) and rest:
            mark, s, e = norm[k]
            if rest.startswith(mark):
                start = s if start is None else start
                end, rest, k = e, rest[len(mark):], k + 1
            else:
                break
        if start is not None and not rest:
            result.append(Word(token, start, end))
            j, matched = k, matched + 1
            continue
        # Пытаемся найти слово чуть дальше (синтезатор мог разбить или пропустить что-то)
        for look in range(j, min(j + 4, len(norm))):
            if norm[look][0] == target:
                result.append(Word(token, norm[look][1], norm[look][2]))
                j, matched = look + 1, matched + 1
                break
        else:
            result.append(None)

    if matched < 0.6 * len(tokens):
        return None

    # Заполняем пропуски интерполяцией между соседями
    words: list[Word] = []
    i = 0
    while i < len(result):
        if result[i] is not None:
            words.append(result[i])
            i += 1
            continue
        gap_start = i
        while i < len(result) and result[i] is None:
            i += 1
        left = words[-1].end if words else norm[0][1]
        right = result[i].start if i < len(result) else norm[-1][2]
        words.extend(estimate_timings(tokens[gap_start:i], left, max(right, left + 0.2 * (i - gap_start))))
    return words


# ─── движки ──────────────────────────────────────────────────────────────
async def _edge_async(text: str, path: Path) -> list[tuple[str, float, float]]:
    import edge_tts

    communicate = edge_tts.Communicate(
        text, config.TTS_VOICE, rate=config.TTS_RATE, boundary="WordBoundary",
    )
    marks: list[tuple[str, float, float]] = []
    with open(path, "wb") as audio:
        async for chunk in communicate.stream():
            if chunk["type"] == "audio":
                audio.write(chunk["data"])
            elif chunk["type"] == "WordBoundary":
                start = chunk["offset"] / 10_000_000
                marks.append((chunk["text"], start, start + chunk["duration"] / 10_000_000))
    return marks


def _edge(text: str, path: Path) -> Speech:
    last_error = None
    for attempt in range(3):
        try:
            marks = asyncio.run(_edge_async(text, path))
            duration = probe_duration(path)
            tokens = display_tokens(text)
            words = align(tokens, marks) or estimate_timings(
                tokens, marks[0][1] if marks else 0.1, marks[-1][2] if marks else duration - 0.1)
            return Speech(path, duration, words, "edge")
        except Exception as error:
            last_error = error
            time.sleep(3 * (attempt + 1))
    raise RuntimeError(f"edge-tts: {last_error}")


def _gtts(text: str, path: Path) -> Speech:
    from gtts import gTTS

    raw = path.with_suffix(".raw.mp3")
    gTTS(text=text, lang="ru", slow=False).save(str(raw))
    # голос Google медленноват — ускоряем на 15%
    subprocess.run(["ffmpeg", "-y", "-v", "error", "-i", str(raw), "-filter:a", "atempo=1.15",
                    "-ar", "24000", str(path)], check=True)
    raw.unlink(missing_ok=True)
    duration = probe_duration(path)
    return Speech(path, duration, estimate_timings(display_tokens(text), 0.15, duration - 0.15), "gtts")


def _silent(text: str, path: Path) -> Speech:
    tokens = display_tokens(text)
    duration = max(8.0, len(tokens) / 2.6)
    subprocess.run(["ffmpeg", "-y", "-v", "error", "-f", "lavfi", "-i", "anullsrc=r=24000:cl=mono",
                    "-t", f"{duration:.2f}", "-q:a", "9", str(path)], check=True)
    return Speech(path, duration, estimate_timings(tokens, 0.2, duration - 0.2), "silent")


def synthesize(text: str, path: Path) -> Speech:
    for engine in (_edge, _gtts, _silent):
        try:
            speech = engine(text, path)
            if speech.engine != "edge":
                log.warning("Озвучка через запасной вариант: %s", speech.engine)
            return speech
        except Exception as error:
            log.warning("Озвучка %s не сработала: %s", engine.__name__.strip("_"), error)
    raise RuntimeError("Не удалось озвучить текст")


def from_file(text: str, audio: Path) -> Speech:
    """Готовая озвучка из файла (для демо и ручных роликов): тайминги оцениваются по длительности."""
    duration = probe_duration(audio)
    return Speech(audio, duration, estimate_timings(display_tokens(text), 0.1, duration - 0.15), "file")


CLEANUP_TIMEOUT = 180  # секунд на обработку голосового (обычно — меньше секунды)


def from_human(text: str, source: Path, out: Path) -> Speech:
    """Живой голос (голосовое из Telegram): обрезаем тишину по краям, выравниваем громкость.
    Тайминги слов оцениваются по тексту — они нужны только для необязательных субтитров."""
    trim = ("silenceremove=start_periods=1:start_threshold=-45dB:start_silence=0.12,"
            "areverse,silenceremove=start_periods=1:start_threshold=-45dB:start_silence=0.25,areverse,")
    level = "acompressor=threshold=-20dB:ratio=3:attack=5:release=120:makeup=2,loudnorm=I=-15:TP=-1.5:LRA=9"
    def clean(filters: str) -> None:
        subprocess.run(["ffmpeg", "-y", "-v", "error", "-nostdin", "-i", str(source), "-af", filters,
                        "-ar", "48000", "-ac", "1", str(out)], check=True, timeout=CLEANUP_TIMEOUT)

    try:
        clean("highpass=f=80," + trim + level)
    except subprocess.TimeoutExpired:
        # на записи совсем без пауз ffmpeg может зависнуть на обрезке тишины — тогда без неё
        clean("highpass=f=80," + level)
    duration = probe_duration(out)
    if duration < 2.0:
        raise ValueError("голосовое слишком короткое")
    return Speech(out, duration, estimate_timings(display_tokens(text), 0.05, duration - 0.1), "human")
