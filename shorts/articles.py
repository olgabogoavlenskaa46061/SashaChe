"""Полные тексты статей для выбранных сюжетов (чтобы сценарий опирался на факты, а не на заголовок)."""
from __future__ import annotations

import logging

import requests

import config
from .collector import NewsItem

log = logging.getLogger(__name__)

try:
    import trafilatura
except ImportError:  # без trafilatura бот работает на заголовках и анонсах
    trafilatura = None


def fetch_text(url: str, limit: int = 3500) -> str:
    if trafilatura is None:
        return ""
    try:
        response = requests.get(url, headers={"User-Agent": config.USER_AGENT}, timeout=20)
        response.raise_for_status()
        text = trafilatura.extract(
            response.text, include_comments=False, include_tables=False,
            favor_precision=True, deduplicate=True,
        ) or ""
        return text[:limit]
    except Exception as error:
        log.info("Не удалось взять текст статьи %s: %s", url, error)
        return ""


def story_material(lead: NewsItem, max_sources: int = 3) -> str:
    """Собирает материалы по сюжету из нескольких источников в один текст для Claude."""
    parts = []
    # Сначала разные источники, потом дубли из того же источника.
    ordered, seen_sources = [], set()
    for item in lead.all_items:
        if item.source not in seen_sources:
            ordered.append(item)
            seen_sources.add(item.source)
    ordered += [i for i in lead.all_items if i not in ordered]

    for item in ordered[:max_sources]:
        body = item.text or fetch_text(item.link) or item.summary
        parts.append(
            f"Источник: {item.source}\n"
            f"Опубликовано: {item.published:%d.%m %H:%M} UTC\n"
            f"Заголовок: {item.title}\n"
            f"Текст: {body[:3500]}"
        )
    return "\n\n---\n\n".join(parts)


def viral_material(selection, max_news: int = 2) -> str:
    """Материалы для темы из популярного видео: подписи к видео и новости про этот момент (для фактов)."""
    from .trends import stats_line
    parts = []
    for number, trend in enumerate(selection.trends):
        head = "Видео, о котором ролик" if number == 0 else "Этот же момент на другой площадке"
        stats = stats_line(trend)
        lines = [f"{head}: {trend.platform}" + (f", автор {trend.author}" if trend.author else "")
                 + (f" · {stats}" if stats else "")]
        lines.append(f"{'Название' if trend.platform == 'YouTube' else 'Подпись автора'}: {trend.title}")
        if trend.description:
            lines.append(f"Описание: {trend.description}")
        if trend.published:
            lines.append(f"Опубликовано: {trend.published}")
        parts.append("\n".join(lines))
    news = selection.item.related[:max_news]
    for item in news:
        body = item.text or fetch_text(item.link) or item.summary
        parts.append(f"Новость про этот момент (для проверки фактов)\nИсточник: {item.source}\n"
                     f"Заголовок: {item.title}\nТекст: {body[:3000]}")
    if not news:
        parts.append("Новостей про этот момент нет: факты — только из подписи и того, что видно на кадрах.")
    return "\n\n---\n\n".join(parts)
