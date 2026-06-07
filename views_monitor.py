"""Monitoring of channel subscribers and post views."""

from __future__ import annotations

import html
from datetime import datetime, timezone
from typing import Any

from loguru import logger

import accounts
import userbot_reader


VIEWS_MIN = 300
SUBS_MIN = 1550
WATCH_MIN_H = 18
WATCH_MAX_H = 50
POST_LIMIT = 25
DEFAULT_FOLDER = "РСЯ"
SUBS_ALERT_INTERVAL_H = 24
SUBS_ALERT_LAST_FIELD = "views_monitor_last_subs_alert_utc"


def is_tester_channel(ch: dict) -> bool:
    oid = ch.get("owner_id")
    return oid is not None and not accounts.is_admin(oid)


def monitor_enabled(ch: dict) -> bool:
    value = ch.get("views_monitor_enabled")
    if value is True:
        return True
    if value is False:
        return False
    return (ch.get("folder") or "").strip().casefold() == DEFAULT_FOLDER.casefold()


def is_default_folder(ch: dict) -> bool:
    return (ch.get("folder") or "").strip().casefold() == DEFAULT_FOLDER.casefold()


def select_monitored_channels(channels: list[dict]) -> list[dict]:
    return [
        ch for ch in channels
        if ch.get("active", True) and not is_tester_channel(ch) and monitor_enabled(ch)
    ]


def channel_username(ch: dict) -> str | None:
    username = (ch.get("username") or "").strip().lstrip("@")
    if username:
        return username
    cid = (ch.get("channel_id") or "").strip().lstrip("@")
    if cid and not cid.lstrip("-").isdigit():
        return cid
    return None


def channel_link(ch: dict) -> str | None:
    username = channel_username(ch)
    if username:
        return f"https://t.me/{username}"
    num = str(ch.get("chat_id_num") or "")
    if num.startswith("-100"):
        return f"https://t.me/c/{num[4:]}"
    return None


def post_link(ch: dict, msg_id: int) -> str | None:
    username = channel_username(ch)
    if username:
        return f"https://t.me/{username}/{msg_id}"
    num = str(ch.get("chat_id_num") or "")
    if num.startswith("-100"):
        return f"https://t.me/c/{num[4:]}/{msg_id}"
    return None


def _parse_post_dt(value: Any):
    if not value:
        return None
    try:
        return datetime.strptime(str(value), "%Y-%m-%dT%H:%M:%S").replace(tzinfo=timezone.utc)
    except Exception:
        return None


def _parse_iso_dt(value: Any):
    if not value:
        return None
    try:
        text = str(value)
        if text.endswith("Z"):
            text = text[:-1] + "+00:00"
        dt = datetime.fromisoformat(text)
        if dt.tzinfo is None:
            dt = dt.replace(tzinfo=timezone.utc)
        return dt.astimezone(timezone.utc)
    except Exception:
        return None


def subscriber_alert_due(ch: dict, now: datetime | None = None) -> bool:
    if not is_default_folder(ch):
        return False
    now = now or datetime.now(timezone.utc)
    last = _parse_iso_dt(ch.get(SUBS_ALERT_LAST_FIELD))
    if last is None:
        return True
    return (now - last).total_seconds() >= SUBS_ALERT_INTERVAL_H * 3600


def mark_subscriber_alert_sent(ch: dict, now: datetime | None = None) -> None:
    now = now or datetime.now(timezone.utc)
    ch[SUBS_ALERT_LAST_FIELD] = now.astimezone(timezone.utc).isoformat()


async def collect_channel_snapshot(bot, ch: dict, now: datetime | None = None) -> dict:
    now = now or datetime.now(timezone.utc)
    handle = ch.get("channel_id")
    target = ch.get("chat_id_num") or handle
    row = {
        "channel": ch,
        "subs": None,
        "subs_error": None,
        "posts_error": None,
        "posts": [],
        "low_posts": [],
        "subs_low": False,
    }

    try:
        row["subs"] = await bot.get_chat_member_count(target)
    except Exception as e:
        row["subs_error"] = str(e)
        logger.debug(f"views-monitor: нет числа подписчиков {handle}: {e}")

    if row["subs"] is not None:
        row["subs_low"] = row["subs"] < SUBS_MIN

    try:
        data = await userbot_reader.read_post_views(handle, limit=POST_LIMIT)
        for post in data.get("posts", []):
            dt = _parse_post_dt(post.get("date"))
            views = post.get("views")
            age_h = (now - dt).total_seconds() / 3600 if dt else None
            item = {
                "id": post.get("id"),
                "date": post.get("date"),
                "views": views,
                "age_h": age_h,
            }
            row["posts"].append(item)
            if (
                item["id"] is not None
                and views is not None
                and age_h is not None
                and WATCH_MIN_H <= age_h <= WATCH_MAX_H
                and views < VIEWS_MIN
            ):
                row["low_posts"].append(item)
    except Exception as e:
        row["posts_error"] = str(e)
        logger.debug(f"views-monitor: нет просмотров {handle}: {e}")

    return row


