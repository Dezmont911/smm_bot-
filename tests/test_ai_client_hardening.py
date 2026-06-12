import unittest

import ai_client


class AiClientHardeningTest(unittest.IsolatedAsyncioTestCase):
    async def test_content_brief_topic_overrides_raw_topic_in_prompt(self):
        captured = {}
        original_claude_text = ai_client.claude_text

        async def fake_claude_text(**kwargs):
            captured["user_prompt"] = kwargs["messages"][0]["content"]
            return "Готовый полезный пост для родителей о занятиях и развитии детей."

        ai_client.claude_text = fake_claude_text
        try:
            post = await ai_client.generate_post(
                {
                    "channel_id": "@robot_school",
                    "name": "Robot School",
                    "topic": "робототехника для детей",
                    "audience": "родители",
                    "tone": "дружелюбный",
                },
                "RAW_TOPIC_SHOULD_NOT_APPEAR",
                format_name="совет",
                strategy={"style": {}, "temperature": 0.2, "archetype": "default"},
                content_brief={
                    "topic": "SAFE_TOPIC_ONLY",
                    "angle": "польза занятий для ребенка",
                    "target_reader": "родители",
                    "post_goal": "объяснить пользу",
                    "must_include": [],
                    "must_avoid": [],
                },
            )
        finally:
            ai_client.claude_text = original_claude_text

        self.assertEqual(post["topic"], "SAFE_TOPIC_ONLY")
        self.assertIn("SAFE_TOPIC_ONLY", captured["user_prompt"])
        self.assertNotIn("RAW_TOPIC_SHOULD_NOT_APPEAR", captured["user_prompt"])

    async def test_refusal_markers_reject_editor_meta_explanation(self):
        self.assertTrue(ai_client._looks_like_refusal(
            "Пояснение: я оставил структуру поста, если нужна более развёрнутая версия — дай исходный текст."
        ))
        self.assertTrue(ai_client._looks_like_refusal(
            "Если нужен исходный текст, пришлите ссылку и я перепишу пост."
        ))

    async def test_refusal_markers_reject_fragmented_numbered_variant(self):
        self.assertTrue(ai_client._looks_like_refusal(
            "Please\n\n---\n\n3/5. Pick'Em - how to guess favorites and outsiders"
        ))
        self.assertTrue(ai_client._looks_like_refusal(
            "3/5. Pick'Em - how to guess favorites and outsiders"
        ))

    async def test_clean_post_output_strips_tail_explanation(self):
        text = (
            "Полезная находка для дома: органайзер помогает держать вещи под рукой.\n\n"
            "Ссылка на товар ниже.\n\n"
            "---\n"
            "**Пояснение:** я оставил CTA и сократил текст."
        )
        cleaned = ai_client._clean_post_output(text)
        self.assertIn("Полезная находка", cleaned)
        self.assertIn("Ссылка на товар ниже.", cleaned)
        self.assertNotIn("Пояснение", cleaned)
        self.assertNotIn("я оставил", cleaned)


if __name__ == "__main__":
    unittest.main()
