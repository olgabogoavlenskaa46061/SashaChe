"""Все настройки бота в одном месте.

Секреты (ключи) берутся из переменных окружения: локально — из файла .env,
в GitHub Actions — из Secrets репозитория. Остальное можно менять прямо здесь
или через переменные окружения с теми же именами.
"""
from __future__ import annotations

import os
from pathlib import Path

try:
    from dotenv import load_dotenv
except ImportError:  # dotenv нужен только для локального запуска
    load_dotenv = None

ROOT = Path(__file__).resolve().parent
if load_dotenv:
    load_dotenv(ROOT / ".env")


def _env(name: str, default: str | None = None) -> str | None:
    value = os.getenv(name)
    if value is None or not value.strip():
        return default
    return value.strip()


# ─── Ключи ────────────────────────────────────────────────────────────────
ANTHROPIC_API_KEY = _env("ANTHROPIC_API_KEY")
TELEGRAM_BOT_TOKEN = _env("TELEGRAM_BOT_TOKEN")
TELEGRAM_CHAT_ID = _env("TELEGRAM_CHAT_ID")
PEXELS_API_KEY = _env("PEXELS_API_KEY")  # необязательно: без него фон рисуется сам

# ─── Редакция ─────────────────────────────────────────────────────────────
# Модель Claude, которая выбирает темы и пишет сценарии.
# Дешевле (и чуть проще тексты): claude-haiku-4-5-20251001
CLAUDE_MODEL = _env("CLAUDE_MODEL", "claude-sonnet-5")

# Сколько роликов каждого типа делать за день.
# Типы: главное — громкие новости, курьёз — смешное и нелепое,
# интересное — рекорды, необычные факты, трогательные истории.
SHORTS_MIX_RAW = _env("SHORTS_MIX", "главное:2,курьёз:1,интересное:1")

CATEGORIES = {
    "главное": {"label": "ГЛАВНОЕ", "color": (232, 48, 64)},
    "курьёз": {"label": "КУРЬЁЗ", "color": (255, 199, 0)},
    "интересное": {"label": "ЭТО ИНТЕРЕСНО", "color": (46, 134, 255)},
}


def parse_mix(raw: str) -> dict[str, int]:
    mix: dict[str, int] = {}
    for part in raw.replace(";", ",").split(","):
        if ":" not in part:
            continue
        name, _, count = part.partition(":")
        name = name.strip().lower().replace("курьез", "курьёз")
        if name in CATEGORIES and count.strip().isdigit():
            mix[name] = mix.get(name, 0) + int(count.strip())
    return mix or {"главное": 2, "курьёз": 1, "интересное": 1}


SHORTS_MIX = parse_mix(SHORTS_MIX_RAW)

# Название канала (для текстов и сводки) и часовой пояс для даты.
CHANNEL_NAME = _env("CHANNEL_NAME", "САША Ч.")
TIMEZONE = _env("TIMEZONE", "Europe/Moscow")

# За сколько последних часов брать новости.
NEWS_WINDOW_HOURS = int(_env("NEWS_WINDOW_HOURS", "24"))

# Стиль роликов:
#   sasha — как на канале САША Ч.: ироничный голос автора, кадры со стадиона без надписей,
#           в конце повтор с зумом и замедлением;
#   news  — новостной: плашка рубрики, заголовок, крупные субтитры.
VIDEO_STYLE = _env("VIDEO_STYLE", "sasha").lower()

# Кто озвучивает:
#   human — бот присылает текст в Telegram, Саша отвечает голосовым, бот собирает ролик;
#   tts   — бот озвучивает сам нейроголосом и присылает готовые ролики.
VOICE_MODE = _env("VOICE_MODE", "human").lower()

# Субтитры в стиле sasha (в новостном стиле они есть всегда).
SUBTITLES = _env("SUBTITLES", "0") == "1"

# Длина текста в словах: у Саши ≈ 20–25 секунд, в новостном стиле ≈ 30–40.
SCRIPT_MIN_WORDS, SCRIPT_MAX_WORDS = (45, 65) if VIDEO_STYLE == "sasha" else (65, 95)

# Сколько дней ждать голосовое на присланный текст.
PENDING_DAYS = int(_env("PENDING_DAYS", "3"))

# ─── Источники ────────────────────────────────────────────────────────────
# only_links_with — брать только ссылки, где есть этот кусок (отсекает теннис и т.п.)
SOURCES = [
    {"name": "Чемпионат", "url": "https://www.championat.com/rss/news/football/",
     "lang": "ru", "max_pages": 12},
    {"name": "Sports.ru", "url": "https://www.sports.ru/rss/rubric.xml?s=208",
     "lang": "ru", "only_links_with": "/football/"},
    {"name": "Спорт-Экспресс", "url": "https://www.sport-express.ru/services/materials/news/football/se/",
     "lang": "ru"},
    {"name": "BBC Sport", "url": "https://feeds.bbci.co.uk/sport/football/rss.xml",
     "lang": "en"},
    {"name": "SPORTbible", "url": "https://www.sportbible.com/football.rss",
     "lang": "en"},
]

