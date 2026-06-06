import unittest

from ui import _draft_html_links, _draft_plain_text, _merge_draft_polished_text


class DraftAiPolishTests(unittest.TestCase):
    def test_preserves_real_html_links_after_polish(self):
        original = 'Кольца по акции\n<a href="https://www.wildberries.ru/catalog/123/detail.aspx">ссылка на товар</a>'
        content, parse_mode = _merge_draft_polished_text("Набор колец для образа на каждый день.", original)

        self.assertEqual(parse_mode, "HTML")
        self.assertIn('<a href="https://www.wildberries.ru/catalog/123/detail.aspx">ссылка на товар</a>', content)
        self.assertIn("Набор колец", content)

    def test_extracts_plain_text_from_html(self):
        text = _draft_plain_text('Привет <b>мир</b><br><a href="https://example.com">сюда</a>')

        self.assertEqual(text, "Привет мир\nсюда")

    def test_ignores_non_http_links(self):
        links = _draft_html_links('<a href="tg://resolve?domain=test">канал</a>')

        self.assertEqual(links, [])


if __name__ == "__main__":
    unittest.main()
