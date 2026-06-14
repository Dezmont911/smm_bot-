"""
Регресс-тесты: партнёрские ссылки в референс-постах НЕ должны теряться, а мета-хвост
(«Пояснение: …») не должен попадать в пост.

Зачем: при рефакторинге генерации/safe-слоя легко случайно убрать переклейку ссылки
(_extract_links → CTA) или забыть срезать мета-хвост. Эти тесты краснеют, если так.
Гонять: py -m unittest tests.test_reference_links -v
"""

import os
import html
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

    def test_rephrased_marketplace_drops_duplicate_cta_and_raw_url(self):
        channel = {"channel_id": "@myjicki", "name": "Myjicki", "topic": "товары",
                   "post_length": "100 слов", "channel_type": "marketplace"}
        url = "https://ali.click/iql3e1b?erid=2SDnje9xsyU&sub=1"
        p = {
            "id": 3569,
            "text": "Тренажёр для кисти и мышц предплечья",
            "text_html": f'Тренажёр для кисти и мышц предплечья\n<a href="{url}">ссылка</a>',
            "media_kind": "video",
            "match_user": "nahodki_for_man",
            "match_id": 3569,
        }
        captured = {}

        async def fake_rephrase(text, ch):
            return (
                "Тренажёр для кисти и мышц предплечья\n"
                "🛒 Купить на AliExpress\n\n"
                f"[Смотреть на Aliexpress]({url}) ({url})"
            )

        def fake_add(record):
            captured.update(record)

        with mock.patch("ai_client.rephrase_text", new=fake_rephrase), \
             mock.patch.object(ri, "evaluate_topic_candidate",
                               return_value={"decision": "ok", "safe_topic": "товар", "reason_code": None}), \
             mock.patch.object(ri, "build_content_brief", return_value={}), \
             mock.patch.object(ri, "validate_generated_post", return_value={"allowed": True}), \
             mock.patch.object(ri.buffer, "add", side_effect=fake_add):
            asyncio.run(ri._store_reference_post(channel, "@myjicki", "@nahodki_for_man", p, do_rephrase=True))

        content = captured["content"]
        escaped_url = html.escape(url, quote=True)
        self.assertIn("Тренажёр для кисти", content)
        self.assertNotIn("Купить на AliExpress", content)
        self.assertNotIn("[Смотреть на Aliexpress]", content)
        self.assertNotIn(f"({url})", content)
        self.assertEqual(content.count(escaped_url), 1)
        self.assertEqual(content.count("<a href="), 1)
        self.assertIn(">Смотреть на Aliexpress</a>", content)
        self.assertEqual(captured.get("parse_mode"), "HTML")

    def test_rephrased_marketplace_keeps_contextual_platform_sentence(self):
        channel = {"channel_id": "@test_mp", "name": "Тест", "topic": "товары с маркетплейсов",
                   "post_length": "100 слов", "channel_type": "marketplace"}
        url = "https://ali.click/context"
        p = {
            "id": 2,
            "text": "Полезная вещь для дома",
            "text_html": f'Полезная вещь для дома\n<a href="{url}">ссылка</a>',
            "media_kind": None,
            "match_user": "donor",
            "match_id": 2,
        }
        captured = {}

        async def fake_rephrase(text, ch):
            return "Поймать его можно на Aliexpress, если нужен компактный вариант для дома."

        def fake_add(record):
            captured.update(record)

        with mock.patch("ai_client.rephrase_text", new=fake_rephrase), \
             mock.patch.object(ri, "evaluate_topic_candidate",
                               return_value={"decision": "ok", "safe_topic": "товар", "reason_code": None}), \
             mock.patch.object(ri, "build_content_brief", return_value={}), \
             mock.patch.object(ri, "validate_generated_post", return_value={"allowed": True}), \
             mock.patch.object(ri.buffer, "add", side_effect=fake_add):
            asyncio.run(ri._store_reference_post(channel, "@test_mp", "@donor", p, do_rephrase=True))

        content = captured["content"]
        self.assertIn("Поймать его можно на Aliexpress", content)
        self.assertEqual(content.count(url), 1)
        self.assertEqual(content.count("<a href="), 1)
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

    def test_rephrased_reference_drops_plain_link_placeholders(self):
        channel = {
            "channel_id": "@angelResursPack",
            "name": "Angel",
            "topic": "Minecraft news",
            "post_length": "100 words",
            "channel_type": "content",
        }
        p = {
            "id": 2743,
            "text": (
                "Minecraft cape drop\n"
                "Minecraft on Twitch https://www.twitch.tv/directory/category/minecraft\n"
                "Key inventory https://www.twitch.tv/drops/inventory\n"
                "Redeem https://www.minecraft.net/ru-ru/redeem"
            ),
            "text_html": (
                'Minecraft cape drop\n'
                '<a href="https://www.twitch.tv/directory/category/minecraft">ссылка</a>\n'
                '<a href="https://www.twitch.tv/drops/inventory">ссылка</a>\n'
                '<a href="https://www.minecraft.net/ru-ru/redeem">ссылка</a>'
            ),
            "media_kind": "photo",
            "match_user": "minecraftoday",
            "match_id": 2743,
        }
        captured = {}

        async def fake_rephrase(text, ch):
            return (
                "Плащ строителя уже начали раздавать за просмотры стримов на Twitch.\n"
                "🔷 Minecraft на Twitch — ссылка\n"
                "🔷 Твой ключ будет здесь — ссылка\n"
                "🔷 Активировать плащ — ссылка\n"
                "ссылка\n"
                "ссылка (https://www.twitch.tv/directory/category/minecraft)\n"
                "ссылка (https://www.twitch.tv/drops/inventory)\n"
                "ссылка (https://www.minecraft.net/ru-ru/redeem)"
            )

        def fake_add(record):
            captured.update(record)

        with mock.patch("ai_client.rephrase_text", new=fake_rephrase), \
             mock.patch.object(ri, "evaluate_topic_candidate",
                               return_value={"decision": "ok", "safe_topic": "minecraft", "reason_code": None}), \
             mock.patch.object(ri, "build_content_brief", return_value={}), \
             mock.patch.object(ri, "validate_generated_post", return_value={"allowed": True}), \
             mock.patch.object(ri.buffer, "add", side_effect=fake_add):
            asyncio.run(ri._store_reference_post(channel, "@angelResursPack", "@minecraftoday", p, do_rephrase=True))

        content = captured["content"]
        self.assertNotIn("— ссылка", content)
        self.assertNotRegex(content, r"(?m)^ссылка$")
        self.assertNotIn("ссылка (https://", content)
        self.assertEqual(content.count("<a href="), 3)
        self.assertIn("https://www.twitch.tv/directory/category/minecraft", content)
        self.assertIn("https://www.twitch.tv/drops/inventory", content)
        self.assertIn("https://www.minecraft.net/ru-ru/redeem", content)

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


