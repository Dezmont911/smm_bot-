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


if __name__ == "__main__":
    unittest.main()
