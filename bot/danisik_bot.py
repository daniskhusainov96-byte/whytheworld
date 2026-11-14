"""Телеграм-бот «Данисик».

Что делает:
1. Забирает новые сообщения, которые вы прислали боту (текст, фото, видео,
   пересланные посты).
2. Сохраняет их в папку inbox/ этого репозитория: текст в .md, фото как .jpg,
   из видео вытаскивает несколько кадров (само видео в репозиторий не кладёт).
3. Если задан ANTHROPIC_API_KEY, отправляет всё это в Claude и отвечает вам в
   Телеграме вариантами похожего контента. Ответ тоже сохраняется в inbox/.

Запуск:
    python bot/danisik_bot.py --once   # один проход (так работает GitHub Actions)
    python bot/danisik_bot.py          # крутится постоянно (для своего компьютера/сервера)

Переменные окружения:
    TELEGRAM_BOT_TOKEN   токен от @BotFather (обязательно)
    ALLOWED_USER_IDS     ваш Telegram user id, можно несколько через запятую
                         (обязательно: чужие сообщения бот игнорирует)
    ANTHROPIC_API_KEY    ключ Claude API (необязательно: без него бот только сохраняет)
"""

from __future__ import annotations

import argparse
import base64
import datetime as dt
import json
import os
import re
import shutil
import subprocess
import sys
import tempfile
import time
from pathlib import Path

import requests

ROOT = Path(__file__).resolve().parent.parent
INBOX = ROOT / "inbox"
OFFSET_FILE = INBOX / ".tg_offset"
STYLE_FILE = Path(__file__).resolve().parent / "style_prompt.md"

TG_API = "https://api.telegram.org/bot{token}/{method}"
TG_FILE = "https://api.telegram.org/file/bot{token}/{path}"
TG_MAX_DOWNLOAD = 20 * 1024 * 1024  # Bot API не отдаёт файлы больше 20 МБ
TG_MAX_MESSAGE = 4000

MODEL = "claude-opus-5-5"
VIDEO_FRAMES = 4


# ---------------------------------------------------------------- Telegram --

class Telegram:
    def __init__(self, token: str):
        self.token = token

    def call(self, method: str, **params):
        r = requests.post(TG_API.format(token=self.token, method=method),
                          json=params, timeout=60)
        data = r.json()
        if not data.get("ok"):
            raise RuntimeError(f"Telegram {method}: {data.get('description')}")
        return data["result"]

    def download(self, file_id: str, size: int | None, dest: Path) -> bool:
        if size and size > TG_MAX_DOWNLOAD:
            return False
        try:
            info = self.call("getFile", file_id=file_id)
        except RuntimeError as e:  # например "file is too big"
            print(f"  не скачался файл: {e}")
            return False
        r = requests.get(TG_FILE.format(token=self.token, path=info["file_path"]),
                         timeout=120)
        r.raise_for_status()
        dest.write_bytes(r.content)
        return True

    def send(self, chat_id: int, text: str, reply_to: int | None = None):
        for i in range(0, len(text), TG_MAX_MESSAGE):
            params = {"chat_id": chat_id, "text": text[i:i + TG_MAX_MESSAGE]}
            if reply_to and i == 0:
                params["reply_parameters"] = {"message_id": reply_to,
                                              "allow_sending_without_reply": True}
            self.call("sendMessage", **params)


# ------------------------------------------------------------------ helpers --

def read_offset() -> int:
    try:
        return int(OFFSET_FILE.read_text().strip())
    except (FileNotFoundError, ValueError):
        return 0


def write_offset(value: int):
    INBOX.mkdir(parents=True, exist_ok=True)
    OFFSET_FILE.write_text(str(value))


def extract_frames(video: Path, out_dir: Path, stem: str, count: int) -> list[Path]:
    """Равномерно вытаскивает count кадров из видео через ffmpeg."""
    if not shutil.which("ffmpeg"):
        print("  ffmpeg не найден, кадры из видео не вытащить")
        return []
    try:
        duration = float(subprocess.run(
            ["ffprobe", "-v", "error", "-show_entries", "format=duration",
             "-of", "default=nw=1:nk=1", str(video)],
            capture_output=True, text=True, check=True).stdout.strip())
    except (subprocess.CalledProcessError, ValueError):
        duration = 0.0
    frames = []
    for n in range(count):
        t = duration * (n + 0.5) / count if duration else 0
        dest = out_dir / f"{stem}_frame{n + 1}.jpg"
        res = subprocess.run(
            ["ffmpeg", "-y", "-loglevel", "error", "-ss", f"{t:.2f}", "-i", str(video),
             "-frames:v", "1", "-vf", "scale='min(1280,iw)':-2", "-q:v", "3", str(dest)],
            capture_output=True)
        if res.returncode == 0 and dest.exists():
            frames.append(dest)
        if not duration:
            break
    return frames