class ReferenceTextMediaModesTest(unittest.TestCase):
    def setUp(self):
        self.channel = {
            "channel_id": "@plain",
            "name": "Plain",
            "topic": "useful notes",
            "post_length": "100 words",
            "channel_type": "content",
        }
        self.marketplace = {
            "channel_id": "@shop",
            "name": "Shop",
            "topic": "marketplace products",
            "post_length": "100 words",
            "channel_type": "marketplace",
        }

    def _capture_add(self):
        captured = {}

        def fake_add(record):
            captured.update(record)

        return captured, fake_add

    def _allow_safety(self):
        return [
            mock.patch.object(ri, "evaluate_topic_candidate",
                              return_value={"decision": "ok", "safe_topic": "safe", "reason_code": None}),
            mock.patch.object(ri, "build_content_brief", return_value={}),
            mock.patch.object(ri, "validate_generated_post", return_value={"allowed": True}),
        ]

    def test_has_meaningful_text_guard(self):
        self.assertFalse(ri.has_meaningful_text(""))
        self.assertFalse(ri.has_meaningful_text("🔥🔥🔥"))
        self.assertFalse(ri.has_meaningful_text("https://t.me/donor"))
        self.assertTrue(ri.has_meaningful_text("Useful caption with several real words."))

    def test_old_donor_defaults_include_text(self):
        captured, fake_add = self._capture_add()
        p = {"id": 1, "text": "Useful caption with several real words.", "media_kind": None}
        with mock.patch.object(ri.buffer, "add", side_effect=fake_add):
            result = asyncio.run(ri._store_reference_post(self.channel, "@plain", "@donor", p, do_rephrase=False))
        self.assertEqual(result, [])
        self.assertIn("Useful caption", captured["content"])

    def test_include_text_false_media_true_saves_media_only(self):
        captured, fake_add = self._capture_add()
        p = {"id": 2, "text": "Caption must be ignored completely", "media_kind": "photo"}

        async def fake_rephrase(_text, _channel):
            raise AssertionError("LLM must not be called when include_text=false")

        with mock.patch("ai_client.rephrase_text", new=fake_rephrase), \
             mock.patch.object(ri.buffer, "add", side_effect=fake_add):
            result = asyncio.run(ri._store_reference_post(
                self.channel, "@plain", "@donor", p, do_rephrase=True,
                ref_config={"include_text": False, "take_media": True},
            ))
        self.assertEqual(result, [2])
        self.assertEqual(captured["content"], "")
        self.assertEqual(captured["media_type"], "photo")

    def test_include_text_false_preserves_external_reference_link(self):
        captured, fake_add = self._capture_add()
        p = {
            "id": 20,
            "text": "Caption must be ignored https://www.twitch.tv/directory/category/minecraft",
            "text_html": '<a href="https://www.twitch.tv/directory/category/minecraft">Twitch</a>',
            "media_kind": "photo",
        }
        patches = self._allow_safety()
        with patches[0], patches[1], patches[2], mock.patch.object(ri.buffer, "add", side_effect=fake_add):
            result = asyncio.run(ri._store_reference_post(
                self.channel, "@plain", "@donor", p, do_rephrase=True,
                ref_config={"include_text": False, "take_media": True},
            ))
        self.assertEqual(result, [20])
        self.assertIn('href="https://www.twitch.tv/directory/category/minecraft"', captured["content"])
        self.assertEqual(captured.get("parse_mode"), "HTML")

    def test_explicit_entity_links_are_restored_when_html_is_empty(self):
        captured, fake_add = self._capture_add()
        p = {
            "id": 21,
            "text": "Minecraft links: ссылка",
            "text_html": "Minecraft links: ссылка",
            "links": [
                {"url": "https://www.minecraft.net/ru-ru/redeem", "label": "ссылка"},
            ],
            "media_kind": "photo",
        }

        async def fake_rephrase(_text, _channel):
            return "Activate the cape here: ссылка"

        patches = self._allow_safety()
        with mock.patch("ai_client.rephrase_text", new=fake_rephrase), \
             patches[0], patches[1], patches[2], \
             mock.patch.object(ri.buffer, "add", side_effect=fake_add):
            result = asyncio.run(ri._store_reference_post(
                self.channel, "@plain", "@donor", p, do_rephrase=True,
                ref_config={"include_text": True, "take_media": True},
            ))
        self.assertEqual(result, [21])
        self.assertIn('href="https://www.minecraft.net/ru-ru/redeem"', captured["content"])
        self.assertNotRegex(captured["content"], r"(?m)^ссылка$")

    def test_include_text_true_media_false_saves_text_only(self):
        captured, fake_add = self._capture_add()
        p = {"id": 3, "text": "Useful caption with several real words.", "media_kind": "photo"}
        with mock.patch.object(ri.buffer, "add", side_effect=fake_add):
            result = asyncio.run(ri._store_reference_post(
                self.channel, "@plain", "@donor", p, do_rephrase=False,
                ref_config={"include_text": True, "take_media": False},
            ))
        self.assertEqual(result, [])
        self.assertEqual(captured["status"], "ready")
        self.assertNotIn("media_type", captured)

    def test_text_and_media_disabled_is_rejected(self):
        p = {"id": 4, "text": "Useful caption with several real words.", "media_kind": "photo"}
        with mock.patch.object(ri.buffer, "add") as add_mock:
            result = asyncio.run(ri._store_reference_post(
                self.channel, "@plain", "@donor", p, do_rephrase=False,
                ref_config={"include_text": False, "take_media": False},
            ))
        self.assertIsNone(result)
        add_mock.assert_not_called()

    def test_empty_and_emoji_caption_do_not_call_llm_or_save_default_media_only(self):
        for idx, text in enumerate(("", "🔥🔥🔥"), start=5):
            p = {"id": idx, "text": text, "media_kind": "photo"}

            async def fake_rephrase(_text, _channel):
                raise AssertionError("LLM must not be called for non-meaningful captions")

            with mock.patch("ai_client.rephrase_text", new=fake_rephrase), \
                 mock.patch.object(ri.buffer, "add") as add_mock:
                result = asyncio.run(ri._store_reference_post(
                    self.channel, "@plain", "@donor", p, do_rephrase=True,
                    ref_config={"include_text": True, "take_media": True},
                ))
            self.assertIsNone(result)
            add_mock.assert_not_called()

    def test_marketplace_text_off_preserves_product_link(self):
        captured, fake_add = self._capture_add()
        p = {
            "id": 7,
            "text": "Caption must be ignored https://www.wildberries.ru/catalog/1/detail.aspx",
            "text_html": '<a href="https://www.wildberries.ru/catalog/1/detail.aspx">WB</a>',
            "media_kind": "photo",
        }
        patches = self._allow_safety()
        with patches[0], patches[1], patches[2], mock.patch.object(ri.buffer, "add", side_effect=fake_add):
            result = asyncio.run(ri._store_reference_post(
                self.marketplace, "@shop", "@donor", p, do_rephrase=True,
                ref_config={"include_text": False, "take_media": True},
            ))
        self.assertEqual(result, [7])
        self.assertIn('href="https://www.wildberries.ru/catalog/1/detail.aspx"', captured["content"])
        self.assertEqual(captured.get("parse_mode"), "HTML")

    def test_marketplace_text_off_without_product_link_is_rejected(self):
        p = {
            "id": 8,
            "text": "Caption must be ignored https://t.me/donor",
            "text_html": '<a href="https://t.me/donor">donor</a>',
            "media_kind": "photo",
        }
        with mock.patch.object(ri.buffer, "add") as add_mock:
            result = asyncio.run(ri._store_reference_post(
                self.marketplace, "@shop", "@donor", p, do_rephrase=True,
                ref_config={"include_text": False, "take_media": True},
            ))
        self.assertIsNone(result)
        add_mock.assert_not_called()

    def test_reference_short_vpn_promo_is_rejected(self):
        p = {
            "id": 30,
            "text": "My top VPN personally tested",
            "media_kind": None,
        }
        with mock.patch.object(ri.buffer, "add") as add_mock:
            result = asyncio.run(ri._store_reference_post(
                self.channel, "@plain", "@donor", p, do_rephrase=False,
            ))
        self.assertIsNone(result)
        add_mock.assert_not_called()

    def test_reference_vpn_photo_promo_is_rejected(self):
        p = {
            "id": 31,
            "text": "LumixVPN: unlimited traffic, many locations, connect here.",
            "media_kind": "photo",
        }
        with mock.patch.object(ri.buffer, "add") as add_mock:
            result = asyncio.run(ri._store_reference_post(
                self.channel, "@plain", "@donor", p, do_rephrase=False,
            ))
        self.assertIsNone(result)
        add_mock.assert_not_called()

    def test_reference_vpn_promo_after_rephrase_is_rejected(self):
        p = {
            "id": 33,
            "text": "A small personal tool recommendation with enough neutral words for import.",
            "media_kind": None,
        }

        async def fake_rephrase(_text, _channel):
            return "🔒 Мой любимый VPN (личная рекомендация)"

        patches = self._allow_safety()
        with mock.patch("ai_client.rephrase_text", new=fake_rephrase), \
             patches[0], patches[1], patches[2], \
             mock.patch.object(ri.buffer, "add") as add_mock:
            result = asyncio.run(ri._store_reference_post(
                self.channel, "@plain", "@donor", p, do_rephrase=True,
            ))
        self.assertIsNone(result)
        add_mock.assert_not_called()

    def test_reference_our_channels_promo_is_rejected(self):
        p = {
            "id": 34,
            "text": "📢 Новые обои для телефона\n⚡️ Наши каналы ⚡️",
            "media_kind": "photo",
        }
        with mock.patch.object(ri.buffer, "add") as add_mock:
            result = asyncio.run(ri._store_reference_post(
                self.channel, "@plain", "@donor", p, do_rephrase=False,
            ))
        self.assertIsNone(result)
        add_mock.assert_not_called()

    def test_reference_ai_service_promo_is_rejected(self):
        p = {
            "id": 35,
            "text": (
                "🔥 НЕЙРОСЕТЬ, КОТОРАЯ ПОРАЖАЕТ\n"
                "🚀 Более 3000 моделей генерации\n"
                "🎬 Рендер до 20 секунд\n"
                "🎧 Создаёт видео с звуком\n"
                "🏆 Качество — до 720p"
            ),
            "media_kind": "animation",
        }
        with mock.patch.object(ri.buffer, "add") as add_mock:
            result = asyncio.run(ri._store_reference_post(
                self.channel, "@plain", "@donor", p, do_rephrase=False,
            ))
        self.assertIsNone(result)
        add_mock.assert_not_called()

    def test_reference_max_relocation_promo_is_rejected(self):
        p = {
            "id": 32,
            "text": (
                "Moved here - https://max.ru/oboi_wallpaper_kpop\n"
                "Admin - https://clck.ru/3QWPaD\n"
                "Manager - https://clck.ru/3R5Lx9"
            ),
            "text_html": '<a href="https://max.ru/oboi_wallpaper_kpop">MAX</a>',
            "media_kind": None,
        }
        with mock.patch.object(ri.buffer, "add") as add_mock:
            result = asyncio.run(ri._store_reference_post(
                self.channel, "@plain", "@donor", p, do_rephrase=False,
            ))
        self.assertIsNone(result)
        add_mock.assert_not_called()

    def test_blocked_reference_hosts_are_not_restored(self):
        self.assertFalse(ri._link_allowed("https://max.ru/oboi_wallpaper_kpop", "MAX", "@donor", self.channel))
        self.assertFalse(ri._link_allowed("https://clck.ru/3QWPaD", "Admin", "@donor", self.channel))
        self.assertFalse(ri._link_allowed("https://tagio.pro/channel/5072/pricing", "Ads", "@donor", self.channel))
        self.assertTrue(ri._link_allowed("https://www.twitch.tv/directory/category/minecraft", "Twitch", "@donor", self.channel))

    def test_meta_rephrase_falls_back_to_clean_original(self):
        captured, fake_add = self._capture_add()
        p = {"id": 9, "text": "Useful caption with several real words.", "media_kind": None}

        async def fake_rephrase(_text, _channel):
            return "пришли текст, и я перепишу"

        with mock.patch("ai_client.rephrase_text", new=fake_rephrase), \
             mock.patch.object(ri.buffer, "add", side_effect=fake_add):
            result = asyncio.run(ri._store_reference_post(self.channel, "@plain", "@donor", p, do_rephrase=True))
        self.assertEqual(result, [])
        self.assertIn("Useful caption", captured["content"])
        self.assertNotIn("пришли текст", captured["content"].lower())

    def test_generate_text_from_media_unavailable_is_rejected_without_explicit_allow(self):
        p = {"id": 10, "text": "", "media_kind": "photo"}
        with mock.patch.object(ri.buffer, "add") as add_mock:
            result = asyncio.run(ri._store_reference_post(
                self.channel, "@plain", "@donor", p, do_rephrase=True,
                ref_config={"include_text": True, "take_media": True, "generate_text_from_media": True},
            ))
        self.assertIsNone(result)
        add_mock.assert_not_called()