# Служебные материалы, которые не бывают темой для ролика.
SKIP_PATTERNS = [
    r"онлайн-трансляци", r"прямая трансляция", r"текстовая трансляция", r"прямую трансляцию",
    r"где смотреть", r"во сколько начало", r"начнётся в \d", r"начнется в \d",
    r"ставка и", r"прогноз", r"коэффициент", r"букмекер", r"промокод", r"фрибет",
    r"расписание матчей", r"составы на", r"стартовые составы", r"видеообзор",
    r"разыгрывает", r"\blive\b", r"as it happened", r"\bodds\b", r"\bbetting\b",
    r"\bprediction", r"\btips\b", r"how to watch", r"tv channel", r"live stream",
]

# ─── Откуда брать темы ────────────────────────────────────────────────────
#   viral — из самых популярных футбольных видео за сутки в X и на YouTube; новости бот читает
#           только чтобы проверить факты (счёт, кто забил). Если X и YouTube недоступны — из новостей.
#   news  — из новостей, а популярные видео только поднимают совпавшие темы выше.
TOPIC_SOURCE = _env("TOPIC_SOURCE", "viral").lower()

# ─── Популярное: YouTube и X (необязательно) ─────────────────────────────
# Самые просматриваемые футбольные видео за сутки. Claude в первую очередь берёт темы,
# которые с ними совпадают, а видео из X становится фоном ролика.
YOUTUBE_API_KEY = _env("YOUTUBE_API_KEY")   # бесплатно: console.cloud.google.com
X_BEARER_TOKEN = _env("X_BEARER_TOKEN")     # платно: console.x.com, $0.005 за пост

# Запросы к YouTube: (что искать, страна, язык).
# Без слова «football»: в США так называют американский футбол.
YOUTUBE_QUERIES = [("футбол", "RU", "ru"), ("гол", "RU", "ru"), ("fútbol", "ES", "es"), ("golazo", "MX", "es"),
                   ("premier league", "GB", "en"), ("soccer", "US", "en")]
YOUTUBE_PER_QUERY = 25

# X: большие футбольные аккаунты с видео и слова для поиска вирусных видео с любых аккаунтов.
X_ACCOUNTS = _env("X_ACCOUNTS", "433,brfootball,goal,ESPNFC,OneFootball,TrollFootball,sportbible,"
                                "FCBarcelona,realmadrid,marca,diarioas,SC_ESPN,LaLiga")
X_KEYWORDS = ('soccer OR fútbol OR futbol OR golazo OR футбол OR "Premier League" OR LaLiga OR "Champions League" '
              'OR Messi OR Ronaldo OR Mbappe OR Yamal OR Vinicius OR Haaland OR "Real Madrid" OR Barcelona')
# Видео болельщиков с трибун: такие слова в подписи + что-то про футбол.
# 🎥 / 📹 @автор — так медиа подписывают видео, снятые болельщиками.
X_STANDS_WORDS = ('"from the stands" OR "fan footage" OR "fan view" OR fancam OR "fan cam" OR "desde la tribuna" '
                  'OR "desde la grada" OR "с трибуны" OR "с трибун" OR 🎥 OR 📹')
X_STANDS_CONTEXT = "soccer OR fútbol OR futbol OR football OR футбол OR gol OR goal OR golazo"
X_STANDS_LIKES = int(_env("X_STANDS_LIKES", "300"))
X_MAX_POSTS = int(_env("X_MAX_POSTS", "100"))       # сколько постов читать за день ($0.005 за каждый)
X_MIN_LIKES = int(_env("X_MIN_LIKES", "1000"))      # порог лайков для постов больших аккаунтов
X_VIRAL_LIKES = int(_env("X_VIRAL_LIKES", "5000"))  # порог лайков для постов с любых аккаунтов
X_PRICE_PER_POST = 0.005

# Instagram — через сервис Apify (рилсы больших футбольных аккаунтов за сутки с просмотрами и видео).
# $2.60 за 1000 рилсов; на бесплатном тарифе Apify даёт $5 в месяц — около 60 рилсов в день.
APIFY_TOKEN = _env("APIFY_TOKEN")           # apify.com → Settings → API & Integrations
INSTAGRAM_ACCOUNTS = _env("INSTAGRAM_ACCOUNTS", "433,brfootball,goal,espnfc,onefootball,sportbible,"
                                                "fcbarcelona,realmadrid,championsleague,premierleague,"
                                                "marca,diarioas,mundodeportivo,laliga")
INSTAGRAM_PER_ACCOUNT = int(_env("INSTAGRAM_PER_ACCOUNT", "4"))   # сколько последних рилсов с аккаунта
INSTAGRAM_MAX_REELS = int(_env("INSTAGRAM_MAX_REELS", "60"))       # потолок рилсов за день (защита от расходов)
INSTAGRAM_ACTOR = "apify~instagram-reel-scraper"
INSTAGRAM_PRICE_PER_REEL = 0.0026

