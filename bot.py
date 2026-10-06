import os
import sys
import re
import io
import requests
import feedparser
from google import genai
from google.genai import types

def get_required_env(name: str) -> str:
    value = os.getenv(name)
    if not value:
        print(f"Error: Environment variable {name} is not set.", file=sys.stderr)
        sys.exit(1)
    return value

def fetch_recent_news(limit: int = 6) -> tuple[str, str | None]:
    """Собирает самые свежие новости за последние 24 часа из Google News и профильных изданий."""
    feeds = [
        "https://news.google.com/rss/headlines/section/topic/TECHNOLOGY?hl=ru&gl=RU&ceid=RU:ru",
        "https://habr.com/ru/rss/hubs/all/",
    ]
    news_items = []
    first_link = None
    for url in feeds:
        try:
            feed = feedparser.parse(url)
            for entry in feed.entries[:limit]:
                title = entry.get("title", "").strip()
                link = entry.get("link", "").strip()
                summary = re.sub(r"<[^>]+>", "", entry.get("summary", "")).strip()
                if title:
                    if not first_link:
                        first_link = link
                    news_items.append(f"- Заголовок: {title}\n  Ссылка: {link}\n  Кратко: {summary[:250]}")
        except Exception as e:
            print(f"Ошибка при чтении ленты {url}: {e}", file=sys.stderr)

    if not news_items:
        return "В мире технологий активно развиваются мультимодальные модели искусственного интеллекта и новые устройства.", None
    return "\n\n".join(news_items[:10]), first_link

def generate_digest(client: genai.Client, raw_news: str) -> tuple[str, str, str]:
    prompt = f"""
Ты профессиональный редактор и преподаватель английского языка. Твоя задача — подготовить ежедневную сводку новостей из мира технологий, искусственного интеллекта и гаджетов по методу двуязычного чтения (Diglot Weave).

Вот актуальные новости за последние 24 часа:
{raw_news}

Строгие требования:
1. Выбери из предоставленных новостей ровно 3 ключевых и самых интересных события и сформируй дайджест на русском языке.
2. Вплети в русский текст ровно 12–15 общеупотребительных английских слов (глаголы, прилагательные, связующие слова и базовые существительные повседневного языка, избегая узких IT-терминов). Контекст предложений должен позволять интуитивно понять значение каждого слова.
3. Выдели каждое английское слово полужирным шрифтом (**word**).
4. Обязательно добавь кликабельные ссылки на первоисточники новостей из списка выше в формате [Название источника](URL).
5. НЕ пиши словарь прямо в основном дайджесте.

Формат вывода должен быть строго разделен на три секции:

[DIGEST]
(Сюда помести сам дайджест из 3 событий с кликабельными ссылками на источники)

[DICTIONARY]
📖 **Словарь для самопроверки:**
(Сюда помести список всех 12–15 использованных английских слов с транскрипцией и переводом на русский язык)

[IMAGE_PROMPT]
(Сюда помести подробный промпт на английском языке для генерации красивой, современной иллюстрации к главной новости)
"""
    print("Генерация дайджеста через Gemini...")
    models_to_try = ["gemini-3.5-flash-lite", "gemini-3.8-flash"]
    response = None

    for model_name in models_to_try:
        try:
            print(f"Пробуем модель {model_name}...")
            response = client.models.generate_content(
                model=model_name,
                contents=prompt,
                config=types.GenerateContentConfig(temperature=0.7),
            )
            if response and response.text:
                break
        except Exception as e:
            print(f"Ошибка при вызове {model_name}: {e}", file=sys.stderr)

    if not response or not response.text:
        raise RuntimeError("Не удалось сгенерировать контент ни одной из доступных моделей Gemini.")

    full_text = response.text
    digest_match = re.search(r"\[DIGEST\](.*?)(\[DICTIONARY\]|$)", full_text, re.DOTALL | re.IGNORECASE)
    dict_match = re.search(r"\[DICTIONARY\](.*?)(\[IMAGE_PROMPT\]|$)", full_text, re.DOTALL | re.IGNORECASE)
    img_match = re.search(r"\[IMAGE_PROMPT\](.*)$", full_text, re.DOTALL | re.IGNORECASE)

    digest_text = digest_match.group(1).strip() if digest_match else full_text.strip()
    dict_text = dict_match.group(1).strip() if dict_match else "📖 Словарь формируется..."
    img_prompt = img_match.group(1).strip() if img_match else "Modern high-tech illustration representing artificial intelligence and future technology"

    return digest_text, dict_text, img_prompt

