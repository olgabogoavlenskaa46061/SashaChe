"""Telegram: сводки, тексты для записи, приём голосовых и отправка готовых роликов."""
from __future__ import annotations

import json
import logging
import time
from pathlib import Path

import requests

import config
from .textutil import tg_escape

log = logging.getLogger(__name__)

EMOJI = {"главное": "🔥", "курьёз": "😂", "интересное": "🤯"}
TTS_WORDS = {"бот", "/tts", "tts", "озвучь", "озвучь сам", "бот озвучь"}


class Telegram:
    def __init__(self, token: str | None = None, chat_id: str | None = None):
        self.token = token or config.TELEGRAM_BOT_TOKEN
        self.chat_id = chat_id or config.TELEGRAM_CHAT_ID
        if not self.token or not self.chat_id:
            raise RuntimeError("Не заданы TELEGRAM_BOT_TOKEN и TELEGRAM_CHAT_ID")
        self.api = f"https://api.telegram.org/bot{self.token}/"

    def _call(self, method: str, data: dict, files: dict | None = None, timeout: int = 60):
        last_error = None
        for attempt in range(4):
            try:
                response = requests.post(self.api + method, data=data, files=files, timeout=timeout)
                payload = response.json()
                if payload.get("ok"):
                    return payload["result"]
                retry = (payload.get("parameters") or {}).get("retry_after")
                if retry:
                    time.sleep(int(retry) + 1)
                    continue
                raise RuntimeError(payload.get("description", "неизвестная ошибка Telegram"))
            except (requests.RequestException, ValueError) as error:
                last_error = error
                time.sleep(5 * (attempt + 1))
                if files:  # файлы нужно перемотать для повторной попытки
                    for f in files.values():
                        f[1].seek(0)
        raise RuntimeError(f"Telegram {method}: {last_error}")

    def _silent(self) -> str:
        return "true" if config.TELEGRAM_SILENT else "false"

    def send_message(self, text: str, reply_to: int | None = None) -> dict:
        data = {"chat_id": self.chat_id, "text": text, "parse_mode": "HTML",
                "disable_web_page_preview": "true", "disable_notification": self._silent()}
        if reply_to:
            data["reply_to_message_id"] = reply_to
            data["allow_sending_without_reply"] = "true"
        return self._call("sendMessage", data)

    def send_video(self, path: Path, caption: str, duration: float, reply_to: int | None = None) -> dict:
        data = {"chat_id": self.chat_id, "caption": caption, "parse_mode": "HTML",
                "supports_streaming": "true", "width": config.WIDTH, "height": config.HEIGHT,
                "duration": int(round(duration)), "disable_notification": self._silent()}
        if reply_to:
            data["reply_to_message_id"] = reply_to
            data["allow_sending_without_reply"] = "true"
        with open(path, "rb") as video:
            return self._call("sendVideo", data, files={"video": (path.name, video, "video/mp4")}, timeout=600)

    # ── входящие ─────────────────────────────────────────────────────────
    def get_updates(self, offset: int | None = None) -> list[dict]:
        data = {"timeout": 0, "limit": 100, "allowed_updates": json.dumps(["message"])}
        if offset is not None:
            data["offset"] = offset
        return self._call("getUpdates", data) or []

    def is_our_chat(self, chat: dict) -> bool:
        wanted = str(self.chat_id).strip()
        if str(chat.get("id")) == wanted:
            return True
        username = chat.get("username")
        return bool(username) and wanted.lstrip("@").lower() == username.lower()

    def download(self, file_id: str, dest: Path) -> Path:
        info = self._call("getFile", {"file_id": file_id})
        if not dest.suffix:
            dest = dest.with_suffix(Path(info["file_path"]).suffix or ".bin")
        url = f"https://api.telegram.org/file/bot{self.token}/{info['file_path']}"
        response = requests.get(url, timeout=120)
        response.raise_for_status()
        dest.write_bytes(response.content)
        return dest


def voice_of(message: dict) -> dict | None:
    """Голосовое, аудиофайл или «кружок» из сообщения."""
    for key in ("voice", "audio", "video_note"):
        if message.get(key):
            return message[key]
    document = message.get("document") or {}
    if str(document.get("mime_type", "")).startswith("audio/"):
        return document
    return None


# ── тексты сообщений ─────────────────────────────────────────────────────
def _sources_line(sources) -> str:
    return ", ".join(f'<a href="{tg_escape(link)}">{tg_escape(name)}</a>' for name, link in sources)


def script_message(script, number: int, total: int) -> str:
    """Сообщение Саше: суть новости, текст для записи, источники."""
    emoji = EMOJI.get(script.category, "⚽")
    label = config.CATEGORIES[script.category]["label"]
    seconds = round(len(script.text.split()) / 2.7)
    parts = [
        f"🎙 <b>{number}/{total} · {emoji} {tg_escape(label)} · {tg_escape(script.hook)}</b>",
        "",
    ]
    if script.context:
        parts += [f"<i>Суть:</i> {tg_escape(script.context)}", ""]
    parts += [
        f"<b>Текст (~{seconds} сек):</b>",
        f"<blockquote>{tg_escape(script.text)}</blockquote>",
        f"Источники: {_sources_line(script.sources)}",
        "",
        "↩️ Ответьте на это сообщение голосовым — соберу ролик. "
        "Ответ «бот» — озвучу сам, ответ текстом — озвучу ваш текст.",
    ]
    return "\n".join(parts)


def video_caption(script, number: int, credits: list[str], engine: str) -> str:
    """Подпись к готовому ролику: название с хештегами и описание — готово к копированию."""
    tags = " ".join(script.hashtags)
    sources = _sources_line(script.sources)
    if config.VIDEO_STYLE == "sasha":
        head = (f"🎬 <b>{number}. Готово: {tg_escape(script.title)}</b>\n\n"
                f"Название для YouTube:\n<code>{tg_escape(script.title)} {tg_escape(tags)}</code>\n\n"
                "Описание:\n")
        tail = f"\n\nИсточники: {sources}"
    else:
        emoji = EMOJI.get(script.category, "⚽")
        label = config.CATEGORIES[script.category]["label"]
        head = f"{emoji} <b>{number}. {tg_escape(label)}</b>\n\n<b>{tg_escape(script.title)}</b>\n\n"
        tail = f"\n\n{tg_escape(tags)}\n\nИсточники: {sources}"
    if credits:
        tail += f"\nФон: {tg_escape(', '.join(credits[:3]))}"
    if engine == "gtts":
        tail += "\n⚠️ Озвучка запасным голосом"
    elif engine == "silent":
        tail += "\n⚠️ Без озвучки: сервис голоса был недоступен"
    body = tg_escape(script.description)
    limit = 1024 - len(head) - len(tail) - 20
    if len(body) > limit:
        body = body[:max(0, limit)].rsplit(" ", 1)[0] + "…"
    return head + body + tail
