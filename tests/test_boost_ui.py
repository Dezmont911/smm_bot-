import unittest
from types import SimpleNamespace
from unittest.mock import patch

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
        self.assertEqual(ui._boost_status_label("dry_run"), "тестовый режим")
        self.assertEqual(ui._boost_status_label("ignored"), "пропущен")
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
        self.assertIn("фото", captured["text"])
        self.assertIn("тестовый режим", captured["text"])
        self.assertIn("https://t.me/boosted/42", captured["text"])

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


if __name__ == "__main__":
    unittest.main()
