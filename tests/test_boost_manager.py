import tempfile
import unittest
from pathlib import Path
from types import SimpleNamespace

from database import Database
from boost_manager import (
    TwiBoostClientWrapper,
    add_tracked_channel,
    boost_configured,
    boost_real_orders_allowed,
    delete_tracked_channel,
    get_boost_settings,
    handle_boost_channel_post_dry_run,
    list_tracked_channels,
    required_env_vars,
    save_boost_settings,
    set_tracked_channel_enabled,
    set_tracked_channel_quantity,
)


class BoostManagerTests(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.db = Database(Path(self.tmp.name) / "boost.db")
        self.config = SimpleNamespace(
            ADMIN_CHAT_IDS=[100, 200],
            TWIBOOST_API_KEY="secret",
            TWIBOOST_API_URL="https://twiboost.com/api/v2",
            TWIBOOST_VIEWS_SERVICE_ID=123,
            BOOST_DEFAULT_QUANTITY=500,
            BOOST_DRY_RUN=True,
            BOOST_REAL_ORDERS_ENABLED=False,
        )

    def tearDown(self):
        self.tmp.cleanup()

    def test_default_settings_are_safe(self):
        settings = get_boost_settings(self.db, self.config)

        self.assertFalse(settings["boost_enabled"])
        self.assertTrue(settings["boost_dry_run"])
        self.assertFalse(settings["real_orders_enabled"])
        self.assertEqual(settings["default_quantity"], 500)
        self.assertTrue(boost_configured(self.config))
        self.assertFalse(boost_real_orders_allowed(settings, self.config))

    def test_tracked_channel_storage_is_separate(self):
        conn = self.db.connect()
        try:
            conn.execute(
                "CREATE TABLE IF NOT EXISTS channels (tg_handle TEXT UNIQUE NOT NULL, name TEXT NOT NULL)"
            )
            conn.execute("INSERT INTO channels (tg_handle, name) VALUES ('@ordinary', 'Ordinary')")
            conn.commit()
        finally:
            conn.close()

        added = add_tracked_channel("@boosted", owner_id=100, database=self.db, config=self.config)
        channels = list_tracked_channels(self.db)

        self.assertEqual(len(channels), 1)
        self.assertEqual(channels[0]["id"], added["id"])
        self.assertEqual(channels[0]["username"], "boosted")
        self.assertFalse(bool(channels[0]["enabled"]))

    def test_toggle_quantity_and_delete_tracked_channel(self):
        added = add_tracked_channel("https://t.me/Boosted", owner_id=100, database=self.db, config=self.config)

        enabled = set_tracked_channel_enabled(added["id"], True, self.db)
        updated = set_tracked_channel_quantity(added["id"], 900, self.db)
        deleted = delete_tracked_channel(added["id"], self.db)

        self.assertTrue(bool(enabled["enabled"]))
        self.assertEqual(updated["quantity"], 900)
        self.assertTrue(deleted)
        self.assertEqual(list_tracked_channels(self.db), [])

    def test_required_env_vars_are_namespaced(self):
        names = required_env_vars()

        self.assertIn("TWIBOOST_API_KEY", names)
        self.assertIn("BOOST_REAL_ORDERS_ENABLED", names)
        self.assertNotIn("API_KEY", names)
        self.assertNotIn("SERVICE_ID", names)


class BoostDryRunEventTests(unittest.IsolatedAsyncioTestCase):
    async def asyncSetUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.db = Database(Path(self.tmp.name) / "boost.db")
        self.config = SimpleNamespace(
            TWIBOOST_API_KEY="secret",
            TWIBOOST_API_URL="https://twiboost.com/api/v2",
            TWIBOOST_VIEWS_SERVICE_ID=123,
            BOOST_DEFAULT_QUANTITY=500,
            BOOST_DRY_RUN=True,
            BOOST_REAL_ORDERS_ENABLED=False,
        )

    async def asyncTearDown(self):
        self.tmp.cleanup()

    def _message(self, username="boosted", chat_id=-1001234567890, message_id=42):
        return SimpleNamespace(
            message_id=message_id,
            chat=SimpleNamespace(id=chat_id, username=username),
        )

    async def test_tracked_enabled_channel_creates_dry_run_event(self):
        save_boost_settings({"boost_enabled": True}, self.db, self.config)
        ch = add_tracked_channel("@boosted", owner_id=100, enabled=True, database=self.db, config=self.config)

        result = await handle_boost_channel_post_dry_run(self._message(), self.db, self.config)

        self.assertEqual(result["status"], "dry_run")
        self.assertEqual(result["event"]["boost_channel_id"], ch["id"])
        self.assertEqual(result["event"]["post_url"], "https://t.me/boosted/42")
        self.assertEqual(result["event"]["quantity"], 500)

    async def test_untracked_or_disabled_or_global_off_is_ignored(self):
        self.assertEqual(
            (await handle_boost_channel_post_dry_run(self._message(), self.db, self.config))["reason"],
            "boost_disabled",
        )

        save_boost_settings({"boost_enabled": True}, self.db, self.config)
        self.assertEqual(
            (await handle_boost_channel_post_dry_run(self._message("other"), self.db, self.config))["reason"],
            "not_tracked",
        )

        add_tracked_channel("@boosted", owner_id=100, enabled=False, database=self.db, config=self.config)
        self.assertEqual(
            (await handle_boost_channel_post_dry_run(self._message(), self.db, self.config))["reason"],
            "channel_disabled",
        )


class TwiBoostClientDryRunTests(unittest.IsolatedAsyncioTestCase):
    async def test_dry_run_never_calls_http_layer(self):
        class ExplodingClient(TwiBoostClientWrapper):
            def _request_sync(self, params):
                raise AssertionError("dry-run must not call HTTP")

        client = ExplodingClient(api_key="secret", api_url="https://example.test/api", service_id=123)

        result = await client.create_views_order("https://t.me/example/1", quantity=500, dry_run=True)

        self.assertTrue(result["dry_run"])
        self.assertTrue(result["would_create_order"])
        self.assertEqual(result["request"]["action"], "add")
        self.assertEqual(result["request"]["service"], 123)

    async def test_real_order_without_config_returns_error(self):
        client = TwiBoostClientWrapper(api_key="", api_url="", service_id=0)

        result = await client.create_views_order("https://t.me/example/1", dry_run=False)

        self.assertEqual(result["error"], "twiboost_not_configured")
        self.assertFalse(result["configured"])


if __name__ == "__main__":
    unittest.main()
