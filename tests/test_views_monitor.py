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

    def test_build_hourly_alert_report_throttles_subscriber_alerts(self):
        now = datetime(2026, 6, 7, 12, 0, 0, tzinfo=timezone.utc)
        due = {"channel_id": "@due", "folder": views_monitor.DEFAULT_FOLDER}
        recent = {
            "channel_id": "@recent",
            "folder": views_monitor.DEFAULT_FOLDER,
            views_monitor.SUBS_ALERT_LAST_FIELD: (now - timedelta(hours=2)).isoformat(),
        }
        other_folder = {"channel_id": "@other", "folder": "Other"}
        low_views = {
            "channel_id": "@views",
            "folder": views_monitor.DEFAULT_FOLDER,
            views_monitor.SUBS_ALERT_LAST_FIELD: now.isoformat(),
        }
        report = {
            "checked": 4,
            "rows": [
                {"channel": due, "subs_low": True, "low_posts": []},
                {"channel": recent, "subs_low": True, "low_posts": []},
                {"channel": other_folder, "subs_low": True, "low_posts": []},
                {"channel": low_views, "subs_low": True, "low_posts": [{"id": 10}]},
            ],
            "flagged": [],
            "created_at": now.isoformat(),
        }

        alert_report, to_mark = views_monitor.build_hourly_alert_report(report, now)

        self.assertEqual([ch["channel_id"] for ch in to_mark], ["@due"])
        self.assertEqual([row["channel"]["channel_id"] for row in alert_report["flagged"]], ["@due", "@views"])
        self.assertTrue(alert_report["flagged"][0]["subs_low"])
        self.assertFalse(alert_report["flagged"][1]["subs_low"])

    def test_mark_subscriber_alert_sent_sets_timestamp(self):
        now = datetime(2026, 6, 7, 12, 0, 0, tzinfo=timezone.utc)
        ch = {"channel_id": "@rsy", "folder": views_monitor.DEFAULT_FOLDER}

        views_monitor.mark_subscriber_alert_sent(ch, now)

        self.assertEqual(ch[views_monitor.SUBS_ALERT_LAST_FIELD], now.isoformat())
        self.assertFalse(views_monitor.subscriber_alert_due(ch, now + timedelta(hours=23)))
        self.assertTrue(views_monitor.subscriber_alert_due(ch, now + timedelta(hours=24)))


if __name__ == "__main__":
    unittest.main()
