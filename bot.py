import os
import sys
import re
import io
import requests
from google import genai
from google.genai import types

def get_required_env(name: str) -> str:
    value = os.getenv(name)
    if not value:
        print(f"Error: Environment variable {name} is not set.", file=sys.stderr)
        sys.exit(1)
    return value

def generate_digest(client: genai.Client) -> tuple[str, str, str]:
    prompt = """
Ты профессиональный редактор и преподаватель английского языка. Твоя задача — подготовить ежедневную сводку новостей из мира технологий, искусственного интеллекта и гаджетов по методу двуязычного чтения (Diglot Weave).

Строгие требования:
1. Найди самые актуальные и значимые события за последние 24 часа в мире технологий, искусственного интеллекта, гаджетов и IT в России и мире.
2. Сформируй дайджест ровно из 3 ключевых событий на русском языке.
3. Вплети в русский текст 12–15 общеупотребительных английских слов (глаголы, прилагательные, связующие слова и базовые существительные повседневного языка, избегая узких IT-терминов). Контекст предложений должен позволять интуитивно понять значение каждого слова.
4. Выдели каждое английское слово полужирным шрифтом (**word**).
5. Укажи кликабельные ссылки на первоисточники новостей в формате [Название источника](URL).
6. НЕ пиши словарь прямо в основном тексте.

Формат вывода должен быть строго разделен на три секции:

[DIGEST]
(Сюда помести сам дайджест из 3 событий с кликабельными ссылками на источники)

[DICTIONARY]
📖 **Словарь для самопроверки:**
(Сюда помести список всех 12–15 использованных английских слов с транскрипцией и переводом на русский язык)

[IMAGE_PROMPT]
(Сюда помести подробный промпт на английском языке для генерации красивой, современной фотореалистичной или 3D-иллюстрации к главной новости)
"""
    print("Генерация дайджеста через Gemini с поиском новостей...")
    chat = client.chats.create(
        model="gemini-3.8-flash",
        config=types.GenerateContentConfig(
            tools=[types.Tool(google_search=types.GoogleSearch())],
            temperature=0.7,
        )
    )
    response = chat.send_message(prompt)

    full_text = response.text or ""
    
    # Парсим секции
    digest_match = re.search(r"\[DIGEST\](.*?)(\[DICTIONARY\]|$)", full_text, re.DOTALL | re.IGNORECASE)
    dict_match = re.search(r"\[DICTIONARY\](.*?)(\[IMAGE_PROMPT\]|$)", full_text, re.DOTALL | re.IGNORECASE)
    img_match = re.search(r"\[IMAGE_PROMPT\](.*)$", full_text, re.DOTALL | re.IGNORECASE)
    
    digest_text = digest_match.group(1).strip() if digest_match else full_text.strip()
    dict_text = dict_match.group(1).strip() if dict_match else "📖 Словарь формируется..."
    img_prompt = img_match.group(1).strip() if img_match else "Modern high-tech illustration representing artificial intelligence and future technology"

    return digest_text, dict_text, img_prompt

def generate_image(client: genai.Client, prompt: str) -> bytes | None:
    print(f"Генерация иллюстрации по промпту: {prompt[:100]}...")
    try:
        result = client.models.generate_images(
            model="imagen-3.0-generate-002",
            prompt=prompt,
            config=dict(
                number_of_images=1,
                aspect_ratio="16:9",
            )
        )
        if result.generated_images:
            return result.generated_images[0].image.image_bytes
    except Exception as e:
        print(f"Предупреждение: генерация Imagen не удалась ({e}). Пост будет отправлен без сгенерированного фото.", file=sys.stderr)
    return None

def send_telegram_photo(token: str, chat_id: str, photo_bytes: bytes) -> int | None:
    url = f"https://api.telegram.org/bot{token}/sendPhoto"
    files = {"photo": ("news.jpg", io.BytesIO(photo_bytes), "image/jpeg")}
    data = {"chat_id": chat_id}
    
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
        # Резервная отправка без Markdown (если в тексте неэкранированные символы)
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

    digest_text, dict_text, img_prompt = generate_digest(client)
    photo_data = generate_image(client, img_prompt)

    print("Публикация в Telegram...")
    # 1. Если есть фото — отправляем его
    if photo_data:
        send_telegram_photo(tg_token, tg_chat_id, photo_data)

    # 2. Отправляем основной пост с дайджестом
    main_msg_id = send_telegram_message(tg_token, tg_chat_id, digest_text)

    # 3. Отправляем словарь ответом (в комментарии) к основному посту
    if main_msg_id:
        print("Отправка словаря в комментарии к посту...")
        send_telegram_message(tg_token, tg_chat_id, dict_text, reply_to_id=main_msg_id)

    print("Готово! Пост и словарь успешно опубликованы.")

if __name__ == "__main__":
    main()
