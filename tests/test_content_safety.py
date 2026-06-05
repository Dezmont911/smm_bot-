import unittest

from content_safety import build_safe_channel_profile, dry_run_topic, validate_generated_post


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


if __name__ == "__main__":
    unittest.main()