def message_text(msg: dict) -> str:
    return (msg.get("text") or msg.get("caption") or "").strip()


def forward_source(msg: dict) -> str:
    origin = msg.get("forward_origin") or {}
    chat = origin.get("chat") or origin.get("sender_chat") or {}
    if chat:
        name = chat.get("title") or ""
        if chat.get("username"):
            name += f" (@{chat['username']})"
        return name.strip()
    user = origin.get("sender_user") or {}
    if user:
        return " ".join(filter(None, [user.get("first_name"), user.get("last_name")]))
    return origin.get("sender_user_name", "")


# ------------------------------------------------------------------- saving --

def save_message(tg: Telegram, msg: dict) -> dict:
    """Сохраняет сообщение в inbox/ и возвращает описание сохранённого."""
    date = dt.datetime.fromtimestamp(msg["date"], dt.timezone.utc)
    day_dir = INBOX / date.strftime("%Y-%m-%d")
    day_dir.mkdir(parents=True, exist_ok=True)
    stem = f"{date.strftime('%H%M%S')}_{msg['message_id']}"

    text = message_text(msg)
    images: list[Path] = []
    notes: list[str] = []

    if msg.get("photo"):
        best = msg["photo"][-1]
        dest = day_dir / f"{stem}.jpg"
        if tg.download(best["file_id"], best.get("file_size"), dest):
            images.append(dest)

    video = msg.get("video") or msg.get("video_note") or msg.get("animation")
    if video:
        notes.append(f"видео, {video.get('duration', '?')} сек")
        with tempfile.TemporaryDirectory() as tmp:
            tmp_video = Path(tmp) / "video.mp4"
            if tg.download(video["file_id"], video.get("file_size"), tmp_video):
                images += extract_frames(tmp_video, day_dir, stem, VIDEO_FRAMES)
            else:
                notes.append("файл больше 20 МБ, Телеграм не даёт боту его скачать; "
                             "сохранено только описание")
        thumb = video.get("thumbnail")
        if not images and thumb:
            dest = day_dir / f"{stem}_thumb.jpg"
            if tg.download(thumb["file_id"], thumb.get("file_size"), dest):
                images.append(dest)

    doc = msg.get("document")
    if doc and (doc.get("mime_type") or "").startswith("image/"):
        ext = Path(doc.get("file_name") or "x.jpg").suffix or ".jpg"
        dest = day_dir / f"{stem}{ext}"
        if tg.download(doc["file_id"], doc.get("file_size"), dest):
            images.append(dest)
    elif doc:
        notes.append(f"файл {doc.get('file_name', '')} (не сохранён)")

    if msg.get("voice") or msg.get("audio"):
        notes.append("аудио/голосовое (не расшифровано)")

    source = forward_source(msg)
    md = [f"# Входящее {date.strftime('%Y-%m-%d %H:%M')} UTC", ""]
    if source:
        md.append(f"**Переслано из:** {source}")
    if notes:
        md.append(f"**Вложения:** {'; '.join(notes)}")
    for img in images:
        md.append(f"![]({img.name})")
    md += ["", "## Текст", "", text or "_(без текста)_", ""]
    md_path = day_dir / f"{stem}.md"
    md_path.write_text("\n".join(md), encoding="utf-8")

    return {"md": md_path, "text": text, "images": images, "notes": notes,
            "source": source}


# ------------------------------------------------------------------- Claude --

def generate_similar(item: dict, wish: str) -> str | None:
    if not os.environ.get("ANTHROPIC_API_KEY"):
        return None
    import anthropic

    system = STYLE_FILE.read_text(encoding="utf-8")

    content: list[dict] = []
    for img in item["images"][:VIDEO_FRAMES + 1]:
        content.append({"type": "image", "source": {
            "type": "base64", "media_type": "image/jpeg",
            "data": base64.standard_b64encode(img.read_bytes()).decode()}})

    parts = ["Вот пример контента, который мне понравился."]
    if item["source"]:
        parts.append(f"Источник: {item['source']}.")
    if item["notes"]:
        parts.append(f"Вложения: {'; '.join(item['notes'])}.")
    if item["images"]:
        parts.append("Картинки выше: фото или кадры из видео по порядку.")
    if wish and wish == item["text"]:
        parts.append(f"Моё пожелание:\n{wish}")
    else:
        parts.append(f"Текст:\n{item['text'] or '(без текста)'}")
        parts.append(wish or "Сделай похожий контент для моих каналов.")
    content.append({"type": "text", "text": "\n\n".join(parts)})

    client = anthropic.Anthropic()
    try:
        response = client.beta.messages.create(
            model=MODEL,
            max_tokens=16000,
            output_config={"effort": "medium"},
            betas=["server-side-fallback-2026-07-01"],
            fallbacks="default",
            system=system,
            messages=[{"role": "user", "content": content}],
        )
    except anthropic.RateLimitError:
        return "Claude сейчас перегружен (лимит запросов). Пришлите ещё раз чуть позже."
    except anthropic.APIStatusError as e:
        return f"Claude вернул ошибку {e.status_code}: {e.message}"
    except anthropic.APIConnectionError:
        return "Не получилось связаться с Claude. Попробуйте позже."

    if response.stop_reason == "refusal":
        return "Claude отказался работать с этим материалом."
    return "".join(b.text for b in response.content if b.type == "text").strip()


