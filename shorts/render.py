"""Монтаж вертикального ролика 1080×1920.

Схема: ffmpeg №1 собирает фон (стоковые клипы или нарисованный фон) и отдаёт кадры,
Python дорисовывает поверх плашку рубрики, заголовок, субтитры с подсветкой
текущего слова и полосу прогресса, ffmpeg №2 кодирует видео и сводит звук.
"""
from __future__ import annotations

import json
import logging
import math
import random
import re
import subprocess
import tempfile
from dataclasses import dataclass
from functools import lru_cache
from pathlib import Path

import numpy as np
from PIL import Image, ImageDraw, ImageFilter, ImageFont

import config
from .footage import Background
from .textutil import normalize_word
from .voice import Speech, Word, probe_duration

log = logging.getLogger(__name__)

W, H, FPS = config.WIDTH, config.HEIGHT, config.FPS
SAFE_X = 70                 # поля слева/справа
HEADER_Y = 225              # верх плашки рубрики
CAPTION_CENTER_Y = 1190     # центр субтитров
CAPTION_MAX_W = W - 2 * SAFE_X - 20
CAPTION_SIZE = 90             # размер шрифта субтитров
PLAIN_CAPTION_Y = 1420        # простые субтитры (стиль sasha) — ниже, как у блогеров


# ─── шрифты и цвета ──────────────────────────────────────────────────────
@lru_cache(maxsize=64)
def font(path: str, size: int) -> ImageFont.FreeTypeFont:
    return ImageFont.truetype(path, size)


def _luminance(rgb) -> float:
    def channel(c):
        c = c / 255
        return c / 12.92 if c <= 0.03928 else ((c + 0.055) / 1.055) ** 2.4
    r, g, b = (channel(c) for c in rgb)
    return 0.2126 * r + 0.7152 * g + 0.0722 * b


def text_on(color) -> tuple[int, int, int]:
    """Белый или почти чёрный текст — что читается лучше на этом цвете."""
    lum = _luminance(color)
    white = 1.05 / (lum + 0.05)
    black = (lum + 0.05) / 0.05
    return (255, 255, 255) if white >= black * 0.85 else (14, 14, 18)


# ─── слои ────────────────────────────────────────────────────────────────
@dataclass
class Layer:
    """Готовый к наложению RGBA-фрагмент: цвет заранее умножен на прозрачность."""
    x: int
    y: int
    premul: np.ndarray   # uint16, H×W×3
    inv: np.ndarray      # uint16, H×W×1 (255 - alpha)
    alpha: np.ndarray    # uint16, H×W×1

    @classmethod
    def from_image(cls, image: Image.Image, x: int, y: int) -> "Layer | None":
        bbox = image.getbbox()
        if not bbox:
            return None
        image = image.crop(bbox)
        arr = np.asarray(image, dtype=np.uint8)
        alpha = arr[..., 3:4].astype(np.uint16)
        premul = arr[..., :3].astype(np.uint16) * alpha
        return cls(x + bbox[0], y + bbox[1], premul, (255 - alpha), alpha)


def _div255(x: np.ndarray) -> np.ndarray:
    v = x + 128
    return ((v + (v >> 8)) >> 8).astype(np.uint8)


def blend(frame: np.ndarray, layer: Layer, dx: int = 0, dy: int = 0) -> None:
    x, y = layer.x + dx, layer.y + dy
    h, w = layer.premul.shape[:2]
    x0, y0, x1, y1 = max(x, 0), max(y, 0), min(x + w, W), min(y + h, H)
    if x0 >= x1 or y0 >= y1:
        return
    sl = (slice(y0 - y, y1 - y), slice(x0 - x, x1 - x))
    region = frame[y0:y1, x0:x1]
    region[:] = _div255(layer.premul[sl] + region.astype(np.uint16) * layer.inv[sl])


def blend_image(frame: np.ndarray, image: Image.Image, x: int, y: int, opacity: float = 1.0) -> None:
    """Наложение с прозрачностью (для анимаций — картинка меняется каждый кадр)."""
    if opacity < 1.0:
        a = image.getchannel("A").point(lambda v: int(v * opacity))
        image = image.copy()
        image.putalpha(a)
    layer = Layer.from_image(image, x, y)
    if layer:
        blend(frame, layer)


def _rounded(draw: ImageDraw.ImageDraw, box, radius, fill):
    draw.rounded_rectangle(box, radius=radius, fill=fill)


# ─── шапка: рубрика + заголовок ─────────────────────────────────────────
def _wrap(text: str, fnt: ImageFont.FreeTypeFont, max_w: int) -> list[str]:
    lines, current = [], ""
    for word in text.split():
        trial = f"{current} {word}".strip()
        if fnt.getlength(trial) <= max_w or not current:
            current = trial
        else:
            lines.append(current)
            current = word
    if current:
        lines.append(current)
    return lines


def _cap_height(fnt: ImageFont.FreeTypeFont) -> int:
    return -fnt.getbbox("НЕ", anchor="ls")[1]


