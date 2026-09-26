"""Память бота: какие сюжеты и фоновые видео уже были, чтобы не повторяться."""
from __future__ import annotations

import json
from datetime import date, datetime, timedelta
from zoneinfo import ZoneInfo

import config


def _today() -> date:
    return datetime.now(ZoneInfo(config.TIMEZONE)).date()


def load() -> dict:
    try:
        data = json.loads(config.HISTORY_FILE.read_text(encoding="utf-8"))
    except (FileNotFoundError, json.JSONDecodeError):
        data = {}
    data.setdefault("stories", [])
    data.setdefault("footage", [])
    return data


def save(data: dict, keep_days: int = 30) -> None:
    border = (_today() - timedelta(days=keep_days)).isoformat()
    data["stories"] = [s for s in data["stories"] if s.get("date", "") >= border]
    data["footage"] = [f for f in data["footage"] if f.get("date", "") >= border]
    config.DATA_DIR.mkdir(parents=True, exist_ok=True)
    config.HISTORY_FILE.write_text(json.dumps(data, ensure_ascii=False, indent=1), encoding="utf-8")


def recent_titles(data: dict, days: int = 5) -> list[str]:
    border = (_today() - timedelta(days=days)).isoformat()
    return [f"{s['date']}: {s['title']}" for s in data["stories"] if s.get("date", "") >= border]


def used_links(data: dict) -> set[str]:
    return {link for s in data["stories"] for link in s.get("links", [])}


def used_footage(data: dict, days: int = 7) -> set[str]:
    border = (_today() - timedelta(days=days)).isoformat()
    return {f["id"] for f in data["footage"] if f.get("date", "") >= border}


def remember(data: dict, title: str, category: str, links: list[str], footage_ids: list[str]) -> None:
    today = _today().isoformat()
    data["stories"].append({"date": today, "title": title, "category": category, "links": links})
    data["footage"].extend({"date": today, "id": fid} for fid in footage_ids)


def remember_footage(data: dict, footage_ids: list[str]) -> None:
    today = _today().isoformat()
    data["footage"].extend({"date": today, "id": fid} for fid in footage_ids)