class PosterCaptionClippingTest(unittest.TestCase):
    def test_html_caption_clip_does_not_break_anchor(self):
        from poster import TG_CAPTION_LIMIT, _caption_for_parse_mode, _html_visible_text

        body = "A" * (TG_CAPTION_LIMIT + 50)
        html = body + '\n<a href="https://store.steampowered.com/app/1/">Steam page</a>'
        clipped = _caption_for_parse_mode(html, "HTML", "HTML")

        self.assertLessEqual(len(_html_visible_text(clipped)), TG_CAPTION_LIMIT)
        self.assertNotIn('<a href="https://st...', clipped)
        self.assertNotRegex(clipped, r'<a\\s+href="[^"]*$')

    def test_html_caption_plain_fallback_strips_tags(self):
        from poster import _caption_for_parse_mode

        html = 'Lead <a href="https://store.steampowered.com/app/1/">Steam page</a>'
        plain = _caption_for_parse_mode(html, None, "HTML")

        self.assertEqual(plain, "Lead Steam page")
        self.assertNotIn("<a href", plain)
        self.assertNotIn("https://store.steampowered.com", plain)

    def test_send_photo_timeout_is_ambiguous_success_without_retry(self):
        from telegram.error import TimedOut
        from poster import Poster

        class TimeoutBot:
            def __init__(self):
                self.calls = 0

            async def send_photo(self, **kwargs):
                self.calls += 1
                raise TimedOut("Timed out")

        bot = TimeoutBot()
        p = Poster()
        p.bot = bot
        post = {
            "id": "timeout-post",
            "channel_id": "@timeout_channel",
            "content": "Тестовый пост с картинкой",
            "image_url": "https://example.com/image.jpg",
            "parse_mode": "Markdown",
        }

        result = asyncio.run(p._publish(post))

        self.assertTrue(result["success"])
        self.assertTrue(result["ambiguous_timeout"])
        self.assertEqual(bot.calls, 1)

    def test_album_publish_returns_lowest_message_id_for_boost(self):
        import json
        from types import SimpleNamespace
        from poster import Poster

        class AlbumBot:
            async def send_media_group(self, **kwargs):
                return [
                    SimpleNamespace(message_id=7081, media_group_id="album-modlenca", chat=SimpleNamespace(id=-1001, username="modlenca")),
                    SimpleNamespace(message_id=7080, media_group_id="album-modlenca", chat=SimpleNamespace(id=-1001, username="modlenca")),
                ]

        p = Poster()
        p.bot = AlbumBot()
        post = {
            "id": "album-post",
            "channel_id": "@modlenca",
            "content": "album caption",
            "media_type": "album",
            "tg_file_id": json.dumps(
                {
                    "members": [1, 2],
                    "items": {
                        "1": {"file_id": "photo-1", "type": "photo"},
                        "2": {"file_id": "photo-2", "type": "photo"},
                    },
                }
            ),
            "parse_mode": "HTML",
        }

        result = asyncio.run(p._publish(post))

        self.assertTrue(result["success"])
        self.assertEqual(result["message"].message_id, 7080)

    def test_wb_product_without_image_is_not_published_as_text(self):
        import poster
        from poster import Poster

        class Bot:
            def __init__(self):
                self.messages = 0

            async def send_message(self, **kwargs):
                self.messages += 1
                return object()

        bot = Bot()
        p = Poster()
        p.bot = bot
        post = {
            "id": "wb-no-image",
            "channel_id": "@shop",
            "content": "WB product text",
            "format": "wb_product",
            "parse_mode": "HTML",
        }

        with mock.patch.object(poster.buffer, "mark_skipped") as mark_skipped:
            result = asyncio.run(p._publish(post))

        self.assertFalse(result["success"])
        self.assertEqual(result["reason"], "wb_image_missing")
        self.assertEqual(bot.messages, 0)
        mark_skipped.assert_called_once_with("wb-no-image")

    def test_wb_product_failed_cdn_image_is_not_published_as_text(self):
        import poster
        from poster import Poster

        class Bot:
            def __init__(self):
                self.messages = 0

            async def send_message(self, **kwargs):
                self.messages += 1
                return object()

        bot = Bot()
        p = Poster()
        p.bot = bot
        post = {
            "id": "wb-cdn-failed",
            "channel_id": "@shop",
            "content": "WB product text",
            "format": "wb_product",
            "image_url": "https://basket-10.wbbasket.ru/vol1/part1/1/images/big/1.webp",
            "parse_mode": "HTML",
        }

        with (
            mock.patch.object(p, "_download_wb_image", new=mock.AsyncMock(return_value=None)),
            mock.patch.object(poster.buffer, "mark_skipped") as mark_skipped,
        ):
            result = asyncio.run(p._publish(post))

        self.assertFalse(result["success"])
        self.assertEqual(result["reason"], "wb_image_unavailable")
        self.assertEqual(bot.messages, 0)
        mark_skipped.assert_called_once_with("wb-cdn-failed")


