import copy
import json
import os
import tempfile
import unittest
from pathlib import Path
from types import SimpleNamespace
from unittest.mock import Mock, patch

import requests

import bot
from telegram_api import (
    DeliveryUnknown,
    Telegram,
    TelegramError,
    safe_error,
    telegram_html,
)


def example_post():
    return {
        "headline": "Гаджет помогает учиться каждый день",
        "topic": "Новый гаджет",
        "text": "Устройство помогает people читать, learn и получать news.",
        "dictionary": [
            {"word": "people", "transcription": "ˈpiːpl", "translation": "люди"},
            {"word": "learn", "transcription": "lɜːn", "translation": "учиться"},
            {"word": "news", "transcription": "njuːz", "translation": "новости"},
        ],
        "source_url": "https://example.com/article",
        "image_urls": ["https://example.com/photo.jpg"],
    }


class ContentTests(unittest.TestCase):
    def test_draft_insertion_guarantees_all_glossary_words(self):
        post = example_post()
        post["text"] = "Люди читают новости и учатся каждый день."
        for item, fragment in zip(post["dictionary"], ("Люди", "учатся", "новости")):
            item["russian_fragment"] = fragment
        result = bot.render_draft(bot.DraftPost.model_validate(post))
        self.assertEqual(result["text"], "People читают news и learn каждый день.")
        self.assertEqual(
            {word.lower() for word in bot.re.findall(r"[A-Za-z]+", result["text"])},
            {item["word"] for item in result["dictionary"]},
        )

    def test_draft_rejects_missing_russian_fragment(self):
        post = example_post()
        post["text"] = "Люди читают новости и учатся каждый день."
        for item in post["dictionary"]:
            item["russian_fragment"] = "отсутствует"
        with self.assertRaises(ValueError):
            bot.render_draft(bot.DraftPost.model_validate(post))

    def test_draft_rejects_overlapping_replacements(self):
        post = example_post()
        post["text"] = "Люди читают новости и учатся каждый день."
        for item, fragment in zip(
            post["dictionary"], ("Люди читают", "читают", "новости")
        ):
            item["russian_fragment"] = fragment
        with self.assertRaises(ValueError):
            bot.render_draft(bot.DraftPost.model_validate(post))

    def test_duplicate_dictionary_entry_is_rejected(self):
        post = example_post()
        post["dictionary"].append(copy.deepcopy(post["dictionary"][0]))
        with self.assertRaises(ValueError):
            bot.validate_post(post)

    def test_valid_post(self):
        self.assertEqual(bot.validate_post(example_post())["topic"], "Новый гаджет")

    def test_legacy_dictionary_migration(self):
        post = example_post()
        post["dictionary"] = bot.dictionary_text(post)
        self.assertEqual(len(bot.validate_post(post)["dictionary"]), 3)

    def test_two_words_are_rejected(self):
        post = example_post()
        post["text"] = "people получают news."
        post["dictionary"] = [post["dictionary"][0], post["dictionary"][2]]
        with self.assertRaises(ValueError):
            bot.validate_post(post)

    def test_mismatched_translation_word(self):
        post = example_post()
        post["dictionary"][0]["word"] = "person"
        with self.assertRaises(ValueError):
            bot.validate_post(post)

    def test_duplicate_word_rejected(self):
        post = example_post()
        post["text"] = "people, people, news"
        with self.assertRaises(ValueError):
            bot.validate_post(post)

    def test_oversize_and_links_are_rejected(self):
        for suffix in ("x" * 850, " https://example.com"):
            post = example_post()
            post["text"] += suffix
            with self.assertRaises(ValueError):
                bot.validate_post(post)

    def test_fabricated_source_is_rejected(self):
        with self.assertRaises(ValueError):
            bot.validate_post(example_post(), {"https://example.com/different"})

    def test_html_escapes_text_and_keeps_ipa(self):
        self.assertEqual(
            telegram_html("**learn** [lɜːn] — <учиться> & расти"),
            "<b>learn</b> [lɜːn] — &lt;учиться&gt; &amp; расти",
        )

    def test_broken_queue_does_not_become_an_empty_queue(self):
        with tempfile.TemporaryDirectory() as directory:
            path = Path(directory) / "queue.json"
            path.write_text("{invalid", encoding="utf-8")
            with self.assertRaises(ValueError):
                bot.read_json(path)
            self.assertEqual(path.read_text(), "{invalid")

    @patch.dict(os.environ, {"GEMINI_MODELS": ""})
    def test_empty_model_override_uses_defaults(self):
        client = Mock()
        post = {
            key: value for key, value in example_post().items() if key != "image_url"
        }
        client.models.generate_content.return_value = SimpleNamespace(
            text=json.dumps(post)
        )
        result = bot.generate_content(
            client,
            "prompt",
            bot.Post,
            lambda value: bot.validate_post(value.model_dump()),
        )
        self.assertEqual(result["topic"], post["topic"])
        self.assertEqual(
            client.models.generate_content.call_args.kwargs["model"],
            "gemini-3.1-flash-lite",
        )

    def test_bad_model_output_is_retried_before_return(self):
        client = Mock()
        post = example_post()
        client.models.generate_content.side_effect = [
            SimpleNamespace(text="not json"),
            SimpleNamespace(text=json.dumps(post)),
        ]
        result = bot.generate_content(
            client,
            "prompt",
            bot.Post,
            lambda value: bot.validate_post(value.model_dump()),
        )
        self.assertEqual(len(result["dictionary"]), 3)
        self.assertEqual(client.models.generate_content.call_count, 2)

    @patch("bot.requests.get", side_effect=requests.Timeout)
    def test_unavailable_news_fails_instead_of_fabricating(self, get):
        with self.assertRaises(RuntimeError):
            bot.fetch_diverse_news_pool()