# TikTok — тоже через Apify (тот же APIFY_TOKEN): там больше всего видео болельщиков с трибун.
# Ищем по запросам, оставляем свежие (до TIKTOK_MAX_AGE_HOURS) и популярные (от TIKTOK_MIN_PLAYS просмотров).
TIKTOK_ACTOR = _env("TIKTOK_ACTOR", "dami_studio~tiktok-scraper")   # $0.25 за 1000 видео, поиск работает
TIKTOK_QUERIES = [q.strip() for q in _env(
    "TIKTOK_QUERIES",
    "gol desde la tribuna;golazo desde la grada;hinchada gol;fan view goal stadium;"
    "football fans stadium goal reaction;гол с трибуны;фанаты на стадионе гол").split(";") if q.strip()]
TIKTOK_HASHTAGS = [h.strip().lstrip("#") for h in _env(
    "TIKTOK_HASHTAGS", "hinchada;golazo;tribuna;ultras;стадион").split(";") if h.strip()]
TIKTOK_PER_QUERY = int(_env("TIKTOK_PER_QUERY", "20"))
TIKTOK_MAX_VIDEOS = int(_env("TIKTOK_MAX_VIDEOS", "150"))   # потолок за запуск (≈ $0.03)
TIKTOK_MIN_PLAYS = int(_env("TIKTOK_MIN_PLAYS", "20000"))
TIKTOK_MAX_AGE_HOURS = int(_env("TIKTOK_MAX_AGE_HOURS", "48"))
TIKTOK_PRICE_PER_VIDEO = 0.00025
TIKTOK_MAX_CHARGE_USD = float(_env("TIKTOK_MAX_CHARGE_USD", "0.10"))  # потолок стоимости одного запуска в Apify
TRENDS_PER_PLATFORM = int(_env("TRENDS_PER_PLATFORM", "25"))  # сколько популярных видео с каждой площадки показывать Claude

# Какие видео брать фоном ролика: stands — только снятые болельщиками с трибун (Claude смотрит обложку
# и отличает их от телетрансляции), any — любые видео из X и Instagram.
CLIP_SOURCE = _env("CLIP_SOURCE", "stands").lower()

# Видео из X или Instagram как фон ролика: 1 — да (если тема совпала с видео), 0 — только стоковые кадры.
USE_CLIPS = _env("USE_CLIPS", "1").lower() not in ("0", "false", "no", "нет")
CLIP_VOLUME = float(_env("CLIP_VOLUME", "0.15"))   # звук исходного видео под голосом (0 — без звука)
CLIP_ZOOM = float(_env("CLIP_ZOOM", "1.35"))       # горизонтальное видео: во сколько раз крупнее, чем «по ширине»
CLIP_MAX_MB = 80

# ─── Озвучка ──────────────────────────────────────────────────────────────
# Голоса Microsoft: ru-RU-DmitryNeural (мужской), ru-RU-SvetlanaNeural (женский)
TTS_VOICE = _env("TTS_VOICE", "ru-RU-DmitryNeural")
TTS_RATE = _env("TTS_RATE", "+8%")

# ─── Видео ────────────────────────────────────────────────────────────────
WIDTH, HEIGHT, FPS = 1080, 1920, 30
VIDEO_CRF = int(_env("VIDEO_CRF", "22"))
MUSIC_VOLUME = float(_env("MUSIC_VOLUME", "0.12"))
LEAD_IN = 0.35   # пауза перед голосом, сек
TAIL = 0.9       # хвост после голоса, сек
SEGMENT_SECONDS = 4.2  # как часто меняется фоновый клип

# Повтор в конце ролика (стиль sasha): какая доля ролика, во сколько раз медленнее и какой зум.
REPLAY_SHARE = float(_env("REPLAY_SHARE", "0.33"))
REPLAY_SPEED = float(_env("REPLAY_SPEED", "0.35"))
REPLAY_ZOOM = float(_env("REPLAY_ZOOM", "1.7"))

# ─── Telegram ─────────────────────────────────────────────────────────────
TELEGRAM_SILENT = _env("TELEGRAM_SILENT", "0") == "1"

# ─── Папки ────────────────────────────────────────────────────────────────
OUTPUT_DIR = ROOT / "output"
CACHE_DIR = ROOT / ".cache"
DATA_DIR = ROOT / "data"
HISTORY_FILE = DATA_DIR / "history.json"
PENDING_FILE = DATA_DIR / "pending.json"
FONTS_DIR = ROOT / "assets" / "fonts"
MUSIC_DIR = ROOT / "assets" / "music"
FOOTAGE_DIR = ROOT / "assets" / "footage"   # свои кадры со стадионов (необязательно)

FONT_BLACK = FONTS_DIR / "Montserrat-Black.ttf"
FONT_BOLD = FONTS_DIR / "Montserrat-Bold.ttf"

USER_AGENT = (
    "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 "
    "(KHTML, like Gecko) Chrome/140.0 Safari/537.36"
)
