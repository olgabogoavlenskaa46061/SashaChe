"""Популярное за сутки: самые просматриваемые футбольные видео на YouTube, в X, Instagram и TikTok.

Из них Claude выбирает темы дня. Из X, Instagram и TikTok бот берёт ещё и само видео (прямую
ссылку на mp4) — видео с трибун становится фоном ролика. В TikTok бот ищет съёмку с трибун
тех моментов, которые сегодня популярны на других площадках.

Все источники необязательны: без ключей бот работает только по новостям.
    YOUTUBE_API_KEY — бесплатно (10 000 единиц в день, бот тратит около 200);
    X_BEARER_TOKEN  — платно: $0.005 за каждый прочитанный пост;
    APIFY_TOKEN     — Instagram и TikTok, платно: несколько центов в день.
"""
from __future__ import annotations

import html
import io
import logging
import re
from concurrent.futures import ThreadPoolExecutor
from dataclasses import asdict, dataclass
from itertools import zip_longest
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
    query: str = ""         # TikTok: по какому запросу нашлось
    origin: str = ""        # TikTok: ссылка на популярное видео момента, к которому искали съёмку с трибун
    origin_title: str = ""  # TikTok: название того видео

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


def _parse_time(value) -> datetime | None:
    """Время из ISO-строки («2026-09-27T21:19:04.000Z») или из числа секунд (миллисекунд) с 1970 года."""
    if value is None or isinstance(value, bool) or value == "":
        return None
    try:
        if isinstance(value, (int, float)) or str(value).strip().isdigit():
            seconds = float(value)
            if seconds > 1e11:  # миллисекунды
                seconds /= 1000
            return datetime.fromtimestamp(seconds, timezone.utc)
        parsed = datetime.fromisoformat(str(value).strip().replace("Z", "+00:00"))
        return parsed if parsed.tzinfo else parsed.replace(tzinfo=timezone.utc)
    except (TypeError, ValueError, OverflowError, OSError):
        return None


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
    border = datetime.now(timezone.utc) - timedelta(hours=hours + 2)
    trends: dict[str, Trend] = {}
    for item in items:
        if not isinstance(item, dict) or item.get("error"):
            continue
        url = item.get("url") or (f"https://www.instagram.com/reel/{item['shortCode']}/" if item.get("shortCode") else "")
        if not url or url in trends or not item.get("videoUrl"):
            continue
        when = _parse_time(item.get("timestamp"))
        if when and when < border:  # сборщик и сам отсекает старое, это страховка
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


# ─── TikTok (через Apify) ────────────────────────────────────────────────
def _first(item: dict, *paths: str):
    """Первое непустое значение по путям вида "videoMeta.coverUrl"."""
    for path in paths:
        value = item
        for key in path.split("."):
            value = value.get(key) if isinstance(value, dict) else None
        if isinstance(value, list):
            value = next((v for v in value if v), None)
        if value:
            return value
    return None


def _tiktok_video_url(item: dict) -> str:
    media = item.get("mediaUrls") or []
    videos = [m for m in media if isinstance(m, str) and (".mp4" in m or "video" in m)]
    return str(videos[0] if videos else _first(item, "videoMeta.downloadAddr", "video.downloadAddr",
                                                 "videoUrl", "video.playAddr", "videoMeta.playAddr") or "")


def _apify_log_tail(message: str, chars: int = 1500) -> str:
    """Если запуск в Apify упал — хвост его лога, чтобы понять причину."""
    match = re.search(r"run ID: ([A-Za-z0-9]+)", message)
    if not match:
        return ""
    try:
        response = requests.get(f"https://api.apify.com/v2/actor-runs/{match.group(1)}/log", timeout=30,
                                headers={"Authorization": f"Bearer {config.APIFY_TOKEN}"})
        lines = [line for line in response.text.splitlines() if line.strip()]
        important = [line for line in lines if re.search(r"ERROR|WARN|error|fail|invalid|required", line)]
        picked = (important[-5:] or lines[-12:])
        # время в начале строк не нужно — оставляем суть
        picked = [re.sub(r"^\S+Z\s+(\d{4}/\d\d/\d\d \d\d:\d\d:\d\d\s+)?", "", line) for line in picked]
        tail = " | ".join(picked)
        return f" — лог Apify: {one_line(tail)[:chars]}" if tail else ""
    except Exception:
        return ""


