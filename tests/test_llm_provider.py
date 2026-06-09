import unittest

import claude_helper


class _FakeUsage:
    input_tokens = 1
    output_tokens = 1


class _FakeResponse:
    output_text = "OK"
    usage = _FakeUsage()


class _FakeResponses:
    def __init__(self):
        self.kwargs = None

    async def create(self, **kwargs):
        self.kwargs = kwargs
        return _FakeResponse()


class _FakeOpenAIClient:
    def __init__(self):
        self.responses = _FakeResponses()


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

    def test_gpt5_openai_payload_drops_temperature(self):
        self.assertFalse(claude_helper._openai_supports_temperature("gpt-5-mini"))
        self.assertTrue(claude_helper._openai_supports_temperature("gpt-4.1-mini"))

    async def _run_openai_payload(self, model: str, temperature: float):
        original_key = claude_helper.cfg.OPENAI_API_KEY
        original_client = claude_helper.openai_client
        fake_client = _FakeOpenAIClient()
        try:
            claude_helper.cfg.OPENAI_API_KEY = "test-key"
            claude_helper.openai_client = fake_client
            await claude_helper._openai_text(
                messages=[{"role": "user", "content": "Ответь OK"}],
                max_tokens=8,
                system=None,
                model=model,
                temperature=temperature,
                retries=0,
                purpose="test",
            )
            return fake_client.responses.kwargs
        finally:
            claude_helper.cfg.OPENAI_API_KEY = original_key
            claude_helper.openai_client = original_client

    def test_gpt5_openai_call_omits_temperature(self):
        kwargs = __import__("asyncio").run(
            self._run_openai_payload("gpt-5-mini", temperature=0.9)
        )
        self.assertEqual(kwargs["model"], "gpt-5-mini")
        self.assertIn("reasoning", kwargs)
        self.assertNotIn("temperature", kwargs)

    def test_gpt4_openai_call_keeps_temperature(self):
        kwargs = __import__("asyncio").run(
            self._run_openai_payload("gpt-4.1-mini", temperature=0.7)
        )
        self.assertEqual(kwargs["model"], "gpt-4.1-mini")
        self.assertEqual(kwargs["temperature"], 0.7)
        self.assertNotIn("reasoning", kwargs)


if __name__ == "__main__":
    unittest.main()
