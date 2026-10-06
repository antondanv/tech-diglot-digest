import io
import json
import unittest
from datetime import datetime, timedelta, timezone
from types import SimpleNamespace
from unittest.mock import Mock, patch

from bs4 import BeautifulSoup
from PIL import Image

import article_media
import news_search


def photo_bytes(color="blue", size=(640, 480)):
    buffer = io.BytesIO()
    Image.new("RGB", size, color=color).save(buffer, format="JPEG")
    return buffer.getvalue()


class MediaTests(unittest.TestCase):
    def test_google_news_placeholder_and_logo_are_rejected(self):
        for url in (
            "https://news.google.com/photo.jpg",
            "https://lh3.gstatic.com/image.png",
            "https://example.com/assets/logo.jpg",
            "data:image/png;base64,AAA",
        ):
            self.assertFalse(article_media.image_candidate(url))

    @patch(
        "article_media.gnewsdecoder",
        return_value={"success": True, "decoded_url": "https://publisher.test/story"},
    )
    def test_google_news_resolves_to_publisher(self, decode):
        self.assertEqual(
            article_media.publisher_url("https://news.google.com/rss/articles/ABC"),
            "https://publisher.test/story",
        )

    @patch("article_media.gnewsdecoder", return_value={"success": False})
    def test_failed_decoder_does_not_use_google_thumbnail(self, decode):
        with self.assertRaises(ValueError):
            article_media.publisher_url("https://news.google.com/rss/articles/ABC")

    def test_images_stay_inside_article_and_ignore_related_items(self):
        soup = BeautifulSoup(
            """
            <meta content="/cover.jpg" property="og:image">
            <article><img src="/body.jpg"><aside class="related"><img src="/unrelated.jpg"></aside>
            <div class="slider"><img src="/slider.jpg"></div>
            <a href="/different-story"><img src="/other-story.jpg"></a></article>
            <div class="sidebar"><img src="/outside.jpg"></div>
        """,
            "html.parser",
        )
        self.assertEqual(
            article_media.article_image_urls(soup, "https://example.com/story"),
            ["https://example.com/cover.jpg", "https://example.com/body.jpg"],
        )

    @patch("article_media.requests.get")
    @patch("article_media.image_bytes")
    def test_resized_photo_versions_do_not_fill_album(self, image, get):
        get.return_value = SimpleNamespace(
            content=b'<meta property="og:image" content="https://static.dw.com/image/123_6.jpg"><article><img src="https://static.dw.com/image/123_804.jpg"></article>',
            url="https://example.com/story",
            raise_for_status=lambda: None,
        )
        image.return_value = photo_bytes()
        media = article_media.inspect_article("https://example.com/story")
        self.assertEqual(media["image_urls"], ["https://static.dw.com/image/123_6.jpg"])
        image.assert_called_once()

    @patch("article_media.requests.get")
    @patch("article_media.image_bytes", return_value=photo_bytes())
    def test_repeated_photo_in_history_is_rejected(self, image, get):
        get.return_value = SimpleNamespace(
            content=b'<meta property="og:image" content="/photo.jpg">',
            url="https://example.com/story",
            raise_for_status=lambda: None,
        )
        digest = article_media.hashlib.sha256(photo_bytes()).hexdigest()
        with self.assertRaises(ValueError):
            article_media.inspect_article("https://example.com/story", {digest})

    @patch("article_media.requests.get")
    def test_tiny_tracking_image_is_rejected(self, get):
        response = Mock()
        response.__enter__ = Mock(return_value=response)
        response.__exit__ = Mock(return_value=False)
        response.iter_content.return_value = [photo_bytes(size=(1, 1))]
        get.return_value = response
        with self.assertRaises(ValueError):
            article_media.image_bytes("https://example.com/pixel.jpg")


class GroundingTests(unittest.TestCase):
    def setUp(self):
        self.now = datetime(2026, 10, 6, 12, tzinfo=timezone.utc)
        self.stories = [
            {
                "title": f"Новость {index}",
                "summary": "Подтверждённый факт",
                "source_url": f"https://example.com/news/{index}",
                "published_at": (self.now - timedelta(hours=2)).isoformat(),
            }
            for index in range(3)
        ]
        self.metadata = SimpleNamespace(
            web_search_queries=["technology news October 6 2026"],
            grounding_chunks=[
                SimpleNamespace(web=SimpleNamespace(uri=story["source_url"]))
                for story in self.stories
            ],
        )

    def response(self):
        return SimpleNamespace(
            text=json.dumps({"stories": self.stories}),
            candidates=[SimpleNamespace(grounding_metadata=self.metadata)],
        )

    def test_grounded_sources_and_dates_are_kept(self):
        pool = news_search.grounded_pool(self.response(), self.now)
        self.assertEqual(len(pool), 3)
        self.assertEqual(pool[0]["grounding_url"], self.stories[0]["source_url"])
        self.assertEqual(pool[0]["search_queries"], self.metadata.web_search_queries)

    def test_answer_without_executed_search_is_rejected(self):
        self.metadata.web_search_queries = []
        with self.assertRaises(ValueError):
            news_search.grounded_pool(self.response(), self.now)

    def test_fabricated_url_is_rejected_even_with_valid_json(self):
        self.stories[0]["source_url"] = "https://invented.test/news"
        with self.assertRaises(ValueError):
            news_search.grounded_pool(self.response(), self.now)

    def test_old_news_is_not_accepted_as_fresh(self):
        self.stories[0]["published_at"] = (self.now - timedelta(days=7)).isoformat()
        with self.assertRaises(ValueError):
            news_search.grounded_pool(self.response(), self.now)

    def test_duplicate_story_does_not_count_as_three_news(self):
        self.stories[1] = self.stories[0]
        with self.assertRaises(ValueError):
            news_search.grounded_pool(self.response(), self.now)

    @patch("news_search.grounded_pool")
    @patch.dict("os.environ", {"GEMINI_SEARCH_MODELS": "test-model"})
    def test_search_tool_is_enabled_independently_of_editor(self, pool):
        client = Mock()
        pool.return_value = [{"link": "https://example.com"}] * 3
        news_search.search_news(client, [])
        config = client.models.generate_content.call_args.kwargs["config"]
        self.assertIsNotNone(config.tools[0].google_search)
        self.assertIsNone(config.response_schema)


if __name__ == "__main__":
    unittest.main()
