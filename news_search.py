"""Find fresh news with actual Google Search grounding citations."""

import json
import os
import re
from datetime import datetime, timedelta, timezone

import requests
from google.genai import types
from pydantic import BaseModel, Field

from article_media import HEADERS, valid_url
from telegram_api import safe_error


class SearchStory(BaseModel):
    title: str
    summary: str
    source_url: str
    published_at: datetime


class SearchStories(BaseModel):
    stories: list[SearchStory] = Field(min_length=3, max_length=12)


def grounded_pool(response, now):
    """Reject plausible-looking answers without an actual search and cited URLs."""
    candidates = response.candidates or []
    if not candidates:
        raise ValueError("Grounding не вернул ответ")
    metadata = candidates[0].grounding_metadata
    if not metadata or not metadata.web_search_queries or not metadata.grounding_chunks:
        raise ValueError("Модель не выполнила поиск Google или не вернула источники")
    sources = {}
    for chunk in metadata.grounding_chunks:
        if not chunk.web or not valid_url(chunk.web.uri):
            continue
        url = chunk.web.uri
        sources[url] = url
        # Search sometimes returns Google redirect URLs instead of publisher URLs.
        if "vertexaisearch.cloud.google.com" in url:
            try:
                result = requests.get(url, headers=HEADERS, timeout=10)
                result.raise_for_status()
                if valid_url(result.url):
                    sources[result.url] = url
            except requests.RequestException:
                pass
    raw = re.sub(r"^```(?:json)?\s*|\s*```$", "", (response.text or "").strip())
    batch = SearchStories.model_validate_json(raw)
    pool = []
    seen = set()
    for story in batch.stories:
        published = story.published_at
        if published.tzinfo is None:
            published = published.replace(tzinfo=timezone.utc)
        if not now - timedelta(hours=48) <= published <= now + timedelta(minutes=15):
            continue
        if story.source_url not in sources or story.source_url in seen:
            continue
        if not story.title.strip() or not story.summary.strip():
            continue
        seen.add(story.source_url)
        pool.append(
            {
                "title": story.title,
                "summary": story.summary[:2000],
                "link": story.source_url,
                "published_at": published.isoformat(),
                "grounding_url": sources[story.source_url],
                "search_queries": list(metadata.web_search_queries),
            }
        )
    if len(pool) < 3:
        raise ValueError(
            "Grounding не подтвердил три свежие новости ссылками из поиска"
        )
    return pool


def search_news(client, history):
    now = datetime.now(timezone.utc)
    schema = json.dumps(SearchStories.model_json_schema(), ensure_ascii=False)
    prompt = f"""Сегодня {now.isoformat()}. ОБЯЗАТЕЛЬНО выполни Google Search.
Найди 6–9 разных реальных новостей за последние 48 часов: искусственный интеллект,
новые гаджеты, вычисления, научные открытия. Предпочитай первоисточники и редакционные статьи.
Каждая summary содержит только подтверждённые факты этой статьи. Не добавляй домыслы.
source_url скопируй из результатов поиска, published_at — фактическая дата публикации с часовым поясом.
Не включай рекламу, старые события, прогнозы как свершившиеся факты и повторные новости:
{json.dumps([item.get("topic") for item in history[-30:]], ensure_ascii=False)}
Ответ: только JSON, без Markdown и номеров сносок. Схема: {schema}
"""
    models = (os.getenv("GEMINI_SEARCH_MODELS") or "gemini-3.8-flash").split(",")
    errors = []
    for model in filter(None, (value.strip() for value in models)):
        try:
            response = client.models.generate_content(
                model=model,
                contents=prompt,
                config=types.GenerateContentConfig(
                    temperature=0.2,
                    tools=[types.Tool(google_search=types.GoogleSearch())],
                    automatic_function_calling=types.AutomaticFunctionCallingConfig(
                        disable=True
                    ),
                ),
            )
            pool = grounded_pool(response, now)
            print(
                f"Google Search Grounding: {len(pool)} новостей с источниками ({model})"
            )
            return pool
        except Exception as error:  # noqa: BLE001 - try the next model, redact API errors
            errors.append(safe_error(error))
    raise RuntimeError("Google Search Grounding недоступен: " + "; ".join(errors))
