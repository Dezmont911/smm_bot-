import unittest
from types import SimpleNamespace

from boost_manager import (
    TwiBoostClientWrapper,
    boost_configured,
    boost_real_orders_allowed,
    effective_boost_enabled,
    load_global_boost_settings,
    required_env_vars,
)


class BoostManagerTests(unittest.TestCase):
    def setUp(self):
        self.config = SimpleNamespace(
            ADMIN_CHAT_IDS=[100, 200],
            TWIBOOST_API_KEY="secret",
            TWIBOOST_API_URL="https://twiboost.com/api/v2",
            TWIBOOST_VIEWS_SERVICE_ID=123,
            BOOST_DRY_RUN=True,
            BOOST_REAL_ORDERS_ENABLED=False,
        )

    def test_effective_boost_defaults_to_disabled(self):
        channel = {"channel_id": "@main"}

        self.assertFalse(effective_boost_enabled(channel, {}, config=self.config))

    def test_effective_boost_inherits_global_for_admin_channel(self):
        channel = {"channel_id": "@main"}
        settings = {"boost_global_enabled": True}

        self.assertTrue(effective_boost_enabled(channel, settings, config=self.config))

    def test_channel_override_off_wins_over_global(self):
        channel = {"channel_id": "@main", "boost_override": "off"}
        settings = {"boost_global_enabled": True}

        self.assertFalse(effective_boost_enabled(channel, settings, config=self.config))

    def test_channel_override_on_does_not_bypass_tester_scope(self):
        channel = {"channel_id": "@tester", "owner_id": 999, "boost_override": "on"}
        settings = {"boost_global_enabled": True}

        self.assertFalse(effective_boost_enabled(channel, settings, config=self.config))

    def test_admin_owned_channel_can_override_on(self):
        channel = {"channel_id": "@admin", "owner_id": 100, "boost_override": "on"}

        self.assertTrue(effective_boost_enabled(channel, {}, config=self.config))

    def test_status_stays_dry_run_when_real_orders_are_not_armed(self):
        settings = load_global_boost_settings({"boost_global_enabled": True}, self.config)

        self.assertEqual(settings["boost_status"], "dry_run")
        self.assertTrue(boost_configured(self.config))
        self.assertFalse(boost_real_orders_allowed(self.config))

    def test_required_env_vars_are_namespaced(self):
        names = required_env_vars()

        self.assertIn("TWIBOOST_API_KEY", names)
        self.assertIn("BOOST_REAL_ORDERS_ENABLED", names)
        self.assertNotIn("API_KEY", names)
        self.assertNotIn("SERVICE_ID", names)


class TwiBoostClientDryRunTests(unittest.IsolatedAsyncioTestCase):
    async def test_dry_run_never_calls_http_layer(self):
        class ExplodingClient(TwiBoostClientWrapper):
            def _request_sync(self, params):
                raise AssertionError("dry-run must not call HTTP")

        client = ExplodingClient(api_key="secret", api_url="https://example.test/api", service_id=123)

        result = await client.create_views_order("https://t.me/example/1", quantity=500, dry_run=True)

        self.assertTrue(result["dry_run"])
        self.assertEqual(result["request"]["action"], "add")
        self.assertEqual(result["request"]["service"], 123)

    async def test_real_order_without_config_returns_error(self):
        client = TwiBoostClientWrapper(api_key="", api_url="", service_id=0)

        result = await client.create_views_order("https://t.me/example/1", dry_run=False)

        self.assertEqual(result["error"], "twiboost_not_configured")
        self.assertFalse(result["configured"])


if __name__ == "__main__":
    unittest.main()
