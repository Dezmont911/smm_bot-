import unittest

from ui import _channel_search_haystack, _channel_search_terms


class ChannelSearchTests(unittest.TestCase):
    def test_tme_link_matches_channel_username(self):
        terms = _channel_search_terms("https://t.me/AnimeMarafone")
        haystack = _channel_search_haystack({
            "channel_id": "@AnimeMarafone",
            "name": "Аниме Марафон Все Серии",
        })

        self.assertTrue(any(term in value for term in terms for value in haystack))

    def test_at_username_matches_link_variants(self):
        terms = _channel_search_terms("@AnimeMarafone")

        self.assertIn("animemarafone", terms)
        self.assertIn("@animemarafone", terms)
        self.assertIn("https://t.me/animemarafone", terms)

    def test_title_fragment_still_matches_name(self):
        terms = _channel_search_terms("марафон")
        haystack = _channel_search_haystack({
            "channel_id": "@AnimeMarafone",
            "name": "Аниме Марафон Все Серии",
        })

        self.assertTrue(any(term in value for term in terms for value in haystack))


if __name__ == "__main__":
    unittest.main()