def render_header(category: str, hook: str, date_label: str) -> tuple[Image.Image, tuple[int, int]]:
    """Шапка кадра (рубрика, канал, заголовок) и точка, от которой она «вырастает» в начале."""
    cat = config.CATEGORIES.get(category, config.CATEGORIES["главное"])
    accent = cat["color"]
    canvas = Image.new("RGBA", (W, 900), (0, 0, 0, 0))
    draw = ImageDraw.Draw(canvas)

    # плашка рубрики: точка + название
    pill_font = font(str(config.FONT_BLACK), 40)
    label = cat["label"]
    cap = _cap_height(pill_font)
    pill_h = cap + 2 * 22
    dot_r = 9
    x0, y0 = SAFE_X, 10
    text_x = x0 + 26 + dot_r * 2 + 14
    pill = (x0, y0, int(text_x + pill_font.getlength(label) + 28), y0 + pill_h)
    _rounded(draw, pill, pill_h // 2, (0, 0, 0, 175))
    cy = y0 + pill_h // 2
    draw.ellipse([x0 + 26, cy - dot_r, x0 + 26 + dot_r * 2, cy + dot_r], fill=accent + (255,))
    draw.text((text_x, cy + cap // 2), label, font=pill_font, fill=accent + (255,), anchor="ls")

    # название канала и дата
    small = font(str(config.FONT_BOLD), 32)
    channel = f"{config.CHANNEL_NAME} · {date_label}"
    draw.text((pill[2] + 28, cy + _cap_height(small) // 2), channel, font=small, anchor="ls",
              fill=(255, 255, 255, 220), stroke_width=2, stroke_fill=(0, 0, 0, 110))

    # заголовок: каждая строка на плашке цвета рубрики
    hook = hook.upper()
    ink = text_on(accent)
    size = 86
    while True:
        f = font(str(config.FONT_BLACK), size)
        lines = _wrap(hook, f, W - 2 * SAFE_X - 48)
        if len(lines) <= 3 or size <= 56:
            break
        size -= 6
    cap = _cap_height(f)
    pad_y, pad_x, gap = int(size * 0.3), 24, 12
    box_h = cap + 2 * pad_y
    y = pill[3] + 30
    boxes = []
    for line in lines:
        box = (SAFE_X, y, SAFE_X + int(f.getlength(line)) + 2 * pad_x, y + box_h)
        boxes.append((box, line))
        y = box[3] + gap
    shadow = Image.new("RGBA", canvas.size, (0, 0, 0, 0))
    sdraw = ImageDraw.Draw(shadow)
    for box, _ in boxes:
        sdraw.rounded_rectangle((box[0] + 4, box[1] + 12, box[2] + 4, box[3] + 12), radius=16,
                                fill=(0, 0, 0, 130))
    canvas = Image.alpha_composite(shadow.filter(ImageFilter.GaussianBlur(10)), canvas)
    draw = ImageDraw.Draw(canvas)
    for box, line in boxes:
        _rounded(draw, box, 16, accent + (255,))
        draw.text((box[0] + pad_x, box[1] + pad_y + cap), line, font=f, fill=ink + (255,), anchor="ls")

    right = max(b[0][2] for b in boxes)
    center = ((SAFE_X + right) // 2, HEADER_Y + (pill[1] + boxes[-1][0][3]) // 2)
    return canvas, center


# ─── субтитры ────────────────────────────────────────────────────────────
@dataclass
class Chunk:
    words: list[Word]
    start: float
    end: float


_FUNCTION_WORDS = {
    "в", "во", "на", "и", "с", "со", "к", "ко", "о", "об", "обо", "у", "а", "но", "по", "за", "до",
    "из", "от", "для", "при", "не", "ни", "что", "как", "чем", "без", "над", "под", "про", "через",
}


def caption_text(text: str) -> str:
    """Слово для субтитра: заглавными, без точек/запятых и висящих тире на конце."""
    text = re.sub(r"[\s—–-]+$", "", text.strip())
    text = text.rstrip(".,;:…")
    text = re.sub(r"[\s—–-]+$", "", text)
    return text.upper()


def _line_width(fnt: ImageFont.FreeTypeFont, words: list[str]) -> float:
    gap = fnt.getlength(" ") + 16  # место под подсветку слова
    return sum(fnt.getlength(w) for w in words) + gap * (len(words) - 1)


def make_chunks(words: list[Word], max_words: int = 3) -> list[Chunk]:
    f = font(str(config.FONT_BLACK), CAPTION_SIZE)
    chunks: list[list[Word]] = []
    current: list[Word] = []
    for word in words:
        trial = [caption_text(w.text) for w in current + [word]]
        if current and (len(current) >= max_words or _line_width(f, trial) > CAPTION_MAX_W):
            chunks.append(current)
            current = []
        current.append(word)
        if word.text.endswith((".", "!", "?", "…", ",", ":", ";")):
            chunks.append(current)
            current = []
    if current:
        chunks.append(current)

    # Предлог или союз в конце фразы переносим в следующую: «НА БАЗУ В» → «НА БАЗУ» / «В КЛЕРФОНТЕНЕ»
    for i in range(len(chunks) - 1):
        last = chunks[i][-1]
        if len(chunks[i]) > 1 and normalize_word(last.text) in _FUNCTION_WORDS \
                and normalize_word(last.text) == last.text.lower().strip():
            chunks[i + 1].insert(0, chunks[i].pop())

    result = []
    for i, group in enumerate(chunks):
        start = group[0].start
        natural_end = group[-1].end + 0.25
        next_start = chunks[i + 1][0].start if i + 1 < len(chunks) else natural_end + 10
        end = next_start if next_start - group[-1].end < 0.7 else natural_end
        result.append(Chunk(group, start, end))
    return result


def render_caption(chunk: Chunk, active: int, accent, plain: bool = False) -> Image.Image:
    """plain — простые субтитры для стиля sasha: белый текст с обводкой, без плашки."""
    if plain:
        return _render_plain_caption(chunk)
    size = CAPTION_SIZE
    words = [caption_text(w.text) for w in chunk.words]
    while True:
        f = font(str(config.FONT_BLACK), size)
        space = f.getlength(" ") + 16
        widths = [f.getlength(w) for w in words]
        total = _line_width(f, words)
        if total <= CAPTION_MAX_W or size <= 40:
            break
        size -= 4
    asc, desc = f.getmetrics()
    height = asc + desc + 60
    canvas = Image.new("RGBA", (W, height), (0, 0, 0, 0))
    draw = ImageDraw.Draw(canvas)
    x = (W - total) / 2
    baseline = 30 + asc
    ink_on_accent = text_on(accent)
    for i, (word, width) in enumerate(zip(words, widths)):
        if i == active:
            bbox = f.getbbox(word, anchor="ls")
            box = (x - 14, baseline + bbox[1] - 12, x + width + 14, baseline + bbox[3] + 14)
            draw.rounded_rectangle(box, radius=14, fill=accent + (255,))
            draw.text((x, baseline), word, font=f, fill=ink_on_accent + (255,), anchor="ls")
        else:
            draw.text((x, baseline), word, font=f, fill=(255, 255, 255, 255), anchor="ls",
                      stroke_width=9, stroke_fill=(0, 0, 0, 255))
        x += width + space
    return canvas


def _render_plain_caption(chunk: Chunk) -> Image.Image:
    text = " ".join(w.text for w in chunk.words)
    text = re.sub(r"[\s—–-]+$", "", text.strip()).rstrip(".,;:…")
    size = 64
    while True:
        f = font(str(config.FONT_BOLD), size)
        if f.getlength(text) <= CAPTION_MAX_W or size <= 40:
            break
        size -= 4
    asc, desc = f.getmetrics()
    canvas = Image.new("RGBA", (W, asc + desc + 40), (0, 0, 0, 0))
    draw = ImageDraw.Draw(canvas)
    draw.text((W / 2, 20 + asc), text, font=f, fill=(255, 255, 255, 255), anchor="ms",
              stroke_width=6, stroke_fill=(0, 0, 0, 230))
    return canvas


# ─── фон через ffmpeg ────────────────────────────────────────────────────
def _gradient_png(path: Path, top_a: int, bottom_a: int, base_a: int) -> Path:
    """Затемнение сверху (под заголовок) и снизу (под субтитры)."""
    y = np.arange(H, dtype=np.float32)
    top = np.clip(1 - y / 900, 0, 1) ** 1.6 * top_a
    bottom = np.clip((y - 780) / (H - 780), 0, 1) ** 1.3 * bottom_a
    alpha = np.clip(np.maximum(top, bottom) + base_a, 0, 255).astype(np.uint8)
    arr = np.zeros((H, W, 4), dtype=np.uint8)
    arr[..., 3] = alpha[:, None]
    Image.fromarray(arr, "RGBA").save(path)
    return path


def _clip_len(path: Path) -> float:
    try:
        return probe_duration(path)
    except Exception:
        return 0.0


def _motion_focus(clip: Path, start: float, length: float) -> tuple[float, float] | None:
    """Где в кадре больше всего движения (доли ширины и высоты видимой части кадра).
    По этой точке повтор «приближает» самое интересное."""
    w, h = 54, 96
    return _focus_from(clip, start, length, w, h,
                       f"scale={W}:{H}:force_original_aspect_ratio=increase,crop={W}:{H},scale={w}:{h}")


def _motion_focus_full(clip: Path, start: float, length: float, width: int, height: int
                       ) -> tuple[float, float] | None:
    """То же, но по всему кадру исходного видео (для видео из X любой ориентации)."""
    if width >= height:
        w, h = 96, max(8, int(round(96 * height / max(1, width))))
    else:
        w, h = max(8, int(round(96 * width / max(1, height)))), 96
    return _focus_from(clip, start, length, w, h, f"scale={w}:{h}")


def _focus_from(clip: Path, start: float, length: float, w: int, h: int, scale: str
                ) -> tuple[float, float] | None:
    cmd = ["ffmpeg", "-v", "error", "-ss", f"{start:.3f}", "-t", f"{max(length, 0.5):.3f}", "-i", str(clip),
           "-vf", f"{scale},fps=10,format=gray", "-f", "rawvideo", "pipe:1"]
    try:
        raw = subprocess.run(cmd, capture_output=True, timeout=60).stdout
    except Exception:
        return None
    frames = np.frombuffer(raw[: len(raw) // (w * h) * (w * h)], dtype=np.uint8).reshape(-1, h, w).astype(np.float32)
    if len(frames) < 3:
        return None
    motion = np.abs(np.diff(frames, axis=0)).sum(axis=0)
    kernel = np.ones(9, dtype=np.float32) / 9  # размываем, чтобы искать область, а не один пиксель
    motion = np.apply_along_axis(lambda r: np.convolve(r, kernel, mode="same"), 1, motion)
    motion = np.apply_along_axis(lambda c: np.convolve(c, kernel, mode="same"), 0, motion)
    # почти нет движения или оно размазано по всему кадру — точки интереса нет
    if motion.max() < 4 * (len(frames) - 1) or motion.max() < 1.8 * motion.mean():
        return None
    y, x = np.unravel_index(int(np.argmax(motion)), motion.shape)
    return (x + 0.5) / w, (y + 0.5) / h


# ─── видео из X как фон ──────────────────────────────────────────────────
@dataclass(frozen=True)
class ClipPlan:
    start: float           # откуда начинается основная часть ролика
    replay_start: float    # откуда берётся повтор (яркий момент)
    focus: tuple           # точка интереса в полном кадре (доли ширины и высоты)
    width: int
    height: int
    has_audio: bool
    length: float


def _probe_video(path: Path) -> tuple[int, int, bool, float]:
    """Размер кадра (с учётом поворота), есть ли звук и длина видео."""
    out = subprocess.run(
        ["ffprobe", "-v", "error", "-show_entries",
         "stream=codec_type,width,height:stream_tags=rotate:stream_side_data=rotation:format=duration",
         "-of", "json", str(path)], capture_output=True, text=True, timeout=60).stdout
    data = json.loads(out or "{}")
    streams = data.get("streams", [])
    video = next((st for st in streams if st.get("codec_type") == "video"), {})
    width, height = int(video.get("width") or W), int(video.get("height") or H)
    rotation = (video.get("tags") or {}).get("rotate") or next(
        (sd.get("rotation") for sd in video.get("side_data_list") or [] if "rotation" in sd), 0)
    try:
        if abs(int(float(rotation))) % 180 == 90:
            width, height = height, width
    except (TypeError, ValueError):
        pass
    has_audio = any(st.get("codec_type") == "audio" for st in streams)
    duration = float((data.get("format") or {}).get("duration") or 0)
    return width, height, has_audio, duration


def _motion_profile(clip: Path, fps: int) -> np.ndarray | None:
    """Сколько движения в каждом кадре (без склеек и смен камеры)."""
    w, h = 64, 36
    cmd = ["ffmpeg", "-v", "error", "-i", str(clip), "-vf", f"scale={w}:{h},fps={fps},format=gray",
           "-f", "rawvideo", "pipe:1"]
    try:
        raw = subprocess.run(cmd, capture_output=True, timeout=180).stdout
    except Exception:
        return None
    n = len(raw) // (w * h)
    if n < 4:
        return None
    frames = np.frombuffer(raw[: n * w * h], dtype=np.uint8).reshape(n, h, w).astype(np.float32)
    diff = np.abs(np.diff(frames, axis=0)).mean(axis=(1, 2))
    median = float(np.median(diff))
    diff[diff > max(18.0, 4 * median)] = median  # склейка — это не движение
    return diff


def _audio_profile(clip: Path, fps: int) -> np.ndarray | None:
    """Громкость звука (дБ) с шагом 1/fps секунды."""
    rate = 8000
    cmd = ["ffmpeg", "-v", "error", "-i", str(clip), "-vn", "-ac", "1", "-ar", str(rate), "-f", "s16le", "pipe:1"]
    try:
        raw = subprocess.run(cmd, capture_output=True, timeout=180).stdout
    except Exception:
        return None
    samples = np.frombuffer(raw[: len(raw) // 2 * 2], dtype=np.int16).astype(np.float32)
    hop = rate // fps
    n = len(samples) // hop
    if n < 4:
        return None
    frames = samples[: n * hop].reshape(n, hop)
    return 20 * np.log10(np.sqrt((frames ** 2).mean(axis=1)) + 1.0)


def _key_window(clip: Path, length: float, window: float, has_audio: bool) -> float:
    """Начало самого яркого момента длиной window секунд.

    Сначала ищем всплеск звука — рёв трибун или крик комментатора звучат сразу после гола,
    поэтому момент берём чуть раньше всплеска. Если звука нет или он ровный (видео под музыку),
    ищем отрезок с наибольшим движением в кадре."""
    fps = 6
    if length <= window + 0.2:
        return 0.0
    latest = length - window - 0.1
    if has_audio:
        loud = _audio_profile(clip, fps)
        if loud is not None and len(loud) > 2 * fps:
            smooth = np.convolve(loud, np.ones(3) / 3, mode="same")
            peak = int(np.argmax(smooth))
            if smooth[peak] - float(np.median(smooth)) >= 6.0:
                return float(min(max(0.0, peak / fps - window * 0.75), latest))
    motion = _motion_profile(clip, fps)
    if motion is not None:
        win = max(1, int(round(window * fps)))
        if len(motion) > win:
            sums = np.convolve(motion, np.ones(win), mode="valid")
            return float(min(max(0.0, int(np.argmax(sums)) / fps), latest))
    return float(min(max(0.0, length * 0.55 - window / 2), latest))


@lru_cache(maxsize=8)
def _clip_plan(clip: str, main_len: float, replay_len: float) -> ClipPlan:
    path = Path(clip)
    width, height, has_audio, length = _probe_video(path)
    length = length or _clip_len(path)
    source = replay_len * config.REPLAY_SPEED
    key = _key_window(path, length, source, has_audio) if source > 0 else max(0.0, length * 0.3)
    if length <= main_len + 0.2:
        start = 0.0  # видео короче голоса — крутим по кругу
    else:
        # основная часть заканчивается чуть позже яркого момента, дальше — его повтор
        start = min(max(0.0, key + source + 0.8 - main_len), length - main_len - 0.1)
    focus = (_motion_focus_full(path, key, source, width, height) if source > 0 else None) or (0.5, 0.45)
    log.info("Видео из X: %dx%d, %.1f с, звук: %s, яркий момент с %.1f с", width, height, length,
             "есть" if has_audio else "нет", key)
    return ClipPlan(round(start, 3), round(key, 3), focus, width, height, has_audio, length)


def _clip_main_filter(idx: int, plan: ClipPlan, main_len: float) -> str:
    """Основная часть: вертикальное видео — на весь экран; горизонтальное — крупно по центру
    на размытом фоне из того же видео."""
    tail = f"setsar=1,fps={FPS},format=yuv420p,trim=duration={main_len:.3f},setpts=PTS-STARTPTS[s{idx}]"
    ratio = plan.width / max(1, plan.height)
    if ratio <= 0.8:
        return (f"[{idx}:v]scale={W}:{H}:force_original_aspect_ratio=increase:flags=bicubic,"
                f"crop={W}:{H},{tail}")
    fg_w = max(W, int(W * (config.CLIP_ZOOM if ratio >= 1.2 else 1.0)) // 2 * 2)
    fg_h = min(H, int(round(fg_w / ratio / 2)) * 2)
    crop = f",crop={W}:{fg_h}" if fg_w > W else ""
    small_w, small_h = W // 4, H // 4
    return (f"[{idx}:v]split=2[c{idx}a][c{idx}b];"
            f"[c{idx}a]scale={small_w}:{small_h}:force_original_aspect_ratio=increase,crop={small_w}:{small_h},"
            f"boxblur=10:2,eq=brightness=-0.12:saturation=0.85,scale={W}:{H}:flags=bilinear[c{idx}bg];"
            f"[c{idx}b]scale={fg_w}:{fg_h}:flags=bicubic{crop}[c{idx}fg];"
            f"[c{idx}bg][c{idx}fg]overlay=0:{(H - fg_h) // 2},{tail}")


def _clip_replay_filter(idx: int, plan: ClipPlan, replay_len: float) -> str:
    """Повтор яркого момента: на весь экран, крупно вокруг точки интереса, замедленно."""
    w0, h0 = plan.width, plan.height
    zoom = config.REPLAY_ZOOM if w0 / max(1, h0) <= 0.8 else 1.0  # горизонтальное и так станет крупнее
    base_h = min(h0, w0 * 16 / 9)
    ch = max(16, int(base_h / zoom) // 2 * 2)
    cw = max(16, int(ch * 9 / 16) // 2 * 2)
    fx, fy = plan.focus
    cx = int(min(max(0.0, fx * w0 - cw / 2), w0 - cw)) // 2 * 2
    cy = int(min(max(0.0, fy * h0 - ch / 2), h0 - ch)) // 2 * 2
    return (f"[{idx}:v]crop={cw}:{ch}:{cx}:{cy},scale={W}:{H}:flags=bicubic,setsar=1,"
            f"setpts=(PTS-STARTPTS)/{config.REPLAY_SPEED},fps={FPS},format=yuv420p,"
            f"trim=duration={replay_len:.3f},setpts=PTS-STARTPTS[s{idx}]")


def _split(total: float, style: str) -> tuple[float, float]:
    """Длина основной части и повтора в конце."""
    replay_len = replay_length(total) if style == "sasha" else 0.0
    return total - replay_len, replay_len


def replay_length(total: float) -> float:
    """Длина повтора в конце ролика (стиль sasha)."""
    if total < 9:
        return round(total * 0.3, 3)
    return round(min(9.0, max(3.5, total * config.REPLAY_SHARE)), 3)


def _video_graph(bg: Background, total: float, seed: int, style: str) -> tuple[list[str], list[str], int]:
    """Входы и фильтры ffmpeg, которые собирают фон под меткой [bg].
    В стиле sasha в конце добавляется повтор последнего плана: зум и замедление."""
    rng = random.Random(seed)
    replay = style == "sasha"
    main_len, replay_len = _split(total, style)
    zoom = config.REPLAY_ZOOM
    args: list[str] = []
    filters: list[str] = []
    idx = 0
    if bg.kind == "clip" and bg.files:
        clip = bg.files[0]
        plan = _clip_plan(str(clip), round(main_len, 3), round(replay_len, 3))
        args += ["-stream_loop", "-1", "-ss", f"{plan.start:.3f}", "-t", f"{main_len + 0.2:.3f}", "-i", str(clip)]
        filters.append(_clip_main_filter(idx, plan, main_len))
        idx += 1
        if replay:
            source_len = replay_len * config.REPLAY_SPEED
            args += ["-stream_loop", "-1", "-ss", f"{plan.replay_start:.3f}", "-t", f"{source_len + 0.3:.3f}",
                     "-i", str(clip)]
            filters.append(_clip_replay_filter(idx, plan, replay_len))
            idx += 1
    elif bg.kind == "video" and bg.files:
        n = max(1, math.ceil(main_len / config.SEGMENT_SECONDS))
        seg = main_len / n
        order = list(bg.files)
        rng.shuffle(order)
        last = None
        for i in range(n):
            clip = order[i % len(order)]
            clip_len = _clip_len(clip) or seg
            offset = rng.uniform(0, max(0.0, clip_len - seg - 0.3))
            args += ["-stream_loop", "-1", "-ss", f"{offset:.3f}", "-t", f"{seg + 0.2:.3f}", "-i", str(clip)]
            filters.append(
                f"[{idx}:v]scale={W}:{H}:force_original_aspect_ratio=increase:flags=bicubic,"
                f"crop={W}:{H},setsar=1,fps={FPS},format=yuv420p,"
                f"trim=duration={seg:.3f},setpts=PTS-STARTPTS[s{idx}]"
            )
            last = (clip, offset, seg)
            idx += 1
        if replay and last:
            clip, offset, seg = last
            source_len = replay_len * config.REPLAY_SPEED
            r_offset = offset + max(0.0, seg - source_len)  # повторяем конец последнего плана
            focus = _motion_focus(clip, r_offset, source_len) or (rng.uniform(0.4, 0.6), rng.uniform(0.4, 0.6))
            zw, zh = int(W * zoom) // 2 * 2, int(H * zoom) // 2 * 2
            x = min(max(0, int(focus[0] * zw - W / 2)), zw - W)
            y = min(max(0, int(focus[1] * zh - H / 2)), zh - H)
            args += ["-stream_loop", "-1", "-ss", f"{r_offset:.3f}", "-t", f"{source_len + 0.3:.3f}",
                     "-i", str(clip)]
            filters.append(
                f"[{idx}:v]scale={W}:{H}:force_original_aspect_ratio=increase:flags=bicubic,crop={W}:{H},"
                f"scale={zw}:{zh}:flags=bicubic,crop={W}:{H}:{x}:{y},setsar=1,"
                f"setpts=(PTS-STARTPTS)/{config.REPLAY_SPEED},fps={FPS},format=yuv420p,"
                f"trim=duration={replay_len:.3f},setpts=PTS-STARTPTS[s{idx}]"
            )
            idx += 1
    else:
        png = bg.files[0]
        args += ["-loop", "1", "-framerate", str(FPS), "-t", f"{main_len:.3f}", "-i", str(png)]
        filters.append(
            f"[{idx}:v]crop={W}:{H}:x='(iw-{W})/2+(iw-{W})/2*sin(t*0.21)':"
            f"y='(ih-{H})/2+(ih-{H})/2*sin(t*0.16+1.3)',setsar=1,fps={FPS},format=yuv420p[s{idx}]"
        )
        idx += 1
        if replay:
            args += ["-loop", "1", "-framerate", str(FPS), "-t", f"{replay_len:.3f}", "-i", str(png)]
            filters.append(
                f"[{idx}:v]scale=iw*{zoom}:ih*{zoom},crop={W}:{H}:x='(iw-{W})/2+60*sin(t*0.6)':"
                f"y='(ih-{H})*0.42',setsar=1,fps={FPS},format=yuv420p[s{idx}]"
            )
            idx += 1
    grade = "eq=contrast=1.05:saturation=1.08" if style == "sasha" else "eq=brightness=-0.05:saturation=1.1"
    concat = "".join(f"[s{i}]" for i in range(idx))
    filters.append(f"{concat}concat=n={idx}:v=1:a=0,{grade}[bg]")
    return args, filters, idx


def _audio_graph(first_input: int, speech: Speech, music: Path | None, total: float,
                 lead: float, bed: tuple[Path, float, float] | None = None) -> tuple[list[str], list[str]]:
    """Голос с задержкой lead; под ним, если есть, тихая музыка и звук исходного видео → метка [a].
    bed — (видео, с какой секунды, сколько секунд): звук видео из X под основной частью."""
    args = ["-i", str(speech.audio)]
    lead_ms = int(lead * 1000)
    stereo = ",aformat=sample_rates=48000:channel_layouts=stereo" if (music or bed) else ""
    graph = [f"[{first_input}:a]aresample=48000,adelay={lead_ms}:all=1{stereo},apad[vo]"]
    mix, nxt = ["[vo]"], first_input + 1
    if music:
        args += ["-stream_loop", "-1", "-i", str(music)]
        fade_out = max(0.0, total - 1.6)
        graph.append(f"[{nxt}:a]aresample=48000{stereo},volume={config.MUSIC_VOLUME},"
                     f"afade=t=in:st=0:d=0.8,afade=t=out:st={fade_out:.2f}:d=1.5[mu]")
        mix.append("[mu]")
        nxt += 1
    if bed:
        clip, start, length = bed
        args += ["-stream_loop", "-1", "-ss", f"{start:.3f}", "-t", f"{length + 0.2:.3f}", "-i", str(clip)]
        graph.append(f"[{nxt}:a]aresample=48000{stereo},volume={config.CLIP_VOLUME},"
                     f"afade=t=in:st=0:d=0.3,afade=t=out:st={max(0.0, length - 0.8):.2f}:d=0.8,apad[bd]")
        mix.append("[bd]")
        nxt += 1
    if len(mix) > 1:
        graph.append(f"{''.join(mix)}amix=inputs={len(mix)}:duration=first:dropout_transition=0:normalize=0,"
                     "alimiter=limit=0.95[a]")
    else:
        graph.append("[vo]anull[a]")
    return args, graph


def _clip_bed(background: Background, total: float, style: str) -> tuple[Path, float, float] | None:
    """Звук видео из X под основной частью ролика (если в видео есть звук и он включён)."""
    if background.kind != "clip" or not background.files or config.CLIP_VOLUME <= 0:
        return None
    main_len, replay_len = _split(total, style)
    plan = _clip_plan(str(background.files[0]), round(main_len, 3), round(replay_len, 3))
    return (background.files[0], plan.start, main_len) if plan.has_audio else None


ENCODE = ["-c:v", "libx264", "-preset", "veryfast", "-crf", str(config.VIDEO_CRF),
          "-pix_fmt", "yuv420p", "-profile:v", "high", "-r", str(FPS),
          "-c:a", "aac", "-b:a", "160k", "-ar", "48000", "-movflags", "+faststart"]


def pick_music(mood: str) -> Path | None:
    exts = {".mp3", ".m4a", ".wav", ".ogg", ".aac", ".flac"}
    folder = config.MUSIC_DIR
    candidates = [p for p in (folder / mood).glob("*") if p.suffix.lower() in exts] if (folder / mood).is_dir() else []
    if not candidates and folder.is_dir():
        candidates = [p for p in folder.glob("*") if p.suffix.lower() in exts]
    return random.choice(candidates) if candidates else None


def _ease_out_back(t: float) -> float:
    c1, c3 = 1.70158, 2.70158
    return 1 + c3 * (t - 1) ** 3 + c1 * (t - 1) ** 2


def _read_exact(stream, buffer: bytearray) -> bool:
    view = memoryview(buffer)
    got = 0
    while got < len(buffer):
        n = stream.readinto(view[got:])
        if not n:
            return False
        got += n
    return True


# ─── сборка ролика ───────────────────────────────────────────────────────
def _render_direct(background: Background, speech: Speech, music: Path | None, total: float,
                   lead: float, out_path: Path, seed: int, style: str) -> None:
    """Ролик без надписей: весь монтаж делает один вызов ffmpeg (быстро)."""
    v_args, v_graph, n = _video_graph(background, total, seed, style)
    a_args, a_graph = _audio_graph(n, speech, music, total, lead, _clip_bed(background, total, style))
    cmd = (["ffmpeg", "-y", "-v", "error", "-nostdin"] + v_args + a_args
           + ["-filter_complex", ";".join(v_graph + a_graph), "-map", "[bg]", "-map", "[a]",
              "-t", f"{total:.3f}"] + ENCODE + [str(out_path)])
    result = subprocess.run(cmd, capture_output=True, text=True)
    if result.returncode != 0 or not out_path.exists():
        raise RuntimeError(f"видео не собралось: {result.stderr[-800:]}")


def render_short(category: str, hook: str, speech: Speech, background: Background,
                 out_path: Path, date_label: str, music: Path | None = None, seed: int = 0,
                 style: str | None = None) -> dict:
    style = style or config.VIDEO_STYLE
    accent = config.CATEGORIES.get(category, config.CATEGORIES["главное"])["color"]
    human = speech.engine == "human"
    lead = 0.12 if human else config.LEAD_IN
    tail = 0.4 if style == "sasha" else config.TAIL
    words = [Word(w.text, w.start + lead, w.end + lead) for w in speech.words]
    last_word_end = words[-1].end if words else lead + speech.duration
    total = max(lead + speech.duration, last_word_end) + tail
    n_frames = int(round(total * FPS))

    show_header = style == "news"
    show_progress = style == "news"
    show_captions = style == "news" or config.SUBTITLES
    plain_captions = style != "news"

    if not (show_header or show_progress or show_captions):
        _render_direct(background, speech, music, total, lead, out_path, seed, style)
        return {"duration": round(total, 2), "frames": n_frames,
                "size_mb": round(out_path.stat().st_size / 1e6, 1)}

    chunks = make_chunks(words) if show_captions else []
    caption_y = CAPTION_CENTER_Y if not plain_captions else PLAIN_CAPTION_Y
    if show_header:
        header_img, header_center = render_header(category, hook, date_label)
        header_layer = Layer.from_image(header_img, 0, HEADER_Y)
    intro = 0.45

    with tempfile.TemporaryDirectory() as tmp:
        if style == "news":
            soft = background.kind not in ("video", "clip")
            gradient = _gradient_png(Path(tmp) / "gradient.png", *((110, 130, 0) if soft else (185, 200, 45)))
        else:
            gradient = _gradient_png(Path(tmp) / "gradient.png", 0, 150, 0)  # только под субтитры
        v_args, v_graph, n = _video_graph(background, total, seed, style)
        v_args += ["-loop", "1", "-framerate", str(FPS), "-t", f"{total:.3f}", "-i", str(gradient)]
        v_graph.append(f"[bg][{n}:v]overlay=0:0:format=auto,format=rgb24[out]")
        dec_cmd = (["ffmpeg", "-v", "error", "-nostdin"] + v_args
                   + ["-filter_complex", ";".join(v_graph), "-map", "[out]", "-t", f"{total:.3f}",
                      "-f", "rawvideo", "-pix_fmt", "rgb24", "pipe:1"])
        a_args, a_graph = _audio_graph(1, speech, music, total, lead, _clip_bed(background, total, style))
        enc_cmd = (["ffmpeg", "-y", "-v", "error", "-f", "rawvideo", "-pix_fmt", "rgb24",
                    "-s", f"{W}x{H}", "-r", str(FPS), "-i", "pipe:0"] + a_args
                   + ["-filter_complex", ";".join(a_graph), "-map", "0:v", "-map", "[a]",
                      "-t", f"{total:.3f}"] + ENCODE + [str(out_path)])
        enc_log = open(Path(tmp) / "encoder.log", "w+")
        dec_log = open(Path(tmp) / "decoder.log", "w+")
        decoder = subprocess.Popen(dec_cmd, stdout=subprocess.PIPE, stderr=dec_log)
        encoder = subprocess.Popen(enc_cmd, stdin=subprocess.PIPE, stderr=enc_log)

        frame_bytes = bytearray(W * H * 3)
        frame = np.frombuffer(frame_bytes, dtype=np.uint8).reshape(H, W, 3)
        last_bg = np.zeros_like(frame)
        have_frame = False
        caption_key, caption_layer, caption_img = None, None, None
        track_bg = np.array([255, 255, 255], dtype=np.uint16)
        try:
            for i in range(n_frames):
                t = i / FPS
                if _read_exact(decoder.stdout, frame_bytes):
                    have_frame = True
                    np.copyto(last_bg, frame)
                elif not have_frame:
                    raise RuntimeError("фон не собрался")
                else:
                    # фон закончился на долю секунды раньше — повторяем последний кадр фона
                    np.copyto(frame, last_bg)

                # шапка с анимацией появления
                if show_header:
                    if t < intro:
                        p = t / intro
                        scale = 0.6 + 0.4 * _ease_out_back(p)
                        opacity = min(1.0, p * 1.8)
                        w0, h0 = header_img.size
                        scaled = header_img.resize((max(1, int(w0 * scale)), max(1, int(h0 * scale))),
                                                   Image.BILINEAR)
                        cx, cy = header_center
                        x = int(cx - (cx * scale))
                        y = int(cy - (cy - HEADER_Y) * scale)
                        blend_image(frame, scaled, x, y, opacity)
                    elif header_layer:
                        blend(frame, header_layer)

                # субтитры
                current = None
                for idx, chunk in enumerate(chunks):
                    if chunk.start <= t < chunk.end:
                        current = idx
                        break
                if current is not None:
                    chunk = chunks[current]
                    active = 0
                    for k, word in enumerate(chunk.words):
                        if t >= word.start:
                            active = k
                    key = (current, active)
                    if key != caption_key:
                        caption_key = key
                        caption_img = render_caption(chunk, active, accent, plain=plain_captions)
                        caption_layer = Layer.from_image(caption_img, 0, caption_y - caption_img.height // 2)
                    age = t - chunk.start
                    if age < 0.1 and not plain_captions:  # лёгкий «прыжок» при появлении фразы
                        scale = 0.86 + 0.14 * (age / 0.1)
                        w0, h0 = caption_img.size
                        scaled = caption_img.resize((int(w0 * scale), int(h0 * scale)), Image.BILINEAR)
                        blend_image(frame, scaled, int((W - scaled.width) / 2), caption_y - scaled.height // 2)
                    elif caption_layer:
                        blend(frame, caption_layer)

                # полоса прогресса сверху
                if show_progress:
                    bar_h = 9
                    filled = int(W * min(1.0, t / max(total - tail, 0.1)))
                    top = frame[0:bar_h]
                    top[:] = _div255(top.astype(np.uint16) * 190 + track_bg * 65)
                    if filled > 0:
                        frame[0:bar_h, :filled] = accent

                encoder.stdin.write(frame_bytes)
        except BrokenPipeError:
            problem = "кодировщик ffmpeg остановился раньше времени"
        except Exception as error:
            problem = str(error)
        else:
            problem = None
        finally:
            try:
                encoder.stdin.close()
            except Exception:
                pass
            if decoder.poll() is None:
                decoder.kill()
            decoder.wait()
            code = encoder.wait()
            enc_log.seek(0)
            dec_log.seek(0)
            enc_err, dec_err = enc_log.read(), dec_log.read()
            enc_log.close()
            dec_log.close()
        if problem or code != 0 or not out_path.exists():
            details = (enc_err[-600:] + " " + dec_err[-600:]).strip()
            raise RuntimeError(f"видео не собралось ({problem or f'код {code}'}): {details}")

    return {"duration": round(total, 2), "frames": n_frames,
            "size_mb": round(out_path.stat().st_size / 1e6, 1)}


TELEGRAM_LIMIT_MB = 49


def fit_for_telegram(path: Path) -> None:
    """Telegram-бот принимает файлы до 50 МБ. Если ролик больше — пережимаем."""
    if path.stat().st_size / 1e6 <= TELEGRAM_LIMIT_MB:
        return
    tmp = path.with_suffix(".small.mp4")
    subprocess.run(["ffmpeg", "-y", "-v", "error", "-i", str(path), "-c:v", "libx264", "-preset", "veryfast",
                    "-crf", "28", "-c:a", "copy", "-movflags", "+faststart", str(tmp)], check=True)
    tmp.replace(path)
