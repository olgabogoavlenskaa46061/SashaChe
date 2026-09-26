"""Небольшие помощники для работы с текстом."""
from __future__ import annotations

import html
import re
import unicodedata

_TAG_RE = re.compile(r"<[^>]+>")
_WS_RE = re.compile(r"\s+")
_WORD_RE = re.compile(r"[0-9a-zа-яё]+", re.IGNORECASE)

# Частые короткие слова, которые не помогают понять, об одном ли сюжет речь.
_STOP = {
    "the", "and", "for", "with", "from", "after", "that", "this", "was", "are", "has", "have",
    "его", "она", "они", "это", "что", "как", "так", "для", "при", "над", "под", "без", "или",
    "уже", "ещё", "еще", "все", "всё", "был", "была", "были", "будет", "свой", "своей",
}

_TRANSLIT = str.maketrans({
    "а": "a", "б": "b", "в": "v", "г": "g", "д": "d", "е": "e", "ё": "e", "ж": "zh", "з": "z",
    "и": "i", "й": "y", "к": "k", "л": "l", "м": "m", "н": "n", "о": "o", "п": "p", "р": "r",
    "с": "s", "т": "t", "у": "u", "ф": "f", "х": "h", "ц": "c", "ч": "ch", "ш": "sh", "щ": "sch",
    "ъ": "", "ы": "y", "ь": "", "э": "e", "ю": "yu", "я": "ya",
})


def clean_html(raw: str | None) -> str:
    """HTML → чистый текст в одну строку."""
    if not raw:
        return ""
    text = re.sub(r"<(br|/p|/h\d|/li)[^>]*>", "\n", raw, flags=re.IGNORECASE)
    text = _TAG_RE.sub(" ", text)
    text = html.unescape(text)
    text = text.replace("\xa0", " ")
    lines = [_WS_RE.sub(" ", line).strip() for line in text.splitlines()]
    return "\n".join(line for line in lines if line)


def one_line(text: str, limit: int | None = None) -> str:
    text = _WS_RE.sub(" ", text or "").strip()
    if limit and len(text) > limit:
        cut = text[:limit].rsplit(" ", 1)[0]
        return cut.rstrip(",;:—-") + "…"
    return text


def normalize_word(word: str) -> str:
    """Слово без регистра, знаков препинания и с ё → е (для сравнения)."""
    word = unicodedata.normalize("NFKC", word).lower().replace("ё", "е")
    return "".join(ch for ch in word if ch.isalnum())


def stems(title: str) -> set[str]:
    """Грубые «основы» слов заголовка: первые 5 букв. Для русского этого хватает,
    чтобы «назначении Кириченко» и «назначил Кириченко» считались похожими."""
    result = set()
    for word in _WORD_RE.findall(title.lower().replace("ё", "е")):
        if len(word) < 3 or word in _STOP:
            continue
        result.add(word[:5])
    return result


def similarity(a: set[str], b: set[str]) -> float:
    if not a or not b:
        return 0.0
    return len(a & b) / len(a | b)


def slugify(text: str, limit: int = 40) -> str:
    text = text.lower().translate(_TRANSLIT)
    text = re.sub(r"[^a-z0-9]+", "-", text).strip("-")
    return text[:limit].strip("-") or "short"


def tg_escape(text: str) -> str:
    """Экранирование для Telegram parse_mode=HTML."""
    return html.escape(text or "", quote=False)
