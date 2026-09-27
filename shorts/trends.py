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
import io
import logging
import re
from concurrent.futures import ThreadPoolExecutor
from dataclasses import asdict, dataclass
from datetime import datetime, timedelta, timezone

import requests

import config
from .textutil import one_line

log = logging.getLogger(__name__)

YT_SEARCH = "https://www.googleapis.com/youtube/v3/search"
YT_VIDEOS = "https://www.googleapis.com/youtube/v3/videos"
X_SEARCH = "https://api.x.com/2/tweets/search/recent"
APIFY_RUN = "https://api.apify.com/v2/acts/{actor}/run-sync-get-dataset-items"
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
    description: str = ""   # описание видео (YouTube)
    thumb_url: str = ""     # обложка видео — по ней Claude понимает, как снято
    footage: str = ""       # stands — снято с трибун, broadcast — трансляция, other — не матч по футболу

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


# Слова, по которым видно, что это не наш футбол (американский, студенческий и т. п.).
_OTHER_SPORT = re.compile(
    r"\b(nfl|ncaa|ncaaf|cfb|college football|touchdowns?|quarterbacks?|heisman|super ?bowl|field goal|"
    r"first down|linebackers?|wide receivers?|running backs?|tight ends?|end zone|pick[- ]six|hail mary|"
    r"dirty hits?|big ten|gridiron|nba|nhl|mlb|rugby|cricket)\b", re.IGNORECASE)


def other_sport(trend: "Trend") -> bool:
    """Подпись явно про другой спорт — американский футбол, баскетбол, регби…"""
    return bool(_OTHER_SPORT.search(f"{trend.title} {trend.description}"))


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
            "relevanceLanguage": language, "videoDuration": "short"}, headers)
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
            published=snippet.get("publishedAt", ""),
            description=one_line(html.unescape(snippet.get("description", "")))[:500],
            thumb_url=f"https://i.ytimg.com/vi/{video_id}/mqdefault.jpg"))
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


_X_USER = "https://api.x.com/2/users/by/username/{name}"


def _x_accounts(headers: dict | None = None) -> list[str]:
    """Аккаунты для оператора from:. Имя из одних цифр (например, 433) X принял бы за номер аккаунта,
    поэтому такие имена сначала переводим в настоящий номер (одно чтение профиля, $0.01)."""
    result = []
    for name in (a.strip().lstrip("@") for a in config.X_ACCOUNTS.split(",") if a.strip()):
        if not name.isdigit():
            result.append(name)
            continue
        if headers is None:
            continue
        try:
            user = _get_json(_X_USER.format(name=name), {}, headers).get("data") or {}
            if user.get("id"):
                result.append(str(user["id"]))
        except Exception as error:
            log.warning("Аккаунт X @%s не найден: %s", name, _safe(error))
    return result


