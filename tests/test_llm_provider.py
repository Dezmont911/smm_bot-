import unittest

import claude_helper


class LLMProviderTests(unittest.TestCase):
    def test_openai_provider_replaces_claude_model_override(self):
        original = claude_helper.cfg.OPENAI_MODEL
        try:
            claude_helper.cfg.OPENAI_MODEL = "gpt-5-mini"
            self.assertEqual(
                claude_helper._openai_model_for("claude-haiku-4-5-20251001"),
                "gpt-5-mini",
            )
        finally:
            claude_helper.cfg.OPENAI_MODEL = original

    def test_openai_provider_keeps_explicit_gpt_model(self):
        self.assertEqual(
            claude_helper._openai_model_for("gpt-5.1"),
            "gpt-5.1",
        )

    def test_anthropic_provider_replaces_gpt_model_override(self):
        original = claude_helper.cfg.CLAUDE_MODEL
        try:
            claude_helper.cfg.CLAUDE_MODEL = "claude-sonnet-4-5"
            self.assertEqual(
                claude_helper._anthropic_model_for("gpt-5-mini"),
                "claude-sonnet-4-5",
            )
        finally:
            claude_helper.cfg.CLAUDE_MODEL = original


if __name__ == "__main__":
    unittest.main()
