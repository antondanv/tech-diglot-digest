import os
import sys
import re
import io
import json
import random
import time
import requests
import feedparser
from google import genai
from google.genai import types

QUEUE_FILE = "queue.json"

def get_required_env(name: str) -> str:
    value = os.getenv(name)
    if not value:
        print(f"Error: Environment variable {name} is not set.", file=sys.stderr)
        sys.exit(1)
    return value

def extract_cover_image(link: str) -> str | None:
    """Извлекает обложку статьи через OpenGraph тег og:image."""
    if not link:
        return None
    try:
        req = requests.get(link, headers={"User-Agent": "Mozilla/5.0"}, timeout=6)
        match = re.search(r'property=["\']og:image["\']\s+content=["\']([^"\']+)["\']', req.text, re.IGNORECASE)
        if not match:
            match = re.search(r'content=["\']([^"\']+)["\']\s+property=["\']og:image["\']', req.text, re.IGNORECASE)
        if match:
            return match.group(1).strip()
    except Exception as e:
        print(f"Не удалось извлечь обложку из {link}: {e}", file=sys.stderr)
    return None

def fetch_diverse_news_pool() -> list[dict]:
    """Собирает разнообразный пул новостей из разных сфер (ИИ, космос, гаджеты, IT, наука)."""
    feeds = [
        "https://news.google.com/rss/headlines/section/topic/TECHNOLOGY?hl=ru&gl=RU&ceid=RU:ru",
        "https://habr.com/ru/rss/hubs/all/",
        "https://news.google.com/rss/search?q=наука+технологии+гаджеты&hl=ru&gl=RU&ceid=RU:ru",
        "https://news.google.com/rss/search?q=искусственный+интеллект&hl=ru&gl=RU&ceid=RU:ru",
    ]
    pool = []
    seen_titles = set()

    for url in feeds:
        try:
            feed = feedparser.parse(url)
            for entry in feed.entries[:8]:
                title = entry.get("title", "").strip()
                link = entry.get("link", "").strip()
                summary = re.sub(r"<[^>]+>", "", entry.get("summary", "")).strip()
                clean_title = re.sub(r"\s*-\s*[^-]+$", "", title).strip()

                if clean_title and clean_title not in seen_titles:
                    seen_titles.add(clean_title)
                    pool.append({
                        "title": clean_title,
                        "link": link,
                        "summary": summary[:300]
                    })
        except Exception as e:
            print(f"Ошибка при чтении ленты {url}: {e}", file=sys.stderr)

    random.shuffle(pool)
    return pool[:12]

def generate_daily_batch(client: genai.Client) -> list[dict]:
    """Генерирует 3 разных поста по разным случайным темам (по 3-4 английских слова на пост)."""
    news_pool = fetch_diverse_news_pool()
    news_text = "\n\n".join(
        f"Новость #{i+1}:\n- Заголовок: {item['title']}\n- Ссылка: {item['link']}\n- Описание: {item['summary']}"
        for i, item in enumerate(news_pool)
    )

    prompt = f"""
Ты профессиональный редактор Telegram-канала и преподаватель английского языка.
Твоя задача — отобрать из списка ниже 3 САМЫЕ РАЗНЫЕ и интересные темы (например: одна про ИИ/нейросети, вторая про гаджет/железо, третья про софт, науку, космос или безопасность — выбирай каждый день разные направления).

Список актуальных новостей:
{news_text}

Для каждой из 3 выбранных тем создай отдельный самостоятельный пост по методу двуязычного чтения (Diglot Weave).

Строгие требования к каждому посту:
1. Пост посвящен ТОЛЬКО одной конкретной теме/событию.
2. В текст на русском языке органично вплети РОВНО 3–4 общеупотребительных английских слова (глаголы, прилагательные, связки, простые существительные). Избегай узких технических терминов.
3. Выдели каждое английское слово полужирным шрифтом (**word**).
4. НЕ вставляй никаких внешних ссылок, URL или сносок в текст поста. Длина текста поста СТРОГО до 850 символов (чтобы он гарантированно поместился в подпись к фото в Telegram).
5. Создай мини-словарь ДЛЯ КОММЕНТАРИЕВ ровно из этих 3–4 слов с транскрипцией и переводом на русский.

Ответ верни строго в формате валидного JSON-массива из 3 объектов:
[
  {{
    "topic": "Краткая тема (например, Искусственный интеллект)",
    "text": "Чистый текст поста на русском с 3-4 выделенными словами **word** (без ссылок)",
    "dictionary": "📖 **Словарь к посту:**\\n• **word** [транскрипция] — перевод\\n• ...",
    "source_url": "URL источника из списка выше (только для извлечения обложки)"
  }},
  ...
]
Никаких комментариев до и после JSON не пиши, только чистый JSON-массив.
"""
    print("Генерация 3 постов на день через Gemini...")
    models = ["gemini-3.5-flash-lite", "gemini-3.8-flash"]
    response = None

    for m in models:
        try:
            print(f"Запрос к {m}...")
            response = client.models.generate_content(
                model=m,
                contents=prompt,
                config=types.GenerateContentConfig(temperature=0.8)
            )
            if response and response.text:
                break
        except Exception as e:
            print(f"Ошибка вызова {m}: {e}", file=sys.stderr)

    if not response or not response.text:
        raise RuntimeError("Не удалось сгенерировать посты через Gemini.")

    raw_json = response.text.strip()
    raw_json = re.sub(r"^```(?:json)?", "", raw_json, flags=re.MULTILINE)
    raw_json = re.sub(r"```$", "", raw_json, flags=re.MULTILINE).strip()

    posts = json.loads(raw_json)

    # Добавляем обложки к каждому посту
    for post in posts:
        url = post.get("source_url")
        post["image_url"] = extract_cover_image(url) if url else None

    return posts

