import unittest

import image_fetcher


class ImageFetcherPolicyTest(unittest.IsolatedAsyncioTestCase):
    def test_cs2_context_skips_stock(self):
        self.assertTrue(image_fetcher._should_skip_stock_for_context(
            "Разбор матча CS2: почему команда забрала карту",
            "CS2 skins and esports",
            "@cs2skinss2025",
            ["cs2", "skins"],
        ))

    def test_csgo_context_skips_stock(self):
        self.assertTrue(image_fetcher._should_skip_stock_for_context(
            "IEM Cologne Major Pick'Em Challenge",
            "CS GO 2 | Only facts",
            "@csgo_only_facts",
            [],
        ))

    def test_generic_context_does_not_skip_stock(self):
        self.assertFalse(image_fetcher._should_skip_stock_for_context(
            "Как не забывать пить воду летом",
            "лайфстайл, здоровье",
            "Полезные советы",
            [],
        ))

    def test_pexels_picker_filters_real_sports_for_gaming_query(self):
        photos = [
            {
                "alt": "Rugby player running with a ball on green field",
                "src": {"large": "https://example.test/rugby.jpg"},
            },
            {
                "alt": "Badminton shuttlecock and racquet on blue background",
                "src": {"large": "https://example.test/badminton.jpg"},
            },
            {
                "alt": "Gamer playing on computer with keyboard and monitor",
                "src": {"large": "https://example.test/gaming.jpg"},
            },
        ]

        chosen = image_fetcher._pick_stock_item(
            photos,
            "counter strike 2 esports match analysis",
            lambda item: item.get("alt", ""),
            lambda item: (item.get("src") or {}).get("large"),
        )

        self.assertIsNotNone(chosen)
        self.assertEqual(chosen["src"]["large"], "https://example.test/gaming.jpg")

    async def test_build_query_anchors_cs2_context(self):
        original = image_fetcher._extract_visual_keywords

        async def fake_extract(post_text, channel_context=""):
            return "match analysis esports"

        image_fetcher._extract_visual_keywords = fake_extract
        try:
            query = await image_fetcher._build_english_query(
                "Появились разборы матчей CS2 с объяснением ключевых моментов",
                "CS2 skins and esports",
            )
        finally:
            image_fetcher._extract_visual_keywords = original

        self.assertIn("counter strike 2", query)
        self.assertIn("match analysis esports", query)


if __name__ == "__main__":
    unittest.main()