def _tiktok_time(item: dict, url: str) -> datetime | None:
    """Когда опубликовано видео TikTok. Надёжнее всего — по номеру видео: его старшие 32 бита — это
    секунды с 1970 года. Если номера нет — по полю даты (сборщики называют его по-разному:
    createTimeISO, createTime строкой или числом…). Непонятно — None."""
    now = datetime.now(timezone.utc)
    earliest = datetime(2016, 9, 1, tzinfo=timezone.utc)  # раньше TikTok не было

    def sane(when: datetime | None) -> datetime | None:
        return when if when and earliest <= when <= now + timedelta(hours=2) else None

    match = re.search(r"/(?:video|photo)/(\d{15,})", url or "")
    video_id = match.group(1) if match else str(item.get("id") or "")
    if video_id.isdigit() and len(video_id) >= 15:
        when = sane(datetime.fromtimestamp(int(video_id) >> 32, timezone.utc))
        if when:
            return when
    for key in ("createTimeISO", "createTime", "createdAt", "create_time", "uploadedAt", "publishedAt"):
        when = sane(_parse_time(item.get(key)))
        if when:
            return when
    return None


def _tiktok_input(actor: str, query: str, limit: int) -> dict:
    """Вход для одного поиска у разных сборщиков TikTok в Apify: у каждого свои названия полей.
    Запрос, начинающийся с #, — хэштег."""
    if actor.startswith("dami_studio"):
        body: dict = {"resultsPerPage": limit, "maxRunSeconds": 90}
    elif actor.startswith("clockworks"):
        body = {"resultsPerPage": limit, "shouldDownloadVideos": False, "shouldDownloadCovers": False}
    else:  # igolaizola и похожие
        body = {"maxVideosPerInput": limit, "maxTotalVideos": limit}
    if query.startswith("#"):
        body["hashtags"] = [query.lstrip("#")]
    else:
        body["searchQueries"] = [query]
    return body


def _tiktok_run(actor: str, query: str, limit: int, max_charge: float) -> list[dict]:
    """Один поиск в TikTok через Apify — не больше limit видео."""
    response = requests.post(APIFY_RUN.format(actor=actor), json=_tiktok_input(actor, query, limit), timeout=330,
                             params={"maxItems": limit,
                                     # сборщик берёт плату за события, а не только за видео: без явного
                                     # потолка в долларах Apify останавливает его сразу после старта
                                     "maxTotalChargeUsd": max_charge},
                             headers={"Authorization": f"Bearer {config.APIFY_TOKEN}"})
    try:
        data = response.json()
    except ValueError:
        data = None
    if response.status_code >= 400:
        error = (data or {}).get("error") if isinstance(data, dict) else None
        message = (error or {}).get("message") if isinstance(error, dict) else None
        message = one_line(message or response.text[:200])[:200]
        raise RuntimeError(f"HTTP {response.status_code}: {message}{_apify_log_tail(message)}")
    return [i for i in (data if isinstance(data, list) else []) if isinstance(i, dict) and not i.get("error")]


@dataclass
class TikTokResult:
    videos: list            # свежие популярные видео (Trend) — по очереди из каждого поиска
    got: int = 0            # сколько видео отдал сборщик (за них платим)
    old: int = 0            # отброшено: старше TIKTOK_MAX_AGE_HOURS или непонятно, когда снято
    failed: int = 0         # сколько поисков не сработало
    note: str = ""          # подсказка для отчёта