async def collect_monitor_report(bot, channels: list[dict]) -> dict:
    selected = select_monitored_channels(channels)
    now = datetime.now(timezone.utc)
    rows = []
    for ch in selected:
        if not ch.get("channel_id"):
            continue
        rows.append(await collect_channel_snapshot(bot, ch, now))
    flagged = [row for row in rows if row.get("subs_low") or row.get("low_posts")]
    return {"checked": len(rows), "rows": rows, "flagged": flagged, "created_at": now.isoformat()}


def build_hourly_alert_report(report: dict, now: datetime | None = None) -> tuple[dict, list[dict]]:
    """Keep hourly view alerts, but throttle subscriber alerts to once per 24h."""
    now = now or datetime.now(timezone.utc)
    flagged = []
    subs_channels_to_mark = []
    for row in report.get("rows") or []:
        low_posts = bool(row.get("low_posts"))
        subs_due = bool(row.get("subs_low")) and subscriber_alert_due(row.get("channel") or {}, now)
        if not low_posts and not subs_due:
            continue
        alert_row = dict(row)
        alert_row["subs_low"] = subs_due
        flagged.append(alert_row)
        if subs_due:
            subs_channels_to_mark.append(row["channel"])
    alert_report = dict(report)
    alert_report["flagged"] = flagged
    return alert_report, subs_channels_to_mark


def _channel_title(ch: dict) -> str:
    name = html.escape(ch.get("name") or ch.get("channel_id") or "?")
    link = channel_link(ch)
    return f'<a href="{link}">{name}</a>' if link else f"<b>{name}</b>"


def digest_text(report: dict, *, manual: bool = False) -> str:
    flagged = report.get("flagged") or []
    checked = int(report.get("checked") or 0)
    title = "📊 <b>Мониторинг охватов</b>"
    subtitle = (
        f"Порог: посты {WATCH_MIN_H}-{WATCH_MAX_H}ч с &lt;{VIEWS_MIN} просмотров "
        f"или канал с &lt;{SUBS_MIN} подписчиков."
    )
    lines = [title, subtitle, f"Проверено каналов: <b>{checked}</b>"]

    if not flagged:
        lines.append("\n✅ Проблем по отслеживаемым каналам сейчас нет.")
        if manual:
            lines.extend(_manual_summary_lines(report))
        return "\n".join(lines)

    lines.append(f"\nПроблемных каналов: <b>{len(flagged)}</b>")
    for row in flagged[:20]:
        ch = row["channel"]
        subs = row.get("subs")
        subs_s = f"{subs} подписчиков" if subs is not None else "подписчики ?"
        mark = " ⚠️" if row.get("subs_low") else ""
        lines.append(f"• {_channel_title(ch)} — {subs_s}{mark}")
        for post in (row.get("low_posts") or [])[:8]:
            msg_id = int(post["id"])
            plink = post_link(ch, msg_id)
            label = f"пост #{msg_id}"
            link = f'<a href="{plink}">{label}</a>' if plink else label
            age = post.get("age_h")
            age_s = f"{age:.0f}ч" if isinstance(age, (int, float)) else "?ч"
            lines.append(f"   📉 {link} — {post.get('views')} просмотров · {age_s}")
    if len(flagged) > 20:
        lines.append(f"\n…и ещё {len(flagged) - 20} каналов")

    if manual:
        lines.extend(_manual_summary_lines(report))
    return "\n".join(lines)


def _manual_summary_lines(report: dict) -> list[str]:
    rows = report.get("rows") or []
    if not rows:
        return []
    lines = ["", "<b>Кратко по отслеживаемым каналам:</b>"]
    for row in rows[:25]:
        ch = row["channel"]
        posts = row.get("posts") or []
        latest = posts[0] if posts else None
        latest_s = "последних постов нет"
        if latest:
            views = latest.get("views")
            age = latest.get("age_h")
            age_s = f"{age:.0f}ч" if isinstance(age, (int, float)) else "?ч"
            latest_s = f"последний #{latest.get('id')}: {views if views is not None else '?'} просмотров · {age_s}"
        problems = len(row.get("low_posts") or [])
        lines.append(f"• {_channel_title(ch)} — {latest_s}; проблемных постов: {problems}")
    if len(rows) > 25:
        lines.append(f"…и ещё {len(rows) - 25} каналов")
    return lines
