#!/usr/bin/env python3
"""Футбольные шортсы в стиле канала «САША Ч.».

    python main.py                — утренний запуск: по настройке VOICE_MODE
                                     human → прислать Саше тексты на день (по умолчанию)
                                     tts   → сразу сделать ролики с нейроголосом
    python main.py scripts        — прислать тексты для записи голосом
    python main.py inbox          — забрать голосовые из Telegram и собрать ролики
    python main.py auto           — сделать ролики целиком самому (нейроголос)
    python main.py demo           — проверить монтаж без ключей и новостей

Флаги: --dry-run (не отправлять в Telegram), --limit N (сколько тем взять).
"""
from __future__ import annotations

import argparse
import json
import logging
import os
import shutil
import sys
import time
from datetime import datetime
from pathlib import Path
from zoneinfo import ZoneInfo

import config
from shorts import articles, collector, footage, history, inbox, pending, render, voice
from shorts import editor as editor_mod
from shorts.telegram import EMOJI, Telegram, announce_chat_ids, script_message, video_caption
from shorts.textutil import slugify, tg_escape

log = logging.getLogger("shorts")


def setup_logging() -> None:
    logging.basicConfig(level=logging.INFO, format="%(asctime)s %(levelname)s %(message)s",
                        datefmt="%H:%M:%S", stream=sys.stdout)
    for noisy in ("httpx", "httpcore", "trafilatura", "urllib3"):
        logging.getLogger(noisy).setLevel(logging.WARNING)


def github_summary(text: str) -> None:
    """Пометка на странице запуска в GitHub Actions (блок Summary) — видно без чтения логов."""
    path = os.environ.get("GITHUB_STEP_SUMMARY")
    if not path:
        return
    try:
        with open(path, "a", encoding="utf-8") as f:
            f.write(text.rstrip() + "\n")
    except OSError:
        pass


def _annotation(level: str, title: str, message: str) -> None:
    """Сообщение в блоке Annotations на странице запуска в GitHub Actions."""
    if os.environ.get("GITHUB_ACTIONS") == "true":
        text = message.replace("%", "%25").replace("\r", "%0D").replace("\n", "%0A")
        print(f"::{level} title={title}::{text}", flush=True)


def github_error(message: str) -> None:
    """Причина ошибки — сразу на странице запуска: в Annotations и в Summary."""
    message = _hide_keys(message)
    _annotation("error", "Бот остановился", message)
    github_summary(f"❌ {message}")


def check_setup(need_claude: bool, need_telegram: bool, need_video: bool = True) -> None:
    problems = []
    if need_claude and not config.ANTHROPIC_API_KEY:
        problems.append("нет секрета ANTHROPIC_API_KEY")
    if need_telegram and not config.TELEGRAM_BOT_TOKEN:
        problems.append("нет секрета TELEGRAM_BOT_TOKEN")
    elif need_telegram and not config.TELEGRAM_CHAT_ID:
        problems.append("нет секрета TELEGRAM_CHAT_ID")
    if need_video:
        for tool in ("ffmpeg", "ffprobe"):
            if not shutil.which(tool):
                problems.append(f"не установлен {tool}")
        for f in (config.FONT_BLACK, config.FONT_BOLD):
            if not f.exists():
                problems.append(f"нет шрифта {f.name}")
    if problems:
        raise SystemExit("Не хватает настроек: " + "; ".join(problems) + ". Смотрите README.md.")


def first_contact() -> int:
    """Номера чата ещё нет: бот сам присылает его во все чаты, где ему писали."""
    username, chats = announce_chat_ids()
    if not chats:
        raise SystemExit(f"Номера чата пока нет, а бот @{username} не видит ни одного чата. Добавьте бота "
                         f"в группу с Сашей, напишите там /start@{username} и запустите ещё раз.")
    names = ", ".join(f"«{c.get('title') or 'личный чат'}»" for c in chats)
    message = (f"Бот прислал номер чата в Telegram ({names}). Добавьте этот номер в GitHub "
               "секретом TELEGRAM_CHAT_ID и запустите ещё раз.")
    log.info(message)
    _annotation("notice", "Номер чата", message)
    github_summary(f"👋 {message}")
    return 0


