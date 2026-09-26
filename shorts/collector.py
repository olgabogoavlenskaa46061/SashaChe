"""Сбор новостей из RSS-лент за последние сутки."""
from __future__ import annotations

import hashlib
import logging
import re
import time
from dataclasses import dataclass, field
from datetime import datetime, timedelta, timezone

import feedparser
import requests

import config
from .textutil import clean_html, one_line, stems

log = logging.getLogger(__name__)

_SKIP_RE = re.compile("|".join(config.SKIP_PATTERNS), re.IGNORECASE)


@dataclass
class NewsItem:
    id: str
    title: str
    summary: str
    link: str
    source: str
    lang: str
    published: datetime
    text: str = ""                      # полный текст, если лента его отдаёт
    tags: list[str] = field(default_factory=list)
    related: list["NewsItem"] = field(default_factory=list)  # тот же сюжет в других источниках

    @property
    def sources(self) -> list[str]:
        names = [self.source] + [r.source for r in self.related]
        return list(dict.fromkeys(names))

    @property
    def all_items(self) -> list["NewsItem"]:
        return [self] + self.related


def _entry_time(entry) -> datetime | None:
    for key in ("published_parsed", "updated_parsed", "created_parsed"):
        value = entry.get(key)
        if value:  # feedparser уже приводит время к UTC
            return datetime(*value[:6], tzinfo=timezone.utc)
    return None


def _make_id(link: str) -> str:
    return hashlib.sha1(link.encode("utf-8")).hexdigest()[:7]


def _download(url: str) -> bytes:
    headers = {
        "User-Agent": config.USER_AGENT,
        "Accept": "application/rss+xml, application/atom+xml, application/xml;q=0.9, */*;q=0.8",
    }
    last_error: Exception | None = None
    for attempt in range(3):
        try:
            response = requests.get(url, headers=headers, timeout=25)
            response.raise_for_status()
            return response.content
        except requests.RequestException as error:
            last_error = error
            time.sleep(2 * (attempt + 1))
    raise RuntimeError(f"не удалось скачать {url}: {last_error}")


def _next_page(parsed) -> str | None:
    for link in parsed.feed.get("links", []) or []:
        if link.get("rel") == "next" and link.get("href"):
            return link["href"]
    return None


def parse_feed(content: bytes, source: dict, cutoff: datetime) -> tuple[list[NewsItem], object]:
    parsed = feedparser.parse(content)
    items: list[NewsItem] = []
    only = source.get("only_links_with")
    for entry in parsed.entries:
        link = (entry.get("link") or "").strip()
        title = one_line(clean_html(entry.get("title")))
        if not link or not title:
            continue
        if only and only not in link:
            continue
        published = _entry_time(entry)
        if published is None or published < cutoff:
            continue
        summary = one_line(clean_html(entry.get("summary") or entry.get("description")), 400)
        if _SKIP_RE.search(title):  # анонсы трансляций, ставки и т.п. видно по заголовку
            continue
        full_text = ""
        if entry.get("content"):
            full_text = clean_html(entry.content[0].get("value", ""))[:6000]
        tags = [t.get("term") for t in entry.get("tags", []) if t.get("term")]
        items.append(NewsItem(
            id=_make_id(link), title=title, summary=summary, link=link,
            source=source["name"], lang=source.get("lang", "ru"),
            published=published, text=full_text, tags=tags,
        ))
    return items, parsed


def _download_any(url: str) -> bytes:
    """Скачивает страницу; если не вышло — пробует тот же адрес через другой протокол (http/https)."""
    try:
        return _download(url)
    except RuntimeError:
        if url.startswith("http://"):
            return _download("https://" + url[len("http://"):])
        if url.startswith("https://"):
            return _download("http://" + url[len("https://"):])
        raise


def fetch_source(source: dict, cutoff: datetime) -> list[NewsItem]:
    """Скачивает ленту; если лента умеет листать назад (rel=next), догружает до начала суток."""
    url = source["url"]
    pages = 0
    result: list[NewsItem] = []
    seen_pages: set[str] = set()
    while url and pages < source.get("max_pages", 1) and url not in seen_pages:
        seen_pages.add(url)
        try:
            content = _download_any(url)
        except RuntimeError as error:
            if pages == 0:
                raise
            # первая страница уже есть — не теряем её из-за сбоя на следующих
            log.info("%s: не удалось догрузить страницу %d: %s", source["name"], pages + 1, error)
            break
        items, parsed = parse_feed(content, source, cutoff)
        result.extend(items)
        pages += 1
        # Листаем дальше, только пока самая старая запись на странице ещё свежая.
        entry_times = [t for t in (_entry_time(e) for e in parsed.entries) if t]
        if not entry_times or min(entry_times) < cutoff:
            break
        url = _next_page(parsed)
    return result


def _same_story(a: set[str], b: set[str]) -> float:
    """Похожесть двух заголовков: доля общих основ от меньшего набора.
    Склеиваем только почти одинаковые новости, а не разные новости об одном клубе."""
    if not a or not b:
        return 0.0
    shared = len(a & b)
    score = shared / min(len(a), len(b))
    if shared >= 4 and score >= 0.6:
        return score
    if shared >= 3 and score >= 0.75:
        return score
    return 0.0


def cluster(items: list[NewsItem]) -> list[NewsItem]:
    """Склеивает один и тот же сюжет из разных источников в одну запись."""
    by_link: dict[str, NewsItem] = {}
    for item in items:
        by_link.setdefault(item.link, item)
    unique = sorted(by_link.values(), key=lambda i: i.published)

    groups: list[tuple[set[str], NewsItem]] = []
    for item in unique:
        item_stems = stems(item.title)  # только заголовок: анонсы часто повторяют чужой сюжет
        best, best_score = None, 0.0
        for group_stems, lead in groups:
            if lead.lang != item.lang:
                continue
            score = _same_story(item_stems, group_stems)
            if score > best_score:
                best, best_score = (group_stems, lead), score
        if best:
            # сравниваем только с первой новостью сюжета, чтобы разные ракурсы
            # одного события (реакции, подробности) оставались отдельными темами
            best[1].related.append(item)
        else:
            groups.append((item_stems, item))

    leads = [lead for _, lead in groups]
    # Сначала сюжеты, о которых пишут несколько источников, потом свежие.
    leads.sort(key=lambda i: (len(i.sources), i.published), reverse=True)
    # Уникальные короткие id
    seen: set[str] = set()
    for lead in leads:
        while lead.id in seen:
            lead.id = _make_id(lead.id + lead.link)
        seen.add(lead.id)
    return leads


def collect(sources: list[dict] | None = None, hours: int | None = None) -> tuple[list[NewsItem], dict]:
    """Возвращает (сюжеты, отчёт по источникам)."""
    sources = sources or config.SOURCES
    cutoff = datetime.now(timezone.utc) - timedelta(hours=hours or config.NEWS_WINDOW_HOURS)
    everything: list[NewsItem] = []
    report: dict[str, str] = {}
    for source in sources:
        try:
            items = fetch_source(source, cutoff)
            everything.extend(items)
            report[source["name"]] = f"новостей: {len(items)}"
            log.info("%s: %d новостей", source["name"], len(items))
        except Exception as error:  # один сломанный источник не должен ронять весь бот
            report[source["name"]] = f"ошибка: {error}"
            log.warning("%s: %s", source["name"], error)
    stories = cluster(everything)
    log.info("Всего %d новостей → %d сюжетов", len(everything), len(stories))
    return stories, report
