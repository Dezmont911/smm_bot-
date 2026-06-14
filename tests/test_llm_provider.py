import unittest

import anthropic
import httpx

import claude_helper
import topic_search


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


class _FakeAnthropicMessages:
    async def create(self, **kwargs):
        request = httpx.Request("POST", "https://api.anthropic.com/v1/messages")
        response = httpx.Response(400, request=request)
        raise anthropic.BadRequestError(
            "Your credit balance is too low to access the Anthropic API.",
            response=response,
            body={"error": {"message": "Your credit balance is too low"}},
        )


class _FakeAnthropicClient:
    def __init__(self):
        self.messages = _FakeAnthropicMessages()


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

    def test_openai_web_search_uses_dedicated_model_by_default(self):
        original = getattr(claude_helper.cfg, "OPENAI_WEB_SEARCH_MODEL", "")
        try:
            claude_helper.cfg.OPENAI_WEB_SEARCH_MODEL = "gpt-4.1-mini"
            self.assertEqual(
                claude_helper._openai_web_search_model_for(None),
                "gpt-4.1-mini",
            )
            self.assertEqual(
                claude_helper._openai_web_search_model_for("gpt-5-mini"),
                "gpt-5-mini",
            )
        finally:
            claude_helper.cfg.OPENAI_WEB_SEARCH_MODEL = original

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

    async def _run_openai_web_search_payload(self, model: str):
        original_key = claude_helper.cfg.OPENAI_API_KEY
        original_client = claude_helper.openai_client
        fake_client = _FakeOpenAIClient()
        try:
            claude_helper.cfg.OPENAI_API_KEY = "test-key"
            claude_helper.openai_client = fake_client
            await claude_helper.openai_web_search_text(
                messages=[{"role": "user", "content": "Найди свежие темы"}],
                max_tokens=64,
                system=None,
                model=model,
                temperature=0.3,
                retries=0,
                purpose="test",
                allowed_domains=["example.com"],
            )
            return fake_client.responses.kwargs
        finally:
            claude_helper.cfg.OPENAI_API_KEY = original_key
            claude_helper.openai_client = original_client

    def test_openai_web_search_payload_forces_tool(self):
        kwargs = __import__("asyncio").run(
            self._run_openai_web_search_payload("")
        )
        self.assertEqual(kwargs["model"], claude_helper.cfg.OPENAI_WEB_SEARCH_MODEL)
        self.assertEqual(kwargs["tool_choice"], "required")
        self.assertEqual(kwargs["tools"][0]["type"], "web_search")
        self.assertEqual(kwargs["tools"][0]["search_context_size"], "medium")
        self.assertEqual(kwargs["tools"][0]["filters"]["allowed_domains"], ["example.com"])
        self.assertNotIn("reasoning", kwargs)

    def test_openai_web_search_gpt5_uses_low_reasoning(self):
        kwargs = __import__("asyncio").run(
            self._run_openai_web_search_payload("gpt-5-mini")
        )
        self.assertEqual(kwargs["model"], "gpt-5-mini")
        self.assertEqual(kwargs["reasoning"], {"effort": "low"})
        self.assertNotIn("temperature", kwargs)

    def test_anthropic_credit_error_falls_back_to_openai(self):
        original_provider = claude_helper.cfg.LLM_PROVIDER
        original_openai_key = claude_helper.cfg.OPENAI_API_KEY
        original_openai_client = claude_helper.openai_client
        original_anthropic_client = claude_helper.aclient
        fake_openai = _FakeOpenAIClient()
        try:
            claude_helper.cfg.LLM_PROVIDER = "anthropic"
            claude_helper.cfg.OPENAI_API_KEY = "test-key"
            claude_helper.openai_client = fake_openai
            claude_helper.aclient = _FakeAnthropicClient()
            result = __import__("asyncio").run(
                claude_helper.claude_text(
                    messages=[{"role": "user", "content": "Ответь OK"}],
                    max_tokens=8,
                    retries=0,
                    purpose="test",
                )
            )
            self.assertEqual(result, "OK")
            self.assertEqual(fake_openai.responses.kwargs["model"], claude_helper.cfg.OPENAI_MODEL)
        finally:
            claude_helper.cfg.LLM_PROVIDER = original_provider
            claude_helper.cfg.OPENAI_API_KEY = original_openai_key
            claude_helper.openai_client = original_openai_client
            claude_helper.aclient = original_anthropic_client

    def test_topic_search_anthropic_credit_error_uses_openai_fallback(self):
        original_provider = claude_helper.cfg.LLM_PROVIDER
        original_openai_key = claude_helper.cfg.OPENAI_API_KEY
        original_openai_client = claude_helper.openai_client
        original_topic_client = topic_search.aclient
        original_fallback = topic_search.openai_web_search_text

        captured = {}

        async def fake_openai_web_search(**kwargs):
            captured.update(kwargs)
            return '["свежая тема 1", "свежая тема 2"]'

        try:
            claude_helper.cfg.LLM_PROVIDER = "anthropic"
            claude_helper.cfg.OPENAI_API_KEY = "test-key"
            claude_helper.openai_client = _FakeOpenAIClient()
            topic_search.aclient = _FakeAnthropicClient()
            topic_search.openai_web_search_text = fake_openai_web_search
            topics = __import__("asyncio").run(
                topic_search.discover_topics(
                    {
                        "channel_id": "@test",
                        "name": "Test",
                        "topic": "тестовая тема",
                        "search_domains": ["example.com"],
                    },
                    count=2,
                    used_topics=[],
                )
            )
            self.assertEqual(topics, ["свежая тема 1", "свежая тема 2"])
            self.assertEqual(captured["allowed_domains"], ["example.com"])
            self.assertEqual(captured["purpose"], "topic_search_openai_web_search_fallback")
        finally:
            claude_helper.cfg.LLM_PROVIDER = original_provider
            claude_helper.cfg.OPENAI_API_KEY = original_openai_key
            claude_helper.openai_client = original_openai_client
            topic_search.aclient = original_topic_client
            topic_search.openai_web_search_text = original_fallback

    def test_topic_search_openai_provider_uses_openai_first(self):
        original_provider = claude_helper.cfg.LLM_PROVIDER
        original_openai_key = claude_helper.cfg.OPENAI_API_KEY
        original_openai_client = claude_helper.openai_client
        original_topic_client = topic_search.aclient
        original_fallback = topic_search.openai_web_search_text

        captured = {}

        class _FailIfAnthropicCalled:
            class messages:
                @staticmethod
                async def create(**kwargs):
                    raise AssertionError("Anthropic should not be called when LLM_PROVIDER=openai")

        async def fake_openai_web_search(**kwargs):
            captured.update(kwargs)
            return '["openai topic 1", "openai topic 2"]'

        try:
            claude_helper.cfg.LLM_PROVIDER = "openai"
            claude_helper.cfg.OPENAI_API_KEY = "test-key"
            claude_helper.openai_client = _FakeOpenAIClient()
            topic_search.aclient = _FailIfAnthropicCalled()
            topic_search.openai_web_search_text = fake_openai_web_search
            topics = __import__("asyncio").run(
                topic_search.discover_topics(
                    {
                        "channel_id": "@test",
                        "name": "Test",
                        "topic": "test topic",
                        "search_domains": ["example.com"],
                    },
                    count=2,
                    used_topics=[],
                )
            )
            self.assertEqual(topics, ["openai topic 1", "openai topic 2"])
            self.assertEqual(captured["allowed_domains"], ["example.com"])
            self.assertEqual(captured["purpose"], "topic_search_openai_web_search_primary")
        finally:
            claude_helper.cfg.LLM_PROVIDER = original_provider
            claude_helper.cfg.OPENAI_API_KEY = original_openai_key
            claude_helper.openai_client = original_openai_client
            topic_search.aclient = original_topic_client
            topic_search.openai_web_search_text = original_fallback

    def test_celeb_drama_search_prompt_requires_specific_person_event(self):
        prompt = topic_search._build_prompt(
            {
                "channel_id": "@novosti_bl0gerov",
                "name": "НОВОСТИ О БЛОГЕРАХ",
                "archetype": "celeb_drama",
                "topic": "новости о жизни российских блогеров и интернет-персоналий",
            },
            count=3,
            used_topics=[],
        )
        self.assertIn("конкретные новости о конкретных блогерах", prompt)
        self.assertIn("имя/ник/проект + конкретное событие", prompt)
        self.assertIn("мягкие creator-news", prompt)
        self.assertIn("иноагентов, Минюст, суды, колонии", prompt)
        self.assertIn("Максим Лутчак", prompt)
        self.assertIn("НЕ предлагай", prompt)


if __name__ == "__main__":
    unittest.main()