class TelegramTests(unittest.TestCase):
    def setUp(self):
        self.telegram = Telegram("dummy")

    def test_find_automatic_copy_with_different_group_id(self):
        def update(number, msg_id, **changes):
            message = {
                "message_id": msg_id,
                "chat": {"id": -200},
                "is_automatic_forward": True,
                "forward_origin": {
                    "type": "channel",
                    "chat": {"id": -100},
                    "message_id": 42,
                },
            }
            message.update(changes)
            return {"update_id": number, "message": message}

        self.telegram.updates = Mock(
            return_value=[
                update(10, 70, is_automatic_forward=False),
                update(11, 71, chat={"id": -300}),
                update(12, 900),
            ]
        )
        self.assertEqual(self.telegram.discussion_message(-100, -200, 42), 900)

    def test_dictionary_replies_to_group_copy_and_has_no_cross_chat_reply(self):
        self.telegram.call = Mock(
            return_value={"message_id": 901, "reply_to_message": {"message_id": 900}}
        )
        self.telegram.comment(-200, 900, "**learn** [lɜːn] — учиться")
        payload = self.telegram.call.call_args.args[1]
        self.assertEqual(payload["chat_id"], -200)
        self.assertEqual(
            payload["reply_parameters"],
            {"message_id": 900, "allow_sending_without_reply": False},
        )
        self.assertNotIn("chat_id", payload["reply_parameters"])

    def test_unconfirmed_thread_is_not_reported_as_success(self):
        self.telegram.call = Mock(return_value={"message_id": 901})
        with self.assertRaises(DeliveryUnknown):
            self.telegram.comment(-200, 900, "dictionary")

    def test_failed_comment_has_no_fallback_to_group_or_channel(self):
        self.telegram.call = Mock(
            side_effect=TelegramError("sendMessage", 400, "not found")
        )
        with self.assertRaises(TelegramError):
            self.telegram.comment(-200, 900, "dictionary")
        self.telegram.call.assert_called_once()

    @patch("telegram_api.requests.post")
    def test_http_200_with_ok_false_is_failure(self, post):
        post.return_value = SimpleNamespace(
            ok=True,
            status_code=200,
            json=lambda: {"ok": False, "error_code": 400, "description": "Bad Request"},
        )
        with self.assertRaises(TelegramError):
            self.telegram.call("sendMessage")

    @patch("telegram_api.requests.post")
    def test_write_ack_without_result_is_unknown_delivery(self, post):
        post.return_value = SimpleNamespace(
            ok=True, status_code=200, json=lambda: {"ok": True}
        )
        with self.assertRaises(DeliveryUnknown):
            self.telegram.call("sendMessage")
        post.assert_called_once()

    @patch("telegram_api.image_bytes", return_value=b"photo")
    @patch("telegram_api.requests.post", side_effect=requests.Timeout)
    def test_send_timeout_is_not_retried(self, post, image):
        with self.assertRaises(DeliveryUnknown):
            self.telegram.post(-100, ["https://example.com/photo.jpg"], "text")
        self.assertEqual(post.call_count, 1)

    @patch("telegram_api.image_bytes", return_value=b"photo")
    def test_rejected_photo_preserves_failure_without_text_fallback(self, image):
        self.telegram.call = Mock(
            side_effect=TelegramError("sendPhoto", 400, "wrong file")
        )
        with self.assertRaises(TelegramError):
            self.telegram.post(-100, ["https://example.com/photo.jpg"], "text")
        self.telegram.call.assert_called_once()

    @patch("telegram_api.image_bytes", side_effect=[b"first", b"second", b"third"])
    def test_album_has_one_caption_and_returns_its_first_message(self, image):
        self.telegram.call = Mock(
            return_value=[{"message_id": 42}, {"message_id": 43}, {"message_id": 44}]
        )
        result = self.telegram.post(
            -100,
            [
                "https://example.com/1.jpg",
                "https://example.com/2.jpg",
                "https://example.com/3.jpg",
            ],
            "**Заголовок**\n\nТекст",
        )
        self.assertEqual(result, 42)
        self.assertEqual(self.telegram.post_message_ids, [42, 43, 44])
        call = self.telegram.call.call_args
        self.assertEqual(call.args[0], "sendMediaGroup")
        media = json.loads(call.args[1]["media"])
        self.assertIn("<b>Заголовок</b>", media[0]["caption"])
        self.assertNotIn("caption", media[1])
        self.assertNotIn("caption", media[2])
        self.assertEqual(len(call.kwargs["files"]), 3)

    @patch("telegram_api.image_bytes", side_effect=[b"photo", ValueError("invalid")])
    def test_album_validates_all_photos_before_publishing(self, image):
        self.telegram.call = Mock()
        with self.assertRaises(ValueError):
            self.telegram.post(
                -100, ["https://example.com/1.jpg", "https://example.com/2.jpg"], "text"
            )
        self.telegram.call.assert_not_called()

    def test_post_requires_one_to_three_different_images(self):
        self.telegram.call = Mock()
        for pictures in ([], ["same", "same"], ["1", "2", "3", "4"]):
            with self.assertRaises(ValueError):
                self.telegram.post(-100, pictures, "text")
        self.telegram.call.assert_not_called()

    @patch.dict(
        os.environ,
        {"TELEGRAM_BOT_TOKEN": "secret-token", "GEMINI_API_KEY": "secret-key"},
    )
    def test_errors_redact_keys(self):
        text = safe_error(
            "secret-token secret-key https://api.telegram.org/bot123:ABC-DEF/getMe"
        )
        self.assertNotIn("secret-token", text)
        self.assertNotIn("secret-key", text)
        self.assertNotIn("123:ABC-DEF", text)

    def test_wrong_discussion_is_rejected_before_publishing(self):
        self.telegram.call = Mock(
            side_effect=[
                {"id": 1},
                {"id": -100, "type": "channel", "linked_chat_id": -200},
                {"id": -300},
            ]
        )
        with self.assertRaises(ValueError):
            self.telegram.check_destination(-100, -300)
        self.assertEqual(self.telegram.call.call_count, 3)