# --------------------------------------------------------------------- main --

HELP = (
    "Пришлите мне текст, фото, видео или перешлите пост.\n"
    "Я сохраню его и предложу похожие варианты для вашего канала.\n\n"
    "Подсказки:\n"
    "• Подпишите видео/фото, что нужно: «подпись под рилс», «пост в телегу» и т.п.\n"
    "• Начните сообщение с /save, чтобы только сохранить без генерации.\n"
    "• /id покажет ваш Telegram id."
)


def handle(tg: Telegram, msg: dict, allowed: set[int]) -> bool:
    """Возвращает True, если в inbox/ что-то записалось."""
    user_id = (msg.get("from") or {}).get("id")
    chat_id = msg["chat"]["id"]
    text = message_text(msg)

    if text.startswith("/id"):
        tg.send(chat_id, f"Ваш id: {user_id}")
        return False
    if user_id not in allowed:
        print(f"  пропускаю сообщение от чужого id {user_id}")
        return False
    if text.startswith("/start") or text.startswith("/help"):
        tg.send(chat_id, HELP)
        return False

    save_only = text.startswith("/save")
    if save_only:
        text = text[len("/save"):].strip()
        if msg.get("text"):
            msg["text"] = text
        else:
            msg["caption"] = text

    item = save_message(tg, msg)
    rel = item["md"].relative_to(ROOT)
    print(f"  сохранено: {rel}")

    if save_only:
        tg.send(chat_id, f"Сохранил: {rel}", reply_to=msg["message_id"])
        return True

    # Если сообщение не пересланное, а ваша подпись, считаем её пожеланием.
    wish = "" if item["source"] else item["text"]
    answer = generate_similar(item, wish)
    if answer is None:
        tg.send(chat_id, f"Сохранил: {rel}\n(генерация выключена: нет ANTHROPIC_API_KEY)",
                reply_to=msg["message_id"])
        return True

    answer_path = item["md"].with_suffix(".answer.md")
    answer_path.write_text(answer + "\n", encoding="utf-8")
    tg.send(chat_id, answer, reply_to=msg["message_id"])
    return True


def poll_once(tg: Telegram, allowed: set[int], timeout: int = 0) -> int:
    offset = read_offset()
    updates = tg.call("getUpdates", offset=offset, timeout=timeout,
                      allowed_updates=["message", "channel_post"])
    saved = 0
    for upd in updates:
        msg = upd.get("message") or upd.get("channel_post")
        if msg:
            print(f"сообщение {msg['message_id']}")
            try:
                saved += handle(tg, msg, allowed)
            except Exception as e:  # одно битое сообщение не должно ронять бота
                print(f"  ошибка: {e!r}")
                try:
                    tg.send(msg["chat"]["id"], f"Не получилось обработать: {e}")
                except Exception:
                    pass
        write_offset(upd["update_id"] + 1)
    return saved


def main():
    parser = argparse.ArgumentParser()
    parser.add_argument("--once", action="store_true", help="один проход и выход")
    args = parser.parse_args()

    token = os.environ.get("TELEGRAM_BOT_TOKEN")
    allowed = {int(x) for x in re.findall(r"-?\d+", os.environ.get("ALLOWED_USER_IDS", ""))}
    if not token:
        sys.exit("Не задан TELEGRAM_BOT_TOKEN")
    if not allowed:
        print("ВНИМАНИЕ: ALLOWED_USER_IDS пуст, бот ответит только на /id")

    tg = Telegram(token)
    if args.once:
        print(json.dumps({"saved": poll_once(tg, allowed)}))
        return
    print("Бот запущен. Ctrl+C для остановки.")
    while True:
        try:
            poll_once(tg, allowed, timeout=50)
        except requests.RequestException as e:
            print(f"сеть: {e!r}")
            time.sleep(5)


if __name__ == "__main__":
    main()
