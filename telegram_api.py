"""Publish channel posts and dictionaries in their linked discussion threads."""

import html
import json
import os
import re
import time

import requests

from article_media import image_bytes, valid_url


class TelegramError(RuntimeError):
    def __init__(self, method, code, description):
        self.code = code
        super().__init__(f"Telegram {method}: {code} {description}")


class DeliveryUnknown(RuntimeError):
    """A timed out write may already have reached Telegram; do not duplicate it."""


def safe_error(error):
    text = str(error)
    for name in ("TELEGRAM_BOT_TOKEN", "GEMINI_API_KEY"):
        value = os.getenv(name)
        if value:
            text = text.replace(value, "[REDACTED]")
    return re.sub(r"bot\d+:[A-Za-z0-9_-]+", "bot[REDACTED]", text)


def telegram_html(text):
    escaped = html.escape(text, quote=False)
    return re.sub(r"\*\*([^*\n]+)\*\*", r"<b>\1</b>", escaped)


class Telegram:
    def __init__(self, token):
        self.base_url = f"https://api.telegram.org/bot{token}/"

    def call(self, method, payload=None, *, files=None, timeout=30):
        payload = payload or {}
        for attempt in range(3):
            try:
                if files:
                    response = requests.post(
                        self.base_url + method,
                        data=payload,
                        files=files,
                        timeout=timeout,
                    )
                else:
                    response = requests.post(
                        self.base_url + method, json=payload, timeout=timeout
                    )
            except requests.RequestException:
                if method.startswith("get"):
                    if attempt < 2:
                        time.sleep(attempt + 1)
                        continue
                    raise RuntimeError(
                        f"Telegram {method}: ошибка соединения"
                    ) from None
                raise DeliveryUnknown(
                    f"Telegram {method}: соединение прервалось; проверьте доставку перед повтором"
                ) from None
            try:
                data = response.json()
            except ValueError:
                if not method.startswith("get"):
                    raise DeliveryUnknown(
                        f"Telegram {method}: неизвестный результат доставки"
                    ) from None
                raise RuntimeError(
                    f"Telegram {method}: невалидный ответ сервера"
                ) from None
            if not isinstance(data, dict) or (
                data.get("ok") is True and "result" not in data
            ):
                if not method.startswith("get"):
                    raise DeliveryUnknown(
                        f"Telegram {method}: неизвестный результат доставки"
                    )
                raise RuntimeError(f"Telegram {method}: невалидный ответ сервера")
            if response.ok and data.get("ok") is True:
                return data["result"]
            code = data.get("error_code", response.status_code)
            delay = data.get("parameters", {}).get("retry_after", attempt + 1)
            if code == 429 and attempt < 2 and delay <= 60 and not files:
                time.sleep(delay)
                continue
            if code >= 500 and method.startswith("get") and attempt < 2:
                time.sleep(attempt + 1)
                continue
            if code >= 500 and not method.startswith("get"):
                raise DeliveryUnknown(
                    f"Telegram {method}: неизвестный результат доставки"
                )
            raise TelegramError(
                method, code, safe_error(data.get("description", "ошибка API"))
            )
        raise RuntimeError(f"Telegram {method}: исчерпаны попытки")

    def check_destination(self, channel_id, discussion_id=None):
        me = self.call("getMe")
        channel = self.call("getChat", {"chat_id": channel_id})
        if channel.get("type") != "channel":
            raise ValueError("TELEGRAM_CHAT_ID должен указывать на канал")
        linked_id = channel.get("linked_chat_id")
        if not linked_id:
            raise ValueError(
                "К каналу не привязана группа обсуждения; включите комментарии"
            )
        if discussion_id:
            configured = self.call("getChat", {"chat_id": discussion_id})
            if configured["id"] != linked_id:
                raise ValueError(
                    "DISCUSSION_CHAT_ID не совпадает с группой обсуждения канала"
                )
        channel_member = self.call(
            "getChatMember", {"chat_id": channel["id"], "user_id": me["id"]}
        )
        if channel_member.get("status") != "creator" and not channel_member.get(
            "can_post_messages"
        ):
            raise ValueError("Дайте боту право публикации сообщений в канале")
        member = self.call("getChatMember", {"chat_id": linked_id, "user_id": me["id"]})
        if member.get("status") not in ("administrator", "creator"):
            raise ValueError(
                "Добавьте бота администратором группы обсуждения для получения копий постов"
            )
        if self.call("getWebhookInfo").get("url"):
            raise ValueError(
                "У бота включён webhook; getUpdates для комментариев недоступен"
            )
        return channel["id"], linked_id, me

    def updates(self, offset=None, timeout=0):
        payload = {
            "limit": 100,
            "timeout": timeout,
            "allowed_updates": ["message", "channel_post"],
        }
        if offset is not None:
            payload["offset"] = offset
        return self.call("getUpdates", payload, timeout=timeout + 15)

    def drain_updates(self):
        offset = None
        while True:
            batch = self.updates(offset)
            if not batch:
                return offset
            offset = max(update["update_id"] for update in batch) + 1

    def post(self, channel_id, photo_urls, text):
        if not isinstance(photo_urls, list) or not 1 <= len(photo_urls) <= 3:
            raise ValueError("Каждому посту нужны 1–3 изображения")
        if len(set(photo_urls)) != len(photo_urls):
            raise ValueError("Изображения поста должны отличаться")
        formatted = telegram_html(text)
        if len(re.sub(r"<[^>]+>", "", formatted).encode("utf-16-le")) // 2 > 1024:
            raise ValueError("Подпись превышает лимит Telegram")
        # Download and validate every photo before sending anything to Telegram.
        pictures = [image_bytes(url) for url in photo_urls]
        if len(pictures) == 1:
            message = self.call(
                "sendPhoto",
                {
                    "chat_id": channel_id,
                    "caption": formatted,
                    "parse_mode": "HTML",
                },
                files={"photo": ("photo.jpg", pictures[0])},
                timeout=60,
            )
            self.post_message_ids = [self.message_id(message)]
        else:
            media = [
                {"type": "photo", "media": f"attach://photo{index}"}
                for index in range(len(pictures))
            ]
            media[0].update(caption=formatted, parse_mode="HTML")
            messages = self.call(
                "sendMediaGroup",
                {"chat_id": channel_id, "media": json.dumps(media)},
                files={
                    f"photo{index}": (f"photo{index}.jpg", picture)
                    for index, picture in enumerate(pictures)
                },
                timeout=90,
            )
            if not isinstance(messages, list) or len(messages) != len(pictures):
                raise DeliveryUnknown("Telegram не подтвердил весь альбом")
            self.post_message_ids = [self.message_id(message) for message in messages]
        return self.post_message_ids[0]

    @staticmethod
    def message_id(message):
        msg_id = message.get("message_id")
        if not isinstance(msg_id, int) or msg_id <= 0:
            raise DeliveryUnknown("Telegram не вернул ID опубликованного сообщения")
        return msg_id

    def discussion_message(
        self, channel_id, discussion_id, channel_msg_id, offset=None, wait=60
    ):
        deadline = time.monotonic() + wait
        while time.monotonic() < deadline:
            batch = self.updates(
                offset, timeout=min(10, max(1, int(deadline - time.monotonic())))
            )
            for update in batch:
                message = update.get("message", {})
                origin = message.get("forward_origin", {})
                if (
                    message.get("chat", {}).get("id") == discussion_id
                    and message.get("is_automatic_forward") is True
                    and origin.get("type") == "channel"
                    and origin.get("chat", {}).get("id") == channel_id
                    and origin.get("message_id") == channel_msg_id
                ):
                    return self.message_id(message)
            if batch:
                offset = max(update["update_id"] for update in batch) + 1
        raise RuntimeError(
            "Не получена копия поста в обсуждении. Словарь не отправлен; повторный запуск продолжит этот пост. Проверьте права бота и авто-пересылку."
        )

    def comment(self, discussion_id, root_id, dictionary, source_url=None):
        formatted = telegram_html(dictionary)
        if source_url:
            if not valid_url(source_url):
                raise ValueError("Некорректная ссылка на источник")
            formatted += (
                f'\n\n<a href="{html.escape(source_url, quote=True)}">Источник</a>'
            )
        message = self.call(
            "sendMessage",
            {
                "chat_id": discussion_id,
                "text": formatted,
                "parse_mode": "HTML",
                "reply_parameters": {
                    "message_id": root_id,
                    "allow_sending_without_reply": False,
                },
                "link_preview_options": {"is_disabled": True},
            },
        )
        comment_id = self.message_id(message)
        if message.get("reply_to_message", {}).get("message_id") != root_id:
            raise DeliveryUnknown("Telegram не подтвердил привязку словаря к посту")
        print(
            f"Словарь опубликован в комментариях: сообщение {comment_id}, пост обсуждения {root_id}"
        )
        return comment_id
