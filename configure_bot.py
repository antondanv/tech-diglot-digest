"""Apply bot, channel and discussion group profiles from checked-in assets."""

import argparse
import json
import sys

from bot import ROOT, load_local_env, required_env
from telegram_api import Telegram, safe_error


def configure_bot(telegram, profile):
    for language in ("", "ru"):
        for field, suffix in (
            ("name", "Name"),
            ("description", "Description"),
            ("short_description", "ShortDescription"),
        ):
            telegram.call(
                "setMy" + suffix, {field: profile[field], "language_code": language}
            )
            result = telegram.call("getMy" + suffix, {"language_code": language})
            if result.get(field) != profile[field]:
                raise RuntimeError(f"Telegram не подтвердил изменение {field}")
    with (ROOT / "assets/avatar.jpg").open("rb") as file:
        telegram.call(
            "setMyProfilePhoto",
            {"photo": json.dumps({"type": "static", "photo": "attach://avatar"})},
            files={"avatar": ("avatar.jpg", file, "image/jpeg")},
        )
    me = telegram.call("getMe")
    photos = telegram.call("getUserProfilePhotos", {"user_id": me["id"], "limit": 1})
    if not photos.get("total_count"):
        raise RuntimeError("Telegram не вернул установленную аватарку")
    print(f"Имя, оба описания и аватарка обновлены: @{me['username']}")


def configure_chats(telegram, profile):
    channel = telegram.call("getChat", {"chat_id": required_env("TELEGRAM_CHAT_ID")})
    if channel.get("type") != "channel" or not channel.get("linked_chat_id"):
        raise ValueError("Нужен канал с привязанной группой обсуждения")
    for chat_id, description, avatar in (
        (channel["id"], profile["channel_description"], "avatar.jpg"),
        (
            channel["linked_chat_id"],
            profile["discussion_description"],
            "discussion-avatar.jpg",
        ),
    ):
        telegram.call(
            "setChatDescription", {"chat_id": chat_id, "description": description}
        )
        with (ROOT / "assets" / avatar).open("rb") as file:
            telegram.call(
                "setChatPhoto",
                {"chat_id": chat_id},
                files={"photo": (avatar, file, "image/jpeg")},
            )
        updated = telegram.call("getChat", {"chat_id": chat_id})
        if updated.get("description") != description or not updated.get("photo"):
            raise RuntimeError("Telegram не подтвердил обновление профиля чата")
        print(f"Аватарка и описание установлены: {updated['title']}")
        print(updated["description"])


def main():
    load_local_env()
    parser = argparse.ArgumentParser()
    parser.add_argument("--chats-only", action="store_true")
    args = parser.parse_args()
    telegram = Telegram(required_env("TELEGRAM_BOT_TOKEN"))
    profile = json.loads((ROOT / "assets/profile.json").read_text(encoding="utf-8"))
    for field, limit in (
        ("name", 64),
        ("description", 512),
        ("short_description", 120),
        ("channel_description", 255),
        ("discussion_description", 255),
    ):
        if not profile[field].strip() or len(profile[field]) > limit:
            raise ValueError(f"Некорректная длина {field}")
    if not args.chats_only:
        configure_bot(telegram, profile)
    configure_chats(telegram, profile)


if __name__ == "__main__":
    try:
        main()
    except Exception as error:  # noqa: BLE001 - redact SDK/network errors before logging
        print(f"Ошибка: {safe_error(error)}", file=sys.stderr)
        sys.exit(1)
