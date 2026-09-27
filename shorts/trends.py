"""Популярное за сутки: самые просматриваемые футбольные видео на YouTube и в X.

Claude получает этот список как подсказку, какие моменты сейчас смотрят больше всего,
и в первую очередь выбирает сюжеты, которые с ними совпадают. Из X бот берёт ещё и само
видео (прямую ссылку на mp4) — оно становится фоном ролика.

Оба источника необязательны: без ключей бот работает только по новостям.
    YOUTUBE_API_KEY — бесплатно (10 000 единиц в день, бот тратит около 200);
    X_BEARER_TOKEN  — платно: $0.005 за каждый прочитанный пост.
"""
from __future__ import annotations

import html
import logging
import re
from dataclasses import asdict, dataclass
from datetime import datetime, timedelta, timezone

import requests

import config
from .textutil import one_line

log = logging.getLogger(__name__)

YT_SEARCH = "https://www.googleapis.com/youtube/v3/search"
YT_VIDEOS = "https://www.googleapis.com/youtube/v3/videos"
X_SEARCH = "https://api.x.com/2/tweets/search/recent"
_T_CO = re.compile(r"https?://t\.co/\S+")


@dataclass
class Trend:
    id: str                 # t1, t2… — по нему Claude ссылается на видео
    platform: str           # «YouTube» или «X»
    title: str
    url: str
    views: int = 0
    likes: int = 0
    author: str = ""
    published: str = ""     # ISO-время публикации
    video_url: str = ""     # прямая ссылка на mp4 (только X)
    duration: float = 0.0   # секунды

    def to_dict(self) -> dict:
        return asdict(self)

    @classmethod
    def from_dict(cls, data: dict) -> "Trend":
        return cls(**{k: v for k, v in data.items() if k in cls.__dataclass_fields__})

    def label(self) -> str:
        """«YouTube · 3,2 млн просмотров» — для сообщений в Telegram."""
        if self.views:
            return f"{self.platform} · {human_count(self.views)} просмотров"
        if self.likes:
            return f"{self.platform} · {human_count(self.likes)} ❤"
        return self.platform


def human_count(n: int) -> str:
    """3 200 000 → «3,2 млн», 850 000 → «850 тыс.»."""
    n = int(n or 0)
    if n >= 1_000_000:
        return f"{n / 1_000_000:.1f}".rstrip("0").rstrip(".").replace(".", ",") + " млн"
    if n >= 1_000:
        return f"{round(n / 1000)} тыс."
    return str(n)


def _since(hours: int) -> str:
    return (datetime.now(timezone.utc) - timedelta(hours=hours)).strftime("%Y-%m-%dT%H:%M:%SZ")


def _get_json(url: str, params: dict, headers: dict) -> dict:
    response = requests.get(url, params=params, headers=headers, timeout=30)
    try:
        data = response.json()
    except ValueError:
        data = {}
    if response.status_code >= 400:
        error = data.get("error")
        message = error.get("message") if isinstance(error, dict) else None
        errors = data.get("errors")
        if not message and isinstance(errors, list) and errors and isinstance(errors[0], dict):
            message = errors[0].get("message")
        message = message or data.get("detail") or data.get("title") or response.text[:200]
        raise RuntimeError(f"HTTP {response.status_code}: {one_line(str(message))[:200]}")
    return data


# ─── YouTube ─────────────────────────────────────────────────────────────
def youtube_popular(hours: int = 24) -> list[Trend]:
    """Самые просматриваемые футбольные видео за сутки (по запросам из YOUTUBE_QUERIES)."""
    headers = {"X-Goog-Api-Key": config.YOUTUBE_API_KEY}  # ключ в заголовке — не попадёт в ссылки и логи
    since = _since(hours)
    found: dict[str, dict] = {}
    for query, region, language in config.YOUTUBE_QUERIES:
        data = _get_json(YT_SEARCH, {
            "part": "snippet", "type": "video", "order": "viewCount", "publishedAfter": since,
            "maxResults": config.YOUTUBE_PER_QUERY, "q": query, "regionCode": region,
            "relevanceLanguage": language}, headers)
        for item in data.get("items", []):
            video_id = (item.get("id") or {}).get("videoId")
            snippet = item.get("snippet") or {}
            if video_id and snippet.get("liveBroadcastContent", "none") == "none":
                found.setdefault(video_id, snippet)
    if not found:
        return []

    stats: dict[str, dict] = {}
    ids = list(found)
    for start in range(0, len(ids), 50):
        data = _get_json(YT_VIDEOS, {"part": "statistics", "id": ",".join(ids[start:start + 50])}, headers)
        for item in data.get("items", []):
            stats[item.get("id")] = item.get("statistics") or {}

    trends = []
    for video_id, snippet in found.items():
        st = stats.get(video_id, {})
        trends.append(Trend(
            id="", platform="YouTube",
            title=one_line(html.unescape(snippet.get("title", "")))[:200],
            url=f"https://www.youtube.com/watch?v={video_id}",
            views=int(st.get("viewCount") or 0), likes=int(st.get("likeCount") or 0),
            author=one_line(html.unescape(snippet.get("channelTitle", ""))),
            published=snippet.get("publishedAt", "")))
    trends.sort(key=lambda t: (t.views, t.likes), reverse=True)
    return trends


# ─── X ───────────────────────────────────────────────────────────────────
def _best_mp4(media: dict) -> str:
    """Самый качественный mp4 не тяжелее ~5 Мбит/с (720p обычно ~2 Мбит/с)."""
    variants = [v for v in media.get("variants") or []
                if v.get("content_type") == "video/mp4" and v.get("url")]
    if not variants:
        return ""
    light = [v for v in variants if (v.get("bit_rate") or 0) <= 5_000_000]
    pool = light or sorted(variants, key=lambda v: v.get("bit_rate") or 0)[:1]
    return max(pool, key=lambda v: v.get("bit_rate") or 0)["url"]