class PublishingTests(unittest.TestCase):
    def setUp(self):
        self.directory = tempfile.TemporaryDirectory()
        root = Path(self.directory.name)
        self.queue_path = root / "queue.json"
        self.history_path = root / "history.json"
        self.paths = patch.multiple(
            bot, QUEUE_FILE=self.queue_path, HISTORY_FILE=self.history_path
        )
        self.paths.start()
        self.addCleanup(self.paths.stop)
        self.addCleanup(self.directory.cleanup)
        self.telegram = Mock()
        self.telegram.drain_updates.return_value = 1
        self.telegram.post.return_value = 42
        self.telegram.discussion_message.return_value = 900
        self.telegram.comment.return_value = 901
        self.queue = [example_post()]
        self.history = []

    def publish(self):
        bot.publish_next(self.telegram, -100, -200, self.queue, self.history, Mock())

    def test_success_removes_head_after_comment_and_records_history(self):
        self.publish()
        self.assertEqual(bot.read_json(self.queue_path), [])
        self.assertEqual(bot.read_json(self.history_path)[0]["comment_message_id"], 901)

    def test_comment_failure_preserves_post_id_and_resume_does_not_repost(self):
        self.telegram.comment.side_effect = TelegramError("sendMessage", 400, "failed")
        with self.assertRaises(TelegramError):
            self.publish()
        state = bot.read_json(self.queue_path)[0]
        self.assertEqual(state["channel_message_id"], 42)
        self.assertEqual(state["discussion_message_id"], 900)
        self.telegram.comment.side_effect = None
        self.publish()
        self.telegram.post.assert_called_once()
        self.assertEqual(self.queue, [])

    def test_post_failure_keeps_unpublished_post(self):
        self.telegram.post.side_effect = TelegramError("sendMessage", 403, "forbidden")
        with self.assertRaises(TelegramError):
            self.publish()
        self.assertEqual(len(bot.read_json(self.queue_path)), 1)
        self.telegram.comment.assert_not_called()

    def test_unknown_post_delivery_blocks_automatic_duplicate(self):
        self.telegram.post.side_effect = DeliveryUnknown("timeout")
        with self.assertRaises(DeliveryUnknown):
            self.publish()
        self.assertTrue(bot.read_json(self.queue_path)[0]["post_delivery_unknown"])
        with self.assertRaises(RuntimeError):
            self.publish()
        self.telegram.post.assert_called_once()

    def test_unknown_comment_delivery_blocks_duplicate_comment(self):
        self.telegram.comment.side_effect = DeliveryUnknown("timeout")
        with self.assertRaises(DeliveryUnknown):
            self.publish()
        self.assertTrue(bot.read_json(self.queue_path)[0]["comment_delivery_unknown"])
        with self.assertRaises(RuntimeError):
            self.publish()
        self.telegram.comment.assert_called_once()

    def test_wait_failure_keeps_post_without_sending_dictionary(self):
        self.telegram.discussion_message.side_effect = RuntimeError("not forwarded")
        with self.assertRaises(RuntimeError):
            self.publish()
        self.assertEqual(bot.read_json(self.queue_path)[0]["channel_message_id"], 42)
        self.telegram.comment.assert_not_called()

    def test_already_finished_post_is_only_removed_from_queue(self):
        self.queue[0].update(
            channel_message_id=42, discussion_message_id=900, comment_message_id=901
        )
        self.history = [
            copy.deepcopy(
                {
                    key: self.queue[0][key]
                    for key in (
                        "topic",
                        "source_url",
                        "channel_message_id",
                        "discussion_message_id",
                        "comment_message_id",
                    )
                }
            )
        ]
        self.publish()
        self.telegram.post.assert_not_called()
        self.telegram.comment.assert_not_called()
        self.assertEqual(len(self.history), 1)


if __name__ == "__main__":
    unittest.main()
