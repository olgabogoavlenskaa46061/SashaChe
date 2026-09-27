"""Ответы Саши в Telegram: голосовое (или «бот», или свой текст) → готовый ролик в ответ."""
from __future__ import annotations

import json
import logging
import shutil
from datetime import datetime
from pathlib import Path
from zoneinfo import ZoneInfo

import config
from . import footage, history, pending, render, voice
from .editor import Script
from .telegram import TTS_WORDS, Telegram, video_caption, voice_of
from .textutil import slugify, tg_escape

log = logging.getLogger(__name__)

HINT_NO_REPLY = ("Не понял, к какой теме это голосовое 🙂 Ответьте им на сообщение с текстом: "
                 "свайп по сообщению влево → запись голосового.")
HINT_UNKNOWN = "Эта тема уже устарела или не найдена. Возьмите одну из свежих."


def _build(item: dict, message: dict, media: dict | None, text: str,
           telegram: Telegram, hist: dict) -> Path:
    script = Script.from_dict(item["script"])
    now = datetime.now(ZoneInfo(config.TIMEZONE))
    day_dir = config.OUTPUT_DIR / now.strftime("%Y-%m-%d")
    day_dir.mkdir(parents=True, exist_ok=True)
    take = len(item.get("videos", [])) + 1
    slug = f"{item['number']:02d}_{slugify(script.hook)}" + (f"_v{take}" if take > 1 else "")
    work = day_dir / f".work_{slug}"
    work.mkdir(parents=True, exist_ok=True)
    try:
        if media:
            source = telegram.download(media["file_id"], work / "voice_in")
            speech = voice.from_human(script.text, source, work / "voice.wav")
        else:
            if text.lower() not in TTS_WORDS:
                script.text = text  # Саша прислал свой вариант текста
            speech = voice.synthesize(script.text, work / "voice.mp3")
        seed = int(now.strftime("%j")) * 10 + item["number"] + take
        bg = footage.clip_background(script.popular, work, telegram) or footage.get_background(
            script.footage_queries, script.category, speech.duration + 1.0,
            work, history.used_footage(hist), seed=seed)
        music = render.pick_music(script.mood)
        out = day_dir / f"{slug}.mp4"
        info = render.render_short(script.category, script.hook, speech, bg, out,
                                   now.strftime("%d.%m"), music, seed=seed)
        render.fit_for_telegram(out)
    finally:
        shutil.rmtree(work, ignore_errors=True)

    clip_url = bg.credits[0] if bg.kind == "clip" and bg.credits else None
    telegram.send_video(out, video_caption(script, item["number"], bg.credits, speech.engine, clip_url),
                        info["duration"], reply_to=message["message_id"])
    history.remember_footage(hist, bg.footage_ids)
    meta = {**script.to_dict(), "voice": speech.engine, "background": bg.kind,
            "background_credits": bg.credits, **info}
    (day_dir / f"{slug}.json").write_text(json.dumps(meta, ensure_ascii=False, indent=1), encoding="utf-8")
    log.info("Ролик по теме %s готов: %s (%s с)", item["id"], out.name, info["duration"])
    return out


def process(telegram: Telegram) -> dict:
    """Забирает новые сообщения из Telegram и собирает ролики по ответам на тексты."""
    data = pending.load()
    hist = history.load()
    offset = data["last_update_id"] + 1 if data["last_update_id"] else None
    updates = sorted(telegram.get_updates(offset), key=lambda u: u["update_id"])
    made, failed = 0, 0
    for update in updates:
        data["last_update_id"] = max(data["last_update_id"], update["update_id"])
        message = update.get("message") or {}
        if not message or not telegram.is_our_chat(message.get("chat", {})):
            continue
        media = voice_of(message)
        text = (message.get("text") or "").strip()
        reply = message.get("reply_to_message") or {}
        item = pending.find(data, reply.get("message_id")) if reply else None
        if item is None:
            if media:
                telegram.send_message(HINT_UNKNOWN if reply else HINT_NO_REPLY, reply_to=message["message_id"])
            continue
        if not media and not text:
            continue
        if item.get("status") == "expired":
            telegram.send_message(HINT_UNKNOWN, reply_to=message["message_id"])
            continue
        try:
            out = _build(item, message, media, text, telegram, hist)
            item["status"] = "done"
            item.setdefault("videos", []).append(out.name)
            made += 1
        except Exception as error:
            failed += 1
            log.exception("Не получилось собрать ролик по теме %s", item.get("id"))
            telegram.send_message(f"⚠️ Не получилось собрать ролик: {tg_escape(str(error))[:500]}",
                                  reply_to=message["message_id"])
    if updates:
        telegram.get_updates(data["last_update_id"] + 1)  # подтверждаем Telegram, что всё обработано
    pending.expire(data)
    pending.save(data)
    history.save(hist)
    log.info("Входящих: %d, собрано роликов: %d, ошибок: %d", len(updates), made, failed)
    return {"updates": len(updates), "videos": made, "failed": failed}
