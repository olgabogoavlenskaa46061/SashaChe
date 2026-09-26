"""Фон для роликов.

Порядок: свои кадры из папки assets/footage → бесплатные стоковые видео Pexels
(их лицензия разрешает использовать ролики без оплаты) → нарисованное футбольное поле.
Кадры из матчей и фото из новостей не используются — на них чужие права.
"""
from __future__ import annotations

import logging
import math
import random
from dataclasses import dataclass, field
from pathlib import Path

import numpy as np
import requests
from PIL import Image, ImageDraw, ImageFilter

import config

log = logging.getLogger(__name__)

PEXELS_SEARCH = "https://api.pexels.com/videos/search"
GENERIC_QUERIES = ["football stadium crowd", "soccer match stadium", "football fans cheering stands",
                   "soccer players on pitch", "football match night"]
VIDEO_EXTS = {".mp4", ".mov", ".m4v", ".webm", ".mkv"}


@dataclass
class Background:
    kind: str                         # "video" или "image"
    files: list[Path]
    credits: list[str] = field(default_factory=list)
    footage_ids: list[str] = field(default_factory=list)


# ─── Pexels ──────────────────────────────────────────────────────────────
def _pick_file(video: dict) -> dict | None:
    files = [f for f in video.get("video_files", [])
             if f.get("file_type") == "video/mp4" and f.get("width") and f.get("height")]
    if not files:
        return None

    def score(f: dict) -> float:
        w, h = f["width"], f["height"]
        portrait_bonus = 0 if h > w else 5000
        too_big = 3000 if h > 2200 else 0
        return abs(h - 1920) + portrait_bonus + too_big

    return min(files, key=score)


def _search(query: str, orientation: str | None) -> list[dict]:
    params = {"query": query, "per_page": 15, "size": "medium"}
    if orientation:
        params["orientation"] = orientation
    response = requests.get(PEXELS_SEARCH, params=params, timeout=20,
                            headers={"Authorization": config.PEXELS_API_KEY})
    response.raise_for_status()
    return response.json().get("videos", [])


def _download(url: str, path: Path) -> Path:
    if path.exists() and path.stat().st_size > 0:
        return path
    tmp = path.with_suffix(".part")
    with requests.get(url, stream=True, timeout=60) as response:
        response.raise_for_status()
        with open(tmp, "wb") as out:
            for chunk in response.iter_content(1 << 20):
                out.write(chunk)
    tmp.rename(path)
    return path


def pexels_background(queries: list[str], duration: float, used_ids: set[str]) -> Background | None:
    if not config.PEXELS_API_KEY:
        return None
    need = min(6, max(2, math.ceil(duration / config.SEGMENT_SECONDS)))
    cache = config.CACHE_DIR / "pexels"
    cache.mkdir(parents=True, exist_ok=True)
    chosen: list[tuple[dict, dict]] = []
    seen: set[str] = set()
    for query in list(queries) + GENERIC_QUERIES:
        per_query = 0  # из одного запроса берём не больше двух клипов — так разнообразнее
        for orientation in ("portrait", None):
            if per_query >= 2 or len(chosen) >= need:
                break
            try:
                videos = _search(query, orientation)
            except Exception as error:
                log.warning("Pexels: поиск «%s» не удался: %s", query, error)
                videos = []
            random.shuffle(videos)
            for video in videos:
                if per_query >= 2 or len(chosen) >= need:
                    break
                vid = str(video.get("id"))
                if vid in seen or vid in used_ids or (video.get("duration") or 0) < 4:
                    continue
                file = _pick_file(video)
                if file is None or file["height"] < 1000:
                    continue
                seen.add(vid)
                chosen.append((video, file))
                per_query += 1
        if len(chosen) >= need:
            break
    if not chosen:
        return None

    files, credits, ids = [], [], []
    for video, file in chosen[:need]:
        try:
            path = cache / f"pexels_{video['id']}_{file.get('id', 'f')}.mp4"
            files.append(_download(file["link"], path))
            author = (video.get("user") or {}).get("name", "Pexels")
            credits.append(f"{author} / Pexels")
            ids.append(str(video["id"]))
        except Exception as error:
            log.warning("Pexels: не скачалось видео %s: %s", video.get("id"), error)
    if not files:
        return None
    return Background("video", files, list(dict.fromkeys(credits)), ids)


