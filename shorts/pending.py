"""Темы, которые ждут голосового от Саши (хранятся в data/pending.json)."""
from __future__ import annotations

import json
from datetime import datetime, timedelta
from zoneinfo import ZoneInfo

import config


def _now() -> datetime:
    return datetime.now(ZoneInfo(config.TIMEZONE))


def load() -> dict:
    try:
        data = json.loads(config.PENDING_FILE.read_text(encoding="utf-8"))
    except (FileNotFoundError, json.JSONDecodeError):
        data = {}
    data.setdefault("items", [])
    data.setdefault("last_update_id", 0)
    return data


def save(data: dict) -> None:
    config.DATA_DIR.mkdir(parents=True, exist_ok=True)
    config.PENDING_FILE.write_text(json.dumps(data, ensure_ascii=False, indent=1), encoding="utf-8")


def add(data: dict, message_id: int, number: int, script_dict: dict, links: list[str]) -> dict:
    now = _now()
    item = {
        "id": f"{now:%Y%m%d}-{number}",
        "date": now.date().isoformat(),
        "created": now.isoformat(timespec="minutes"),
        "number": number,
        "message_id": message_id,
        "status": "waiting",
        "script": script_dict,
        "links": links,
        "videos": [],
    }
    data["items"].append(item)
    return item


def find(data: dict, message_id: int) -> dict | None:
    for item in data["items"]:
        if item.get("message_id") == message_id:
            return item
    return None


def waiting(data: dict) -> list[dict]:
    return [i for i in data["items"] if i.get("status") == "waiting"]


def expire(data: dict, days: int | None = None) -> None:
    """Старые темы помечаем устаревшими, совсем старые удаляем."""
    days = days or config.PENDING_DAYS
    border = (_now().date() - timedelta(days=days)).isoformat()
    drop = (_now().date() - timedelta(days=days + 14)).isoformat()
    for item in data["items"]:
        if item.get("status") == "waiting" and item.get("date", "") < border:
            item["status"] = "expired"
    data["items"] = [i for i in data["items"] if i.get("date", "") >= drop]
