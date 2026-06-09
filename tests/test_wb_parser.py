import json
import unittest
from unittest.mock import patch

from wb_parser import WBParser


class _FakeResponse:
    status = 200

    def __enter__(self):
        return self

    def __exit__(self, exc_type, exc, tb):
        return False

    def getcode(self):
        return 200

    def read(self):
        return json.dumps(
            {
                "products": [
                    {
                        "id": 159177664,
                        "brand": "MegaYun",
                        "name": "Светлячок для ночной рыбалки",
                        "reviewRating": 4.9,
                        "feedbacks": 2399,
                        "sizes": [
                            {"price": {"product": 13300, "basic": 30000}},
                        ],
                    }
                ]
            }
        ).encode("utf-8")


class _FakeOpener:
    def __init__(self):
        self.request = None

    def open(self, request, timeout=0):
        self.request = request
        return _FakeResponse()


class WBParserTests(unittest.TestCase):
    def test_fetch_batch_sync_uses_browser_headers_and_keeps_image_url(self):
        parser = WBParser()
        opener = _FakeOpener()

        with patch("urllib.request.build_opener", return_value=opener):
            posts = parser._fetch_batch_sync([159177664])

        self.assertEqual(len(posts), 1)
        self.assertEqual(posts[0]["wb_article"], "159177664")
        self.assertEqual(posts[0]["parse_mode"], "HTML")
        self.assertIn("wbbasket.ru", posts[0]["image_url"])
        self.assertEqual(opener.request.headers["User-agent"], parser.HEADERS["User-Agent"])
        self.assertIn("nm=159177664", opener.request.full_url)


if __name__ == "__main__":
    unittest.main()
