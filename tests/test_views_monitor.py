import unittest
import tempfile
from datetime import datetime, timedelta, timezone
from pathlib import Path
from types import SimpleNamespace
from unittest.mock import AsyncMock, Mock, patch

import bot as bot_module
import buffer_manager as buffer_module
import views_monitor
from database import Database


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

    async def test_monitor_channel_views_saves_subscriber_cooldown_without_db_replace(self):
        channel = {"channel_id": "@rsy", "folder": views_monitor.DEFAULT_FOLDER}
        report = {"checked": 1, "rows": [], "flagged": [], "created_at": "2026-06-07T12:00:00+00:00"}
        alert_report = {
            "checked": 1,
            "rows": [],
            "flagged": [{"channel": channel, "subs_low": True, "low_posts": []}],
            "created_at": "2026-06-07T12:00:00+00:00",
        }
        fake_bot = SimpleNamespace(send_message=AsyncMock())

        with patch.object(bot_module, "load_all_channels", return_value=[channel]), \
                patch.object(bot_module, "collect_monitor_report", new=AsyncMock(return_value=report)), \
                patch.object(bot_module, "build_hourly_alert_report", return_value=(alert_report, [channel])), \
                patch.object(bot_module, "views_digest_text", return_value="alert"), \
                patch.object(bot_module, "save_channel_json_only") as json_save, \
                patch.object(bot_module, "save_channel_card") as db_save:
            await bot_module.monitor_channel_views(fake_bot)

        fake_bot.send_message.assert_awaited_once()
        json_save.assert_called_once_with(channel)
        db_save.assert_not_called()
        self.assertIn(views_monitor.SUBS_ALERT_LAST_FIELD, channel)

    def test_manual_post_covers_pending_rsy_overlay(self):
        tmp = tempfile.TemporaryDirectory(ignore_cleanup_errors=True)
        original_db = buffer_module.db
        try:
            buffer_module.db = Database(Path(tmp.name) / "rsy.db")
            buffer_module.db.init()
            self.assertTrue(buffer_module.buffer.record_pending_ad("@wb_bomb", 1001, "2026-06-07T03:15:00+00:00"))
            self.assertTrue(buffer_module.buffer.has_pending_overlay("@wb_bomb"))

            covered = buffer_module.buffer.cover_pending_overlay("@wb_bomb", "covered_manual")

            self.assertEqual(covered, 1)
            self.assertFalse(buffer_module.buffer.has_pending_overlay("@wb_bomb"))
            with buffer_module.db.connect() as conn:
                row = conn.execute("SELECT status FROM processed_ads WHERE channel_id = ?", ("@wb_bomb",)).fetchone()
            self.assertEqual(row["status"], "covered_manual")
        finally:
            buffer_module.db = original_db
            tmp.cleanup()

    async def test_due_rsy_overlay_skips_when_recent_post_exists(self):
        fake_buffer = SimpleNamespace(
            get_due_ads=lambda now_iso: [{"id": "ad-1", "channel_id": "@wb_bomb", "due_at": now_iso}],
            mark_ad_failed=Mock(),
            get_ready_count=Mock(side_effect=AssertionError("should not publish")),
        )
        fake_poster = SimpleNamespace(
            minutes_since_published=lambda channel_id: 3,
            post_now=AsyncMock(),
        )
        fake_bot = SimpleNamespace(send_message=AsyncMock())

        with patch.object(bot_module, "buffer", fake_buffer), \
                patch.object(bot_module, "poster", fake_poster):
            await bot_module.process_due_ads(fake_bot)

        fake_buffer.mark_ad_failed.assert_called_once_with("ad-1", "covered_recent_post")
        fake_poster.post_now.assert_not_awaited()
        fake_bot.send_message.assert_not_awaited()

    async def test_rsy_channel_post_matches_by_chat_id_num(self):
        channel = {
            "channel_id": "@old_handle",
            "username": "@new_handle",
            "chat_id_num": -1001234567890,
            "rsy_override": True,
        }
        message = SimpleNamespace(
            chat=SimpleNamespace(id=-1001234567890, username="new_handle"),
            message_id=77,
            text="Реклама erid: test",
            caption=None,
            entities=[],
            caption_entities=[],
        )
        update = SimpleNamespace(channel_post=message)
        fake_buffer = SimpleNamespace(record_pending_ad=Mock(return_value=True))

        with patch.object(bot_module, "handle_boost_channel_post_dry_run", new=AsyncMock(return_value={})), \
                patch.object(bot_module, "load_all_channels", return_value=[channel]), \
                patch.object(bot_module, "buffer", fake_buffer):
            await bot_module.handle_channel_post(update, SimpleNamespace())

        fake_buffer.record_pending_ad.assert_called_once()
        args = fake_buffer.record_pending_ad.call_args.args
        self.assertEqual(args[0], "@old_handle")
        self.assertEqual(args[1], 77)


if __name__ == "__main__":
    unittest.main()