def cleanup(days: int = 7) -> None:
    """Удаляет старые ролики и кэш стоковых видео, чтобы не забивать диск."""
    border = time.time() - days * 86400
    for folder in (config.OUTPUT_DIR, config.CACHE_DIR / "pexels"):
        if not folder.exists():
            continue
        for item in folder.iterdir():
            if item.stat().st_mtime < border:
                shutil.rmtree(item, ignore_errors=True) if item.is_dir() else item.unlink(missing_ok=True)


def pick_topics(ed: editor_mod.Editor, hist: dict, limit: int) -> tuple[list, dict]:
    """Новости за сутки → темы дня (Claude)."""
    stories, report = collector.collect()
    used = history.used_links(hist)
    stories = [s for s in stories if not any(i.link in used for i in s.all_items)]
    if not stories:
        raise RuntimeError("не удалось получить свежие новости ни из одного источника: "
                           + "; ".join(f"{k} — {v}" for k, v in report.items()))
    mix = dict(config.SHORTS_MIX)
    if limit:
        left = limit
        for name in list(mix):
            mix[name] = min(mix[name], left)
            left -= mix[name]
    selections = ed.select(stories, mix, history.recent_titles(hist))
    if not selections:
        raise RuntimeError("Claude не выбрал ни одной темы")
    return selections, report


def broken_sources(report: dict) -> list[str]:
    return [f"• {tg_escape(f'{k}: {v}'[:150])}" for k, v in report.items() if v.startswith("ошибка")]


def _hide_keys(text: str) -> str:
    for secret in (config.TELEGRAM_BOT_TOKEN, config.ANTHROPIC_API_KEY, config.PEXELS_API_KEY):
        if secret:
            text = text.replace(secret, "***")
    return text


def notify_error(telegram: Telegram | None, what: str, error: Exception) -> None:
    log.exception("Бот остановился с ошибкой")
    details = _hide_keys(str(error))
    github_error(f"{what}: {type(error).__name__}: {details[:800]}")
    if telegram:
        try:
            telegram.send_message(f"⚠️ {what}:\n<code>{tg_escape(details)[:3000]}</code>")
        except Exception:
            log.exception("Не удалось отправить сообщение об ошибке")


# ─── тексты для Саши ─────────────────────────────────────────────────────
def run_scripts(args) -> int:
    check_setup(need_claude=True, need_telegram=not args.dry_run, need_video=False)
    now = datetime.now(ZoneInfo(config.TIMEZONE))
    today_text = editor_mod.today_label()
    telegram = None if args.dry_run else Telegram()
    hist = history.load()
    data = pending.load()
    try:
        ed = editor_mod.Editor()
        selections, report = pick_topics(ed, hist, args.limit)
        scripts = []
        for selection in selections:
            try:
                material = articles.story_material(selection.item)
                script = ed.write_script(selection, material, today_text, reader="human")
                scripts.append((selection, script))
                log.info("Текст «%s» (%s, %d слов)", script.hook, script.category, script.words)
            except Exception:
                log.exception("Не получился текст по теме «%s»", selection.item.title[:80])
        if not scripts:
            raise RuntimeError("не получилось написать ни одного текста")

        total = len(scripts)
        if telegram:
            lines = [f"🎙 <b>{tg_escape(config.CHANNEL_NAME)} · темы на {tg_escape(today_text)}: {total}</b>",
                     "Ответьте голосовым на сообщение с текстом — ролик придёт в ответ в течение часа.",
                     "Ответ «бот» — озвучу сам."]
            broken = broken_sources(report)
            if broken:
                lines += ["", "Источники с ошибкой:"] + broken
            lines += ["", f"<i>Claude: {tg_escape(ed.cost_line())}</i>"]
            telegram.send_message("\n".join(lines))

        preview = []
        for number, (selection, script) in enumerate(scripts, 1):
            links = [i.link for i in selection.item.all_items]
            if telegram:
                sent = telegram.send_message(script_message(script, number, total))
                pending.add(data, sent["message_id"], number, script.to_dict(), links)
            preview.append(f"## {number}. {script.hook} ({script.category})\n\n{script.context}\n\n"
                           f"{script.text}\n\nНазвание: {script.title} {' '.join(script.hashtags)}\n")
            history.remember(hist, script.title, selection.category, links, [])

        day_dir = config.OUTPUT_DIR / now.strftime("%Y-%m-%d")
        day_dir.mkdir(parents=True, exist_ok=True)
        (day_dir / "texts.md").write_text("\n".join(preview), encoding="utf-8")
        pending.expire(data)
        pending.save(data)
        history.save(hist)
        log.info("Отправлено текстов: %d. Claude: %s", total, ed.cost_line())
        return 0
    except Exception as error:
        notify_error(telegram, f"Не получилось подготовить тексты за {now:%d.%m}", error)
        return 1


