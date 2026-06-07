import unittest
from datetime import datetime, timedelta, timezone
from types import SimpleNamespace
from unittest.mock import AsyncMock, patch

import views_monitor


class ViewsMonitorTests(unittest.IsolatedAsyncioTestCase):
    def test_select_monitored_channels_uses_rsy_default_and_manual_flags(self):
        channels = [
            {"channel_id": "@rsy", "folder": "РСЯ", "active": True},
            {"channel_id": "@manual", "folder": "Other", "views_monitor_enabled": True, "active": True},
            {"channel_id": "@off", "folder": "РСЯ", "views_monitor_enabled": False, "active": True},
            {"channel_id": "@other", "folder": "Other", "active": True},
            {"channel_id": "@inactive", "folder": "РСЯ", "active": False},
            {"channel_id": "@tester", "folder": "РСЯ", "owner_id": 999, "active": True},
        ]

        with patch.object(views_monitor.accounts, "is_admin", side_effect=lambda uid: uid == 100):
            selected = views_monitor.select_monitored_channels(channels)

        self.assertEqual([ch["channel_id"] for ch in selected], ["@rsy", "@manual"])

    async def test_collect_channel_snapshot_flags_only_18_to_50h_under_300_views(self):
        now = datetime(2026, 6, 7, 12, 0, 0, tzinfo=timezone.utc)
        posts = [
            {"id": 1, "date": (now - timedelta(hours=17)).strftime("%Y-%m-%dT%H:%M:%S"), "views": 10},
            {"id": 2, "date": (now - timedelta(hours=18)).strftime("%Y-%m-%dT%H:%M:%S"), "views": 299},
            {"id": 3, "date": (now - timedelta(hours=30)).strftime("%Y-%m-%dT%H:%M:%S"), "views": 300},
            {"id": 4, "date": (now - timedelta(hours=50)).strftime("%Y-%m-%dT%H:%M:%S"), "views": 250},
            {"id": 5, "date": (now - timedelta(hours=51)).strftime("%Y-%m-%dT%H:%M:%S"), "views": 1},
        ]
        bot = SimpleNamespace(get_chat_member_count=AsyncMock(return_value=2000))

        with patch.object(views_monitor.userbot_reader, "read_post_views", new=AsyncMock(return_value={"posts": posts})):
            row = await views_monitor.collect_channel_snapshot(bot, {"channel_id": "@rsy"}, now)

        self.assertEqual([post["id"] for post in row["low_posts"]], [2, 4])
        self.assertFalse(row["subs_low"])

    async def test_collect_channel_snapshot_flags_low_subscribers(self):
        now = datetime(2026, 6, 7, 12, 0, 0, tzinfo=timezone.utc)
        bot = SimpleNamespace(get_chat_member_count=AsyncMock(return_value=100))

        with patch.object(views_monitor.userbot_reader, "read_post_views", new=AsyncMock(return_value={"posts": []})):
            row = await views_monitor.collect_channel_snapshot(bot, {"channel_id": "@rsy"}, now)

        self.assertTrue(row["subs_low"])


if __name__ == "__main__":
    unittest.main()
