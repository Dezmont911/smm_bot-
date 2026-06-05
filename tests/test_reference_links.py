"""
Регресс-тесты: партнёрские ссылки в референс-постах НЕ должны теряться, а мета-хвост
(«Пояснение: …») не должен попадать в пост.

Зачем: при рефакторинге генерации/safe-слоя легко случайно убрать переклейку ссылки
(_extract_links → CTA) или забыть срезать мета-хвост. Эти тесты краснеют, если так.
Гонять: py -m unittest tests.test_reference_links -v
"""

import os
# Фиктивные секреты до импортов: config._require() падает без них, а локально
# (вне VPS) реального ключа нет/он пустой. Ставим, только если пусто — настоящий
# непустой ключ не трогаем (рефраз в тесте замокан, реальный вызов не идёт).
for _k, _v in {"ANTHROPIC_API_KEY": "test-key", "BOT_TOKEN": "123:test", "ADMIN_CHAT_ID": "1"}.items():
    if not os.environ.get(_k):
        os.environ[_k] = _v

import asyncio
import unittest
from unittest import mock

import reference_importer as ri
from reference_importer import _extract_links
from ai_client import _clean_post_output, _looks_like_refusal


class ExtractLinksTest(unittest.TestCase):
    """_extract_links — фундамент переклейки: достаёт ссылки из HTML донора."""

    def test_plain_ali_link(self):
        html = 'Товар\n🛒 <a href="https://ali.click/abc?erid=X">ссылка</a>'
        self.assertEqual(_extract_links(html), [("https://ali.click/abc?erid=X", "ссылка")])

    def test_label_html_stripped(self):
        # ярлык внутри <a> может содержать теги — их надо вычистить
        html = '<a href="https://ali.click/x?erid=Y&amp;sub=1"><strong>ссылка</strong></a>'
        links = _extract_links(html)
        self.assertEqual(len(links), 1)
        self.assertTrue(links[0][0].startswith("https://ali.click/"))
        self.assertEqual(links[0][1], "ссылка")

    def test_dedup_and_http_only(self):
        html = ('<a href="https://market.yandex.ru/cc/1">Маркет</a> '
                '<a href="https://ali.click/2">Али</a> '
                '<a href="https://market.yandex.ru/cc/1">дубль</a> '
                '<a href="tg://resolve?domain=x">tg</a>')
        urls = [u for u, _ in _extract_links(html)]
        self.assertEqual(urls, ["https://market.yandex.ru/cc/1", "https://ali.click/2"])

    def test_no_links(self):
        self.assertEqual(_extract_links("просто текст"), [])
        self.assertEqual(_extract_links(""), [])


class MetaLeakTest(unittest.TestCase):
    """Мета-хвост и отказы не должны попадать в пост."""

    def test_clean_strips_poyasnenie_tail(self):
        post = ("Многофункциональный ключ для раковин — полезная вещь.\n\n"
                "🛒 Найти на Aliexpress\n\n"
                "---\n**Пояснение:** Я оставил суть поста, но переписал заголовок.")
        cleaned = _clean_post_output(post)
        self.assertNotIn("Пояснение", cleaned)
        self.assertNotIn("оставил суть", cleaned)
        self.assertIn("Многофункциональный ключ", cleaned)

    def test_refusal_flagged(self):
        self.assertTrue(_looks_like_refusal("Извините, но я не могу помочь с этим запросом."))

    def test_normal_post_not_flagged(self):
        self.assertFalse(_looks_like_refusal(
            "Многофункциональный ключ для раковин — вещь неожиданно полезная. Найти на Aliexpress."
        ))


class ReattachLinkInStoreTest(unittest.TestCase):
    """Главный регресс-тест: переписанный marketplace-референс сохраняет реальный <a href>.
    Имитируем потерю ссылки рефразом (модель вернула плоский текст) и проверяем, что
    _store_reference_post переклеил настоящую партнёрскую ссылку из text_html донора."""

    def test_rephrased_post_keeps_affiliate_link(self):
        channel = {"channel_id": "@test_mp", "name": "Тест", "topic": "товары с маркетплейсов",
                   "post_length": "100 слов", "channel_type": "marketplace"}
        p = {
            "id": 1,
            "text": "Ключ для раковин",
            "text_html": 'Ключ для раковин\n🛒 Заказать на Aliexpress - '
                         '<a href="https://ali.click/abc?erid=XYZ">ссылка</a>',
            "media_kind": None,
            "match_user": "donor",
            "match_id": 1,
        }
        captured = {}

        async def fake_rephrase(text, ch):
            # модель «потеряла» ссылку и написала заглушку — как в реальном баге
            return "Многофункциональный ключ для раковин — полезная вещь. 🛒 Найти на Aliexpress — ссылка"

        def fake_add(record):
            captured.update(record)

        with mock.patch("ai_client.rephrase_text", new=fake_rephrase), \
             mock.patch.object(ri, "evaluate_topic_candidate",
                               return_value={"decision": "ok", "safe_topic": "товары", "reason_code": None}), \
             mock.patch.object(ri, "build_content_brief", return_value={}), \
             mock.patch.object(ri, "validate_generated_post", return_value={"allowed": True}), \
             mock.patch.object(ri.buffer, "add", side_effect=fake_add):
            asyncio.run(ri._store_reference_post(channel, "@test_mp", "donor", p, do_rephrase=True))

        self.assertIn("content", captured, "пост не сохранён в буфер")
        self.assertIn('<a href="https://ali.click/', captured["content"],
                      "партнёрская ссылка потеряна при рефразе — переклейка сломана")
        self.assertEqual(captured.get("parse_mode"), "HTML")


if __name__ == "__main__":
    unittest.main()