def _x_queries() -> list[tuple[str, int]]:
    """Два запроса: видео больших футбольных аккаунтов и вирусные видео с любых аккаунтов."""
    budget = max(10, config.X_MAX_POSTS)
    accounts = [a.strip().lstrip("@") for a in config.X_ACCOUNTS.split(",") if a.strip()]
    queries = []
    viral = f"({config.X_KEYWORDS}) has:video_link -is:retweet -is:reply min_likes:{config.X_VIRAL_LIKES}"
    if accounts and budget >= 20:
        from_part = " OR ".join(f"from:{a}" for a in accounts)
        own = min(100, max(10, round(budget * 0.6)))
        queries.append((f"({from_part}) has:video_link -is:retweet min_likes:{config.X_MIN_LIKES}", own))
        queries.append((viral, min(100, max(10, budget - own))))
    elif accounts:
        from_part = " OR ".join(f"from:{a}" for a in accounts)
        queries.append((f"({from_part}) has:video_link -is:retweet min_likes:{config.X_MIN_LIKES}", budget))
    else:
        queries.append((viral, min(100, budget)))
    return queries


def _simplify(query: str) -> str:
    """Запасной вариант запроса, если X не понял какой-то оператор."""
    query = re.sub(r"\s*min_likes:\d+", "", query)
    return query.replace("has:video_link", "has:media")


def x_popular(hours: int = 24) -> tuple[list[Trend], int]:
    """Самые залайканные футбольные видео в X за сутки. Возвращает видео и число прочитанных постов."""
    headers = {"Authorization": f"Bearer {config.X_BEARER_TOKEN}"}
    since = _since(hours)
    read = 0
    trends: dict[str, Trend] = {}
    for query, limit in _x_queries():
        params = {"query": query, "start_time": since, "max_results": limit,
                  "tweet.fields": "created_at,public_metrics,attachments",
                  "expansions": "attachments.media_keys",
                  "media.fields": "type,variants,duration_ms"}
        try:
            data = _get_json(X_SEARCH, params, headers)
        except RuntimeError as error:
            if "HTTP 400" not in str(error):
                raise
            log.warning("X не принял запрос (%s) — пробую попроще", error)
            data = _get_json(X_SEARCH, {**params, "query": _simplify(query)}, headers)
        posts = data.get("data") or []
        read += len(posts)
        media = {m.get("media_key"): m for m in (data.get("includes") or {}).get("media", [])}
        for post in posts:
            keys = (post.get("attachments") or {}).get("media_keys") or []
            videos = [media[k] for k in keys if k in media and media[k].get("type") in ("video", "animated_gif")]
            if not videos or post.get("id") in trends:
                continue
            video = videos[0]
            metrics = post.get("public_metrics") or {}
            text = one_line(_T_CO.sub("", html.unescape(post.get("text", ""))))
            trends[post["id"]] = Trend(
                id="", platform="X", title=text[:200] or "видео без подписи",
                url=f"https://x.com/i/web/status/{post['id']}",
                views=int(metrics.get("impression_count") or 0), likes=int(metrics.get("like_count") or 0),
                published=post.get("created_at", ""), video_url=_best_mp4(video),
                duration=round((video.get("duration_ms") or 0) / 1000, 1))
    result = sorted(trends.values(), key=lambda t: (t.views, t.likes), reverse=True)
    return result, read


# ─── вместе ──────────────────────────────────────────────────────────────
def _safe(error: Exception) -> str:
    text = f"{type(error).__name__}: {error}" if not isinstance(error, RuntimeError) else str(error)
    for secret in (config.YOUTUBE_API_KEY, config.X_BEARER_TOKEN):
        if secret:
            text = text.replace(secret, "***")
    return one_line(text)[:200]


def collect(hours: int = 24) -> tuple[list[Trend], dict[str, str]]:
    """Популярное за сутки с обеих площадок вперемешку: 1-е место YouTube, 1-е место X, 2-е…"""
    report: dict[str, str] = {}
    youtube, x_posts = [], []
    if config.YOUTUBE_API_KEY:
        try:
            youtube = youtube_popular(hours)
            report["YouTube"] = f"видео: {len(youtube)}"
        except Exception as error:
            log.warning("YouTube недоступен: %s", _safe(error))
            report["YouTube"] = f"ошибка: {_safe(error)}"
    if config.X_BEARER_TOKEN:
        try:
            x_posts, read = x_popular(hours)
            report["X"] = f"постов прочитано: {read} ≈ ${read * config.X_PRICE_PER_POST:.2f}"
        except Exception as error:
            log.warning("X недоступен: %s", _safe(error))
            report["X"] = f"ошибка: {_safe(error)}"

    per = config.TRENDS_PER_PLATFORM
    mixed: list[Trend] = []
    for pair in zip(youtube[:per] + [None] * per, x_posts[:per] + [None] * per):
        mixed += [t for t in pair if t is not None]
    for number, trend in enumerate(mixed, 1):
        trend.id = f"t{number}"
    if mixed:
        log.info("Популярное за сутки: YouTube %d, X %d", min(len(youtube), per), min(len(x_posts), per))
    return mixed, report


def popularity_note(trends: list[Trend]) -> str:
    """Строка для материалов сценария: сколько посмотрели этот момент."""
    parts = [f"{human_count(t.views)} просмотров на {'YouTube' if t.platform == 'YouTube' else 'X'}"
             for t in trends if t.views]
    if not parts:
        return ""
    return "Популярность: видео с этим моментом набрали " + ", ".join(parts[:3]) + " за сутки."
