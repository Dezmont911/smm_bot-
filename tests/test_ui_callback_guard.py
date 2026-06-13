import asyncio
import os
import unittest

for _k, _v in {"ANTHROPIC_API_KEY": "test-key", "BOT_TOKEN": "123:test", "ADMIN_CHAT_ID": "1"}.items():
    if not os.environ.get(_k):
        os.environ[_k] = _v

import ui


class _User:
    id = 42


class _Message:
    chat_id = 100


class _Query:
    from_user = _User()
    message = _Message()


class UiCallbackGuardTest(unittest.IsolatedAsyncioTestCase):
    def setUp(self):
        ui._UI_CALLBACK_LOCKS.clear()
        ui._UI_CALLBACK_LAST_DONE.clear()

    async def test_blocks_parallel_click_for_same_user(self):
        query = _Query()

        allowed, lock, msg = await ui._enter_ui_callback(query)
        self.assertTrue(allowed)
        self.assertIsNone(msg)

        allowed2, lock2, msg2 = await ui._enter_ui_callback(query)
        self.assertFalse(allowed2)
        self.assertIsNone(lock2)
        self.assertIn("предыдущее действие", msg2)

        ui._finish_ui_callback(query, lock)

    async def test_blocks_immediate_repeat_after_finish(self):
        query = _Query()

        allowed, lock, _ = await ui._enter_ui_callback(query)
        self.assertTrue(allowed)
        ui._finish_ui_callback(query, lock)

        allowed2, lock2, msg2 = await ui._enter_ui_callback(query)
        self.assertFalse(allowed2)
        self.assertIsNone(lock2)
        self.assertIn("Слишком быстро", msg2)

    async def test_allows_after_cooldown(self):
        query = _Query()

        allowed, lock, _ = await ui._enter_ui_callback(query)
        self.assertTrue(allowed)
        ui._finish_ui_callback(query, lock)

        await asyncio.sleep(ui.UI_CALLBACK_COOLDOWN_SEC + 0.05)
        allowed2, lock2, msg2 = await ui._enter_ui_callback(query)
        self.assertTrue(allowed2)
        self.assertIsNone(msg2)
        ui._finish_ui_callback(query, lock2)


if __name__ == "__main__":
    unittest.main()