def send_telegram_post(token: str, chat_id: str, photo_url: str | None, text: str) -> int | None:
    """Отправляет изображение и текст ОДНИМ сообщением через sendPhoto с caption."""
    if photo_url:
        url = f"https://api.telegram.org/bot{token}/sendPhoto"
        data = {
            "chat_id": chat_id,
            "photo": photo_url,
            "caption": text[:1024],
            "parse_mode": "Markdown"
        }
        resp = requests.post(url, data=data, timeout=30)
        if resp.status_code == 200:
            return resp.json().get("result", {}).get("message_id")
        print(f"Ошибка sendPhoto (попробуем отправить текстом): {resp.text}", file=sys.stderr)

    # Резервная отправка текстом, если фото не подошло
    url = f"https://api.telegram.org/bot{token}/sendMessage"
    payload = {"chat_id": chat_id, "text": text[:4000], "parse_mode": "Markdown"}
    res = requests.post(url, json=payload, timeout=30)
    if res.status_code != 200:
        payload.pop("parse_mode", None)
        res = requests.post(url, json=payload, timeout=30)
        res.raise_for_status()

    return res.json().get("result", {}).get("message_id")

def send_telegram_comment(token: str, channel_id: str, discussion_id: str | None, channel_msg_id: int, dict_text: str):
    """Отправляет словарь в группу обсуждения под постом канала."""
    if discussion_id:
        url = f"https://api.telegram.org/bot{token}/sendMessage"
        payload = {
            "chat_id": discussion_id,
            "text": dict_text[:4000],
            "parse_mode": "Markdown",
            "reply_parameters": {
                "chat_id": channel_id,
                "message_id": channel_msg_id
            }
        }
        res = requests.post(url, json=payload, timeout=30)
        if res.status_code == 200:
            print("Словарь отправлен прямо в комментарии к посту в группе обсуждения!")
            return res.json().get("result", {}).get("message_id")
        else:
            print(f"Предупреждение: не удалось отправить в группу обсуждения {discussion_id} ({res.text}). Пробуем в канал...", file=sys.stderr)

    # Резервный вариант, если бот не добавлен в группу обсуждения
    url = f"https://api.telegram.org/bot{token}/sendMessage"
    payload = {
        "chat_id": channel_id,
        "text": dict_text[:4000],
        "parse_mode": "Markdown",
        "reply_to_message_id": channel_msg_id
    }
    res = requests.post(url, json=payload, timeout=30)
    return res.json().get("result", {}).get("message_id") if res.status_code == 200 else None

def load_queue() -> list[dict]:
    if os.path.exists(QUEUE_FILE):
        try:
            with open(QUEUE_FILE, "r", encoding="utf-8") as f:
                return json.load(f)
        except Exception as e:
            print(f"Ошибка чтения {QUEUE_FILE}: {e}", file=sys.stderr)
    return []

def save_queue(queue: list[dict]):
    with open(QUEUE_FILE, "w", encoding="utf-8") as f:
        json.dump(queue, f, ensure_ascii=False, indent=2)

def main():
    gemini_key = get_required_env("GEMINI_API_KEY")
    tg_token = get_required_env("TELEGRAM_BOT_TOKEN")
    tg_chat_id = get_required_env("TELEGRAM_CHAT_ID")
    discussion_id = os.getenv("DISCUSSION_CHAT_ID")

    client = genai.Client(api_key=gemini_key)
    queue = load_queue()

    if not queue:
        print("Очередь пуста. Генерируем 3 новых поста на сегодня с разными темами...")
        queue = generate_daily_batch(client)
        if not queue:
            print("Не удалось сгенерировать посты.", file=sys.stderr)
            sys.exit(1)

    current_post = queue.pop(0)
    save_queue(queue)
    print(f"Публикуем пост: {current_post.get('topic', 'Новость')} (в очереди осталось: {len(queue)})")

    # 1. Отправляем изображение и текст ОДНИМ сообщением
    msg_id = send_telegram_post(
        tg_token,
        tg_chat_id,
        current_post.get("image_url"),
        current_post.get("text", "")
    )

    # 2. Отправляем словарь в комментарии под этим постом
    dict_text = current_post.get("dictionary", "")
    if msg_id and dict_text:
        print("Пауза 2 сек перед отправкой комментария...")
        time.sleep(2)
        print("Отправка словаря (3-4 слова) в комментарии...")
        send_telegram_comment(tg_token, tg_chat_id, discussion_id, msg_id, dict_text)

    print("Публикация завершена успешно!")

if __name__ == "__main__":
    main()
