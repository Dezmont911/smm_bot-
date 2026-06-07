import unittest
import json
import tempfile
from pathlib import Path
from types import SimpleNamespace
from unittest.mock import AsyncMock, patch

import ui
from database import Database


class FakeQuery:
    def __init__(self, user_id=100):
        self.from_user = SimpleNamespace(id=user_id)
        self.answers = []
        self.edits = []

    async def answer(self, text=None, show_alert=False):
        self.answers.append((text, show_alert))

    async def edit_message_text(self, text, parse_mode=None, reply_markup=None):
        self.edits.append((text, parse_mode, reply_markup))


class BoostUiTests(unittest.IsolatedAsyncioTestCase):
    def test_clear_boost_pending_only_removes_boost_state(self):
        context = SimpleNamespace(user_data={
            "boost_add_channel": True,
            "boost_add_external_channel": "@old",
            "boost_add_smm_channel_id": "@old",
            "boost_set_quantity_for": 1,
            "other": "keep",
        })

        ui._clear_boost_pending(context)

        self.assertEqual(context.user_data, {"other": "keep"})

    async def test_picker_uses_owner_scoped_mine_channels(self):
        captured = {}

        def fake_load_channels(include_inactive=False, owner_id=None, scope=None):
            captured["include_inactive"] = include_inactive
            captured["owner_id"] = owner_id
            captured["scope"] = scope
            return [
                {
                    "channel_id": "@mine",
                    "name": "Mine",
                    "username": "mine",
                    "owner_id": 100,
                    "active": True,
                }
            ]

        async def fake_answer_or_send(qm, text, kb):
            captured["text"] = text
            captured["kb"] = kb

        with patch.object(ui.accounts, "is_superadmin", return_value=True), \
                patch.object(ui, "_load_channels", side_effect=fake_load_channels), \
                patch.object(ui, "find_tracked_channel_for_smm_channel", return_value=None), \
                patch.object(ui, "_answer_or_send", side_effect=fake_answer_or_send):
            await ui.screen_boost_smm_picker(FakeQuery(100), SimpleNamespace(user_data={}), page=0)

        self.assertTrue(captured["include_inactive"])
        self.assertEqual(captured["owner_id"], 100)
        self.assertEqual(captured["scope"], "mine")
        self.assertIn("Mine", captured["text"])

    async def test_picker_rejects_non_superadmin(self):
        query = FakeQuery(300)

        with patch.object(ui.accounts, "is_superadmin", return_value=False):
            await ui.screen_boost_smm_picker(query, SimpleNamespace(user_data={}), page=0)

        self.assertEqual(query.answers, [("Только для владельца.", True)])

    async def test_duplicate_picker_add_opens_existing_boost_channel(self):
        query = FakeQuery(100)
        context = SimpleNamespace(user_data={})
        opened = {}
        smm_channel = {
            "channel_id": "@mine",
            "name": "Mine",
            "username": "mine",
            "owner_id": 100,
            "active": True,
        }
        existing = {"id": 7, "enabled": False, "username": "mine"}

        async def fake_detail(qm, ctx, channel_id):
            opened["channel_id"] = channel_id

        with patch.object(ui.accounts, "is_superadmin", return_value=True), \
                patch.object(ui, "_load_channels", return_value=[smm_channel]), \
                patch.object(ui, "find_tracked_channel_for_smm_channel", return_value=existing), \
                patch.object(ui, "link_tracked_channel_to_smm_channel", return_value={**existing, "smm_channel_id": "@mine"}) as link_mock, \
                patch.object(ui, "screen_boost_channel_detail", side_effect=fake_detail):
            await ui.action_boost_pick_smm_channel(query, context, page=0, index=0)

        self.assertEqual(opened["channel_id"], 7)
        link_mock.assert_called_once_with(7, smm_channel, owner_id=100)
        self.assertEqual(query.answers, [("Уже добавлен в Boost.", False)])
        self.assertEqual(context.user_data, {})

    def test_boost_label_helpers_hide_machine_codes(self):
        self.assertEqual(ui._boost_status_label("dry_run"), "тестовый заказ")
        self.assertEqual(ui._boost_status_label("ignored"), "пропущено")
        self.assertEqual(ui._boost_reason_label("no_public_post_url"), "нет публичной ссылки на пост")
        self.assertEqual(ui._boost_reason_label("boost_channel_disabled"), "Boost для канала выключен")
        self.assertEqual(ui._boost_event_type_label("media_group"), "альбом")

    async def test_boost_events_screen_shows_latest_events(self):
        captured = {}
        events = [
            {
                "id": 3,
                "boost_channel_id": 7,
                "channel_username": "boosted",
                "channel_tg_chat_id": "-100123",
                "channel_title": "Boosted",
                "message_id": 42,
                "event_type": "photo",
                "quantity": 500,
                "status": "dry_run",
                "reason_code": "public_username",
                "error": None,
                "post_url": "https://t.me/boosted/42",
                "created_at": "2026-06-07T01:02:03+00:00",
            }
        ]

        async def fake_answer_or_send(qm, text, kb):
            captured["text"] = text
            captured["kb"] = kb

        with patch.object(ui.accounts, "is_superadmin", return_value=True), \
                patch.object(ui, "list_boost_events", return_value=events), \
                patch.object(ui, "_answer_or_send", side_effect=fake_answer_or_send):
            await ui.screen_boost_events(FakeQuery(100), SimpleNamespace(user_data={}))

        self.assertIn("Журнал Boost", captured["text"])
        self.assertIn("@boosted", captured["text"])
        self.assertIn("Фото", captured["text"])
        self.assertIn(ui._boost_status_label("dry_run"), captured["text"])
        self.assertIn("https://t.me/boosted/42", captured["text"])

    def test_boost_event_line_does_not_duplicate_reason_without_post_url(self):
        event = {
            "id": 8,
            "boost_channel_id": 7,
            "channel_username": "channel",
            "message_id": 2040,
            "event_type": "media_group",
            "quantity": 650,
            "status": "ignored",
            "reason_code": "no_public_post_url",
            "error": "no_public_post_url",
            "post_url": None,
            "created_at": "2026-06-06T17:56:00+00:00",
        }

        line = ui._boost_event_line(event)
        reason = ui._boost_reason_label("no_public_post_url")

        self.assertEqual(line.count(reason), 1)
        self.assertNotIn("no_public_post_url", line)

    async def test_channel_settings_has_no_boost_buttons(self):
        captured = {}
        channel = {
            "channel_id": "@mine",
            "name": "Mine",
            "topic": "Topic",
            "tone": "Tone",
            "active": True,
            "post_times_utc": [],
            "channel_type": "content",
            "topic_source": "search",
        }

        async def fake_answer_or_send(qm, text, kb):
            captured["kb"] = kb

        with patch.object(ui, "_load_channel", return_value=channel), \
                patch.object(ui, "_answer_or_send", side_effect=fake_answer_or_send):
            await ui.screen_channel_settings(FakeQuery(100), SimpleNamespace(user_data={}), "@mine")

        buttons = [button for row in captured["kb"].inline_keyboard for button in row]
        labels = " ".join(str(button.text) for button in buttons)
        callbacks = " ".join(str(button.callback_data) for button in buttons)
        self.assertNotIn("Boost", labels)
        self.assertNotIn("boost", callbacks.lower())

    async def test_boost_admin_screen_hides_env_variable_list(self):
        captured = {}
        settings = {
            "boost_enabled": True,
            "default_service_id": 123,
            "default_quantity": 500,
            "last_error": None,
        }

        async def fake_answer_or_send(qm, text, kb):
            captured["text"] = text

        with patch.object(ui.accounts, "is_superadmin", return_value=True), \
                patch.object(ui, "get_boost_settings", return_value=settings), \
                patch.object(ui, "list_tracked_channels", return_value=[]), \
                patch.object(ui, "boost_configured", return_value=True), \
                patch.object(ui, "boost_real_orders_allowed", return_value=False), \
                patch.object(ui, "boost_status", return_value="dry_run"), \
                patch.object(ui, "_answer_or_send", side_effect=fake_answer_or_send):
            await ui.screen_boost_admin(FakeQuery(100), SimpleNamespace(user_data={}))

        self.assertNotIn("TWIBOOST_API_KEY", captured["text"])
        self.assertNotIn("Переменные окружения", captured["text"])
        self.assertIn("Реальные заказы сейчас НЕ отправляются", captured["text"])

    async def test_queue_screen_has_publish_all_button(self):
        captured = {}
        channels = [
            {"channel_id": "@one", "name": "One"},
            {"channel_id": "@two", "name": "Two"},
        ]

        async def fake_answer_or_send(qm, text, kb):
            captured["kb"] = kb

        with patch.object(ui, "_load_channels", return_value=channels), \
                patch.object(ui.buffer, "get_level", side_effect=[1, 0]), \
                patch.object(ui, "_answer_or_send", side_effect=fake_answer_or_send):
            await ui.screen_queue(FakeQuery(100), SimpleNamespace(user_data={}))

        callbacks = [
            button.callback_data
            for row in captured["kb"].inline_keyboard
            for button in row
        ]
        self.assertIn("ui:queue_publish_all", callbacks)

    async def test_queue_screen_filters_channels_by_selected_folder(self):
        captured = {}
        channels = [
            {"channel_id": "@one", "name": "One", "folder": "РСЯ"},
            {"channel_id": "@two", "name": "Two", "folder": "Anime"},
            {"channel_id": "@three", "name": "Three"},
        ]
        context = SimpleNamespace(user_data={"queuefolder": "Anime"})

        async def fake_answer_or_send(qm, text, kb):
            captured["text"] = text
            captured["kb"] = kb

        with patch.object(ui, "_load_channels", return_value=channels), \
                patch.object(ui.buffer, "get_level", return_value=2), \
                patch.object(ui, "_answer_or_send", side_effect=fake_answer_or_send):
            await ui.screen_queue(FakeQuery(100), context)

        callbacks = [
            button.callback_data
            for row in captured["kb"].inline_keyboard
            for button in row
        ]
        self.assertIn("ui:ch_review:@two", callbacks)
        self.assertNotIn("ui:ch_review:@one", callbacks)
        self.assertNotIn("ui:ch_review:@three", callbacks)
        self.assertIn("Anime", captured["text"])

    async def test_views_monitor_screen_filters_channels_by_selected_folder(self):
        captured = {}
        channels = [
            {"channel_id": "@one", "name": "One", "folder": ui.VIEWS_MONITOR_DEFAULT_FOLDER, "active": True},
            {"channel_id": "@two", "name": "Two", "folder": "Anime", "views_monitor_enabled": True, "active": True},
            {"channel_id": "@three", "name": "Three", "active": True},
        ]
        context = SimpleNamespace(user_data={"viewsfolder": "Anime"})

        async def fake_answer_or_send(qm, text, kb):
            captured["text"] = text
            captured["kb"] = kb

        with patch.object(ui, "_load_channels", return_value=channels), \
                patch.object(ui, "_answer_or_send", side_effect=fake_answer_or_send):
            await ui.screen_admin_views_monitor(FakeQuery(100), context)

        labels = [
            button.text
            for row in captured["kb"].inline_keyboard
            for button in row
        ]
        self.assertTrue(any("Two" in label for label in labels))
        self.assertFalse(any("One" in label for label in labels))
        self.assertFalse(any("Three" in label for label in labels))
        self.assertIn("Anime", captured["text"])

    async def test_ensure_one_ready_post_keeps_existing_ready_post(self):
        with patch.object(ui.buffer, "get_ready_count", return_value=1), \
                patch.object(ui.generator, "run_for_channel", new=AsyncMock()) as gen_mock:
            source, reason = await ui._ensure_one_ready_post_for_channel({"channel_id": "@one"})

        self.assertEqual(source, "буфер")
        self.assertIsNone(reason)
        gen_mock.assert_not_called()

    async def test_queue_publish_all_publishes_once_per_channel(self):
        query = FakeQuery(100)
        context = SimpleNamespace(user_data={})
        channels = [
            {"channel_id": "@one", "name": "One"},
            {"channel_id": "@two", "name": "Two"},
        ]

        async def fake_answer_or_send(qm, text, kb):
            query.edits.append((text, None, kb))

        with patch.object(ui, "_load_channels", return_value=channels), \
                patch.object(ui, "_ensure_one_ready_post_for_channel", new=AsyncMock(side_effect=[
                    ("буфер", None),
                    ("генерация", None),
                ])), \
                patch.object(ui, "_answer_or_send", side_effect=fake_answer_or_send), \
                patch.object(ui.poster, "post_now", new=AsyncMock(return_value={"success": True})) as post_now_mock:
            await ui.action_queue_publish_all_run(query, context)

        self.assertEqual(post_now_mock.await_count, 2)
        post_now_mock.assert_any_await("@one")
        post_now_mock.assert_any_await("@two")
        self.assertNotIn("queue_publish_all_running", context.user_data)
        self.assertIn("Опубликовано: <b>2</b>", query.edits[-1][0])

    async def test_queue_publish_all_uses_selected_folder_scope(self):
        query = FakeQuery(100)
        context = SimpleNamespace(user_data={"queuefolder": "Anime"})
        channels = [
            {"channel_id": "@one", "name": "One", "folder": "РСЯ"},
            {"channel_id": "@two", "name": "Two", "folder": "Anime"},
        ]

        async def fake_answer_or_send(qm, text, kb):
            query.edits.append((text, None, kb))

        with patch.object(ui, "_load_channels", return_value=channels), \
                patch.object(ui, "_ensure_one_ready_post_for_channel", new=AsyncMock(return_value=("буфер", None))), \
                patch.object(ui, "_answer_or_send", side_effect=fake_answer_or_send), \
                patch.object(ui.poster, "post_now", new=AsyncMock(return_value={"success": True})) as post_now_mock:
            await ui.action_queue_publish_all_run(query, context)

        post_now_mock.assert_awaited_once_with("@two")
        self.assertIn("Anime", query.edits[-1][0])

    async def test_channel_diagnostics_rejects_foreign_owner(self):
        captured = {}
        channel = {"channel_id": "@kids", "owner_id": 200, "archetype": "kids_education"}

        async def fake_answer_or_send(qm, text, kb):
            captured["text"] = text

        with patch.object(ui, "_load_channel", return_value=channel), \
                patch.object(ui.accounts, "is_superadmin", return_value=False), \
                patch.object(ui, "_answer_or_send", side_effect=fake_answer_or_send):
            await ui.screen_channel_diagnostics(FakeQuery(100), SimpleNamespace(user_data={}), "@kids")

        self.assertIn("Нет доступа", captured["text"])

    async def test_channel_diagnostics_allows_superadmin(self):
        captured = {}
        channel = {
            "channel_id": "@kids",
            "owner_id": 200,
            "name": "Kids",
            "archetype": "kids_education",
            "channel_dna": {
                "audience": "родители",
                "known_facts": {
                    "age_groups": [{"age": "4–6", "directions": ["Lego WeDo"]}],
                    "city": "Владивосток",
                },
                "unknown_facts": ["free_trial"],
            },
        }

        async def fake_answer_or_send(qm, text, kb):
            captured["text"] = text
            captured["kb"] = kb

        with patch.object(ui, "_load_channel", return_value=channel), \
                patch.object(ui.accounts, "is_superadmin", return_value=True), \
                patch.object(ui, "_answer_or_send", side_effect=fake_answer_or_send):
            await ui.screen_channel_diagnostics(FakeQuery(100), SimpleNamespace(user_data={}), "@kids")

        self.assertIn("Диагностика канала", captured["text"])
        self.assertIn("4–6", captured["text"])

    async def test_deleted_channels_screen_lists_channels_only(self):
        captured = {}
        channels = [
            {"channel_id": "@gone", "name": "Gone", "owner_id": 100, "active": False},
        ]

        async def fake_answer_or_send(qm, text, kb):
            captured["text"] = text
            captured["kb"] = kb

        with patch.object(ui, "_load_channels", return_value=channels), \
                patch.object(ui, "_answer_or_send", side_effect=fake_answer_or_send):
            await ui.screen_channels_deleted(FakeQuery(100), SimpleNamespace(user_data={}), page=0)

        callbacks = [
            button.callback_data
            for row in captured["kb"].inline_keyboard
            for button in row
        ]
        self.assertIn("ui:ch_deleted_card:@gone", callbacks)
        self.assertNotIn("ui:ch_restore:@gone", callbacks)
        self.assertNotIn("ui:ch_purge:@gone", callbacks)
        self.assertIn("Выбери канал", captured["text"])

    async def test_deleted_channel_card_offers_restore_and_purge(self):
        captured = {}
        channel = {"channel_id": "@gone", "name": "Gone", "owner_id": 100, "active": False}

        async def fake_answer_or_send(qm, text, kb):
            captured["text"] = text
            captured["kb"] = kb

        with patch.object(ui, "_load_channel", return_value=channel), \
                patch.object(ui, "_answer_or_send", side_effect=fake_answer_or_send):
            await ui.screen_deleted_channel_actions(FakeQuery(100), SimpleNamespace(user_data={}), "@gone")

        callbacks = [
            button.callback_data
            for row in captured["kb"].inline_keyboard
            for button in row
        ]
        self.assertIn("ui:ch_restore:@gone", callbacks)
        self.assertIn("ui:ch_purge:@gone", callbacks)
        self.assertIn("Удалённый канал", captured["text"])

    def test_permanent_delete_channel_removes_database_rows_and_json(self):
        with tempfile.TemporaryDirectory(ignore_cleanup_errors=True) as tmp:
            root = Path(tmp)
            database = Database(root / "purge.db")
            database.init()
            channels_dir = root / "channels"
            channels_dir.mkdir()
            channel_json = channels_dir / "gone.json"
            channel_json.write_text(
                json.dumps({"channel_id": "@gone", "active": False}, ensure_ascii=False),
                encoding="utf-8",
            )

            with database.connect() as conn:
                conn.execute(
                    "INSERT INTO channels (tg_handle, name, topic, tone, config_json, active) VALUES (?, ?, ?, ?, ?, 0)",
                    ("@gone", "Gone", "topic", "tone", "{}"),
                )
                conn.execute(
                    "INSERT INTO posts (id, channel_id, content, status, generated_at) VALUES (?, ?, ?, ?, ?)",
                    ("p1", "@gone", "content", "ready", "2026-06-07T00:00:00+00:00"),
                )
                conn.execute(
                    "INSERT INTO processed_ads (id, channel_id, ad_message_id, detected_at, status) VALUES (?, ?, ?, ?, ?)",
                    ("ad1", "@gone", 1, "2026-06-07T00:00:00+00:00", "detected"),
                )
                conn.execute(
                    "INSERT INTO topic_cache (channel_id, topic, created_at, used) VALUES (?, ?, ?, 0)",
                    ("@gone", "topic", "2026-06-07T00:00:00+00:00"),
                )
                conn.execute(
                    "INSERT INTO evergreen_topics (channel_id, topic, last_used_at, use_count) VALUES (?, ?, ?, 0)",
                    ("@gone", "evergreen", None),
                )
                conn.execute(
                    "INSERT INTO error_log (channel_id, error_type, message, occurred_at) VALUES (?, ?, ?, ?)",
                    ("@gone", "test", "error", "2026-06-07T00:00:00+00:00"),
                )
                boost_cur = conn.execute(
                    """
                    INSERT INTO boost_channels (
                        channel_key, smm_channel_id, enabled, created_at, updated_at
                    ) VALUES (?, ?, 1, ?, ?)
                    """,
                    ("user:gone", "@gone", "2026-06-07T00:00:00+00:00", "2026-06-07T00:00:00+00:00"),
                )
                boost_id = boost_cur.lastrowid
                conn.execute(
                    """
                    INSERT INTO boost_orders (
                        boost_channel_id, message_id, event_type, quantity, status, dry_run, created_at, updated_at
                    ) VALUES (?, 10, 'post', 500, 'dry_run', 1, ?, ?)
                    """,
                    (boost_id, "2026-06-07T00:00:00+00:00", "2026-06-07T00:00:00+00:00"),
                )
                conn.commit()

            counts = ui._permanently_delete_channel("@gone", database=database, channels_dir=channels_dir)

            self.assertEqual(counts["posts"], 1)
            self.assertEqual(counts["processed_ads"], 1)
            self.assertEqual(counts["topic_cache"], 1)
            self.assertEqual(counts["evergreen_topics"], 1)
            self.assertEqual(counts["error_log"], 1)
            self.assertEqual(counts["channels"], 1)
            self.assertEqual(counts["boost_orders"], 1)
            self.assertEqual(counts["boost_channels"], 1)
            self.assertEqual(counts["json_deleted"], 1)
            self.assertFalse(channel_json.exists())

            with database.connect() as conn:
                for table, field in (
                    ("channels", "tg_handle"),
                    ("posts", "channel_id"),
                    ("processed_ads", "channel_id"),
                    ("topic_cache", "channel_id"),
                    ("evergreen_topics", "channel_id"),
                    ("error_log", "channel_id"),
                ):
                    count = conn.execute(f"SELECT COUNT(*) FROM {table} WHERE {field} = ?", ("@gone",)).fetchone()[0]
                    self.assertEqual(count, 0, table)
                self.assertEqual(conn.execute("SELECT COUNT(*) FROM boost_channels").fetchone()[0], 0)
                self.assertEqual(conn.execute("SELECT COUNT(*) FROM boost_orders").fetchone()[0], 0)


if __name__ == "__main__":
    unittest.main()