def run_inbox(args) -> int:
    check_setup(need_claude=False, need_telegram=True)
    cleanup()
    telegram = Telegram()
    try:
        result = inbox.process(telegram)
        return 0 if not result["failed"] else 1
    except Exception as error:
        notify_error(telegram, "Не получилось обработать голосовые", error)
        return 1


# ─── полностью автоматический режим (нейроголос) ────────────────────────
def make_short(ed: editor_mod.Editor, selection, number: int, day_dir: Path, hist: dict,
               date_label: str, today_text: str, seed: int) -> dict:
    material = articles.story_material(selection.item)
    script = ed.write_script(selection, material, today_text, reader="tts")
    log.info("Сценарий %d: «%s» (%s, %d слов)", number, script.hook, script.category, script.words)

    slug = f"{number:02d}_{slugify(script.hook)}"
    work = day_dir / f".work_{slug}"
    work.mkdir(parents=True, exist_ok=True)
    try:
        speech = voice.synthesize(script.text, work / "voice.mp3")
        bg = footage.get_background(script.footage_queries, script.category, speech.duration + 1.5,
                                    work, history.used_footage(hist), seed=seed)
        music = render.pick_music(script.mood)
        out = day_dir / f"{slug}.mp4"
        info = render.render_short(script.category, script.hook, speech, bg, out, date_label, music, seed=seed)
        render.fit_for_telegram(out)
    finally:
        shutil.rmtree(work, ignore_errors=True)

    meta = {**script.to_dict(), "background": bg.kind, "background_credits": bg.credits,
            "voice": speech.engine, "music": music.name if music else None, **info}
    (day_dir / f"{slug}.json").write_text(json.dumps(meta, ensure_ascii=False, indent=1), encoding="utf-8")
    log.info("Готов ролик %s (%.1f с, %.1f МБ)", out.name, info["duration"], out.stat().st_size / 1e6)
    return {"path": out, "script": script, "info": info, "credits": bg.credits,
            "engine": speech.engine, "footage_ids": bg.footage_ids}


def run_auto(args) -> int:
    check_setup(need_claude=True, need_telegram=not args.dry_run)
    cleanup()
    now = datetime.now(ZoneInfo(config.TIMEZONE))
    date_label = now.strftime("%d.%m")
    today_text = editor_mod.today_label()
    day_dir = config.OUTPUT_DIR / now.strftime("%Y-%m-%d")
    day_dir.mkdir(parents=True, exist_ok=True)
    telegram = None if args.dry_run else Telegram()
    hist = history.load()

    try:
        ed = editor_mod.Editor()
        selections, report = pick_topics(ed, hist, args.limit)
        results, failures = [], []
        seed_base = int(now.strftime("%j")) * 10
        for number, selection in enumerate(selections, 1):
            try:
                result = make_short(ed, selection, number, day_dir, hist, date_label, today_text,
                                    seed=seed_base + number)
                results.append(result)
                history.remember(hist, result["script"].title, selection.category,
                                 [i.link for i in selection.item.all_items], result["footage_ids"])
            except Exception as error:
                log.exception("Ролик %d не получился", number)
                failures.append(f"{selection.item.title[:90]} — {str(error)[:200]}")

        history.save(hist)
        log.info("Claude: %s", ed.cost_line())

        if telegram:
            lines = [f"⚽ <b>{tg_escape(config.CHANNEL_NAME)} · {tg_escape(today_text)}</b>",
                     f"Готово роликов: {len(results)}", ""]
            for n, r in enumerate(results, 1):
                s = r["script"]
                lines.append(f"{n}. {EMOJI.get(s.category, '⚽')} {tg_escape(s.title)}")
            if failures:
                lines += ["", "Не получилось:"] + [f"• {tg_escape(f)}" for f in failures]
            broken = broken_sources(report)
            if broken:
                lines += ["", "Источники с ошибкой:"] + broken
            lines += ["", f"<i>Claude: {tg_escape(ed.cost_line())}</i>"]
            telegram.send_message("\n".join(lines))
            for n, r in enumerate(results, 1):
                telegram.send_video(r["path"], video_caption(r["script"], n, r["credits"], r["engine"]),
                                    r["info"]["duration"])
        log.info("Готово: %d роликов в %s", len(results), day_dir)
        return 0 if results else 1

    except Exception as error:
        notify_error(telegram, f"Не получилось сделать ролики за {date_label}", error)
        return 1


