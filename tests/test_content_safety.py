import unittest
import asyncio

import content_generator as content_generator_module
from channel_dna import attach_channel_dna, build_channel_dna, channel_dna_compatibility, get_effective_channel_dna
from content_generator import ContentGenerator
from content_safety import (
    build_content_brief,
    build_safe_channel_profile,
    dry_run_topic,
    evaluate_topic_candidate,
    is_celeb_drama_channel,
    is_kids_education_channel,
    validate_generated_post,
    validate_imported_post,
)
from channel_questionnaire import (
    build_proposed_channel_dna,
    questionnaire_supported,
    validate_questionnaire_input,
)


ROBO_CHANNEL = {
    "channel_id": "@robot_school",
    "topic": "робототехника и программирование для детей",
    "archetype": "kids_education",
    "channel_dna": {
        "audience": "родители детей 4-15 лет",
        "goal": "запись на пробное занятие",
        "offer": "школа робототехники и программирования для детей",
        "cta": "напишите в WhatsApp и запишитесь на пробное занятие",
        "forbidden_angles": ["игровые новости", "взрослая IT-карьера"],
    },
}


MARKETPLACE_CHANNEL = {
    "channel_id": "@wb_bomb",
    "channel_type": "marketplace",
    "topic": "товары Wildberries, Ozon, AliExpress со ссылками на покупку",
}

BLOGGER_NEWS_CHANNEL = {
    "channel_id": "@novosti_bl0gerov",
    "channel_type": "content",
    "archetype": "celeb_drama",
    "topic": "новости о жизни российских блогеров и интернет-персоналий",
}


