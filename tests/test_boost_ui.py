import unittest
from types import SimpleNamespace
from unittest.mock import AsyncMock, patch

import ui


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


if __name__ == "__main__":
    unittest.main()