# ─── демо ────────────────────────────────────────────────────────────────
def demo(args) -> int:
    """Ролик из готового сценария: проверка озвучки и монтажа без Claude и новостей."""
    check_setup(need_claude=False, need_telegram=False)
    story = json.loads((config.ROOT / "demo" / "demo_story.json").read_text(encoding="utf-8"))
    out_dir = config.OUTPUT_DIR / "demo"
    out_dir.mkdir(parents=True, exist_ok=True)
    if args.voice:
        speech = voice.from_human(story["script"], Path(args.voice), out_dir / "voice.wav")
    else:
        speech = voice.synthesize(story["script"], out_dir / "voice.mp3")
    bg = footage.get_background(story["footage_queries"], story["category"], speech.duration + 1.5,
                                out_dir, set(), seed=11)
    music = render.pick_music(story.get("mood", "весело"))
    out = out_dir / "demo_short.mp4"
    date_label = datetime.now(ZoneInfo(config.TIMEZONE)).strftime("%d.%m")
    info = render.render_short(story["category"], story["hook"], speech, bg, out, date_label, music, seed=11)
    log.info("Демо-ролик: %s (%s, голос: %s, фон: %s)", out, info, speech.engine, bg.kind)
    if args.send:
        script = editor_mod.Script.from_dict({**story, "text": story["script"]})
        Telegram().send_video(out, video_caption(script, 1, bg.credits, speech.engine), info["duration"])
    return 0


def main() -> int:
    parser = argparse.ArgumentParser(description="Футбольные шортсы в стиле канала «САША Ч.»")
    parser.add_argument("command", nargs="?", default="daily",
                        choices=["daily", "scripts", "inbox", "auto", "demo"],
                        help="что сделать (по умолчанию — утренний запуск)")
    parser.add_argument("--dry-run", action="store_true", help="не отправлять в Telegram")
    parser.add_argument("--limit", type=int, default=0, help="сколько тем взять (по умолчанию — все)")
    parser.add_argument("--demo", action="store_true", help="то же, что команда demo")
    parser.add_argument("--voice", help="для demo: файл с записанным голосом вместо синтеза")
    parser.add_argument("--send", action="store_true", help="для demo: отправить ролик в Telegram")
    args = parser.parse_args()
    setup_logging()

    command = "demo" if args.demo else args.command
    if command == "daily":
        command = "scripts" if config.VOICE_MODE == "human" else "auto"
    wants_telegram = not args.dry_run and (command != "demo" or args.send)
    if wants_telegram and config.TELEGRAM_BOT_TOKEN and not config.TELEGRAM_CHAT_ID:
        return first_contact()
    return {"scripts": run_scripts, "inbox": run_inbox, "auto": run_auto, "demo": demo}[command](args)


def run() -> int:
    """main() + причина любой остановки на странице запуска в GitHub."""
    try:
        return main()
    except SystemExit as stop:
        if isinstance(stop.code, str):
            github_error(stop.code)
        raise
    except Exception as error:
        github_error(f"{type(error).__name__}: {error}")
        raise


if __name__ == "__main__":
    sys.exit(run())
