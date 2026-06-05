import unittest

from channel_dna import attach_channel_dna, build_channel_dna
from content_safety import (
    build_content_brief,
    build_safe_channel_profile,
    dry_run_topic,
    evaluate_topic_candidate,
    is_kids_education_channel,
    validate_generated_post,
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

    def test_marketplace_with_kids_category_is_not_kids_education(self):
        channel = {
            "channel_id": "@shop",
            "channel_type": "marketplace",
            "topic": "wb_categories = [косметика, игрушки для детей, бижутерия]",
            "wb_categories": ["косметика", "игрушки для детей", "бижутерия"],
        }
        self.assertFalse(is_kids_education_channel(channel))

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


if __name__ == "__main__":
    unittest.main()
