import sys
import types
import unittest

import ai_client
import content_generator
import reference_importer
import wb_parser


class FakeBuffer:
    def __init__(self):
        self.posts = []

    def add(self, post):
        self.posts.append(post)
        return f"post-{len(self.posts)}"

    def get_level(self, channel_id):
        return len(self.posts)


PLAIN_CHANNEL = {
    "channel_id": "@plain",
    "topic": "личные финансы",
    "tone": "полезный",
}


class ImportPipelineSafetyTest(unittest.IsolatedAsyncioTestCase):
    async def test_reference_rephrase_refusal_falls_back_to_original(self):
        fake_buffer = FakeBuffer()
        original_buffer = reference_importer.buffer
        original_rephrase = ai_client.rephrase_text

        async def fake_rephrase(raw, channel):
            return "Извините, но я не могу написать такой пост."

        reference_importer.buffer = fake_buffer
        ai_client.rephrase_text = fake_rephrase
        try:
            result = await reference_importer._store_reference_post(
                PLAIN_CHANNEL,
                "@plain",
                "@donor",
                {"id": 1, "text": "Как вести семейный бюджет без лишнего стресса"},
                do_rephrase=True,
            )
        finally:
            reference_importer.buffer = original_buffer
            ai_client.rephrase_text = original_rephrase

        self.assertEqual(result, [])
        self.assertEqual(len(fake_buffer.posts), 1)
        self.assertIn("Как вести", fake_buffer.posts[0]["content"])

    async def test_reference_blocked_donor_does_not_call_llm(self):
        fake_buffer = FakeBuffer()
        original_buffer = reference_importer.buffer
        original_rephrase = ai_client.rephrase_text
        called = {"llm": False}

        async def fake_rephrase(raw, channel):
            called["llm"] = True
            return raw

        reference_importer.buffer = fake_buffer
        ai_client.rephrase_text = fake_rephrase
        try:
            result = await reference_importer._store_reference_post(
                PLAIN_CHANNEL,
                "@plain",
                "@donor",
                {"id": 2, "text": "порно и наркотики"},
                do_rephrase=True,
            )
        finally:
            reference_importer.buffer = original_buffer
            ai_client.rephrase_text = original_rephrase

        self.assertIsNone(result)
        self.assertFalse(called["llm"])
        self.assertEqual(fake_buffer.posts, [])

    async def test_normal_reference_post_is_buffered(self):
        fake_buffer = FakeBuffer()
        original_buffer = reference_importer.buffer

        reference_importer.buffer = fake_buffer
        try:
            result = await reference_importer._store_reference_post(
                PLAIN_CHANNEL,
                "@plain",
                "@donor",
                {"id": 3, "text": "Как вести семейный бюджет без лишнего стресса"},
                do_rephrase=False,
            )
        finally:
            reference_importer.buffer = original_buffer

        self.assertEqual(result, [])
        self.assertEqual(len(fake_buffer.posts), 1)
        self.assertEqual(fake_buffer.posts[0]["format"], "reference")

    async def test_marketplace_refusal_post_is_not_buffered(self):
        fake_buffer = FakeBuffer()
        original_buffer = content_generator.buffer
        original_wb = sys.modules.get("wb_parser")

        class FakeParser:
            async def generate_posts(self, channel, count):
                return [{
                    "content": "К сожалению, я не могу создать такой контент.",
                    "wb_article": "123",
                    "wb_category": "test",
                }]

        sys.modules["wb_parser"] = types.SimpleNamespace(wb_parser=FakeParser())
        content_generator.buffer = fake_buffer
        gen = content_generator.ContentGenerator()
        original_dup = gen._wb_article_in_buffer
        gen._wb_article_in_buffer = lambda channel_id, article: False
        try:
            result = await gen._run_marketplace(
                {"channel_id": "@shop", "topic": "товары для дома", "channel_type": "marketplace"},
                1,
            )
        finally:
            gen._wb_article_in_buffer = original_dup
            content_generator.buffer = original_buffer
            if original_wb is None:
                sys.modules.pop("wb_parser", None)
            else:
                sys.modules["wb_parser"] = original_wb

        self.assertEqual(result["generated"], 0)
        self.assertEqual(fake_buffer.posts, [])

    async def test_normal_marketplace_post_is_buffered(self):
        fake_buffer = FakeBuffer()
        original_buffer = content_generator.buffer
        original_wb = sys.modules.get("wb_parser")

        class FakeParser:
            async def generate_posts(self, channel, count):
                return [{
                    "content": (
                        "Полезная находка для дома: компактный органайзер помогает держать вещи под рукой.\n\n"
                        '🔗 <a href="https://www.wildberries.ru/catalog/456/detail.aspx">Смотреть на Wildberries</a>'
                    ),
                    "wb_article": "456",
                    "wb_category": "home",
                    "parse_mode": "HTML",
                }]

        sys.modules["wb_parser"] = types.SimpleNamespace(wb_parser=FakeParser())
        content_generator.buffer = fake_buffer
        gen = content_generator.ContentGenerator()
        original_dup = gen._wb_article_in_buffer
        gen._wb_article_in_buffer = lambda channel_id, article: False
        try:
            result = await gen._run_marketplace(
                {"channel_id": "@shop", "topic": "товары для дома", "channel_type": "marketplace"},
                1,
            )
        finally:
            gen._wb_article_in_buffer = original_dup
            content_generator.buffer = original_buffer
            if original_wb is None:
                sys.modules.pop("wb_parser", None)
            else:
                sys.modules["wb_parser"] = original_wb

        self.assertEqual(result["generated"], 1)
        self.assertEqual(len(fake_buffer.posts), 1)
        self.assertEqual(fake_buffer.posts[0]["format"], "wb_product")

    async def test_marketplace_post_without_href_is_not_buffered(self):
        fake_buffer = FakeBuffer()
        original_buffer = content_generator.buffer
        original_wb = sys.modules.get("wb_parser")

        class FakeParser:
            async def generate_posts(self, channel, count):
                return [{
                    "content": "Полезная находка для дома. Ссылка: смотрите в описании.",
                    "wb_article": "789",
                    "wb_category": "home",
                    "parse_mode": "HTML",
                }]

        sys.modules["wb_parser"] = types.SimpleNamespace(wb_parser=FakeParser())
        content_generator.buffer = fake_buffer
        gen = content_generator.ContentGenerator()
        original_dup = gen._wb_article_in_buffer
        gen._wb_article_in_buffer = lambda channel_id, article: False
        try:
            result = await gen._run_marketplace(
                {"channel_id": "@shop", "topic": "товары для дома", "channel_type": "marketplace"},
                1,
            )
        finally:
            gen._wb_article_in_buffer = original_dup
            content_generator.buffer = original_buffer
            if original_wb is None:
                sys.modules.pop("wb_parser", None)
            else:
                sys.modules["wb_parser"] = original_wb

        self.assertEqual(result["generated"], 0)
        self.assertEqual(fake_buffer.posts, [])

    async def test_marketplace_empty_parser_returns_wb_reason(self):
        fake_buffer = FakeBuffer()
        original_buffer = content_generator.buffer
        original_wb = sys.modules.get("wb_parser")

        class FakeParser:
            async def generate_posts(self, channel, count):
                return []

        sys.modules["wb_parser"] = types.SimpleNamespace(wb_parser=FakeParser())
        content_generator.buffer = fake_buffer
        try:
            result = await content_generator.ContentGenerator()._run_marketplace(
                {"channel_id": "@shop", "topic": "товары", "channel_type": "marketplace"},
                3,
            )
        finally:
            content_generator.buffer = original_buffer
            if original_wb is None:
                sys.modules.pop("wb_parser", None)
            else:
                sys.modules["wb_parser"] = original_wb

        self.assertEqual(result["generated"], 0)
        self.assertIn("WB-парсер", result["reason"])

    async def test_wb_parser_falls_back_to_cache_when_search_cards_empty(self):
        class FakeWBParser(wb_parser.WBParser):
            def __init__(self):
                super().__init__()
                self.fetch_calls = []

            async def _discover_articles(self, channel, count):
                return [101, 102]

            def _pick_articles(self, channel, count):
                return [201]

            async def _fetch_posts(self, article_ids, count):
                self.fetch_calls.append(list(article_ids))
                if article_ids == [101, 102]:
                    return []
                return [{"content": "post", "wb_article": "201"}]

        parser = FakeWBParser()
        posts = await parser.generate_posts({"channel_id": "@shop"}, 1)

        self.assertEqual(parser.fetch_calls, [[101, 102], [201]])
        self.assertEqual(len(posts), 1)
        self.assertEqual(posts[0]["wb_article"], "201")


if __name__ == "__main__":
    unittest.main()