# ─── Нарисованный фон ────────────────────────────────────────────────────
def generated_background(category: str, path: Path, seed: int = 0) -> Path:
    """Тёмно-зелёное «поле сверху» с подсветкой цвета рубрики.
    Картинка больше кадра — при монтаже она медленно плывёт."""
    rng = random.Random(seed)
    w, h = int(config.WIDTH * 1.22), int(config.HEIGHT * 1.22)
    accent = np.array(config.CATEGORIES.get(category, config.CATEGORIES["главное"])["color"], dtype=np.float32)
    top = np.array((14, 58, 36), dtype=np.float32)
    bottom = np.array((5, 22, 14), dtype=np.float32)

    # вертикальный градиент газона
    t = np.linspace(0, 1, h, dtype=np.float32)[:, None, None]
    img = top[None, None, :] * (1 - t) + bottom[None, None, :] * t
    img = np.repeat(img, w, axis=1)

    yy, xx = np.mgrid[0:h, 0:w].astype(np.float32)
    # полосы «стриженого газона»
    stripes = ((yy // 170) % 2) * 0.09
    img = img * (1 + stripes[..., None])

    # подсветка цвета рубрики (как прожектор)
    cx, cy = w * rng.uniform(0.3, 0.7), h * rng.uniform(0.25, 0.4)
    dist = np.sqrt(((xx - cx) / (w * 0.6)) ** 2 + ((yy - cy) / (h * 0.4)) ** 2)
    glow = (np.clip(1 - dist, 0, 1) ** 1.6 * 0.42)[..., None]
    img = img * (1 - glow) + accent[None, None, :] * glow

    # виньетка
    vx = (xx - w / 2) / (w / 2)
    vy = (yy - h / 2) / (h / 2)
    vignette = np.clip(1 - 0.35 * (vx ** 2 + vy ** 2), 0.45, 1)[..., None]
    img = img * vignette

    base = Image.fromarray(np.clip(img, 0, 255).astype(np.uint8), "RGB").convert("RGBA")

    # разметка поля
    lines = Image.new("RGBA", (w, h), (0, 0, 0, 0))
    draw = ImageDraw.Draw(lines)
    line = (255, 255, 255, 48)
    lw = 7
    m = int(w * 0.09)
    left, right, upper, lower = m, w - m, int(h * 0.05), int(h * 0.95)
    draw.rectangle([left, upper, right, lower], outline=line, width=lw)
    mid = (upper + lower) // 2
    draw.line([left, mid, right, mid], fill=line, width=lw)
    r = int(w * 0.2)
    draw.ellipse([w // 2 - r, mid - r, w // 2 + r, mid + r], outline=line, width=lw)
    draw.ellipse([w // 2 - 12, mid - 12, w // 2 + 12, mid + 12], fill=line)
    box_w, box_h = int((right - left) * 0.6), int(h * 0.15)
    goal_w, goal_h = int((right - left) * 0.28), int(h * 0.055)
    for y0, sign in ((upper, 1), (lower, -1)):
        bx0, bx1 = w // 2 - box_w // 2, w // 2 + box_w // 2
        gx0, gx1 = w // 2 - goal_w // 2, w // 2 + goal_w // 2
        by1, gy1 = y0 + sign * box_h, y0 + sign * goal_h
        draw.rectangle([bx0, min(y0, by1), bx1, max(y0, by1)], outline=line, width=lw)
        draw.rectangle([gx0, min(y0, gy1), gx1, max(y0, gy1)], outline=line, width=lw)
        spot = y0 + sign * int(box_h * 0.72)
        draw.ellipse([w // 2 - 10, spot - 10, w // 2 + 10, spot + 10], fill=line)
    lines = lines.filter(ImageFilter.GaussianBlur(1.2))
    base = Image.alpha_composite(base, lines)

    # световые лучи по диагонали
    rays = Image.new("RGBA", (w, h), (0, 0, 0, 0))
    rd = ImageDraw.Draw(rays)
    accent_t = tuple(int(c) for c in accent)
    for _ in range(4):
        x = rng.randint(-w // 2, w)
        width = rng.randint(90, 240)
        rd.polygon([(x, 0), (x + width, 0), (x + width + h // 2, h), (x + h // 2, h)],
                   fill=accent_t + (rng.randint(14, 28),))
    rays = rays.filter(ImageFilter.GaussianBlur(45))
    base = Image.alpha_composite(base, rays)

    # зерно
    arr = np.asarray(base.convert("RGB")).astype(np.int16)
    noise = np.random.default_rng(seed).normal(0, 4, (h, w, 1)).astype(np.int16)
    arr = np.clip(arr + noise, 0, 255).astype(np.uint8)
    Image.fromarray(arr, "RGB").save(path)
    return path


def own_background(duration: float, used_ids: set[str], seed: int = 0) -> Background | None:
    """Свои кадры со стадионов из папки assets/footage — они всегда в приоритете."""
    folder = config.FOOTAGE_DIR
    if not folder.is_dir():
        return None
    files = sorted(p for p in folder.rglob("*") if p.suffix.lower() in VIDEO_EXTS)
    if not files:
        return None
    rng = random.Random(seed)
    fresh = [p for p in files if f"own:{p.name}" not in used_ids] or files
    rng.shuffle(fresh)
    need = min(len(fresh), max(2, math.ceil(duration / config.SEGMENT_SECONDS)))
    chosen = fresh[:need]
    return Background("video", chosen, [], [f"own:{p.name}" for p in chosen])


def get_background(queries: list[str], category: str, duration: float,
                   workdir: Path, used_ids: set[str], seed: int = 0) -> Background:
    bg = own_background(duration, used_ids, seed)
    if bg:
        return bg
    try:
        bg = pexels_background(queries, duration, used_ids)
        if bg:
            return bg
    except Exception as error:
        log.warning("Pexels недоступен: %s", error)
    path = generated_background(category, workdir / "background.png", seed)
    return Background("image", [path])
