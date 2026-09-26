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