def tiktok_popular(plan: list | None = None, hours: int | None = None, per_query: int | None = None,
                   min_plays: int | None = None, max_charge: float | None = None,
                   actor: str | None = None) -> TikTokResult:
    """Свежие популярные футбольные видео TikTok.

    plan — поиски: пары (запрос, популярное видео момента, к которому ищем съёмку с трибун, или None).
    Без plan — общие запросы из TIKTOK_QUERIES. Каждый поиск — отдельный запуск в Apify: лимит видео
    у сборщика общий на запуск, и первый запрос съел бы его целиком."""
    if plan is None:
        plan = [(q, None) for q in config.TIKTOK_QUERIES[:max(1, config.TIKTOK_SEARCHES)]]
        plan += [(f"#{h}", None) for h in config.TIKTOK_HASHTAGS]
    per_query = per_query or config.TIKTOK_PER_QUERY
    min_plays = config.TIKTOK_MIN_PLAYS if min_plays is None else min_plays
    max_charge = max_charge or config.TIKTOK_MAX_CHARGE_USD
    actor = actor or config.TIKTOK_ACTOR
    result = TikTokResult(videos=[])
    if not plan:
        return result

    def run(query: str):
        try:
            return _tiktok_run(actor, query, per_query, max_charge), None
        except Exception as error:
            return [], error

    with ThreadPoolExecutor(max_workers=max(1, config.TIKTOK_PARALLEL)) as pool:
        outcomes = list(pool.map(run, [query for query, _ in plan]))
    for number, (_, error) in enumerate(outcomes):
        # Apify не дал запустить несколько поисков сразу (лимит памяти на тарифе) — повторяем по одному
        if error is not None and re.search(r"memory|concurren", str(error), re.IGNORECASE):
            outcomes[number] = run(plan[number][0])
    errors = [error for _, error in outcomes if error is not None]
    if len(errors) == len(plan):
        raise errors[0] if isinstance(errors[0], RuntimeError) else RuntimeError(_safe(errors[0], 1500))

    border = datetime.now(timezone.utc) - timedelta(hours=hours or config.TIKTOK_MAX_AGE_HOURS)
    seen: set[str] = set()
    per_search: list[list[Trend]] = []
    with_file = 0
    sample: dict = {}
    for (query, origin), (items, _) in zip(plan, outcomes):
        found = []
        for item in items:
            result.got += 1
            sample = sample or item
            url = str(_first(item, "webVideoUrl", "url", "shareUrl") or "")
            if not url or url in seen:
                continue
            seen.add(url)
            when = _tiktok_time(item, url)
            if when is None or when < border:  # старое или непонятно, когда снято, — мимо
                result.old += 1
                continue
            plays = int(_first(item, "playCount", "stats.playCount") or 0)
            if plays < min_plays:
                continue
            video_url = _tiktok_video_url(item)
            with_file += bool(video_url)
            found.append(Trend(
                id="", platform="TikTok",
                title=one_line(html.unescape(str(item.get("text") or "")))[:200] or "видео без подписи",
                url=url, views=plays, likes=int(_first(item, "diggCount", "stats.diggCount") or 0),
                author=str(_first(item, "authorMeta.name", "author.uniqueId", "author.nickname") or ""),
                published=when.isoformat(), video_url=video_url,
                duration=round(float(_first(item, "videoMeta.duration", "video.duration", "duration") or 0), 1),
                thumb_url=str(_first(item, "videoMeta.coverUrl", "video.cover", "covers.default", "cover") or ""),
                query=query, origin=origin.url if origin is not None else "",
                origin_title=origin.title[:150] if origin is not None else ""))
        found.sort(key=lambda t: (t.views, t.likes), reverse=True)
        per_search.append(found)
    # по очереди из каждого поиска — чтобы в списке были все моменты, а не только самый вирусный
    for row in zip_longest(*per_search):
        result.videos += [t for t in row if t is not None]
    result.failed = len(errors)
    if result.videos and not with_file:  # подсказка для отладки: сборщик отдаёт видео под другим полем
        result.note = " (без ссылок на файл; поля: " + ", ".join(sorted(sample)[:25]) + ")"
    if errors:
        result.note += f"; не сработало поисков: {len(errors)} ({_safe(errors[0], 150)})"
    return result


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