class ContentSafetyTest(unittest.TestCase):
    def test_kids_education_topic_allowed(self):
        result = dry_run_topic(
            ROBO_CHANNEL,
            "Почему робототехника развивает логику у детей",
        )
        self.assertIn(result["safety"]["decision"], {"allowed_safe", "allowed"})
        self.assertIn("робототех", result["safety"]["safe_topic"].lower())
        self.assertIsNotNone(result["content_brief"])

    def test_gaming_news_reframed_for_robo_channel(self):
        result = dry_run_topic(
            ROBO_CHANNEL,
            "Nintendo Direct показала новые игры",
        )
        self.assertEqual(result["safety"]["decision"], "reframe")
        self.assertIn("интерес ребенка к играм", result["safety"]["safe_topic"])
        self.assertNotIn("Nintendo", result["safety"]["safe_topic"])

    def test_blocked_topic_does_not_build_brief(self):
        result = dry_run_topic(ROBO_CHANNEL, "порно и наркотики")
        self.assertEqual(result["safety"]["decision"], "blocked")
        self.assertIsNone(result["content_brief"])

    def test_channel_without_dna_keeps_basic_allowed_flow(self):
        channel = {"channel_id": "@plain", "topic": "личные финансы"}
        result = dry_run_topic(channel, "Как вести семейный бюджет")
        self.assertEqual(result["safety"]["decision"], "allowed")
        self.assertEqual(result["safety"]["safe_topic"], "Как вести семейный бюджет")

    def test_celeb_drama_rejects_general_hard_news_topics(self):
        samples = [
            "Признание главкома НАТО о России привело в шок Запад",
            "Россиянина заподозрили в убийстве многодетной матери",
            "Бесплатное протезирование обернулось для россиянки потерей зубов",
            "Блогер Хилми Форкс остался в колонии после отказа в УДО",
            "Минюст признал иноагентом блогера Романа Алехина",
            "Валерия Чекалина прошла шестой курс химиотерапии при раке желудка",
            "ВПШ — российское интернет-издание, публикующее новости о блогерах",
            "Евгения Тутушкина оштрафована за рекламу в Instagram",
        ]
        for sample in samples:
            with self.subTest(sample=sample):
                safety = evaluate_topic_candidate(
                    BLOGGER_NEWS_CHANNEL,
                    {"topic": sample, "source": "rss"},
                )
                self.assertEqual(safety["decision"], "review")
                self.assertEqual(safety["reason_code"], "celeb_drama_fit_unclear")

    def test_celeb_drama_allows_blogger_or_celebrity_topics(self):
        safety = evaluate_topic_candidate(
            BLOGGER_NEWS_CHANNEL,
            {"topic": "Блогерша сменила образ и получила реакцию подписчиков", "source": "rss"},
        )
        self.assertEqual(safety["decision"], "allowed_safe")
        self.assertEqual(safety["reason_code"], "celeb_drama_fit")

    def test_celeb_drama_rejects_generic_channel_description_topics(self):
        samples = [
            "Канал публикует новости о жизни российских блогеров и интернет-персоналий: мифы и правда",
            "Личная жизнь блогеров и инфлюенсеров: стоит ли им быть честными в соцсетях",
            "Сюрприз: блогеры тоже люди — правда ли, что все их жизни похожи на сторис",
        ]
        for sample in samples:
            with self.subTest(sample=sample):
                safety = evaluate_topic_candidate(
                    BLOGGER_NEWS_CHANNEL,
                    {"topic": sample, "source": "fallback"},
                )
                self.assertEqual(safety["decision"], "review")
                self.assertEqual(safety["reason_code"], "celeb_drama_fit_unclear")

    def test_celeb_drama_allows_specific_person_event_topics(self):
        samples = [
            "Максим Лутчак попал в Forbes 30 до 30 после роста аудитории",
            "Фешн-блогер Игор Синяк рассказал об ограблении в Париже",
            "Ольга Бузова, Оксана Самойлова и Ида Галич потеряли доступ к аккаунтам после взлома",
            "Александра Митрошина объявила о запуске нового марафона в соцсетях",
            "MrBeast запустил канал в Max и набрал миллион подписчиков",
            "Блогер Иван Петров перезапустил страницу в Instagram после взлома",
            "Инфлюенсер Анна Смирнова вернулась во ВКонтакте с новым проектом",
            "Стример Алексей Орлов открыл страницу в Facebook для зарубежной аудитории",
        ]
        for sample in samples:
            with self.subTest(sample=sample):
                safety = evaluate_topic_candidate(
                    BLOGGER_NEWS_CHANNEL,
                    {"topic": sample, "source": "search"},
                )
                self.assertEqual(safety["decision"], "allowed_safe")
                self.assertEqual(safety["reason_code"], "celeb_drama_fit")

    def test_celeb_drama_search_channel_can_use_concrete_rss_when_search_is_empty(self):
        original_get_topics = content_generator_module.get_topics
        original_rss = content_generator_module.rss
        original_web_scraper = content_generator_module.web_scraper

        async def fake_get_topics(*args, **kwargs):
            return []

        class FakeRss:
            async def fetch_for_channel(self, *args, **kwargs):
                return [{
                    "title": "Фешн-блогер Игор Синяк рассказал об ограблении в Париже",
                    "summary": "Инцидент обсуждают подписчики и другие инфлюенсеры.",
                    "image_url": None,
                }]

        class FakeWebScraper:
            async def scrape_for_channel(self, *args, **kwargs):
                return []

        generator = ContentGenerator()
        generator._get_used_topics = lambda *args, **kwargs: []
        channel = {
            **BLOGGER_NEWS_CHANNEL,
            "topic_source": "search",
            "rss_sources": ["https://lenta.ru/rss/"],
        }

        try:
            content_generator_module.get_topics = fake_get_topics
            content_generator_module.rss = FakeRss()
            content_generator_module.web_scraper = FakeWebScraper()
            topics, sources = asyncio.run(generator._collect_topics(channel, 1))
            self.assertEqual(len(topics), 1)
            self.assertEqual(topics[0]["source"], "rss")
            self.assertIn("Игор Синяк", topics[0]["topic"])
            self.assertIn("rss", sources)
        finally:
            content_generator_module.get_topics = original_get_topics
            content_generator_module.rss = original_rss
            content_generator_module.web_scraper = original_web_scraper

    def test_celeb_drama_search_channel_does_not_use_synthetic_channel_description_fallback(self):
        original_get_topics = content_generator_module.get_topics
        original_rss = content_generator_module.rss
        original_web_scraper = content_generator_module.web_scraper
        original_get_evergreen_topic = content_generator_module.buffer.get_evergreen_topic

        async def fake_get_topics(*args, **kwargs):
            return []

        class EmptyRss:
            async def fetch_for_channel(self, *args, **kwargs):
                return []

        class EmptyWebScraper:
            async def scrape_for_channel(self, *args, **kwargs):
                return []

        generator = ContentGenerator()
        generator._get_used_topics = lambda *args, **kwargs: []
        channel = {
            **BLOGGER_NEWS_CHANNEL,
            "topic_source": "search",
            "rss_sources": [],
        }

        try:
            content_generator_module.get_topics = fake_get_topics
            content_generator_module.rss = EmptyRss()
            content_generator_module.web_scraper = EmptyWebScraper()
            content_generator_module.buffer.get_evergreen_topic = lambda *args, **kwargs: None
            topics, sources = asyncio.run(generator._collect_topics(channel, 5))
            self.assertEqual(topics, [])
            self.assertNotIn("fallback", sources)
        finally:
            content_generator_module.get_topics = original_get_topics
            content_generator_module.rss = original_rss
            content_generator_module.web_scraper = original_web_scraper
            content_generator_module.buffer.get_evergreen_topic = original_get_evergreen_topic

    def test_broad_fact_rss_channel_uses_web_search_reserve_after_relevance_drop(self):
        original_get_topics = content_generator_module.get_topics
        original_rss = content_generator_module.rss
        original_web_scraper = content_generator_module.web_scraper
        original_get_evergreen_topic = content_generator_module.buffer.get_evergreen_topic

        async def fake_get_topics(*args, **kwargs):
            return ["Почему у осьминогов три сердца и как это помогает им выживать"]

        class FakeRss:
            async def fetch_for_channel(self, *args, **kwargs):
                return [{
                    "title": "A dying star could create a new universe",
                    "summary": "Off-topic astronomy headline.",
                    "image_url": None,
                }]

        class EmptyWebScraper:
            async def scrape_for_channel(self, *args, **kwargs):
                return []

        generator = ContentGenerator()
        calls = {"n": 0}

        async def fake_filter_relevant(channel, topics, count):
            calls["n"] += 1
            if calls["n"] == 1:
                return []
            return topics

        generator._filter_relevant = fake_filter_relevant
        generator._get_used_topics = lambda *args, **kwargs: []
        channel = {
            "channel_id": "@facts",
            "name": "Хочу все знать",
            "topic": "Канал публикует удивительные и малоизвестные факты о природе, животных, науке и организме.",
            "channel_type": "content",
            "topic_source": "rss",
            "rss_sources": ["https://example.com/rss"],
        }

        try:
            content_generator_module.get_topics = fake_get_topics
            content_generator_module.rss = FakeRss()
            content_generator_module.web_scraper = EmptyWebScraper()
            content_generator_module.buffer.get_evergreen_topic = lambda *args, **kwargs: None
            topics, sources = asyncio.run(generator._collect_topics(channel, 1))
            self.assertIn("search", sources)
            self.assertEqual(topics[0]["source"], "search")
            self.assertIn("осьминогов", topics[0]["topic"])
        finally:
            content_generator_module.get_topics = original_get_topics
            content_generator_module.rss = original_rss
            content_generator_module.web_scraper = original_web_scraper
            content_generator_module.buffer.get_evergreen_topic = original_get_evergreen_topic

    def test_broad_fact_channel_uses_softer_relevance_threshold(self):
        original_backend = content_generator_module.dedup.backend
        original_aembed = content_generator_module.dedup.aembed
        original_cosine = content_generator_module.dedup.cosine

        async def fake_aembed(text):
            return [1.0]

        try:
            content_generator_module.dedup.backend = lambda: "embedding"
            content_generator_module.dedup.aembed = fake_aembed
            content_generator_module.dedup.cosine = lambda *_args, **_kwargs: 0.20

            generator = ContentGenerator()
            topics = [{"topic": "Ученые обнаружили редкое поведение осьминогов", "source": "rss"}]
            broad_fact = {
                "channel_id": "@facts",
                "name": "Хочу все знать",
                "topic": "Удивительные малоизвестные факты о природе, животных, науке и организме.",
                "channel_type": "content",
            }
            narrow = {
                "channel_id": "@cs2",
                "name": "CS2 facts",
                "topic": "Канал про матчи и тактики Counter-Strike 2.",
                "channel_type": "content",
            }

            self.assertEqual(asyncio.run(generator._filter_relevant(broad_fact, topics, 1)), topics)
            self.assertEqual(asyncio.run(generator._filter_relevant(narrow, topics, 1)), [])
        finally:
            content_generator_module.dedup.backend = original_backend
            content_generator_module.dedup.aembed = original_aembed
            content_generator_module.dedup.cosine = original_cosine

    def test_celeb_drama_output_rejects_offtopic_drift(self):
        validation = validate_generated_post(
            BLOGGER_NEWS_CHANNEL,
            {"format": "инфоповод", "content": "Главком НАТО сделал заявление о России и это изменит повестку."},
            {"decision": "allowed", "safe_topic": "новость"},
            {},
        )
        self.assertFalse(validation["allowed"])
        self.assertEqual(validation["reason_code"], "celeb_drama_offtopic_output")

    def test_marketplace_with_kids_category_is_not_kids_education(self):
        channel = {
            "channel_id": "@shop",
            "channel_type": "marketplace",
            "topic": "wb_categories = [косметика, игрушки для детей, бижутерия]",
            "wb_categories": ["косметика", "игрушки для детей", "бижутерия"],
        }
        self.assertFalse(is_kids_education_channel(channel))

    def test_gaming_channel_ignores_contaminated_kids_dna(self):
        channel = {
            "channel_id": "@pradowsteam",
            "channel_type": "content",
            "archetype": "gaming_casual",
            "topic": "Канал о видеоиграх: новости Steam, релизы, обзоры и забавные случаи из игровой жизни.",
            "channel_dna": {
                **ROBO_CHANNEL["channel_dna"],
                "audience": "родители детей",
                "goal": "запись на пробное занятие / консультацию / подбор направления",
                "forbidden_angles": ["игровые новости", "релизы Nintendo/Steam/консолей"],
            },
        }
        self.assertIsNone(get_effective_channel_dna(channel))
        self.assertEqual(channel_dna_compatibility(channel)["status"], "ignored_incompatible")
        self.assertFalse(is_kids_education_channel(channel))
        result = dry_run_topic(channel, "Новый трейлер Hollow Knight Silksong вышел в Steam")
        self.assertIn(result["safety"]["decision"], {"allowed", "allowed_safe"})
        brief = result["content_brief"]
        self.assertNotIn("пробное занятие", "\n".join(brief["must_include"]).lower())
        self.assertEqual(brief["cta"], "")

    def test_non_dna_archetypes_ignore_kids_dna(self):
        for archetype in ("news", "tech_news", "auto", "music", "anime", "memes"):
            channel = {
                "channel_id": f"@{archetype}",
                "channel_type": "content",
                "archetype": archetype,
                "topic": "обычный тематический канал",
                "channel_dna": {
                    **ROBO_CHANNEL["channel_dna"],
                    "audience": "родители детей",
                    "goal": "запись на пробное занятие",
                    "cta": "напишите в WhatsApp, подберем направление",
                },
            }
            self.assertIsNone(get_effective_channel_dna(channel), archetype)

    def test_gaming_channel_with_native_dna_is_not_used_by_runtime(self):
        channel = {
            "channel_id": "@games",
            "channel_type": "content",
            "archetype": "gaming_casual",
            "topic": "игровые новости Steam",
            "channel_dna": {
                "audience": "игроки и подписчики, которые следят за релизами",
                "goal": "дать короткую игровую новость",
                "tone": "живой игровой тон",
            },
        }
        info = channel_dna_compatibility(channel)
        self.assertEqual(info["status"], "ignored_unknown")
        self.assertIsNone(get_effective_channel_dna(channel))

    def test_marketplace_ignores_kids_dna_even_for_child_products(self):
        channel = {
            **MARKETPLACE_CHANNEL,
            "topic": "детские товары и игрушки на Wildberries",
            "wb_categories": ["игрушки для детей"],
            "channel_dna": {
                **ROBO_CHANNEL["channel_dna"],
                "audience": "родители детей",
                "goal": "запись на пробное занятие",
            },
        }
        self.assertIsNone(get_effective_channel_dna(channel))
        self.assertFalse(is_kids_education_channel(channel))

    def test_kids_and_local_service_dna_stays_active(self):
        kids = {
            **ROBO_CHANNEL,
            "channel_dna": {
                **ROBO_CHANNEL["channel_dna"],
                "known_facts": {"age_groups": [{"age": "4–6", "directions": ["Lego WeDo"]}]},
            },
        }
        local = {
            "channel_id": "@dent",
            "channel_type": "content",
            "archetype": "local_service",
            "topic": "локальная стоматология",
            "channel_dna": {
                "audience": "жители района",
                "goal": "запись на консультацию",
                "cta": "позвонить",
                "known_facts": {"address": "ул. Ленина, 1"},
            },
        }
        self.assertIsNotNone(get_effective_channel_dna(kids))
        self.assertIsNotNone(get_effective_channel_dna(local))

    def test_marketplace_wb_topic_uses_marketplace_fit(self):
        channel = {
            "channel_id": "@shop",
            "channel_type": "marketplace",
            "topic": "wb_categories = [косметика, игрушки для детей, бижутерия]",
            "wb_categories": ["косметика", "игрушки для детей", "бижутерия"],
        }
        result = evaluate_topic_candidate(
            channel,
            {"topic": "WB арт.271598054 [бижутерия]", "source": "wb_parser"},
        )
        self.assertEqual(result["decision"], "allowed_safe")
        self.assertEqual(result["reason_code"], "marketplace_product_fit")

    def test_kids_education_classifier_still_detects_robo_channel(self):
        self.assertTrue(is_kids_education_channel(ROBO_CHANNEL))

    def test_output_validator_rejects_refusal(self):
        safety_and_brief = dry_run_topic(
            ROBO_CHANNEL,
            "Почему робототехника развивает логику у детей",
        )
        validation = validate_generated_post(
            ROBO_CHANNEL,
            {"content": "Извините, но я не могу написать такой пост."},
            safety_and_brief["safety"],
            safety_and_brief["content_brief"],
        )
        self.assertFalse(validation["allowed"])
        self.assertEqual(validation["reason_code"], "meta_or_refusal_output")

    def test_output_validator_rejects_fragmented_numbered_variant(self):
        channel = {
            "channel_id": "@cs2skinss2025",
            "topic": "CS2 skins and esports",
            "archetype": "gaming_esports",
            "channel_type": "content",
        }
        safety_and_brief = dry_run_topic(
            channel,
            "IEM Cologne Major 2026 Pick'Em Challenge Megathread",
        )
        validation = validate_generated_post(
            channel,
            {"content": "Please\n\n---\n\n3/5. Pick'Em - how to guess favorites and outsiders"},
            safety_and_brief["safety"],
            safety_and_brief["content_brief"],
        )
        self.assertFalse(validation["allowed"])
        self.assertEqual(validation["reason_code"], "meta_or_refusal_output")

    def test_output_validator_allows_normal_i_cannot_phrase(self):
        safety_and_brief = dry_run_topic(
            ROBO_CHANNEL,
            "Почему детям полезно учиться на ошибках в робототехнике",
        )
        validation = validate_generated_post(
            ROBO_CHANNEL,
            {
                "content": (
                    "Ребенок говорит: «я не могу собрать робота», и это нормальная "
                    "часть обучения. На занятиях дети пробуют еще раз, видят ошибку "
                    "и постепенно учатся доводить проект до результата. Напишите нам, "
                    "чтобы записаться на пробное занятие."
                )
            },
            safety_and_brief["safety"],
            safety_and_brief["content_brief"],
        )
        self.assertTrue(validation["allowed"])

    def test_output_validator_rejects_meta_refusal(self):
        safety_and_brief = dry_run_topic(
            ROBO_CHANNEL,
            "Почему робототехника развивает логику у детей",
        )
        validation = validate_generated_post(
            ROBO_CHANNEL,
            {"content": "К сожалению, я не могу помочь с этой темой. Выберите другую тему."},
            safety_and_brief["safety"],
            safety_and_brief["content_brief"],
        )
        self.assertFalse(validation["allowed"])
        self.assertEqual(validation["reason_code"], "meta_or_refusal_output")

    def test_output_validator_rejects_meta_explanation_tail(self):
        safety_and_brief = dry_run_topic(
            ROBO_CHANNEL,
            "Почему робототехника развивает логику у детей",
        )
        validation = validate_generated_post(
            ROBO_CHANNEL,
            {"content": "Пояснение: я оставил ссылку и сделал текст короче."},
            safety_and_brief["safety"],
            safety_and_brief["content_brief"],
        )
        self.assertFalse(validation["allowed"])
        self.assertEqual(validation["reason_code"], "meta_or_refusal_output")

    def test_safe_channel_profile_blocks_forbidden_analysis(self):
        profile = build_safe_channel_profile({
            "topic": "порно и наркотики",
            "archetype": "default",
            "forbidden": True,
            "forbidden_reason": "запрещённая тематика",
        })
        self.assertFalse(profile["supported"])
        self.assertEqual(profile["risk_level"], "blocked")

    def test_robo_analysis_builds_channel_dna(self):
        analysis = {
            "topic": "робототехника и программирование для детей",
            "archetype": "kids_education",
            "tone": "дружелюбный",
            "analysis_notes": "Канал школы робототехники для детей и родителей.",
            "safe_profile": build_safe_channel_profile({
                "topic": "робототехника и программирование для детей",
                "archetype": "kids_education",
                "analysis_notes": "Канал школы робототехники для детей и родителей.",
            }),
        }
        dna = build_channel_dna(
            analysis,
            posts_sample=[
                "На занятиях по робототехнике дети собирают проекты и развивают логику.",
                "Родители часто спрашивают, с какого возраста начинать программирование.",
            ],
        )
        self.assertEqual(dna["audience"], "родители детей")
        self.assertIn("игровые новости", dna["forbidden_angles"])
        self.assertIn("free_trial", dna["unknown_facts"])
        self.assertIn("discount", dna["unknown_facts"])
        self.assertIn("age_range", dna["unknown_facts"])
        self.assertIn("guaranteed_results", dna["unknown_facts"])

    def test_marketplace_kids_products_do_not_build_kids_channel_dna(self):
        analysis = {
            "topic": "детские товары / игрушки / товары для детей",
            "channel_type": "marketplace",
            "archetype": "default",
            "safe_profile": build_safe_channel_profile({
                "topic": "детские товары / игрушки / товары для детей",
                "channel_type": "marketplace",
                "archetype": "default",
            }),
        }
        dna = build_channel_dna(
            analysis,
            posts_sample=[
                "Подборка игрушек и товаров для детей с Wildberries.",
                "Артикул, цена и ссылка на товар.",
            ],
        )
        self.assertNotEqual(dna["audience"], "родители детей")
        self.assertEqual(dna["confidence"], "low")
        self.assertNotIn("игровые новости", dna["forbidden_angles"])

    def test_marketplace_wb_product_format_does_not_get_parent_forbidden_angles(self):
        analysis = {
            "topic": "игрушки и детские товары",
            "channel_type": "content",
            "archetype": "default",
            "post_formats": ["wb_product"],
            "safe_profile": build_safe_channel_profile({
                "topic": "игрушки и детские товары",
                "archetype": "default",
            }),
        }
        dna = build_channel_dna(analysis)
        self.assertNotEqual(dna["audience"], "родители детей")
        self.assertNotIn("игровые новости", dna["forbidden_angles"])
        self.assertEqual(dna["pain_points"], [])

    def test_non_kids_channels_do_not_build_kids_channel_dna(self):
        for topic, archetype in (
            ("новости технологий и стартапов", "news"),
            ("новые музыкальные релизы", "default"),
            ("автомобильные обзоры и советы", "auto"),
            ("мемы и юмор каждый день", "default"),
        ):
            analysis = {
                "topic": topic,
                "archetype": archetype,
                "channel_type": "content",
                "safe_profile": build_safe_channel_profile({
                    "topic": topic,
                    "archetype": archetype,
                }),
            }
            dna = build_channel_dna(analysis)
            self.assertNotEqual(dna["audience"], "родители детей")
            self.assertNotIn("игровые новости", dna["forbidden_angles"])

    def test_attach_channel_dna_preserves_existing_manual_dna(self):
        analysis = {
            "topic": "робототехника для детей",
            "archetype": "kids_education",
            "safe_profile": {"supported": True, "risk_level": "safe"},
            "channel_dna": {"audience": "ручная аудитория", "confidence": "high"},
        }
        attach_channel_dna(analysis, ["робототехника для детей"])
        self.assertEqual(analysis["channel_dna"]["audience"], "ручная аудитория")
        self.assertEqual(analysis["channel_dna"]["confidence"], "high")

    def test_content_brief_adds_unknown_fact_guards(self):
        channel = {
            **ROBO_CHANNEL,
            "channel_dna": {
                **ROBO_CHANNEL["channel_dna"],
                "unknown_facts": ["free_trial", "concrete_time_to_result", "guaranteed_results"],
                "known_facts": {},
            },
        }
        safety = dry_run_topic(channel, "Почему робототехника развивает логику у детей")["safety"]
        brief = build_content_brief(channel, safety)
        text = "\n".join(brief["must_avoid"])
        self.assertIn("пробное занятие", text)
        self.assertIn("за месяц", text)
        self.assertIn("гарантированный результат", text)

    def test_output_validator_rejects_unknown_free_trial_claim(self):
        channel = {
            **ROBO_CHANNEL,
            "channel_dna": {
                **ROBO_CHANNEL["channel_dna"],
                "unknown_facts": ["free_trial"],
                "known_facts": {},
            },
        }
        safety_and_brief = dry_run_topic(channel, "С какого возраста начинать программирование")
        validation = validate_generated_post(
            channel,
            {"content": "Родителям важно начать мягко: первый урок бесплатно, а дальше ребенок осваивает проекты. Напишите нам, чтобы подобрать направление."},
            safety_and_brief["safety"],
            safety_and_brief["content_brief"],
        )
        self.assertFalse(validation["allowed"])
        self.assertEqual(validation["reason_code"], "unsupported_claim_or_unknown_fact")

    def test_output_validator_rejects_unknown_time_and_guarantee_claim(self):
        channel = {
            **ROBO_CHANNEL,
            "channel_dna": {
                **ROBO_CHANNEL["channel_dna"],
                "unknown_facts": ["concrete_time_to_result", "guaranteed_results"],
                "known_facts": {},
            },
        }
        safety_and_brief = dry_run_topic(channel, "Ребенок постоянно сидит в телефоне")
        validation = validate_generated_post(
            channel,
            {"content": "Через месяц ребенок забудет о телефоне и точно научится собирать роботов. Напишите нам, чтобы подобрать направление."},
            safety_and_brief["safety"],
            safety_and_brief["content_brief"],
        )
        self.assertFalse(validation["allowed"])
        self.assertEqual(validation["reason_code"], "unsupported_claim_or_unknown_fact")

    def test_output_validator_allows_normal_post_without_unknown_claims(self):
        channel = {
            **ROBO_CHANNEL,
            "channel_dna": {
                **ROBO_CHANNEL["channel_dna"],
                "unknown_facts": ["free_trial", "concrete_time_to_result", "guaranteed_results"],
                "known_facts": {},
            },
        }
        safety_and_brief = dry_run_topic(channel, "Как выбрать секцию на лето")
        validation = validate_generated_post(
            channel,
            {"content": "Родителям проще выбирать секцию, когда понятно, что ребенок будет делать руками. В робототехнике дети собирают проекты, тренируют логику и видят результат своей работы. Напишите нам, подберем направление по возрасту."},
            safety_and_brief["safety"],
            safety_and_brief["content_brief"],
        )
        self.assertTrue(validation["allowed"])

    def test_output_validator_rejects_unknown_first_lesson_free_claim(self):
        channel = {
            **ROBO_CHANNEL,
            "channel_dna": {
                **ROBO_CHANNEL["channel_dna"],
                "unknown_facts": ["free_trial"],
                "known_facts": {},
            },
        }
        safety_and_brief = dry_run_topic(channel, "Как выбрать секцию на лето")
        validation = validate_generated_post(
            channel,
            {"content": "Родителям важно попробовать формат: первое занятие бесплатно, а дальше можно решить. Напишите, подберем направление по возрасту."},
            safety_and_brief["safety"],
            safety_and_brief["content_brief"],
        )
        self.assertFalse(validation["allowed"])
        self.assertEqual(validation["reason_code"], "unsupported_claim_or_unknown_fact")

    def test_output_validator_rejects_unknown_discount_claim(self):
        channel = {
            **ROBO_CHANNEL,
            "channel_dna": {
                **ROBO_CHANNEL["channel_dna"],
                "unknown_facts": ["discount"],
                "known_facts": {},
            },
        }
        safety_and_brief = dry_run_topic(channel, "Как выбрать секцию на лето")
        validation = validate_generated_post(
            channel,
            {"content": "Родителям проще начать летом: можно прийти со скидкой и подобрать направление по возрасту."},
            safety_and_brief["safety"],
            safety_and_brief["content_brief"],
        )
        self.assertFalse(validation["allowed"])
        self.assertEqual(validation["reason_code"], "unsupported_claim_or_unknown_fact")

    def test_output_validator_rejects_first_day_screen_guarantee(self):
        channel = {
            **ROBO_CHANNEL,
            "channel_dna": {
                **ROBO_CHANNEL["channel_dna"],
                "unknown_facts": ["concrete_time_to_result", "guaranteed_results"],
                "known_facts": {},
            },
        }
        safety_and_brief = dry_run_topic(channel, "Ребенок постоянно сидит в телефоне")
        validation = validate_generated_post(
            channel,
            {"content": "Родители часто видят, что дети в первый же день забывают про экран. Напишите, подберем направление по возрасту."},
            safety_and_brief["safety"],
            safety_and_brief["content_brief"],
        )
        self.assertFalse(validation["allowed"])
        self.assertEqual(validation["reason_code"], "unsupported_claim_or_unknown_fact")

    def test_output_validator_rejects_unknown_age_ranges(self):
        channel = {
            **ROBO_CHANNEL,
            "channel_dna": {
                **ROBO_CHANNEL["channel_dna"],
                "unknown_facts": ["age_range"],
                "known_facts": {},
            },
        }
        safety_and_brief = dry_run_topic(channel, "С какого возраста начинать программирование")
        validation = validate_generated_post(
            channel,
            {"content": "В 5–6 лет подойдет робототехника, в 7–9 лет можно начинать программирование. Напишите, подберем направление по возрасту."},
            safety_and_brief["safety"],
            safety_and_brief["content_brief"],
        )
        self.assertFalse(validation["allowed"])
        self.assertEqual(validation["reason_code"], "unsupported_claim_or_unknown_fact")

    def test_output_validator_allows_soft_age_cta(self):
        channel = {
            **ROBO_CHANNEL,
            "channel_dna": {
                **ROBO_CHANNEL["channel_dna"],
                "unknown_facts": ["age_range", "free_trial", "discount"],
                "known_facts": {},
            },
        }
        safety_and_brief = dry_run_topic(channel, "С какого возраста начинать программирование")
        validation = validate_generated_post(
            channel,
            {"content": "Родителям важно смотреть не только на возраст, но и на интерес ребенка. Напишите, подберем направление по возрасту и уровню."},
            safety_and_brief["safety"],
            safety_and_brief["content_brief"],
        )
        self.assertTrue(validation["allowed"])

    def test_output_validator_allows_known_free_trial(self):
        channel = {
            **ROBO_CHANNEL,
            "channel_dna": {
                **ROBO_CHANNEL["channel_dna"],
                "unknown_facts": ["free_trial"],
                "known_facts": {"free_trial": True},
            },
        }
        safety_and_brief = dry_run_topic(channel, "Как выбрать секцию на лето")
        validation = validate_generated_post(
            channel,
            {"content": "Родителям удобно начать мягко: бесплатное пробное занятие поможет понять интерес ребенка. Напишите, подберем направление по возрасту."},
            safety_and_brief["safety"],
            safety_and_brief["content_brief"],
        )
        self.assertTrue(validation["allowed"])

    def test_questionnaire_age_groups_normalizes_robotop(self):
        result = validate_questionnaire_input(
            "age_groups",
            "4-6: Lego WeDo, Lego WeDo 2.0\nс 7: Lego Mindstorms EV3, разработка игр",
            ROBO_CHANNEL,
        )
        self.assertTrue(result["ok"])
        self.assertEqual(result["normalized"][0]["age"], "4–6")
        self.assertIn("Lego WeDo 2.0", result["normalized"][0]["directions"])
        self.assertEqual(result["normalized"][1]["age"], "с 7")

    def test_questionnaire_age_groups_rejects_long_and_blocked(self):
        too_long = "4-6: " + ("Lego " * 250)
        self.assertFalse(validate_questionnaire_input("age_groups", too_long, ROBO_CHANNEL)["ok"])
        self.assertFalse(validate_questionnaire_input("age_groups", "4-6: казино", ROBO_CHANNEL)["ok"])

    def test_questionnaire_merge_updates_known_facts_and_unknowns(self):
        age_result = validate_questionnaire_input(
            "age_groups",
            "4-6: Lego WeDo, Lego WeDo 2.0\nс 7: Lego Mindstorms EV3, разработка игр",
            ROBO_CHANNEL,
        )
        dna = build_proposed_channel_dna(
            {
                **ROBO_CHANNEL["channel_dna"],
                "unknown_facts": ["age_range", "free_trial", "price", "address"],
                "known_facts": {"manual_note": "не трогать"},
                "known_facts_source": {"manual_note": "manual"},
            },
            {"age_groups": age_result["normalized"], "city": "Владивосток", "free_trial": None},
        )
        self.assertIn("manual_note", dna["known_facts"])
        self.assertEqual(dna["known_facts_source"]["age_groups"], "questionnaire")
        self.assertNotIn("age_range", dna["unknown_facts"])
        self.assertIn("free_trial", dna["unknown_facts"])
        self.assertIn("price", dna["unknown_facts"])
        self.assertEqual(dna["known_facts"]["city"], "Владивосток")

    def test_content_brief_uses_known_age_groups_and_directions(self):
        channel = {
            **ROBO_CHANNEL,
            "channel_dna": {
                **ROBO_CHANNEL["channel_dna"],
                "unknown_facts": ["free_trial", "price"],
                "known_facts": {
                    "age_groups": [
                        {"age": "4–6", "directions": ["Lego WeDo", "Lego WeDo 2.0"]},
                        {"age": "с 7", "directions": ["Lego Mindstorms EV3", "разработка игр"]},
                    ],
                    "directions": ["Lego WeDo", "Lego WeDo 2.0", "Lego Mindstorms EV3", "разработка игр"],
                },
            },
        }
        safety = dry_run_topic(channel, "Как выбрать направление робототехники")["safety"]
        brief = build_content_brief(channel, safety)
        include = "\n".join(brief["must_include"])
        avoid = "\n".join(brief["must_avoid"])
        self.assertIn("4–6", include)
        self.assertIn("Lego Mindstorms EV3", include)
        self.assertNotIn("age_range", avoid)

    def test_content_brief_does_not_force_program_facts_for_generic_topic(self):
        channel = {
            **ROBO_CHANNEL,
            "channel_dna": {
                **ROBO_CHANNEL["channel_dna"],
                "unknown_facts": ["free_trial", "price"],
                "known_facts": {
                    "age_groups": [
                        {"age": "4–6", "directions": ["Lego WeDo", "Lego WeDo 2.0"]},
                        {"age": "с 7", "directions": ["Lego Mindstorms EV3", "разработка игр"]},
                    ],
                    "directions": ["Lego WeDo", "Lego WeDo 2.0", "Lego Mindstorms EV3", "разработка игр"],
                },
            },
        }
        safety = dry_run_topic(channel, "Почему детям полезны проектные занятия")["safety"]
        brief = build_content_brief(channel, safety)
        include = "\n".join(brief["must_include"])
        avoid = "\n".join(brief["must_avoid"])
        self.assertNotIn("4–6", include)
        self.assertNotIn("Lego Mindstorms EV3", include)
        self.assertIn("не перечислять возрастные группы и направления", avoid)

    def test_generator_topic_dedup_normalizes_service_words_and_punctuation(self):
        gen = ContentGenerator()
        used = ["Инфоповод: образовательные программы для детей: летний городской лагерь"]
        self.assertTrue(
            gen._topic_already_used(
                "Образовательные программы для детей — летний городской лагерь",
                used,
            )
        )

    def test_generator_fallback_topic_dedup_can_reuse_common_channel_base(self):
        gen = ContentGenerator()
        used = [
            "Канал публикует удивительные и малоизвестные факты о природе: интересный неочевидный факт"
        ]

        self.assertFalse(
            gen._topic_already_used(
                "Канал публикует удивительные и малоизвестные факты о природе: разбор для новичка",
                used,
                fuzzy=False,
            )
        )
        self.assertTrue(
            gen._topic_already_used(
                "Канал публикует удивительные и малоизвестные факты о природе: интересный неочевидный факт",
                used,
                fuzzy=False,
            )
        )

    def test_output_validator_allows_known_age_groups(self):
        channel = {
            **ROBO_CHANNEL,
            "channel_dna": {
                **ROBO_CHANNEL["channel_dna"],
                "unknown_facts": ["age_range"],
                "known_facts": {
                    "age_groups": [
                        {"age": "4–6", "directions": ["Lego WeDo"]},
                        {"age": "с 7", "directions": ["Lego Mindstorms EV3"]},
                    ],
                    "directions": ["Lego WeDo", "Lego Mindstorms EV3"],
                },
            },
        }
        safety_and_brief = dry_run_topic(channel, "С какого возраста начинать робототехнику")
        validation = validate_generated_post(
            channel,
            {"content": "Для 4–6 лет подойдет Lego WeDo, а с 7 лет можно перейти к Lego Mindstorms EV3. Напишите, подберем направление по возрасту."},
            safety_and_brief["safety"],
            safety_and_brief["content_brief"],
        )
        self.assertTrue(validation["allowed"])

    def test_output_validator_rejects_unconfirmed_specific_direction(self):
        channel = {
            **ROBO_CHANNEL,
            "channel_dna": {
                **ROBO_CHANNEL["channel_dna"],
                "unknown_facts": [],
                "known_facts": {
                    "age_groups": [{"age": "4–6", "directions": ["Lego WeDo"]}],
                    "directions": ["Lego WeDo"],
                },
            },
        }
        safety_and_brief = dry_run_topic(channel, "Как выбрать направление робототехники")
        validation = validate_generated_post(
            channel,
            {"content": "На занятиях дети изучают Scratch и быстро создают свои игры. Напишите, подберем направление."},
            safety_and_brief["safety"],
            safety_and_brief["content_brief"],
        )
        self.assertFalse(validation["allowed"])
        self.assertEqual(validation["reason_code"], "unsupported_claim_or_unknown_fact")

    def test_validator_ignores_unknown_facts_from_incompatible_dna(self):
        channel = {
            "channel_id": "@games",
            "channel_type": "content",
            "archetype": "gaming_casual",
            "topic": "игровые новости Steam",
            "channel_dna": {
                **ROBO_CHANNEL["channel_dna"],
                "unknown_facts": ["free_trial", "age_range"],
                "known_facts": {"directions": ["Lego WeDo"]},
            },
        }
        safety_and_brief = dry_run_topic(channel, "Новый трейлер игры появился в Steam")
        validation = validate_generated_post(
            channel,
            {"content": "В Steam появился новый трейлер игры. Выглядит бодро: разработчики показали боевую систему и дату релиза."},
            safety_and_brief["safety"],
            safety_and_brief["content_brief"],
        )
        self.assertTrue(validation["allowed"])
        self.assertEqual(safety_and_brief["content_brief"]["cta"], "")

    def test_reference_pipeline_uses_no_kids_constraints_for_gaming_channel(self):
        channel = {
            "channel_id": "@games",
            "channel_type": "content",
            "archetype": "gaming_casual",
            "topic": "игровые новости Steam",
            "channel_dna": {
                **ROBO_CHANNEL["channel_dna"],
                "audience": "родители детей",
                "goal": "запись на пробное занятие",
                "cta": "напишите, подберем направление по возрасту",
            },
        }
        safety = evaluate_topic_candidate(
            channel,
            {"topic": "Baldur's Gate 2 может получить ремейк", "source": "reference_import"},
        )
        brief = build_content_brief(channel, safety, "reference")
        validation = validate_generated_post(
            channel,
            {"content": "Baldur's Gate 2 может получить ремейк. Если слух подтвердится, фанаты классики точно оживятся.", "format": "reference"},
            safety,
            brief,
        )
        self.assertTrue(validation["allowed"])
        self.assertEqual(brief["cta"], "")
        self.assertNotIn("родител", brief["target_reader"].lower())

    def test_questionnaire_not_forced_for_generic_and_marketplace(self):
        self.assertFalse(questionnaire_supported({"channel_id": "@news", "archetype": "news"}))
        self.assertFalse(questionnaire_supported({**MARKETPLACE_CHANNEL, "archetype": "kids_education"}))

    def test_questionnaire_available_for_detected_robo_channel_with_default_archetype(self):
        channel = {
            "channel_id": "@robotop",
            "archetype": "default",
            "topic": "робототехника и программирование для детей",
            "channel_dna": {
                "audience": "родители детей 4-15 лет",
                "offer": "школа робототехники и программирования для детей",
            },
        }
        self.assertTrue(questionnaire_supported(channel))

    def test_marketplace_reference_requires_product_link(self):
        validation = validate_generated_post(
            MARKETPLACE_CHANNEL,
            {
                "format": "reference",
                "content": (
                    'Чайный сервиз на 4 персоны\n\n'
                    'Заказать на <a href="https://ozon.ru/product/525022324">OZON</a>'
                ),
            },
            {"decision": "allowed", "safe_topic": "товар"},
            {},
        )
        self.assertTrue(validation["allowed"])

    def test_marketplace_wb_product_ignores_channel_dna_unknown_facts(self):
        channel = {
            **MARKETPLACE_CHANNEL,
            "channel_dna": {
                "unknown_facts": ["price", "discount", "free_trial"],
                "known_facts": {},
            },
        }
        validation = validate_generated_post(
            channel,
            {
                "format": "wb_product",
                "content": (
                    'Органайзер для дома со скидкой, цена 499 руб.\n'
                    '<a href="https://www.wildberries.ru/catalog/123/detail.aspx">WB</a>'
                ),
            },
            {"decision": "allowed", "safe_topic": "товар"},
            {},
        )
        self.assertTrue(validation["allowed"])

    def test_marketplace_reference_rejects_product_plus_telegram_invite(self):
        validation = validate_generated_post(
            MARKETPLACE_CHANNEL,
            {
                "format": "reference",
                "content": (
                    'Органайзер для дома\n'
                    '<a href="https://ozon.ru/product/525022324">OZON</a>\n'
                    '<a href="https://t.me/+abc123">закрытый канал</a>'
                ),
            },
            {"decision": "allowed", "safe_topic": "товар"},
            {},
        )
        self.assertFalse(validation["allowed"])
        self.assertEqual(validation["reason_code"], "forbidden_marketplace_reference_link")

    def test_marketplace_reference_rejects_product_plus_unknown_link(self):
        validation = validate_generated_post(
            MARKETPLACE_CHANNEL,
            {
                "format": "reference",
                "content": (
                    'Органайзер для дома\n'
                    '<a href="https://www.wildberries.ru/catalog/123/detail.aspx">WB</a>\n'
                    '<a href="https://random-blog.example/deal">обзор</a>'
                ),
            },
            {"decision": "allowed", "safe_topic": "товар"},
            {},
        )
        self.assertFalse(validation["allowed"])
        self.assertEqual(validation["reason_code"], "forbidden_marketplace_reference_link")

    def test_import_guard_allows_multiple_product_links_for_marketplace(self):
        validation = validate_imported_post(
            MARKETPLACE_CHANNEL,
            {
                "format": "reference",
                "content": (
                    'Подборка товаров\n'
                    '<a href="https://www.wildberries.ru/catalog/123/detail.aspx">WB</a>\n'
                    '<a href="https://ozon.ru/product/525022324">OZON</a>'
                ),
            },
        )
        self.assertTrue(validation["allowed"])

    def test_marketplace_manual_placeholder_link_is_rejected(self):
        validation = validate_generated_post(
            MARKETPLACE_CHANNEL,
            {
                "format": "manual",
                "content": "Набор колец\nцена 676₽\n\n🔗 ссылка на товар",
            },
            {"decision": "allowed", "safe_topic": "manual draft"},
            {},
        )
        self.assertFalse(validation["allowed"])
        self.assertEqual(validation["reason_code"], "missing_marketplace_link")

    def test_marketplace_reference_service_ad_is_rejected(self):
        validation = validate_generated_post(
            MARKETPLACE_CHANNEL,
            {
                "format": "reference",
                "content": (
                    "Стоматологическая клиника в Китае. Бесплатный трансфер и проживание.\n"
                    '<a href="https://stomkitay.ru/kak-dobratsya/">Сайт клиники</a>\n'
                    '<a href="https://chat.whatsapp.com/example">WhatsApp</a>'
                ),
            },
            {"decision": "allowed", "safe_topic": "reference"},
            {},
        )
        self.assertFalse(validation["allowed"])
        self.assertEqual(validation["reason_code"], "ad_or_offtopic_output")

    def test_marketplace_reference_advisory_post_is_rejected(self):
        validation = validate_generated_post(
            MARKETPLACE_CHANNEL,
            {
                "format": "reference",
                "content": (
                    "Пост не совсем по нашей теме, но важный для кошелька. "
                    "Озон поднял комиссию для продавцов, налоги и логистика дорожают. "
                    "Это не финансовая рекомендация. Загляните на маркетплейсы."
                ),
            },
            {"decision": "allowed", "safe_topic": "reference"},
            {},
        )
        self.assertFalse(validation["allowed"])
        self.assertEqual(validation["reason_code"], "ad_or_offtopic_output")

    def test_import_guard_rejects_navigation_only_text(self):
        validation = validate_imported_post(
            {"channel_id": "@plain", "topic": "сериал"},
            {"format": "manual", "content": "Серия тут"},
        )
        self.assertFalse(validation["allowed"])
        self.assertEqual(validation["reason_code"], "navigation_only_import")

    def test_import_guard_rejects_service_ad(self):
        validation = validate_imported_post(
            {"channel_id": "@plain", "topic": "товары"},
            {
                "format": "reference",
                "content": (
                    "Стоматологическая клиника в Китае. Бесплатный трансфер и проживание. "
                    "Telegram / WhatsApp: +7 999 000-00-00"
                ),
            },
        )
        self.assertFalse(validation["allowed"])
        self.assertEqual(validation["reason_code"], "import_ad_or_offtopic")

    def test_import_guard_rejects_max_channel_promo_without_blocking_platform_news(self):
        imported = validate_imported_post(
            {"channel_id": "@plain", "topic": "новости"},
            {
                "format": "reference",
                "content": "Мы в Max: подпишись и читай наш канал там.",
            },
        )
        self.assertFalse(imported["allowed"])
        self.assertEqual(imported["reason_code"], "import_ad_or_offtopic")

        topic_safety = evaluate_topic_candidate(
            BLOGGER_NEWS_CHANNEL,
            {"topic": "MrBeast запустил канал в Max и набрал миллион подписчиков", "source": "search"},
        )
        self.assertEqual(topic_safety["decision"], "allowed_safe")
        self.assertEqual(topic_safety["reason_code"], "celeb_drama_fit")

    def test_wallpaper_channel_is_not_misclassified_as_celeb_drama(self):
        channel = {
            "channel_id": "@wallgramava",
            "name": "Wallgram | Аватарки и обои",
            "topic": (
                "Канал публикует разнообразные обои и фоновые изображения для "
                "мобильных устройств: пейзажи, фантазийные сцены, автомобили, самураи."
            ),
            "channel_type": "content",
            "archetype": "default",
        }

        topic_safety = evaluate_topic_candidate(
            channel,
            {"topic": "Самурайские обои для телефона", "source": "reference"},
        )

        self.assertEqual(topic_safety["decision"], "allowed")
        self.assertNotEqual(topic_safety.get("reason_code"), "celeb_drama_fit_unclear")

    def test_import_guard_rejects_marketplace_advisory_offtopic(self):
        validation = validate_imported_post(
            MARKETPLACE_CHANNEL,
            {
                "format": "reference",
                "content": (
                    "Пост не совсем по нашей теме. Озон поднял комиссию для продавцов, "
                    "налоги и логистика дорожают. Это не финансовая рекомендация."
                ),
            },
        )
        self.assertFalse(validation["allowed"])
        self.assertEqual(validation["reason_code"], "import_ad_or_offtopic")

    def test_import_guard_allows_marketplace_product_with_html_link(self):
        validation = validate_imported_post(
            MARKETPLACE_CHANNEL,
            {
                "format": "manual",
                "content": (
                    "Набор колец\nцена 676₽\n\n"
                    '<a href="https://www.wildberries.ru/catalog/123/detail.aspx">ссылка на товар</a>'
                ),
            },
        )
        self.assertTrue(validation["allowed"])

    def test_import_guard_rejects_marketplace_placeholder_link(self):
        validation = validate_imported_post(
            MARKETPLACE_CHANNEL,
            {"format": "manual", "content": "Набор колец\nцена 676₽\n\n🔗 ссылка на товар"},
        )
        self.assertFalse(validation["allowed"])
        self.assertEqual(validation["reason_code"], "missing_marketplace_link")

    def test_war_and_politics_variants_are_blocked(self):
        channel = {"channel_id": "@travel", "topic": "путешествия, маршруты и красивые места"}
        samples = [
            "Военные РФ подошли вплотную к запорожской Новоселовке",
            "Названа главная задача ВС России для продвижения у Красноармейска",
            "Узнать свои права при онлайн-цензуре",
            "Сенатор предложил новый законопроект",
        ]
        for sample in samples:
            with self.subTest(sample=sample):
                safety = evaluate_topic_candidate(channel, {"topic": sample, "source": "rss"})
                self.assertEqual(safety["decision"], "blocked")
                validation = validate_generated_post(
                    channel,
                    {"format": "инфоповод", "content": sample},
                    {"decision": "allowed", "safe_topic": sample},
                    {},
                )
                self.assertFalse(validation["allowed"])
                self.assertEqual(validation["reason_code"], "blocked_output_content")

    def test_movie_channel_allows_fictional_war_titles(self):
        channel = {
            "channel_id": "@kinoclever",
            "name": "Киноклевер",
            "topic": "фильмы, сериалы, обзоры кино и подборки",
            "channel_type": "content",
        }
        samples = [
            "Война по времени: почему этот фантастический фильм цепляет с первых минут",
            "Война миров — классика фантастики про столкновение людей с неизвестной угрозой",
            "Терминатор: чем хорош культовый боевик и почему его всё ещё пересматривают",
        ]
        for sample in samples:
            with self.subTest(sample=sample):
                safety = evaluate_topic_candidate(channel, {"topic": sample, "source": "rss"})
                self.assertIn(safety["decision"], {"allowed", "allowed_safe"})
                validation = validate_generated_post(
                    channel,
                    {"format": "разбор", "content": sample},
                    {"decision": "allowed", "safe_topic": sample},
                    {},
                )
                self.assertTrue(validation["allowed"])

    def test_movie_channel_still_blocks_real_world_war_and_adult_content(self):
        channel = {
            "channel_id": "@kinoclever",
            "name": "Киноклевер",
            "topic": "фильмы, сериалы, обзоры кино и подборки",
            "channel_type": "content",
        }
        blocked_samples = [
            "Фильм про войну в Украине и реальные обстрелы",
            "Эротический фильм с нюдесами и фетишами",
        ]
        for sample in blocked_samples:
            with self.subTest(sample=sample):
                safety = evaluate_topic_candidate(channel, {"topic": sample, "source": "rss"})
                self.assertEqual(safety["decision"], "blocked")
                validation = validate_generated_post(
                    channel,
                    {"format": "разбор", "content": sample},
                    {"decision": "allowed", "safe_topic": sample},
                    {},
                )
                self.assertFalse(validation["allowed"])

    def test_generated_ad_or_giveaway_output_rejected(self):
        validation = validate_generated_post(
            {"channel_id": "@plain", "topic": "игровые новости"},
            {
                "format": "reference",
                "content": "Розыгрыш в MAX: подпишись на наш канал и забери промокод.",
            },
            {"decision": "allowed", "safe_topic": "reference"},
            {},
        )
        self.assertFalse(validation["allowed"])
        self.assertEqual(validation["reason_code"], "ad_or_offtopic_output")

    def test_reference_media_only_without_explicit_allow_rejected(self):
        validation = validate_imported_post(
            {"channel_id": "@plain", "topic": "новости"},
            {"format": "reference", "content": "", "media_type": "photo"},
        )
        self.assertFalse(validation["allowed"])
        self.assertEqual(validation["reason_code"], "media_only_reference_no_text")

    def test_reference_media_only_explicit_allow_is_allowed(self):
        validation = validate_imported_post(
            {"channel_id": "@plain", "topic": "новости"},
            {
                "format": "reference",
                "content": "",
                "media_type": "photo",
                "allow_media_only": True,
            },
        )
        self.assertTrue(validation["allowed"])

    def test_reference_import_rejects_hentai_ad_text(self):
        validation = validate_imported_post(
            {"channel_id": "@anime", "topic": "аниме эстетика и арты"},
            {
                "format": "reference",
                "content": "Коллекция хентая, фетиши и нюдесы каждый день.",
                "media_type": "photo",
            },
        )
        self.assertFalse(validation["allowed"])
        self.assertEqual(validation["reason_code"], "blocked_imported_content")


