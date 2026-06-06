import unittest

from poster import Poster
from ui import (
    _msk_minutes_to_utc_entries,
    _parse_schedule_msk_text,
    _schedule_msk_minutes,
)


class ScheduleTimesTests(unittest.TestCase):
    def test_parse_hh_mm_and_hour_slots(self):
        minutes = _parse_schedule_msk_text("08:14 09:20 12")

        self.assertEqual(minutes, [8 * 60 + 14, 9 * 60 + 20, 12 * 60])
        self.assertEqual(_msk_minutes_to_utc_entries(minutes), ["05:14", "06:20", 9])

    def test_round_trip_old_and_new_storage(self):
        entries = [6, "05:14", "06:20"]

        self.assertEqual(_schedule_msk_minutes(entries), [8 * 60 + 14, 9 * 60, 9 * 60 + 20])
        self.assertEqual(Poster._schedule_utc_minutes(entries), {5 * 60 + 14, 6 * 60, 6 * 60 + 20})

    def test_invalid_time_input_is_rejected(self):
        for value in ("abc", "25:00", "09:60", "8-14", "1:2"):
            with self.subTest(value=value):
                with self.assertRaises(ValueError):
                    _parse_schedule_msk_text(value)


if __name__ == "__main__":
    unittest.main()