def _x_queries(headers: dict | None = None) -> list[tuple[str, int]]:
    """Три запроса: видео больших футбольных аккаунтов, вирусные видео с любых аккаунтов
    и видео болельщиков с трибун. Бюджет постов делится примерно 50 / 30 / 20."""
    budget = max(10, config.X_MAX_POSTS)
    accounts = _x_accounts(headers)
    viral = f"({config.X_KEYWORDS}) has:video_link -is:retweet -is:reply min_likes:{config.X_VIRAL_LIKES}"
    stands = (f"({config.X_STANDS_WORDS}) ({config.X_STANDS_CONTEXT}) has:video_link -is:retweet "
              f"min_likes:{config.X_STANDS_LIKES}")
    parts: list[tuple[str, float]] = []
    if accounts:
        from_part = " OR ".join(f"from:{a}" for a in accounts)
        parts.append((f"({from_part}) has:video_link -is:retweet min_likes:{config.X_MIN_LIKES}", 0.5))
    parts += [(viral, 0.3), (stands, 0.2)]
    if budget < 10 * len(parts):  # маленький бюджет — только самые полезные запросы
        parts = parts[:max(1, budget // 10)]
    total = sum(w for _, w in parts)
    return [(q, min(100, max(10, round(budget * w / total)))) for q, w in parts]


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
    for query, limit in _x_queries(headers):
        params = {"query": query, "start_time": since, "max_results": limit,
                  "tweet.fields": "created_at,public_metrics,attachments",
                  "expansions": "attachments.media_keys",
                  "media.fields": "type,variants,duration_ms,preview_image_url"}
        try:
            data = _get_json(X_SEARCH, params, headers)
        except RuntimeError as error:
            if "HTTP 400" not in str(error):
                raise
            log.warning("X не принял запрос (%s) — пробую попроще", error)
            try:
                data = _get_json(X_SEARCH, {**params, "query": _simplify(query)}, headers)
            except RuntimeError as again:  # один неудачный запрос не должен ломать остальные
                log.warning("X не принял и упрощённый запрос: %s", again)
                continue
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
                duration=round((video.get("duration_ms") or 0) / 1000, 1),
                thumb_url=video.get("preview_image_url") or "")
    result = sorted(trends.values(), key=lambda t: (t.views, t.likes), reverse=True)
    return result, read


# ─── Instagram (через Apify) ─────────────────────────────────────────────
def instagram_popular(hours: int = 24) -> tuple[list[Trend], int]:
    """Рилсы больших футбольных аккаунтов за сутки. Возвращает видео и число полученных рилсов."""
    accounts = [a.strip().lstrip("@") for a in config.INSTAGRAM_ACCOUNTS.split(",") if a.strip()]
    if not accounts:
        return [], 0
    days = max(1, round(hours / 24))
    body = {"username": accounts, "resultsLimit": config.INSTAGRAM_PER_ACCOUNT,
            "onlyPostsNewerThan": f"{days} day" if days == 1 else f"{days} days", "skipPinnedPosts": True}
    response = requests.post(APIFY_RUN.format(actor=config.INSTAGRAM_ACTOR), json=body, timeout=330,
                             params={"maxItems": config.INSTAGRAM_MAX_REELS},
                             headers={"Authorization": f"Bearer {config.APIFY_TOKEN}"})
    try:
        data = response.json()
    except ValueError:
        data = None
    if response.status_code >= 400:
        error = (data or {}).get("error") if isinstance(data, dict) else None
        message = (error or {}).get("message") if isinstance(error, dict) else None
        raise RuntimeError(f"HTTP {response.status_code}: {one_line(message or response.text[:200])[:200]}")
    items = data if isinstance(data, list) else []
    trends: dict[str, Trend] = {}
    for item in items:
        if not isinstance(item, dict) or item.get("error"):
            continue
        url = item.get("url") or (f"https://www.instagram.com/reel/{item['shortCode']}/" if item.get("shortCode") else "")
        if not url or url in trends or not item.get("videoUrl"):
            continue
        views = item.get("videoPlayCount") or item.get("videoViewCount") or 0
        caption = one_line(html.unescape(item.get("caption") or ""))
        trends[url] = Trend(
            id="", platform="Instagram", title=caption[:200] or "видео без подписи", url=url,
            views=max(0, int(views or 0)), likes=max(0, int(item.get("likesCount") or 0)),
            author=item.get("ownerUsername") or "", published=item.get("timestamp") or "",
            video_url=item["videoUrl"], duration=round(float(item.get("videoDuration") or 0), 1),
            thumb_url=item.get("displayUrl") or item.get("thumbnailUrl") or "")
    result = sorted(trends.values(), key=lambda t: (t.views, t.likes), reverse=True)
    return result, len(items)


# ─── проверка ключей ─────────────────────────────────────────────────────
def youtube_check() -> str:
    data = _get_json(YT_SEARCH, {"part": "snippet", "type": "video", "q": "футбол", "maxResults": 1},
                     {"X-Goog-Api-Key": config.YOUTUBE_API_KEY})
    items = data.get("items") or []
    if not items:
        return "ключ работает, но поиск ничего не вернул"
    title = one_line(html.unescape((items[0].get("snippet") or {}).get("title", "")))[:60]
    return f"ключ работает (для проверки нашлось видео «{title}»)"


def youtube_hint(error: str) -> str:
    low = error.lower()
    if "not been used" in low or "disabled" in low or "accessnotconfigured" in low:
        return "в Google Cloud не включён YouTube Data API v3: APIs & Services → Library → YouTube Data API v3 → Enable"
    if "not valid" in low or "invalid" in low and "key" in low:
        return "ключ не подходит — скопируйте его заново: APIs & Services → Credentials"
    if "referer" in low or "referrer" in low or "blocked" in low:
        return "у ключа стоят ограничения: Credentials → ключ → Application restrictions → None"
    if "quota" in low:
        return "закончилась дневная квота YouTube — завтра снова заработает"
    return ""


def x_check() -> str:
    data = _get_json(X_SEARCH, {"query": "(football OR soccer) has:media -is:retweet", "max_results": 10,
                                "tweet.fields": "public_metrics"},
                     {"Authorization": f"Bearer {config.X_BEARER_TOKEN}"})
    read = len(data.get("data") or [])
    return f"токен работает (для проверки прочитано постов: {read} ≈ ${read * config.X_PRICE_PER_POST:.2f})"


def x_hint(error: str) -> str:
    low = error.lower()
    if "http 401" in low or "unauthorized" in low:
        return "токен не подходит — скопируйте Bearer Token заново на console.x.com (Apps → ваше приложение)"
    if "http 402" in low or "credit" in low or "payment" in low:
        return "закончились кредиты — пополните на console.x.com: Billing → Credits"
    if "http 403" in low:
        return "у приложения нет доступа к поиску — проверьте, что оно создано в console.x.com и есть кредиты"
    if "http 429" in low:
        return "слишком много запросов — попробуйте через 15 минут"
    return ""


def apify_check() -> str:
    response = requests.get("https://api.apify.com/v2/users/me", timeout=30,
                            headers={"Authorization": f"Bearer {config.APIFY_TOKEN}"})
    if response.status_code >= 400:
        raise RuntimeError(f"HTTP {response.status_code}: токен не подходит — скопируйте его заново: "
                           "Apify → Settings → API & Integrations")
    data = (response.json() or {}).get("data") or {}
    return f"токен работает (аккаунт {data.get('username') or 'Apify'})"


# ─── вместе ──────────────────────────────────────────────────────────────
def _safe(error: Exception) -> str:
    text = f"{type(error).__name__}: {error}" if not isinstance(error, RuntimeError) else str(error)
    for secret in (config.YOUTUBE_API_KEY, config.X_BEARER_TOKEN, config.APIFY_TOKEN):
        if secret and len(secret) >= 8:
            text = text.replace(secret, "***")
    return one_line(text)[:200]


def collect(hours: int = 24) -> tuple[list[Trend], dict[str, str]]:
    """Популярное за сутки со всех площадок вперемешку: 1-е место YouTube, X, Instagram, потом 2-е…"""
    report: dict[str, str] = {}
    youtube, x_posts, reels = [], [], []
    if config.YOUTUBE_API_KEY:
        try:
            youtube = youtube_popular(hours)
            report["YouTube"] = f"видео: {len(youtube)}"
        except Exception as error:
            log.warning("YouTube недоступен: %s", _safe(error))
            hint = youtube_hint(_safe(error))
            report["YouTube"] = f"ошибка: {hint or _safe(error)}"
    else:
        report["YouTube"] = "не подключён (нет секрета YOUTUBE_API_KEY)"
    if config.X_BEARER_TOKEN:
        try:
            x_posts, read = x_popular(hours)
            report["X"] = f"постов прочитано: {read} ≈ ${read * config.X_PRICE_PER_POST:.2f}"
        except Exception as error:
            log.warning("X недоступен: %s", _safe(error))
            hint = x_hint(_safe(error))
            report["X"] = f"ошибка: {hint or _safe(error)}"
    else:
        report["X"] = "не подключён (нет секрета X_BEARER_TOKEN)"

    if config.APIFY_TOKEN:
        try:
            reels, got = instagram_popular(hours)
            report["Instagram"] = f"рилсов: {got} ≈ ${got * config.INSTAGRAM_PRICE_PER_REEL:.2f}"
        except Exception as error:
            log.warning("Instagram недоступен: %s", _safe(error))
            report["Instagram"] = f"ошибка: {_safe(error)}"
    else:
        report["Instagram"] = "не подключён (нет секрета APIFY_TOKEN)"

    dropped = 0
    for bucket in (youtube, x_posts, reels):
        keep = [t for t in bucket if not other_sport(t)]
        dropped += len(bucket) - len(keep)
        bucket[:] = keep
    if dropped:
        log.info("Отброшено видео про другой спорт (американский футбол и т. п.): %d", dropped)

    per = config.TRENDS_PER_PLATFORM
    pad = [None] * per
    mixed: list[Trend] = []
    for trio in zip(youtube[:per] + pad, x_posts[:per] + pad, reels[:per] + pad):
        mixed += [t for t in trio if t is not None]
    for number, trend in enumerate(mixed, 1):
        trend.id = f"t{number}"
    if mixed:
        log.info("Популярное за сутки: YouTube %d, X %d, Instagram %d",
                 min(len(youtube), per), min(len(x_posts), per), min(len(reels), per))
    return mixed, report


def stats_line(trend: Trend) -> str:
    """«850 тыс. просмотров · 45 тыс. лайков»."""
    return " · ".join(x for x in (
        f"{human_count(trend.views)} просмотров" if trend.views else "",
        f"{human_count(trend.likes)} лайков" if trend.likes else "") if x)


def youtube_id(url: str) -> str:
    match = re.search(r"(?:v=|/shorts/|youtu\.be/)([\w-]{6,})", url or "")
    return match.group(1) if match else ""


def fetch_thumbnails(trends: list[Trend], size: int = 384) -> dict[str, bytes]:
    """Обложки видео, уменьшенные до size точек по большей стороне (JPEG). Не скачалось — пропускаем."""
    from PIL import Image

    def load(trend: Trend):
        if not trend.thumb_url:
            return trend.id, None
        try:
            response = requests.get(trend.thumb_url, timeout=15, headers={"User-Agent": config.USER_AGENT})
            response.raise_for_status()
            image = Image.open(io.BytesIO(response.content)).convert("RGB")
            image.thumbnail((size, size))
            out = io.BytesIO()
            image.save(out, "JPEG", quality=72)
            return trend.id, out.getvalue()
        except Exception as error:
            log.info("Обложка не скачалась (%s): %s", trend.url, error)
            return trend.id, None

    with ThreadPoolExecutor(max_workers=8) as pool:
        return {tid: data for tid, data in pool.map(load, trends) if data}


def popularity_note(trends: list[Trend]) -> str:
    """Строка для материалов сценария: сколько посмотрели этот момент."""
    parts = [f"{human_count(t.views)} просмотров в {t.platform}" if t.platform != "YouTube"
             else f"{human_count(t.views)} просмотров на YouTube" for t in trends if t.views]
    if not parts:
        return ""
    return "Популярность: видео с этим моментом набрали " + ", ".join(parts[:3]) + " за сутки."