class ChannelSafetyIsolationTest(unittest.TestCase):
    def test_counter_strike_channel_rejects_real_sport_drift(self):
        channel = {
            "channel_id": "@csgo_only_facts",
            "name": "CS GO 2 | Only facts",
            "topic": "CS2 esports, Counter-Strike tactics, matches and skins",
            "channel_type": "content",
        }
        topic = "Victor Shuttlecocks: why badminton stability matters for a good racquet"

        safety = evaluate_topic_candidate(channel, {"topic": topic, "source": "rss"})
        self.assertEqual(safety["decision"], "blocked")
        self.assertEqual(safety["reason_code"], "counter_strike_real_sport_drift")

        validation = validate_generated_post(
            channel,
            {
                "format": "fact",
                "content": (
                    "Victor Shuttlecocks показывают ровную траекторию, "
                    "а стабильный слайд помогает контролировать темп раунда."
                ),
            },
            {"decision": "allowed", "safe_topic": topic},
            {},
        )
        self.assertFalse(validation["allowed"])
        self.assertEqual(validation["reason_code"], "counter_strike_real_sport_drift")

    def test_blogger_news_policy_does_not_classify_wallpaper_channels(self):
        wallpaper_channel = {
            "channel_id": "@wallgramava",
            "name": "Worldgram",
            "channel_type": "content",
            "topic": "обои, заставки, аватарки и красивые изображения для телефона",
        }

        self.assertFalse(is_celeb_drama_channel(wallpaper_channel))

        imported = validate_imported_post(
            wallpaper_channel,
            {
                "format": "reference",
                "content": "",
                "media_type": "photo",
                "allow_media_only": True,
            },
        )
        self.assertTrue(imported["allowed"])

    def test_blogger_news_allows_platform_context_but_blocks_generic_promos(self):
        platform_topic = evaluate_topic_candidate(
            BLOGGER_NEWS_CHANNEL,
            {
                "topic": "MrBeast запустил новый формат коротких видео в Instagram и YouTube",
                "source": "search",
            },
        )
        self.assertEqual(platform_topic["decision"], "allowed_safe")

        imported_promo = validate_imported_post(
            {"channel_id": "@plain", "topic": "игровые новости"},
            {
                "format": "reference",
                "content": "Мы в Max: подпишись на наш канал и забери промокод.",
            },
        )
        self.assertFalse(imported_promo["allowed"])
        self.assertEqual(imported_promo["reason_code"], "import_ad_or_offtopic")

    def test_robotop_policy_does_not_override_marketplace_or_movie_channels(self):
        kids_topic = evaluate_topic_candidate(
            ROBO_CHANNEL,
            {"topic": "Как робототехника помогает ребенку развивать логику", "source": "manual"},
        )
        self.assertEqual(kids_topic["reason_code"], "kids_education_fit")

        marketplace_topic = evaluate_topic_candidate(
            MARKETPLACE_CHANNEL,
            {"topic": "Подборка недорогих товаров Wildberries для кухни", "source": "manual"},
        )
        self.assertEqual(marketplace_topic["reason_code"], "marketplace_product_fit")

        movie_channel = {
            "channel_id": "@kinoclever",
            "name": "Киноклевер",
            "topic": "фильмы, сериалы, обзоры кино и подборки",
            "channel_type": "content",
        }
        movie_topic = "Война миров: почему этот фантастический фильм до сих пор работает"
        movie_safety = evaluate_topic_candidate(movie_channel, {"topic": movie_topic, "source": "rss"})
        self.assertIn(movie_safety["decision"], {"allowed", "allowed_safe"})

    def test_hard_news_stays_blocked_outside_movie_context(self):
        travel_channel = {
            "channel_id": "@just_the_view",
            "topic": "путешествия, маршруты, красивые места и советы туристам",
        }
        war_topic = "Названа главная задача ВС России для продвижения у Красноармейска"
        safety = evaluate_topic_candidate(travel_channel, {"topic": war_topic, "source": "rss"})
        self.assertEqual(safety["decision"], "blocked")

        validation = validate_generated_post(
            travel_channel,
            {"format": "разбор", "content": war_topic},
            {"decision": "allowed", "safe_topic": war_topic},
            {},
        )
        self.assertFalse(validation["allowed"])
        self.assertEqual(validation["reason_code"], "blocked_output_content")


if __name__ == "__main__":
    unittest.main()
