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
                params = payload.get("parameters") or {}
                if params.get("retry_after"):
                    last_error = payload.get("description", "слишком много запросов")
                    time.sleep(int(params["retry_after"]) + 1)
                    continue
                new_chat = params.get("migrate_to_chat_id")
                if new_chat and str(data.get("chat_id")) == str(self.chat_id):
                    self._chat_migrated(new_chat)
                    data["chat_id"] = self.chat_id
                    _rewind(files)
                    continue
                raise RuntimeError(payload.get("description", "неизвестная ошибка Telegram"))
            except (requests.RequestException, ValueError) as error:
                last_error = error
                time.sleep(5 * (attempt + 1))
                _rewind(files)  # файлы нужно перемотать для повторной попытки
        raise RuntimeError(f"Telegram {method}: {last_error}")

    def _chat_migrated(self, new_chat) -> None:
        """Группа превратилась в супергруппу, и у неё сменился номер: пишем по новому и подсказываем."""
        self.chat_id = str(new_chat)
        log.warning("Группа стала супергруппой, у неё новый номер — замените секрет TELEGRAM_CHAT_ID")
        note = ("ℹ️ Эта группа стала супергруппой, и у неё сменился номер:\n"
                f"<code>{self.chat_id}</code>\n\n"
                "Замените в GitHub секрет <b>TELEGRAM_CHAT_ID</b> на этот номер: Settings → "
                "Secrets and variables → Actions → карандаш рядом с TELEGRAM_CHAT_ID. "
                "Пока там старый номер, бот не видит голосовые.")
        try:
            requests.post(self.api + "sendMessage", timeout=30,
                          data={"chat_id": self.chat_id, "text": note, "parse_mode": "HTML"})
        except requests.RequestException:
            log.warning("Не удалось отправить подсказку про новый номер чата")

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

    def get_chat(self) -> dict:
        """Проверка, что бот может писать в чат из настроек."""
        return self._call("getChat", {"chat_id": self.chat_id})

    def send_clip(self, path: Path, caption: str, reply_to: int | None = None) -> dict:
        """Исходное видео момента (с трибун) — чтобы Саша видел, о чём говорит. Без звука уведомления."""
        data = {"chat_id": self.chat_id, "caption": caption, "parse_mode": "HTML",
                "supports_streaming": "true", "disable_notification": "true"}
        if reply_to:
            data["reply_to_message_id"] = reply_to
            data["allow_sending_without_reply"] = "true"
        with open(path, "rb") as video:
            return self._call("sendVideo", data, files={"video": (path.name, video, "video/mp4")}, timeout=300)

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


def _rewind(files: dict | None) -> None:
    for f in (files or {}).values():
        f[1].seek(0)


def announce_chat_ids(token: str | None = None) -> tuple[str, list[dict]]:
    """Первый запуск, номера чата ещё нет в настройках.

    Находит чаты, где боту писали за последние сутки, и присылает в каждый его номер —
    искать номер вручную не нужно. Возвращает адрес бота и чаты, куда удалось написать.
    """
    api = f"https://api.telegram.org/bot{token or config.TELEGRAM_BOT_TOKEN}/"

    def call(method: str, **data):
        payload = requests.post(api + method, data=data, timeout=30).json()
        if not payload.get("ok"):
            raise RuntimeError(payload.get("description", "ошибка Telegram"))
        return payload["result"]

    try:
        username = call("getMe").get("username", "")
    except (requests.RequestException, ValueError, RuntimeError) as error:
        raise SystemExit(f"Токен бота не подходит ({error}). Проверьте секрет TELEGRAM_BOT_TOKEN: "
                         "его нужно скопировать из сообщения @BotFather целиком.")
    updates = call("getUpdates", timeout=0, limit=100,
                   allowed_updates=json.dumps(["message", "my_chat_member"]))
    chats: dict = {}
    for update in updates:
        for key in ("message", "edited_message", "my_chat_member"):
            chat = (update.get(key) or {}).get("chat") or {}
            if chat.get("type") in ("private", "group", "supergroup"):
                chats[chat["id"]] = chat

    sent = []
    for chat_id, chat in chats.items():
        text = (f"👋 Бот канала «{tg_escape(config.CHANNEL_NAME)}» на связи!\n\n"
                f"Номер этого чата: <code>{chat_id}</code>\n"
                "(нажмите на номер — он скопируется)\n\n"
                "Что дальше:\n"
                "1. В GitHub откройте репозиторий → Settings → Secrets and variables → Actions → "
                "New repository secret.\n"
                "2. Name: <code>TELEGRAM_CHAT_ID</code>, Secret: номер выше → Add secret.\n"
                "3. Снова запустите «Тексты на день» — тексты придут сюда.")
        try:
            call("sendMessage", chat_id=chat_id, text=text, parse_mode="HTML")
            sent.append(chat)
        except (requests.RequestException, ValueError, RuntimeError) as error:
            log.warning("Не удалось написать в чат «%s»: %s", chat.get("title") or "личный чат", error)
    return username, sent


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
    ]
    popular = popular_line(script.popular)
    if popular:
        parts.append(popular)
        stands_only = config.CLIP_SOURCE == "stands"
        clips = [p for p in script.popular if p.get("video_url")
                 and (not stands_only or p.get("footage") == "stands")]
        if config.USE_CLIPS and clips:
            best = max(clips, key=lambda p: (p.get("views") or 0, p.get("likes") or 0))
            what = "видео с трибун" if stands_only else "видео"
            parts.append(f"🎬 Фоном ролика будет {what} из {tg_escape(best.get('platform') or 'X')} про этот момент.")
        elif config.USE_CLIPS and stands_only:
            parts.append("🏟 Видео с трибун про этот момент нет — фоном будут общие кадры стадиона.")
    parts += [
        "",
        "↩️ Ответьте на это сообщение голосовым — соберу ролик. "
        "Ответ «бот» — озвучу сам, ответ текстом — озвучу ваш текст.",
    ]
    return "\n".join(parts)


def popular_line(popular: list[dict]) -> str:
    """«🔥 Смотрят: YouTube · 3,2 млн просмотров, X · 850 тыс. просмотров» со ссылками."""
    from .trends import Trend
    links = []
    for data in (popular or [])[:3]:
        trend = Trend.from_dict(data)
        if trend.url:
            links.append(f'<a href="{tg_escape(trend.url)}">{tg_escape(trend.label())}</a>')
    return "🔥 Смотрят: " + ", ".join(links) if links else ""


def video_caption(script, number: int, credits: list[str], engine: str, clip_url: str | None = None) -> str:
    """Подпись к готовому ролику: название с хештегами и описание — готово к копированию.
    clip_url — если фоном стало видео из X: ссылка на него попадает в описание."""
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
    if credits and not clip_url:
        tail += f"\nФон: {tg_escape(', '.join(credits[:3]))}"
    if engine == "gtts":
        tail += "\n⚠️ Озвучка запасным голосом"
    elif engine == "silent":
        tail += "\n⚠️ Без озвучки: сервис голоса был недоступен"
    body = tg_escape(script.description)
    video = f"\n\nВидео: {tg_escape(clip_url)}" if clip_url else ""
    limit = 1024 - len(head) - len(tail) - len(video) - 20
    if len(body) > limit:
        body = body[:max(0, limit)].rsplit(" ", 1)[0] + "…"
    return head + body + video + tail
