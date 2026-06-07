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

    def test_source_channel_link_skipped_when_handle_known(self):
        html = (
            'Новость\n'
            '<a href="https://t.me/prepodsteam">Pradow</a>\n'
            '<a href="https://store.steampowered.com/app/1">Steam</a>'
        )
        self.assertEqual(
            _extract_links(html, source_handle="@prepodsteam"),
            [("https://store.steampowered.com/app/1", "Steam")],
        )

    def test_telegram_invites_are_not_restored(self):
        html = (
            'Пост\n'
            '<a href="https://t.me/+abc123">закрытый канал</a>\n'
            '<a href="https://telegram.me/joinchat/abc123">invite</a>\n'
            '<a href="https://store.steampowered.com/app/1">Steam</a>'
        )
        self.assertEqual(
            _extract_links(html, source_handle="@prepodsteam"),
            [("https://store.steampowered.com/app/1", "Steam")],
        )

    def test_marketplace_extract_keeps_only_product_links(self):
        channel = {"channel_type": "marketplace"}
        html = (
            '<a href="https://www.wildberries.ru/catalog/1/detail.aspx">WB</a>\n'
            '<a href="https://random-blog.example/deal">обзор</a>\n'
            '<a href="https://t.me/+abc123">донор</a>'
        )
        self.assertEqual(
            _extract_links(html, source_handle="@donor", channel=channel),
            [("https://www.wildberries.ru/catalog/1/detail.aspx", "WB")],
        )


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

    def test_rephrased_reference_strips_source_channel_footer(self):
        channel = {
            "channel_id": "@pradowsteam",
            "name": "Pradow Steam",
            "topic": "игровые новости, релизы Steam и обзоры игр",
            "post_length": "10-50",
            "archetype": "gaming_casual",
            "channel_type": "content",
        }
        p = {
            "id": 10604,
            "text": "Похоже, Baldur's Gate 2 может получить ремейк\n\nБольше новостей: @prepodsteam",
            "text_html": (
                "Похоже, Baldur&#x27;s Gate 2 может получить ремейк\n\n"
                'Больше новостей: <a href="https://t.me/prepodsteam">@prepodsteam</a>'
            ),
            "media_kind": None,
            "match_user": "prepodsteam",
            "match_id": 10604,
        }
        captured = {}

        async def fake_rephrase(text, ch):
            self.assertNotIn("@prepodsteam", text)
            return "Baldur's Gate 2, похоже, может получить ремейк. Подробности пока звучат осторожно."

        def fake_add(record):
            captured.update(record)

        with mock.patch("ai_client.rephrase_text", new=fake_rephrase), \
             mock.patch.object(ri.buffer, "add", side_effect=fake_add):
            asyncio.run(ri._store_reference_post(channel, "@pradowsteam", "@prepodsteam", p, do_rephrase=True))

        self.assertIn("content", captured)
        self.assertNotIn("@prepodsteam", captured["content"])
        self.assertNotIn("t.me/prepodsteam", captured["content"])

    def test_rephrased_marketplace_keeps_product_but_drops_donor_invite(self):
        channel = {
            "channel_id": "@shop",
            "name": "Shop",
            "topic": "товары",
            "post_length": "100 слов",
            "channel_type": "marketplace",
        }
        p = {
            "id": 10,
            "text": "Удобный органайзер для ванной\n\nБольше тут: https://t.me/+abc123",
            "text_html": (
                'Удобный органайзер для ванной\n'
                '<a href="https://www.wildberries.ru/catalog/1/detail.aspx">ссылка на товар</a>\n'
                '<a href="https://t.me/+abc123">закрытый канал</a>'
            ),
            "media_kind": None,
            "match_user": "donor",
            "match_id": 10,
        }
        captured = {}

        async def fake_rephrase(text, ch):
            return "Органайзер помогает держать мелочи под рукой. Смотреть товар по ссылке ниже."

        def fake_add(record):
            captured.update(record)

        with mock.patch("ai_client.rephrase_text", new=fake_rephrase), \
             mock.patch.object(ri, "evaluate_topic_candidate",
                               return_value={"decision": "ok", "safe_topic": "товар", "reason_code": None}), \
             mock.patch.object(ri, "build_content_brief", return_value={}), \
             mock.patch.object(ri, "validate_generated_post", return_value={"allowed": True}), \
             mock.patch.object(ri.buffer, "add", side_effect=fake_add):
            asyncio.run(ri._store_reference_post(channel, "@shop", "@donor", p, do_rephrase=True))

        self.assertIn('href="https://www.wildberries.ru/catalog/1/detail.aspx"', captured["content"])
        self.assertNotIn("t.me/+", captured["content"])
        self.assertNotIn("закрытый канал", captured["content"])
        self.assertEqual(captured.get("parse_mode"), "HTML")

    def test_reference_cleanup_works_when_rephrase_disabled(self):
        channel = {
            "channel_id": "@game",
            "name": "Game",
            "topic": "игры",
            "post_length": "100 слов",
            "channel_type": "content",
            "archetype": "gaming_casual",
        }
        p = {
            "id": 11,
            "text": "Вышел патч для игры\n\n@prepodsteam\nhttps://t.me/+abc123",
            "text_html": (
                'Вышел патч для игры\n'
                '<a href="https://store.steampowered.com/app/1">Steam</a>\n'
                '<a href="https://t.me/+abc123">закрытый канал</a>'
            ),
            "media_kind": None,
            "match_user": "prepodsteam",
            "match_id": 11,
        }
        captured = {}

        def fake_add(record):
            captured.update(record)

        with mock.patch.object(ri, "evaluate_topic_candidate",
                               return_value={"decision": "ok", "safe_topic": "игра", "reason_code": None}), \
             mock.patch.object(ri, "build_content_brief", return_value={}), \
             mock.patch.object(ri, "validate_generated_post", return_value={"allowed": True}), \
             mock.patch.object(ri.buffer, "add", side_effect=fake_add):
            asyncio.run(ri._store_reference_post(channel, "@game", "@prepodsteam", p, do_rephrase=False))

        self.assertIn('href="https://store.steampowered.com/app/1"', captured["content"])
        self.assertNotIn("@prepodsteam", captured["content"])
        self.assertNotIn("t.me/+", captured["content"])


if __name__ == "__main__":
    unittest.main()
