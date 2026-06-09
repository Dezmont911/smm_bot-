import tempfile
import unittest
from pathlib import Path
from types import SimpleNamespace
from unittest.mock import AsyncMock, Mock, patch

from database import Database
import poster as poster_module
from boost_manager import (
    TwiBoostClientWrapper,
    add_tracked_channel,
    add_tracked_channel_from_smm_channel,
    build_telegram_post_url,
    boost_configured,
    boost_real_orders_allowed,
    delete_tracked_channel,
    get_boost_settings,
    handle_boost_channel_post_dry_run,
    list_boost_events,
    list_tracked_channels,
    parse_boost_quantity,
    required_env_vars,
    save_boost_settings,
    select_boost_quantity,
    set_tracked_channel_enabled,
    set_tracked_channel_quantity,
    validate_boost_quantity,
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
        self.assertEqual(settings["default_service_id"], "123")
        self.assertTrue(boost_configured(self.config))
        self.assertFalse(boost_real_orders_allowed(settings, self.config))

    def test_settings_default_service_id_falls_back_to_env_config(self):
        missing_service_config = SimpleNamespace(**{**self.config.__dict__, "TWIBOOST_VIEWS_SERVICE_ID": 0})
        save_boost_settings({"boost_enabled": True, "default_service_id": None}, self.db, missing_service_config)

        settings = get_boost_settings(self.db, self.config)

        self.assertEqual(settings["default_service_id"], "123")

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

    def test_delete_tracked_channel_removes_existing_orders_first(self):
        added = add_tracked_channel("https://t.me/Boosted", owner_id=100, database=self.db, config=self.config)
        conn = self.db.connect()
        try:
            conn.execute(
                """
                INSERT INTO boost_orders (
                    boost_channel_id, tg_chat_id, message_id, event_key, event_type,
                    post_url, quantity, service_id, status, dry_run, created_at, updated_at
                )
                VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
                """,
                (
                    added["id"],
                    "-1001234567890",
                    42,
                    "msg:42",
                    "text",
                    "https://t.me/boosted/42",
                    500,
                    "123",
                    "dry_run",
                    1,
                    "2026-06-07T00:00:00",
                    "2026-06-07T00:00:00",
                ),
            )
            conn.commit()
        finally:
            conn.close()

        deleted = delete_tracked_channel(added["id"], self.db)

        self.assertTrue(deleted)
        conn = self.db.connect()
        try:
            orders = conn.execute("SELECT COUNT(*) FROM boost_orders").fetchone()[0]
            channels = conn.execute("SELECT COUNT(*) FROM boost_channels").fetchone()[0]
        finally:
            conn.close()
        self.assertEqual(orders, 0)
        self.assertEqual(channels, 0)

    def test_duplicate_manual_add_does_not_reset_existing_settings(self):
        added = add_tracked_channel("@Boosted", owner_id=100, quantity=900, database=self.db, config=self.config)
        set_tracked_channel_enabled(added["id"], True, self.db)

        duplicate = add_tracked_channel("https://t.me/boosted", owner_id=100, database=self.db, config=self.config)
        channels = list_tracked_channels(self.db)

        self.assertEqual(len(channels), 1)
        self.assertEqual(duplicate["id"], added["id"])
        self.assertEqual(duplicate["quantity"], 900)
        self.assertTrue(bool(duplicate["enabled"]))

    def test_add_from_existing_smm_channel_links_snapshot(self):
        smm_channel = {
            "channel_id": "@boosted",
            "name": "Boosted Channel",
            "username": "boosted",
            "chat_id_num": -1001234567890,
            "owner_id": 100,
            "active": True,
        }

        added, created = add_tracked_channel_from_smm_channel(
            smm_channel,
            owner_id=smm_channel["owner_id"],
            quantity=750,
            database=self.db,
            config=self.config,
        )

        self.assertTrue(created)
        self.assertEqual(added["smm_channel_id"], "@boosted")
        self.assertEqual(added["owner_id"], 100)
        self.assertEqual(added["tg_chat_id"], "-1001234567890")
        self.assertEqual(added["username"], "boosted")
        self.assertEqual(added["title"], "Boosted Channel")
        self.assertEqual(added["quantity"], 750)
        self.assertFalse(bool(added["enabled"]))

    def test_add_from_existing_smm_channel_links_existing_manual_record(self):
        manual = add_tracked_channel("@boosted", owner_id=100, quantity=900, database=self.db, config=self.config)
        smm_channel = {
            "channel_id": "@boosted",
            "name": "Boosted Channel",
            "username": "boosted",
            "chat_id_num": -1001234567890,
            "owner_id": 100,
            "active": True,
        }

        linked, created = add_tracked_channel_from_smm_channel(
            smm_channel,
            owner_id=smm_channel["owner_id"],
            quantity=750,
            database=self.db,
            config=self.config,
        )
        channels = list_tracked_channels(self.db)

        self.assertFalse(created)
        self.assertEqual(len(channels), 1)
        self.assertEqual(linked["id"], manual["id"])
        self.assertEqual(linked["smm_channel_id"], "@boosted")
        self.assertEqual(linked["tg_chat_id"], "-1001234567890")
        self.assertEqual(linked["title"], "Boosted Channel")
        self.assertEqual(linked["quantity"], 900)

    def test_smm_first_then_external_username_does_not_duplicate(self):
        smm_channel = {
            "channel_id": "@boosted",
            "name": "Boosted Channel",
            "username": "boosted",
            "chat_id_num": -1001234567890,
            "owner_id": 100,
            "active": True,
        }
        added, created = add_tracked_channel_from_smm_channel(
            smm_channel,
            owner_id=smm_channel["owner_id"],
            quantity=750,
            database=self.db,
            config=self.config,
        )

        duplicate = add_tracked_channel("@boosted", owner_id=100, database=self.db, config=self.config)
        duplicate_link = add_tracked_channel("https://t.me/Boosted", owner_id=100, database=self.db, config=self.config)
        channels = list_tracked_channels(self.db)

        self.assertTrue(created)
        self.assertEqual(len(channels), 1)
        self.assertEqual(duplicate["id"], added["id"])
        self.assertEqual(duplicate_link["id"], added["id"])
        self.assertEqual(channels[0]["channel_key"], "chat:-1001234567890")
        self.assertEqual(channels[0]["username"], "boosted")
        self.assertEqual(channels[0]["smm_channel_id"], "@boosted")

    def test_external_first_then_smm_backfills_snapshot_without_duplicate(self):
        manual = add_tracked_channel("@boosted", owner_id=100, quantity=900, database=self.db, config=self.config)
        smm_channel = {
            "channel_id": "@boosted",
            "name": "Boosted Channel",
            "username": "boosted",
            "chat_id_num": -1001234567890,
            "owner_id": 100,
            "active": True,
        }

        linked, created = add_tracked_channel_from_smm_channel(
            smm_channel,
            owner_id=smm_channel["owner_id"],
            quantity=750,
            database=self.db,
            config=self.config,
        )
        channels = list_tracked_channels(self.db)

        self.assertFalse(created)
        self.assertEqual(len(channels), 1)
        self.assertEqual(linked["id"], manual["id"])
        self.assertEqual(linked["tg_chat_id"], "-1001234567890")
        self.assertEqual(linked["username"], "boosted")
        self.assertEqual(linked["title"], "Boosted Channel")

    def test_quantity_validation_rejects_bad_values_without_mutating(self):
        added = add_tracked_channel("@boosted", owner_id=100, quantity=900, database=self.db, config=self.config)

        for bad in ("abc", "0", "-5", "499", "1-600", "100001"):
            with self.subTest(bad=bad):
                with self.assertRaises(ValueError):
                    set_tracked_channel_quantity(added["id"], bad, self.db)

        current = list_tracked_channels(self.db)[0]
        self.assertEqual(current["quantity"], 900)
        self.assertEqual(validate_boost_quantity("100000"), 100000)

    def test_parse_boost_quantity_fixed_and_ranges(self):
        self.assertEqual(
            parse_boost_quantity("500", self.config),
            {"quantity_min": 500, "quantity_max": 500, "quantity_display": "500"},
        )
        self.assertEqual(
            parse_boost_quantity("500-550", self.config),
            {"quantity_min": 500, "quantity_max": 550, "quantity_display": "500–550"},
        )
        self.assertEqual(
            parse_boost_quantity("600-700", self.config),
            {"quantity_min": 600, "quantity_max": 700, "quantity_display": "600–700"},
        )
        self.assertEqual(
            parse_boost_quantity("600 – 610", self.config),
            {"quantity_min": 600, "quantity_max": 610, "quantity_display": "600–610"},
        )
        self.assertEqual(validate_boost_quantity("100000"), 100000)

    def test_parse_boost_quantity_rejects_invalid_inputs(self):
        for bad in ("", "0", "-1", "499", "1-600", "600-", "-600", "600--700", "600abc", "600/700", "600,700", "600.700", "0-100", "700-600", "100001"):
            with self.subTest(bad=bad):
                with self.assertRaises(ValueError):
                    parse_boost_quantity(bad, self.config)

    def test_quantity_selection_fixed_and_range(self):
        self.assertEqual(select_boost_quantity(600, 600), 600)
        self.assertEqual(select_boost_quantity(600, 610, rng=lambda low, high: high), 610)

    def test_quantity_range_storage_and_legacy_fixed_compatibility(self):
        legacy = add_tracked_channel("@legacy", owner_id=100, quantity=600, database=self.db, config=self.config)
        ranged = add_tracked_channel("@ranged", owner_id=100, quantity="650-750", database=self.db, config=self.config)

        self.assertEqual(legacy["quantity"], 600)
        self.assertEqual(legacy["quantity_min"], 600)
        self.assertEqual(legacy["quantity_max"], 600)
        self.assertEqual(legacy["quantity_display"], "600")
        self.assertEqual(ranged["quantity"], 650)
        self.assertEqual(ranged["quantity_min"], 650)
        self.assertEqual(ranged["quantity_max"], 750)
        self.assertEqual(ranged["quantity_display"], "650–750")

    def test_manual_external_inputs_still_work(self):
        username = add_tracked_channel("@external", owner_id=100, quantity=500, database=self.db, config=self.config)
        link = add_tracked_channel("https://t.me/external2", owner_id=100, quantity=600, database=self.db, config=self.config)
        private = add_tracked_channel("-1001234567890", owner_id=100, quantity=700, database=self.db, config=self.config)

        self.assertEqual(username["username"], "external")
        self.assertEqual(link["username"], "external2")
        self.assertEqual(private["tg_chat_id"], "-1001234567890")
        self.assertIsNone(private["smm_channel_id"])

    def test_required_env_vars_are_namespaced(self):
        names = required_env_vars()

        self.assertIn("TWIBOOST_API_KEY", names)
        self.assertIn("TWIBOOST_VIEWS_SERVICE_ID", names)
        self.assertIn("BOOST_REAL_ORDERS_ENABLED", names)
        self.assertNotIn("API_KEY", names)
        self.assertNotIn("SERVICE_ID", names)
        self.assertNotIn("BOOST_SERVICE_ID", names)
        self.assertNotIn("SMM_SERVICE_ID", names)


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

    def _message(
        self,
        username="boosted",
        chat_id=-1001234567890,
        message_id=42,
        media_group_id=None,
        **fields,
    ):
        content_keys = {"text", "caption", "photo", "video", "animation", "document", "audio", "voice", "video_note", "sticker"}
        if not any(key in fields for key in content_keys):
            fields["text"] = "plain text"
        return SimpleNamespace(
            message_id=message_id,
            media_group_id=media_group_id,
            chat=SimpleNamespace(id=chat_id, username=username),
            **fields,
        )

    def _count_events(self):
        conn = self.db.connect()
        try:
            return conn.execute("SELECT COUNT(*) FROM boost_orders").fetchone()[0]
        finally:
            conn.close()

    async def _await_album_result(self, *results):
        task = next((result.get("task") for result in results if result.get("task")), None)
        self.assertIsNotNone(task)
        return await task

    async def test_tracked_enabled_channel_creates_dry_run_event(self):
        save_boost_settings({"boost_enabled": True}, self.db, self.config)
        ch = add_tracked_channel("@boosted", owner_id=100, enabled=True, database=self.db, config=self.config)

        result = await handle_boost_channel_post_dry_run(self._message(), self.db, self.config)

        self.assertEqual(result["status"], "dry_run")
        self.assertEqual(result["event"]["boost_channel_id"], ch["id"])
        self.assertEqual(result["event"]["post_url"], "https://t.me/boosted/42")
        self.assertEqual(result["event"]["event_key"], "msg:42")
        self.assertEqual(result["event"]["canonical_message_id"], 42)
        self.assertEqual(result["event"]["event_type"], "text")
        self.assertEqual(result["event"]["reason_code"], "public_username")
        self.assertEqual(result["event"]["quantity"], 500)

    async def test_untracked_or_disabled_or_global_off_is_ignored(self):
        add_tracked_channel("@boosted", owner_id=100, enabled=True, database=self.db, config=self.config)
        self.assertEqual(
            (await handle_boost_channel_post_dry_run(self._message(), self.db, self.config))["reason"],
            "boost_global_disabled",
        )
        self.assertEqual(self._count_events(), 0)

        save_boost_settings({"boost_enabled": True}, self.db, self.config)
        self.assertEqual(
            (await handle_boost_channel_post_dry_run(self._message("other"), self.db, self.config))["reason"],
            "not_tracked",
        )

        set_tracked_channel_enabled(list_tracked_channels(self.db)[0]["id"], False, self.db)
        self.assertEqual(
            (await handle_boost_channel_post_dry_run(self._message(message_id=43), self.db, self.config))["reason"],
            "boost_channel_disabled",
        )
        self.assertEqual(self._count_events(), 0)

    async def test_post_url_helper_requires_public_username(self):
        channel = {"username": "boosted", "tg_chat_id": "-1001234567890"}
        public_result = build_telegram_post_url(channel, self._message(caption="https://wrong.example/post"))

        self.assertTrue(public_result["ok"])
        self.assertEqual(public_result["post_url"], "https://t.me/boosted/42")
        self.assertEqual(public_result["reason_code"], "public_username")
        self.assertTrue(public_result["is_public"])

        private_channel = {"username": None, "tg_chat_id": "-1001234567890"}
        private_result = build_telegram_post_url(private_channel, self._message(username=None))

        self.assertFalse(private_result["ok"])
        self.assertIsNone(private_result["post_url"])
        self.assertEqual(private_result["reason_code"], "no_public_post_url")
        self.assertFalse(private_result["is_public"])

    async def test_text_photo_and_video_posts_create_dry_run_events(self):
        save_boost_settings({"boost_enabled": True}, self.db, self.config)
        add_tracked_channel("@boosted", owner_id=100, enabled=True, database=self.db, config=self.config)

        variants = (
            ("text", {"text": "plain text"}),
            ("photo", {"photo": [object()], "caption": None}),
            ("photo", {"photo": [object()], "caption": "caption with https://wrong.example"}),
            ("video", {"video": object(), "caption": None}),
            ("video", {"video": object(), "caption": "video caption"}),
        )
        for index, (event_type, fields) in enumerate(variants, start=1):
            with self.subTest(fields=fields):
                message_id = 100 + index

                result = await handle_boost_channel_post_dry_run(
                    self._message(message_id=message_id, **fields),
                    self.db,
                    self.config,
                )

                self.assertEqual(result["status"], "dry_run")
                self.assertEqual(result["event"]["post_url"], f"https://t.me/boosted/{message_id}")
                self.assertEqual(result["event"]["event_type"], event_type)
                self.assertEqual(result["request"]["request"]["link"], f"https://t.me/boosted/{message_id}")
                self.assertEqual(self._count_events(), index)

    async def test_range_quantity_event_stores_chosen_quantity(self):
        save_boost_settings({"boost_enabled": True}, self.db, self.config)
        add_tracked_channel("@boosted", owner_id=100, quantity="600-610", enabled=True, database=self.db, config=self.config)

        result = await handle_boost_channel_post_dry_run(
            self._message(message_id=200, photo=[object()]),
            self.db,
            self.config,
        )

        self.assertGreaterEqual(result["event"]["quantity"], 600)
        self.assertLessEqual(result["event"]["quantity"], 610)
        self.assertEqual(result["request"]["request"]["quantity"], result["event"]["quantity"])

    async def test_private_tracked_channel_records_review_event_without_client_call(self):
        class ExplodingDryRunClient(TwiBoostClientWrapper):
            async def create_views_order(self, *args, **kwargs):
                raise AssertionError("private post must not be sent to TwiBoost dry-run wrapper")

        save_boost_settings({"boost_enabled": True}, self.db, self.config)
        add_tracked_channel("-1001234567890", owner_id=100, enabled=True, database=self.db, config=self.config)

        result = await handle_boost_channel_post_dry_run(
            self._message(username=None),
            self.db,
            self.config,
            client=ExplodingDryRunClient(config=self.config),
        )

        self.assertEqual(result["status"], "ignored")
        self.assertEqual(result["reason"], "no_public_post_url")
        self.assertIsNone(result["event"]["post_url"])
        self.assertEqual(result["event"]["reason_code"], "no_public_post_url")
        self.assertEqual(result["event"]["error"], "no_public_post_url")
        self.assertEqual(self._count_events(), 1)

    async def test_media_group_is_idempotent_by_group_and_service(self):
        save_boost_settings({"boost_enabled": True}, self.db, self.config)
        add_tracked_channel("@boosted", owner_id=100, enabled=True, database=self.db, config=self.config)

        first = await handle_boost_channel_post_dry_run(
            self._message(message_id=10, media_group_id="album-1", photo=[object()]),
            self.db,
            self.config,
            album_settle_seconds=0.01,
        )
        second = await handle_boost_channel_post_dry_run(
            self._message(message_id=11, media_group_id="album-1", photo=[object()], caption="album caption"),
            self.db,
            self.config,
            album_settle_seconds=0.01,
        )
        final = await self._await_album_result(first, second)
        duplicate = await handle_boost_channel_post_dry_run(
            self._message(message_id=11, media_group_id="album-1", photo=[object()], caption="album caption"),
            self.db,
            self.config,
            album_settle_seconds=0.01,
        )

        self.assertEqual(first["status"], "pending")
        self.assertEqual(second["status"], "pending")
        self.assertEqual(final["status"], "dry_run")
        self.assertEqual(final["event"]["event_key"], "mg:album-1")
        self.assertEqual(final["event"]["media_group_id"], "album-1")
        self.assertEqual(final["event"]["canonical_message_id"], 10)
        self.assertEqual(final["event"]["event_type"], "media_group")
        self.assertEqual(duplicate["status"], "duplicate")
        self.assertEqual(duplicate["reason"], "already_has_event")
        self.assertEqual(duplicate["event"]["canonical_message_id"], 10)
        self.assertEqual(self._count_events(), 1)

    async def test_media_group_duplicate_can_correct_album_main_link(self):
        save_boost_settings({"boost_enabled": True}, self.db, self.config)
        add_tracked_channel("@boosted", owner_id=100, enabled=True, database=self.db, config=self.config)

        first = await handle_boost_channel_post_dry_run(
            self._message(message_id=2042, media_group_id="album-out-of-order", photo=[object()]),
            self.db,
            self.config,
            album_settle_seconds=0.01,
        )
        second = await handle_boost_channel_post_dry_run(
            self._message(message_id=2041, media_group_id="album-out-of-order", photo=[object()], caption="album text"),
            self.db,
            self.config,
            album_settle_seconds=0.01,
        )
        final = await self._await_album_result(first, second)

        self.assertEqual(first["status"], "pending")
        self.assertEqual(second["status"], "pending")
        self.assertEqual(final["status"], "dry_run")
        self.assertEqual(final["event"]["message_id"], 2041)
        self.assertEqual(final["event"]["canonical_message_id"], 2041)
        self.assertEqual(final["event"]["post_url"], "https://t.me/boosted/2041")
        self.assertEqual(self._count_events(), 1)

    async def test_list_boost_events_returns_latest_with_channel_snapshot(self):
        save_boost_settings({"boost_enabled": True}, self.db, self.config)
        ch = add_tracked_channel(
            "@boosted",
            owner_id=100,
            title="Boosted Channel",
            enabled=True,
            database=self.db,
            config=self.config,
        )

        await handle_boost_channel_post_dry_run(self._message(message_id=50, text="first"), self.db, self.config)
        await handle_boost_channel_post_dry_run(self._message(message_id=51, photo=[object()]), self.db, self.config)

        events = list_boost_events(limit=10, database=self.db)

        self.assertEqual(len(events), 2)
        self.assertEqual(events[0]["message_id"], 51)
        self.assertEqual(events[0]["event_type"], "photo")
        self.assertEqual(events[0]["boost_channel_id"], ch["id"])
        self.assertEqual(events[0]["channel_username"], "boosted")
        self.assertEqual(events[0]["channel_title"], "Boosted Channel")
        self.assertEqual(events[1]["message_id"], 50)

    async def test_real_order_disabled_flag_uses_dry_run_only(self):
        class RecordingClient:
            api_key = "secret"
            api_url = "https://twiboost.example/api"

            def __init__(self):
                self.calls = []

            async def create_views_order(self, post_url, quantity, service_id, dry_run=True):
                self.calls.append({"post_url": post_url, "quantity": quantity, "service_id": service_id, "dry_run": dry_run})
                return {"dry_run": dry_run, "would_create_order": True, "request": {"link": post_url, "quantity": quantity}}

        save_boost_settings({"boost_enabled": True}, self.db, self.config)
        add_tracked_channel("@boosted", owner_id=100, enabled=True, database=self.db, config=self.config)
        client = RecordingClient()

        result = await handle_boost_channel_post_dry_run(self._message(message_id=60), self.db, self.config, client=client)

        self.assertEqual(result["status"], "dry_run")
        self.assertEqual(client.calls[0]["dry_run"], True)

    async def test_real_order_dry_run_flag_uses_dry_run_only(self):
        config = SimpleNamespace(**{**self.config.__dict__, "BOOST_REAL_ORDERS_ENABLED": True, "BOOST_DRY_RUN": True})

        class RecordingClient:
            api_key = "secret"
            api_url = "https://twiboost.example/api"

            def __init__(self):
                self.calls = []

            async def create_views_order(self, post_url, quantity, service_id, dry_run=True):
                self.calls.append(dry_run)
                return {"dry_run": dry_run, "would_create_order": True, "request": {"link": post_url, "quantity": quantity}}

        save_boost_settings({"boost_enabled": True}, self.db, config)
        add_tracked_channel("@boosted", owner_id=100, enabled=True, database=self.db, config=config)
        client = RecordingClient()

        result = await handle_boost_channel_post_dry_run(self._message(message_id=61), self.db, config, client=client)

        self.assertEqual(result["status"], "dry_run")
        self.assertEqual(client.calls, [True])

    async def test_missing_api_key_creates_config_event_without_client_call(self):
        class ExplodingClient:
            api_key = ""
            api_url = "https://twiboost.example/api"

            async def create_views_order(self, *args, **kwargs):
                raise AssertionError("missing API key must not call provider")

        config = SimpleNamespace(**{**self.config.__dict__, "TWIBOOST_API_KEY": "", "BOOST_REAL_ORDERS_ENABLED": True, "BOOST_DRY_RUN": False})
        save_boost_settings({"boost_enabled": True}, self.db, config)
        add_tracked_channel("@boosted", owner_id=100, enabled=True, database=self.db, config=config)

        result = await handle_boost_channel_post_dry_run(self._message(message_id=62), self.db, config, client=ExplodingClient())

        self.assertEqual(result["status"], "ignored")
        self.assertEqual(result["reason"], "twiboost_not_configured")
        self.assertEqual(result["event"]["reason_code"], "twiboost_not_configured")

    async def test_missing_service_id_creates_config_event_without_client_call(self):
        class ExplodingClient:
            api_key = "secret"
            api_url = "https://twiboost.example/api"

            async def create_views_order(self, *args, **kwargs):
                raise AssertionError("missing service_id must not call provider")

        config = SimpleNamespace(**{**self.config.__dict__, "TWIBOOST_VIEWS_SERVICE_ID": 0, "BOOST_REAL_ORDERS_ENABLED": True, "BOOST_DRY_RUN": False})
        save_boost_settings({"boost_enabled": True, "default_service_id": None}, self.db, config)
        add_tracked_channel("@boosted", owner_id=100, service_id=None, enabled=True, database=self.db, config=config)

        result = await handle_boost_channel_post_dry_run(self._message(message_id=64), self.db, config, client=ExplodingClient())

        self.assertEqual(result["status"], "ignored")
        self.assertEqual(result["reason"], "missing_service_id")
        self.assertEqual(result["event"]["reason_code"], "missing_service_id")

    async def test_real_order_all_flags_true_calls_provider_once(self):
        class RecordingClient:
            api_key = "secret"
            api_url = "https://twiboost.example/api"

            def __init__(self):
                self.calls = []

            async def create_views_order(self, post_url, quantity, service_id, dry_run=True):
                self.calls.append({"dry_run": dry_run, "quantity": quantity, "service_id": service_id})
                return {"order": 98765}

        config = SimpleNamespace(**{**self.config.__dict__, "BOOST_REAL_ORDERS_ENABLED": True, "BOOST_DRY_RUN": False})
        save_boost_settings({"boost_enabled": True}, self.db, config)
        add_tracked_channel("@boosted", owner_id=100, enabled=True, database=self.db, config=config)
        client = RecordingClient()

        result = await handle_boost_channel_post_dry_run(self._message(message_id=63), self.db, config, client=client)

        self.assertEqual(result["status"], "ordered")
        self.assertEqual(client.calls, [{"dry_run": False, "quantity": 500, "service_id": "123"}])
        self.assertEqual(result["event"]["provider_order_id"], "98765")
        self.assertFalse(bool(result["event"]["dry_run"]))

    async def test_real_order_prefers_channel_service_id_over_env_default(self):
        class RecordingClient:
            api_key = "secret"
            api_url = "https://twiboost.example/api"

            def __init__(self):
                self.calls = []

            async def create_views_order(self, post_url, quantity, service_id, dry_run=True):
                self.calls.append({"dry_run": dry_run, "service_id": service_id})
                return {"order": 12345}

        config = SimpleNamespace(**{**self.config.__dict__, "BOOST_REAL_ORDERS_ENABLED": True, "BOOST_DRY_RUN": False})
        save_boost_settings({"boost_enabled": True}, self.db, config)
        add_tracked_channel("@boosted", owner_id=100, service_id=456, enabled=True, database=self.db, config=config)
        client = RecordingClient()

        result = await handle_boost_channel_post_dry_run(self._message(message_id=65), self.db, config, client=client)

        self.assertEqual(result["status"], "ordered")
        self.assertEqual(client.calls, [{"dry_run": False, "service_id": "456"}])

    async def test_real_order_uses_env_service_id_when_channel_service_id_empty(self):
        class RecordingClient:
            api_key = "secret"
            api_url = "https://twiboost.example/api"

            def __init__(self):
                self.calls = []

            async def create_views_order(self, post_url, quantity, service_id, dry_run=True):
                self.calls.append({"dry_run": dry_run, "service_id": service_id})
                return {"order": 12346}

        config = SimpleNamespace(**{**self.config.__dict__, "BOOST_REAL_ORDERS_ENABLED": True, "BOOST_DRY_RUN": False})
        save_boost_settings({"boost_enabled": True, "default_service_id": None}, self.db, config)
        add_tracked_channel("@boosted", owner_id=100, service_id=None, enabled=True, database=self.db, config=config)
        client = RecordingClient()

        result = await handle_boost_channel_post_dry_run(self._message(message_id=66), self.db, config, client=client)

        self.assertEqual(result["status"], "ordered")
        self.assertEqual(client.calls, [{"dry_run": False, "service_id": "123"}])

    async def test_real_order_media_group_duplicate_calls_provider_once(self):
        class RecordingClient:
            api_key = "secret"
            api_url = "https://twiboost.example/api"

            def __init__(self):
                self.calls = 0

            async def create_views_order(self, post_url, quantity, service_id, dry_run=True):
                self.calls += 1
                return {"order": self.calls}

        config = SimpleNamespace(**{**self.config.__dict__, "BOOST_REAL_ORDERS_ENABLED": True, "BOOST_DRY_RUN": False})
        save_boost_settings({"boost_enabled": True}, self.db, config)
        add_tracked_channel("@boosted", owner_id=100, enabled=True, database=self.db, config=config)
        client = RecordingClient()

        first = await handle_boost_channel_post_dry_run(
            self._message(message_id=70, media_group_id="album-real", photo=[object()]),
            self.db,
            config,
            client=client,
            album_settle_seconds=0.01,
        )
        second = await handle_boost_channel_post_dry_run(
            self._message(message_id=71, media_group_id="album-real", photo=[object()]),
            self.db,
            config,
            client=client,
            album_settle_seconds=0.01,
        )
        final = await self._await_album_result(first, second)
        duplicate = await handle_boost_channel_post_dry_run(
            self._message(message_id=71, media_group_id="album-real", photo=[object()]),
            self.db,
            config,
            client=client,
            album_settle_seconds=0.01,
        )

        self.assertEqual(first["status"], "pending")
        self.assertEqual(second["status"], "pending")
        self.assertEqual(final["status"], "ordered")
        self.assertEqual(duplicate["status"], "duplicate")
        self.assertEqual(client.calls, 1)

    async def test_real_order_album_out_of_order_uses_lowest_message_link_once(self):
        class RecordingClient:
            api_key = "secret"
            api_url = "https://twiboost.example/api"

            def __init__(self):
                self.calls = []

            async def create_views_order(self, post_url, quantity, service_id, dry_run=True):
                self.calls.append(post_url)
                return {"order": len(self.calls)}

        config = SimpleNamespace(**{**self.config.__dict__, "BOOST_REAL_ORDERS_ENABLED": True, "BOOST_DRY_RUN": False})
        save_boost_settings({"boost_enabled": True}, self.db, config)
        add_tracked_channel("@boosted", owner_id=100, enabled=True, database=self.db, config=config)
        client = RecordingClient()

        first = await handle_boost_channel_post_dry_run(
            self._message(message_id=2042, media_group_id="album-real-out-of-order", photo=[object()]),
            self.db,
            config,
            client=client,
            album_settle_seconds=0.01,
        )
        second = await handle_boost_channel_post_dry_run(
            self._message(message_id=2041, media_group_id="album-real-out-of-order", photo=[object()]),
            self.db,
            config,
            client=client,
            album_settle_seconds=0.01,
        )
        final = await self._await_album_result(first, second)

        self.assertEqual(first["status"], "pending")
        self.assertEqual(second["status"], "pending")
        self.assertEqual(final["status"], "ordered")
        self.assertEqual(client.calls, ["https://t.me/boosted/2041"])
        self.assertEqual(final["event"]["post_url"], "https://t.me/boosted/2041")
        self.assertEqual(final["event"]["canonical_message_id"], 2041)

    async def test_empty_channel_post_after_album_is_ignored_without_provider_call(self):
        class RecordingClient:
            api_key = "secret"
            api_url = "https://twiboost.example/api"

            def __init__(self):
                self.calls = []

            async def create_views_order(self, post_url, quantity, service_id, dry_run=True):
                self.calls.append(post_url)
                return {"order": len(self.calls)}

        config = SimpleNamespace(**{**self.config.__dict__, "BOOST_REAL_ORDERS_ENABLED": True, "BOOST_DRY_RUN": False})
        save_boost_settings({"boost_enabled": True}, self.db, config)
        add_tracked_channel("@boosted", owner_id=100, enabled=True, database=self.db, config=config)
        client = RecordingClient()

        first = await handle_boost_channel_post_dry_run(
            self._message(message_id=2056, media_group_id="album-empty-tail", photo=[object()]),
            self.db,
            config,
            client=client,
            album_settle_seconds=0.01,
        )
        second = await handle_boost_channel_post_dry_run(
            self._message(message_id=2059, media_group_id="album-empty-tail", photo=[object()]),
            self.db,
            config,
            client=client,
            album_settle_seconds=0.01,
        )
        final = await self._await_album_result(first, second)
        phantom = await handle_boost_channel_post_dry_run(
            self._message(message_id=2060, text=None),
            self.db,
            config,
            client=client,
        )

        self.assertEqual(final["status"], "ordered")
        self.assertEqual(final["event"]["post_url"], "https://t.me/boosted/2056")
        self.assertEqual(phantom["status"], "ignored")
        self.assertEqual(phantom["reason"], "no_boostable_content")
        self.assertEqual(client.calls, ["https://t.me/boosted/2056"])
        self.assertEqual(self._count_events(), 1)


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

    async def test_real_order_http_path_uses_stdlib_urlopen(self):
        captured = {}

        class FakeResponse:
            status = 200

            def __enter__(self):
                return self

            def __exit__(self, exc_type, exc, tb):
                return False

            def read(self):
                return b'{"order": 321}'

        def fake_urlopen(url, timeout):
            captured["url"] = url
            captured["timeout"] = timeout
            return FakeResponse()

        client = TwiBoostClientWrapper(api_key="secret", api_url="https://example.test/api", service_id=123, timeout=7)

        with patch("urllib.request.urlopen", side_effect=fake_urlopen):
            result = await client.create_views_order("https://t.me/example/1", quantity=500, dry_run=False)

        self.assertEqual(result["order"], 321)
        self.assertEqual(captured["timeout"], 7)
        self.assertIn("action=add", captured["url"])
        self.assertIn("service=123", captured["url"])
        self.assertIn("quantity=500", captured["url"])


class PosterBoostIntegrationTests(unittest.IsolatedAsyncioTestCase):
    async def test_post_now_sends_published_bot_post_to_boost(self):
        sent_message = SimpleNamespace(
            message_id=5375,
            chat=SimpleNamespace(id=-1001753440231, username="history_loopa"),
            text="published text",
            caption=None,
            entities=[],
            caption_entities=[],
            photo=None,
            video=None,
            media_group_id=None,
        )
        fake_bot = SimpleNamespace(send_message=AsyncMock(return_value=sent_message))
        fake_post = {
            "id": "post-1",
            "channel_id": "@history_loopa",
            "content": "published text",
            "format": "text",
            "topic": "topic",
            "parse_mode": "HTML",
        }
        fake_buffer = SimpleNamespace(
            get_next=Mock(return_value=fake_post),
            mark_published=Mock(),
        )
        poster = poster_module.Poster()
        poster.set_bot(fake_bot)
        poster.record_published = Mock()
        poster._load_channel_by_id = Mock(return_value={"chat_id_num": -1001753440231})

        with patch.object(poster_module, "buffer", fake_buffer), \
                patch("boost_manager.handle_boost_channel_post_dry_run", new=AsyncMock(return_value={"status": "ordered", "event": {"id": 42}})) as boost_handler:
            result = await poster.post_now("@history_loopa")

        self.assertTrue(result["success"])
        fake_buffer.mark_published.assert_called_once_with("post-1")
        poster.record_published.assert_called_once_with("@history_loopa")
        boost_handler.assert_awaited_once()
        args, kwargs = boost_handler.await_args
        self.assertIs(args[0], sent_message)
        self.assertFalse(kwargs["defer_media_groups"])


if __name__ == "__main__":
    unittest.main()
