"""Быстрая проверка для GitHub Actions — без установки библиотек.

Смотрит, есть ли в Telegram новые голосовые или ответы на тексты, и печатает
work=true/false. Если пришло только что-то постороннее, помечает это прочитанным,
чтобы не запускать тяжёлую сборку зря.

    python3 shorts/check_inbox.py >> "$GITHUB_OUTPUT"
"""
import json
import os
import sys
import urllib.parse
import urllib.request
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent


def call(token: str, method: str, **params):
    query = urllib.parse.urlencode(params)
    with urllib.request.urlopen(f"https://api.telegram.org/bot{token}/{method}?{query}", timeout=30) as r:
        return json.load(r).get("result", [])


def main() -> None:
    token = os.environ.get("TELEGRAM_BOT_TOKEN", "").strip()
    chat = os.environ.get("TELEGRAM_CHAT_ID", "").strip()
    if not token or not chat:
        print("work=false")
        return
    try:
        pending = json.loads((ROOT / "data" / "pending.json").read_text(encoding="utf-8"))
    except (FileNotFoundError, json.JSONDecodeError):
        pending = {"items": [], "last_update_id": 0}
    known = {i.get("message_id") for i in pending.get("items", []) if i.get("status") in ("waiting", "done")}
    last = pending.get("last_update_id") or 0
    params = {"timeout": 0, "limit": 100, "allowed_updates": '["message"]'}
    if last:
        params["offset"] = last + 1
    try:
        updates = call(token, "getUpdates", **params)
    except Exception as error:  # сеть или Telegram недоступны — попробуем в следующий раз
        print(f"проверка не удалась: {error}", file=sys.stderr)
        print("work=false")
        return

    relevant = False
    foreign = False
    for update in updates:
        message = update.get("message") or {}
        chat_info = message.get("chat") or {}
        ours = str(chat_info.get("id")) == chat or (
            chat_info.get("username") and chat.lstrip("@").lower() == chat_info["username"].lower())
        if not ours:
            foreign = True
            continue
        document = message.get("document") or {}
        has_voice = any(message.get(k) for k in ("voice", "audio", "video_note")) or \
            str(document.get("mime_type", "")).startswith("audio/")
        reply_id = (message.get("reply_to_message") or {}).get("message_id")
        if has_voice or (reply_id in known and message.get("text")):
            relevant = True
            break

    # Сообщения из других чатов не трогаем: если номер чата в настройках неверный,
    # по ним бот подскажет правильный. Через сутки Telegram удалит их сам.
    if updates and not relevant and not foreign:
        call(token, "getUpdates", offset=max(u["update_id"] for u in updates) + 1, timeout=0)
    print(f"work={'true' if relevant else 'false'}")


if __name__ == "__main__":
    main()