def extract_cover_image(link: str) -> str | None:
    """Извлекает обложку статьи через OpenGraph тег og:image."""
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

def generate_image(client: genai.Client, prompt: str) -> bytes | None:
    print(f"Попытка генерации иллюстрации Imagen: {prompt[:100]}...")
    try:
        result = client.models.generate_images(
            model="imagen-3.0-generate-002",
            prompt=prompt,
            config=dict(number_of_images=1, aspect_ratio="16:9")
        )
        if result.generated_images:
            return result.generated_images[0].image.image_bytes
    except Exception as e:
        print(f"Imagen недоступен ({e}), будет использована оригинальная обложка статьи.", file=sys.stderr)
    return None

def send_telegram_photo(token: str, chat_id: str, photo: bytes | str) -> int | None:
    url = f"https://api.telegram.org/bot{token}/sendPhoto"
    data = {"chat_id": chat_id}
    
    if isinstance(photo, str):
        data["photo"] = photo
        resp = requests.post(url, data=data, timeout=30)
    else:
        files = {"photo": ("news.jpg", io.BytesIO(photo), "image/jpeg")}
        resp = requests.post(url, data=data, files=files, timeout=30)

    if resp.status_code == 200:
        return resp.json().get("result", {}).get("message_id")
    print(f"Ошибка отправки фото: {resp.text}", file=sys.stderr)
    return None

def send_telegram_message(token: str, chat_id: str, text: str, reply_to_id: int | None = None) -> int | None:
    url = f"https://api.telegram.org/bot{token}/sendMessage"
    
    payload = {
        "chat_id": chat_id,
        "text": text[:4000],
        "parse_mode": "Markdown"
    }
    if reply_to_id:
        payload["reply_to_message_id"] = reply_to_id

    res = requests.post(url, json=payload, timeout=30)
    if res.status_code != 200:
        print("Предупреждение: ошибка разметки Markdown, отправка обычным текстом...")
        payload.pop("parse_mode", None)
        res = requests.post(url, json=payload, timeout=30)
        res.raise_for_status()

    return res.json().get("result", {}).get("message_id")

def main():
    gemini_key = get_required_env("GEMINI_API_KEY")
    tg_token = get_required_env("TELEGRAM_BOT_TOKEN")
    tg_chat_id = get_required_env("TELEGRAM_CHAT_ID")

    client = genai.Client(api_key=gemini_key)

    raw_news, first_link = fetch_recent_news(limit=6)
    digest_text, dict_text, img_prompt = generate_digest(client, raw_news)
    
    photo = generate_image(client, img_prompt)
    if not photo and first_link:
        print("Поиск обложки из оригинальной статьи...")
        photo = extract_cover_image(first_link)

    print("Публикация в Telegram...")
    if photo:
        send_telegram_photo(tg_token, tg_chat_id, photo)

    main_msg_id = send_telegram_message(tg_token, tg_chat_id, digest_text)

    if main_msg_id:
        print("Отправка словаря в комментарии к посту...")
        send_telegram_message(tg_token, tg_chat_id, dict_text, reply_to_id=main_msg_id)

    print("Готово! Пост и словарь успешно опубликованы.")

if __name__ == "__main__":
    main()
