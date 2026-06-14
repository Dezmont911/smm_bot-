import os
import unittest
from types import SimpleNamespace
from unittest.mock import patch

for _k, _v in {"ANTHROPIC_API_KEY": "test-key", "BOT_TOKEN": "123:test", "ADMIN_CHAT_ID": "1"}.items():
    if not os.environ.get(_k):
        os.environ[_k] = _v

import ui


class FakeSent:
    chat_id = 100
    message_id = 501


class FakeMessage:
    def __init__(self, text="hello"):
        self.chat_id = 100
        self.message_id = 10
        self.text = text
        self.caption = None
        self.photo = []
        self.animation = None
        self.video = None
        self.document = None
        self.media_group_id = None
        self.deleted = False
        self.replies = []

    async def reply_text(self, text, **kwargs):
        self.replies.append((text, kwargs))
        return FakeSent()

    async def delete(self):
        self.deleted = True


class FakeBuffer:
    def __init__(self):
        self.posts = []

    def add(self, post):
        post_id = f"draft-{len(self.posts) + 1}"
        stored = dict(post)
        stored["id"] = post_id
        self.posts.append(stored)
        return post_id

    def get_drafts(self, handle):
        return [p for p in self.posts if p.get("channel_id") == handle and p.get("status") == "draft"]


class DraftComposeModeTests(unittest.IsolatedAsyncioTestCase):
    async def test_single_manual_draft_turns_compose_off_and_replies_with_card(self):
        msg = FakeMessage("manual post")
        context = SimpleNamespace(user_data={"draft_compose": "@chan"})
        fake_buffer = FakeBuffer()

        with (
            patch.object(ui, "buffer", fake_buffer),
            patch.object(ui, "_load_channel", return_value={"channel_id": "@chan", "channel_type": "general"}),
            patch.object(ui, "_manual_post_duplicate", return_value=False),
        ):
            handled = await ui.create_draft_from_message(SimpleNamespace(message=msg), context)

        self.assertTrue(handled)
        self.assertNotIn("draft_compose", context.user_data)
        self.assertNotIn("draft_compose_batch", context.user_data)
        self.assertTrue(msg.deleted)
        self.assertEqual(len(fake_buffer.posts), 1)
        self.assertEqual(len(msg.replies), 1)
        reply_markup = msg.replies[0][1].get("reply_markup")
        labels = [button.text for row in reply_markup.inline_keyboard for button in row]
        self.assertIn("📤 В очередь", labels)
        self.assertIn("➕ Ещё пост", labels)

    async def test_batch_manual_draft_keeps_compose_on_for_more_messages(self):
        msg = FakeMessage("batch post")
        context = SimpleNamespace(user_data={"draft_compose": "@chan", "draft_compose_batch": True})
        fake_buffer = FakeBuffer()

        with (
            patch.object(ui, "buffer", fake_buffer),
            patch.object(ui, "_load_channel", return_value={"channel_id": "@chan", "channel_type": "general"}),
            patch.object(ui, "_manual_post_duplicate", return_value=False),
        ):
            handled = await ui.create_draft_from_message(SimpleNamespace(message=msg), context)

        self.assertTrue(handled)
        self.assertEqual(context.user_data.get("draft_compose"), "@chan")
        self.assertTrue(context.user_data.get("draft_compose_batch"))
        self.assertTrue(msg.deleted)
        self.assertEqual(len(fake_buffer.posts), 1)
        self.assertEqual(len(msg.replies), 0)


if __name__ == "__main__":
    unittest.main()