class WbImageDownloadRoutingTest(unittest.TestCase):
    def test_proxy_routes_rotate_cap_proxies_and_add_direct_fallback(self):
        import poster

        def reverse_in_place(items):
            items.reverse()

        with mock.patch.object(poster.random, "shuffle", side_effect=reverse_in_place):
            routes = poster._wb_proxy_routes(["proxy-1", "proxy-2", "proxy-3", "proxy-4"])

        self.assertEqual(routes, ["proxy-4", "proxy-3", "proxy-2", None])

    def test_safe_wb_exception_does_not_leak_proxy_credentials(self):
        import poster

        class ProxyError(Exception):
            status = 502
            message = "Bad Gateway"

            def __str__(self):
                return "http://user:secret@example.proxy:9000"

        text = poster._safe_wb_exception(ProxyError())

        self.assertIn("ProxyError", text)
        self.assertIn("status=502", text)
        self.assertIn("Bad Gateway", text)
        self.assertNotIn("secret", text)
        self.assertNotIn("example.proxy", text)
        self.assertNotIn("http://", text)


class ReferenceUiModeTest(unittest.TestCase):
    def test_ref_content_mode_defaults_to_text_and_media(self):
        import ui

        ref = {"handle": "@donor"}

        self.assertEqual(ui._ref_content_mode(ref), "text_media")
        self.assertEqual(ui._ref_content_mode_label(ref), "текст + медиа")

    def test_ref_content_mode_cycles_through_clear_presets(self):
        import ui

        ref = {"handle": "@donor", "include_text": True, "take_media": True}

        self.assertEqual(ui._cycle_ref_content_mode(ref), "media_only")
        self.assertEqual(ref["include_text"], False)
        self.assertEqual(ref["take_media"], True)
        self.assertEqual(ref["allow_media_only"], True)

        self.assertEqual(ui._cycle_ref_content_mode(ref), "text_only")
        self.assertEqual(ref["include_text"], True)
        self.assertEqual(ref["take_media"], False)
        self.assertNotIn("allow_media_only", ref)

        self.assertEqual(ui._cycle_ref_content_mode(ref), "text_media")
        self.assertEqual(ref["include_text"], True)
        self.assertEqual(ref["take_media"], True)

    def test_ref_content_mode_repairs_invalid_disabled_state(self):
        import ui

        ref = {"handle": "@donor", "include_text": False, "take_media": False}

        self.assertEqual(ui._ref_content_mode(ref), "disabled")
        self.assertEqual(ui._cycle_ref_content_mode(ref), "media_only")
        self.assertEqual(ref["include_text"], False)
        self.assertEqual(ref["take_media"], True)


if __name__ == "__main__":
    unittest.main()
