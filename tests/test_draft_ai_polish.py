import unittest

from ui import _draft_html_links, _draft_links, _draft_plain_text, _merge_draft_polished_text


class DraftAiPolishTests(unittest.TestCase):
    def test_preserves_real_html_links_after_polish(self):
        original = 'Кольца по акции\n<a href="https://www.wildberries.ru/catalog/123/detail.aspx">ссылка на товар</a>'
        content, parse_mode = _merge_draft_polished_text(
            "Набор колец для образа на каждый день.\n\nhttps://evil.example/item",
            original,
        )

        self.assertEqual(parse_mode, "HTML")
        self.assertIn('<a href="https://www.wildberries.ru/catalog/123/detail.aspx">Смотреть на Wildberries</a>', content)
        self.assertIn("Набор колец", content)
        self.assertNotIn("evil.example", content)

    def test_extracts_plain_text_from_html(self):
        text = _draft_plain_text('Привет <b>мир</b><br><a href="https://example.com">сюда</a>')

        self.assertEqual(text, "Привет мир\nсюда")

    def test_ignores_non_http_links(self):
        links = _draft_html_links('<a href="tg://resolve?domain=test">канал</a>')

        self.assertEqual(links, [])

    def test_plain_url_is_normalized_to_html_link(self):
        original = "Серьги за 904₽\nhttps://www.wildberries.ru/catalog/230412229/detail.aspx?size=363887871"
        content, parse_mode = _merge_draft_polished_text(
            "Серьги за 904₽\nРеально крутые или мимо?\n🔗 ссылка на товар",
            original,
        )

        self.assertEqual(parse_mode, "HTML")
        self.assertIn('<a href="https://www.wildberries.ru/catalog/230412229/detail.aspx?size=363887871">Смотреть на Wildberries</a>', content)
        self.assertNotIn("https://www.wildberries.ru/catalog/230412229/detail.aspx?size=363887871\n", content)
        self.assertNotIn("🔗 ссылка на товар", content)

    def test_duplicate_urls_are_deduplicated(self):
        url = "https://www.wildberries.ru/catalog/230412229/detail.aspx"
        original = f'<a href="{url}">ссылка</a>\n{url}\n{url}'

        links = _draft_links(original)
        content, _ = _merge_draft_polished_text("Текст", original)

        self.assertEqual(links, [(url, "Смотреть на Wildberries")])
        self.assertEqual(content.count(f'href="{url}"'), 1)

    def test_markdown_link_from_llm_is_removed_and_original_html_restored(self):
        original = '<a href="https://www.wildberries.ru/catalog/1/detail.aspx">товар</a>'
        content, parse_mode = _merge_draft_polished_text(
            "Новый текст\n[ссылка на товар](https://www.wildberries.ru/catalog/2/detail.aspx)",
            original,
        )

        self.assertEqual(parse_mode, "HTML")
        self.assertIn('href="https://www.wildberries.ru/catalog/1/detail.aspx"', content)
        self.assertNotIn("catalog/2", content)

    def test_trailing_link_phrase_is_removed_before_restoring_link(self):
        original = '<a href="https://aliexpress.ru/item/1.html">ссылка</a>'
        content, parse_mode = _merge_draft_polished_text(
            "Поймать его можно на Aliexpress по ссылке",
            original,
        )

        self.assertEqual(parse_mode, "HTML")
        self.assertIn("Поймать его можно на Aliexpress", content)
        self.assertNotIn("по ссылке", content)
        self.assertIn('<a href="https://aliexpress.ru/item/1.html">Смотреть на Aliexpress</a>', content)


if __name__ == "__main__":
    unittest.main()