def tiktok_check() -> str:
    """Маленький пробный поиск в TikTok: 3 видео (≈ $0.001), дата не важна."""
    found = tiktok_popular([("gol desde la tribuna", None)], hours=24 * 365 * 10, per_query=3, min_plays=0,
                           max_charge=0.02)
    with_file = sum(1 for v in found.videos if v.video_url)
    return f"сборщик TikTok работает (получено видео: {found.got}, со ссылкой на файл: {with_file}){found.note}"


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
def _safe(error: Exception, limit: int = 200) -> str:
    text = f"{type(error).__name__}: {error}" if not isinstance(error, RuntimeError) else str(error)
    for secret in (config.YOUTUBE_API_KEY, config.X_BEARER_TOKEN, config.APIFY_TOKEN):
        if secret and len(secret) >= 8:
            text = text.replace(secret, "***")
    return one_line(text)[:limit]


def _interleave(*buckets: list[Trend]) -> list[Trend]:
    """1-е место каждой площадки, потом 2-е… — не больше TRENDS_PER_PLATFORM с площадки."""
    per = config.TRENDS_PER_PLATFORM
    rows = zip_longest(*(bucket[:per] for bucket in buckets))
    return [t for row in rows for t in row if t is not None]


def collect(hours: int = 24, tiktok_planner=None) -> tuple[list[Trend], dict[str, str]]:
    """Популярное за сутки со всех площадок вперемешку: 1-е место YouTube, X, Instagram, TikTok, потом 2-е…

    tiktok_planner(видео) → [(запрос, видео момента)] — по каким моментам дня искать в TikTok съёмку
    с трибун (выбирает Claude). Без него или если не вышло — общие запросы из TIKTOK_QUERIES."""
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

    tiktok: list[Trend] = []
    if config.APIFY_TOKEN and config.TIKTOK_SEARCHES > 0:
        plan: list = []
        candidates = _interleave(youtube, x_posts, reels)
        if tiktok_planner is not None and candidates:
            try:
                plan = list(tiktok_planner(candidates) or [])
            except Exception as error:
                log.warning("Не получилось выбрать моменты для поиска в TikTok: %s", _safe(error))
        targeted = bool(plan)
        if not plan:
            plan = [(q, None) for q in config.TIKTOK_QUERIES[:config.TIKTOK_SEARCHES]]
            plan += [(f"#{h}", None) for h in config.TIKTOK_HASHTAGS]
        try:
            found = tiktok_popular(plan)
            tiktok = found.videos
            kind = "по моментам дня" if targeted else "общих"
            report["TikTok"] = (f"свежих видео: {len(tiktok)} из {found.got}, поисков {kind}: {len(plan)} ≈ "
                                f"${found.got * config.TIKTOK_PRICE_PER_VIDEO:.2f}{found.note}")
            report["TikTok поиск"] = (f"старше {config.TIKTOK_MAX_AGE_HOURS} ч отброшено: {found.old} · "
                                      "запросы: " + "; ".join(q for q, _ in plan))[:900]
        except Exception as error:
            log.warning("TikTok недоступен: %s", _safe(error))
            report["TikTok"] = f"ошибка: {_safe(error)}"
        keep = [t for t in tiktok if not other_sport(t)]
        dropped += len(tiktok) - len(keep)
        tiktok = keep
    if dropped:
        log.info("Отброшено видео про другой спорт (американский футбол и т. п.): %d", dropped)

    mixed = _interleave(youtube, x_posts, reels, tiktok)
    for number, trend in enumerate(mixed, 1):
        trend.id = f"t{number}"
    if mixed:
        per = config.TRENDS_PER_PLATFORM
        log.info("Популярное за сутки: YouTube %d, X %d, Instagram %d, TikTok %d", min(len(youtube), per),
                 min(len(x_posts), per), min(len(reels), per), min(len(tiktok), per))
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
